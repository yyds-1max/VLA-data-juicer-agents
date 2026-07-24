"""Stopped-service operations boundary for Annotation recovery.

This module intentionally exposes no HTTP surface and implements no recovery
state transitions of its own.  It validates an explicit database/lock scope,
then delegates every mutation to :class:`AnnotationStore`.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any

from vla_data_juicer_agents.annotation.maintenance import (
    AnnotationMaintenanceError,
    AnnotationServiceOnlineError,
    acquire_annotation_maintenance,
)
from vla_data_juicer_agents.annotation.models import (
    AnnotationConflictError,
    AnnotationNotFoundError,
    AnnotationValidationError,
)
from vla_data_juicer_agents.annotation.store import AnnotationStore
from vla_data_juicer_agents.navigation.writer_lock import (
    NavigationWriterLockError,
    NavigationWriterQuarantinedError,
    configured_writer_lock_path,
    navigation_writer_marker_state,
)


GLOBAL_CONFIRMATION = "all_navigation_annotation_writer_process_groups_absent"
JOB_CONFIRMATION = "old_process_group_absent"

_JOB_REF = re.compile(r"^job_[0-9a-f]{32}$")
_ERROR_REF = re.compile(r"^annotation_error_[0-9a-f]{32}$")
_ACTION_REF = re.compile(r"^writer_quarantine_action_[0-9a-f]{32}$")
_DATE = re.compile(r"^[0-9]{8}$")
_SAFE_ERROR_CODE = re.compile(r"^[a-z0-9_]{1,100}$")
_POSIX_ABSOLUTE_PATH = re.compile(
    r"(?<![A-Za-z0-9_.-])/(?!/)[^\s\"'<>|,;:)}\]]+"
    r"(?:/[^\s\"'<>|,;:)}\]]+)*",
)
_WINDOWS_ABSOLUTE_PATH = re.compile(
    r"(?<![A-Za-z0-9_.-])[A-Za-z]:\\[^\s\"'<>|,;)}\]]+",
)
_SECRET = re.compile(
    r"(?i)(?:\bbearer\s+[a-z0-9._~+/=-]{8,}|"
    r"(?<![a-z0-9])(?:sk-[a-z0-9_-]{12,}|"
    r"dashscope[-_a-z0-9]{12,}))",
)


class _OperatorCLIUsageError(ValueError):
    pass


class _OperatorScopeError(ValueError):
    pass


class _UnsafeProjectionError(RuntimeError):
    pass


class _SafeArgumentParser(argparse.ArgumentParser):
    """Do not echo rejected operator input into terminal or service logs."""

    def error(self, message: str) -> None:
        del message
        raise _OperatorCLIUsageError("invalid operator CLI arguments")


def _nonnegative_revision(value: str) -> int:
    try:
        revision = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("revision must be an integer") from exc
    if revision < 0:
        raise argparse.ArgumentTypeError("revision must be non-negative")
    return revision


def build_parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(
        prog="vla-annotation-operator",
        description=(
            "Inspect and resolve Annotation recovery while the Web service is stopped."
        ),
    )
    parser.add_argument(
        "--annotation-db",
        type=Path,
        required=True,
        help="Explicit absolute path to the existing annotation database.",
    )
    parser.add_argument(
        "--writer-lock",
        type=Path,
        required=True,
        help="Explicit absolute path to the shared Navigation/Annotation writer lock.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser(
        "list-recovery",
        help="List public recovery state without mutating the database or markers.",
    )

    clear_global = subparsers.add_parser(
        "clear-global",
        help="Record and clear the exact global writer recovery marker set.",
    )
    clear_global.add_argument("--confirmation", required=True)
    clear_global.add_argument("--operator-reference", required=True)
    clear_global.add_argument("--idempotency-key", required=True)

    confirm_job = subparsers.add_parser(
        "confirm-job",
        help="Resolve one quarantined Annotation Job using an audited Store action.",
    )
    confirm_job.add_argument("disposition", choices=("retry", "abandon"))
    confirm_job.add_argument("--job-ref", required=True)
    confirm_job.add_argument(
        "--expected-job-revision",
        type=_nonnegative_revision,
        required=True,
    )
    confirm_job.add_argument("--global-action-ref", required=True)
    confirm_job.add_argument("--confirmation", required=True)
    confirm_job.add_argument("--operator-reference", required=True)
    confirm_job.add_argument("--idempotency-key", required=True)
    return parser


def _require_explicit_absolute_paths(
    annotation_db: Path,
    writer_lock: Path,
) -> None:
    if not annotation_db.is_absolute() or not writer_lock.is_absolute():
        raise _OperatorCLIUsageError(
            "annotation database and writer lock paths must be absolute",
        )


def _read_store(annotation_db: Path) -> AnnotationStore:
    return AnnotationStore.open_existing_read_only(annotation_db)


def _mutation_store(annotation_db: Path) -> AnnotationStore:
    return AnnotationStore.open_existing_mutable(annotation_db)


def _bind_production_scope(
    annotation_db: Path,
    writer_lock: Path,
) -> tuple[Path, Path]:
    configured_working_dir = os.getenv("VLA_DATA_AGENT_WEB_WORKING_DIR")
    if not configured_working_dir:
        raise _OperatorScopeError(
            "the DataPilot working directory must be configured explicitly",
        )
    working_dir = Path(configured_working_dir)
    if not working_dir.is_absolute():
        raise _OperatorScopeError(
            "the DataPilot working directory must be absolute",
        )
    try:
        metadata = working_dir.lstat()
        canonical_working_dir = working_dir.resolve(strict=True)
    except OSError as exc:
        raise _OperatorScopeError(
            "the DataPilot working directory is unavailable",
        ) from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or canonical_working_dir != working_dir
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise _OperatorScopeError(
            "the DataPilot working directory is unsafe",
        )
    expected_database = canonical_working_dir / "annotation.sqlite"
    if annotation_db != expected_database:
        raise _OperatorScopeError(
            "the annotation database does not match the production scope",
        )

    configured_lock = configured_writer_lock_path()
    if writer_lock != configured_lock:
        raise _OperatorScopeError(
            "the writer lock does not match the production scope",
        )
    return expected_database, configured_lock


def _safe_recovery_job(job: dict[str, Any]) -> dict[str, Any]:
    job_ref = job.get("job_ref")
    dataset_date = job.get("dataset_date")
    state_revision = job.get("state_revision")
    failure = job.get("failure")
    if (
        not isinstance(job_ref, str)
        or _JOB_REF.fullmatch(job_ref) is None
        or not isinstance(dataset_date, str)
        or _DATE.fullmatch(dataset_date) is None
        or not isinstance(state_revision, int)
        or isinstance(state_revision, bool)
        or state_revision < 0
        or not isinstance(failure, dict)
        or failure.get("code") != "recovery_required"
    ):
        raise _UnsafeProjectionError("unsafe recovery job projection")
    error_ref = failure.get("error_ref")
    if (
        not isinstance(error_ref, str)
        or _ERROR_REF.fullmatch(error_ref) is None
    ):
        raise _UnsafeProjectionError("unsafe recovery error reference")
    return {
        "job_ref": job_ref,
        "dataset_date": dataset_date,
        "status": "failed",
        "state_revision": state_revision,
        "cancel_requested": bool(job.get("cancel_requested")),
        "error_ref": error_ref,
    }


def _safe_job_result(job: dict[str, Any]) -> dict[str, Any]:
    job_ref = job.get("job_ref")
    status = job.get("status")
    state_revision = job.get("state_revision")
    if (
        not isinstance(job_ref, str)
        or _JOB_REF.fullmatch(job_ref) is None
        or status not in {"preparing", "tracking", "cancelled"}
        or not isinstance(state_revision, int)
        or isinstance(state_revision, bool)
        or state_revision < 0
    ):
        raise _UnsafeProjectionError("unsafe operator action projection")
    result: dict[str, Any] = {
        "job_ref": job_ref,
        "status": status,
        "state_revision": state_revision,
    }
    if status == "cancelled":
        if job.get("completion_outcome") != (
            "abandoned_after_recovery_confirmation"
        ):
            raise _UnsafeProjectionError("unsafe recovery outcome projection")
        result["completion_outcome"] = (
            "abandoned_after_recovery_confirmation"
        )
    return result


def _safe_global_result(result: dict[str, Any]) -> dict[str, Any]:
    action_ref = result.get("action_ref")
    if (
        not isinstance(action_ref, str)
        or _ACTION_REF.fullmatch(action_ref) is None
        or result.get("status") != "global_quarantine_clear_confirmed"
        or not isinstance(result.get("marker_was_present"), bool)
    ):
        raise _UnsafeProjectionError("unsafe global recovery projection")
    return {
        "action_ref": action_ref,
        "status": "global_quarantine_clear_confirmed",
        "marker_was_present": result["marker_was_present"],
    }


def _list_recovery(
    *,
    annotation_db: Path,
    writer_lock: Path,
) -> dict[str, Any]:
    store = _read_store(annotation_db)
    marker_state = navigation_writer_marker_state(writer_lock)
    jobs = [
        _safe_recovery_job(job)
        for job in store.list_jobs()
        if isinstance(job.get("failure"), dict)
        and job["failure"].get("code") == "recovery_required"
    ]
    return {
        "global_writer": {
            "recovery_marker_present": (
                marker_state.active_present or marker_state.quarantine_present
            ),
            "active_marker_present": marker_state.active_present,
            "quarantine_marker_present": marker_state.quarantine_present,
            "marker_count": len(marker_state.marker_entry_sha256s),
        },
        "jobs": jobs,
    }


def _execute(args: argparse.Namespace) -> dict[str, Any]:
    annotation_db = Path(args.annotation_db)
    writer_lock = Path(args.writer_lock)
    _require_explicit_absolute_paths(annotation_db, writer_lock)
    annotation_db, writer_lock = _bind_production_scope(
        annotation_db,
        writer_lock,
    )

    with acquire_annotation_maintenance(annotation_db):
        if args.command == "list-recovery":
            return _list_recovery(
                annotation_db=annotation_db,
                writer_lock=writer_lock,
            )
        store = _mutation_store(annotation_db)
        if args.command == "clear-global":
            return _safe_global_result(
                store.operator_clear_global_writer_quarantine(
                    confirmation=args.confirmation,
                    operator_reference=args.operator_reference,
                    idempotency_key=args.idempotency_key,
                    writer_lock_path=writer_lock,
                ),
            )
        if args.command == "confirm-job":
            return _safe_job_result(
                store.operator_confirm_recovery(
                    job_ref=args.job_ref,
                    expected_job_revision=args.expected_job_revision,
                    confirmation=args.confirmation,
                    operator_reference=args.operator_reference,
                    idempotency_key=args.idempotency_key,
                    global_quarantine_action_ref=args.global_action_ref,
                    writer_lock_path=writer_lock,
                    disposition=args.disposition,
                ),
            )
        raise _OperatorCLIUsageError("unsupported operator command")


def _serialize(payload: dict[str, Any]) -> str:
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    if (
        _POSIX_ABSOLUTE_PATH.search(serialized)
        or _WINDOWS_ABSOLUTE_PATH.search(serialized)
        or _SECRET.search(serialized)
    ):
        raise _UnsafeProjectionError("operator output failed safety scan")
    return serialized


def _error_code(exc: BaseException) -> str:
    if isinstance(exc, _OperatorCLIUsageError):
        return "invalid_arguments"
    if isinstance(exc, _OperatorScopeError):
        return "operator_scope_mismatch"
    if isinstance(exc, (AnnotationValidationError, AnnotationConflictError)):
        return (
            exc.code
            if _SAFE_ERROR_CODE.fullmatch(exc.code) is not None
            else "operator_infrastructure_error"
        )
    if isinstance(exc, AnnotationNotFoundError):
        return "annotation_job_not_found"
    if isinstance(exc, NavigationWriterQuarantinedError):
        return "writer_recovery_state_changed"
    if isinstance(exc, NavigationWriterLockError):
        return "writer_coordination_unavailable"
    if isinstance(exc, AnnotationServiceOnlineError):
        return "annotation_service_online"
    if isinstance(exc, AnnotationMaintenanceError):
        return "annotation_maintenance_unavailable"
    if isinstance(exc, _UnsafeProjectionError):
        return "unsafe_operator_output"
    return "operator_infrastructure_error"


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        result = _execute(args)
        print(_serialize({"ok": True, "result": result}))
        return 0
    except Exception as exc:
        # Never print exception text: Store and filesystem exceptions may carry
        # private paths, database details, process commands, or operator input.
        print(
            _serialize({"ok": False, "error": {"code": _error_code(exc)}}),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

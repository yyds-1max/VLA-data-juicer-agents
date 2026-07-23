from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator
from uuid import uuid4

from vla_data_juicer_agents.annotation.migrations import (
    LATEST_ANNOTATION_SCHEMA_VERSION,
    apply_annotation_migrations,
    prepare_annotation_migration_ledger,
)
from vla_data_juicer_agents.annotation.models import (
    AnnotationConflictError,
    AnnotationNotFoundError,
    AnnotationValidationError,
    DraftAnnotationTarget,
)
from vla_data_juicer_agents.navigation.writer_lock import (
    configured_writer_lock_path,
    ensure_navigation_writer_quarantine,
    navigation_writer_marker_state,
    navigation_writer_quarantine_clearance,
    navigation_writer_quarantine_present,
)

_SAFE_RUNTIME_STEP_CODES = frozenset(
    {
        "processing_calibration_snapshot",
        "assemble_finish_temp",
        "preprocess_create_box",
        "preprocess_odom_convert",
        "preprocess_resize",
        "metadata_generate",
        "map_publish",
        "video_prepare",
        "initial_annotation",
        "tracking",
    }
)
_SAFE_RUNTIME_DIAGNOSTIC_KINDS = frozenset(
    {"nonzero_exit", "timeout", "cancelled", "error"}
)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _new_ref(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _payload_hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _secure_sqlite_storage(db_path: Path) -> None:
    """Protect the database before SQLite creates WAL sidecars."""

    try:
        parent_metadata = db_path.parent.lstat()
    except OSError as exc:
        raise RuntimeError("annotation database parent is unavailable") from exc
    if (
        stat.S_ISLNK(parent_metadata.st_mode)
        or not stat.S_ISDIR(parent_metadata.st_mode)
        or parent_metadata.st_mode & stat.S_IWGRP
        or parent_metadata.st_mode & stat.S_IWOTH
    ):
        raise RuntimeError(
            "annotation database parent must be a real, non-shared-writable directory",
        )

    _secure_sqlite_file(db_path, create=True)
    for suffix in ("-wal", "-shm"):
        _secure_sqlite_file(Path(f"{db_path}{suffix}"), create=False)


def _secure_sqlite_file(path: Path, *, create: bool) -> None:
    flags = os.O_RDWR
    if create:
        flags |= os.O_CREAT
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileNotFoundError:
        if create:
            raise
        return
    except OSError as exc:
        raise RuntimeError("annotation database storage is unsafe") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise RuntimeError("annotation database storage must be regular files")
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)


def _existing_regular_sqlite_path(db_path: Path) -> Path:
    """Resolve an already-created SQLite database without following a file link."""

    try:
        metadata = db_path.lstat()
    except OSError as exc:
        raise RuntimeError("annotation database must already exist") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError(
            "annotation database must be a real regular file",
        )
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{db_path}{suffix}")
        try:
            sidecar_metadata = sidecar.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise RuntimeError("annotation database sidecar is unavailable") from exc
        if (
            stat.S_ISLNK(sidecar_metadata.st_mode)
            or not stat.S_ISREG(sidecar_metadata.st_mode)
        ):
            raise RuntimeError(
                "annotation database sidecars must be real regular files",
            )
    try:
        return db_path.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError("annotation database is unavailable") from exc


class AnnotationStore:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self._read_only = False
        self.deployment_instance = os.environ.get(
            "VLA_DEPLOYMENT_INSTANCE",
            "deployment_unconfigured",
        )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # SQLite derives new -wal/-shm modes from the database file. Tighten
        # the main file before enabling WAL, then also secure pre-existing
        # sidecars left by an older process.
        _secure_sqlite_storage(self.db_path)
        self._init_schema()
        _secure_sqlite_storage(self.db_path)

    @classmethod
    def open_existing_read_only(cls, db_path: Path | str) -> "AnnotationStore":
        """Open a committed Store for comparison without creating or migrating it."""

        instance = cls.__new__(cls)
        instance.db_path = _existing_regular_sqlite_path(Path(db_path))
        instance._read_only = True
        instance.deployment_instance = os.environ.get(
            "VLA_DEPLOYMENT_INSTANCE",
            "deployment_unconfigured",
        )
        try:
            with instance._connect() as connection:
                versions = [
                    int(row["version"])
                    for row in connection.execute(
                        """
                        SELECT version
                        FROM annotation_schema_migrations
                        ORDER BY version
                        """,
                    ).fetchall()
                ]
        except sqlite3.Error as exc:
            raise RuntimeError(
                "annotation database is not an initialized AnnotationStore",
            ) from exc
        expected = list(range(1, LATEST_ANNOTATION_SCHEMA_VERSION + 1))
        if versions != expected:
            raise RuntimeError(
                "annotation database migration ledger is not current",
            )
        return instance

    def _connect(self) -> sqlite3.Connection:
        if self._read_only:
            connection = sqlite3.connect(
                f"{self.db_path.as_uri()}?mode=ro",
                timeout=10,
                uri=True,
            )
        else:
            connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        if self._read_only:
            connection.execute("PRAGMA query_only = ON")
        else:
            connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            prepare_annotation_migration_ledger(connection)
            apply_annotation_migrations(connection, applied_at=_now())

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        if self._read_only:
            raise RuntimeError("read-only AnnotationStore cannot mutate state")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def mutate(
        self,
        *,
        idempotency_key: str,
        operation: str,
        request_payload: Any,
        callback: Callable[[sqlite3.Connection], dict[str, Any]],
    ) -> dict[str, Any]:
        if not idempotency_key or len(idempotency_key) > 200:
            raise AnnotationValidationError(
                "invalid_idempotency_key",
                "Idempotency-Key must contain between 1 and 200 characters.",
            )
        request_sha = _payload_hash({"operation": operation, "payload": request_payload})
        with self._write() as connection:
            receipt = connection.execute(
                """
                SELECT operation, request_sha256, response_json
                FROM annotation_mutation_receipts
                WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
            if receipt is not None:
                if (
                    receipt["operation"] != operation
                    or receipt["request_sha256"] != request_sha
                ):
                    raise AnnotationConflictError(
                        "idempotency_key_reused",
                        "Idempotency-Key was already used for a different request.",
                    )
                return json.loads(receipt["response_json"])
            response = callback(connection)
            connection.execute(
                """
                INSERT INTO annotation_mutation_receipts (
                    idempotency_key, operation, request_sha256, response_json,
                    actor_kind, deployment_instance, created_at
                ) VALUES (?, ?, ?, ?, 'manual_web', ?, ?)
                """,
                (
                    idempotency_key,
                    operation,
                    request_sha,
                    _canonical_json(response),
                    self.deployment_instance,
                    _now(),
                ),
            )
            return response

    def replay_receipt(
        self,
        *,
        idempotency_key: str,
        operation: str,
        request_payload: Any,
    ) -> dict[str, Any] | None:
        request_sha = _payload_hash({"operation": operation, "payload": request_payload})
        with self._connect() as connection:
            receipt = connection.execute(
                """
                SELECT operation, request_sha256, response_json
                FROM annotation_mutation_receipts WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
        if receipt is None:
            return None
        if receipt["operation"] != operation or receipt["request_sha256"] != request_sha:
            raise AnnotationConflictError(
                "idempotency_key_reused",
                "Idempotency-Key was already used for a different request.",
            )
        return json.loads(receipt["response_json"])

    def recover_interrupted_runs(
        self,
        *,
        current_owner_epoch: str | None = None,
        writer_lock_path: Path | None = None,
    ) -> int:
        """Fail closed on work whose side effects cannot be inferred after restart."""

        with self._write() as connection:
            return self._recover_interrupted_runs_conn(
                connection,
                current_owner_epoch=current_owner_epoch,
                writer_lock_path=writer_lock_path,
            )

    def has_recovery_quarantine(self) -> bool:
        with self._connect() as connection:
            return self._has_recovery_quarantine_conn(connection)

    def runtime_control_state(self, *, run_id: int, worker_id: str) -> str:
        """Return durable cancellation/lease state for an executing worker."""

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT r.status AS run_status, r.failure_code,
                       j.cancel_requested,
                       l.worker_id AS lease_worker, l.expires_at
                FROM runtime_runs r
                JOIN annotation_jobs j ON j.id = r.job_id
                LEFT JOIN runtime_leases l ON l.run_id = r.id
                WHERE r.id = ?
                """,
                (run_id,),
            ).fetchone()
            if row is None:
                return "not_running"
            if row["run_status"] != "running":
                return (
                    "recovery_required"
                    if row["run_status"] == "failed"
                    and row["failure_code"] == "recovery_required"
                    else "finished"
                )
            if bool(row["cancel_requested"]):
                return "cancel_requested"
            if (
                row["lease_worker"] != worker_id
                or row["expires_at"] is None
                or str(row["expires_at"]) <= _now()
            ):
                return "lease_lost"
            return "continue"

    def active_reserved_bytes(self, *, excluding_job_id: int | None = None) -> int:
        with self._connect() as connection:
            return self._active_reserved_bytes_conn(
                connection,
                excluding_job_id=excluding_job_id,
            )

    def has_job(self, job_ref: str) -> bool:
        with self._connect() as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM annotation_jobs WHERE job_ref = ?",
                    (job_ref,),
                ).fetchone()
                is not None
            )

    def job_has_running_run(self, job_ref: str) -> bool:
        with self._connect() as connection:
            job_id = self._job_id(connection, job_ref)
            return (
                connection.execute(
                    """
                    SELECT 1 FROM runtime_runs
                    WHERE job_id = ? AND status = 'running'
                    LIMIT 1
                    """,
                    (job_id,),
                ).fetchone()
                is not None
            )

    def job_status_for_run(self, run_id: int) -> str:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT j.status
                FROM runtime_runs r
                JOIN annotation_jobs j ON j.id = r.job_id
                WHERE r.id = ?
                """,
                (run_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError("runtime run not found")
            return str(row["status"])

    def cancellation_requested_for_run(self, run_id: int) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT j.cancel_requested
                FROM runtime_runs r
                JOIN annotation_jobs j ON j.id = r.job_id
                WHERE r.id = ?
                """,
                (run_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError("runtime run not found")
            return bool(row["cancel_requested"])

    def create_job(
        self,
        *,
        job_ref: str,
        dataset_date: str,
        source_clips: list[str],
        calibration: dict[str, Any],
        snapshot_dir: Path,
        snapshot_files: list[dict[str, Any]],
        reserved_bytes: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        payload = {
            "dataset_date": dataset_date,
            "source_clips": source_clips,
            "calibration_profile_ref": calibration["profile_ref"],
            "calibration_content_sha256": calibration["content_sha256"],
        }

        def create(connection: sqlite3.Connection) -> dict[str, Any]:
            placeholders = ",".join("?" for _ in source_clips)
            conflicts = connection.execute(
                f"""
                SELECT j.job_ref, l.source_clip
                FROM annotation_source_leases l
                JOIN annotation_jobs j ON j.id = l.job_id
                WHERE l.dataset_date = ?
                  AND l.source_clip IN ({placeholders})
                ORDER BY l.source_clip
                """,
                (dataset_date, *source_clips),
            ).fetchall()
            if conflicts:
                raise AnnotationConflictError(
                    "annotation_scope_conflict",
                    "One or more selected clips already belong to an annotation job.",
                    current={
                        "job_ref": conflicts[0]["job_ref"],
                        "source_clips": [row["source_clip"] for row in conflicts],
                    },
                )
            timestamp = _now()
            cursor = connection.execute(
                """
                INSERT INTO annotation_jobs (
                    job_ref, dataset_date, status, reserved_bytes,
                    created_at, updated_at
                ) VALUES (?, ?, 'preparing', ?, ?, ?)
                """,
                (
                    job_ref,
                    dataset_date,
                    reserved_bytes,
                    timestamp,
                    timestamp,
                ),
            )
            job_id = int(cursor.lastrowid)
            snapshot_ref = _new_ref("calibration")
            snapshot_cursor = connection.execute(
                """
                INSERT INTO calibration_snapshots (
                    snapshot_ref, job_id, profile_ref, label, content_sha256,
                    private_snapshot_dir, files_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_ref,
                    job_id,
                    calibration["profile_ref"],
                    calibration["label"],
                    calibration["content_sha256"],
                    str(snapshot_dir),
                    _canonical_json(snapshot_files),
                    timestamp,
                ),
            )
            connection.execute(
                "UPDATE annotation_jobs SET calibration_snapshot_id = ? WHERE id = ?",
                (int(snapshot_cursor.lastrowid), job_id),
            )
            for ordinal, clip in enumerate(source_clips, start=1):
                connection.execute(
                    """
                    INSERT INTO annotation_job_source_clips (job_id, ordinal, source_clip)
                    VALUES (?, ?, ?)
                    """,
                    (job_id, ordinal, clip),
                )
                connection.execute(
                    """
                    INSERT INTO annotation_source_leases (
                        dataset_date, source_clip, job_id, acquired_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (dataset_date, clip, job_id, timestamp),
                )
            self._enqueue_run(connection, job_id=job_id, kind="prepare")
            return self._job_projection(connection, job_id)

        return self.mutate(
            idempotency_key=idempotency_key,
            operation="create_job",
            request_payload=payload,
            callback=create,
        )

    def list_jobs(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id FROM annotation_jobs ORDER BY created_at DESC"
            ).fetchall()
            return [
                self._job_projection(connection, int(row["id"]), include_segments=False)
                for row in rows
            ]

    def get_job(self, job_ref: str) -> dict[str, Any]:
        with self._connect() as connection:
            return self._job_projection(connection, self._job_id(connection, job_ref))

    def get_segment(self, job_ref: str, segment_ref: str) -> dict[str, Any]:
        with self._connect() as connection:
            job_id = self._job_id(connection, job_ref)
            row = self._segment_row(connection, job_id, segment_ref)
            return self._segment_projection(connection, row, include_draft=True)

    def save_draft(
        self,
        *,
        job_ref: str,
        segment_ref: str,
        expected_segment_revision: int,
        expected_draft_revision: int | None,
        targets: list[dict[str, Any]],
        idempotency_key: str,
    ) -> dict[str, Any]:
        payload = {
            "job_ref": job_ref,
            "segment_ref": segment_ref,
            "expected_segment_revision": expected_segment_revision,
            "expected_draft_revision": expected_draft_revision,
            "targets": targets,
        }

        def save(connection: sqlite3.Connection) -> dict[str, Any]:
            job_id, job = self._mutable_waiting_job(connection, job_ref)
            segment = self._segment_row(connection, job_id, segment_ref)
            self._require_segment_revision(segment, expected_segment_revision, connection)
            if segment["status"] not in {
                "pending_initial_annotation",
                "draft",
            }:
                raise AnnotationConflictError(
                    "segment_not_editable",
                    "This segment cannot be edited in its current state.",
                    current=self._segment_projection(connection, segment, include_draft=True),
                )
            actual_draft_revision = (
                int(segment["draft_revision"]) if int(segment["draft_revision"]) else None
            )
            if actual_draft_revision != expected_draft_revision:
                raise AnnotationConflictError(
                    "draft_revision_conflict",
                    "The annotation draft changed; refresh before saving.",
                    current=self._segment_projection(connection, segment, include_draft=True),
                )
            next_draft = (expected_draft_revision or 0) + 1
            targets_json = _canonical_json(targets)
            content_sha = hashlib.sha256(targets_json.encode("utf-8")).hexdigest()
            timestamp = _now()
            connection.execute(
                """
                INSERT INTO initial_annotation_drafts (
                    segment_id, draft_revision, targets_json, content_sha256, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(segment_id) DO UPDATE SET
                    draft_revision = excluded.draft_revision,
                    targets_json = excluded.targets_json,
                    content_sha256 = excluded.content_sha256,
                    updated_at = excluded.updated_at
                """,
                (segment["id"], next_draft, targets_json, content_sha, timestamp),
            )
            connection.execute(
                """
                UPDATE annotation_segments
                SET status = 'draft', state_revision = state_revision + 1,
                    draft_revision = ?, submitted_revision = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (next_draft, timestamp, segment["id"]),
            )
            self._touch_job(connection, int(job["id"]))
            self._record_action(connection, int(segment["id"]), "draft_saved", {})
            updated = self._segment_row(connection, job_id, segment_ref)
            return self._segment_projection(connection, updated, include_draft=True)

        return self.mutate(
            idempotency_key=idempotency_key,
            operation="save_draft",
            request_payload=payload,
            callback=save,
        )

    def submit_segment(
        self,
        *,
        job_ref: str,
        segment_ref: str,
        expected_segment_revision: int,
        expected_draft_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        payload = {
            "job_ref": job_ref,
            "segment_ref": segment_ref,
            "expected_segment_revision": expected_segment_revision,
            "expected_draft_revision": expected_draft_revision,
        }

        def submit(connection: sqlite3.Connection) -> dict[str, Any]:
            job_id, job = self._mutable_waiting_job(connection, job_ref)
            segment = self._segment_row(connection, job_id, segment_ref)
            self._require_segment_revision(segment, expected_segment_revision, connection)
            if int(segment["draft_revision"]) != expected_draft_revision:
                raise AnnotationConflictError(
                    "draft_revision_conflict",
                    "The annotation draft changed; refresh before submitting.",
                    current=self._segment_projection(connection, segment, include_draft=True),
                )
            draft = connection.execute(
                """
                SELECT targets_json, content_sha256
                FROM initial_annotation_drafts WHERE segment_id = ?
                """,
                (segment["id"],),
            ).fetchone()
            if draft is None:
                raise AnnotationValidationError(
                    "annotation_incomplete",
                    "Save a complete initial annotation before submitting.",
                )
            targets = json.loads(draft["targets_json"])
            self._validate_submission(segment, targets)
            revision_number_row = connection.execute(
                """
                SELECT COALESCE(MAX(revision_number), 0) + 1 AS next_revision
                FROM initial_annotation_revisions WHERE segment_id = ?
                """,
                (segment["id"],),
            ).fetchone()
            revision_number = int(revision_number_row["next_revision"])
            timestamp = _now()
            connection.execute(
                """
                INSERT INTO initial_annotation_revisions (
                    revision_ref, segment_id, revision_number, targets_json,
                    content_sha256, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    _new_ref("annotation_revision"),
                    segment["id"],
                    revision_number,
                    draft["targets_json"],
                    draft["content_sha256"],
                    timestamp,
                ),
            )
            connection.execute(
                """
                UPDATE annotation_segments
                SET status = 'submitted', state_revision = state_revision + 1,
                    submitted_revision = ?, updated_at = ?
                WHERE id = ?
                """,
                (revision_number, timestamp, segment["id"]),
            )
            self._touch_job(connection, int(job["id"]))
            self._record_action(
                connection,
                int(segment["id"]),
                "submitted",
                {"revision": revision_number},
            )
            return self._segment_projection(
                connection,
                self._segment_row(connection, job_id, segment_ref),
                include_draft=True,
            )

        return self.mutate(
            idempotency_key=idempotency_key,
            operation="submit_segment",
            request_payload=payload,
            callback=submit,
        )

    def segment_action(
        self,
        *,
        operation: str,
        job_ref: str,
        segment_ref: str,
        expected_segment_revision: int,
        idempotency_key: str,
        reason_code: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            "job_ref": job_ref,
            "segment_ref": segment_ref,
            "expected_segment_revision": expected_segment_revision,
            "reason_code": reason_code,
            "note": note,
        }

        def apply(connection: sqlite3.Connection) -> dict[str, Any]:
            job_id, job = self._mutable_waiting_job(connection, job_ref)
            segment = self._segment_row(connection, job_id, segment_ref)
            self._require_segment_revision(segment, expected_segment_revision, connection)
            current_status = str(segment["status"])
            if operation == "reopen":
                if current_status != "submitted":
                    self._invalid_segment_action(connection, segment)
                next_status = "draft"
                submitted_revision: int | None = None
                skip_reason = None
                skip_note = None
                skip_restore_status = None
                skip_restore_revision = None
            elif operation == "skip":
                if current_status not in {
                    "pending_initial_annotation",
                    "draft",
                    "submitted",
                }:
                    self._invalid_segment_action(connection, segment)
                next_status = "skipped"
                submitted_revision = None
                skip_reason = reason_code
                skip_note = note
                skip_restore_status = current_status
                skip_restore_revision = (
                    int(segment["submitted_revision"])
                    if current_status == "submitted"
                    and segment["submitted_revision"] is not None
                    else None
                )
            elif operation == "unskip":
                if current_status != "skipped":
                    self._invalid_segment_action(connection, segment)
                next_status = segment["skip_restore_status"]
                if next_status not in {
                    "pending_initial_annotation",
                    "draft",
                    "submitted",
                }:
                    raise RuntimeError("skipped segment lacks a restore state")
                submitted_revision = (
                    int(segment["skip_restore_submitted_revision"])
                    if next_status == "submitted"
                    and segment["skip_restore_submitted_revision"] is not None
                    else None
                )
                if next_status == "submitted" and submitted_revision is None:
                    raise RuntimeError("submitted restore state lacks a revision")
                skip_reason = None
                skip_note = None
                skip_restore_status = None
                skip_restore_revision = None
            else:
                raise RuntimeError(f"unsupported segment action: {operation}")
            timestamp = _now()
            connection.execute(
                """
                UPDATE annotation_segments
                SET status = ?, state_revision = state_revision + 1,
                    submitted_revision = ?, skip_reason_code = ?, skip_note = ?,
                    skip_restore_status = ?,
                    skip_restore_submitted_revision = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    next_status,
                    submitted_revision,
                    skip_reason,
                    skip_note,
                    skip_restore_status,
                    skip_restore_revision,
                    timestamp,
                    segment["id"],
                ),
            )
            self._touch_job(connection, int(job["id"]))
            self._record_action(
                connection,
                int(segment["id"]),
                operation,
                {"reason_code": reason_code, "note": note},
            )
            return self._segment_projection(
                connection,
                self._segment_row(connection, job_id, segment_ref),
                include_draft=True,
            )

        return self.mutate(
            idempotency_key=idempotency_key,
            operation=operation,
            request_payload=payload,
            callback=apply,
        )

    def start_tracking(
        self,
        *,
        job_ref: str,
        expected_job_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        payload = {"job_ref": job_ref, "expected_job_revision": expected_job_revision}

        def start(connection: sqlite3.Connection) -> dict[str, Any]:
            job_id = self._job_id(connection, job_ref)
            job = self._job_row(connection, job_id)
            self._require_job_revision(job, expected_job_revision, connection)
            if job["status"] != "waiting_initial_annotation":
                self._invalid_job_action(connection, job_id)
            statuses = [
                str(row["status"])
                for row in connection.execute(
                    "SELECT status FROM annotation_segments WHERE job_id = ?",
                    (job_id,),
                ).fetchall()
            ]
            if not statuses or any(status not in {"submitted", "skipped"} for status in statuses):
                raise AnnotationConflictError(
                    "segments_not_resolved",
                    "Every segment must be submitted or skipped before Tracking starts.",
                    current=self._job_projection(connection, job_id),
                )
            if all(status == "skipped" for status in statuses):
                raise AnnotationConflictError(
                    "no_processable_segments",
                    "Use the no-processable-targets action when every segment is skipped.",
                    current=self._job_projection(connection, job_id),
                )
            timestamp = _now()
            connection.execute(
                """
                UPDATE annotation_jobs
                SET status = 'tracking', state_revision = state_revision + 1,
                    updated_at = ?
                WHERE id = ?
                """,
                (timestamp, job_id),
            )
            connection.execute(
                """
                UPDATE annotation_segments
                SET status = 'tracking', state_revision = state_revision + 1,
                    updated_at = ?
                WHERE job_id = ? AND status = 'submitted'
                """,
                (timestamp, job_id),
            )
            self._enqueue_run(connection, job_id=job_id, kind="tracking")
            return self._job_projection(connection, job_id)

        return self.mutate(
            idempotency_key=idempotency_key,
            operation="start_tracking",
            request_payload=payload,
            callback=start,
        )

    def complete_no_processable_targets(
        self,
        *,
        job_ref: str,
        expected_job_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        payload = {"job_ref": job_ref, "expected_job_revision": expected_job_revision}

        def complete(connection: sqlite3.Connection) -> dict[str, Any]:
            job_id = self._job_id(connection, job_ref)
            job = self._job_row(connection, job_id)
            self._require_job_revision(job, expected_job_revision, connection)
            if job["status"] != "waiting_initial_annotation":
                self._invalid_job_action(connection, job_id)
            statuses = [
                str(row["status"])
                for row in connection.execute(
                    "SELECT status FROM annotation_segments WHERE job_id = ?",
                    (job_id,),
                ).fetchall()
            ]
            if not statuses or any(status != "skipped" for status in statuses):
                raise AnnotationConflictError(
                    "processable_segments_remain",
                    "Every segment must be skipped before completing this way.",
                    current=self._job_projection(connection, job_id),
                )
            self._cancel_job(
                connection,
                job_id,
                completion_outcome="no_processable_targets",
                request_runtime_cancel=False,
            )
            return self._job_projection(connection, job_id)

        return self.mutate(
            idempotency_key=idempotency_key,
            operation="complete_no_processable_targets",
            request_payload=payload,
            callback=complete,
        )

    def cancel_job(
        self,
        *,
        job_ref: str,
        expected_job_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        payload = {"job_ref": job_ref, "expected_job_revision": expected_job_revision}

        def cancel(connection: sqlite3.Connection) -> dict[str, Any]:
            job_id = self._job_id(connection, job_ref)
            job = self._job_row(connection, job_id)
            self._require_job_revision(job, expected_job_revision, connection)
            if job["status"] in {"tracked", "cancelled"}:
                self._invalid_job_action(connection, job_id)
            if (
                job["status"] == "failed"
                and job["failure_code"] == "recovery_required"
            ):
                raise AnnotationConflictError(
                    "recovery_confirmation_required",
                    "The failed Runtime scope remains quarantined until an "
                    "operator confirms that the old process group is absent.",
                    current=self._job_projection(connection, job_id),
                )
            self._cancel_job(
                connection,
                job_id,
                completion_outcome="cancelled_by_user",
                request_runtime_cancel=True,
            )
            return self._job_projection(connection, job_id)

        return self.mutate(
            idempotency_key=idempotency_key,
            operation="cancel_job",
            request_payload=payload,
            callback=cancel,
        )

    def retry_job(
        self,
        *,
        job_ref: str,
        expected_job_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        payload = {"job_ref": job_ref, "expected_job_revision": expected_job_revision}

        def retry(connection: sqlite3.Connection) -> dict[str, Any]:
            job_id = self._job_id(connection, job_ref)
            job = self._job_row(connection, job_id)
            self._require_job_revision(job, expected_job_revision, connection)
            if job["status"] != "failed" or not bool(job["failure_retryable"]):
                self._invalid_job_action(connection, job_id)
            last_run = connection.execute(
                """
                SELECT kind FROM runtime_runs
                WHERE job_id = ?
                ORDER BY id DESC LIMIT 1
                """,
                (job_id,),
            ).fetchone()
            kind = str(last_run["kind"]) if last_run is not None else "prepare"
            next_status = "tracking" if kind == "tracking" else "preparing"
            timestamp = _now()
            connection.execute(
                """
                UPDATE annotation_jobs
                SET status = ?, state_revision = state_revision + 1,
                    failure_code = NULL, failure_message = NULL,
                    failure_ref = NULL, private_failure_detail = NULL,
                    failure_retryable = 0, cancel_requested = 0,
                    cancel_requested_at = NULL, updated_at = ?
                WHERE id = ?
                """,
                (next_status, timestamp, job_id),
            )
            if kind == "tracking":
                connection.execute(
                    """
                    UPDATE annotation_segments
                    SET status = 'tracking', state_revision = state_revision + 1,
                        updated_at = ?
                    WHERE job_id = ? AND status IN ('submitted', 'tracking')
                    """,
                    (timestamp, job_id),
                )
            self._enqueue_run(connection, job_id=job_id, kind=kind)
            return self._job_projection(connection, job_id)

        return self.mutate(
            idempotency_key=idempotency_key,
            operation="retry_job",
            request_payload=payload,
            callback=retry,
        )

    def operator_clear_global_writer_quarantine(
        self,
        *,
        confirmation: str,
        operator_reference: str,
        idempotency_key: str,
        writer_lock_path: Path,
    ) -> dict[str, Any]:
        """Audit and clear the cross-domain writer quarantine.

        This is deliberately separate from resolving an Annotation Job. The
        operator confirmation covers every Navigation and Annotation writer,
        so a Job-specific action cannot accidentally clear a marker created by
        an unrelated Navigation process.
        """

        required_confirmation = (
            "all_navigation_annotation_writer_process_groups_absent"
        )
        if confirmation != required_confirmation:
            raise AnnotationValidationError(
                "invalid_global_recovery_confirmation",
                "Global recovery requires confirmation that all writer process "
                "groups are absent.",
            )
        normalized_reference = operator_reference.strip()
        if not normalized_reference or len(normalized_reference) > 200:
            raise AnnotationValidationError(
                "invalid_operator_reference",
                "An operator or ticket reference is required for recovery.",
            )
        if not idempotency_key or len(idempotency_key) > 200:
            raise AnnotationValidationError(
                "invalid_idempotency_key",
                "Idempotency-Key must contain between 1 and 200 characters.",
            )
        request_sha = _payload_hash(
            {
                "action": "clear_global_quarantine",
                "confirmation": confirmation,
                "operator_reference": normalized_reference,
            }
        )
        observed_state = navigation_writer_marker_state(writer_lock_path)
        action_ref: str
        expected_marker_state_sha256: str
        expected_marker_entry_sha256s: tuple[str, ...]
        completed_response: dict[str, Any] | None = None
        with self._write() as connection:
            existing = connection.execute(
                """
                SELECT a.action_ref, a.request_sha256,
                       a.expected_marker_state_sha256,
                       a.expected_marker_entries_json, c.response_json
                FROM writer_quarantine_actions a
                LEFT JOIN writer_quarantine_action_completions c
                  ON c.action_ref = a.action_ref
                WHERE a.idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if existing["request_sha256"] != request_sha:
                    raise AnnotationConflictError(
                        "idempotency_key_reused",
                        "Idempotency-Key was already used for another recovery request.",
                    )
                action_ref = str(existing["action_ref"])
                expected_marker_state_sha256 = str(
                    existing["expected_marker_state_sha256"],
                )
                raw_entries = json.loads(existing["expected_marker_entries_json"])
                if (
                    not isinstance(raw_entries, list)
                    or any(
                        not isinstance(entry, str)
                        or len(entry) != 64
                        or any(
                            character not in "0123456789abcdef"
                            for character in entry
                        )
                        for entry in raw_entries
                    )
                    or len(set(raw_entries)) != len(raw_entries)
                ):
                    raise RuntimeError(
                        "writer quarantine action marker binding is invalid",
                    )
                expected_marker_entry_sha256s = tuple(raw_entries)
                if existing["response_json"] is not None:
                    completed_response = json.loads(existing["response_json"])
            else:
                action_ref = _new_ref("writer_quarantine_action")
                expected_marker_state_sha256 = observed_state.sha256
                expected_marker_entry_sha256s = (
                    observed_state.marker_entry_sha256s
                )
                timestamp = _now()
                connection.execute(
                    """
                    INSERT INTO writer_quarantine_actions (
                        idempotency_key, action_ref, action, confirmation,
                        operator_reference, deployment_instance, request_sha256,
                        expected_marker_state_sha256,
                        expected_marker_entries_json, created_at
                    ) VALUES (?, ?, 'clear_global_quarantine', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        idempotency_key,
                        action_ref,
                        confirmation,
                        normalized_reference,
                        self.deployment_instance,
                        request_sha,
                        expected_marker_state_sha256,
                        _canonical_json(list(expected_marker_entry_sha256s)),
                        timestamp,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO writer_quarantine_action_recoveries (
                        action_ref, failure_ref
                    )
                    SELECT ?, failure_ref
                    FROM annotation_jobs
                    WHERE status = 'failed'
                      AND failure_code = 'recovery_required'
                      AND failure_ref IS NOT NULL
                    """,
                    (action_ref,),
                )

        clearance_expected_sha256 = expected_marker_state_sha256
        if completed_response is not None:
            current_entries = set(observed_state.marker_entry_sha256s)
            expected_entries = set(expected_marker_entry_sha256s)
            if not current_entries:
                return completed_response
            if not current_entries.issubset(expected_entries):
                # At least one marker belongs to a newer writer incident.
                # The old action must not delete either old or new state.
                return completed_response
            # Completion committed before a partial marker cleanup. The exact
            # remaining subset is still owned by this action and can be
            # removed safely while flock is held.
            clearance_expected_sha256 = observed_state.sha256

        # Keep flock and the exact marker set until the completion row commits.
        # A DB failure therefore leaves every marker intact. A crash after the
        # commit but before unlink is repaired by replaying this same action.
        with navigation_writer_quarantine_clearance(
            writer_lock_path,
            expected_marker_state_sha256=clearance_expected_sha256,
            all_writer_process_groups_absent=True,
        ) as marker_state:
            if completed_response is not None:
                response = completed_response
            else:
                response = {
                    "action_ref": action_ref,
                    "status": "global_quarantine_clear_confirmed",
                    "marker_was_present": (
                        marker_state.active_present
                        or marker_state.quarantine_present
                    ),
                }
                with self._write() as connection:
                    completed = connection.execute(
                        """
                        SELECT response_json
                        FROM writer_quarantine_action_completions
                        WHERE action_ref = ?
                        """,
                        (action_ref,),
                    ).fetchone()
                    if completed is not None:
                        response = json.loads(completed["response_json"])
                    else:
                        connection.execute(
                            """
                            INSERT INTO writer_quarantine_action_completions (
                                action_ref, marker_was_present,
                                response_json, completed_at
                            ) VALUES (?, ?, ?, ?)
                            """,
                            (
                                action_ref,
                                int(response["marker_was_present"]),
                                _canonical_json(response),
                                _now(),
                            ),
                        )
        return response

    def operator_confirm_recovery(
        self,
        *,
        job_ref: str,
        expected_job_revision: int,
        confirmation: str,
        operator_reference: str,
        idempotency_key: str,
        global_quarantine_action_ref: str,
        writer_lock_path: Path,
        disposition: str = "retry",
    ) -> dict[str, Any]:
        """Resolve recovery_required through an explicit operations boundary.

        This method is intentionally not exposed by the trusted-intranet Web
        router. The caller must first verify that the old process group is gone
        and provide an auditable external operator/ticket reference. The
        operator may then retry the quarantined job or safely abandon it and
        release its source scope.
        """

        if confirmation != "old_process_group_absent":
            raise AnnotationValidationError(
                "invalid_recovery_confirmation",
                "Recovery requires confirmation that the old process group is absent.",
            )
        if disposition not in {"retry", "abandon"}:
            raise AnnotationValidationError(
                "invalid_recovery_disposition",
                "Recovery disposition must be retry or abandon.",
            )
        normalized_reference = operator_reference.strip()
        if not normalized_reference or len(normalized_reference) > 200:
            raise AnnotationValidationError(
                "invalid_operator_reference",
                "An operator or ticket reference is required for recovery.",
            )
        if not idempotency_key or len(idempotency_key) > 200:
            raise AnnotationValidationError(
                "invalid_idempotency_key",
                "Idempotency-Key must contain between 1 and 200 characters.",
            )
        request_sha = _payload_hash(
            {
                "action": (
                    "confirm_recovery"
                    if disposition == "retry"
                    else "abandon_recovery"
                ),
                "job_ref": job_ref,
                "expected_job_revision": expected_job_revision,
                "confirmation": confirmation,
                "operator_reference": normalized_reference,
                "global_quarantine_action_ref": global_quarantine_action_ref,
            }
        )
        with self._write() as connection:
            existing = connection.execute(
                """
                SELECT response_json, request_sha256
                FROM annotation_operator_actions
                WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if existing["request_sha256"] != request_sha:
                    raise AnnotationConflictError(
                        "idempotency_key_reused",
                        "Idempotency-Key was already used for another recovery request.",
                    )
                return json.loads(existing["response_json"])
            job_id = self._job_id(connection, job_ref)
            job = self._job_row(connection, job_id)
            self._require_job_revision(job, expected_job_revision, connection)
            if (
                job["status"] != "failed"
                or job["failure_code"] != "recovery_required"
            ):
                self._invalid_job_action(connection, job_id)
            global_confirmation = connection.execute(
                """
                SELECT c.completed_at
                FROM writer_quarantine_actions a
                JOIN writer_quarantine_action_completions c
                  ON c.action_ref = a.action_ref
                JOIN writer_quarantine_action_recoveries r
                  ON r.action_ref = a.action_ref
                WHERE a.action_ref = ?
                  AND a.action = 'clear_global_quarantine'
                  AND a.confirmation =
                      'all_navigation_annotation_writer_process_groups_absent'
                  AND r.failure_ref = ?
                """,
                (global_quarantine_action_ref, job["failure_ref"]),
            ).fetchone()
            if global_confirmation is None:
                raise AnnotationValidationError(
                    "global_quarantine_confirmation_required",
                    "Recovery requires a recorded global writer safety confirmation.",
                )
            if navigation_writer_quarantine_present(writer_lock_path):
                raise AnnotationConflictError(
                    "global_writer_quarantine_active",
                    "A newer global writer quarantine requires another safety "
                    "confirmation.",
                    current=self._job_projection(connection, job_id),
                )
            active = connection.execute(
                """
                SELECT 1 FROM runtime_runs
                WHERE job_id = ? AND status = 'running'
                """,
                (job_id,),
            ).fetchone()
            if active is not None:
                raise AnnotationConflictError(
                    "runtime_still_active",
                    "Recovery cannot start while the old Runtime run is active.",
                    current=self._job_projection(connection, job_id),
                )
            if disposition == "abandon":
                self._cancel_job(
                    connection,
                    job_id,
                    completion_outcome="abandoned_after_recovery_confirmation",
                    request_runtime_cancel=False,
                )
                response = self._job_projection(connection, job_id)
                timestamp = _now()
                action = "abandon_recovery"
                connection.execute(
                    """
                    INSERT INTO annotation_operator_actions (
                        idempotency_key, action_ref, job_id, action, confirmation,
                        operator_reference, deployment_instance, request_sha256,
                        response_json, created_at, global_quarantine_action_ref
                    ) VALUES (?, ?, ?, ?, 'old_process_group_absent',
                              ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        idempotency_key,
                        _new_ref("operator_action"),
                        job_id,
                        action,
                        normalized_reference,
                        self.deployment_instance,
                        request_sha,
                        _canonical_json(response),
                        timestamp,
                        global_quarantine_action_ref,
                    ),
                )
                return response
            last_run = connection.execute(
                """
                SELECT kind FROM runtime_runs
                WHERE job_id = ? ORDER BY id DESC LIMIT 1
                """,
                (job_id,),
            ).fetchone()
            kind = str(last_run["kind"]) if last_run is not None else "prepare"
            next_status = "tracking" if kind == "tracking" else "preparing"
            timestamp = _now()
            connection.execute(
                """
                UPDATE annotation_jobs
                SET status = ?, state_revision = state_revision + 1,
                    failure_code = NULL, failure_message = NULL,
                    failure_ref = NULL, private_failure_detail = NULL,
                    failure_retryable = 0, cancel_requested = 0,
                    cancel_requested_at = NULL, updated_at = ?
                WHERE id = ?
                """,
                (next_status, timestamp, job_id),
            )
            if kind == "tracking":
                connection.execute(
                    """
                    UPDATE annotation_segments
                    SET status = 'tracking', state_revision = state_revision + 1,
                        updated_at = ?
                    WHERE job_id = ? AND status IN ('submitted', 'tracking')
                    """,
                    (timestamp, job_id),
                )
            self._enqueue_run(connection, job_id=job_id, kind=kind)
            response = self._job_projection(connection, job_id)
            action = "confirm_recovery"
            connection.execute(
                """
                INSERT INTO annotation_operator_actions (
                    idempotency_key, action_ref, job_id, action, confirmation,
                    operator_reference, deployment_instance, request_sha256,
                    response_json, created_at, global_quarantine_action_ref
                ) VALUES (?, ?, ?, ?, 'old_process_group_absent',
                          ?, ?, ?, ?, ?, ?)
                """,
                (
                    idempotency_key,
                    _new_ref("operator_action"),
                    job_id,
                    action,
                    normalized_reference,
                    self.deployment_instance,
                    request_sha,
                    _canonical_json(response),
                    timestamp,
                    global_quarantine_action_ref,
                ),
            )
            return response

    def claim_next_run(
        self,
        *,
        worker_id: str,
        owner_epoch: str | None = None,
        writer_lock_path: Path | None = None,
        lease_seconds: int = 120,
    ) -> dict[str, Any] | None:
        with self._write() as connection:
            timestamp = _now()
            if self._recover_interrupted_runs_conn(
                connection,
                current_owner_epoch=owner_epoch,
                writer_lock_path=writer_lock_path,
                timestamp=timestamp,
            ):
                return None
            if self._has_recovery_quarantine_conn(connection):
                return None
            coordination_path = self._optional_writer_lock_path(writer_lock_path)
            if (
                coordination_path is not None
                and navigation_writer_quarantine_present(coordination_path)
            ):
                return None
            connection.execute(
                "DELETE FROM runtime_leases WHERE expires_at <= ?",
                (timestamp,),
            )
            row = connection.execute(
                """
                SELECT r.*, j.job_ref, j.dataset_date, j.status AS job_status,
                       j.staging_root, c.private_snapshot_dir,
                       c.files_json AS calibration_files_json,
                       c.content_sha256 AS calibration_content_sha256
                FROM runtime_runs r
                JOIN annotation_jobs j ON j.id = r.job_id
                JOIN calibration_snapshots c ON c.id = j.calibration_snapshot_id
                WHERE r.status = 'queued'
                  AND j.status IN ('preparing', 'tracking')
                ORDER BY r.created_at, r.id
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            lease_key = "annotation_writer"
            existing = connection.execute(
                "SELECT lease_key FROM runtime_leases WHERE lease_key = ?",
                (lease_key,),
            ).fetchone()
            if existing is not None:
                return None
            expires = (
                datetime.now(UTC) + timedelta(seconds=lease_seconds)
            ).isoformat(timespec="milliseconds")
            connection.execute(
                """
                INSERT INTO runtime_leases (
                    lease_key, run_id, worker_id, expires_at, acquired_at,
                    owner_epoch
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    lease_key,
                    row["id"],
                    worker_id,
                    expires,
                    timestamp,
                    owner_epoch,
                ),
            )
            connection.execute(
                """
                UPDATE runtime_runs
                SET status = 'running', worker_id = ?, started_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (worker_id, timestamp, timestamp, row["id"]),
            )
            clips = [
                str(item["source_clip"])
                for item in connection.execute(
                    """
                    SELECT source_clip FROM annotation_job_source_clips
                    WHERE job_id = ? ORDER BY ordinal
                    """,
                    (row["job_id"],),
                ).fetchall()
            ]
            return {
                "run_id": int(row["id"]),
                "run_ref": row["run_ref"],
                "kind": row["kind"],
                "job_id": int(row["job_id"]),
                "job_ref": row["job_ref"],
                "dataset_date": row["dataset_date"],
                "source_clips": clips,
                "staging_root": row["staging_root"],
                "calibration_snapshot_dir": row["private_snapshot_dir"],
                "calibration_snapshot_files": json.loads(
                    row["calibration_files_json"]
                ),
                "calibration_snapshot_sha256": row[
                    "calibration_content_sha256"
                ],
                "attempt": int(row["attempt"]),
                "active_reserved_bytes": self._active_reserved_bytes_conn(
                    connection,
                    excluding_job_id=int(row["job_id"]),
                ),
            }

    def _recover_interrupted_runs_conn(
        self,
        connection: sqlite3.Connection,
        *,
        current_owner_epoch: str | None,
        writer_lock_path: Path | None,
        timestamp: str | None = None,
    ) -> int:
        checked_at = timestamp or _now()
        rows = connection.execute(
            """
            SELECT r.id
            FROM runtime_runs r
            LEFT JOIN runtime_leases l ON l.run_id = r.id
            WHERE r.status = 'running'
              AND (
                    l.run_id IS NULL
                    OR l.expires_at <= ?
                    OR (
                        ? IS NOT NULL
                        AND l.owner_epoch IS NOT NULL
                        AND l.owner_epoch <> ?
                    )
                  )
            """,
            (checked_at, current_owner_epoch, current_owner_epoch),
        ).fetchall()
        if rows:
            coordination_path = self._required_writer_lock_path(writer_lock_path)
        recoveries = [
            (row, _new_ref("annotation_error"))
            for row in rows
        ]
        for _row, failure_ref in recoveries:
            ensure_navigation_writer_quarantine(
                coordination_path,
                recovery_ref=failure_ref,
            )
        for row, failure_ref in recoveries:
            self._fail_run_conn(
                connection,
                int(row["id"]),
                code="recovery_required",
                message="Runtime recovery requires an operator safety check.",
                retryable=False,
                private_detail=(
                    "The Web process restarted while a runtime run was marked running. "
                    "No success was inferred from filesystem state."
                ),
                failure_ref=failure_ref,
            )
        return len(rows)

    @staticmethod
    def _optional_writer_lock_path(
        writer_lock_path: Path | None,
    ) -> Path | None:
        if writer_lock_path is not None:
            return Path(writer_lock_path)
        if os.getenv("VLA_NAVIGATION_WRITER_LOCK_PATH"):
            return configured_writer_lock_path()
        return None

    @classmethod
    def _required_writer_lock_path(
        cls,
        writer_lock_path: Path | None,
    ) -> Path:
        resolved = cls._optional_writer_lock_path(writer_lock_path)
        if resolved is None:
            return configured_writer_lock_path()
        return resolved

    @staticmethod
    def _has_recovery_quarantine_conn(
        connection: sqlite3.Connection,
    ) -> bool:
        row = connection.execute(
            """
            SELECT 1
            FROM annotation_jobs
            WHERE status = 'failed' AND failure_code = 'recovery_required'
            LIMIT 1
            """
        ).fetchone()
        return row is not None

    def renew_run_lease(
        self,
        *,
        run_id: int,
        worker_id: str,
        lease_seconds: int = 120,
    ) -> bool:
        with self._write() as connection:
            expires = (
                datetime.now(UTC) + timedelta(seconds=lease_seconds)
            ).isoformat(timespec="milliseconds")
            cursor = connection.execute(
                """
                UPDATE runtime_leases
                SET expires_at = ?
                WHERE run_id = ? AND worker_id = ?
                """,
                (expires, run_id, worker_id),
            )
            return cursor.rowcount == 1

    def start_runtime_step(
        self,
        *,
        run_id: int,
        safe_step_code: str,
    ) -> None:
        if safe_step_code not in _SAFE_RUNTIME_STEP_CODES:
            raise RuntimeError("runtime step code is not public-safe")
        with self._write() as connection:
            self._running_run(connection, run_id)
            existing = connection.execute(
                """
                SELECT 1 FROM runtime_run_steps
                WHERE run_id = ? AND safe_step_code = ?
                """,
                (run_id, safe_step_code),
            ).fetchone()
            if existing is not None:
                raise RuntimeError("runtime semantic step was already recorded")
            ordinal_row = connection.execute(
                """
                SELECT COALESCE(MAX(ordinal), 0) + 1 AS ordinal
                FROM runtime_run_steps WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
            timestamp = _now()
            connection.execute(
                """
                INSERT INTO runtime_run_steps (
                    run_id, ordinal, safe_step_code, status,
                    artifact_sha256, return_code, diagnostic_ref,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 'started', NULL, NULL, NULL, ?, ?)
                """,
                (
                    run_id,
                    int(ordinal_row["ordinal"]),
                    safe_step_code,
                    timestamp,
                    timestamp,
                ),
            )

    def finish_runtime_step(
        self,
        *,
        run_id: int,
        safe_step_code: str,
        status: str,
        return_code: int | None = None,
        diagnostic_kind: str | None = None,
    ) -> str | None:
        if safe_step_code not in _SAFE_RUNTIME_STEP_CODES:
            raise RuntimeError("runtime step code is not public-safe")
        if status not in {"succeeded", "failed"}:
            raise RuntimeError("runtime step terminal status is invalid")
        if status == "succeeded" and return_code != 0:
            raise RuntimeError("successful runtime step must record return code zero")
        if return_code is not None and isinstance(return_code, bool):
            raise RuntimeError("runtime step return code is invalid")
        if status == "succeeded" and diagnostic_kind is not None:
            raise RuntimeError("successful runtime step cannot have a diagnostic kind")
        if status == "failed":
            if diagnostic_kind not in _SAFE_RUNTIME_DIAGNOSTIC_KINDS:
                raise RuntimeError("failed runtime step requires a safe diagnostic kind")
            if diagnostic_kind == "nonzero_exit" and (
                return_code is None or return_code == 0
            ):
                raise RuntimeError(
                    "nonzero runtime failure requires its actual return code",
                )
        with self._write() as connection:
            self._running_run(connection, run_id)
            row = connection.execute(
                """
                SELECT id, status FROM runtime_run_steps
                WHERE run_id = ? AND safe_step_code = ?
                """,
                (run_id, safe_step_code),
            ).fetchone()
            if row is None or row["status"] != "started":
                raise RuntimeError("runtime semantic step is not active")
            diagnostic_ref = (
                _new_ref(f"runtime_step_{diagnostic_kind}")
                if status == "failed"
                else None
            )
            timestamp = _now()
            connection.execute(
                """
                UPDATE runtime_run_steps
                SET status = ?, return_code = ?, diagnostic_ref = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    return_code,
                    diagnostic_ref,
                    timestamp,
                    row["id"],
                ),
            )
            return diagnostic_ref

    def fail_active_runtime_step(
        self,
        *,
        run_id: int,
        return_code: int | None = None,
        diagnostic_kind: str = "error",
    ) -> str | None:
        if diagnostic_kind not in _SAFE_RUNTIME_DIAGNOSTIC_KINDS:
            raise RuntimeError("runtime step diagnostic kind is invalid")
        if return_code is not None and isinstance(return_code, bool):
            raise RuntimeError("runtime step return code is invalid")
        if diagnostic_kind == "nonzero_exit" and (
            return_code is None or return_code == 0
        ):
            raise RuntimeError(
                "nonzero runtime failure requires its actual return code",
            )
        with self._write() as connection:
            run = connection.execute(
                "SELECT status FROM runtime_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if run is None:
                raise RuntimeError("runtime run not found")
            row = connection.execute(
                """
                SELECT id FROM runtime_run_steps
                WHERE run_id = ? AND status = 'started'
                ORDER BY ordinal DESC LIMIT 1
                """,
                (run_id,),
            ).fetchone()
            if row is None:
                return None
            diagnostic_ref = _new_ref(f"runtime_step_{diagnostic_kind}")
            connection.execute(
                """
                UPDATE runtime_run_steps
                SET status = 'failed', return_code = ?,
                    diagnostic_ref = ?, updated_at = ?
                WHERE id = ?
                """,
                (return_code, diagnostic_ref, _now(), row["id"]),
            )
            return diagnostic_ref

    def complete_prepare(
        self,
        *,
        run_id: int,
        staging_root: str,
        segments: list[dict[str, Any]],
        manifest: dict[str, Any],
    ) -> None:
        with self._write() as connection:
            run = self._running_run(connection, run_id)
            job = self._job_row(connection, int(run["job_id"]))
            if job["status"] == "cancelled" or bool(
                job["cancel_requested"],
            ):
                self._finish_run(connection, run_id, "cancelled")
                self._finalize_cancelled_job(
                    connection,
                    int(run["job_id"]),
                    completion_outcome="cancelled_by_user",
                )
                return
            if job["status"] != "preparing":
                raise RuntimeError("prepare run no longer owns a preparing job")
            self._require_runtime_step_ledger(
                connection,
                run_id,
                manifest.get("command_steps"),
            )
            if not segments:
                self._fail_run_conn(
                    connection,
                    run_id,
                    code="no_segments_prepared",
                    message="No processable segments were prepared.",
                    retryable=False,
                )
                return
            source_clips = {
                str(row["source_clip"])
                for row in connection.execute(
                    """
                    SELECT source_clip FROM annotation_job_source_clips
                    WHERE job_id = ?
                    """,
                    (run["job_id"],),
                ).fetchall()
            }
            prepared_clips = [str(segment["source_clip"]) for segment in segments]
            if (
                any(clip not in source_clips for clip in prepared_clips)
                or len({str(segment["private_segment_key"]) for segment in segments})
                != len(segments)
            ):
                self._fail_run_conn(
                    connection,
                    run_id,
                    code="invalid_preparation_result",
                    message="The runtime returned an invalid prepared segment set.",
                    retryable=False,
                    private_detail=(
                        "Prepared segments contained an unselected source clip "
                        "or duplicate private segment key."
                    ),
                )
                return
            timestamp = _now()
            for ordinal, segment in enumerate(segments, start=1):
                connection.execute(
                    """
                    INSERT INTO annotation_segments (
                        segment_ref, job_id, ordinal, source_clip, status,
                        private_segment_key, private_segment_root,
                        private_first_frame_path, first_frame_width,
                        first_frame_height, first_frame_sha256, first_frame_etag,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'pending_initial_annotation',
                              ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        _new_ref("segment"),
                        run["job_id"],
                        ordinal,
                        segment["source_clip"],
                        segment["private_segment_key"],
                        segment["segment_root"],
                        segment["first_frame_path"],
                        segment["width"],
                        segment["height"],
                        segment["sha256"],
                        segment["etag"],
                        timestamp,
                        timestamp,
                    ),
                )
            connection.execute(
                """
                UPDATE annotation_jobs
                SET status = 'waiting_initial_annotation',
                    state_revision = state_revision + 1,
                    staging_root = ?, updated_at = ?
                WHERE id = ?
                """,
                (staging_root, timestamp, run["job_id"]),
            )
            self._insert_manifest(connection, run, "prepare", manifest)
            self._finish_run(connection, run_id, "succeeded")

    def tracking_inputs(self, job_id: int) -> dict[str, Any]:
        with self._connect() as connection:
            job = self._job_row(connection, job_id)
            prepare_manifest_row = connection.execute(
                """
                SELECT a.manifest_json, a.content_sha256, r.id AS run_id
                FROM artifact_manifests a
                JOIN runtime_runs r ON r.id = a.run_id
                WHERE a.job_id = ? AND a.stage = 'prepare'
                  AND r.status = 'succeeded'
                ORDER BY a.id DESC LIMIT 1
                """,
                (job_id,),
            ).fetchone()
            if prepare_manifest_row is None:
                raise RuntimeError("tracking job lacks a committed prepare manifest")
            prepare_manifest = json.loads(
                prepare_manifest_row["manifest_json"],
            )
            runtime_manifest_sha256 = prepare_manifest.get(
                "runtime_manifest_sha256",
            )
            prepared_artifact_tree_sha256 = prepare_manifest.get(
                "prepared_artifact_tree_sha256",
            )
            for label, value in (
                ("runtime manifest", runtime_manifest_sha256),
                ("prepared artifact tree", prepared_artifact_tree_sha256),
            ):
                if (
                    not isinstance(value, str)
                    or len(value) != 64
                    or any(
                        character not in "0123456789abcdef"
                        for character in value
                    )
                ):
                    raise RuntimeError(
                        f"prepare manifest lacks a valid {label} hash",
                    )
            segments: list[dict[str, Any]] = []
            rows = connection.execute(
                """
                SELECT * FROM annotation_segments
                WHERE job_id = ? AND status = 'tracking'
                ORDER BY ordinal
                """,
                (job_id,),
            ).fetchall()
            for segment in rows:
                revision = connection.execute(
                    """
                    SELECT targets_json, content_sha256
                    FROM initial_annotation_revisions
                    WHERE segment_id = ? AND revision_number = ?
                    """,
                    (segment["id"], segment["submitted_revision"]),
                ).fetchone()
                if revision is None:
                    raise RuntimeError("tracking segment has no submitted revision")
                segments.append(
                    {
                        "segment_id": int(segment["id"]),
                        "segment_ref": segment["segment_ref"],
                        "segment_root": segment["private_segment_root"],
                        "revision_number": int(segment["submitted_revision"]),
                        "revision_sha256": revision["content_sha256"],
                        "targets": json.loads(revision["targets_json"]),
                    }
                )
                for target in segments[-1]["targets"]:
                    checkpoint = connection.execute(
                        """
                        SELECT checkpoint_ref, identity, private_output_dir,
                               private_points_path, artifact_sha256
                        FROM tracking_checkpoints
                        WHERE job_id = ? AND segment_id = ? AND target_ref = ?
                          AND revision_sha256 = ?
                        """,
                        (
                            job_id,
                            segment["id"],
                            target["target_ref"],
                            revision["content_sha256"],
                        ),
                    ).fetchone()
                    if checkpoint is not None:
                        target["checkpoint"] = {
                            "checkpoint_ref": checkpoint["checkpoint_ref"],
                            "identity": checkpoint["identity"],
                            "output_dir": checkpoint["private_output_dir"],
                            "points_path": checkpoint["private_points_path"],
                            "artifact_sha256": checkpoint["artifact_sha256"],
                        }
            return {
                "job_ref": job["job_ref"],
                "staging_root": job["staging_root"],
                "expected_runtime_manifest_sha256": (
                    runtime_manifest_sha256
                ),
                "expected_prepared_artifact_tree_sha256": (
                    prepared_artifact_tree_sha256
                ),
                "segments": segments,
            }

    def record_tracking_checkpoint(
        self,
        *,
        run_id: int,
        segment_ref: str,
        target_ref: str,
        revision_sha256: str,
        identity: str,
        output_dir: str,
        points_path: str,
        artifact_sha256: str,
    ) -> dict[str, Any]:
        with self._write() as connection:
            run = self._running_run(connection, run_id)
            if run["kind"] != "tracking":
                raise RuntimeError("checkpoint belongs to a non-tracking run")
            job = self._job_row(connection, int(run["job_id"]))
            if job["status"] != "tracking":
                raise RuntimeError("checkpoint job is not tracking")
            if bool(job["cancel_requested"]):
                raise RuntimeError(
                    "tracking cancellation was requested before checkpoint commit",
                )
            segment = self._segment_row(
                connection,
                int(run["job_id"]),
                segment_ref,
            )
            revision = connection.execute(
                """
                SELECT content_sha256, targets_json FROM initial_annotation_revisions
                WHERE segment_id = ? AND revision_number = ?
                """,
                (segment["id"], segment["submitted_revision"]),
            ).fetchone()
            if revision is None or revision["content_sha256"] != revision_sha256:
                raise RuntimeError("checkpoint revision does not match submitted annotation")
            revision_target_refs = {
                str(item.get("target_ref"))
                for item in json.loads(revision["targets_json"])
                if isinstance(item, dict)
            }
            if target_ref not in revision_target_refs:
                raise RuntimeError("checkpoint target is not in the submitted annotation")
            existing = connection.execute(
                """
                SELECT checkpoint_ref, identity, private_output_dir,
                       private_points_path, artifact_sha256
                FROM tracking_checkpoints
                WHERE job_id = ? AND segment_id = ? AND target_ref = ?
                  AND revision_sha256 = ?
                """,
                (
                    run["job_id"],
                    segment["id"],
                    target_ref,
                    revision_sha256,
                ),
            ).fetchone()
            safe = {
                "segment_ref": segment_ref,
                "target_ref": target_ref,
                "identity": identity,
                "artifact_sha256": artifact_sha256,
            }
            if existing is not None:
                if (
                    existing["identity"] != identity
                    or existing["private_output_dir"] != output_dir
                    or existing["private_points_path"] != points_path
                    or existing["artifact_sha256"] != artifact_sha256
                ):
                    raise RuntimeError("committed tracking checkpoint is immutable")
                return safe
            timestamp = _now()
            connection.execute(
                """
                INSERT INTO tracking_checkpoints (
                    checkpoint_ref, job_id, run_id, segment_id, target_ref,
                    revision_sha256, identity, private_output_dir,
                    private_points_path, artifact_sha256, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _new_ref("tracking_checkpoint"),
                    run["job_id"],
                    run_id,
                    segment["id"],
                    target_ref,
                    revision_sha256,
                    identity,
                    output_dir,
                    points_path,
                    artifact_sha256,
                    timestamp,
                ),
            )
            ordinal_row = connection.execute(
                """
                SELECT COALESCE(MAX(ordinal), 0) + 1 AS ordinal
                FROM runtime_run_steps WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO runtime_run_steps (
                    run_id, ordinal, safe_step_code, status,
                    artifact_sha256, created_at, updated_at
                ) VALUES (?, ?, 'tracking_target_completed', 'succeeded', ?, ?, ?)
                """,
                (
                    run_id,
                    int(ordinal_row["ordinal"]),
                    artifact_sha256,
                    timestamp,
                    timestamp,
                ),
            )
            return safe

    def complete_tracking(
        self,
        *,
        run_id: int,
        manifest: dict[str, Any],
    ) -> None:
        with self._write() as connection:
            run = self._running_run(connection, run_id)
            job = self._job_row(connection, int(run["job_id"]))
            if job["status"] == "cancelled" or bool(
                job["cancel_requested"],
            ):
                self._finish_run(connection, run_id, "cancelled")
                self._finalize_cancelled_job(
                    connection,
                    int(run["job_id"]),
                    completion_outcome="cancelled_by_user",
                )
                return
            if job["status"] != "tracking":
                raise RuntimeError("tracking run no longer owns a tracking job")
            self._require_runtime_step_ledger(
                connection,
                run_id,
                manifest.get("command_steps"),
            )
            expected_targets = 0
            for segment in connection.execute(
                """
                SELECT id, submitted_revision FROM annotation_segments
                WHERE job_id = ? AND status = 'tracking'
                """,
                (run["job_id"],),
            ).fetchall():
                revision = connection.execute(
                    """
                    SELECT targets_json, content_sha256
                    FROM initial_annotation_revisions
                    WHERE segment_id = ? AND revision_number = ?
                    """,
                    (segment["id"], segment["submitted_revision"]),
                ).fetchone()
                if revision is None:
                    raise RuntimeError("tracking segment revision is missing")
                targets = json.loads(revision["targets_json"])
                expected_targets += len(targets)
                checkpoint_count = connection.execute(
                    """
                    SELECT COUNT(*) AS count FROM tracking_checkpoints
                    WHERE job_id = ? AND segment_id = ? AND revision_sha256 = ?
                    """,
                    (run["job_id"], segment["id"], revision["content_sha256"]),
                ).fetchone()
                if int(checkpoint_count["count"]) != len(targets):
                    raise RuntimeError("tracking checkpoints are incomplete")
            if expected_targets == 0:
                raise RuntimeError("tracking has no submitted targets")
            timestamp = _now()
            connection.execute(
                """
                UPDATE annotation_segments
                SET status = 'tracked', state_revision = state_revision + 1,
                    updated_at = ?
                WHERE job_id = ? AND status = 'tracking'
                """,
                (timestamp, run["job_id"]),
            )
            connection.execute(
                """
                UPDATE annotation_jobs
                SET status = 'tracked', state_revision = state_revision + 1,
                    reserved_bytes = 0, updated_at = ?
                WHERE id = ?
                """,
                (timestamp, run["job_id"]),
            )
            self._insert_manifest(connection, run, "tracking", manifest)
            self._finish_run(connection, run_id, "succeeded")

    def fail_run(
        self,
        *,
        run_id: int,
        code: str,
        message: str,
        retryable: bool,
        private_detail: str | None = None,
    ) -> None:
        with self._write() as connection:
            self._fail_run_conn(
                connection,
                run_id,
                code=code,
                message=message,
                retryable=retryable,
                private_detail=private_detail,
            )

    def complete_cancelled_run(self, *, run_id: int) -> None:
        with self._write() as connection:
            run = connection.execute(
                "SELECT * FROM runtime_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if run is None:
                raise RuntimeError("runtime run not found")
            if run["status"] == "cancelled":
                return
            if run["status"] != "running":
                raise RuntimeError("runtime run is not running")
            job = self._job_row(connection, int(run["job_id"]))
            if not bool(job["cancel_requested"]):
                raise RuntimeError(
                    "runtime cancellation lacks a durable cancel request",
                )
            self._finish_run(connection, run_id, "cancelled")
            self._finalize_cancelled_job(
                connection,
                int(run["job_id"]),
                completion_outcome="cancelled_by_user",
            )

    def first_frame_private(self, job_ref: str, segment_ref: str) -> dict[str, Any]:
        with self._connect() as connection:
            job_id = self._job_id(connection, job_ref)
            segment = self._segment_row(connection, job_id, segment_ref)
            job = self._job_row(connection, job_id)
            return {
                "path": segment["private_first_frame_path"],
                "staging_root": job["staging_root"],
                "sha256": segment["first_frame_sha256"],
                "etag": segment["first_frame_etag"],
                "width": int(segment["first_frame_width"]),
                "height": int(segment["first_frame_height"]),
            }

    def runtime_run_attestation(self, run_ref: str) -> dict[str, Any]:
        """Return the safe Golden attestation derived only from committed rows."""

        from vla_data_juicer_agents.annotation.legacy_yaml import (
            LegacyYamlAdapter,
        )

        with self._connect() as connection:
            tracking_run = connection.execute(
                """
                SELECT * FROM runtime_runs WHERE run_ref = ?
                """,
                (run_ref,),
            ).fetchone()
            if tracking_run is None:
                raise AnnotationNotFoundError("annotation runtime run not found")
            job_id = int(tracking_run["job_id"])
            job = self._job_row(connection, job_id)
            if (
                tracking_run["kind"] != "tracking"
                or tracking_run["status"] != "succeeded"
                or job["status"] != "tracked"
            ):
                raise AnnotationConflictError(
                    "runtime_run_not_committed",
                    "Runtime attestation requires a succeeded Tracking run.",
                    current=self._job_projection(connection, job_id),
                )
            prepare_manifest_row = connection.execute(
                """
                SELECT a.manifest_json, a.content_sha256, r.id AS run_id
                FROM artifact_manifests a
                JOIN runtime_runs r ON r.id = a.run_id
                WHERE a.job_id = ? AND a.stage = 'prepare'
                  AND r.status = 'succeeded'
                ORDER BY a.id DESC LIMIT 1
                """,
                (job_id,),
            ).fetchone()
            tracking_manifest_row = connection.execute(
                """
                SELECT manifest_json, content_sha256 FROM artifact_manifests
                WHERE run_id = ? AND stage = 'tracking'
                ORDER BY id DESC LIMIT 1
                """,
                (tracking_run["id"],),
            ).fetchone()
            calibration = connection.execute(
                """
                SELECT content_sha256 FROM calibration_snapshots
                WHERE id = ?
                """,
                (job["calibration_snapshot_id"],),
            ).fetchone()
            if (
                prepare_manifest_row is None
                or tracking_manifest_row is None
                or calibration is None
            ):
                raise RuntimeError("tracked job has an incomplete committed ledger")
            for manifest_row in (
                prepare_manifest_row,
                tracking_manifest_row,
            ):
                encoded_manifest = manifest_row["manifest_json"]
                if (
                    not isinstance(encoded_manifest, str)
                    or hashlib.sha256(
                        encoded_manifest.encode("utf-8"),
                    ).hexdigest()
                    != manifest_row["content_sha256"]
                ):
                    raise RuntimeError(
                        "committed Runtime manifest content hash changed",
                    )
            prepare_manifest = json.loads(prepare_manifest_row["manifest_json"])
            tracking_manifest = json.loads(tracking_manifest_row["manifest_json"])
            runtime_manifest_sha256 = prepare_manifest.get(
                "runtime_manifest_sha256"
            )
            if not isinstance(runtime_manifest_sha256, str):
                raise RuntimeError("prepare manifest lacks runtime manifest hash")
            tracking_runtime_manifest_sha256 = tracking_manifest.get(
                "runtime_manifest_sha256",
            )
            if (
                not isinstance(tracking_runtime_manifest_sha256, str)
                or tracking_runtime_manifest_sha256
                != runtime_manifest_sha256
            ):
                raise RuntimeError(
                    "prepare and Tracking Runtime manifest hashes differ",
                )
            prepared_artifact_tree_sha256 = prepare_manifest.get(
                "prepared_artifact_tree_sha256",
            )
            tracking_prepared_artifact_tree_sha256 = tracking_manifest.get(
                "prepared_artifact_tree_sha256",
            )
            if (
                not isinstance(prepared_artifact_tree_sha256, str)
                or tracking_prepared_artifact_tree_sha256
                != prepared_artifact_tree_sha256
            ):
                raise RuntimeError(
                    "prepare and Tracking artifact hashes differ",
                )
            revision_set: list[dict[str, Any]] = []
            checkpoint_entries: list[
                tuple[tuple[str, str], dict[str, str]]
            ] = []
            yaml_adapter = LegacyYamlAdapter()
            for segment in connection.execute(
                """
                SELECT id, segment_ref, submitted_revision,
                       private_segment_root
                FROM annotation_segments
                WHERE job_id = ? AND status = 'tracked'
                ORDER BY ordinal
                """,
                (job_id,),
            ).fetchall():
                revision = connection.execute(
                    """
                    SELECT content_sha256, targets_json
                    FROM initial_annotation_revisions
                    WHERE segment_id = ? AND revision_number = ?
                    """,
                    (segment["id"], segment["submitted_revision"]),
                ).fetchone()
                if revision is None:
                    raise RuntimeError("tracked segment revision is missing")
                submitted_actions = connection.execute(
                    """
                    SELECT safe_payload_json
                    FROM annotation_segment_actions
                    WHERE segment_id = ? AND action = 'submitted'
                    ORDER BY id
                    """,
                    (segment["id"],),
                ).fetchall()
                submitted_revision = int(segment["submitted_revision"])
                if not any(
                    json.loads(row["safe_payload_json"]).get("revision")
                    == submitted_revision
                    for row in submitted_actions
                ):
                    raise RuntimeError(
                        "tracked segment lacks its committed submit action"
                    )
                try:
                    targets = json.loads(revision["targets_json"])
                    if _payload_hash(targets) != revision["content_sha256"]:
                        raise RuntimeError(
                            "tracked annotation revision content hash changed",
                        )
                    rendered = yaml_adapter.render(
                        Path(segment["private_segment_root"]),
                        [
                            {
                                "target_ref": target["target_ref"],
                                "bbox": target["bbox"],
                                "point": target["point"],
                                "upper_color": target["colors"]["upper"],
                                "lower_color": target["colors"]["lower"],
                                "shoes_color": target["colors"]["shoes"],
                            }
                            for target in targets
                        ],
                    )
                except Exception as exc:
                    raise RuntimeError(
                        "tracked segment revision cannot be re-rendered",
                    ) from exc
                for rendered_target in rendered:
                    checkpoints = connection.execute(
                        """
                        SELECT c.target_ref, c.identity, c.artifact_sha256
                        FROM tracking_checkpoints c
                        JOIN runtime_runs r ON r.id = c.run_id
                        WHERE c.job_id = ? AND c.segment_id = ?
                          AND c.target_ref = ? AND c.revision_sha256 = ?
                          AND r.job_id = c.job_id AND r.kind = 'tracking'
                          AND EXISTS (
                              SELECT 1 FROM runtime_run_steps step
                              WHERE step.run_id = c.run_id
                                AND step.safe_step_code =
                                    'tracking_target_completed'
                                AND step.status = 'succeeded'
                                AND step.artifact_sha256 = c.artifact_sha256
                          )
                        """,
                        (
                            job_id,
                            segment["id"],
                            rendered_target.target_ref,
                            revision["content_sha256"],
                        ),
                    ).fetchall()
                    if len(checkpoints) != 1:
                        raise RuntimeError(
                            "tracked target lacks one authoritative checkpoint",
                        )
                    checkpoint = checkpoints[0]
                    expected_identity = Path(
                        rendered_target.filename,
                    ).stem
                    if checkpoint["identity"] != expected_identity:
                        raise RuntimeError(
                            "tracked checkpoint identity differs from revision",
                        )
                    checkpoint_entries.append(
                        (
                            (
                                str(segment["private_segment_root"]),
                                rendered_target.filename,
                            ),
                            {
                                "segment_ref": str(segment["segment_ref"]),
                                "target_ref": str(
                                    rendered_target.target_ref,
                                ),
                                "identity": str(checkpoint["identity"]),
                                "artifact_sha256": str(
                                    checkpoint["artifact_sha256"],
                                ),
                            },
                        )
                    )
                revision_set.append(
                    {
                        "segment_ref": segment["segment_ref"],
                        "revision": submitted_revision,
                        "sha256": revision["content_sha256"],
                    }
                )
            completed_target_step_count = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM runtime_run_steps step
                JOIN runtime_runs r ON r.id = step.run_id
                WHERE r.job_id = ? AND r.kind = 'tracking'
                  AND step.safe_step_code = 'tracking_target_completed'
                  AND step.status = 'succeeded'
                """,
                (job_id,),
            ).fetchone()
            if int(completed_target_step_count["count"]) != len(
                checkpoint_entries,
            ):
                raise RuntimeError(
                    "Tracking checkpoint completion ledger is inconsistent",
                )
            if tracking_manifest.get("revision_set") != revision_set:
                raise RuntimeError(
                    "Tracking manifest and committed annotation revisions differ",
                )
            checkpoint_ledger = [
                checkpoint
                for _sort_key, checkpoint in sorted(
                    checkpoint_entries,
                    key=lambda item: item[0],
                )
            ]
            declared_checkpoints = tracking_manifest.get("checkpoints")
            if declared_checkpoints != checkpoint_ledger:
                raise RuntimeError(
                    "Tracking manifest and committed checkpoints differ",
                )
            command_steps = self._require_runtime_step_ledger(
                connection,
                int(prepare_manifest_row["run_id"]),
                prepare_manifest.get("command_steps"),
            )
            command_steps.extend(
                self._require_runtime_step_ledger(
                    connection,
                    int(tracking_run["id"]),
                    tracking_manifest.get("command_steps"),
                )
            )
            if (
                not command_steps
                or len(command_steps) != len(set(command_steps))
                or any(not isinstance(step, str) for step in command_steps)
            ):
                raise RuntimeError("runtime command ledger is invalid")
            return {
                "source": "runtime_run",
                "run_ref": tracking_run["run_ref"],
                "committed": True,
                "runtime_manifest_sha256": runtime_manifest_sha256,
                "calibration_snapshot_sha256": calibration["content_sha256"],
                "annotation_revision_set_sha256": _payload_hash(revision_set),
                "command_steps": command_steps,
            }

    def golden_candidate_binding(
        self,
        *,
        run_ref: str,
        dataset_date: str,
        source_clip: str,
        internal_segment: str | None,
        scope_kind: str = "segment",
    ) -> dict[str, Any]:
        """Bind one Golden case to the private tree owned by a committed run.

        Paths and internal segment names in this projection are process-local
        inputs to the comparator.  Public reports consume only ``attestation``.
        """

        from vla_data_juicer_agents.annotation.legacy_yaml import (
            LegacyYamlAdapter,
        )
        from vla_data_juicer_agents.annotation.runtime import (
            RuntimeExecutionError,
            TrackingTarget,
            prepared_staging_artifact_sha256,
            regular_artifact_sha256,
            tracking_checkpoint_artifact_sha256,
        )

        attestation = self.runtime_run_attestation(run_ref)
        with self._connect() as connection:
            run = connection.execute(
                """
                SELECT id, job_id, kind, status
                FROM runtime_runs
                WHERE run_ref = ?
                """,
                (run_ref,),
            ).fetchone()
            if run is None:
                raise AnnotationNotFoundError("annotation runtime run not found")
            job = self._job_row(connection, int(run["job_id"]))
            if (
                run["kind"] != "tracking"
                or run["status"] != "succeeded"
                or job["status"] != "tracked"
            ):
                raise AnnotationConflictError(
                    "runtime_run_not_committed",
                    "Golden comparison requires a succeeded Tracking run.",
                )
            clips = [
                str(row["source_clip"])
                for row in connection.execute(
                    """
                    SELECT source_clip
                    FROM annotation_job_source_clips
                    WHERE job_id = ?
                    ORDER BY ordinal
                    """,
                    (run["job_id"],),
                ).fetchall()
            ]
            if (
                str(job["dataset_date"]) != dataset_date
                or source_clip not in clips
            ):
                raise AnnotationConflictError(
                    "golden_candidate_scope_mismatch",
                    "Golden case date or source clip is not owned by this run.",
                )
            rows = connection.execute(
                """
                SELECT id, segment_ref, source_clip, submitted_revision,
                       private_segment_key, private_segment_root
                FROM annotation_segments
                WHERE job_id = ? AND status = 'tracked'
                ORDER BY ordinal
                """,
                (run["job_id"],),
            ).fetchall()
            if scope_kind not in {
                "segment",
                "prepare_maps",
                "prepare_metadata",
            }:
                raise AnnotationConflictError(
                    "golden_candidate_scope_mismatch",
                    "Golden case artifact scope is not supported.",
                )
            if scope_kind == "segment":
                selected = [
                    row
                    for row in rows
                    if str(row["private_segment_key"]) == internal_segment
                ]
                if (
                    internal_segment is None
                    or len(selected) != 1
                    or str(selected[0]["source_clip"]) != source_clip
                ):
                    raise AnnotationConflictError(
                        "golden_candidate_scope_mismatch",
                        "Golden case segment is not owned by this run and clip.",
                    )
            elif internal_segment is not None:
                raise AnnotationConflictError(
                    "golden_candidate_scope_mismatch",
                    "Golden prepare-global scope cannot select a segment.",
                )
            raw_staging_root = job["staging_root"]
            if not isinstance(raw_staging_root, str) or not raw_staging_root:
                raise RuntimeError(
                    "committed Tracking job lacks a private staging root",
                )
            staging_path = Path(raw_staging_root)
            if not staging_path.is_absolute():
                raise RuntimeError(
                    "committed Tracking staging root is invalid",
                )
            try:
                staging_metadata = staging_path.lstat()
                staging_root = staging_path.resolve(strict=True)
            except OSError as exc:
                raise RuntimeError(
                    "committed Tracking staging root is unavailable",
                ) from exc
            if (
                stat.S_ISLNK(staging_metadata.st_mode)
                or not stat.S_ISDIR(staging_metadata.st_mode)
            ):
                raise RuntimeError(
                    "committed Tracking staging root is unsafe",
                )

            prepare_manifest_row = connection.execute(
                """
                SELECT a.manifest_json
                FROM artifact_manifests a
                JOIN runtime_runs r ON r.id = a.run_id
                WHERE a.job_id = ? AND a.stage = 'prepare'
                  AND r.status = 'succeeded'
                ORDER BY a.id DESC LIMIT 1
                """,
                (run["job_id"],),
            ).fetchone()
            if prepare_manifest_row is None:
                raise RuntimeError(
                    "committed Tracking job lacks its prepare manifest",
                )
            prepare_manifest = json.loads(
                prepare_manifest_row["manifest_json"],
            )
            expected_prepared_sha256 = prepare_manifest.get(
                "prepared_artifact_tree_sha256",
            )
            if (
                not isinstance(expected_prepared_sha256, str)
                or len(expected_prepared_sha256) != 64
            ):
                raise RuntimeError(
                    "committed prepare manifest lacks its artifact hash",
                )

            segments: list[dict[str, str]] = []
            runtime_targets: list[TrackingTarget] = []
            yaml_adapter = LegacyYamlAdapter()
            for row in rows:
                segment_key = str(row["private_segment_key"])
                segment_clip = str(row["source_clip"])
                raw_segment_root = row["private_segment_root"]
                if (
                    not isinstance(raw_segment_root, str)
                    or not raw_segment_root
                    or not Path(raw_segment_root).is_absolute()
                ):
                    raise RuntimeError(
                        "committed Tracking segment mapping is invalid",
                    )
                segment_path = Path(raw_segment_root)
                try:
                    lexical_relative = segment_path.relative_to(staging_path)
                    if (
                        not lexical_relative.parts
                        or any(
                            component in {".", ".."}
                            for component in lexical_relative.parts
                        )
                    ):
                        raise ValueError
                    current = staging_path
                    for component in lexical_relative.parts:
                        current = current / component
                        metadata = current.lstat()
                        if (
                            stat.S_ISLNK(metadata.st_mode)
                            or not stat.S_ISDIR(metadata.st_mode)
                        ):
                            raise RuntimeError(
                                "committed Tracking segment mapping is unsafe",
                            )
                    segment_root = segment_path.resolve(strict=True)
                    relative = segment_root.relative_to(staging_root)
                except (OSError, ValueError) as exc:
                    raise RuntimeError(
                        "committed Tracking segment escapes its staging root",
                    ) from exc
                if (
                    not relative.parts
                    or relative.name != segment_key
                ):
                    raise RuntimeError(
                        "committed Tracking segment mapping is inconsistent",
                    )
                revision = connection.execute(
                    """
                    SELECT content_sha256, targets_json
                    FROM initial_annotation_revisions
                    WHERE segment_id = ? AND revision_number = ?
                    """,
                    (row["id"], row["submitted_revision"]),
                ).fetchone()
                if revision is None:
                    raise RuntimeError(
                        "committed Tracking segment revision is missing",
                    )
                try:
                    targets = json.loads(revision["targets_json"])
                    if _payload_hash(targets) != revision["content_sha256"]:
                        raise RuntimeError(
                            "committed annotation revision content hash changed",
                        )
                    target_payloads = [
                        {
                            "target_ref": target["target_ref"],
                            "bbox": target["bbox"],
                            "point": target["point"],
                            "upper_color": target["colors"]["upper"],
                            "lower_color": target["colors"]["lower"],
                            "shoes_color": target["colors"]["shoes"],
                        }
                        for target in targets
                    ]
                    rendered = yaml_adapter.render(
                        segment_root,
                        target_payloads,
                    )
                except Exception as exc:
                    raise RuntimeError(
                        "committed annotation revision cannot be re-rendered",
                    ) from exc
                checkpoints = connection.execute(
                    """
                    SELECT target_ref, revision_sha256, identity,
                           private_output_dir, private_points_path,
                           artifact_sha256
                    FROM tracking_checkpoints c
                    JOIN runtime_runs r ON r.id = c.run_id
                    WHERE c.job_id = ? AND c.segment_id = ?
                      AND c.revision_sha256 = ?
                      AND r.job_id = c.job_id AND r.kind = 'tracking'
                    ORDER BY c.id
                    """,
                    (
                        run["job_id"],
                        row["id"],
                        revision["content_sha256"],
                    ),
                ).fetchall()
                if len(checkpoints) != len(rendered):
                    raise RuntimeError(
                        "committed Tracking checkpoint set is incomplete",
                    )
                checkpoints_by_target = {
                    str(checkpoint["target_ref"]): checkpoint
                    for checkpoint in checkpoints
                }
                if len(checkpoints_by_target) != len(checkpoints):
                    raise RuntimeError(
                        "committed Tracking checkpoint targets are duplicated",
                    )
                try:
                    for rendered_target in rendered:
                        identity = Path(rendered_target.filename).stem
                        yaml_path = segment_root / rendered_target.filename
                        if (
                            regular_artifact_sha256(yaml_path)
                            != rendered_target.sha256
                        ):
                            raise RuntimeError(
                                "committed Tracking YAML changed after completion",
                            )
                        runtime_target = TrackingTarget(
                            segment_root=segment_root,
                            yaml_path=yaml_path,
                            identity=identity,
                        )
                        runtime_targets.append(runtime_target)
                        checkpoint = checkpoints_by_target.get(
                            rendered_target.target_ref,
                        )
                        if checkpoint is None:
                            raise RuntimeError(
                                "committed Tracking target lacks a checkpoint",
                            )
                        expected_output = (
                            segment_root / f"tracking_img_{identity}"
                        )
                        expected_points = (
                            segment_root / f"img_{identity}.txt"
                        )
                        if (
                            checkpoint["revision_sha256"]
                            != revision["content_sha256"]
                            or checkpoint["identity"] != identity
                            or checkpoint["private_output_dir"]
                            != str(expected_output)
                            or checkpoint["private_points_path"]
                            != str(expected_points)
                        ):
                            raise RuntimeError(
                                "committed Tracking checkpoint mapping changed",
                            )
                        if (
                            tracking_checkpoint_artifact_sha256(
                                expected_output,
                                expected_points,
                            )
                            != checkpoint["artifact_sha256"]
                        ):
                            raise RuntimeError(
                                "committed Tracking checkpoint artifacts changed",
                            )
                except RuntimeExecutionError as exc:
                    raise RuntimeError(
                        "committed Tracking artifacts failed safety verification",
                    ) from exc
                segments.append(
                    {
                        "source_clip": segment_clip,
                        "internal_segment": segment_key,
                        "artifact_scope": relative.as_posix(),
                    },
                )
            try:
                current_prepared_sha256 = prepared_staging_artifact_sha256(
                    staging_root,
                    tuple(runtime_targets),
                )
            except RuntimeExecutionError as exc:
                raise RuntimeError(
                    "committed prepare artifacts failed safety verification",
                ) from exc
            if current_prepared_sha256 != expected_prepared_sha256:
                raise RuntimeError(
                    "committed prepare artifacts changed after completion",
                )
            if scope_kind == "segment":
                selected_scope = next(
                    segment["artifact_scope"]
                    for segment in segments
                    if segment["internal_segment"] == internal_segment
                )
            else:
                selected_scope = {
                    "prepare_maps": "maps",
                    "prepare_metadata": "v1.0-trainval",
                }[scope_kind]
                expected_top_level = {
                    ".runtime",
                    "maps",
                    "samples",
                    "v1.0-trainval",
                }
                try:
                    top_level = {
                        child.name: child.lstat()
                        for child in staging_root.iterdir()
                    }
                except OSError as exc:
                    raise RuntimeError(
                        "committed prepare-global artifacts are unavailable",
                    ) from exc
                if set(top_level) != expected_top_level or any(
                    stat.S_ISLNK(metadata.st_mode)
                    or not stat.S_ISDIR(metadata.st_mode)
                    for metadata in top_level.values()
                ):
                    raise RuntimeError(
                        "committed prepare-global artifact roots changed",
                    )
                expected_segment_scopes = {
                    f"samples/{dataset_date}/{segment['internal_segment']}"
                    for segment in segments
                }
                actual_segment_scopes = {
                    segment["artifact_scope"]
                    for segment in segments
                }
                if actual_segment_scopes != expected_segment_scopes:
                    raise RuntimeError(
                        "committed prepare-global segment layout is inconsistent",
                    )
                samples_date = staging_root / "samples" / dataset_date
                try:
                    sample_dates = {
                        child.name: child.lstat()
                        for child in (staging_root / "samples").iterdir()
                    }
                    sample_segments = {
                        child.name: child.lstat()
                        for child in samples_date.iterdir()
                    }
                except OSError as exc:
                    raise RuntimeError(
                        "committed prepare-global sample layout is unavailable",
                    ) from exc
                if (
                    set(sample_dates) != {dataset_date}
                    or any(
                        stat.S_ISLNK(metadata.st_mode)
                        or not stat.S_ISDIR(metadata.st_mode)
                        for metadata in sample_dates.values()
                    )
                    or set(sample_segments)
                    != {
                        segment["internal_segment"]
                        for segment in segments
                    }
                    or any(
                        stat.S_ISLNK(metadata.st_mode)
                        or not stat.S_ISDIR(metadata.st_mode)
                        for metadata in sample_segments.values()
                    )
                ):
                    raise RuntimeError(
                        "committed prepare-global sample roots changed",
                    )
            return {
                "source": "annotation_store",
                "run_ref": run_ref,
                "dataset_date": dataset_date,
                "source_clips": clips,
                "source_clip": source_clip,
                "scope_kind": scope_kind,
                "internal_segment": internal_segment,
                "staging_root": str(staging_root),
                "artifact_scope": selected_scope,
                "segments": segments,
                "attestation": attestation,
            }

    def _job_projection(
        self,
        connection: sqlite3.Connection,
        job_id: int,
        *,
        include_segments: bool = True,
    ) -> dict[str, Any]:
        job = self._job_row(connection, job_id)
        clips = [
            str(row["source_clip"])
            for row in connection.execute(
                """
                SELECT source_clip FROM annotation_job_source_clips
                WHERE job_id = ? ORDER BY ordinal
                """,
                (job_id,),
            ).fetchall()
        ]
        segment_rows = connection.execute(
            "SELECT * FROM annotation_segments WHERE job_id = ? ORDER BY ordinal",
            (job_id,),
        ).fetchall()
        segments = [
            self._segment_projection(connection, row, include_draft=False)
            for row in segment_rows
        ]
        all_statuses = (
            "pending_initial_annotation",
            "draft",
            "submitted",
            "skipped",
            "tracking",
            "tracked",
        )
        counts = {"total": len(segments), **{status: 0 for status in all_statuses}}
        for segment in segments:
            counts[segment["status"]] += 1
        calibration = connection.execute(
            """
            SELECT profile_ref, label, content_sha256
            FROM calibration_snapshots WHERE id = ?
            """,
            (job["calibration_snapshot_id"],),
        ).fetchone()
        resolved = counts["total"] > 0 and (
            counts["submitted"] + counts["skipped"] == counts["total"]
        )
        ready_for_tracking = (
            job["status"] == "waiting_initial_annotation"
            and resolved
            and counts["submitted"] > 0
        )
        ready_for_no_targets = (
            job["status"] == "waiting_initial_annotation"
            and counts["total"] > 0
            and counts["skipped"] == counts["total"]
        )
        failure = None
        if job["failure_code"]:
            failure = {
                "code": job["failure_code"],
                "message": job["failure_message"],
                "retryable": bool(job["failure_retryable"]),
                "error_ref": job["failure_ref"],
            }
        result = {
            "job_ref": job["job_ref"],
            "dataset_date": job["dataset_date"],
            "source_clips": clips,
            "status": job["status"],
            "completion_outcome": job["completion_outcome"],
            "cancel_requested": bool(job["cancel_requested"]),
            "state_revision": int(job["state_revision"]),
            "calibration": {
                "profile_ref": calibration["profile_ref"],
                "label": calibration["label"],
                "content_sha256": calibration["content_sha256"],
            },
            "counts": counts,
            "ready_for_tracking": ready_for_tracking,
            "ready_for_no_processable_targets": ready_for_no_targets,
            "failure": failure,
            "created_at": job["created_at"],
            "updated_at": job["updated_at"],
        }
        if include_segments:
            result["segments"] = segments
        return result

    def _segment_projection(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        include_draft: bool,
    ) -> dict[str, Any]:
        frame = {
            "url": (
                f"/api/annotation/jobs/"
                f"{self._job_ref_for_id(connection, int(row['job_id']))}/segments/"
                f"{row['segment_ref']}/first-frame"
            ),
            "width": int(row["first_frame_width"]),
            "height": int(row["first_frame_height"]),
            "sha256": row["first_frame_sha256"],
            "etag": row["first_frame_etag"],
        }
        result: dict[str, Any] = {
            "segment_ref": row["segment_ref"],
            "ordinal": int(row["ordinal"]),
            "source_clip": row["source_clip"],
            "status": row["status"],
            "state_revision": int(row["state_revision"]),
            "draft_revision": (
                int(row["draft_revision"]) if int(row["draft_revision"]) else None
            ),
            "submitted_revision": (
                int(row["submitted_revision"])
                if row["submitted_revision"] is not None
                else None
            ),
            "first_frame": frame,
        }
        if include_draft:
            draft = connection.execute(
                """
                SELECT draft_revision, targets_json
                FROM initial_annotation_drafts WHERE segment_id = ?
                """,
                (row["id"],),
            ).fetchone()
            result["draft"] = (
                {
                    "revision": int(draft["draft_revision"]),
                    "targets": json.loads(draft["targets_json"]),
                }
                if draft is not None
                else None
            )
            result["skip_reason"] = (
                {
                    "reason_code": row["skip_reason_code"],
                    "note": row["skip_note"],
                }
                if row["skip_reason_code"]
                else None
            )
        return result

    def _validate_submission(
        self,
        segment: sqlite3.Row,
        targets: list[dict[str, Any]],
    ) -> None:
        if not targets:
            raise AnnotationValidationError(
                "annotation_incomplete",
                "At least one target is required.",
            )
        width = int(segment["first_frame_width"])
        height = int(segment["first_frame_height"])
        for raw in targets:
            target = DraftAnnotationTarget.model_validate(raw)
            if target.bbox is None or target.point is None:
                raise AnnotationValidationError(
                    "annotation_incomplete",
                    "Every target requires a bounding box and foreground point.",
                )
            if any(value is None for value in target.colors.values()):
                raise AnnotationValidationError(
                    "annotation_incomplete",
                    "Every target requires upper, lower, and shoes colors.",
                )
            x, y, box_width, box_height = target.bbox
            point_x, point_y = target.point
            if (
                box_width < 0
                or box_height < 0
                or x < 0
                or y < 0
                or x + box_width > width
                or y + box_height > height
                or point_x < 0
                or point_y < 0
                or point_x >= width
                or point_y >= height
            ):
                raise AnnotationValidationError(
                    "annotation_coordinates_out_of_bounds",
                    "Annotation coordinates must remain inside the first frame.",
                )

    def _mutable_waiting_job(
        self,
        connection: sqlite3.Connection,
        job_ref: str,
    ) -> tuple[int, sqlite3.Row]:
        job_id = self._job_id(connection, job_ref)
        job = self._job_row(connection, job_id)
        if job["status"] != "waiting_initial_annotation":
            self._invalid_job_action(connection, job_id)
        return job_id, job

    def _job_id(self, connection: sqlite3.Connection, job_ref: str) -> int:
        row = connection.execute(
            "SELECT id FROM annotation_jobs WHERE job_ref = ?",
            (job_ref,),
        ).fetchone()
        if row is None:
            raise AnnotationNotFoundError("annotation job not found")
        return int(row["id"])

    def _job_ref_for_id(self, connection: sqlite3.Connection, job_id: int) -> str:
        return str(self._job_row(connection, job_id)["job_ref"])

    def _job_row(self, connection: sqlite3.Connection, job_id: int) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM annotation_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            raise AnnotationNotFoundError("annotation job not found")
        return row

    def _segment_row(
        self,
        connection: sqlite3.Connection,
        job_id: int,
        segment_ref: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT * FROM annotation_segments
            WHERE job_id = ? AND segment_ref = ?
            """,
            (job_id, segment_ref),
        ).fetchone()
        if row is None:
            raise AnnotationNotFoundError("annotation segment not found")
        return row

    def _require_job_revision(
        self,
        job: sqlite3.Row,
        expected: int,
        connection: sqlite3.Connection,
    ) -> None:
        if int(job["state_revision"]) != expected:
            raise AnnotationConflictError(
                "job_revision_conflict",
                "The annotation job changed; refresh before retrying.",
                current=self._job_projection(connection, int(job["id"])),
            )

    def _require_segment_revision(
        self,
        segment: sqlite3.Row,
        expected: int,
        connection: sqlite3.Connection,
    ) -> None:
        if int(segment["state_revision"]) != expected:
            raise AnnotationConflictError(
                "segment_revision_conflict",
                "The annotation segment changed; refresh before retrying.",
                current=self._segment_projection(connection, segment, include_draft=True),
            )

    def _invalid_job_action(
        self,
        connection: sqlite3.Connection,
        job_id: int,
    ) -> None:
        raise AnnotationConflictError(
            "invalid_job_state",
            "The requested action is unavailable in the current job state.",
            current=self._job_projection(connection, job_id),
        )

    def _invalid_segment_action(
        self,
        connection: sqlite3.Connection,
        segment: sqlite3.Row,
    ) -> None:
        raise AnnotationConflictError(
            "invalid_segment_state",
            "The requested action is unavailable in the current segment state.",
            current=self._segment_projection(connection, segment, include_draft=True),
        )

    def _touch_job(self, connection: sqlite3.Connection, job_id: int) -> None:
        connection.execute(
            """
            UPDATE annotation_jobs
            SET state_revision = state_revision + 1, updated_at = ?
            WHERE id = ?
            """,
            (_now(), job_id),
        )

    def _record_action(
        self,
        connection: sqlite3.Connection,
        segment_id: int,
        action: str,
        payload: dict[str, Any],
    ) -> None:
        connection.execute(
            """
            INSERT INTO annotation_segment_actions (
                action_ref, segment_id, action, safe_payload_json,
                actor_kind, deployment_instance, created_at
            ) VALUES (?, ?, ?, ?, 'manual_web', ?, ?)
            """,
            (
                _new_ref("segment_action"),
                segment_id,
                action,
                _canonical_json(payload),
                self.deployment_instance,
                _now(),
            ),
        )

    def _enqueue_run(
        self,
        connection: sqlite3.Connection,
        *,
        job_id: int,
        kind: str,
    ) -> str:
        row = connection.execute(
            "SELECT COALESCE(MAX(attempt), 0) + 1 AS attempt FROM runtime_runs "
            "WHERE job_id = ? AND kind = ?",
            (job_id, kind),
        ).fetchone()
        run_ref = _new_ref("run")
        timestamp = _now()
        connection.execute(
            """
            INSERT INTO runtime_runs (
                run_ref, job_id, kind, status, attempt, created_at, updated_at
            ) VALUES (?, ?, ?, 'queued', ?, ?, ?)
            """,
            (run_ref, job_id, kind, int(row["attempt"]), timestamp, timestamp),
        )
        return run_ref

    def _cancel_job(
        self,
        connection: sqlite3.Connection,
        job_id: int,
        *,
        completion_outcome: str,
        request_runtime_cancel: bool,
    ) -> None:
        timestamp = _now()
        active = connection.execute(
            """
            SELECT 1 FROM runtime_runs
            WHERE job_id = ? AND status = 'running'
            LIMIT 1
            """,
            (job_id,),
        ).fetchone()
        connection.execute(
            """
            UPDATE runtime_runs
            SET status = 'cancelled', finished_at = ?, updated_at = ?
            WHERE job_id = ? AND status = 'queued'
            """,
            (timestamp, timestamp, job_id),
        )
        if active is not None:
            if not request_runtime_cancel:
                raise RuntimeError(
                    "cannot release a source scope while its Runtime is active",
                )
            connection.execute(
                """
                UPDATE annotation_jobs
                SET state_revision = state_revision + 1,
                    cancel_requested = 1,
                    cancel_requested_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (timestamp, timestamp, job_id),
            )
            return
        self._finalize_cancelled_job(
            connection,
            job_id,
            completion_outcome=completion_outcome,
        )

    def _finalize_cancelled_job(
        self,
        connection: sqlite3.Connection,
        job_id: int,
        *,
        completion_outcome: str,
    ) -> None:
        timestamp = _now()
        connection.execute(
            """
            UPDATE annotation_jobs
            SET status = 'cancelled', completion_outcome = ?,
                state_revision = state_revision + 1,
                cancel_requested = 0,
                cancel_requested_at = NULL,
                updated_at = ?
            WHERE id = ?
            """,
            (
                completion_outcome,
                timestamp,
                job_id,
            ),
        )
        connection.execute(
            "DELETE FROM annotation_source_leases WHERE job_id = ?",
            (job_id,),
        )

    def _running_run(self, connection: sqlite3.Connection, run_id: int) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM runtime_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        if row is None or row["status"] != "running":
            raise RuntimeError("runtime run is not running")
        return row

    def _finish_run(
        self,
        connection: sqlite3.Connection,
        run_id: int,
        status: str,
    ) -> None:
        timestamp = _now()
        connection.execute(
            """
            UPDATE runtime_runs
            SET status = ?, finished_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (status, timestamp, timestamp, run_id),
        )
        connection.execute("DELETE FROM runtime_leases WHERE run_id = ?", (run_id,))

    def _fail_run_conn(
        self,
        connection: sqlite3.Connection,
        run_id: int,
        *,
        code: str,
        message: str,
        retryable: bool,
        private_detail: str | None = None,
        failure_ref: str | None = None,
    ) -> None:
        run = connection.execute(
            "SELECT * FROM runtime_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        if run is None:
            raise RuntimeError("runtime run not found")
        job = self._job_row(connection, int(run["job_id"]))
        if (
            code != "recovery_required"
            and run["status"] == "running"
            and bool(job["cancel_requested"])
        ):
            timestamp = _now()
            connection.execute(
                """
                UPDATE runtime_run_steps
                SET status = 'failed', diagnostic_ref = ?,
                    updated_at = ?
                WHERE run_id = ? AND status = 'started'
                """,
                (
                    _new_ref("runtime_step_cancelled"),
                    timestamp,
                    run_id,
                ),
            )
            self._finish_run(connection, run_id, "cancelled")
            self._finalize_cancelled_job(
                connection,
                int(run["job_id"]),
                completion_outcome="cancelled_by_user",
            )
            return
        if (
            run["status"] == "failed"
            and run["failure_code"] == "recovery_required"
        ):
            # Recovery already made the authoritative fail-closed decision.
            # A late exception from the old worker/thread must not downgrade
            # or relabel that terminal safety state.
            return
        timestamp = _now()
        resolved_failure_ref = failure_ref or _new_ref("annotation_error")
        connection.execute(
            """
            UPDATE runtime_runs
            SET status = 'failed', failure_code = ?, failure_message = ?,
                failure_ref = ?, private_failure_detail = ?,
                finished_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                code,
                message,
                resolved_failure_ref,
                private_detail,
                timestamp,
                timestamp,
                run_id,
            ),
        )
        if job["status"] != "cancelled":
            connection.execute(
                """
                UPDATE annotation_jobs
                SET status = 'failed', state_revision = state_revision + 1,
                    failure_code = ?, failure_message = ?, failure_ref = ?,
                    private_failure_detail = ?,
                    failure_retryable = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    code,
                    message,
                    resolved_failure_ref,
                    private_detail,
                    int(retryable),
                    timestamp,
                    run["job_id"],
                ),
            )
        connection.execute("DELETE FROM runtime_leases WHERE run_id = ?", (run_id,))

    def _insert_manifest(
        self,
        connection: sqlite3.Connection,
        run: sqlite3.Row,
        stage: str,
        manifest: dict[str, Any],
    ) -> None:
        encoded = _canonical_json(manifest)
        connection.execute(
            """
            INSERT INTO artifact_manifests (
                manifest_ref, job_id, run_id, stage, content_sha256,
                manifest_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _new_ref("artifact_manifest"),
                run["job_id"],
                run["id"],
                stage,
                hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
                encoded,
                _now(),
            ),
        )

    def _require_runtime_step_ledger(
        self,
        connection: sqlite3.Connection,
        run_id: int,
        declared_steps: Any,
    ) -> list[str]:
        rows = connection.execute(
            """
            SELECT safe_step_code, status, return_code, diagnostic_ref
            FROM runtime_run_steps
            WHERE run_id = ?
            ORDER BY ordinal
            """,
            (run_id,),
        ).fetchall()
        if not rows:
            raise RuntimeError("runtime step ledger is empty")
        if any(row["status"] != "succeeded" for row in rows):
            raise RuntimeError("runtime step ledger is not fully committed")
        if any(row["diagnostic_ref"] is not None for row in rows):
            raise RuntimeError("successful runtime step has a diagnostic reference")
        semantic_steps = [
            str(row["safe_step_code"])
            for row in rows
            if row["safe_step_code"] != "tracking_target_completed"
        ]
        if any(step not in _SAFE_RUNTIME_STEP_CODES for step in semantic_steps):
            raise RuntimeError("runtime step ledger contains an unsafe step")
        semantic_rows = [
            row
            for row in rows
            if row["safe_step_code"] != "tracking_target_completed"
        ]
        if any(
            row["return_code"] is None or int(row["return_code"]) != 0
            for row in semantic_rows
        ):
            raise RuntimeError("runtime step ledger lacks a successful return code")
        if (
            not isinstance(declared_steps, list)
            and not isinstance(declared_steps, tuple)
        ):
            raise RuntimeError("runtime manifest lacks declared command steps")
        if semantic_steps != list(declared_steps):
            raise RuntimeError(
                "runtime manifest and committed step ledger differ",
            )
        return semantic_steps

    @staticmethod
    def _active_reserved_bytes_conn(
        connection: sqlite3.Connection,
        *,
        excluding_job_id: int | None,
    ) -> int:
        if excluding_job_id is None:
            row = connection.execute(
                """
                SELECT COALESCE(SUM(reserved_bytes), 0) AS total
                FROM annotation_jobs j
                WHERE j.status <> 'cancelled'
                   OR EXISTS (
                       SELECT 1 FROM runtime_runs r
                       WHERE r.job_id = j.id AND r.status = 'running'
                   )
                """
            ).fetchone()
        else:
            row = connection.execute(
                """
                SELECT COALESCE(SUM(reserved_bytes), 0) AS total
                FROM annotation_jobs j
                WHERE (
                    j.status <> 'cancelled'
                    OR EXISTS (
                        SELECT 1 FROM runtime_runs r
                        WHERE r.job_id = j.id AND r.status = 'running'
                    )
                )
                  AND j.id <> ?
                """,
                (excluding_job_id,),
            ).fetchone()
        return int(row["total"])

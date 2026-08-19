from __future__ import annotations

import hashlib
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from vla_data_juicer_agents.annotation.models import (
    AnnotationConflictError,
    AnnotationValidationError,
)
from vla_data_juicer_agents.annotation.store import AnnotationStore
from vla_data_juicer_agents.navigation.config import NavigationSettings
from vla_data_juicer_agents.navigation.models import _validate_date
from vla_data_juicer_agents.navigation.task_store import SqliteNavigationTaskStore
from vla_data_juicer_agents.navigation.writer_lock import (
    NavigationWriterLockError,
    navigation_writer_lock,
)


_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class _StagedArtifact:
    label: str
    original: Path
    staged: Path


def _reset_ref(dataset_date: str, idempotency_key: str) -> str:
    digest = hashlib.sha256(
        f"navigation-dataset-reset:{dataset_date}:{idempotency_key}".encode("utf-8")
    ).hexdigest()
    return f"dataset_reset_{digest[:32]}"


def _reset_targets(
    dataset_date: str,
    settings: NavigationSettings,
) -> list[tuple[str, Path]]:
    return [
        ("raw_temp", settings.raw_data_root / f"{dataset_date}_temp"),
        ("clip_data", settings.clip_data_root / dataset_date),
        ("finish_temp", settings.finish_data_root / f"{dataset_date}_temp"),
        ("finish_data", settings.finish_data_root / dataset_date),
    ]


def _require_original_raw_data(
    dataset_date: str,
    settings: NavigationSettings,
) -> None:
    raw_date = settings.raw_data_root / dataset_date
    if raw_date.is_symlink() or not raw_date.is_dir():
        raise AnnotationValidationError(
            "raw_dataset_unavailable",
            "The original raw dataset is unavailable and cannot be reset safely.",
        )
    if not any(path.is_dir() and not path.is_symlink() for path in raw_date.iterdir()):
        raise AnnotationValidationError(
            "raw_dataset_empty",
            "The original raw dataset has no managed clips.",
        )


def _stage_artifacts(
    targets: list[tuple[str, Path]],
    *,
    reset_ref: str,
) -> list[_StagedArtifact]:
    staged: list[_StagedArtifact] = []
    try:
        for label, original in targets:
            if not original.exists() and not original.is_symlink():
                continue
            if original.is_symlink() or not original.is_dir():
                raise AnnotationValidationError(
                    "unsafe_reset_artifact",
                    "A processing artifact has an unsafe filesystem shape.",
                )
            destination = original.parent / f".{reset_ref}-{original.name}"
            if destination.exists() or destination.is_symlink():
                raise AnnotationConflictError(
                    "dataset_reset_recovery_required",
                    "A previous reset cleanup requires operator recovery.",
                )
            original.rename(destination)
            staged.append(
                _StagedArtifact(label=label, original=original, staged=destination)
            )
    except BaseException:
        _restore_staged_artifacts(staged)
        raise
    return staged


def _receipt_staged_artifacts(
    targets: list[tuple[str, Path]],
    *,
    reset_ref: str,
    removed_artifacts: object,
) -> list[_StagedArtifact]:
    if not isinstance(removed_artifacts, list):
        return []
    removed_labels = {label for label in removed_artifacts if isinstance(label, str)}
    staged: list[_StagedArtifact] = []
    for label, original in targets:
        if label not in removed_labels:
            continue
        destination = original.parent / f".{reset_ref}-{original.name}"
        if destination.is_dir() and not destination.is_symlink():
            staged.append(
                _StagedArtifact(label=label, original=original, staged=destination)
            )
    return staged


def _restore_staged_artifacts(staged: list[_StagedArtifact]) -> None:
    restore_errors: list[Exception] = []
    for artifact in reversed(staged):
        try:
            if artifact.staged.exists() and not artifact.original.exists():
                artifact.staged.rename(artifact.original)
        except OSError as exc:
            restore_errors.append(exc)
    if restore_errors:
        raise RuntimeError(
            "dataset reset rollback could not restore staged artifacts"
        ) from restore_errors[0]


def _purge_staged_artifacts(staged: list[_StagedArtifact]) -> bool:
    cleanup_pending = False
    for artifact in staged:
        try:
            shutil.rmtree(artifact.staged)
        except OSError:
            cleanup_pending = True
            _LOGGER.exception(
                "Dataset reset committed but staged cleanup failed: reset artifact=%s",
                artifact.label,
            )
    return cleanup_pending


def reset_navigation_dataset(
    *,
    dataset_date: str,
    confirmation: str,
    idempotency_key: str,
    annotation_store: AnnotationStore,
    task_store: SqliteNavigationTaskStore,
    settings: NavigationSettings | None = None,
) -> dict[str, object]:
    """Reset one unreleased date to its original raw-data state."""

    _validate_date(dataset_date)
    if confirmation.strip() != dataset_date:
        raise AnnotationValidationError(
            "dataset_reset_confirmation_mismatch",
            "The reset confirmation does not match the dataset date.",
        )
    settings = settings or NavigationSettings()
    reset_ref = _reset_ref(dataset_date, idempotency_key)
    request_payload = {
        "dataset_date": dataset_date,
        "reset_ref": reset_ref,
    }
    replay = annotation_store.replay_receipt(
        idempotency_key=idempotency_key,
        operation="reset_unreleased_dataset",
        request_payload=request_payload,
    )
    if replay is not None:
        cleanup_pending = _purge_staged_artifacts(
            _receipt_staged_artifacts(
                _reset_targets(dataset_date, settings),
                reset_ref=reset_ref,
                removed_artifacts=replay.get("removed_artifacts"),
            )
        )
        return {**replay, "cleanup_pending": cleanup_pending}

    annotation_store.dataset_reset_preflight(dataset_date=dataset_date)
    _require_original_raw_data(dataset_date, settings)
    if task_store.find_nonterminal_for_date(dataset_date) is not None:
        raise AnnotationConflictError(
            "navigation_task_active",
            "The dataset has an active DataPilot task and cannot be reset.",
        )
    staged: list[_StagedArtifact] = []
    try:
        with navigation_writer_lock():
            if task_store.find_nonterminal_for_date(dataset_date) is not None:
                raise AnnotationConflictError(
                    "navigation_task_active",
                    "The dataset has an active DataPilot task and cannot be reset.",
                )
            annotation_store.dataset_reset_preflight(dataset_date=dataset_date)
            staged = _stage_artifacts(
                _reset_targets(dataset_date, settings),
                reset_ref=reset_ref,
            )
            try:
                result = annotation_store.reset_unreleased_dataset(
                    dataset_date=dataset_date,
                    reset_ref=reset_ref,
                    removed_artifacts=[artifact.label for artifact in staged],
                    idempotency_key=idempotency_key,
                )
            except BaseException:
                _restore_staged_artifacts(staged)
                raise
    except NavigationWriterLockError as exc:
        raise AnnotationConflictError(
            "navigation_writer_unavailable",
            "Navigation processing is active or requires operator recovery.",
        ) from exc

    cleanup_pending = _purge_staged_artifacts(staged)
    return {
        **result,
        "cleanup_pending": cleanup_pending,
    }

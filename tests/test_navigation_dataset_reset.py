from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from vla_data_juicer_agents.annotation.models import (
    AnnotationConflictError,
    AnnotationValidationError,
)
from vla_data_juicer_agents.annotation.store import AnnotationStore
from vla_data_juicer_agents.navigation.config import NavigationSettings
from vla_data_juicer_agents.navigation.dataset_reset import reset_navigation_dataset
from vla_data_juicer_agents.navigation.task_store import SqliteNavigationTaskStore


DATASET_DATE = "20270623"
SOURCE_CLIP = "20260623_145550"


def _stores(tmp_path: Path) -> tuple[AnnotationStore, SqliteNavigationTaskStore]:
    tmp_path.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp_path.chmod(0o700)
    return (
        AnnotationStore(tmp_path / "annotation.sqlite"),
        SqliteNavigationTaskStore(tmp_path / "navigation.sqlite"),
    )


def _settings(tmp_path: Path) -> NavigationSettings:
    return NavigationSettings(
        vladatasets_root=tmp_path / "VLADatasets",
        processing_root=tmp_path / "processing",
    )


def _seed_files(settings: NavigationSettings) -> dict[str, Path]:
    raw_clip = settings.raw_data_root / DATASET_DATE / SOURCE_CLIP
    raw_clip.mkdir(parents=True)
    (raw_clip / "metadata.yaml").write_text("raw: true\n", encoding="utf-8")
    targets = {
        "raw_temp": settings.raw_data_root / f"{DATASET_DATE}_temp",
        "clip_data": settings.clip_data_root / DATASET_DATE,
        "finish_temp": settings.finish_data_root / f"{DATASET_DATE}_temp",
        "finish_data": settings.finish_data_root / DATASET_DATE,
    }
    for label, target in targets.items():
        target.mkdir(parents=True)
        (target / f"{label}.txt").write_text("derived\n", encoding="utf-8")
    return targets


def _seed_completed_job(store: AnnotationStore, tmp_path: Path) -> str:
    job_ref = "job_" + "1" * 32
    store.create_job(
        job_ref=job_ref,
        dataset_date=DATASET_DATE,
        source_clips=[SOURCE_CLIP],
        calibration={
            "profile_ref": "20260529_go2w",
            "label": "20260529_go2w",
            "content_sha256": "a" * 64,
        },
        snapshot_dir=tmp_path / "calibration",
        snapshot_files=[],
        reserved_bytes=1,
        idempotency_key="seed-completed-job",
    )
    with store._write() as connection:
        job_id = int(
            connection.execute(
                "SELECT id FROM annotation_jobs WHERE job_ref = ?",
                (job_ref,),
            ).fetchone()["id"]
        )
        connection.execute("DELETE FROM runtime_runs WHERE job_id = ?", (job_id,))
        connection.execute(
            "UPDATE annotation_jobs SET status = 'annotated' WHERE id = ?",
            (job_id,),
        )
    return job_ref


def _configure_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lock_root = tmp_path / "private-lock"
    lock_root.mkdir(mode=0o700)
    lock_root.chmod(0o700)
    monkeypatch.setenv(
        "VLA_NAVIGATION_WRITER_LOCK_PATH",
        str(lock_root / "navigation.lock"),
    )


def test_reset_keeps_raw_data_removes_outputs_and_releases_annotation_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    annotation_store, task_store = _stores(tmp_path)
    settings = _settings(tmp_path)
    targets = _seed_files(settings)
    old_job_ref = _seed_completed_job(annotation_store, tmp_path)
    _configure_lock(tmp_path, monkeypatch)

    result = reset_navigation_dataset(
        dataset_date=DATASET_DATE,
        confirmation=DATASET_DATE,
        idempotency_key="reset-once",
        annotation_store=annotation_store,
        task_store=task_store,
        settings=settings,
    )

    raw_file = settings.raw_data_root / DATASET_DATE / SOURCE_CLIP / "metadata.yaml"
    assert raw_file.read_text(encoding="utf-8") == "raw: true\n"
    assert all(not target.exists() for target in targets.values())
    assert result == {
        "reset_ref": result["reset_ref"],
        "dataset_date": DATASET_DATE,
        "status": "raw_only",
        "retired_job_count": 1,
        "released_source_clip_count": 1,
        "removed_artifacts": [
            "raw_temp",
            "clip_data",
            "finish_temp",
            "finish_data",
        ],
        "cleanup_pending": False,
    }
    with sqlite3.connect(annotation_store.db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM annotation_source_leases WHERE dataset_date = ?",
            (DATASET_DATE,),
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT status FROM annotation_jobs WHERE job_ref = ?",
            (old_job_ref,),
        ).fetchone()[0] == "annotated"

    replacement = annotation_store.create_job(
        job_ref="job_" + "2" * 32,
        dataset_date=DATASET_DATE,
        source_clips=[SOURCE_CLIP],
        calibration={
            "profile_ref": "20260529_go2w",
            "label": "20260529_go2w",
            "content_sha256": "b" * 64,
        },
        snapshot_dir=tmp_path / "replacement-calibration",
        snapshot_files=[],
        reserved_bytes=1,
        idempotency_key="replacement-job",
    )
    assert replacement["status"] == "preparing"


def test_released_dataset_cannot_be_reset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    annotation_store, task_store = _stores(tmp_path)
    settings = _settings(tmp_path)
    targets = _seed_files(settings)
    _configure_lock(tmp_path, monkeypatch)
    with annotation_store._write() as connection:
        connection.execute(
            """INSERT INTO dataset_releases (
                   release_ref, domain, dataset_date, scope_manifest_sha256,
                   scope_json, source_clip_count, total_duration_ns,
                   verified_unit_count, discarded_unit_count, note,
                   actor_kind, deployment_instance, released_at
               ) VALUES (?, 'navigation', ?, ?, '{}', 1, 0, 1, 0, NULL,
                         'manual_web', 'test', ?)""",
            ("dataset_release_" + "3" * 32, DATASET_DATE, "c" * 64, "2026-08-19T00:00:00Z"),
        )

    with pytest.raises(AnnotationConflictError) as caught:
        reset_navigation_dataset(
            dataset_date=DATASET_DATE,
            confirmation=DATASET_DATE,
            idempotency_key="released-reset",
            annotation_store=annotation_store,
            task_store=task_store,
            settings=settings,
        )

    assert caught.value.code == "dataset_already_released"
    assert all(target.is_dir() for target in targets.values())


def test_reset_cancels_active_navigation_task_and_invalidates_its_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    annotation_store, task_store = _stores(tmp_path)
    settings = _settings(tmp_path)
    targets = _seed_files(settings)
    _configure_lock(tmp_path, monkeypatch)
    creation = task_store.create_task_attempt(
        request="process data",
        target="all",
        date=DATASET_DATE,
        segments=None,
        scene_mode=None,
        dry_run=False,
        web_session_id="web-session",
        agentscope_session_id="agentscope-session",
    )
    task_id = creation.task.task_id
    plan_id = "plan_" + "3" * 32
    with task_store._connect() as connection:
        connection.execute(
            """INSERT INTO navigation_plans (
                   plan_id, task_id, phase, plan_revision, contract_version,
                   observation_revision, plan_json, validation_summary_json,
                   status, invalidation_reason, created_at, updated_at
               ) VALUES (?, ?, 'extract_sync', 1, 'test', 1, ?, '{}',
                         'active', NULL, ?, ?)""",
            (
                plan_id,
                task_id,
                '{"steps":[{"step_id":"step-1","action":"inspect"}]}',
                creation.task.created_at,
                creation.task.updated_at,
            ),
        )
        connection.execute(
            """INSERT INTO navigation_task_steps (
                   id, task_id, phase, step_id, tool_name, status,
                   plan_id, plan_revision, sequence
               ) VALUES (?, ?, 'extract_sync', 'step-1', 'inspect',
                         'pending', ?, 1, 1)""",
            ("ledger_" + "4" * 32, task_id, plan_id),
        )
        connection.execute(
            """INSERT INTO navigation_step_result_outbox (
                   plan_id, step_id, task_id, plan_revision, target_status,
                   expected_statuses_json, full_result_json,
                   result_summary_json, result_ref, created_at, updated_at
               ) VALUES (?, 'step-1', ?, 1, 'completed', '[\"running\"]',
                         '{}', '{}', ?, ?, ?)""",
            (
                plan_id,
                task_id,
                "result_" + "5" * 32,
                creation.task.created_at,
                creation.task.updated_at,
            ),
        )

    result = reset_navigation_dataset(
        dataset_date=DATASET_DATE,
        confirmation=DATASET_DATE,
        idempotency_key="active-navigation-reset",
        annotation_store=annotation_store,
        task_store=task_store,
        settings=settings,
    )

    assert result["status"] == "raw_only"
    assert all(not target.exists() for target in targets.values())
    assert task_store.get_task(task_id).status.value == "cancelled"
    with task_store._connect() as connection:
        plan = connection.execute(
            "SELECT status, invalidation_reason FROM navigation_plans WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
        assert plan["status"] == "invalidated"
        assert plan["invalidation_reason"].startswith("dataset_reset:")
        assert connection.execute(
            "SELECT status FROM navigation_task_steps WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()["status"] == "failed"
        assert connection.execute(
            "SELECT COUNT(*) FROM navigation_step_result_outbox WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()[0] == 0


def test_reset_still_rejects_active_annotation_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    second_root = tmp_path / "second"
    annotation_store, task_store = _stores(second_root)
    settings = _settings(second_root)
    targets = _seed_files(settings)
    _configure_lock(tmp_path, monkeypatch)

    _seed_completed_job(annotation_store, second_root)
    with annotation_store._write() as connection:
        connection.execute("UPDATE annotation_jobs SET status = 'preparing'")

    with pytest.raises(AnnotationConflictError) as annotation_conflict:
        reset_navigation_dataset(
            dataset_date=DATASET_DATE,
            confirmation=DATASET_DATE,
            idempotency_key="active-annotation-reset",
            annotation_store=annotation_store,
            task_store=task_store,
            settings=settings,
        )
    assert annotation_conflict.value.code == "annotation_workflow_active"
    assert all(target.is_dir() for target in targets.values())


def test_reset_requires_exact_date_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    annotation_store, task_store = _stores(tmp_path)
    settings = _settings(tmp_path)
    targets = _seed_files(settings)
    _configure_lock(tmp_path, monkeypatch)

    with pytest.raises(AnnotationValidationError) as caught:
        reset_navigation_dataset(
            dataset_date=DATASET_DATE,
            confirmation="20270624",
            idempotency_key="wrong-confirmation",
            annotation_store=annotation_store,
            task_store=task_store,
            settings=settings,
        )

    assert caught.value.code == "dataset_reset_confirmation_mismatch"
    assert all(target.is_dir() for target in targets.values())


def test_database_failure_restores_every_staged_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    annotation_store, task_store = _stores(tmp_path)
    settings = _settings(tmp_path)
    targets = _seed_files(settings)
    _configure_lock(tmp_path, monkeypatch)

    def fail_reset(**_kwargs: object) -> dict[str, object]:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(annotation_store, "reset_unreleased_dataset", fail_reset)
    with pytest.raises(RuntimeError, match="database unavailable"):
        reset_navigation_dataset(
            dataset_date=DATASET_DATE,
            confirmation=DATASET_DATE,
            idempotency_key="failed-reset",
            annotation_store=annotation_store,
            task_store=task_store,
            settings=settings,
        )

    assert all(target.is_dir() for target in targets.values())
    assert not list(settings.vladatasets_root.rglob(".dataset_reset_*"))


def test_reset_idempotency_replays_without_touching_new_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    annotation_store, task_store = _stores(tmp_path)
    settings = _settings(tmp_path)
    _seed_files(settings)
    _configure_lock(tmp_path, monkeypatch)

    first = reset_navigation_dataset(
        dataset_date=DATASET_DATE,
        confirmation=DATASET_DATE,
        idempotency_key="repeat-reset",
        annotation_store=annotation_store,
        task_store=task_store,
        settings=settings,
    )
    new_output = settings.clip_data_root / DATASET_DATE / "new-output.txt"
    new_output.parent.mkdir(parents=True)
    new_output.write_text("new generation", encoding="utf-8")

    replay = reset_navigation_dataset(
        dataset_date=DATASET_DATE,
        confirmation=DATASET_DATE,
        idempotency_key="repeat-reset",
        annotation_store=annotation_store,
        task_store=task_store,
        settings=settings,
    )

    assert replay == first
    assert new_output.read_text(encoding="utf-8") == "new generation"

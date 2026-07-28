from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import vla_data_juicer_agents.navigation.migrations as migration_module
from vla_data_juicer_agents.annotation.maintenance import (
    AnnotationServiceOnlineError,
    acquire_annotation_maintenance,
)
from vla_data_juicer_agents.navigation.migrations import (
    migrate_navigation_store_offline,
)
from vla_data_juicer_agents.navigation.schema import (
    LATEST_NAVIGATION_SCHEMA_VERSION,
    M1_NAVIGATION_INDEX_SQL,
    M1_NAVIGATION_TABLE_SQL,
    M1_NAVIGATION_TRIGGER_SQL,
    NAVIGATION_STATE_SCHEMA_GENERATION,
    PREVIOUS_NAVIGATION_STATE_SCHEMA_GENERATION,
    NavigationOfflineMigrationRequired,
    NavigationMigrationSafetyError,
    UnsupportedNavigationSchemaVersion,
)
from vla_data_juicer_agents.navigation.task_store import (
    SqliteNavigationTaskStore,
)


NOW = "2026-07-28T00:00:00+00:00"


def _create_m1_store(path: Path) -> dict[str, object]:
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        for statement in M1_NAVIGATION_TABLE_SQL.values():
            connection.execute(statement)
        for statement in M1_NAVIGATION_INDEX_SQL.values():
            connection.execute(statement)
        for statement in M1_NAVIGATION_TRIGGER_SQL.values():
            connection.execute(statement)
        connection.execute(
            "INSERT INTO navigation_state_schema VALUES (1, ?)",
            (PREVIOUS_NAVIGATION_STATE_SCHEMA_GENERATION,),
        )
        connection.execute(
            """
            INSERT INTO navigation_tasks (
                task_id, request, target, date, segments_json, segments_key,
                scene_mode, dry_run, guidance_revision, state_revision, status,
                accepted_plan_phase, created_by_web_session_id,
                agentscope_session_id, schema_version, created_at, updated_at
            ) VALUES (
                'nav-history', '历史请求', 'navigation_data', '20260623',
                '["clip-a"]', '["clip-a"]', NULL, 0, 3, 8, 'completed',
                'finish_processing', 'web-history', 'agent-history', 3, ?, ?
            )
            """,
            (NOW, NOW),
        )
        connection.execute(
            """
            INSERT INTO navigation_observation_revisions
            VALUES ('nav-history', 1, '{"revision":1}', ?)
            """,
            (NOW,),
        )
        connection.execute(
            """
            INSERT INTO navigation_evidence
            VALUES (
                'evidence-history', 'nav-history', 1, 'artifact',
                '历史证据', 12, 'inspect_navigation_data', ?
            )
            """,
            (NOW,),
        )
        for revision, contract, status in (
            (1, "navigation-plan-v1", "superseded"),
            (2, "navigation-plan-v2", "superseded"),
            (3, "navigation-plan-v3", "completed"),
        ):
            connection.execute(
                """
                INSERT INTO navigation_plans (
                    plan_id, task_id, phase, plan_revision, contract_version,
                    observation_revision, plan_json, validation_summary_json,
                    status, invalidation_reason, created_at, updated_at
                ) VALUES (
                    ?, 'nav-history', 'finish_processing', ?, ?, 1,
                    '{"steps":[]}', '{"valid":true}', ?, NULL, ?, ?
                )
                """,
                (
                    f"plan-history-v{revision}",
                    revision,
                    contract,
                    status,
                    NOW,
                    NOW,
                ),
            )
            connection.execute(
                """
                INSERT INTO navigation_plan_submission_attempts
                VALUES (
                    ?, 'nav-history', 'finish_processing', ?,
                    '{"phase":"finish_processing"}', '{"accepted":true}', ?
                )
                """,
                (
                    f"attempt-history-v{revision}",
                    f"context-v{revision}",
                    NOW,
                ),
            )
        connection.commit()
        tables = (
            "navigation_tasks",
            "navigation_observation_revisions",
            "navigation_evidence",
            "navigation_plans",
            "navigation_plan_submission_attempts",
        )
        return {
            table: connection.execute(
                f'SELECT * FROM "{table}" ORDER BY rowid'
            ).fetchall()
            for table in tables
        }


def _rows(path: Path, table: str) -> list[tuple[object, ...]]:
    with sqlite3.connect(path) as connection:
        return connection.execute(
            f'SELECT * FROM "{table}" ORDER BY rowid'
        ).fetchall()


def test_m1_store_requires_explicit_offline_migration_without_mutation(
    tmp_path: Path,
) -> None:
    database = tmp_path / "navigation-tasks.sqlite"
    before = _create_m1_store(database)

    with pytest.raises(NavigationOfflineMigrationRequired):
        SqliteNavigationTaskStore(database)

    assert _rows(database, "navigation_plans") == before["navigation_plans"]
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT generation FROM navigation_state_schema"
        ).fetchone() == (PREVIOUS_NAVIGATION_STATE_SCHEMA_GENERATION,)


def test_offline_migration_preserves_historical_plan_and_evidence_ledgers(
    tmp_path: Path,
) -> None:
    database = tmp_path / "navigation-tasks.sqlite"
    before = _create_m1_store(database)
    backup = tmp_path / "navigation-migration-backup-test"

    result = migrate_navigation_store_offline(
        database,
        backup_root=backup,
    )
    store = SqliteNavigationTaskStore(database)

    for table, rows in before.items():
        assert _rows(database, table) == rows
    assert store.get_task("nav-history") is not None
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT generation FROM navigation_state_schema"
        ).fetchone() == (NAVIGATION_STATE_SCHEMA_GENERATION,)
        assert connection.execute(
            """
            SELECT version, source_generation, target_generation
            FROM navigation_schema_migrations
            """
        ).fetchone() == (
            LATEST_NAVIGATION_SCHEMA_VERSION,
            PREVIOUS_NAVIGATION_STATE_SCHEMA_GENERATION,
            NAVIGATION_STATE_SCHEMA_GENERATION,
        )
        assert connection.execute(
            """
            SELECT schema_version, status
            FROM navigation_migration_safety
            """
        ).fetchone() == (LATEST_NAVIGATION_SCHEMA_VERSION, "verified")
        assert connection.execute(
            """
            SELECT task_id, requested_outcome, completion_outcome, metadata_json
            FROM navigation_task_outcomes
            """
        ).fetchone() == ("nav-history", "auto", None, "{}")
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        connection.execute(
            """
            INSERT INTO navigation_plan_submission_attempts
            VALUES (
                'attempt-new-m2', 'nav-history', 'trajectory_review',
                'context-m2', '{"phase":"trajectory_review"}',
                '{"accepted":true}', ?
            )
            """,
            (NOW,),
        )
    manifest = json.loads(
        (backup / "backup-manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["source_generation"] == PREVIOUS_NAVIGATION_STATE_SCHEMA_GENERATION
    assert manifest["target_generation"] == NAVIGATION_STATE_SCHEMA_GENERATION
    assert result["backup_manifest_sha256"]


def test_transaction_failure_rolls_back_to_exact_m1_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "navigation-tasks.sqlite"
    before = _create_m1_store(database)
    original = migration_module._apply_m1_to_m2

    def fail_after_changes(
        connection: sqlite3.Connection,
        *,
        applied_at: str,
    ) -> None:
        original(connection, applied_at=applied_at)
        raise RuntimeError("injected migration failure")

    monkeypatch.setattr(migration_module, "_apply_m1_to_m2", fail_after_changes)
    with pytest.raises(RuntimeError, match="injected migration failure"):
        migrate_navigation_store_offline(
            database,
            backup_root=tmp_path / "navigation-migration-backup-rollback",
        )

    assert _rows(database, "navigation_plans") == before["navigation_plans"]
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT generation FROM navigation_state_schema"
        ).fetchone() == (PREVIOUS_NAVIGATION_STATE_SCHEMA_GENERATION,)
        assert connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type='table' AND name='navigation_schema_migrations'
            """
        ).fetchone() is None


def test_post_commit_verification_failure_stays_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "navigation-tasks.sqlite"
    _create_m1_store(database)
    monkeypatch.setattr(
        migration_module,
        "_verify_and_mark",
        lambda _connection: (_ for _ in ()).throw(
            RuntimeError("injected integrity failure")
        ),
    )

    with pytest.raises(RuntimeError, match="injected integrity failure"):
        migrate_navigation_store_offline(
            database,
            backup_root=tmp_path / "navigation-migration-backup-integrity",
        )
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT status FROM navigation_migration_safety"
        ).fetchone() == ("pending_integrity_check",)
    with pytest.raises(
        NavigationMigrationSafetyError,
        match="verification is incomplete",
    ):
        SqliteNavigationTaskStore(database)


def test_future_navigation_schema_version_is_rejected(tmp_path: Path) -> None:
    database = tmp_path / "navigation-tasks.sqlite"
    SqliteNavigationTaskStore(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            INSERT INTO navigation_schema_migrations (
                version, name, source_generation, target_generation, applied_at
            ) VALUES (2, 'future', ?, ?, ?)
            """,
            (
                NAVIGATION_STATE_SCHEMA_GENERATION,
                NAVIGATION_STATE_SCHEMA_GENERATION,
                NOW,
            ),
        )

    with pytest.raises(UnsupportedNavigationSchemaVersion):
        SqliteNavigationTaskStore(database)


def test_missing_navigation_migration_ledger_entry_is_fail_closed(
    tmp_path: Path,
) -> None:
    database = tmp_path / "navigation-tasks.sqlite"
    SqliteNavigationTaskStore(database)
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM navigation_schema_migrations")

    with pytest.raises(
        NavigationMigrationSafetyError,
        match="ledger is not current",
    ):
        SqliteNavigationTaskStore(database)


def test_offline_migration_refuses_while_service_lifecycle_lock_is_held(
    tmp_path: Path,
) -> None:
    database = tmp_path / "navigation-tasks.sqlite"
    _create_m1_store(database)

    with acquire_annotation_maintenance(database, create_lock_file=True):
        with pytest.raises(AnnotationServiceOnlineError):
            migrate_navigation_store_offline(
                database,
                backup_root=tmp_path / "navigation-migration-backup-locked",
            )

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT generation FROM navigation_state_schema"
        ).fetchone() == (PREVIOUS_NAVIGATION_STATE_SCHEMA_GENERATION,)

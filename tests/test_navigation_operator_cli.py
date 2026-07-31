from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from vla_data_juicer_agents.navigation import operator_cli
from vla_data_juicer_agents.navigation.schema import (
    M1_NAVIGATION_INDEX_SQL,
    M1_NAVIGATION_TABLE_SQL,
    M1_NAVIGATION_TRIGGER_SQL,
    PREVIOUS_NAVIGATION_STATE_SCHEMA_GENERATION,
)


def _create_m1_store(path: Path) -> dict[str, object]:
    with sqlite3.connect(path) as connection:
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
                dry_run, guidance_revision, state_revision, status,
                created_by_web_session_id, agentscope_session_id,
                schema_version, created_at, updated_at
            ) VALUES (
                'nav-history', '历史', 'navigation_data', '20260623',
                '["clip-a"]', '["clip-a"]', 0, 0, 0, 'completed',
                'web-history', 'agent-history', 3,
                '2026-07-28T00:00:00+00:00',
                '2026-07-28T00:00:00+00:00'
            )
            """
        )
    return _create_snapshot(path)


def test_operator_migrates_only_bound_production_navigation_store(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    working = tmp_path / "working"
    working.mkdir(mode=0o700)
    database = working / "navigation-tasks.sqlite"
    writer_lock = working / "writer.lock"
    writer_lock.touch(mode=0o600)
    backup = working / "navigation-migration-backup-test"
    _create_m1_store(database)
    monkeypatch.setenv("VLA_DATA_AGENT_WEB_WORKING_DIR", str(working))
    monkeypatch.setenv("VLA_NAVIGATION_WRITER_LOCK_PATH", str(writer_lock))

    assert operator_cli.main(
        [
            "--navigation-db",
            str(database),
            "--writer-lock",
            str(writer_lock),
            "migrate-schema",
            "--backup-root",
            str(backup),
        ]
    ) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "migrated"
    assert "backup_root" not in payload


def test_operator_rejects_unbound_database_without_mutation(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    working = tmp_path / "working"
    working.mkdir(mode=0o700)
    database = tmp_path / "other.sqlite"
    writer_lock = working / "writer.lock"
    writer_lock.touch(mode=0o600)
    before = _create_m1_store(database)
    monkeypatch.setenv("VLA_DATA_AGENT_WEB_WORKING_DIR", str(working))
    monkeypatch.setenv("VLA_NAVIGATION_WRITER_LOCK_PATH", str(writer_lock))

    assert operator_cli.main(
        [
            "--navigation-db",
            str(database),
            "--writer-lock",
            str(writer_lock),
            "migrate-schema",
            "--backup-root",
            str(working / "navigation-migration-backup-test"),
        ]
    ) == 2

    assert _create_snapshot(database) == before
    assert json.loads(capsys.readouterr().err)["code"] == "invalid_scope"


def _create_snapshot(database: Path) -> dict[str, object]:
    with sqlite3.connect(database) as connection:
        return {
            table: connection.execute(
                f'SELECT * FROM "{table}" ORDER BY rowid'
            ).fetchall()
            for table in (
                "navigation_tasks",
                "navigation_observation_revisions",
                "navigation_evidence",
                "navigation_plans",
                "navigation_plan_submission_attempts",
            )
        }

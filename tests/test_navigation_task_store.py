import json
import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from pathlib import Path

import pytest

import vla_data_juicer_agents.navigation.task_store as task_store_module
from vla_data_juicer_agents.navigation.plan_store import (
    SqliteNavigationPlanRepository,
)
from vla_data_juicer_agents.navigation.task_state import (
    TASK_SCHEMA_VERSION,
    NavigationTaskStatus,
)
from vla_data_juicer_agents.navigation.task_store import (
    NavigationStateResetRequired,
    SqliteNavigationTaskStore,
)


_CORE_SCHEMA_OBJECTS = (
    "navigation_state_schema",
    "navigation_tasks",
    "navigation_task_steps",
    "navigation_observation_revisions",
    "navigation_evidence",
    "idx_navigation_tasks_date_updated",
    "idx_navigation_tasks_target_history",
    "idx_navigation_tasks_session",
    "idx_navigation_tasks_attempt_replay",
    "idx_navigation_task_steps_plan_sequence",
    "idx_navigation_task_steps_plan_step_id",
    "idx_navigation_evidence_task_revision_kind",
)


def _create_supported_schema_variant(
    tmp_path: Path,
    db_path: Path,
    *,
    task_sql_transform: Callable[[str], str],
) -> None:
    canonical_path = tmp_path / f"canonical-{db_path.name}"
    SqliteNavigationTaskStore(canonical_path)
    placeholders = ", ".join("?" for _ in _CORE_SCHEMA_OBJECTS)
    with sqlite3.connect(canonical_path) as connection:
        sql_by_name = dict(
            connection.execute(
                f"SELECT name, sql FROM sqlite_master WHERE name IN ({placeholders})",
                _CORE_SCHEMA_OBJECTS,
            ).fetchall()
        )

    with sqlite3.connect(db_path) as connection:
        connection.execute(sql_by_name["navigation_state_schema"])
        connection.execute(
            "INSERT INTO navigation_state_schema VALUES (1, ?)",
            ("navigation-attempts-transitional-v1",),
        )
        connection.execute(task_sql_transform(sql_by_name["navigation_tasks"]))
        connection.execute(sql_by_name["navigation_task_steps"])
        for name in _CORE_SCHEMA_OBJECTS[3:]:
            connection.execute(sql_by_name[name])


def _insert_plan_ledger_step(
    db_path: Path,
    *,
    task_id: str,
    ledger_action: str,
    plan_action: str | None = None,
    status: str = "running",
) -> tuple[str, str]:
    SqliteNavigationPlanRepository(db_path)
    plan_id = f"plan-{task_id}"
    step_id = "step-1"
    plan_payload = {
        "steps": [
            {
                "step_id": step_id,
                "action": plan_action or ledger_action,
            }
        ]
    }
    with sqlite3.connect(db_path) as connection:
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
                json.dumps(plan_payload),
                "2026-07-08T00:00:00.000+00:00",
                "2026-07-08T00:00:00.000+00:00",
            ),
        )
        connection.execute(
            """INSERT INTO navigation_task_steps (
                   id, task_id, phase, step_id, tool_name, status,
                   plan_id, plan_revision, sequence
               ) VALUES (?, ?, 'extract_sync', ?, ?, ?, ?, 1, 1)""",
            (
                f"ledger-{task_id}",
                task_id,
                step_id,
                ledger_action,
                status,
                plan_id,
            ),
        )
    return plan_id, step_id


def test_task_store_exposes_only_attempt_scoped_mutators(tmp_path: Path):
    store = SqliteNavigationTaskStore(tmp_path / "navigation_tasks.sqlite")

    assert not hasattr(store, "create_or_" + "update_task")
    assert not hasattr(store, "find_latest_" + "by_date")
    assert not hasattr(store, "restore_task_" + "exact_if_current")
    assert not hasattr(store, "update_" + "task")
    assert hasattr(store, "delete_task_if_current")
    assert hasattr(store, "update_task_for_session")


def test_task_store_idempotently_creates_supported_schema_generation(tmp_path: Path):
    db_path = tmp_path / "navigation_tasks.sqlite"

    SqliteNavigationTaskStore(db_path)
    SqliteNavigationTaskStore(db_path)

    with sqlite3.connect(db_path) as connection:
        generation = connection.execute(
            "SELECT generation FROM navigation_state_schema WHERE singleton = 1"
        ).fetchone()[0]
        task_columns = {
            row[1] for row in connection.execute(
                "PRAGMA table_info(navigation_tasks)"
            )
        }
        ledger_columns = {
            row[1] for row in connection.execute(
                "PRAGMA table_info(navigation_task_steps)"
            )
        }
        indexes = {
            row[1]: bool(row[2])
            for row in connection.execute("PRAGMA index_list(navigation_tasks)")
        }
    assert generation == "navigation-attempts-final-v2"
    assert task_columns == {
        "task_id",
        "request",
        "target",
        "date",
        "segments_json",
        "segments_key",
        "scene_mode",
        "dry_run",
        "guidance_revision",
        "state_revision",
        "status",
        "accepted_plan_phase",
        "created_by_web_session_id",
        "agentscope_session_id",
        "schema_version",
        "created_at",
        "updated_at",
    }
    assert {
        "plan_id",
        "plan_revision",
        "sequence",
        "result_summary_json",
        "result_ref",
        "retry_count",
    } <= ledger_columns
    assert indexes["idx_navigation_tasks_target_history"] is False
    assert indexes["idx_navigation_tasks_session"] is False
    assert indexes["idx_navigation_tasks_attempt_replay"] is True
    assert "idx_navigation_tasks_active_date_segments_key" not in indexes


def test_task_store_creates_and_loads_navigation_task(tmp_path: Path):
    store = SqliteNavigationTaskStore(tmp_path / "navigation_tasks.sqlite")

    creation = store.create_task_attempt(
        request="Process the requested navigation data.",
        target="20270623",
        date="20270623",
        segments=["20260623_101010"],
        scene_mode=None,
        dry_run=False,
        web_session_id="web-1",
        agentscope_session_id="agent-1",
    )
    task = creation.task

    loaded = store.get_task(task.task_id)

    assert loaded is not None
    assert loaded.task_id == task.task_id
    assert loaded.date == "20270623"
    assert loaded.segments == ["20260623_101010"]
    assert loaded.scene_mode is None
    assert loaded.status == NavigationTaskStatus.ACTIVE
    assert loaded.created_by_web_session_id == "web-1"
    assert loaded.agentscope_session_id == "agent-1"
    assert loaded.schema_version == TASK_SCHEMA_VERSION


def test_new_web_sessions_create_distinct_attempts_for_same_target(tmp_path: Path):
    store = SqliteNavigationTaskStore(tmp_path / "tasks.sqlite")
    first = store.create_task_attempt(
        request="处理数据",
        target="20270623",
        date="20270623",
        segments=["20260623_145550"],
        scene_mode=None,
        dry_run=False,
        web_session_id="web-a",
        agentscope_session_id="as-a",
    )
    second = store.create_task_attempt(
        request="继续处理",
        target="20270623",
        date="20270623",
        segments=["20260623_145550"],
        scene_mode=None,
        dry_run=False,
        web_session_id="web-b",
        agentscope_session_id="as-b",
    )

    assert first.created is True
    assert second.created is True
    assert first.task.request == "处理数据"
    assert first.task.target == "20270623"
    assert first.task.status.value == "active"
    assert first.task.accepted_plan_phase is None
    assert first.task_id != second.task_id
    found = store.find_by_session(
        web_session_id="web-b", agentscope_session_id="as-b"
    )
    assert found is not None
    assert found.task_id == second.task_id


def test_foreign_session_cannot_mutate_an_attempt(tmp_path: Path):
    store = SqliteNavigationTaskStore(tmp_path / "tasks.sqlite")
    task = store.create_task_attempt(
        request="处理数据",
        target="20270623",
        date="20270623",
        segments=None,
        scene_mode=None,
        dry_run=False,
        web_session_id="web-a",
        agentscope_session_id="as-a",
    )

    with pytest.raises(task_store_module.NavigationTaskOwnershipError):
        store.update_task_for_session(
            task.task_id,
            web_session_id="web-b",
            agentscope_session_id="as-b",
            status="completed",
        )


def test_exact_attempt_replay_returns_existing_attempt(tmp_path: Path):
    store = SqliteNavigationTaskStore(tmp_path / "tasks.sqlite")
    arguments = {
        "request": "处理数据",
        "target": "20270623",
        "date": "20270623",
        "segments": ["segment-b", "segment-a"],
        "scene_mode": None,
        "dry_run": False,
        "web_session_id": "web-a",
        "agentscope_session_id": "as-a",
    }

    first = store.create_task_attempt(**arguments)
    replay = store.create_task_attempt(
        **{
            **arguments,
            "request": "这次重放不应覆盖原请求",
            "segments": ["segment-a", "segment-b"],
        }
    )

    assert first.created is True
    assert replay.created is False
    assert replay.task_id == first.task_id
    assert replay.task.request == "处理数据"


def test_concurrent_exact_attempt_replay_creates_one_row(tmp_path: Path):
    db_path = tmp_path / "tasks.sqlite"
    SqliteNavigationTaskStore(db_path)
    barrier = Barrier(2)

    def create_attempt(request: str):
        store = SqliteNavigationTaskStore(db_path)
        barrier.wait()
        return store.create_task_attempt(
            request=request,
            target="20270623",
            date="20270623",
            segments=["segment-a"],
            scene_mode=None,
            dry_run=False,
            web_session_id="web-a",
            agentscope_session_id="as-a",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(create_attempt, ["first", "second"]))

    assert {result.task_id for result in results} == {results[0].task_id}
    assert sorted(result.created for result in results) == [False, True]
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT count(*) FROM navigation_tasks").fetchone()[0] == 1


def test_same_session_can_create_later_attempt_for_different_target(tmp_path: Path):
    store = SqliteNavigationTaskStore(tmp_path / "tasks.sqlite")
    common = {
        "request": "处理数据",
        "date": "20270623",
        "segments": ["segment-a"],
        "scene_mode": None,
        "dry_run": False,
        "web_session_id": "web-a",
        "agentscope_session_id": "as-a",
    }

    first = store.create_task_attempt(target="20270623", **common)
    second = store.create_task_attempt(target="20260623_145550", **common)

    assert first.created is True
    assert second.created is True
    assert first.task_id != second.task_id
    found = store.find_by_session(
        web_session_id="web-a", agentscope_session_id="as-a"
    )
    assert found is not None
    assert found.task_id == second.task_id


def test_running_target_writer_returns_overlapping_mutating_plan_step(tmp_path: Path):
    db_path = tmp_path / "tasks.sqlite"
    store = SqliteNavigationTaskStore(db_path)
    attempt = store.create_task_attempt(
        request="处理数据",
        target="20270623",
        date="20270623",
        segments=["segment-a", "segment-b"],
        scene_mode=None,
        dry_run=False,
        web_session_id="web-a",
        agentscope_session_id="as-a",
    )
    plan_id, step_id = _insert_plan_ledger_step(
        db_path,
        task_id=attempt.task_id,
        ledger_action="prepare_raw_data",
    )

    writer = store.find_running_target_writer(
        date="20270623", segments=["segment-b", "segment-c"]
    )

    assert writer is not None
    assert writer.task_id == attempt.task_id
    assert writer.plan_id == plan_id
    assert writer.step_id == step_id
    assert writer.action == "prepare_raw_data"
    assert writer.date == "20270623"
    assert writer.segments == ["segment-a", "segment-b"]
    assert (
        store.find_running_target_writer(
            date="20270623", segments=["segment-c"]
        )
        is None
    )
    assert (
        store.find_running_target_writer(date="20270624", segments=None) is None
    )


@pytest.mark.parametrize(
    ("writer_segments", "requested_segments"),
    [(None, ["segment-z"]), (["segment-a"], None)],
)
def test_running_target_writer_treats_all_segments_as_overlapping(
    tmp_path: Path,
    writer_segments: list[str] | None,
    requested_segments: list[str] | None,
):
    db_path = tmp_path / "tasks.sqlite"
    store = SqliteNavigationTaskStore(db_path)
    attempt = store.create_task_attempt(
        request="处理数据",
        target="20270623",
        date="20270623",
        segments=writer_segments,
        scene_mode=None,
        dry_run=False,
        web_session_id="web-a",
        agentscope_session_id="as-a",
    )
    _insert_plan_ledger_step(
        db_path,
        task_id=attempt.task_id,
        ledger_action="extract_and_sync_navigation_data",
    )

    writer = store.find_running_target_writer(
        date="20270623", segments=requested_segments
    )

    assert writer is not None
    assert writer.task_id == attempt.task_id


@pytest.mark.parametrize(
    ("dry_run", "status", "ledger_action", "plan_action"),
    [
        (True, "running", "prepare_raw_data", None),
        (False, "pending", "prepare_raw_data", None),
        (False, "running", "validate_navigation_outputs", None),
        (False, "running", "prepare_raw_data", "validate_navigation_outputs"),
    ],
)
def test_running_target_writer_ignores_non_writers(
    tmp_path: Path,
    dry_run: bool,
    status: str,
    ledger_action: str,
    plan_action: str | None,
):
    db_path = tmp_path / "tasks.sqlite"
    store = SqliteNavigationTaskStore(db_path)
    attempt = store.create_task_attempt(
        request="处理数据",
        target="20270623",
        date="20270623",
        segments=None,
        scene_mode=None,
        dry_run=dry_run,
        web_session_id="web-a",
        agentscope_session_id="as-a",
    )
    _insert_plan_ledger_step(
        db_path,
        task_id=attempt.task_id,
        ledger_action=ledger_action,
        plan_action=plan_action,
        status=status,
    )

    assert (
        store.find_running_target_writer(
            date="20270623", segments=["segment-a"]
        )
        is None
    )


def test_owned_update_rejects_identity_field_changes_without_mutation(tmp_path: Path):
    store = SqliteNavigationTaskStore(tmp_path / "navigation_tasks.sqlite")
    owned = store.create_task_attempt(
        request="Process navigation data",
        target="20270623",
        date="20270623",
        segments=None,
        scene_mode=None,
        dry_run=False,
        web_session_id="web-owner",
        agentscope_session_id="as-owner",
    ).task

    with pytest.raises(ValueError, match="identity fields"):
        store.update_task_for_session(
            owned.task_id,
            web_session_id="web-owner",
            agentscope_session_id="as-owner",
            created_by_web_session_id="web-foreign",
        )

    assert store.get_task(owned.task_id) == owned


def test_new_task_cleanup_delete_is_state_revision_cas(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(task_store_module, "utc_now", lambda: "2026-07-10T00:00:00+00:00")
    store = SqliteNavigationTaskStore(tmp_path / "tasks.sqlite")
    created = store.create_task_attempt(
        request="Process navigation data", target="20270623",
        date="20270623", segments=None, scene_mode=None, dry_run=False,
        web_session_id="web-owner", agentscope_session_id="as-owner",
    ).task
    updated = store.update_task_for_session(
        created.task_id,
        web_session_id="web-owner",
        agentscope_session_id="as-owner",
        guidance_revision=1,
    )

    deleted = store.delete_task_if_current(
        created.task_id, expected_state_revision=created.state_revision,
        expected_web_session_id="web-owner", expected_agentscope_session_id="as-owner",
    )

    assert deleted is False
    assert store.get_task(created.task_id) == updated


def test_incompatible_navigation_schema_requires_reset_without_mutation(
    tmp_path: Path,
):
    db_path = tmp_path / "legacy-navigation.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """CREATE TABLE navigation_tasks (
                   task_id TEXT PRIMARY KEY,
                   date TEXT NOT NULL,
                   segments_json TEXT,
                   scene_mode TEXT,
                   phase TEXT NOT NULL,
                   status TEXT NOT NULL,
                   waiting_reason TEXT,
                   next_required_input TEXT,
                   created_by_web_session_id TEXT,
                   latest_web_session_id TEXT,
                   agentscope_session_id TEXT,
                   latest_run_id TEXT,
                   last_completed_step TEXT,
                   data_profile_json TEXT,
                   artifact_snapshot_json TEXT,
                   drift_json TEXT,
                   schema_version INTEGER NOT NULL,
                   created_at TEXT NOT NULL,
                   updated_at TEXT NOT NULL
               );
               INSERT INTO navigation_tasks VALUES (
                   'legacy', '20270623', NULL, NULL, 'intake', 'pending',
                   NULL, NULL, 'web-old', 'web-old', 'as-old', NULL, NULL,
                   '{"legacy":true}', NULL, NULL, 1,
                   '2026-07-08T00:00:00.000+00:00',
                   '2026-07-08T00:00:00.000+00:00'
               );"""
        )
        before_schema = connection.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
        before_rows = connection.execute(
            "SELECT * FROM navigation_tasks ORDER BY task_id"
        ).fetchall()
    before_bytes = db_path.read_bytes()

    caught: RuntimeError | None = None
    try:
        SqliteNavigationTaskStore(db_path)
    except RuntimeError as error:
        caught = error

    after_bytes = db_path.read_bytes()
    with sqlite3.connect(db_path) as connection:
        after_schema = connection.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
        after_rows = connection.execute(
            "SELECT * FROM navigation_tasks ORDER BY task_id"
        ).fetchall()

    assert after_bytes == before_bytes
    assert after_schema == before_schema
    assert after_rows == before_rows
    assert caught is not None
    assert caught.__class__.__name__ == "NavigationStateResetRequired"
    assert str(db_path) in str(caught)
    assert "back up" in str(caught).lower()
    assert "fresh" in str(caught).lower()
    assert len(str(caught)) <= 1000


def test_task1_transitional_generation_requires_reset_without_mutation(
    tmp_path: Path,
):
    db_path = tmp_path / "task1-transitional-navigation.sqlite"
    SqliteNavigationTaskStore(db_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS navigation_state_schema (
                   singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                   generation TEXT NOT NULL
               )"""
        )
        connection.execute(
            """INSERT OR REPLACE INTO navigation_state_schema
               (singleton, generation) VALUES (1, ?)""",
            ("navigation-attempts-transitional-v1",),
        )
        before_schema = connection.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
    before_bytes = db_path.read_bytes()

    caught: RuntimeError | None = None
    try:
        SqliteNavigationTaskStore(db_path)
    except RuntimeError as error:
        caught = error

    after_bytes = db_path.read_bytes()
    with sqlite3.connect(db_path) as connection:
        after_schema = connection.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()

    assert after_bytes == before_bytes
    assert after_schema == before_schema
    assert caught is not None
    assert caught.__class__.__name__ == "NavigationStateResetRequired"


def test_supported_generation_with_wrong_partial_index_requires_reset(
    tmp_path: Path,
):
    db_path = tmp_path / "wrong-partial-index.sqlite"
    SqliteNavigationTaskStore(db_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute("DROP INDEX idx_navigation_task_steps_plan_sequence")
        connection.execute(
            """CREATE UNIQUE INDEX idx_navigation_task_steps_plan_sequence
               ON navigation_task_steps (plan_id, sequence)
               WHERE plan_id IS NULL"""
        )
        before_schema = connection.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
    before_bytes = db_path.read_bytes()

    caught: RuntimeError | None = None
    try:
        SqliteNavigationTaskStore(db_path)
    except RuntimeError as error:
        caught = error

    assert db_path.read_bytes() == before_bytes
    with sqlite3.connect(db_path) as connection:
        after_schema = connection.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
    assert after_schema == before_schema
    assert caught is not None
    assert caught.__class__.__name__ == "NavigationStateResetRequired"


def test_supported_generation_with_altered_column_definition_requires_reset(
    tmp_path: Path,
):
    db_path = tmp_path / "nullable-target.sqlite"
    _create_supported_schema_variant(
        tmp_path,
        db_path,
        task_sql_transform=lambda sql: sql.replace(
            "target TEXT NOT NULL",
            "target TEXT",
        ),
    )
    with sqlite3.connect(db_path) as connection:
        target = next(
            row
            for row in connection.execute("PRAGMA table_xinfo(navigation_tasks)")
            if row[1] == "target"
        )
        before_schema = connection.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
    assert target[3] == 0
    before_bytes = db_path.read_bytes()

    with pytest.raises(NavigationStateResetRequired):
        SqliteNavigationTaskStore(db_path)

    assert db_path.read_bytes() == before_bytes
    with sqlite3.connect(db_path) as connection:
        after_schema = connection.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
    assert after_schema == before_schema


def test_supported_generation_rejects_unexpected_unique_autoindex_without_mutation(
    tmp_path: Path,
):
    db_path = tmp_path / "global-target-owner.sqlite"

    def add_global_target_owner(sql: str) -> str:
        prefix, closing = sql.rsplit(")", 1)
        return f"{prefix}, UNIQUE (date, segments_key)){closing}"

    _create_supported_schema_variant(
        tmp_path,
        db_path,
        task_sql_transform=add_global_target_owner,
    )
    with sqlite3.connect(db_path) as connection:
        indexes = connection.execute("PRAGMA index_list(navigation_tasks)").fetchall()
        before_schema = connection.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
    assert any(row[3] == "u" for row in indexes)
    before_bytes = db_path.read_bytes()

    with pytest.raises(NavigationStateResetRequired):
        SqliteNavigationTaskStore(db_path)

    assert db_path.read_bytes() == before_bytes
    with sqlite3.connect(db_path) as connection:
        after_schema = connection.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
    assert after_schema == before_schema


def test_corrupt_navigation_database_requires_reset_without_mutation(tmp_path: Path):
    db_path = tmp_path / "corrupt-navigation.sqlite"
    original = b"this is not a sqlite database\x00\xff"
    db_path.write_bytes(original)

    with pytest.raises(NavigationStateResetRequired) as caught:
        SqliteNavigationTaskStore(db_path)

    assert db_path.read_bytes() == original
    assert isinstance(caught.value.__cause__, sqlite3.DatabaseError)
    assert str(db_path) in str(caught.value)
    assert "back up" in str(caught.value).lower()
    assert "fresh" in str(caught.value).lower()
    assert len(str(caught.value)) <= 1000


def test_task_store_round_trips_attempt_fields_and_finds_exact_session(
    tmp_path: Path,
):
    store = SqliteNavigationTaskStore(tmp_path / "navigation_tasks.sqlite")

    task = store.create_task_attempt(
        request="Process navigation data",
        target="20270623",
        date="20270623",
        segments=["segment_a"],
        scene_mode=None,
        dry_run=True,
        web_session_id="web-1",
        agentscope_session_id="agent-1",
    ).task
    updated = store.update_task_for_session(
        task.task_id,
        web_session_id="web-1",
        agentscope_session_id="agent-1",
        guidance_revision=3,
        status=NavigationTaskStatus.NEEDS_REPLAN,
    )

    loaded = store.get_task(task.task_id)
    by_session = store.find_by_session(
        web_session_id="web-1", agentscope_session_id="agent-1"
    )

    assert loaded is not None
    assert loaded.dry_run is True
    assert loaded.guidance_revision == 3
    assert loaded.status == NavigationTaskStatus.NEEDS_REPLAN
    assert by_session is not None
    assert by_session.task_id == updated.task_id

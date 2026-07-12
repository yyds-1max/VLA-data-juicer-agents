import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from pathlib import Path

import pytest

import vla_data_juicer_agents.navigation.task_store as task_store_module
from vla_data_juicer_agents.navigation.task_state import NavigationTaskPhase, NavigationTaskStatus
from vla_data_juicer_agents.navigation.task_store import SqliteNavigationTaskStore


def test_task_store_idempotently_migrates_plan_ledger_columns(tmp_path: Path):
    db_path = tmp_path / "navigation_tasks.sqlite"

    SqliteNavigationTaskStore(db_path)
    SqliteNavigationTaskStore(db_path)

    with sqlite3.connect(db_path) as connection:
        columns = {
            row[1]: row for row in connection.execute(
                "PRAGMA table_info(navigation_task_steps)"
            ).fetchall()
        }
    assert {
        "plan_id",
        "plan_revision",
        "sequence",
        "result_summary_json",
        "result_ref",
        "retry_count",
    } <= columns.keys()
    assert columns["retry_count"][3] == 1
    assert columns["retry_count"][4] == "0"


def test_task_store_creates_and_loads_navigation_task(tmp_path: Path):
    store = SqliteNavigationTaskStore(tmp_path / "navigation_tasks.sqlite")

    task = store.create_or_update_task(
        date="20270623",
        segments=["20260623_101010"],
        scene_mode=None,
        web_session_id="web-1",
        agentscope_session_id="agent-1",
    )

    loaded = store.get_task(task.task_id)

    assert loaded is not None
    assert loaded.task_id == task.task_id
    assert loaded.date == "20270623"
    assert loaded.segments == ["20260623_101010"]
    assert loaded.scene_mode is None
    assert loaded.phase == NavigationTaskPhase.INTAKE
    assert loaded.status == NavigationTaskStatus.PENDING
    assert loaded.created_by_web_session_id == "web-1"
    assert loaded.latest_web_session_id == "web-1"
    assert loaded.agentscope_session_id == "agent-1"
    assert loaded.schema_version == 1


def test_task_store_updates_existing_date_and_segments(tmp_path: Path):
    store = SqliteNavigationTaskStore(tmp_path / "navigation_tasks.sqlite")
    first = store.create_or_update_task(date="20270623", segments=None, scene_mode=None)

    second = store.create_or_update_task(
        date="20270623",
        segments=None,
        scene_mode="out",
        web_session_id="web-1",
    )

    assert second.task_id == first.task_id
    assert second.scene_mode == "out"
    assert second.latest_web_session_id == "web-1"
    assert store.find_latest_by_date("20270623").task_id == first.task_id


def test_task_claim_rejects_cross_web_owner_without_any_mutation(tmp_path: Path):
    store = SqliteNavigationTaskStore(tmp_path / "navigation_tasks.sqlite")
    owned = store.create_or_update_task(
        date="20270623",
        segments=None,
        scene_mode=None,
        dry_run=False,
        web_session_id="web-owner",
        agentscope_session_id="as-owner",
    )

    with pytest.raises(task_store_module.NavigationTaskOwnershipError):
        store.create_or_update_task(
            date="20270623",
            segments=None,
            scene_mode="out",
            dry_run=True,
            web_session_id="web-foreign",
            agentscope_session_id="as-foreign",
        )

    assert store.get_task(owned.task_id) == owned


def test_concurrent_task_claim_has_one_creator_and_never_rebinds_loser(tmp_path: Path):
    db_path = tmp_path / "navigation_tasks.sqlite"
    SqliteNavigationTaskStore(db_path)
    barrier = Barrier(2)

    def claim(web_session_id: str):
        store = SqliteNavigationTaskStore(db_path)
        barrier.wait()
        try:
            task = store.create_or_update_task(
                date="20270623",
                segments=["segment-a"],
                scene_mode=None,
                web_session_id=web_session_id,
                agentscope_session_id=f"as-{web_session_id}",
            )
            return ("won", task.created_by_web_session_id)
        except task_store_module.NavigationTaskOwnershipError:
            return ("lost", web_session_id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(claim, ["web-a", "web-b"]))

    assert sorted(status for status, _ in results) == ["lost", "won"]
    stored = SqliteNavigationTaskStore(db_path).find_latest_by_date(
        "20270623", ["segment-a"]
    )
    winner = next(owner for status, owner in results if status == "won")
    assert stored.created_by_web_session_id == winner
    assert stored.latest_web_session_id == winner
    assert stored.agentscope_session_id == f"as-{winner}"


def test_owned_update_rejects_identity_field_changes_without_mutation(tmp_path: Path):
    store = SqliteNavigationTaskStore(tmp_path / "navigation_tasks.sqlite")
    owned = store.create_or_update_task(
        date="20270623",
        segments=None,
        scene_mode=None,
        web_session_id="web-owner",
        agentscope_session_id="as-owner",
    )

    with pytest.raises(ValueError, match="identity fields"):
        store.update_task_for_session(
            owned.task_id,
            web_session_id="web-owner",
            agentscope_session_id="as-owner",
            created_by_web_session_id="web-foreign",
        )

    assert store.get_task(owned.task_id) == owned


def test_same_timestamp_state_revision_prevents_restore_aba(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(task_store_module, "utc_now", lambda: "2026-07-10T00:00:00+00:00")
    store = SqliteNavigationTaskStore(tmp_path / "tasks.sqlite")
    original = store.create_or_update_task(
        date="20270623", segments=None, scene_mode=None,
        web_session_id="web-owner", agentscope_session_id="as-old",
    )
    claimed = store.update_task_for_session(
        original.task_id, web_session_id="web-owner",
        agentscope_session_id="as-old", scene_mode="in",
    )
    concurrent = store.create_or_update_task(
        date=original.date, segments=original.segments, scene_mode="out",
        web_session_id="web-owner", agentscope_session_id="as-new",
    )

    restored = store.restore_task_exact_if_current(
        original, expected_state_revision=claimed.state_revision,
        expected_web_session_id="web-owner", expected_agentscope_session_id="as-old",
    )

    assert restored is False
    current = store.get_task(original.task_id)
    assert current.scene_mode == "out"
    assert current.agentscope_session_id == "as-new"
    assert current.state_revision > claimed.state_revision


def test_new_task_cleanup_delete_is_state_revision_cas(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(task_store_module, "utc_now", lambda: "2026-07-10T00:00:00+00:00")
    store = SqliteNavigationTaskStore(tmp_path / "tasks.sqlite")
    created = store.create_or_update_task(
        date="20270623", segments=None, scene_mode=None,
        web_session_id="web-owner", agentscope_session_id="as-old",
    )
    updated = store.create_or_update_task(
        date=created.date, segments=created.segments, scene_mode="out",
        web_session_id="web-owner", agentscope_session_id="as-new",
    )

    deleted = store.delete_task_if_current(
        created.task_id, expected_state_revision=created.state_revision,
        expected_web_session_id="web-owner", expected_agentscope_session_id="as-old",
    )

    assert deleted is False
    assert store.get_task(created.task_id) == updated


def test_task_store_exact_restore_preserves_entire_persisted_model(
    monkeypatch,
    tmp_path: Path,
):
    tick = 0

    def advancing_utc_now() -> str:
        nonlocal tick
        tick += 1
        return f"2026-07-10T00:00:00.{tick:03d}+00:00"

    monkeypatch.setattr(task_store_module, "utc_now", advancing_utc_now)
    store = SqliteNavigationTaskStore(tmp_path / "navigation_tasks.sqlite")
    original = store.create_or_update_task(
        date="20270623",
        segments=["segment_a"],
        scene_mode=None,
        web_session_id="web-old",
        agentscope_session_id="as-old",
    )
    original = store.update_task_for_session(
        original.task_id,
        web_session_id="web-old",
        agentscope_session_id="as-old",
        guidance_revision=4,
        status=NavigationTaskStatus.NEEDS_RECONCILE,
    )
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            """UPDATE navigation_tasks
               SET guidance_revision = 5, phase = 'extract_sync', status = 'running',
                   latest_web_session_id = 'web-new', agentscope_session_id = 'as-new',
                   state_revision = state_revision + 1
               WHERE task_id = ?""",
            (original.task_id,),
        )

    restored = store.restore_task_exact(original)

    assert restored.model_copy(update={"state_revision": original.state_revision}) == original
    assert restored.state_revision > original.state_revision
    assert store.get_task(original.task_id) == restored


def test_task_store_update_task_ignores_caller_timestamps(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(
        task_store_module,
        "utc_now",
        lambda: "2026-07-10T00:00:01.000+00:00",
    )
    store = SqliteNavigationTaskStore(tmp_path / "navigation_tasks.sqlite")
    task = store.create_or_update_task(
        date="20270623",
        segments=["segment_a"],
        scene_mode=None,
    )

    updated = store.update_task(
        task.task_id,
        status=NavigationTaskStatus.NEEDS_RECONCILE,
        created_at="2000-01-01T00:00:00.000+00:00",
        updated_at="2000-01-01T00:00:00.000+00:00",
    )

    assert updated.created_at == task.created_at
    assert updated.updated_at == "2026-07-10T00:00:01.000+00:00"


def test_update_task_serializes_same_timestamp_read_merge_write(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(task_store_module, "utc_now", lambda: "2026-07-10T00:00:00+00:00")
    store = SqliteNavigationTaskStore(tmp_path / "navigation_tasks.sqlite")
    original = store.create_or_update_task(
        date="20270623", segments=None, scene_mode=None,
    )
    barrier = Barrier(2)
    original_get_task = store.get_task

    def synchronized_get_task(task_id: str):
        task = original_get_task(task_id)
        barrier.wait()
        return task

    monkeypatch.setattr(store, "get_task", synchronized_get_task)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(store.update_task, original.task_id, scene_mode="in"),
            executor.submit(
                store.update_task,
                original.task_id,
                guidance_revision=7,
            ),
        ]
        revisions = sorted(future.result().state_revision for future in futures)

    current = original_get_task(original.task_id)
    assert revisions == [original.state_revision + 1, original.state_revision + 2]
    assert current.state_revision == original.state_revision + 2
    assert current.scene_mode == "in"
    assert current.guidance_revision == 7


def test_task_store_keeps_one_active_task_per_date_and_segments_key(tmp_path: Path):
    store = SqliteNavigationTaskStore(tmp_path / "navigation_tasks.sqlite")

    first = store.create_or_update_task(
        date="20270623",
        segments=["segment_b", "segment_a"],
        scene_mode=None,
        web_session_id="web-1",
    )
    second = store.create_or_update_task(
        date="20270623",
        segments=["segment_a", "segment_b"],
        scene_mode="out",
        web_session_id="web-1",
    )
    different = store.create_or_update_task(
        date="20270623",
        segments=["segment_a"],
        scene_mode=None,
        web_session_id="web-3",
    )

    assert second.task_id == first.task_id
    assert second.segments == ["segment_b", "segment_a"]
    assert second.scene_mode == "out"
    assert different.task_id != first.task_id
    assert store.find_latest_by_date("20270623", ["segment_b", "segment_a"]).task_id == first.task_id
    assert store.find_latest_by_date("20270623", ["segment_a", "segment_b"]).task_id == first.task_id
    assert store.find_latest_by_date("20270623", ["segment_a"]).task_id == different.task_id


def test_task_store_migrates_legacy_duplicate_active_segments(tmp_path: Path):
    db_path = tmp_path / "navigation_tasks.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE navigation_tasks (
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
            )
            """
        )
        for task_id, updated_at in [
            ("nav_old", "2026-07-08T00:00:00.000+00:00"),
            ("nav_new", "2026-07-08T00:00:01.000+00:00"),
        ]:
            connection.execute(
                """
                INSERT INTO navigation_tasks (
                    task_id, date, segments_json, scene_mode, phase, status,
                    waiting_reason, next_required_input, created_by_web_session_id,
                    latest_web_session_id, agentscope_session_id, latest_run_id,
                    last_completed_step, data_profile_json, artifact_snapshot_json,
                    drift_json, schema_version, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    "20270623",
                    json.dumps(["segment_a"]),
                    None,
                    "intake",
                    "pending",
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    1,
                    "2026-07-08T00:00:00.000+00:00",
                    updated_at,
                ),
            )

    store = SqliteNavigationTaskStore(db_path)
    task = store.create_or_update_task(
        date="20270623",
        segments=["segment_a"],
        scene_mode="out",
        web_session_id="web-new",
    )

    with sqlite3.connect(db_path) as connection:
        active_count = connection.execute(
            """
            SELECT count(*) FROM navigation_tasks
            WHERE date = ? AND status != ?
            """,
            ("20270623", NavigationTaskStatus.SUPERSEDED.value),
        ).fetchone()[0]
        old_status = connection.execute(
            "SELECT status FROM navigation_tasks WHERE task_id = ?",
            ("nav_old",),
        ).fetchone()[0]

    assert task.task_id == "nav_new"
    assert task.scene_mode == "out"
    assert active_count == 1
    assert old_status == NavigationTaskStatus.SUPERSEDED.value


def test_task_store_migrates_json_encoded_segment_entry(tmp_path: Path):
    db_path = tmp_path / "navigation_tasks.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE navigation_tasks (
                task_id TEXT PRIMARY KEY,
                date TEXT NOT NULL,
                segments_json TEXT,
                segments_key TEXT,
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
            )
            """
        )
        connection.execute(
            """
            INSERT INTO navigation_tasks (
                task_id, date, segments_json, segments_key, scene_mode, phase, status,
                waiting_reason, next_required_input, created_by_web_session_id,
                latest_web_session_id, agentscope_session_id, latest_run_id,
                last_completed_step, data_profile_json, artifact_snapshot_json,
                drift_json, schema_version, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "nav_bad_segments",
                "20270623",
                json.dumps(['["20260623_145550"]']),
                json.dumps(['["20260623_145550"]'], separators=(",", ":")),
                None,
                "intake",
                "pending",
                None,
                None,
                "web-1",
                "web-1",
                "agent-1",
                None,
                None,
                None,
                None,
                None,
                1,
                "2026-07-08T00:00:00.000+00:00",
                "2026-07-08T00:00:00.000+00:00",
            ),
        )

    store = SqliteNavigationTaskStore(db_path)
    task = store.get_task("nav_bad_segments")

    assert task.segments == ["20260623_145550"]
    assert store.find_latest_by_date("20270623", ["20260623_145550"]).task_id == task.task_id

    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT segments_json, segments_key FROM navigation_tasks WHERE task_id = ?",
            ("nav_bad_segments",),
        ).fetchone()

    assert json.loads(row[0]) == ["20260623_145550"]
    assert row[1] == '["20260623_145550"]'


def test_task_store_migrates_json_encoded_segment_entry_with_existing_unique_index(
    tmp_path: Path,
):
    db_path = tmp_path / "navigation_tasks.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE navigation_tasks (
                task_id TEXT PRIMARY KEY,
                date TEXT NOT NULL,
                segments_json TEXT,
                segments_key TEXT,
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
            )
            """
        )
        connection.execute(
            """
            CREATE UNIQUE INDEX idx_navigation_tasks_active_date_segments_key
            ON navigation_tasks (date, segments_key)
            WHERE status != 'superseded'
            """
        )
        for task_id, segments, key, updated_at in [
            (
                "nav_good",
                ["20260623_145550"],
                '["20260623_145550"]',
                "2026-07-08T00:00:01.000+00:00",
            ),
            (
                "nav_bad",
                ['["20260623_145550"]'],
                '["[\\"20260623_145550\\"]"]',
                "2026-07-08T00:00:00.000+00:00",
            ),
        ]:
            connection.execute(
                """
                INSERT INTO navigation_tasks (
                    task_id, date, segments_json, segments_key, scene_mode, phase, status,
                    waiting_reason, next_required_input, created_by_web_session_id,
                    latest_web_session_id, agentscope_session_id, latest_run_id,
                    last_completed_step, data_profile_json, artifact_snapshot_json,
                    drift_json, schema_version, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    "20270623",
                    json.dumps(segments),
                    key,
                    None,
                    "intake",
                    "pending",
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    1,
                    "2026-07-08T00:00:00.000+00:00",
                    updated_at,
                ),
            )

    store = SqliteNavigationTaskStore(db_path)
    active = store.find_latest_by_date("20270623", ["20260623_145550"])

    assert active.task_id == "nav_good"
    assert active.segments == ["20260623_145550"]
    assert store.get_task("nav_bad").status == NavigationTaskStatus.SUPERSEDED


def test_create_or_update_task_preserves_latest_web_session_when_omitted(tmp_path: Path):
    store = SqliteNavigationTaskStore(tmp_path / "navigation_tasks.sqlite")
    first = store.create_or_update_task(
        date="20270623",
        segments=["20260623_101010"],
        scene_mode=None,
        web_session_id="web-1",
    )

    with pytest.raises(task_store_module.NavigationTaskOwnershipError):
        store.create_or_update_task(
            date="20270623",
            segments=["20260623_101010"],
            scene_mode=None,
            web_session_id=None,
        )

    assert store.get_task(first.task_id) == first
    assert store.get_task(first.task_id).latest_web_session_id == "web-1"


def test_task_store_lists_resumable_tasks(tmp_path: Path):
    store = SqliteNavigationTaskStore(tmp_path / "navigation_tasks.sqlite")
    waiting = store.create_or_update_task(date="20270623", segments=None, scene_mode=None)
    store.update_task(
        waiting.task_id,
        phase=NavigationTaskPhase.WAITING_SCENE_MODE,
        status=NavigationTaskStatus.WAITING_USER,
        next_required_input="scene_mode",
    )
    completed = store.create_or_update_task(date="20270624", segments=None, scene_mode="in")
    store.update_task(
        completed.task_id,
        phase=NavigationTaskPhase.COMPLETED,
        status=NavigationTaskStatus.COMPLETED,
    )
    needs_replan = store.create_or_update_task(date="20270625", segments=None, scene_mode="out")
    store.update_task(needs_replan.task_id, status=NavigationTaskStatus.NEEDS_REPLAN)

    resumable = store.list_resumable()

    assert [task.task_id for task in resumable] == [needs_replan.task_id, waiting.task_id]


def test_task_store_round_trips_entry_fields_and_finds_latest_agentscope_session(
    tmp_path: Path,
):
    store = SqliteNavigationTaskStore(tmp_path / "navigation_tasks.sqlite")

    task = store.create_or_update_task(
        date="20270623",
        segments=["segment_a"],
        scene_mode=None,
        dry_run=True,
        agentscope_session_id="agent-1",
    )
    updated = store.update_task_for_session(
        task.task_id,
        web_session_id=None,
        agentscope_session_id="agent-1",
        guidance_revision=3,
        status=NavigationTaskStatus.NEEDS_REPLAN,
    )

    loaded = store.get_task(task.task_id)
    by_session = store.find_latest_by_agentscope_session("agent-1")

    assert loaded is not None
    assert loaded.dry_run is True
    assert loaded.guidance_revision == 3
    assert loaded.status == NavigationTaskStatus.NEEDS_REPLAN
    assert by_session is not None
    assert by_session.task_id == updated.task_id


def test_task_store_preserves_dry_run_when_upsert_omits_it(tmp_path: Path):
    store = SqliteNavigationTaskStore(tmp_path / "navigation_tasks.sqlite")
    first = store.create_or_update_task(
        date="20270623",
        segments=["segment_a"],
        scene_mode=None,
        dry_run=True,
    )

    preserved = store.create_or_update_task(
        date="20270623",
        segments=["segment_a"],
        scene_mode=None,
    )
    explicitly_disabled = store.create_or_update_task(
        date="20270623",
        segments=["segment_a"],
        scene_mode=None,
        dry_run=False,
    )

    assert preserved.task_id == first.task_id
    assert preserved.dry_run is True
    assert explicitly_disabled.dry_run is False


def test_task_store_can_update_task_after_recording_step(tmp_path: Path):
    store = SqliteNavigationTaskStore(tmp_path / "navigation_tasks.sqlite")
    task = store.create_or_update_task(date="20270623", segments=None, scene_mode=None)
    store.record_step(
        task_id=task.task_id,
        phase=NavigationTaskPhase.EXTRACT_SYNC,
        step_id="extract_and_sync_navigation_data",
        tool_name="extract_and_sync_navigation_data",
        status=NavigationTaskStatus.COMPLETED,
    )

    updated = store.update_task(
        task.task_id,
        status=NavigationTaskStatus.RUNNING,
        latest_run_id="run-1",
    )

    assert updated.status == NavigationTaskStatus.RUNNING
    assert updated.latest_run_id == "run-1"
    assert [step.step_id for step in store.list_steps(task.task_id)] == [
        "extract_and_sync_navigation_data"
    ]


def test_task_store_update_task_can_clear_optional_fields(tmp_path: Path):
    store = SqliteNavigationTaskStore(tmp_path / "navigation_tasks.sqlite")
    task = store.create_or_update_task(date="20270623", segments=None, scene_mode=None)
    store.update_task(
        task.task_id,
        waiting_reason="awaiting scene mode",
        next_required_input="scene_mode",
    )

    cleared = store.update_task(
        task.task_id,
        waiting_reason=None,
        next_required_input=None,
    )

    assert cleared.waiting_reason is None
    assert cleared.next_required_input is None


def test_task_store_records_step_with_result_json(tmp_path: Path):
    store = SqliteNavigationTaskStore(tmp_path / "navigation_tasks.sqlite")
    task = store.create_or_update_task(date="20270623", segments=None, scene_mode=None)

    step = store.record_step(
        task_id=task.task_id,
        phase=NavigationTaskPhase.EXTRACT_SYNC,
        step_id="extract_and_sync_navigation_data",
        tool_name="extract_and_sync_navigation_data",
        status=NavigationTaskStatus.COMPLETED,
        arguments={"date": "20270623"},
        result={"ok": True},
        produced_paths=["clip_data/20270623"],
    )

    assert step.task_id == task.task_id
    assert step.result == {"ok": True}
    assert step.produced_paths == ["clip_data/20270623"]
    assert store.list_steps(task.task_id)[0].step_id == "extract_and_sync_navigation_data"
    assert store.get_task(task.task_id).state_revision == task.state_revision + 1

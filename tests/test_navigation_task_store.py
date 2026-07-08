import json
import sqlite3
from pathlib import Path

from vla_data_juicer_agents.navigation.task_state import NavigationTaskPhase, NavigationTaskStatus
from vla_data_juicer_agents.navigation.task_store import SqliteNavigationTaskStore


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
        web_session_id="web-2",
    )

    assert second.task_id == first.task_id
    assert second.scene_mode == "out"
    assert second.latest_web_session_id == "web-2"
    assert store.find_latest_by_date("20270623").task_id == first.task_id


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
        web_session_id="web-2",
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


def test_create_or_update_task_preserves_latest_web_session_when_omitted(tmp_path: Path):
    store = SqliteNavigationTaskStore(tmp_path / "navigation_tasks.sqlite")
    first = store.create_or_update_task(
        date="20270623",
        segments=["20260623_101010"],
        scene_mode=None,
        web_session_id="web-1",
    )

    second = store.create_or_update_task(
        date="20270623",
        segments=["20260623_101010"],
        scene_mode=None,
        web_session_id=None,
    )

    assert second.task_id == first.task_id
    assert second.latest_web_session_id == "web-1"
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

    resumable = store.list_resumable()

    assert [task.task_id for task in resumable] == [waiting.task_id]


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

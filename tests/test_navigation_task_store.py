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

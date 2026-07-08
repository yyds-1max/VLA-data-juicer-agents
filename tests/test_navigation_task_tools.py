import asyncio
import json
from pathlib import Path

from vla_data_juicer_agents.navigation.config import NavigationSettings
from vla_data_juicer_agents.navigation.task_state import (
    NavigationTaskPhase,
    NavigationTaskStatus,
)
from vla_data_juicer_agents.navigation.task_store import SqliteNavigationTaskStore
from vla_data_juicer_agents.navigation.task_tools import build_navigation_task_tools


def _tools(tmp_path: Path):
    root = tmp_path / "VLADatasets"
    store = SqliteNavigationTaskStore(tmp_path / "tasks.sqlite")
    settings = NavigationSettings(vladatasets_root=root)
    tools = {
        tool.name: tool
        for tool in build_navigation_task_tools(
            store=store,
            session_id="agent-session",
            web_session_id="web-session",
            settings=settings,
        )
    }
    return root, store, tools


def _decode_tool_payload(payload):
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, str):
        return json.loads(payload)
    if hasattr(payload, "content"):
        return _decode_tool_payload(payload.content)
    if hasattr(payload, "model_dump"):
        return payload.model_dump(mode="json")
    if isinstance(payload, (list, tuple)):
        texts = [
            block.text
            for block in payload
            if hasattr(block, "text") and isinstance(block.text, str)
        ]
        if texts:
            return _decode_tool_payload("".join(texts))
    return payload


def _call(tool, **kwargs):
    return _decode_tool_payload(asyncio.run(tool(**kwargs)))


def test_get_or_create_navigation_task_tool_creates_date_only_task(tmp_path: Path):
    _root, store, tools = _tools(tmp_path)

    result = _call(
        tools["get_or_create_navigation_task_tool"],
        date="20270623",
        segments=None,
        scene_mode=None,
    )

    assert result["ok"] is True
    assert result["task"]["date"] == "20270623"
    assert result["task"]["scene_mode"] is None
    assert store.get_task(result["task"]["task_id"]) is not None


def test_reconcile_navigation_task_tool_updates_missing_sync_to_needs_rerun(
    tmp_path: Path,
):
    root, _store, tools = _tools(tmp_path)
    (root / "raw_data" / "20270623" / "segment_a").mkdir(parents=True)
    created = _call(
        tools["get_or_create_navigation_task_tool"],
        date="20270623",
        segments=["segment_a"],
        scene_mode=None,
    )
    task_id = created["task"]["task_id"]
    _call(
        tools["update_navigation_task_state_tool"],
        task_id=task_id,
        phase="waiting_scene_mode",
        status="waiting_user",
        waiting_reason="scene_mode_required_after_extract_sync",
        next_required_input="scene_mode",
    )

    result = _call(tools["reconcile_navigation_task_tool"], task_id=task_id)

    assert result["ok"] is True
    assert result["task"]["phase"] == "extract_sync"
    assert result["task"]["status"] == "needs_rerun"
    assert result["task"]["drift"]["type"] == "missing_expected_artifact"


def test_update_navigation_task_scene_mode_tool_sets_finish_processing(
    tmp_path: Path,
):
    _root, _store, tools = _tools(tmp_path)
    created = _call(
        tools["get_or_create_navigation_task_tool"],
        date="20270623",
        segments=None,
        scene_mode=None,
    )

    result = _call(
        tools["update_navigation_task_scene_mode_tool"],
        task_id=created["task"]["task_id"],
        scene_mode="in",
    )

    assert result["ok"] is True
    assert result["task"]["scene_mode"] == "in"
    assert result["task"]["phase"] == NavigationTaskPhase.FINISH_PROCESSING.value
    assert result["task"]["status"] == NavigationTaskStatus.PENDING.value


def test_resumable_task_can_be_claimed_from_new_web_session(tmp_path: Path):
    root, store, first_tools = _tools(tmp_path)
    created = _call(
        first_tools["get_or_create_navigation_task_tool"],
        date="20270623",
        segments=None,
        scene_mode=None,
    )
    _call(
        first_tools["update_navigation_task_state_tool"],
        task_id=created["task"]["task_id"],
        phase="waiting_scene_mode",
        status="waiting_user",
        waiting_reason="scene_mode_required_after_extract_sync",
        next_required_input="scene_mode",
    )
    second_tools = {
        tool.name: tool
        for tool in build_navigation_task_tools(
            store=store,
            session_id="agent-session-2",
            web_session_id="web-session-2",
            settings=NavigationSettings(vladatasets_root=root),
        )
    }

    listed = _call(second_tools["list_resumable_navigation_tasks_tool"], date="20270623")
    updated = _call(
        second_tools["update_navigation_task_scene_mode_tool"],
        task_id=listed["tasks"][0]["task_id"],
        scene_mode="out",
    )

    assert updated["task"]["latest_web_session_id"] == "web-session-2"
    assert updated["task"]["agentscope_session_id"] == "agent-session-2"
    assert updated["task"]["scene_mode"] == "out"

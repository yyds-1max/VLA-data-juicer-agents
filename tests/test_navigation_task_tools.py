import asyncio
import json
from pathlib import Path

from vla_data_juicer_agents.navigation.config import NavigationSettings
from vla_data_juicer_agents.navigation.plan_draft import WorkflowPlanDraftState
from vla_data_juicer_agents.navigation.plan_draft_store import InMemoryNavigationPlanDraftStore
from vla_data_juicer_agents.navigation.models import NavigationRequest, WorkflowPlan, WorkflowStep
from vla_data_juicer_agents.navigation.session_plan_draft_tools import build_session_plan_draft_tools
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


def _tools_with_draft_store(tmp_path: Path, *, session_id: str):
    root = tmp_path / "VLADatasets"
    store = SqliteNavigationTaskStore(tmp_path / "tasks.sqlite")
    draft_store = InMemoryNavigationPlanDraftStore()
    settings = NavigationSettings(vladatasets_root=root)
    tools = {
        tool.name: tool
        for tool in build_navigation_task_tools(
            store=store,
            session_id=session_id,
            web_session_id=f"web-{session_id}",
            settings=settings,
            draft_store=draft_store,
        )
    }
    return root, store, draft_store, tools


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
    (root / "raw_data" / "20270623" / "segment_a").mkdir(parents=True)
    (root / "clip_data" / "20270623" / "segment_a" / "sync_data" / "clip_0").mkdir(
        parents=True
    )
    created = _call(
        first_tools["get_or_create_navigation_task_tool"],
        date="20270623",
        segments=["segment_a"],
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


def test_scene_mode_claim_syncs_new_session_draft_to_finish_processing(tmp_path: Path):
    root, store, draft_store, first_tools = _tools_with_draft_store(
        tmp_path,
        session_id="agent-session-a",
    )
    (root / "raw_data" / "20270623" / "segment_a").mkdir(parents=True)
    (root / "clip_data" / "20270623" / "segment_a" / "sync_data" / "clip_0").mkdir(
        parents=True
    )
    created = _call(
        first_tools["get_or_create_navigation_task_tool"],
        date="20270623",
        segments=["segment_a"],
        scene_mode=None,
    )
    store.update_task(
        created["task"]["task_id"],
        phase=NavigationTaskPhase.WAITING_SCENE_MODE,
        status=NavigationTaskStatus.WAITING_USER,
        waiting_reason="scene_mode_required_after_extract_sync",
        next_required_input="scene_mode",
    )
    draft_store.save(
        "agent-session-b",
        WorkflowPlanDraftState(
            request=NavigationRequest(date="20270623", segments=["segment_a"]),
            finalized_plan=WorkflowPlan(
                date="20270623",
                phase="extract_sync",
                scene_mode=None,
                steps=[
                    WorkflowStep(
                        step_id="extract_and_sync_navigation_data",
                        tool_name="extract_and_sync_navigation_data",
                    )
                ],
            ),
        ),
    )
    second_tools = {
        tool.name: tool
        for tool in build_navigation_task_tools(
            store=store,
            session_id="agent-session-b",
            web_session_id="web-session-b",
            settings=NavigationSettings(vladatasets_root=root),
            draft_store=draft_store,
        )
    }

    result = _call(
        second_tools["update_navigation_task_scene_mode_tool"],
        task_id=created["task"]["task_id"],
        scene_mode="out",
    )
    draft = draft_store.load("agent-session-b")

    assert result["ok"] is True
    assert result["task"]["phase"] == NavigationTaskPhase.FINISH_PROCESSING.value
    assert draft is not None
    assert draft.request.scene_mode == "out"
    assert draft.plan_phase == "finish_processing"
    assert draft.finalized_plan is None
    assert draft.next_required_observation()["observation_id"] == "processing_state"

    plan_tools = {
        tool.name: tool
        for tool in build_session_plan_draft_tools(
            store=draft_store,
            session_id="agent-session-b",
        )
    }
    _call(
        plan_tools["update_workflow_plan_draft_tool"],
        data_profile_patch={
            "processing_profile": {
                "id": "parameterized_navigation_v1",
                "platform_hint": "go2w",
                "topic_params": {
                    "profile_hint": "go2w",
                    "confidence": 1.0,
                    "topic_whitelist": ["/cam"],
                    "topic_map": {"cam": "fisheye_front"},
                    "query_dir": "cam",
                    "evidence": [],
                    "warnings": [],
                    "blocking_issues": [],
                },
                "localization_policy": {"source": "odom", "conversion": "odom_to_ins"},
                "gridmap_policy": {"source": "existing_gridmap"},
                "calibration_policy": {
                    "mode": "hardcoded_with_user_confirmation",
                    "requires_user_confirmation": True,
                },
                "warnings": [],
                "blocking_issues": [],
                "evidence": {},
            },
            "platform_hint": "go2w",
            "topic_params": {
                "profile_hint": "go2w",
                "confidence": 1.0,
                "topic_whitelist": ["/cam"],
                "topic_map": {"cam": "fisheye_front"},
                "query_dir": "cam",
                "evidence": [],
                "warnings": [],
                "blocking_issues": [],
            },
            "localization_policy": {"source": "odom", "conversion": "odom_to_ins"},
            "gridmap_source": "existing_gridmap",
            "stage_variants": {
                "extract_and_sync_navigation_data": {"variant": "explicit_topic_params"},
                "prepare_gridmap_for_projection": {"variant": "copy_existing_gridmap"},
                "run_projection_and_trajectory": {"variant": "cjl_with_gridmap"},
            },
        },
        observation_id="processing_state",
        used_tool="inspect_processing_state_tool",
    )
    finalized = _call(plan_tools["finalize_finish_processing_plan_tool"])

    assert finalized["ok"] is True
    assert finalized["workflow_plan_json"]["phase"] == "finish_processing"

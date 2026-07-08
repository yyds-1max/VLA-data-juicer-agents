import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from vla_data_juicer_agents.navigation.models import NavigationRequest
from vla_data_juicer_agents.navigation.plan_draft import WorkflowPlanDraftState
from vla_data_juicer_agents.navigation.plan_draft_store import (
    InMemoryNavigationPlanDraftStore,
    JsonNavigationPlanDraftStore,
)


def test_in_memory_store_keeps_sessions_isolated():
    store = InMemoryNavigationPlanDraftStore()
    first = WorkflowPlanDraftState(
        request=NavigationRequest(date="20270605", scene_mode="out")
    )
    second = WorkflowPlanDraftState(
        request=NavigationRequest(date="20270606", scene_mode="in")
    )

    store.save("session-a", first)
    store.save("session-b", second)

    assert store.load("session-a").date == "20270605"
    assert store.load("session-b").date == "20270606"


def test_json_store_survives_new_store_instance(tmp_path: Path):
    store = JsonNavigationPlanDraftStore(tmp_path)
    state = WorkflowPlanDraftState(
        request=NavigationRequest(date="20270605", scene_mode="out")
    )
    state.update(
        data_profile_patch={"gridmap_source": "existing_gridmap"},
        observation_id="gridmap_artifacts",
        used_tool="inspect_gridmap_artifacts_tool",
    )

    store.save("agent/session:1", state)

    restored = JsonNavigationPlanDraftStore(tmp_path).load("agent/session:1")

    assert restored is not None
    assert restored.date == "20270605"
    assert restored.scene_mode == "out"
    assert restored.data_profile_draft["gridmap_source"] == "existing_gridmap"
    assert restored.completed_observations == [
        {
            "observation_id": "gridmap_artifacts",
            "used_tool": "inspect_gridmap_artifacts_tool",
        }
    ]


def test_json_store_clear_removes_only_one_session(tmp_path: Path):
    store = JsonNavigationPlanDraftStore(tmp_path)
    first = WorkflowPlanDraftState(
        request=NavigationRequest(date="20270605", scene_mode="out")
    )
    second = WorkflowPlanDraftState(
        request=NavigationRequest(date="20270606", scene_mode="in")
    )

    store.save("session-a", first)
    store.save("session-b", second)
    store.clear("session-a")

    assert store.load("session-a") is None
    assert store.load("session-b").date == "20270606"


def test_json_store_rejects_stale_unknown_fields(tmp_path: Path):
    session_id = "session-with-stale-json"
    store = JsonNavigationPlanDraftStore(tmp_path)
    state = WorkflowPlanDraftState(
        request=NavigationRequest(date="20270605", scene_mode="out")
    )
    store.save(session_id, state)

    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    path = tmp_path / f"{digest}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["removed_field"] = "stale"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValidationError):
        JsonNavigationPlanDraftStore(tmp_path).load(session_id)

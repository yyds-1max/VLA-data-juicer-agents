import asyncio
import json
from pathlib import Path

from vla_data_juicer_agents.navigation.services import build_navigation_services
from vla_data_juicer_agents.navigation.task_tools import build_navigation_task_tools


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


def _bound_tools(tmp_path: Path):
    services = build_navigation_services(tmp_path)
    task = services.task_store.create_task_attempt(
        request="process navigation data",
        target="20270623/segment_a",
        date="20270623",
        segments=["segment_a"],
        scene_mode=None,
        dry_run=False,
        web_session_id="web-session",
        agentscope_session_id="agent-session",
    ).task
    tools = {
        tool.name: tool
        for tool in build_navigation_task_tools(
            store=services.task_store,
            observation_store=services.observation_store,
            evidence_store=services.evidence_store,
            session_id="agent-session",
            web_session_id="web-session",
            bound_task=task,
        )
    }
    return services, task, tools


def test_unbound_task_exposes_no_model_facing_lifecycle_tools(tmp_path: Path):
    services = build_navigation_services(tmp_path)

    tools = build_navigation_task_tools(
        store=services.task_store,
        observation_store=services.observation_store,
        evidence_store=services.evidence_store,
        session_id="agent-session",
        web_session_id="web-session",
    )

    assert tools == []


def test_bound_task_tool_schema_contains_only_guidance_and_optional_scene_mode(
    tmp_path: Path,
):
    _services, _task, tools = _bound_tools(tmp_path)

    assert set(tools) == {"record_navigation_user_guidance_tool"}
    schema = tools["record_navigation_user_guidance_tool"].input_schema
    assert set(schema["properties"]) == {"text", "scene_mode"}
    assert schema["required"] == ["text"]
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]["scene_mode"]["anyOf"][0]["enum"]) == {
        "in",
        "out",
    }


def test_bound_task_tool_records_guidance_without_lifecycle_or_phase_selection(
    tmp_path: Path,
):
    services, task, tools = _bound_tools(tmp_path)

    result = _call(
        tools["record_navigation_user_guidance_tool"],
        text="Continue with indoor processing after checking current artifacts.",
        scene_mode="in",
    )

    assert result == {
        "ok": True,
        "guidance_revision": 1,
        "observation_revision": 1,
    }
    stored = services.task_store.get_task(task.task_id)
    observation = services.observation_store.latest(task.task_id)
    assert stored is not None and observation is not None
    assert stored.scene_mode == "in"
    assert stored.guidance_revision == 1
    assert stored.phase.value == "intake"
    assert stored.accepted_plan_phase is None
    assert observation.completed_kinds == ["user_guidance"]


def test_guidance_rejects_stale_session_without_mutation(tmp_path: Path):
    services, task, _tools = _bound_tools(tmp_path)
    stale = {
        tool.name: tool
        for tool in build_navigation_task_tools(
            store=services.task_store,
            observation_store=services.observation_store,
            evidence_store=services.evidence_store,
            session_id="stale-agent-session",
            web_session_id="web-session",
            bound_task=task,
        )
    }
    before = services.task_store.get_task(task.task_id)

    result = _call(
        stale["record_navigation_user_guidance_tool"],
        text="stale guidance",
    )

    assert result["ok"] is False
    assert result["error_type"] == "navigation_task_session_mismatch"
    assert services.task_store.get_task(task.task_id) == before
    assert services.observation_store.latest(task.task_id) is None


def test_guidance_observation_failure_restores_logical_task_state(
    tmp_path: Path,
    monkeypatch,
):
    services, task, tools = _bound_tools(tmp_path)
    before = services.task_store.get_task(task.task_id)
    monkeypatch.setattr(
        services.evidence_store,
        "write",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("unavailable")),
    )
    monkeypatch.setattr(
        services.task_store,
        "restore_task_exact_if_current",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("atomic guidance must not require compensation")
        ),
    )

    result = _call(
        tools["record_navigation_user_guidance_tool"],
        text="do not partially persist this",
        scene_mode="out",
    )

    assert result["ok"] is False
    assert result["error_type"] == "navigation_guidance_persistence_failed"
    after = services.task_store.get_task(task.task_id)
    assert before is not None and after is not None
    assert after.guidance_revision == before.guidance_revision
    assert after.scene_mode == before.scene_mode
    assert after.phase == before.phase
    assert after.accepted_plan_phase == before.accepted_plan_phase
    assert services.observation_store.latest(task.task_id) is None

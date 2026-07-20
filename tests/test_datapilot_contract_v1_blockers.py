from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from vla_data_juicer_agents.navigation.task_state import NavigationTaskStatus
from vla_data_juicer_agents.navigation.task_store import SqliteNavigationTaskStore
from vla_data_juicer_agents.runtime.agentscope_config import AgentScopeRuntimeConfig
from vla_data_juicer_agents.runtime.agentscope_runtime import AgentScopeRuntime
from vla_data_juicer_agents.web.agent_session import AgentScopeWebSessionManager
from vla_data_juicer_agents.web.app import create_app
from vla_data_juicer_agents.web.session_store import WebSessionStore


def _config(tmp_path: Path) -> AgentScopeRuntimeConfig:
    return AgentScopeRuntimeConfig(
        user_id="test-user",
        redis_url="redis://localhost:6379/0",
        workspace_root=tmp_path,
        dashscope_api_key="test-key",
        dashscope_base_url=None,
        default_model="test-model",
        router_model="router-model",
        navigation_model="navigation-model",
        datapilot_single_agent_mode="new_sessions",
    )


def _runtime(
    tmp_path: Path,
    *,
    store: WebSessionStore,
    task_store: SqliteNavigationTaskStore | None = None,
) -> AgentScopeRuntime:
    runtime = AgentScopeRuntime(
        config=_config(tmp_path),
        storage=None,
        message_bus=SimpleNamespace(),
        workspace_manager=None,
        app=SimpleNamespace(state=SimpleNamespace()),
        web_session_store=store,
    )
    if task_store is not None:
        runtime._navigation_task_store = lambda: task_store  # type: ignore[method-assign]
        runtime._navigation_durable_state_anchor = (  # type: ignore[method-assign]
            lambda _session_id, **_kwargs: {"execution_status": "running"}
        )
    return runtime


def _create_navigation_task(
    *,
    store: WebSessionStore,
    task_store: SqliteNavigationTaskStore,
    web_session_id: str,
    task_id: str = "task-private-1",
    task_ref: str = "task_public_A1B2",
    navigation_session_id: str = "navigation-private-1",
    segments: list[str] | None = None,
):
    creation = store.create_task_binding(
        web_session_id,
        task_id=task_id,
        task_ref=task_ref,
        navigation_session_id=navigation_session_id,
    )
    task = task_store.create_task_attempt(
        task_id=task_id,
        request="处理导航数据",
        target="navigation_data",
        date="20260720",
        segments=segments or ["clip_public_001"],
        scene_mode="out",
        dry_run=False,
        web_session_id=web_session_id,
        agentscope_session_id=navigation_session_id,
    ).task
    return creation.binding, task


class _RecordingBridge:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[Any]]] = []

    async def publish_records(self, session_id: str, records: list[Any]) -> None:
        self.calls.append((session_id, list(records)))


@pytest.mark.asyncio
async def test_system_controller_final_is_published_live_and_clears_active_mapping(
    tmp_path: Path,
) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    session = store.create_session("系统接管", contract_version=1)
    turn = store.begin_user_turn(session.id, "启动任务").turn
    mapping = store.save_conversation_agent_session(
        session.id,
        agent_role="router",
        agent_id="main-router-agent",
        agentscope_session_id="router-private-1",
    )
    store.bind_conversation_agent_session_to_turn(mapping.agentscope_session_id, turn.id)
    runtime = _runtime(tmp_path, store=store)
    bridge = _RecordingBridge()
    runtime.set_web_event_bridge(bridge)

    await runtime._commit_system_controller_final(
        turn_id=turn.id,
        text="任务未能安全启动，请稍后重试。",
    )

    assert len(bridge.calls) == 1
    assert bridge.calls[0][0] == session.id
    assert [record.type for record in bridge.calls[0][1]] == ["final", "turn_state"]
    assert bridge.calls[0][1][0].payload["text"] == "任务未能安全启动，请稍后重试。"
    assert store.get_active_turn(session.id) is None
    updated_mapping = store.get_conversation_agent_session_by_agentscope_session(
        mapping.agentscope_session_id
    )
    assert updated_mapping is not None
    assert updated_mapping.active_turn_id is None
    authority = store.get_response_authority(turn.id)
    assert authority is not None
    assert authority.producer == "system_controller"
    assert authority.lease_state == "closed"
    detail = store.get_session(session.id)
    assert detail is not None
    assert [message.content for message in detail.messages if message.role == "assistant"] == [
        "任务未能安全启动，请稍后重试。"
    ]


@pytest.mark.asyncio
async def test_v1_async_reply_failure_closes_authority_with_one_safe_final(
    tmp_path: Path,
) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    session = store.create_session("异步失败", contract_version=1)
    turn = store.begin_user_turn(session.id, "开始处理").turn
    mapping = store.save_conversation_agent_session(
        session.id,
        agent_role="router",
        agent_id="main-router-agent",
        agentscope_session_id="router-private-failure",
    )
    store.bind_conversation_agent_session_to_turn(mapping.agentscope_session_id, turn.id)
    lease_id = store.register_pending_reply(
        turn_id=turn.id,
        agentscope_session_id=mapping.agentscope_session_id,
        agent_id=mapping.agent_id,
        source="user",
    )
    runtime = _runtime(tmp_path, store=store)
    bridge = _RecordingBridge()
    runtime.set_web_event_bridge(bridge)

    await runtime._settle_failed_reply(lease_id)

    authority = store.get_response_authority(turn.id)
    assert authority is not None
    assert authority.producer == "system_controller"
    assert authority.lease_state == "closed"
    assert authority.final_message_id is not None
    detail = store.get_session(session.id)
    assert detail is not None
    assistant_messages = [
        message for message in detail.messages if message.role == "assistant"
    ]
    assert len(assistant_messages) == 1
    assert assistant_messages[0].content.strip()
    assert "router-private-failure" not in assistant_messages[0].content
    assert detail.turns[0].status == "failed"
    assert [event.type for event in detail.events].count("final") == 1
    assert len(bridge.calls) == 1
    assert [record.type for record in bridge.calls[0][1]] == ["final", "turn_state"]
    assert store.get_active_turn(session.id) is None


@pytest.mark.asyncio
async def test_request_input_ends_origin_turn_and_same_reply_resumes_in_interaction_turn(
    tmp_path: Path,
) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    task_store = SqliteNavigationTaskStore(tmp_path / "navigation.sqlite")
    session = store.create_session("等待确认", contract_version=1)
    origin_turn = store.begin_user_turn(session.id, "开始处理导航数据").turn
    binding, _task = _create_navigation_task(
        store=store,
        task_store=task_store,
        web_session_id=session.id,
    )
    store.bind_conversation_agent_session_to_turn(
        binding.navigation_session_id,
        origin_turn.id,
    )
    router_authority = store.get_response_authority(origin_turn.id)
    assert router_authority is not None
    store.handover_response_authority(
        origin_turn.id,
        expected_producer="router",
        expected_generation=router_authority.generation,
    )
    lease_id = store.register_pending_reply(
        turn_id=origin_turn.id,
        agentscope_session_id=binding.navigation_session_id,
        agent_id="navigation-data-agent",
        source="delegation",
    )
    store.append_projected_event_batch(
        web_session_id=session.id,
        agentscope_session_id=binding.navigation_session_id,
        entry_id="request-input-0",
        events=[],
        raw_event_type="REPLY_START",
        reply_id="reply-private-shared",
    )
    runtime = _runtime(tmp_path, store=store, task_store=task_store)

    projected = runtime.project_contract_v1_event_batch(
        web_session_id=session.id,
        agentscope_session_id=binding.navigation_session_id,
        entry_id="request-input-1",
        events=(
            {
                "type": "human_decision_required",
                "payload": {
                    "decision_type": "camera_params",
                    "summary": "请确认预览中的标定参数。",
                    "request_id": "request-private-shared",
                    "tool_call_id": "tool-call-private-shared",
                    "reply_id": "reply-private-shared",
                    "plan_id": "plan-private-shared",
                    "step_id": "step-private-shared",
                },
            },
        ),
    )
    assert {event["type"] for event in projected} == {
        "task_state_updated",
        "interaction_required",
    }
    store.append_projected_event_batch(
        web_session_id=session.id,
        agentscope_session_id=binding.navigation_session_id,
        entry_id="request-input-1",
        events=list(projected),
        raw_event_type="REPLY_CHUNK",
        reply_id="reply-private-shared",
    )

    closed_origin = store.get_turn(origin_turn.id)
    assert closed_origin is not None
    assert closed_origin.status == "completed"
    origin_authority = store.get_response_authority(origin_turn.id)
    assert origin_authority is not None
    assert origin_authority.lease_state == "closed"
    detail = store.get_session(session.id)
    assert detail is not None
    origin_answers = [
        message.content
        for message in detail.messages
        if message.role == "assistant" and message.turn_id == origin_turn.id
    ]
    assert len(origin_answers) == 1
    assert "请确认预览中的标定参数" in origin_answers[0]
    assert store.get_active_turn(session.id) is None

    interaction = store.get_open_interaction(web_session_id=session.id)
    assert interaction is not None
    resumed: list[dict[str, Any]] = []

    async def accept_decision(**kwargs: Any) -> bool:
        resumed.append(dict(kwargs))
        return True

    runtime.submit_human_decision = accept_decision  # type: ignore[method-assign]
    manager = AgentScopeWebSessionManager(store=store, runtime=runtime)
    result = await manager.submit_interaction_response(
        session.id,
        interaction.interaction_id,
        {
            "option_id": "confirm",
            "interaction_revision": interaction.revision,
            "expected_task_revision": interaction.expected_task_revision,
            "idempotency_key": "confirm-shared-reply",
        },
    )

    interaction_turn_id = result["turn_id"]
    assert interaction_turn_id != origin_turn.id
    interaction_turn = store.get_turn(interaction_turn_id)
    assert interaction_turn is not None
    assert interaction_turn.origin == "interaction"
    assert len(resumed) == 1
    assert resumed[0]["decision"]["reply_id"] == "reply-private-shared"
    assert resumed[0]["agentscope_session_id_override"] == binding.navigation_session_id

    final_projection = runtime.project_contract_v1_event_batch(
        web_session_id=session.id,
        agentscope_session_id=binding.navigation_session_id,
        entry_id="request-input-2",
        events=(
            {
                "type": "final",
                "payload": {"text": "已按确认的标定参数继续处理。"},
            },
        ),
    )
    store.append_projected_event_batch(
        web_session_id=session.id,
        agentscope_session_id=binding.navigation_session_id,
        entry_id="request-input-2",
        events=list(final_projection),
        raw_event_type="REPLY_END",
        reply_id="reply-private-shared",
    )

    completed_interaction_turn = store.get_turn(interaction_turn_id)
    assert completed_interaction_turn is not None
    assert completed_interaction_turn.status == "completed"
    detail = store.get_session(session.id)
    assert detail is not None
    interaction_answers = [
        message.content
        for message in detail.messages
        if message.role == "assistant" and message.turn_id == interaction_turn_id
    ]
    assert interaction_answers == ["已按确认的标定参数继续处理。"]
    # The original lease is the same private reply that the interaction turn resumes.
    assert lease_id


class _RevisionAdvancingRuntime:
    def __init__(self, tmp_path: Path) -> None:
        self.app = FastAPI()
        self.config = _config(tmp_path)
        self.store: WebSessionStore | None = None
        self.task_revision = 7
        self.resumes: list[dict[str, Any]] = []
        self.decisions: list[dict[str, Any]] = []

    def set_web_session_store(self, store: WebSessionStore) -> None:
        self.store = store

    async def submit_user_message(self, **_kwargs: Any) -> str:
        return "router-private-session"

    def interaction_task_revision_v1(self, **_kwargs: Any) -> int:
        return self.task_revision

    async def submit_interaction_response_v1(self, **kwargs: Any) -> bool:
        self.resumes.append(dict(kwargs))
        self.task_revision += 1
        return True

    async def submit_human_decision(self, **kwargs: Any) -> bool:
        self.decisions.append(dict(kwargs))
        return True

    async def recover_human_decision_handoff(self, **_kwargs: Any) -> dict[str, Any]:
        return {"recovered": True}

    def session_task_snapshots(self, _session_id: str) -> list[dict[str, Any]]:
        return [
            {
                "task_ref": "task_public_REPLAY",
                "status": "waiting_user" if self.task_revision == 7 else "active",
                "state_revision": self.task_revision,
            }
        ]

    def pending_interaction_snapshot(self, session_id: str) -> dict[str, Any] | None:
        if self.store is None:
            return None
        interaction = self.store.get_open_interaction(web_session_id=session_id)
        if interaction is None:
            return None
        return {
            "interaction_id": interaction.interaction_id,
            "task_ref": interaction.task_ref,
            "kind": interaction.kind,
            "blocking": interaction.blocking,
            "risk": interaction.risk,
            "title": interaction.title,
            "summary": interaction.summary,
            "options": list(interaction.options),
            "interaction_revision": interaction.revision,
            "expected_task_revision": interaction.expected_task_revision,
            "expires_at": interaction.expires_at,
        }

    async def subscribe_web_session_events(self, *, web_session_id: str):
        del web_session_id
        if False:
            yield None


def _v1_interaction_client(
    tmp_path: Path,
) -> tuple[TestClient, FastAPI, _RevisionAdvancingRuntime, str, str]:
    runtime = _RevisionAdvancingRuntime(tmp_path)
    app = create_app(
        working_dir=str(tmp_path / ".djx"),
        db_path=tmp_path / "api-sessions.sqlite",
        agentscope_runtime=runtime,
    )
    client = TestClient(app)
    created = client.post(
        "/api/sessions",
        json={"message": "处理导航数据", "entrypoint": "chat"},
    )
    assert created.status_code == 200
    session_id = created.json()["session"]["id"]
    binding = app.state.store.create_task_binding(
        session_id,
        task_id="task-private-replay",
        task_ref="task_public_REPLAY",
        navigation_session_id="navigation-private-replay",
    )
    interaction = app.state.store.create_interaction(
        session_id,
        task_ref=binding.binding.task_ref,
        kind="high_risk_confirmation",
        blocking=True,
        risk="high",
        title="确认继续",
        summary="确认后继续处理。",
        options=[
            {"option_id": "confirm", "label": "确认"},
            {"option_id": "reject", "label": "停止"},
        ],
        expected_task_revision=runtime.task_revision,
        private_payload={
            "request_id": "request-private-replay",
            "tool_call_id": "tool-private-replay",
            "reply_id": "reply-private-replay",
        },
    )
    return client, app, runtime, session_id, interaction.interaction_id


def test_interaction_idempotency_replay_survives_task_revision_advancing(
    tmp_path: Path,
) -> None:
    client, app, runtime, session_id, interaction_id = _v1_interaction_client(tmp_path)
    payload = {
        "option_id": "confirm",
        "interaction_revision": 1,
        "expected_task_revision": 7,
        "idempotency_key": "deterministic-replay-key",
    }

    first = client.post(
        f"/api/sessions/{session_id}/interactions/{interaction_id}/responses",
        json=payload,
    )
    replay = client.post(
        f"/api/sessions/{session_id}/interactions/{interaction_id}/responses",
        json=payload,
    )

    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json()["turn_id"] == first.json()["turn_id"]
    assert len(runtime.resumes) == 1
    detail = app.state.store.get_session(session_id)
    assert detail is not None
    assert len([turn for turn in detail.turns if turn.origin == "interaction"]) == 1


def test_contract_v1_rejects_legacy_human_decision_endpoint(tmp_path: Path) -> None:
    client, _app, runtime, session_id, _interaction_id = _v1_interaction_client(tmp_path)

    response = client.post(
        f"/api/sessions/{session_id}/human-decisions",
        json={
            "action": "confirm",
            "request_id": "request-private-legacy",
            "tool_call_id": "tool-private-legacy",
            "reply_id": "reply-private-legacy",
        },
    )

    assert response.status_code == 409
    assert runtime.decisions == []


def test_task_summary_clips_are_bounded_and_sanitized(tmp_path: Path) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    task_store = SqliteNavigationTaskStore(tmp_path / "navigation.sqlite")
    session = store.create_session("安全任务摘要", contract_version=1)
    secret_token = "Bearer task-summary-secret-token"
    secret_path = "/Users/private-user/company/workspace/raw/clip-secret-001"
    segments = [
        "clip_public_001",
        secret_path,
        secret_token,
        "api_key=task-summary-secret-key",
        *[f"clip_{index:03d}_" + "x" * 500 for index in range(20)],
    ]
    binding, task = _create_navigation_task(
        store=store,
        task_store=task_store,
        web_session_id=session.id,
        segments=segments,
    )
    task_store.update_task_for_session(
        task.task_id,
        web_session_id=session.id,
        agentscope_session_id=binding.navigation_session_id,
        expected_state_revision=task.state_revision,
        status=NavigationTaskStatus.ACTIVE,
    )
    runtime = _runtime(tmp_path, store=store, task_store=task_store)

    summary = runtime._task_summary(binding, max_chars=1200)

    assert summary is not None
    rendered = json.dumps(summary, ensure_ascii=False, separators=(",", ":"))
    assert len(rendered) <= 1200
    assert "clip_public_001" in rendered
    for secret in (
        secret_path,
        "/Users/private-user",
        "private-user",
        secret_token,
        "task-summary-secret-token",
        "task-summary-secret-key",
    ):
        assert secret not in rendered

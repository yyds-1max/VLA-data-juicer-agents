from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from vla_data_juicer_agents.runtime.agentscope_config import AgentScopeRuntimeConfig
from vla_data_juicer_agents.runtime.agentscope_runtime import AgentScopeRuntime
from vla_data_juicer_agents.web.app import create_app
from vla_data_juicer_agents.web.schemas import SessionDetail
from vla_data_juicer_agents.web.session_store import WebSessionStore


def _config(tmp_path) -> AgentScopeRuntimeConfig:
    return AgentScopeRuntimeConfig(
        user_id="test-user",
        redis_url="redis://localhost:6379/0",
        workspace_root=tmp_path,
        dashscope_api_key="test-key",
        dashscope_base_url=None,
        default_model="test-model",
        router_model="router-model",
        navigation_model="navigation-model",
    )


@pytest.mark.parametrize("entrypoint", ["chat", "data_management_shortcut"])
def test_all_new_sessions_use_contract_v1(
    tmp_path,
    entrypoint: str,
) -> None:
    config = _config(tmp_path)

    runtime = _ContractRuntime(config=config)
    app = create_app(
        working_dir=str(tmp_path / ".djx"),
        db_path=tmp_path / f"{entrypoint}.sqlite",
        agentscope_runtime=runtime,
    )
    payload: dict[str, Any] = {"message": "处理导航数据", "entrypoint": entrypoint}
    if entrypoint == "data_management_shortcut":
        payload["request_context"] = {
            "kind": "navigation_dataset_selection_v1",
            "dataset_date": "20260720",
            "selection": {"kind": "all_clips"},
        }
    with TestClient(app) as client:
        response = client.post("/api/sessions", json=payload)

    assert response.status_code == 200
    session_id = response.json()["session"]["id"]
    assert app.state.store.get_session_contract_version(session_id) == 1


def test_shortcut_requires_private_scope_and_chat_rejects_it(tmp_path) -> None:
    runtime = _ContractRuntime(config=_config(tmp_path))
    app = create_app(
        working_dir=str(tmp_path / ".djx"),
        db_path=tmp_path / "scope-validation.sqlite",
        agentscope_runtime=runtime,
    )
    context = {
        "kind": "navigation_dataset_selection_v1",
        "dataset_date": "20260720",
        "selection": {
            "kind": "selected_clips",
            "clips": ["20260605_152856"],
        },
    }
    with TestClient(app) as client:
        missing = client.post(
            "/api/sessions",
            json={"message": "处理选中数据", "entrypoint": "data_management_shortcut"},
        )
        unexpected = client.post(
            "/api/sessions",
            json={"message": "处理选中数据", "entrypoint": "chat", "request_context": context},
        )

    assert missing.status_code == 422
    assert unexpected.status_code == 422


def test_shortcut_rejects_scope_that_exceeds_router_envelope_budget(tmp_path) -> None:
    runtime = _ContractRuntime(config=_config(tmp_path))
    app = create_app(
        working_dir=str(tmp_path / ".djx"),
        db_path=tmp_path / "oversized-scope.sqlite",
        agentscope_runtime=runtime,
    )
    context = {
        "kind": "navigation_dataset_selection_v1",
        "dataset_date": "20260720",
        "selection": {
            "kind": "selected_clips",
            "clips": [f"20260605_{index:06d}" for index in range(200)],
        },
    }

    with TestClient(app) as client:
        response = client.post(
            "/api/sessions",
            json={
                "message": "处理选中数据",
                "entrypoint": "data_management_shortcut",
                "request_context": context,
            },
        )

    assert response.status_code == 422
    assert "Router context budget" in response.text


def test_session_detail_serializer_always_exposes_v1_snapshot() -> None:
    common: dict[str, Any] = {
        "id": "session-public",
        "title": "导航任务",
        "status": "active",
        "created_at": "2026-07-20T00:00:00Z",
        "updated_at": "2026-07-20T00:00:00Z",
        "tasks": [{"task_ref": "task_public_abc", "status": "active"}],
        "pending_interaction": {
            "interaction_id": "interaction_public_abc",
            "task_ref": "task_public_abc",
        },
    }

    v1 = SessionDetail(contract_version=1, **common).model_dump(mode="json")
    assert v1["contract_version"] == 1
    assert v1["tasks"] == common["tasks"]
    assert v1["pending_interaction"] == common["pending_interaction"]


def test_router_context_envelope_is_bounded_and_excludes_specialist_internals(tmp_path) -> None:
    internal_task_id = "task-internal-secret-42"
    internal_navigation_session = "navigation-session-secret-42"
    binding = SimpleNamespace(
        task_id=internal_task_id,
        task_ref="task_public_A1B2",
        navigation_session_id=internal_navigation_session,
        state_revision=3,
        latest_public_update=(
            "已检查 /Users/private-user/work/result.json，Bearer secret-token-value。" * 80
        ),
    )
    task = SimpleNamespace(
        task_id=internal_task_id,
        status=SimpleNamespace(value="active"),
        accepted_plan_phase="inspection",
        date="20260720",
        segments=[f"clip_{index:03d}" for index in range(200)],
        scene_mode="outdoor",
        state_revision=3,
        created_at="2026-07-20T00:00:00Z",
        updated_at="2026-07-20T00:01:00Z",
    )
    interaction = SimpleNamespace(
        interaction_id="interaction_public_1",
        task_ref="task_public_A1B2",
        kind="high_risk_confirmation",
        blocking=True,
        risk="high",
        title="确认执行",
        summary="请选择是否继续。",
        options=({"option_id": "confirm", "label": "确认"},),
        private_payload={
            "request_id": "request-internal-secret",
            "tool_call_id": "call-internal-secret",
        },
        revision=1,
        expected_task_revision=3,
        expires_at=None,
    )
    store = _EnvelopeStore(binding=binding, interaction=interaction)
    runtime = AgentScopeRuntime(
        config=_config(tmp_path),
        storage=None,
        message_bus=None,
        workspace_manager=None,
        app=SimpleNamespace(),
        web_session_store=store,
    )
    runtime._navigation_task_store = lambda: _TaskStore(task)  # type: ignore[method-assign]

    envelope = runtime.router_context_envelope("web-session-internal")
    rendered = json.dumps(envelope, ensure_ascii=False)

    assert len(rendered) <= 4000
    assert envelope["focused_task_ref"] == "task_public_A1B2"
    assert internal_task_id not in rendered
    assert internal_navigation_session not in rendered
    assert "request-internal-secret" not in rendered
    assert "call-internal-secret" not in rendered
    assert "/Users/private-user" not in rendered
    assert "secret-token-value" not in rendered


def test_late_router_final_is_ignored_after_navigation_receives_authority(tmp_path) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    session = store.create_session("导航任务", contract_version=1)
    turn = store.begin_user_turn(session.id, "处理导航数据").turn
    store.save_conversation_agent_session(
        session.id,
        agent_role="router",
        agent_id="main-router-agent",
        agentscope_session_id="router-private-session",
    )
    store.create_task_binding(
        session.id,
        task_id="task-private-1",
        task_ref="task_public_1",
        navigation_session_id="navigation-private-session",
    )
    store.bind_conversation_agent_session_to_turn("router-private-session", turn.id)
    store.bind_conversation_agent_session_to_turn("navigation-private-session", turn.id)
    store.handover_response_authority(
        turn.id,
        expected_producer="router",
        expected_generation=1,
    )

    late = store.append_projected_event_batch(
        web_session_id=session.id,
        agentscope_session_id="router-private-session",
        entry_id="router-2",
        raw_event_type="REPLY_END",
        reply_id="router-reply",
        events=[{"type": "final", "payload": {"text": "迟到的 Router 总结"}}],
    )
    navigation = store.append_projected_event_batch(
        web_session_id=session.id,
        agentscope_session_id="navigation-private-session",
        entry_id="navigation-2",
        raw_event_type="REPLY_END",
        reply_id="navigation-reply",
        events=[{"type": "final", "payload": {"text": "导航处理完成"}}],
    )

    assert all(event.type != "final" for event in late)
    assert [event.payload["text"] for event in navigation if event.type == "final"] == [
        "导航处理完成"
    ]
    detail = store.get_session(session.id)
    assert detail is not None
    assistant_messages = [message.content for message in detail.messages if message.role == "assistant"]
    assert assistant_messages == ["导航处理完成"]
    authority = store.get_response_authority(turn.id)
    assert authority is not None
    assert authority.producer == "navigation"
    assert authority.lease_state == "closed"


def test_interaction_api_is_idempotent_and_resumes_navigation_without_router(tmp_path) -> None:
    runtime = _ContractRuntime(config=_config(tmp_path))
    app = create_app(
        working_dir=str(tmp_path / ".djx"),
        db_path=tmp_path / "interaction.sqlite",
        agentscope_runtime=runtime,
    )
    with TestClient(app) as client:
        session_id, interaction_id = _create_interaction_fixture(client, app, runtime)
        payload = {
            "option_id": "confirm",
            "interaction_revision": 1,
            "expected_task_revision": 7,
            "idempotency_key": "click-once",
        }

        first = client.post(
            f"/api/sessions/{session_id}/interactions/{interaction_id}/responses",
            json=payload,
        )
        duplicate = client.post(
            f"/api/sessions/{session_id}/interactions/{interaction_id}/responses",
            json=payload,
        )

    assert first.status_code == 200
    assert duplicate.status_code == 200
    assert duplicate.json()["turn_id"] == first.json()["turn_id"]
    assert runtime.router_calls == []
    assert len(runtime.navigation_resumes) == 1
    assert runtime.navigation_resumes[0]["option_ids"] == ["confirm"]
    detail = app.state.store.get_session(session_id)
    assert detail is not None
    interaction_turns = [turn for turn in detail.turns if turn.origin == "interaction"]
    assert [turn.id for turn in interaction_turns] == [first.json()["turn_id"]]


def test_interaction_revision_conflict_returns_latest_v1_snapshot(tmp_path) -> None:
    runtime = _ContractRuntime(config=_config(tmp_path))
    app = create_app(
        working_dir=str(tmp_path / ".djx"),
        db_path=tmp_path / "revision-conflict.sqlite",
        agentscope_runtime=runtime,
    )
    with TestClient(app) as client:
        session_id, interaction_id = _create_interaction_fixture(client, app, runtime)
        runtime.task_revision = 8
        response = client.post(
            f"/api/sessions/{session_id}/interactions/{interaction_id}/responses",
            json={
                "option_id": "confirm",
                "interaction_revision": 1,
                "expected_task_revision": 7,
                "idempotency_key": "stale-click",
            },
        )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "task_revision_mismatch"
    assert detail["session"]["contract_version"] == 1
    assert detail["session"]["tasks"] == runtime.task_snapshots
    assert runtime.router_calls == []
    assert runtime.navigation_resumes == []


def test_runtime_public_projection_has_no_private_metadata_or_percentage(tmp_path) -> None:
    task_id = "task-internal-999"
    navigation_session = "navigation-session-internal-999"
    task_ref = "task_public_Z9Y8"
    binding = SimpleNamespace(
        task_id=task_id,
        task_ref=task_ref,
        navigation_session_id=navigation_session,
        state_revision=1,
        latest_public_update=None,
    )
    task = SimpleNamespace(
        task_id=task_id,
        status=SimpleNamespace(value="active"),
        accepted_plan_phase="inspection",
        date="20260720",
        segments=["clip_a"],
        scene_mode="outdoor",
        state_revision=1,
        created_at="2026-07-20T00:00:00Z",
        updated_at="2026-07-20T00:01:00Z",
    )
    store = _ProjectionStore(
        mapping=SimpleNamespace(
            agent_role="navigation",
            task_id=task_id,
            active_turn_id="turn-internal-999",
        ),
        binding=binding,
    )
    runtime = AgentScopeRuntime(
        config=_config(tmp_path),
        storage=None,
        message_bus=None,
        workspace_manager=None,
        app=SimpleNamespace(),
        web_session_store=store,
    )
    runtime._navigation_task_store = lambda: _TaskStore(task)  # type: ignore[method-assign]

    events = runtime.project_contract_v1_event_batch(
        web_session_id="web-internal-999",
        agentscope_session_id=navigation_session,
        entry_id="10-0",
        events=(
            {
                "type": "tool_start",
                "source": "NavigationDataAgent",
                "run_id": "run-internal-999",
                "payload": {
                    "tool": "extract_and_sync_navigation_data_tool",
                    "call_id": "call-internal-999",
                },
            },
            {
                "type": "progress_delta",
                "payload": {
                    "delta": "NavigationDataAgent 已完成 75%，结果位于 /Users/sfy/private/out.json。"
                },
            },
            {
                "type": "final",
                "payload": {
                    "text": "NavigationDataAgent 已完成 75%，tool_call_id=call-internal-999。"
                },
            },
        ),
    )
    rendered = json.dumps(events, ensure_ascii=False)

    assert events
    assert all(set(event) == {"contract_version", "type", "payload"} for event in events)
    assert task_ref in rendered
    for private_value in (
        task_id,
        navigation_session,
        "turn-internal-999",
        "run-internal-999",
        "call-internal-999",
        "extract_and_sync_navigation_data_tool",
        "NavigationDataAgent",
        "/Users/sfy/private/out.json",
        "75%",
    ):
        assert private_value not in rendered
    assert "%" not in rendered


class _TaskStore:
    def __init__(self, task: Any) -> None:
        self.task = task

    def get_task(self, task_id: str) -> Any | None:
        return self.task if task_id == self.task.task_id else None


class _EnvelopeStore:
    def __init__(self, *, binding: Any, interaction: Any) -> None:
        self.binding = binding
        self.interaction = interaction

    @staticmethod
    def get_session_contract_version(_session_id: str) -> int:
        return 1

    @staticmethod
    def get_active_turn(_session_id: str) -> None:
        return None

    def get_focused_task_binding(self, _session_id: str) -> Any:
        return self.binding

    @staticmethod
    def get_task_focus(_session_id: str) -> Any:
        return SimpleNamespace(generation=5)

    def list_task_bindings(self, _session_id: str) -> list[Any]:
        return [self.binding]

    def get_open_interaction(self, *, web_session_id: str) -> Any:
        del web_session_id
        return self.interaction


class _ProjectionStore:
    def __init__(self, *, mapping: Any, binding: Any) -> None:
        self.mapping = mapping
        self.binding = binding

    @staticmethod
    def get_session_contract_version(_session_id: str) -> int:
        return 1

    def get_conversation_agent_session_by_agentscope_session(self, _session_id: str) -> Any:
        return self.mapping

    def get_task_binding(self, task_id: str) -> Any | None:
        return self.binding if task_id == self.binding.task_id else None


class _ContractRuntime:
    def __init__(self, *, config: AgentScopeRuntimeConfig) -> None:
        self.app = FastAPI()
        self.config = config
        self.store: WebSessionStore | None = None
        self.task_revision = 7
        self.router_calls: list[dict[str, Any]] = []
        self.navigation_resumes: list[dict[str, Any]] = []
        self.task_snapshots = [
            {
                "task_ref": "task_public_A1B2",
                "status": "waiting_user",
                "phase": "waiting_input",
                "state_revision": 7,
            }
        ]

    def set_web_session_store(self, store: WebSessionStore) -> None:
        self.store = store

    async def submit_user_message(self, **kwargs: Any) -> str:
        self.router_calls.append(dict(kwargs))
        return str(kwargs.get("turn_id") or "turn-router")

    def interaction_task_revision_v1(self, *, web_session_id: str, task_id: str) -> int:
        del web_session_id, task_id
        return self.task_revision

    async def submit_interaction_response_v1(self, **kwargs: Any) -> bool:
        self.navigation_resumes.append(dict(kwargs))
        return True

    def session_task_snapshots(self, _session_id: str) -> list[dict[str, Any]]:
        return self.task_snapshots

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


def _create_interaction_fixture(
    client: TestClient,
    app: FastAPI,
    runtime: _ContractRuntime,
) -> tuple[str, str]:
    created = client.post(
        "/api/sessions",
        json={
            "message": "处理导航数据",
            "entrypoint": "data_management_shortcut",
            "request_context": {
                "kind": "navigation_dataset_selection_v1",
                "dataset_date": "20260720",
                "selection": {"kind": "all_clips"},
            },
        },
    )
    assert created.status_code == 200
    session_id = created.json()["session"]["id"]
    binding = app.state.store.create_task_binding(
        session_id,
        task_id="task-internal-A1B2",
        task_ref="task_public_A1B2",
        navigation_session_id="navigation-session-internal-A1B2",
    )
    interaction = app.state.store.create_interaction(
        session_id,
        task_ref=binding.binding.task_ref,
        kind="high_risk_confirmation",
        blocking=True,
        risk="high",
        title="确认执行",
        summary="确认后将继续执行。",
        options=[
            {"id": "confirm", "label": "确认"},
            {"id": "reject", "label": "拒绝"},
        ],
        expected_task_revision=runtime.task_revision,
        private_payload={
            "request_id": "request-private",
            "tool_call_id": "call-private",
            "reply_id": "reply-private",
        },
    )
    return session_id, interaction.interaction_id

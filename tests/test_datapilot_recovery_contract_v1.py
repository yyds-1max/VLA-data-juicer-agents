from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from vla_data_juicer_agents.navigation.task_state import NavigationTaskStatus
from vla_data_juicer_agents.navigation.task_store import SqliteNavigationTaskStore
from vla_data_juicer_agents.runtime.agentscope_config import AgentScopeRuntimeConfig
from vla_data_juicer_agents.runtime.agentscope_runtime import AgentScopeRuntime
from vla_data_juicer_agents.web.agent_session import AgentScopeWebSessionManager
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
    )


def _runtime(
    tmp_path: Path,
    *,
    store: WebSessionStore,
    task_store: SqliteNavigationTaskStore,
) -> AgentScopeRuntime:
    runtime = AgentScopeRuntime(
        config=_config(tmp_path),
        storage=None,
        message_bus=SimpleNamespace(),
        workspace_manager=None,
        app=SimpleNamespace(state=SimpleNamespace()),
        web_session_store=store,
    )
    runtime._navigation_task_store = lambda: task_store  # type: ignore[method-assign]
    return runtime


def _create_bound_task(
    *,
    store: WebSessionStore,
    task_store: SqliteNavigationTaskStore,
    web_session_id: str,
    task_id: str = "task-private-1",
    task_ref: str = "task_public_A1B2",
    navigation_session_id: str = "navigation-private-1",
    origin_turn_id: str | None = None,
):
    payload = {
        "request": "处理导航数据",
        "target": "navigation_data",
        "date": "20260720",
        "clips": ["clip_a"],
        "scene_mode": "outdoor",
        "response_language": "Chinese",
    }
    if origin_turn_id is not None:
        payload["origin_turn_id"] = origin_turn_id
    creation = store.create_task_binding(
        web_session_id,
        task_id=task_id,
        task_ref=task_ref,
        navigation_session_id=navigation_session_id,
        outbox_payload=payload,
    )
    task = task_store.create_task_attempt(
        task_id=task_id,
        request="处理导航数据",
        target="navigation_data",
        date="20260720",
        segments=["clip_a"],
        scene_mode="out",
        dry_run=False,
        web_session_id=web_session_id,
        agentscope_session_id=navigation_session_id,
    ).task
    return creation, task


def _complete_outbox(store: WebSessionStore, outbox_id: str) -> None:
    claimed = store.claim_outbox_item(outbox_id, worker_id="test-fixture")
    assert claimed.status == "claimed"
    store.complete_outbox(outbox_id, worker_id="test-fixture")


@pytest.mark.asyncio
async def test_navigation_start_recovery_does_not_silently_complete_a_stranded_running_run(
    tmp_path: Path,
) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    task_store = SqliteNavigationTaskStore(tmp_path / "navigation.sqlite")
    session = store.create_session("恢复导航启动", contract_version=1)
    turn = store.begin_user_turn(session.id, "处理导航数据").turn
    creation, task = _create_bound_task(
        store=store,
        task_store=task_store,
        web_session_id=session.id,
        origin_turn_id=turn.id,
    )
    store.bind_conversation_agent_session_to_turn(
        creation.binding.navigation_session_id,
        turn.id,
    )
    store.handover_response_authority(
        turn.id,
        expected_producer="router",
        expected_generation=1,
        new_producer="navigation",
    )
    store.create_turn_run(
        run_id="navigation-run-stranded",
        turn_id=turn.id,
        producer="navigation",
        task_id=task.task_id,
        agentscope_session_id=creation.binding.navigation_session_id,
        status="running",
    )
    runtime = _runtime(tmp_path, store=store, task_store=task_store)
    starts: list[dict[str, Any]] = []

    async def ensure_session(*_args: Any, **_kwargs: Any) -> str:
        return creation.binding.navigation_session_id

    async def start_run(**kwargs: Any) -> None:
        starts.append(dict(kwargs))

    runtime.ensure_web_session = ensure_session  # type: ignore[method-assign]
    runtime._start_agent_run = start_run  # type: ignore[method-assign]

    await runtime.recover_contract_v1_outbox_once()

    outbox = store.get_outbox(creation.outbox.outbox_id)
    run = store.get_latest_turn_run(turn.id, producer="navigation")
    authority = store.get_response_authority(turn.id)
    assert outbox is not None
    assert run is not None
    assert authority is not None
    recovered = bool(starts) or run.status != "running"
    safely_failed = outbox.status == "failed" and authority.lease_state == "closed"
    assert recovered or safely_failed, (
        "a persisted running turn_run is not proof that a runner survived restart; "
        "the start must be recovered or the turn must be closed by the system controller"
    )
    if outbox.status == "completed":
        assert recovered


@pytest.mark.asyncio
async def test_navigation_continue_handover_outbox_recovers_a_stranded_run(
    tmp_path: Path,
) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    task_store = SqliteNavigationTaskStore(tmp_path / "navigation.sqlite")
    session = store.create_session("恢复继续任务", contract_version=1)
    creation, task = _create_bound_task(
        store=store,
        task_store=task_store,
        web_session_id=session.id,
    )
    _complete_outbox(store, creation.outbox.outbox_id)
    pausing = task_store.update_task_for_session(
        task.task_id,
        web_session_id=session.id,
        agentscope_session_id=creation.binding.navigation_session_id,
        expected_state_revision=task.state_revision,
        status=NavigationTaskStatus.PAUSING,
    )
    paused = task_store.update_task_for_session(
        task.task_id,
        web_session_id=session.id,
        agentscope_session_id=creation.binding.navigation_session_id,
        expected_state_revision=pausing.state_revision,
        status=NavigationTaskStatus.PAUSED,
    )
    binding = store.update_task_binding(
        task.task_id,
        expected_revision=creation.binding.state_revision,
        status="paused",
        latest_public_update="任务已暂停。",
    )
    turn = store.begin_user_turn(session.id, "继续刚才的任务").turn
    authority = store.get_response_authority(turn.id)
    assert authority is not None
    _, outbox = store.handover_response_authority_with_outbox(
        turn.id,
        expected_producer="router",
        expected_generation=authority.generation,
        new_producer="navigation",
        kind="navigation_continue",
        aggregate_type="navigation_task",
        aggregate_id=task.task_id,
        payload={
            "original_user_message": "继续刚才的任务",
            "navigation_session_id": binding.navigation_session_id,
        },
        idempotency_key=f"navigation_continue:{turn.id}:{task.task_id}",
        web_session_id=session.id,
        task_id=task.task_id,
    )
    store.create_turn_run(
        run_id="stranded-continue-run",
        turn_id=turn.id,
        producer="navigation",
        task_id=task.task_id,
        agentscope_session_id=binding.navigation_session_id,
        status="running",
    )
    runtime = _runtime(tmp_path, store=store, task_store=task_store)
    starts: list[dict[str, Any]] = []

    async def ensure_session(*_args: Any, **_kwargs: Any) -> str:
        return binding.navigation_session_id

    async def start_run(**kwargs: Any) -> None:
        starts.append(dict(kwargs))

    runtime.ensure_web_session = ensure_session  # type: ignore[method-assign]
    runtime._start_agent_run = start_run  # type: ignore[method-assign]

    await runtime.recover_contract_v1_outbox_once()

    recovered_task = task_store.get_task(paused.task_id)
    recovered_binding = store.get_task_binding(task.task_id)
    recovered_outbox = store.get_outbox(outbox.outbox_id)
    stale_run = store.get_latest_turn_run(turn.id, producer="navigation")
    assert starts
    assert recovered_task is not None
    assert recovered_task.status == NavigationTaskStatus.ACTIVE
    assert recovered_binding is not None
    assert recovered_binding.status == "active"
    assert recovered_outbox is not None
    assert recovered_outbox.status == "completed"
    assert stale_run is not None
    assert stale_run.status == "interrupted"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("transitional", "stable"),
    [
        (
            NavigationTaskStatus.PAUSING,
            {NavigationTaskStatus.PAUSED, NavigationTaskStatus.NEEDS_REPLAN},
        ),
        (
            NavigationTaskStatus.CANCELLING,
            {NavigationTaskStatus.CANCELLED, NavigationTaskStatus.NEEDS_REPLAN},
        ),
    ],
)
async def test_startup_recovery_settles_control_transition_and_updates_binding(
    tmp_path: Path,
    transitional: NavigationTaskStatus,
    stable: set[NavigationTaskStatus],
) -> None:
    store = WebSessionStore(tmp_path / f"sessions-{transitional.value}.sqlite")
    task_store = SqliteNavigationTaskStore(
        tmp_path / f"navigation-{transitional.value}.sqlite"
    )
    session = store.create_session("恢复控制状态", contract_version=1)
    creation, task = _create_bound_task(
        store=store,
        task_store=task_store,
        web_session_id=session.id,
    )
    _complete_outbox(store, creation.outbox.outbox_id)
    task_store.update_task_for_session(
        task.task_id,
        web_session_id=session.id,
        agentscope_session_id=creation.binding.navigation_session_id,
        expected_state_revision=task.state_revision,
        status=transitional,
    )
    store.update_task_binding(
        task.task_id,
        expected_revision=creation.binding.state_revision,
        status=transitional.value,
        latest_public_update="控制操作正在安全收尾。",
    )
    runtime = _runtime(tmp_path, store=store, task_store=task_store)

    await runtime.recover_contract_v1_outbox_once()

    recovered_task = task_store.get_task(task.task_id)
    recovered_binding = store.get_task_binding(task.task_id)
    assert recovered_task is not None
    assert recovered_binding is not None
    assert recovered_task.status in stable
    assert recovered_binding.status == recovered_task.status.value
    if recovered_task.status == NavigationTaskStatus.CANCELLED:
        assert recovered_binding.slot_state == "closed"
    else:
        assert recovered_binding.slot_state == "open"


def test_active_background_update_has_no_public_system_turn(
    tmp_path: Path,
) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    task_store = SqliteNavigationTaskStore(tmp_path / "navigation.sqlite")
    session = store.create_session("后台导航任务", contract_version=1)
    creation, _task = _create_bound_task(
        store=store,
        task_store=task_store,
        web_session_id=session.id,
    )
    runtime = _runtime(tmp_path, store=store, task_store=task_store)

    store.append_projected_event_batch(
        web_session_id=session.id,
        agentscope_session_id=creation.binding.navigation_session_id,
        entry_id="background-1-start",
        events=[],
        raw_event_type="REPLY_START",
        reply_id="background-reply-1",
    )
    projected = runtime.project_contract_v1_event_batch(
        web_session_id=session.id,
        agentscope_session_id=creation.binding.navigation_session_id,
        entry_id="background-1-end",
        events=(
            {
                "type": "final",
                "payload": {"text": "后台检查仍在继续。"},
            },
        ),
    )
    store.append_projected_event_batch(
        web_session_id=session.id,
        agentscope_session_id=creation.binding.navigation_session_id,
        entry_id="background-1-end",
        events=list(projected),
        raw_event_type="REPLY_END",
        reply_id="background-reply-1",
    )

    detail = store.get_session(session.id)
    assert detail is not None
    assert all(turn.origin != "system" for turn in detail.turns)
    assert [event.type for event in detail.events] == ["task_state_updated"]
    assert detail.events[0].payload["task_ref"] == creation.binding.task_ref
    assert detail.events[0].payload["status"] == "active"
    assert [message for message in detail.messages if message.role == "assistant"] == []


class _InteractionRuntime:
    def __init__(self, *, result: bool = False, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls = 0

    @staticmethod
    def interaction_task_revision_v1(*, web_session_id: str, task_id: str) -> int:
        del web_session_id, task_id
        return 7

    async def submit_interaction_response_v1(self, **_kwargs: Any) -> bool:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result

    @staticmethod
    def session_task_snapshots(_session_id: str) -> list[dict[str, Any]]:
        return []

    @staticmethod
    def pending_interaction_snapshot(_session_id: str) -> None:
        return None


def _interaction_fixture(
    tmp_path: Path,
    runtime: _InteractionRuntime,
) -> tuple[WebSessionStore, AgentScopeWebSessionManager, str, str, dict[str, Any]]:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    manager = AgentScopeWebSessionManager(store=store, runtime=runtime)
    session = store.create_session("确认导航参数", contract_version=1)
    creation = store.create_task_binding(
        session.id,
        task_id="task-private-interaction",
        task_ref="task_public_interaction",
        navigation_session_id="navigation-private-interaction",
    )
    interaction = store.create_interaction(
        session.id,
        task_ref=creation.binding.task_ref,
        kind="high_risk_confirmation",
        blocking=True,
        risk="high",
        title="确认执行",
        summary="确认后继续执行。",
        options=[
            {"option_id": "confirm", "label": "确认"},
            {"option_id": "reject", "label": "拒绝"},
        ],
        expected_task_revision=7,
    )
    response = {
        "option_id": "confirm",
        "interaction_revision": interaction.revision,
        "expected_task_revision": 7,
        "idempotency_key": "same-click",
    }
    return store, manager, session.id, interaction.interaction_id, response


@pytest.mark.asyncio
async def test_duplicate_interaction_response_preserves_original_false_result(
    tmp_path: Path,
) -> None:
    runtime = _InteractionRuntime(result=False)
    _store, manager, session_id, interaction_id, response = _interaction_fixture(
        tmp_path,
        runtime,
    )

    first = await manager.submit_interaction_response(
        session_id,
        interaction_id,
        response,
    )
    duplicate = await manager.submit_interaction_response(
        session_id,
        interaction_id,
        response,
    )

    assert first["accepted"] is False
    assert duplicate["accepted"] is False
    assert duplicate["turn_id"] == first["turn_id"]
    assert runtime.calls == 1


@pytest.mark.asyncio
async def test_interaction_resume_exception_does_not_leave_an_unrecoverable_active_turn(
    tmp_path: Path,
) -> None:
    runtime = _InteractionRuntime(error=RuntimeError("resume failed"))
    store, manager, session_id, interaction_id, response = _interaction_fixture(
        tmp_path,
        runtime,
    )

    with pytest.raises(RuntimeError, match="resume failed"):
        await manager.submit_interaction_response(
            session_id,
            interaction_id,
            response,
        )

    assert store.get_active_turn(session_id) is None

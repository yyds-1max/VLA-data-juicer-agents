from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest
from agentscope.message import HintBlock

from vla_data_juicer_agents.navigation.task_state import NavigationTaskStatus
from vla_data_juicer_agents.navigation.task_entry import NavigationTaskEntryError
from vla_data_juicer_agents.navigation.task_store import (
    NavigationTaskTransitionError,
    SqliteNavigationTaskStore,
)
from vla_data_juicer_agents.runtime.agentscope_config import AgentScopeRuntimeConfig
from vla_data_juicer_agents.runtime.agentscope_runtime import AgentScopeRuntime
from vla_data_juicer_agents.web.session_store import WebSessionStore


def _task_store(tmp_path: Path) -> tuple[SqliteNavigationTaskStore, object]:
    store = SqliteNavigationTaskStore(tmp_path / "navigation.sqlite")
    task = store.create_task_attempt(
        task_id="task-private-1",
        request="处理导航数据",
        target="navigation_data",
        date="20260720",
        segments=["clip-a"],
        scene_mode="out",
        dry_run=False,
        web_session_id="web-1",
        agentscope_session_id="navigation-private-1",
    ).task
    return store, task


def _update_status(
    store: SqliteNavigationTaskStore,
    task: object,
    status: NavigationTaskStatus,
):
    return store.update_task_for_session(
        task.task_id,
        web_session_id=task.created_by_web_session_id,
        agentscope_session_id=task.agentscope_session_id,
        expected_state_revision=task.state_revision,
        status=status,
    )


def test_navigation_task_status_transitions_are_centralized_and_idempotent(
    tmp_path: Path,
) -> None:
    store, task = _task_store(tmp_path)

    waiting = _update_status(store, task, NavigationTaskStatus.WAITING_USER)
    resumed = _update_status(store, waiting, NavigationTaskStatus.ACTIVE)
    pausing = _update_status(store, resumed, NavigationTaskStatus.PAUSING)
    paused = _update_status(store, pausing, NavigationTaskStatus.PAUSED)

    replay = _update_status(store, paused, NavigationTaskStatus.PAUSED)
    assert replay.state_revision == paused.state_revision

    with pytest.raises(NavigationTaskTransitionError):
        _update_status(store, paused, NavigationTaskStatus.COMPLETED)


def test_terminal_navigation_task_cannot_be_reactivated(tmp_path: Path) -> None:
    store, task = _task_store(tmp_path)
    completed = _update_status(store, task, NavigationTaskStatus.COMPLETED)

    with pytest.raises(NavigationTaskTransitionError):
        _update_status(store, completed, NavigationTaskStatus.ACTIVE)


def test_request_context_is_consumed_by_first_turn_and_never_public(
    tmp_path: Path,
) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    session = store.create_session("快捷入口")
    context = {
        "kind": "navigation_dataset_selection_v1",
        "dataset_date": "20260720",
        "selection": {"kind": "selected_clips", "clips": ["clip-a", "clip-b"]},
    }
    store.save_pending_request_context(session.id, context)

    submission = store.begin_user_turn(session.id, "处理选中的导航数据")

    assert session.contract_version == 1
    assert store.get_turn_user_message(submission.turn.id) == "处理选中的导航数据"
    assert store.get_turn_request_context(submission.turn.id) == context
    detail = store.get_session(session.id)
    assert detail is not None
    serialized = detail.model_dump_json()
    assert "navigation_dataset_selection_v1" not in serialized
    assert "clip-b" not in serialized


def test_request_context_is_idempotent_and_cannot_be_replaced(tmp_path: Path) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    session = store.create_session("快捷入口")
    context = {
        "kind": "navigation_dataset_selection_v1",
        "dataset_date": "20260720",
        "selection": {"kind": "all_clips"},
    }
    store.save_pending_request_context(session.id, context)
    store.save_pending_request_context(session.id, context)

    first = store.begin_user_turn(
        session.id,
        "处理数据",
        invocation_id="request-1",
    )
    replay = store.begin_user_turn(
        session.id,
        "处理数据",
        invocation_id="request-1",
    )

    assert replay.created is False
    assert replay.turn.id == first.turn.id
    assert store.get_turn_request_context(replay.turn.id) == context
    with pytest.raises(RuntimeError, match="before the first turn"):
        store.save_pending_request_context(
            session.id,
            {**context, "dataset_date": "20260721"},
        )


def test_session_store_no_longer_creates_contract_v0_sessions(tmp_path: Path) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")

    assert store.create_session("默认").contract_version == 1
    with pytest.raises(ValueError, match="contract_version must be 1"):
        store.create_session("旧契约", contract_version=0)


def _runtime(tmp_path: Path, store: WebSessionStore, task_store: SqliteNavigationTaskStore):
    config = AgentScopeRuntimeConfig(
        user_id="test-user",
        redis_url="redis://localhost:6379/0",
        workspace_root=tmp_path,
        dashscope_api_key="test-key",
        dashscope_base_url=None,
        default_model="test-model",
        router_model="router-model",
        navigation_model="navigation-model",
    )
    runtime = AgentScopeRuntime(
        config=config,
        storage=None,
        message_bus=SimpleNamespace(),
        workspace_manager=None,
        app=SimpleNamespace(state=SimpleNamespace()),
        web_session_store=store,
    )
    runtime._navigation_task_store = lambda: task_store  # type: ignore[method-assign]
    return runtime


class _IdempotentRecordingBus:
    def __init__(self) -> None:
        self.inbox: list[tuple[str, dict]] = []
        self.wakeups: list[tuple[str, str, str]] = []
        self._inbox_tokens: set[str] = set()
        self._wakeup_tokens: set[str] = set()

    async def inbox_push_idempotent(
        self,
        session_id: str,
        payload: dict,
        *,
        idempotency_key: str,
    ) -> bool:
        if idempotency_key in self._inbox_tokens:
            return False
        self._inbox_tokens.add(idempotency_key)
        self.inbox.append((session_id, payload))
        return True

    async def enqueue_wakeup_idempotent(
        self,
        user_id: str,
        session_id: str,
        agent_id: str,
        *,
        idempotency_key: str,
    ) -> bool:
        if idempotency_key in self._wakeup_tokens:
            return False
        self._wakeup_tokens.add(idempotency_key)
        self.wakeups.append((user_id, session_id, agent_id))
        return True

    def simulate_full_redis_loss(self) -> None:
        self.inbox.clear()
        self.wakeups.clear()
        self._inbox_tokens.clear()
        self._wakeup_tokens.clear()


def test_private_background_lifecycle_closes_user_turn_without_safe_failure(
    tmp_path: Path,
) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    task_store = SqliteNavigationTaskStore(tmp_path / "navigation.sqlite")
    session = store.create_session("后台任务")
    turn = store.begin_user_turn(session.id, "处理导航数据").turn
    binding = store.create_task_binding(
        session.id,
        task_id="task-private-1",
        task_ref="DP-PUBLIC1",
        navigation_session_id="navigation-private-1",
    ).binding
    task_store.create_task_attempt(
        task_id=binding.task_id,
        request="处理导航数据",
        target="navigation_data",
        date="20260720",
        segments=["clip-a"],
        scene_mode="out",
        dry_run=False,
        web_session_id=session.id,
        agentscope_session_id=binding.navigation_session_id,
    )
    store.bind_conversation_agent_session_to_turn(binding.navigation_session_id, turn.id)
    authority = store.get_response_authority(turn.id)
    assert authority is not None
    store.handover_response_authority(
        turn.id,
        expected_producer="router",
        expected_generation=authority.generation,
        new_producer="navigation",
    )
    store.append_projected_event_batch(
        web_session_id=session.id,
        agentscope_session_id=binding.navigation_session_id,
        entry_id="1-0",
        events=[],
        private_events=[],
        raw_event_type="REPLY_START",
        reply_id="reply-1",
    )
    store.append_projected_event_batch(
        web_session_id=session.id,
        agentscope_session_id=binding.navigation_session_id,
        entry_id="2-0",
        events=[],
        private_events=[
            {
                "type": "tool_start",
                "payload": {
                    "tool": "extract_and_sync_navigation_data_tool",
                    "call_id": "call-private-1",
                },
            },
            {
                "type": "tool_background",
                "payload": {
                    "tool": "extract_and_sync_navigation_data_tool",
                    "call_id": "call-private-1",
                },
            },
        ],
        raw_event_type="TOOL_RESULT",
        reply_id="reply-1",
    )
    records = store.append_projected_event_batch(
        web_session_id=session.id,
        agentscope_session_id=binding.navigation_session_id,
        entry_id="3-0",
        events=[
            {
                "contract_version": 1,
                "type": "task_state_updated",
                "payload": {"task_ref": binding.task_ref, "status": "active"},
            }
        ],
        private_events=[],
        raw_event_type="REPLY_END",
        reply_id="reply-1",
    )

    assert [record.type for record in records][-2:] == ["final", "turn_state"]
    detail = store.get_session(session.id)
    assert detail is not None
    assert detail.turns[0].status == "completed"
    assert detail.messages[-1].role == "assistant"
    assert "后台" in detail.messages[-1].content
    assert "未能生成可安全展示的回复" not in detail.messages[-1].content
    assert "extract_and_sync_navigation_data_tool" not in detail.model_dump_json()


def test_open_active_task_without_tool_event_never_emits_safe_failure(
    tmp_path: Path,
) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    task_store = SqliteNavigationTaskStore(tmp_path / "navigation.sqlite")
    session = store.create_session("开放任务")
    turn = store.begin_user_turn(session.id, "处理导航数据").turn
    binding = store.create_task_binding(
        session.id,
        task_id="task-private-open",
        task_ref="DP-PUBLIC-OPEN",
        navigation_session_id="navigation-private-open",
    ).binding
    task_store.create_task_attempt(
        task_id=binding.task_id,
        request="处理导航数据",
        target="navigation_data",
        date="20260720",
        segments=["clip-a"],
        scene_mode="out",
        dry_run=False,
        web_session_id=session.id,
        agentscope_session_id=binding.navigation_session_id,
    )
    store.bind_conversation_agent_session_to_turn(binding.navigation_session_id, turn.id)
    authority = store.get_response_authority(turn.id)
    assert authority is not None
    store.handover_response_authority(
        turn.id,
        expected_producer="router",
        expected_generation=authority.generation,
        new_producer="navigation",
    )
    store.append_projected_event_batch(
        web_session_id=session.id,
        agentscope_session_id=binding.navigation_session_id,
        entry_id="open-1",
        events=[],
        private_events=[],
        raw_event_type="REPLY_START",
        reply_id="reply-open",
    )
    store.append_projected_event_batch(
        web_session_id=session.id,
        agentscope_session_id=binding.navigation_session_id,
        entry_id="open-2",
        events=[
            {
                "contract_version": 1,
                "type": "task_state_updated",
                "payload": {"task_ref": binding.task_ref, "status": "active"},
            }
        ],
        private_events=[],
        raw_event_type="REPLY_END",
        reply_id="reply-open",
    )

    detail = store.get_session(session.id)
    assert detail is not None
    assert detail.turns[0].status == "completed"
    assert "未能生成可安全展示的回复" not in detail.messages[-1].content
    assert "后台" not in detail.messages[-1].content
    assert "仍为处理中" in detail.messages[-1].content


def test_navigation_await_user_disposition_durably_yields_active_task(
    tmp_path: Path,
) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    task_store = SqliteNavigationTaskStore(tmp_path / "navigation.sqlite")
    session = store.create_session("等待补充信息")
    turn = store.begin_user_turn(session.id, "从已有产物继续处理").turn
    binding = store.create_task_binding(
        session.id,
        task_id="task-private-await-user",
        task_ref="DP-AWAIT-USER",
        navigation_session_id="navigation-private-await-user",
    ).binding
    task_store.create_task_attempt(
        task_id=binding.task_id,
        request="从已有产物继续处理",
        target="navigation_data",
        date="20260720",
        segments=["clip-a"],
        scene_mode=None,
        dry_run=False,
        web_session_id=session.id,
        agentscope_session_id=binding.navigation_session_id,
    )
    store.bind_conversation_agent_session_to_turn(binding.navigation_session_id, turn.id)
    authority = store.get_response_authority(turn.id)
    assert authority is not None
    store.handover_response_authority(
        turn.id,
        expected_producer="router",
        expected_generation=authority.generation,
        new_producer="navigation",
    )
    runtime = _runtime(tmp_path, store, task_store)

    projected = runtime.project_contract_v1_event_batch(
        web_session_id=session.id,
        agentscope_session_id=binding.navigation_session_id,
        entry_id="await-user-1",
        events=(
            {
                "type": "turn_disposition",
                "payload": {
                    "version": 1,
                    "kind": "await_user",
                    "purpose": "collect_finish_processing_inputs",
                    "requested_fields": ["scene_mode"],
                    "response_channel": "router_text",
                    "public_prompt": "继续处理前，请告诉我这是室内还是室外数据。",
                    "_public_prefix": (
                        "已核对已有产物，拆解同步已经完成。\n"
                        "继续处理前，请告诉我这是室内还是室外数据。"
                    ),
                },
            },
        ),
    )

    task = task_store.get_task(binding.task_id)
    current_binding = store.get_task_binding(binding.task_id)
    assert task is not None
    assert task.status == NavigationTaskStatus.WAITING_USER
    assert current_binding is not None
    assert current_binding.status == "waiting_user"
    assert current_binding.latest_public_update == "等待你补充场景模式。"
    wait_record = current_binding.scope["_runtime_await_user"]
    assert wait_record["disposition_id"] == "await-user-1:0"
    assert wait_record["requested_fields"] == ["scene_mode"]
    assert [event["type"] for event in projected] == [
        "task_state_updated",
        "final",
    ]
    assert projected[0]["payload"]["status"] == "waiting_user"
    assert projected[1]["payload"] == {
        "text": (
            "已核对已有产物，拆解同步已经完成。\n"
            "继续处理前，请告诉我这是室内还是室外数据。"
        ),
        "task_status": "waiting_user",
        "task_ref": binding.task_ref,
    }
    assert "turn_disposition" not in json.dumps(projected, ensure_ascii=False)
    assert "_runtime_await_user" not in json.dumps(
        runtime.session_task_snapshots(session.id),
        ensure_ascii=False,
    )


def test_await_user_projection_rolls_back_for_durable_event_replay(
    tmp_path: Path,
) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    task_store = SqliteNavigationTaskStore(tmp_path / "navigation.sqlite")
    session = store.create_session("等待事件重放")
    turn = store.begin_user_turn(session.id, "检查并继续").turn
    binding = store.create_task_binding(
        session.id,
        task_id="task-await-replay",
        task_ref="DP-AWAIT-REPLAY",
        navigation_session_id="navigation-await-replay",
    ).binding
    task_store.create_task_attempt(
        task_id=binding.task_id,
        request="检查并继续",
        target="navigation_data",
        date="20260720",
        segments=["clip-a"],
        scene_mode=None,
        dry_run=False,
        web_session_id=session.id,
        agentscope_session_id=binding.navigation_session_id,
    )
    store.bind_conversation_agent_session_to_turn(binding.navigation_session_id, turn.id)
    authority = store.get_response_authority(turn.id)
    assert authority is not None
    store.handover_response_authority(
        turn.id,
        expected_producer="router",
        expected_generation=authority.generation,
        new_producer="navigation",
    )
    runtime = _runtime(tmp_path, store, task_store)
    active = task_store.get_task(binding.task_id)
    assert active is not None
    _update_status(task_store, active, NavigationTaskStatus.WAITING_USER)
    assert store.get_task_binding(binding.task_id).status == "active"
    private_event = {
        "type": "turn_disposition",
        "payload": {
            "version": 1,
            "kind": "await_user",
            "purpose": "collect_finish_processing_inputs",
            "requested_fields": ["scene_mode"],
            "response_channel": "router_text",
            "public_prompt": "继续处理前，请告诉我这是室内还是室外数据。",
        },
    }

    first = runtime.project_contract_v1_event_batch(
        web_session_id=session.id,
        agentscope_session_id=binding.navigation_session_id,
        entry_id="await-replay-entry",
        events=(private_event,),
        transactional=True,
    )
    assert store.get_task_binding(binding.task_id).status == "waiting_user"
    runtime.rollback_contract_v1_projection_batch(
        agentscope_session_id=binding.navigation_session_id,
        entry_id="await-replay-entry",
    )
    replay = runtime.project_contract_v1_event_batch(
        web_session_id=session.id,
        agentscope_session_id=binding.navigation_session_id,
        entry_id="await-replay-entry",
        events=(private_event,),
        transactional=True,
    )
    runtime.commit_contract_v1_projection_batch(
        agentscope_session_id=binding.navigation_session_id,
        entry_id="await-replay-entry",
    )

    assert [event for event in first if event["type"] == "final"] == [
        event for event in replay if event["type"] == "final"
    ]
    assert replay[-1]["payload"]["text"] == private_event["payload"]["public_prompt"]
    assert runtime._projection_checkpoints == {}


def test_new_await_user_cannot_overwrite_existing_pending_request(
    tmp_path: Path,
) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    task_store = SqliteNavigationTaskStore(tmp_path / "navigation.sqlite")
    session = store.create_session("禁止覆盖等待请求")
    turn = store.begin_user_turn(session.id, "检查并继续").turn
    binding = store.create_task_binding(
        session.id,
        task_id="task-await-no-overwrite",
        task_ref="DP-AWAIT-NO-OVERWRITE",
        navigation_session_id="navigation-await-no-overwrite",
    ).binding
    task_store.create_task_attempt(
        task_id=binding.task_id,
        request="检查并继续",
        target="navigation_data",
        date="20260720",
        segments=["clip-a"],
        scene_mode=None,
        dry_run=False,
        web_session_id=session.id,
        agentscope_session_id=binding.navigation_session_id,
    )
    store.bind_conversation_agent_session_to_turn(binding.navigation_session_id, turn.id)
    authority = store.get_response_authority(turn.id)
    assert authority is not None
    store.handover_response_authority(
        turn.id,
        expected_producer="router",
        expected_generation=authority.generation,
        new_producer="navigation",
    )
    runtime = _runtime(tmp_path, store, task_store)
    base_payload = {
        "version": 1,
        "kind": "await_user",
        "purpose": "collect_finish_processing_inputs",
        "requested_fields": ["scene_mode"],
        "response_channel": "router_text",
        "public_prompt": "请补充场景模式。",
    }
    runtime.project_contract_v1_event_batch(
        web_session_id=session.id,
        agentscope_session_id=binding.navigation_session_id,
        entry_id="await-original",
        events=({"type": "turn_disposition", "payload": base_payload},),
    )

    with pytest.raises(RuntimeError, match="different pending request"):
        runtime.project_contract_v1_event_batch(
            web_session_id=session.id,
            agentscope_session_id=binding.navigation_session_id,
            entry_id="await-late",
            events=(
                {
                    "type": "turn_disposition",
                    "payload": {
                        **base_payload,
                        "purpose": "task_clarification",
                        "requested_fields": ["task_guidance"],
                        "public_prompt": "请改为补充任务要求。",
                    },
                },
            ),
        )

    current_binding = store.get_task_binding(binding.task_id)
    assert current_binding is not None
    assert current_binding.scope["_runtime_await_user"]["public_prompt"] == "请补充场景模式。"


def test_invalid_await_user_moves_active_task_to_recoverable_state(
    tmp_path: Path,
) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    task_store = SqliteNavigationTaskStore(tmp_path / "navigation.sqlite")
    session = store.create_session("错误等待契约")
    turn = store.begin_user_turn(session.id, "检查并继续").turn
    binding = store.create_task_binding(
        session.id,
        task_id="task-invalid-await",
        task_ref="DP-INVALID-AWAIT",
        navigation_session_id="navigation-invalid-await",
    ).binding
    task_store.create_task_attempt(
        task_id=binding.task_id,
        request="检查并继续",
        target="navigation_data",
        date="20260720",
        segments=["clip-a"],
        scene_mode=None,
        dry_run=False,
        web_session_id=session.id,
        agentscope_session_id=binding.navigation_session_id,
    )
    store.bind_conversation_agent_session_to_turn(binding.navigation_session_id, turn.id)
    authority = store.get_response_authority(turn.id)
    assert authority is not None
    store.handover_response_authority(
        turn.id,
        expected_producer="router",
        expected_generation=authority.generation,
        new_producer="navigation",
    )
    runtime = _runtime(tmp_path, store, task_store)

    projected = runtime.project_contract_v1_event_batch(
        web_session_id=session.id,
        agentscope_session_id=binding.navigation_session_id,
        entry_id="invalid-await-entry",
        events=({"type": "invalid_turn_disposition", "payload": {}},),
        transactional=True,
    )
    runtime.rollback_contract_v1_projection_batch(
        agentscope_session_id=binding.navigation_session_id,
        entry_id="invalid-await-entry",
    )
    replay = runtime.project_contract_v1_event_batch(
        web_session_id=session.id,
        agentscope_session_id=binding.navigation_session_id,
        entry_id="invalid-await-entry",
        events=({"type": "invalid_turn_disposition", "payload": {}},),
        transactional=True,
    )
    runtime.commit_contract_v1_projection_batch(
        agentscope_session_id=binding.navigation_session_id,
        entry_id="invalid-await-entry",
    )

    task = task_store.get_task(binding.task_id)
    current_binding = store.get_task_binding(binding.task_id)
    assert task is not None and task.status == NavigationTaskStatus.NEEDS_REPLAN
    assert current_binding is not None and current_binding.status == "needs_replan"
    assert [event["type"] for event in projected] == ["task_state_updated", "final"]
    assert replay == projected
    assert "AwaitUser" not in json.dumps(projected, ensure_ascii=False)


@pytest.mark.asyncio
async def test_waiting_navigation_receives_exact_next_user_message(
    tmp_path: Path,
) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    task_store = SqliteNavigationTaskStore(tmp_path / "navigation.sqlite")
    session = store.create_session("精确传递补充信息")
    first_turn = store.begin_user_turn(session.id, "检查已有产物并继续").turn
    binding = store.create_task_binding(
        session.id,
        task_id="task-private-exact-supplement",
        task_ref="DP-EXACT-SUPPLEMENT",
        navigation_session_id="navigation-private-exact-supplement",
    ).binding
    task_store.create_task_attempt(
        task_id=binding.task_id,
        request="检查已有产物并继续",
        target="navigation_data",
        date="20260720",
        segments=["clip-a"],
        scene_mode=None,
        dry_run=False,
        web_session_id=session.id,
        agentscope_session_id=binding.navigation_session_id,
    )
    store.bind_conversation_agent_session_to_turn(
        binding.navigation_session_id,
        first_turn.id,
    )
    first_authority = store.get_response_authority(first_turn.id)
    assert first_authority is not None
    store.handover_response_authority(
        first_turn.id,
        expected_producer="router",
        expected_generation=first_authority.generation,
        new_producer="navigation",
    )
    store.append_projected_event_batch(
        web_session_id=session.id,
        agentscope_session_id=binding.navigation_session_id,
        entry_id="exact-supplement-start",
        events=[],
        private_events=[],
        raw_event_type="REPLY_START",
        reply_id="reply-exact-supplement-wait",
    )
    runtime = _runtime(tmp_path, store, task_store)
    projected = runtime.project_contract_v1_event_batch(
        web_session_id=session.id,
        agentscope_session_id=binding.navigation_session_id,
        entry_id="exact-supplement-wait",
        events=(
            {
                "type": "turn_disposition",
                "payload": {
                    "version": 1,
                    "kind": "await_user",
                    "purpose": "collect_finish_processing_inputs",
                    "requested_fields": ["scene_mode"],
                    "response_channel": "router_text",
                    "public_prompt": "请告诉我这是室内还是室外数据。",
                },
            },
        ),
    )
    store.append_projected_event_batch(
        web_session_id=session.id,
        agentscope_session_id=binding.navigation_session_id,
        entry_id="exact-supplement-wait",
        events=list(projected),
        private_events=[],
        raw_event_type="REPLY_END",
        reply_id="reply-exact-supplement-wait",
    )
    assert store.append_projected_event_batch(
        web_session_id=session.id,
        agentscope_session_id=binding.navigation_session_id,
        entry_id="exact-supplement-wait",
        events=list(projected),
        private_events=[],
        raw_event_type="REPLY_END",
        reply_id="reply-exact-supplement-wait",
    ) == []

    exact_message = "是的，继续。室内数据；标定参数先按推荐值。"
    store.begin_user_turn(session.id, exact_message)
    runtime.router_context_envelope(
        session.id,
        router_session_id="router-private-exact-supplement",
    )
    captured: dict[str, str] = {}

    async def start_agent_run(**kwargs):
        captured["message"] = kwargs["message"]
        captured["agentscope_session_id"] = kwargs["agentscope_session_id"]

    async def publish_task_state(**_kwargs):
        return None

    runtime._start_agent_run = start_agent_run  # type: ignore[method-assign]
    runtime._publish_v1_task_state = publish_task_state  # type: ignore[method-assign]

    result = await runtime.continue_navigation_agent_task_v1(
        web_session_id=session.id,
        router_session_id="router-private-exact-supplement",
    )

    assert result["ok"] is True
    assert captured["message"] == exact_message
    assert captured["agentscope_session_id"] == binding.navigation_session_id
    current = task_store.get_task(binding.task_id)
    assert current is not None and current.status == NavigationTaskStatus.ACTIVE
    resumed_binding = store.get_task_binding(binding.task_id)
    assert resumed_binding is not None
    assert "_runtime_await_user" not in resumed_binding.scope


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_layer", ["task", "binding"])
async def test_continue_does_not_spawn_until_both_stores_are_active(
    tmp_path: Path,
    failure_layer: str,
) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    task_store = SqliteNavigationTaskStore(tmp_path / "navigation.sqlite")
    session = store.create_session("继续半提交恢复")
    binding = store.create_task_binding(
        session.id,
        task_id="task-continue-reconcile",
        task_ref="DP-CONTINUE-RECONCILE",
        navigation_session_id="navigation-continue-reconcile",
        scope={"_runtime_await_user": {"disposition_id": "old-wait:0"}},
    ).binding
    active = task_store.create_task_attempt(
        task_id=binding.task_id,
        request="检查并继续",
        target="navigation_data",
        date="20260720",
        segments=["clip-a"],
        scene_mode=None,
        dry_run=False,
        web_session_id=session.id,
        agentscope_session_id=binding.navigation_session_id,
    ).task
    _update_status(task_store, active, NavigationTaskStatus.WAITING_USER)
    binding = store.update_task_binding(
        binding.task_id,
        expected_revision=binding.state_revision,
        status="waiting_user",
    )
    store.begin_user_turn(session.id, "继续，室内").turn
    runtime = _runtime(tmp_path, store, task_store)
    runtime.router_context_envelope(
        session.id,
        router_session_id="router-continue-reconcile",
    )
    spawned = False

    async def start_agent_run(**_kwargs):
        nonlocal spawned
        spawned = True

    async def publish_task_state(**_kwargs):
        return None

    runtime._start_agent_run = start_agent_run  # type: ignore[method-assign]
    runtime._publish_v1_task_state = publish_task_state  # type: ignore[method-assign]
    real_update_binding = store.update_task_binding
    real_update_task = task_store.update_task_for_session
    failed_once = False

    def fail_first_active_binding(*args, **kwargs):
        nonlocal failed_once
        if kwargs.get("status") == "active" and not failed_once:
            failed_once = True
            raise RuntimeError("injected binding write failure")
        return real_update_binding(*args, **kwargs)

    store.update_task_binding = fail_first_active_binding  # type: ignore[method-assign]

    def fail_first_active_task(*args, **kwargs):
        nonlocal failed_once
        if kwargs.get("status") == NavigationTaskStatus.ACTIVE and not failed_once:
            failed_once = True
            raise RuntimeError("injected task write failure")
        return real_update_task(*args, **kwargs)

    if failure_layer == "task":
        store.update_task_binding = real_update_binding  # type: ignore[method-assign]
        task_store.update_task_for_session = fail_first_active_task  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match=f"injected {failure_layer} write failure"):
        await runtime.continue_navigation_agent_task_v1(
            web_session_id=session.id,
            router_session_id="router-continue-reconcile",
        )

    assert spawned is False and failed_once is True
    current = task_store.get_task(binding.task_id)
    expected_status = (
        NavigationTaskStatus.WAITING_USER
        if failure_layer == "task"
        else NavigationTaskStatus.NEEDS_REPLAN
    )
    assert current is not None and current.status == expected_status
    current_binding = store.get_task_binding(binding.task_id)
    assert current_binding is not None and current_binding.status == expected_status.value
    with sqlite3.connect(store.db_path) as connection:
        outbox = connection.execute(
            "SELECT status FROM runtime_outbox WHERE kind = 'navigation_continue'"
        ).fetchone()
    assert outbox is not None and outbox[0] == "failed"


def test_stale_reply_cannot_move_resumed_task_back_to_waiting(tmp_path: Path) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    task_store = SqliteNavigationTaskStore(tmp_path / "navigation.sqlite")
    session = store.create_session("迟到回复隔离")
    first_turn = store.begin_user_turn(session.id, "检查数据").turn
    binding = store.create_task_binding(
        session.id,
        task_id="task-stale-reply",
        task_ref="DP-STALE-REPLY",
        navigation_session_id="navigation-stale-reply",
    ).binding
    task_store.create_task_attempt(
        task_id=binding.task_id,
        request="检查数据",
        target="navigation_data",
        date="20260720",
        segments=["clip-a"],
        scene_mode=None,
        dry_run=False,
        web_session_id=session.id,
        agentscope_session_id=binding.navigation_session_id,
    )
    store.bind_conversation_agent_session_to_turn(
        binding.navigation_session_id,
        first_turn.id,
    )
    authority = store.get_response_authority(first_turn.id)
    assert authority is not None
    authority = store.handover_response_authority(
        first_turn.id,
        expected_producer="router",
        expected_generation=authority.generation,
        new_producer="navigation",
    )
    store.append_projected_event_batch(
        web_session_id=session.id,
        agentscope_session_id=binding.navigation_session_id,
        entry_id="old-reply-start",
        events=[],
        raw_event_type="REPLY_START",
        reply_id="old-reply",
    )
    store.commit_authorized_final(
        first_turn.id,
        producer="navigation",
        response_generation=authority.generation,
        text="请补充信息。",
    )
    runtime = _runtime(tmp_path, store, task_store)
    late_payload = {
        "version": 1,
        "kind": "await_user",
        "purpose": "collect_finish_processing_inputs",
        "requested_fields": ["scene_mode"],
        "response_channel": "router_text",
        "public_prompt": "这条旧问题不应生效。",
    }
    assert runtime.project_contract_v1_event_batch(
        web_session_id=session.id,
        agentscope_session_id=binding.navigation_session_id,
        entry_id="late-old-reply-without-active-turn",
        reply_id="old-reply",
        events=({"type": "turn_disposition", "payload": late_payload},),
    ) == ()
    current = task_store.get_task(binding.task_id)
    assert current is not None and current.status == NavigationTaskStatus.ACTIVE

    second_turn = store.begin_user_turn(session.id, "继续，室内").turn
    store.bind_conversation_agent_session_to_turn(
        binding.navigation_session_id,
        second_turn.id,
    )
    second_authority = store.get_response_authority(second_turn.id)
    assert second_authority is not None
    store.handover_response_authority(
        second_turn.id,
        expected_producer="router",
        expected_generation=second_authority.generation,
        new_producer="navigation",
    )
    projected = runtime.project_contract_v1_event_batch(
        web_session_id=session.id,
        agentscope_session_id=binding.navigation_session_id,
        entry_id="late-old-reply-end",
        reply_id="old-reply",
        events=(
            {
                "type": "turn_disposition",
                "payload": late_payload,
            },
        ),
    )

    current = task_store.get_task(binding.task_id)
    assert projected == ()
    assert current is not None and current.status == NavigationTaskStatus.ACTIVE


def test_second_await_user_repairs_half_commit_with_old_wait_identity(
    tmp_path: Path,
) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    task_store = SqliteNavigationTaskStore(tmp_path / "navigation.sqlite")
    session = store.create_session("第二次等待重放")
    turn = store.begin_user_turn(session.id, "继续处理").turn
    binding = store.create_task_binding(
        session.id,
        task_id="task-second-wait",
        task_ref="DP-SECOND-WAIT",
        navigation_session_id="navigation-second-wait",
        scope={"_runtime_await_user": {"disposition_id": "old:0"}},
    ).binding
    task = task_store.create_task_attempt(
        task_id=binding.task_id,
        request="继续处理",
        target="navigation_data",
        date="20260720",
        segments=["clip-a"],
        scene_mode=None,
        dry_run=False,
        web_session_id=session.id,
        agentscope_session_id=binding.navigation_session_id,
    ).task
    waiting = _update_status(task_store, task, NavigationTaskStatus.WAITING_USER)
    store.bind_conversation_agent_session_to_turn(binding.navigation_session_id, turn.id)
    authority = store.get_response_authority(turn.id)
    assert authority is not None
    store.handover_response_authority(
        turn.id,
        expected_producer="router",
        expected_generation=authority.generation,
        new_producer="navigation",
    )
    runtime = _runtime(tmp_path, store, task_store)

    projected = runtime.project_contract_v1_event_batch(
        web_session_id=session.id,
        agentscope_session_id=binding.navigation_session_id,
        entry_id="second-wait",
        events=(
            {
                "type": "turn_disposition",
                "payload": {
                    "version": 1,
                    "kind": "await_user",
                    "purpose": "collect_finish_processing_inputs",
                    "requested_fields": ["scene_mode"],
                    "response_channel": "router_text",
                    "public_prompt": "请补充新的场景模式。",
                },
            },
        ),
    )

    assert waiting.status == NavigationTaskStatus.WAITING_USER
    assert projected[-1]["type"] == "final"
    repaired = store.get_task_binding(binding.task_id)
    assert repaired is not None and repaired.status == "waiting_user"
    assert repaired.scope["_runtime_await_user"]["disposition_id"] == "second-wait:0"


def test_progress_update_uses_current_tool_phase(tmp_path: Path) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    task_store = SqliteNavigationTaskStore(tmp_path / "navigation.sqlite")
    session = store.create_session("进度阶段")
    turn = store.begin_user_turn(session.id, "处理导航数据").turn
    binding = store.create_task_binding(
        session.id,
        task_id="task-private-1",
        task_ref="DP-PUBLIC1",
        navigation_session_id="navigation-private-1",
    ).binding
    task_store.create_task_attempt(
        task_id=binding.task_id,
        request="处理导航数据",
        target="navigation_data",
        date="20260720",
        segments=["clip-a"],
        scene_mode="out",
        dry_run=False,
        web_session_id=session.id,
        agentscope_session_id=binding.navigation_session_id,
    )
    store.bind_conversation_agent_session_to_turn(binding.navigation_session_id, turn.id)
    authority = store.get_response_authority(turn.id)
    assert authority is not None
    store.handover_response_authority(
        turn.id,
        expected_producer="router",
        expected_generation=authority.generation,
        new_producer="navigation",
    )
    runtime = _runtime(tmp_path, store, task_store)

    projected = runtime.project_contract_v1_event_batch(
        web_session_id=session.id,
        agentscope_session_id=binding.navigation_session_id,
        entry_id="phase-1",
        events=(
            {
                "type": "tool_start",
                "payload": {
                    "tool": "extract_and_sync_navigation_data_tool",
                    "call_id": "call-1",
                },
            },
            {"type": "progress_update", "payload": {"text": "正在核对同步结果。"}},
        ),
    )

    assert [event["type"] for event in projected] == ["action_start", "progress_start"]
    assert projected[0]["payload"]["phase"] == "extract_sync"
    assert projected[1]["payload"]["phase"] == "extract_sync"
    assert projected[1]["payload"]["summary"] == "正在核对同步结果。"


def test_tool_background_closes_public_action_as_background(tmp_path: Path) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    task_store = SqliteNavigationTaskStore(tmp_path / "navigation.sqlite")
    session = store.create_session("后台动作")
    turn = store.begin_user_turn(session.id, "处理导航数据").turn
    binding = store.create_task_binding(
        session.id,
        task_id="task-private-background-action",
        task_ref="DP-BACKGROUND-ACTION",
        navigation_session_id="navigation-private-background-action",
    ).binding
    task_store.create_task_attempt(
        task_id=binding.task_id,
        request="处理导航数据",
        target="navigation_data",
        date="20260720",
        segments=["clip-a"],
        scene_mode="out",
        dry_run=False,
        web_session_id=session.id,
        agentscope_session_id=binding.navigation_session_id,
    )
    store.bind_conversation_agent_session_to_turn(binding.navigation_session_id, turn.id)
    authority = store.get_response_authority(turn.id)
    assert authority is not None
    store.handover_response_authority(
        turn.id,
        expected_producer="router",
        expected_generation=authority.generation,
        new_producer="navigation",
    )
    runtime = _runtime(tmp_path, store, task_store)

    projected = runtime.project_contract_v1_event_batch(
        web_session_id=session.id,
        agentscope_session_id=binding.navigation_session_id,
        entry_id="background-action-1",
        events=(
            {
                "type": "tool_start",
                "payload": {
                    "tool": "extract_and_sync_navigation_data_tool",
                    "call_id": "call-private-background",
                },
            },
            {
                "type": "tool_background",
                "payload": {
                    "tool": "extract_and_sync_navigation_data_tool",
                    "call_id": "call-private-background",
                    "status": "background",
                },
            },
        ),
    )

    assert [event["type"] for event in projected] == ["action_start", "action_end"]
    assert projected[1]["payload"]["status"] == "background"
    assert "tool" not in json.dumps(projected, ensure_ascii=False)


def test_progress_update_before_tool_start_uses_forward_action_phase(tmp_path: Path) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    task_store = SqliteNavigationTaskStore(tmp_path / "navigation.sqlite")
    session = store.create_session("进度阶段前瞻")
    turn = store.begin_user_turn(session.id, "处理导航数据").turn
    binding = store.create_task_binding(
        session.id,
        task_id="task-private-lookahead",
        task_ref="DP-LOOKAHEAD",
        navigation_session_id="navigation-private-lookahead",
    ).binding
    task_store.create_task_attempt(
        task_id=binding.task_id,
        request="处理导航数据",
        target="navigation_data",
        date="20260720",
        segments=["clip-a"],
        scene_mode="out",
        dry_run=False,
        web_session_id=session.id,
        agentscope_session_id=binding.navigation_session_id,
    )
    store.bind_conversation_agent_session_to_turn(binding.navigation_session_id, turn.id)
    authority = store.get_response_authority(turn.id)
    assert authority is not None
    store.handover_response_authority(
        turn.id,
        expected_producer="router",
        expected_generation=authority.generation,
        new_producer="navigation",
    )
    runtime = _runtime(tmp_path, store, task_store)

    projected = runtime.project_contract_v1_event_batch(
        web_session_id=session.id,
        agentscope_session_id=binding.navigation_session_id,
        entry_id="phase-lookahead",
        events=(
            {"type": "progress_update", "payload": {"text": "准备提取导航数据。"}},
            {
                "type": "tool_start",
                "payload": {
                    "tool": "extract_and_sync_navigation_data_tool",
                    "call_id": "call-lookahead",
                },
            },
        ),
    )

    assert projected[0]["type"] == "progress_start"
    assert projected[0]["payload"]["phase"] == "extract_sync"


def _install_start_stubs(runtime: AgentScopeRuntime, task_store: SqliteNavigationTaskStore) -> None:
    runtime._navigation_services = lambda: SimpleNamespace(task_store=task_store)  # type: ignore[method-assign]

    async def ensure_session(*_args, **kwargs):
        return kwargs.get("preallocated_session_id")

    async def start_run(**_kwargs):
        return None

    async def publish_state(**_kwargs):
        return None

    runtime.ensure_web_session = ensure_session  # type: ignore[method-assign]
    runtime._start_agent_run = start_run  # type: ignore[method-assign]
    runtime._publish_v1_task_state = publish_state  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_shortcut_envelope_starts_exact_trusted_single_clip_scope(tmp_path: Path) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    task_store = SqliteNavigationTaskStore(tmp_path / "navigation.sqlite")
    session = store.create_session("快捷范围")
    context = {
        "kind": "navigation_dataset_selection_v1",
        "dataset_date": "20260720",
        "selection": {
            "kind": "selected_clips",
            "clips": ["20260605_152856"],
        },
    }
    store.save_pending_request_context(session.id, context)
    user_text = "处理我在数据管理页选中的这一条数据"
    store.begin_user_turn(session.id, user_text)
    runtime = _runtime(tmp_path, store, task_store)
    _install_start_stubs(runtime, task_store)

    envelope = runtime.router_context_envelope(
        session.id,
        router_session_id="router-shortcut",
    )
    assert envelope["request_context"] == context
    result = await runtime.start_navigation_agent_task_v1(
        web_session_id=session.id,
        router_session_id="router-shortcut",
        scope_source="request_context",
        dataset_date=envelope["request_context"]["dataset_date"],
        selection=envelope["request_context"]["selection"],
        scene_mode=None,
        requested_outcome="postprocessing",
    )

    task = task_store.get_task(result["task_id"])
    assert task is not None
    assert task.date == "20260720"
    assert task.segments == ["20260605_152856"]
    assert task.request == user_text
    outcome = task_store.get_task_outcome(task.task_id)
    assert outcome is not None and outcome.requested_outcome == "postprocessing"
    assert result["latest_task"]["dataset_date"] == "20260720"
    assert result["latest_task"]["requested_outcome"] == "postprocessing"
    assert result["latest_task"]["selection"] == context["selection"]
    assert "target" not in result["latest_task"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scope_source", "dataset_date", "selection"),
    [
        ("interpreted_user_text", "20260720", {"kind": "all_clips"}),
        ("request_context", "20260721", {"kind": "all_clips"}),
    ],
)
async def test_shortcut_rejects_reinterpreted_or_mismatched_scope_without_side_effects(
    tmp_path: Path,
    scope_source: str,
    dataset_date: str,
    selection: dict[str, object],
) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    task_store = SqliteNavigationTaskStore(tmp_path / "navigation.sqlite")
    session = store.create_session("拒绝改写范围")
    context = {
        "kind": "navigation_dataset_selection_v1",
        "dataset_date": "20260720",
        "selection": {"kind": "all_clips"},
    }
    store.save_pending_request_context(session.id, context)
    store.begin_user_turn(session.id, "处理选中的导航数据")
    runtime = _runtime(tmp_path, store, task_store)
    _install_start_stubs(runtime, task_store)

    with pytest.raises(NavigationTaskEntryError):
        await runtime.start_navigation_agent_task_v1(
            web_session_id=session.id,
            router_session_id="router-mismatch",
            scope_source=scope_source,
            dataset_date=dataset_date,
            selection=selection,
            scene_mode=None,
        )

    assert store.list_task_bindings(session.id) == []
    with sqlite3.connect(task_store.db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM navigation_tasks").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_plain_text_date_only_starts_all_clips_without_scene_mode(tmp_path: Path) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    task_store = SqliteNavigationTaskStore(tmp_path / "navigation.sqlite")
    session = store.create_session("整日数据")
    store.begin_user_turn(session.id, "处理 20260720 的导航数据")
    runtime = _runtime(tmp_path, store, task_store)
    _install_start_stubs(runtime, task_store)

    result = await runtime.start_navigation_agent_task_v1(
        web_session_id=session.id,
        router_session_id="router-date-only",
        scope_source="interpreted_user_text",
        dataset_date="20260720",
        selection={"kind": "all_clips"},
        scene_mode=None,
    )

    task = task_store.get_task(result["task_id"])
    assert task is not None
    assert task.segments in (None, [])
    assert task.scene_mode is None
    assert result["latest_task"]["selection"] == {"kind": "all_clips"}


@pytest.mark.asyncio
async def test_plain_text_cross_date_clip_is_an_opaque_identifier(tmp_path: Path) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    task_store = SqliteNavigationTaskStore(tmp_path / "navigation.sqlite")
    session = store.create_session("复制数据的原始 clip 名")
    store.begin_user_turn(
        session.id,
        "确认处理 20270605，这一天有 20260605_152856，只处理这个 clip。",
    )
    runtime = _runtime(tmp_path, store, task_store)
    _install_start_stubs(runtime, task_store)

    result = await runtime.start_navigation_agent_task_v1(
        web_session_id=session.id,
        router_session_id="router-cross-date-clip",
        scope_source="interpreted_user_text",
        dataset_date="20270605",
        selection={
            "kind": "selected_clips",
            "clips": ["20260605_152856"],
        },
        scene_mode=None,
    )

    task = task_store.get_task(result["task_id"])
    assert task is not None
    assert task.date == "20270605"
    assert task.segments == ["20260605_152856"]
    assert result["latest_task"]["selection"] == {
        "kind": "selected_clips",
        "clips": ["20260605_152856"],
    }


def _seed_completed_fix_parent(
    tmp_path: Path,
    *,
    requested_outcome: str = "postprocessing_and_fix",
) -> tuple[
    WebSessionStore,
    SqliteNavigationTaskStore,
    object,
    object,
]:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    task_store = SqliteNavigationTaskStore(tmp_path / "navigation.sqlite")
    session = store.create_session("后处理后自动继续 Fix")
    parent_binding_creation = store.create_task_binding(
        session.id,
        task_id="task-explicit-fix-parent",
        task_ref="DP-EXPLICIT-FIX",
        navigation_session_id="navigation-explicit-fix-parent",
    )
    parent_binding = parent_binding_creation.binding
    parent_outbox_worker = "seed-completed-fix-parent"
    store.claim_outbox_item(
        parent_binding_creation.outbox.outbox_id,
        worker_id=parent_outbox_worker,
    )
    store.complete_outbox(
        parent_binding_creation.outbox.outbox_id,
        worker_id=parent_outbox_worker,
    )
    parent = task_store.create_task_attempt(
        task_id=parent_binding.task_id,
        request="完成自动标注、后处理并继续 Fix",
        target="navigation_data",
        date="20270623",
        segments=["20260623_145550"],
        scene_mode="out",
        dry_run=False,
        web_session_id=session.id,
        agentscope_session_id=parent_binding.navigation_session_id,
        requested_outcome=requested_outcome,
    ).task
    parent = task_store.update_task_for_session(
        parent.task_id,
        web_session_id=session.id,
        agentscope_session_id=parent_binding.navigation_session_id,
        expected_state_revision=parent.state_revision,
        accepted_plan_phase="finish_processing",
        status=NavigationTaskStatus.COMPLETED,
    )
    with sqlite3.connect(task_store.db_path) as connection:
        connection.execute(
            """UPDATE navigation_task_outcomes
               SET completion_outcome = 'postprocessing_completed_fix_pending',
                   revision = revision + 1
               WHERE task_id = ?""",
            (parent.task_id,),
        )
    parent_binding = store.mark_task_binding_terminal(
        parent_binding.task_id,
        expected_revision=parent_binding.state_revision,
        status="completed",
        latest_public_update="后处理已完成。",
    )
    turn = store.begin_user_turn(session.id, "后处理已完成").turn
    authority = store.get_response_authority(turn.id)
    assert authority is not None
    store.commit_authorized_final(
        turn.id,
        producer="router",
        response_generation=authority.generation,
        text="后处理已完成。",
    )
    return store, task_store, session, parent


@pytest.mark.asyncio
async def test_completed_postprocessing_continuation_creates_linked_fix_child(
    tmp_path: Path,
) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    task_store = SqliteNavigationTaskStore(tmp_path / "navigation.sqlite")
    session = store.create_session("后处理完成后继续 Fix")
    parent_binding = store.create_task_binding(
        session.id,
        task_id="task-postprocessing-parent",
        task_ref="DP-POSTPROCESSING",
        navigation_session_id="navigation-postprocessing-parent",
    ).binding
    parent = task_store.create_task_attempt(
        task_id=parent_binding.task_id,
        request="完成自动标注和后处理",
        target="navigation_data",
        date="20270623",
        segments=["20260623_145550"],
        scene_mode="out",
        dry_run=False,
        web_session_id=session.id,
        agentscope_session_id=parent_binding.navigation_session_id,
        requested_outcome="postprocessing",
    ).task
    parent = task_store.update_task_for_session(
        parent.task_id,
        web_session_id=session.id,
        agentscope_session_id=parent_binding.navigation_session_id,
        expected_state_revision=parent.state_revision,
        accepted_plan_phase="finish_processing",
        status=NavigationTaskStatus.COMPLETED,
    )
    with sqlite3.connect(task_store.db_path) as connection:
        connection.execute(
            """UPDATE navigation_task_outcomes
               SET completion_outcome = 'postprocessing_completed_fix_pending',
                   revision = revision + 1
               WHERE task_id = ?""",
            (parent.task_id,),
        )
    parent_binding = store.mark_task_binding_terminal(
        parent_binding.task_id,
        expected_revision=parent_binding.state_revision,
        status="completed",
        latest_public_update="后处理已完成。",
    )
    turn = store.begin_user_turn(session.id, "继续 Fix").turn
    runtime = _runtime(tmp_path, store, task_store)
    captured: dict[str, str] = {}

    class _Gateway:
        def begin_linked_fix(self, **kwargs):
            captured["gateway_parent"] = kwargs["parent_navigation_task_id"]
            captured["gateway_child"] = kwargs["child_navigation_task_id"]
            return {"ok": True, "eligible_review_count": 1}

    runtime._navigation_services = lambda: SimpleNamespace(  # type: ignore[method-assign]
        task_store=task_store,
        annotation_gateway=_Gateway(),
    )

    async def ensure_session(*_args, **kwargs):
        return kwargs.get("preallocated_session_id")

    async def start_run(**kwargs):
        captured["message"] = kwargs["message"]
        captured["task_id"] = kwargs["task_id"]

    async def publish_state(**_kwargs):
        return None

    runtime.ensure_web_session = ensure_session  # type: ignore[method-assign]
    runtime._start_agent_run = start_run  # type: ignore[method-assign]
    runtime._publish_v1_task_state = publish_state  # type: ignore[method-assign]
    envelope = runtime.router_context_envelope(
        session.id,
        router_session_id="router-linked-fix",
    )
    assert envelope["focused_task_summary"]["available_actions"] == ["continue_fix"]

    result = await runtime.continue_navigation_agent_task_v1(
        web_session_id=session.id,
        router_session_id="router-linked-fix",
    )

    durable_parent = task_store.get_task(parent.task_id)
    child = task_store.get_task(result["task_id"])
    lineage = task_store.get_task_lineage(result["task_id"])
    assert durable_parent is not None
    assert durable_parent.status == NavigationTaskStatus.COMPLETED
    assert parent_binding.slot_state == "closed"
    assert child is not None
    assert child.status == NavigationTaskStatus.ACTIVE
    assert child.target == "trajectory_review"
    assert lineage is not None and lineage.parent_task_id == parent.task_id
    assert captured["gateway_parent"] == parent.task_id
    assert captured["gateway_child"] == child.task_id
    assert captured["task_id"] == child.task_id
    assert '"requested_outcome": "trajectory_fix"' in captured["message"]
    assert store.get_response_authority(turn.id).producer == "navigation"
    focused = store.get_focused_task_binding(session.id)
    assert focused is not None and focused.task_id == child.task_id


@pytest.mark.asyncio
async def test_explicit_linked_fix_recovery_persists_binding_before_child(
    tmp_path: Path,
) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    task_store = SqliteNavigationTaskStore(tmp_path / "navigation.sqlite")
    session = store.create_session("后处理后自动继续 Fix")
    parent_binding = store.create_task_binding(
        session.id,
        task_id="task-explicit-fix-parent",
        task_ref="DP-EXPLICIT-FIX",
        navigation_session_id="navigation-explicit-fix-parent",
    ).binding
    parent = task_store.create_task_attempt(
        task_id=parent_binding.task_id,
        request="完成自动标注、后处理并继续 Fix",
        target="navigation_data",
        date="20270623",
        segments=["20260623_145550"],
        scene_mode="out",
        dry_run=False,
        web_session_id=session.id,
        agentscope_session_id=parent_binding.navigation_session_id,
        requested_outcome="postprocessing_and_fix",
    ).task
    parent = task_store.update_task_for_session(
        parent.task_id,
        web_session_id=session.id,
        agentscope_session_id=parent_binding.navigation_session_id,
        expected_state_revision=parent.state_revision,
        accepted_plan_phase="finish_processing",
        status=NavigationTaskStatus.COMPLETED,
    )
    with sqlite3.connect(task_store.db_path) as connection:
        connection.execute(
            """UPDATE navigation_task_outcomes
               SET completion_outcome = 'postprocessing_completed_fix_pending',
                   revision = revision + 1
               WHERE task_id = ?""",
            (parent.task_id,),
        )
    store.mark_task_binding_terminal(
        parent_binding.task_id,
        expected_revision=parent_binding.state_revision,
        status="completed",
        latest_public_update="后处理已完成。",
    )

    runtime = _runtime(tmp_path, store, task_store)

    class _FailOnceGateway:
        calls = 0

        def begin_linked_fix(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("simulated annotation gateway crash")
            return {"ok": True, "eligible_review_count": 1}

    gateway = _FailOnceGateway()
    runtime.annotation_gateway = gateway

    async def ensure_session(*_args, **kwargs):
        return kwargs.get("preallocated_session_id")

    async def wake_task(**_kwargs):
        return True

    runtime.ensure_web_session = ensure_session  # type: ignore[method-assign]
    runtime.wake_navigation_task_from_workbench = wake_task  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="simulated annotation gateway crash"):
        await runtime.recover_explicit_linked_fix_tasks_once()

    identity_digest = hashlib.sha256(
        f"trajectory_fix:{parent.task_id}".encode("utf-8")
    ).hexdigest()
    child_task_id = f"nav_fix_{identity_digest[:32]}"
    orphan_safe_binding = store.get_task_binding(child_task_id)
    assert orphan_safe_binding is not None
    assert task_store.get_task(child_task_id) is None
    assert task_store.get_linked_child(parent.task_id) is None

    assert await runtime.recover_explicit_linked_fix_tasks_once() == 1
    child = task_store.get_task(child_task_id)
    assert child is not None
    assert child.target == "trajectory_review"
    assert task_store.get_linked_child(parent.task_id) == child
    assert store.get_task_binding(child_task_id) == orphan_safe_binding
    assert gateway.calls == 2


@pytest.mark.asyncio
async def test_explicit_linked_fix_recovers_after_child_before_create_outbox_complete(
    tmp_path: Path,
) -> None:
    store, task_store, session, parent = _seed_completed_fix_parent(tmp_path)
    runtime = _runtime(tmp_path, store, task_store)

    class _Gateway:
        calls = 0

        def begin_linked_fix(self, **_kwargs):
            self.calls += 1
            return {"ok": True, "eligible_review_count": 1}

    gateway = _Gateway()
    runtime.annotation_gateway = gateway
    wake_calls: list[str] = []

    async def ensure_session(*_args, **kwargs):
        return kwargs.get("preallocated_session_id")

    async def wake_task(**kwargs):
        wake_calls.append(kwargs["task_id"])
        return True

    runtime.ensure_web_session = ensure_session  # type: ignore[method-assign]
    runtime.wake_navigation_task_from_workbench = wake_task  # type: ignore[method-assign]
    real_complete_outbox = store.complete_outbox
    failed_create_completion = False

    def crash_before_create_outbox_completion(outbox_id: str, **kwargs):
        nonlocal failed_create_completion
        outbox = store.get_outbox(outbox_id)
        assert outbox is not None
        if (
            outbox.kind == "navigation_explicit_linked_fix_create"
            and not failed_create_completion
        ):
            failed_create_completion = True
            raise RuntimeError("simulated crash before create outbox completion")
        return real_complete_outbox(outbox_id, **kwargs)

    store.complete_outbox = crash_before_create_outbox_completion  # type: ignore[method-assign]
    with pytest.raises(
        RuntimeError,
        match="simulated crash before create outbox completion",
    ):
        await runtime.recover_explicit_linked_fix_tasks_once()
    store.complete_outbox = real_complete_outbox  # type: ignore[method-assign]

    identity_digest = hashlib.sha256(
        f"trajectory_fix:{parent.task_id}".encode("utf-8")
    ).hexdigest()
    child_task_id = f"nav_fix_{identity_digest[:32]}"
    child = task_store.get_task(child_task_id)
    create_outbox = store.get_outbox_by_idempotency_key(
        f"navigation_auto_linked_fix:{parent.task_id}"
    )
    wake_outbox = store.get_outbox_by_idempotency_key(
        f"navigation_auto_linked_fix_wake:{parent.task_id}"
    )
    assert child is not None
    assert task_store.get_task_lineage(child_task_id) is not None
    assert create_outbox is not None and create_outbox.status == "pending"
    assert wake_outbox is not None and wake_outbox.status == "pending"
    assert wake_outbox.turn_id is None
    assert wake_calls == []

    assert await runtime.recover_explicit_linked_fix_tasks_once() == 1
    assert await runtime.recover_explicit_linked_fix_tasks_once() == 0
    assert gateway.calls == 1
    assert wake_calls == [child_task_id]
    with sqlite3.connect(task_store.db_path) as connection:
        lineage_count = connection.execute(
            """SELECT COUNT(*) FROM navigation_task_lineage
               WHERE parent_task_id = ? AND relation = 'trajectory_fix'""",
            (parent.task_id,),
        ).fetchone()[0]
    assert lineage_count == 1
    durable_parent = task_store.get_task(parent.task_id)
    parent_binding = store.get_task_binding(parent.task_id)
    assert durable_parent is not None
    assert durable_parent.status == NavigationTaskStatus.COMPLETED
    assert parent_binding is not None and parent_binding.slot_state == "closed"


@pytest.mark.asyncio
async def test_explicit_linked_fix_recovers_after_create_outbox_before_turnless_wake(
    tmp_path: Path,
) -> None:
    store, task_store, session, parent = _seed_completed_fix_parent(tmp_path)
    runtime = _runtime(tmp_path, store, task_store)

    class _Gateway:
        calls = 0

        def begin_linked_fix(self, **_kwargs):
            self.calls += 1
            return {"ok": True, "eligible_review_count": 1}

    gateway = _Gateway()
    runtime.annotation_gateway = gateway
    fail_ensure = True
    ensure_calls = 0
    wake_calls: list[str] = []

    async def ensure_session(*_args, **kwargs):
        nonlocal ensure_calls
        ensure_calls += 1
        if fail_ensure:
            raise RuntimeError("simulated crash before linked Fix wake")
        return kwargs.get("preallocated_session_id")

    async def wake_task(**kwargs):
        wake_calls.append(kwargs["task_id"])
        return True

    runtime.ensure_web_session = ensure_session  # type: ignore[method-assign]
    runtime.wake_navigation_task_from_workbench = wake_task  # type: ignore[method-assign]
    with pytest.raises(
        RuntimeError,
        match="simulated crash before linked Fix wake",
    ):
        await runtime.recover_explicit_linked_fix_tasks_once()

    identity_digest = hashlib.sha256(
        f"trajectory_fix:{parent.task_id}".encode("utf-8")
    ).hexdigest()
    child_task_id = f"nav_fix_{identity_digest[:32]}"
    create_outbox = store.get_outbox_by_idempotency_key(
        f"navigation_auto_linked_fix:{parent.task_id}"
    )
    wake_outbox = store.get_outbox_by_idempotency_key(
        f"navigation_auto_linked_fix_wake:{parent.task_id}"
    )
    child_binding = store.get_task_binding(child_task_id)
    assert create_outbox is not None and create_outbox.status == "completed"
    assert wake_outbox is not None and wake_outbox.status == "pending"
    assert wake_outbox.turn_id is None
    assert child_binding is not None and child_binding.slot_state == "open"
    assert wake_calls == []

    fail_ensure = False
    assistant_messages_before = [
        message
        for message in store.get_session(session.id).messages
        if message.role == "assistant"
    ]
    assert await runtime.recover_explicit_linked_fix_tasks_once() == 1
    assert await runtime.recover_explicit_linked_fix_tasks_once() == 0
    assistant_messages_after = [
        message
        for message in store.get_session(session.id).messages
        if message.role == "assistant"
    ]
    assert assistant_messages_after == assistant_messages_before
    assert ensure_calls == 2
    assert wake_calls == [child_task_id]
    assert gateway.calls == 1
    assert child_task_id not in store.get_session(session.id).model_dump_json()
    assert child_binding.navigation_session_id not in (
        store.get_session(session.id).model_dump_json()
    )


@pytest.mark.asyncio
async def test_explicit_linked_fix_wake_receipts_dedupe_outbox_completion_retry(
    tmp_path: Path,
) -> None:
    store, task_store, session, parent = _seed_completed_fix_parent(tmp_path)
    runtime = _runtime(tmp_path, store, task_store)

    class _Gateway:
        calls = 0

        def begin_linked_fix(self, **_kwargs):
            self.calls += 1
            return {"ok": True, "eligible_review_count": 1}

    gateway = _Gateway()
    bus = _IdempotentRecordingBus()
    runtime.annotation_gateway = gateway
    runtime.message_bus = bus

    async def ensure_session(*_args, **kwargs):
        return kwargs.get("preallocated_session_id")

    runtime.ensure_web_session = ensure_session  # type: ignore[method-assign]
    real_complete_outbox = store.complete_outbox
    failed_wake_completion = False

    def crash_after_wake_dispatch(outbox_id: str, **kwargs):
        nonlocal failed_wake_completion
        outbox = store.get_outbox(outbox_id)
        assert outbox is not None
        if (
            outbox.kind == "navigation_explicit_linked_fix_wake"
            and kwargs.get("success", True)
            and not failed_wake_completion
        ):
            failed_wake_completion = True
            raise RuntimeError("simulated crash after linked Fix wake dispatch")
        return real_complete_outbox(outbox_id, **kwargs)

    assistant_messages_before = [
        message
        for message in store.get_session(session.id).messages
        if message.role == "assistant"
    ]
    store.complete_outbox = crash_after_wake_dispatch  # type: ignore[method-assign]
    with pytest.raises(
        RuntimeError,
        match="simulated crash after linked Fix wake dispatch",
    ):
        await runtime.recover_explicit_linked_fix_tasks_once()
    store.complete_outbox = real_complete_outbox  # type: ignore[method-assign]

    identity_digest = hashlib.sha256(
        f"trajectory_fix:{parent.task_id}".encode("utf-8")
    ).hexdigest()
    child_task_id = f"nav_fix_{identity_digest[:32]}"
    dispatch_key = f"navigation_auto_linked_fix_dispatch:{parent.task_id}"
    inbox_receipt = store.get_outbox_by_idempotency_key(f"{dispatch_key}:inbox")
    wakeup_receipt = store.get_outbox_by_idempotency_key(f"{dispatch_key}:wakeup")
    wake_outbox = store.get_outbox_by_idempotency_key(
        f"navigation_auto_linked_fix_wake:{parent.task_id}"
    )
    assert inbox_receipt is not None and inbox_receipt.status == "completed"
    assert wakeup_receipt is not None and wakeup_receipt.status == "completed"
    assert wake_outbox is not None and wake_outbox.status == "pending"
    assert len(bus.inbox) == 1
    assert len(bus.wakeups) == 1

    assert await runtime.recover_explicit_linked_fix_tasks_once() == 1
    assert await runtime.recover_explicit_linked_fix_tasks_once() == 0
    assert len(bus.inbox) == 1
    assert len(bus.wakeups) == 1
    assert gateway.calls == 1
    with sqlite3.connect(task_store.db_path) as connection:
        lineage_count = connection.execute(
            """SELECT COUNT(*) FROM navigation_task_lineage
               WHERE parent_task_id = ? AND relation = 'trajectory_fix'""",
            (parent.task_id,),
        ).fetchone()[0]
    assert lineage_count == 1
    assistant_messages_after = [
        message
        for message in store.get_session(session.id).messages
        if message.role == "assistant"
    ]
    assert assistant_messages_after == assistant_messages_before
    public_detail = store.get_session(session.id).model_dump_json()
    child_binding = store.get_task_binding(child_task_id)
    assert child_binding is not None
    assert child_task_id not in public_detail
    assert child_binding.navigation_session_id not in public_detail


@pytest.mark.parametrize(
    "receipt_kind",
    [
        "navigation_workbench_inbox_dispatched",
        "navigation_workbench_wakeup_enqueued",
    ],
)
@pytest.mark.asyncio
async def test_explicit_linked_fix_transport_is_idempotent_before_sqlite_receipt(
    tmp_path: Path,
    receipt_kind: str,
) -> None:
    store, task_store, session, parent = _seed_completed_fix_parent(tmp_path)
    runtime = _runtime(tmp_path, store, task_store)

    class _Gateway:
        calls = 0

        def begin_linked_fix(self, **_kwargs):
            self.calls += 1
            return {"ok": True, "eligible_review_count": 1}

    gateway = _Gateway()
    bus = _IdempotentRecordingBus()
    runtime.annotation_gateway = gateway
    runtime.message_bus = bus

    async def ensure_session(*_args, **kwargs):
        return kwargs.get("preallocated_session_id")

    runtime.ensure_web_session = ensure_session  # type: ignore[method-assign]
    real_record_receipt = store.record_outbox_receipt
    failed_receipt = False

    def crash_before_receipt(**kwargs):
        nonlocal failed_receipt
        if kwargs["kind"] == receipt_kind and not failed_receipt:
            failed_receipt = True
            raise RuntimeError(f"simulated crash before {receipt_kind} receipt")
        return real_record_receipt(**kwargs)

    assistant_messages_before = [
        message
        for message in store.get_session(session.id).messages
        if message.role == "assistant"
    ]
    store.record_outbox_receipt = crash_before_receipt  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="simulated crash before"):
        await runtime.recover_explicit_linked_fix_tasks_once()
    store.record_outbox_receipt = real_record_receipt  # type: ignore[method-assign]

    assert await runtime.recover_explicit_linked_fix_tasks_once() == 1
    assert await runtime.recover_explicit_linked_fix_tasks_once() == 0
    assert len(bus.inbox) == 1
    assert len(bus.wakeups) == 1
    assert gateway.calls == 1

    identity_digest = hashlib.sha256(
        f"trajectory_fix:{parent.task_id}".encode("utf-8")
    ).hexdigest()
    child_task_id = f"nav_fix_{identity_digest[:32]}"
    dispatch_key = f"navigation_auto_linked_fix_dispatch:{parent.task_id}"
    assert (
        store.get_outbox_by_idempotency_key(f"{dispatch_key}:inbox").status
        == "completed"
    )
    assert (
        store.get_outbox_by_idempotency_key(f"{dispatch_key}:wakeup").status
        == "completed"
    )
    with sqlite3.connect(task_store.db_path) as connection:
        lineage_count = connection.execute(
            """SELECT COUNT(*) FROM navigation_task_lineage
               WHERE parent_task_id = ? AND relation = 'trajectory_fix'""",
            (parent.task_id,),
        ).fetchone()[0]
    assert lineage_count == 1
    assistant_messages_after = [
        message
        for message in store.get_session(session.id).messages
        if message.role == "assistant"
    ]
    assert assistant_messages_after == assistant_messages_before
    assert child_task_id not in store.get_session(session.id).model_dump_json()


@pytest.mark.asyncio
async def test_linked_fix_transport_replays_after_redis_state_loss_despite_receipts(
    tmp_path: Path,
) -> None:
    store, task_store, _session, parent = _seed_completed_fix_parent(tmp_path)
    runtime = _runtime(tmp_path, store, task_store)

    class _Gateway:
        def begin_linked_fix(self, **_kwargs):
            return {"ok": True, "eligible_review_count": 1}

    bus = _IdempotentRecordingBus()
    runtime.annotation_gateway = _Gateway()
    runtime.message_bus = bus

    async def ensure_session(*_args, **kwargs):
        return kwargs.get("preallocated_session_id")

    runtime.ensure_web_session = ensure_session  # type: ignore[method-assign]
    assert await runtime.recover_explicit_linked_fix_tasks_once() == 1
    assert len(bus.inbox) == 1
    assert len(bus.wakeups) == 1

    identity_digest = hashlib.sha256(
        f"trajectory_fix:{parent.task_id}".encode("utf-8")
    ).hexdigest()
    child_task_id = f"nav_fix_{identity_digest[:32]}"
    bus.simulate_full_redis_loss()

    dispatched = await runtime.wake_navigation_task_from_workbench(
        task_id=child_task_id,
        reason="explicit_postprocessing_and_fix",
        dispatch_idempotency_key=(
            f"navigation_auto_linked_fix_dispatch:{parent.task_id}"
        ),
    )

    assert dispatched is True
    assert len(bus.inbox) == 1
    assert len(bus.wakeups) == 1


@pytest.mark.asyncio
async def test_workbench_dispatch_uses_atomic_redis_marker_and_xadd(
    tmp_path: Path,
) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    task_store = SqliteNavigationTaskStore(tmp_path / "navigation.sqlite")
    session = store.create_session("Redis 幂等传输")
    binding = store.create_task_binding(
        session.id,
        task_id="task-redis-dispatch",
        task_ref="DP-REDIS",
        navigation_session_id="navigation-redis-dispatch",
    ).binding
    task_store.create_task_attempt(
        task_id=binding.task_id,
        request="继续工作台任务",
        target="trajectory_review",
        date="20270623",
        segments=["20260623_145550"],
        scene_mode="out",
        dry_run=False,
        web_session_id=session.id,
        agentscope_session_id=binding.navigation_session_id,
        requested_outcome="trajectory_fix",
    )
    runtime = _runtime(tmp_path, store, task_store)

    class _FakeRedisClient:
        def __init__(self) -> None:
            self.markers: dict[str, str] = {}
            self.streams: dict[str, list[str]] = {}

        async def eval(
            self,
            script: str,
            key_count: int,
            marker_key: str,
            stream_key: str,
            payload: str,
        ) -> int:
            assert key_count == 2
            assert "XADD" in script and "SET" in script
            if marker_key in self.markers:
                return 0
            entry_id = f"{len(self.streams.get(stream_key, [])) + 1}-0"
            self.streams.setdefault(stream_key, []).append(payload)
            self.markers[marker_key] = entry_id
            return 1

    class _FakeRedisBus:
        _INBOX_KEY = "agentscope:inbox:{sid}"
        _WAKEUP_QUEUE_KEY = "agentscope:wakeups"
        _WAKEUP_SIGNAL_KEY = "agentscope:wakeup_signal"

        def __init__(self) -> None:
            self.client = _FakeRedisClient()
            self.signals = 0

        def get_client(self):
            return self.client

        async def publish(self, key: str, payload: dict) -> None:
            assert key == self._WAKEUP_SIGNAL_KEY
            assert payload == {}
            self.signals += 1

    bus = _FakeRedisBus()
    runtime.message_bus = bus

    async def ensure_session(*_args, **kwargs):
        return kwargs.get("preallocated_session_id")

    runtime.ensure_web_session = ensure_session  # type: ignore[method-assign]
    dispatch_key = "navigation_auto_linked_fix_dispatch:redis"
    assert await runtime.wake_navigation_task_from_workbench(
        task_id=binding.task_id,
        reason="explicit_postprocessing_and_fix",
        dispatch_idempotency_key=dispatch_key,
    )
    inbox_payload = bus.client.streams[
        "agentscope:inbox:navigation-redis-dispatch"
    ][0]
    validated_hint = HintBlock.model_validate_json(inbox_payload)
    assert validated_hint.source == "datapilot_workbench"
    assert "explicit_postprocessing_and_fix" in str(validated_hint.hint)
    milestone_events = [
        event
        for event in store.get_session(session.id).events
        if str(event.payload.get("phase", "")).startswith("workflow_")
    ]
    assert [event.type for event in milestone_events] == [
        "progress_start",
        "progress_end",
    ]
    assert milestone_events[0].payload["summary"] == (
        "后处理已完成，DataPilot 将继续处理轨迹 Fix。"
    )
    assert not await runtime.wake_navigation_task_from_workbench(
        task_id=binding.task_id,
        reason="explicit_postprocessing_and_fix",
        dispatch_idempotency_key=dispatch_key,
    )
    assert len([
        event
        for event in store.get_session(session.id).events
        if str(event.payload.get("phase", "")).startswith("workflow_")
    ]) == 2
    assert len(bus.client.streams["agentscope:inbox:navigation-redis-dispatch"]) == 1
    assert len(bus.client.streams["agentscope:wakeups"]) == 1
    assert bus.signals == 2

    bus.client.markers.clear()
    bus.client.streams.clear()
    assert await runtime.wake_navigation_task_from_workbench(
        task_id=binding.task_id,
        reason="explicit_postprocessing_and_fix",
        dispatch_idempotency_key=dispatch_key,
    )
    assert len(bus.client.streams["agentscope:inbox:navigation-redis-dispatch"]) == 1
    assert len(bus.client.streams["agentscope:wakeups"]) == 1


@pytest.mark.asyncio
async def test_postprocessing_without_explicit_fix_never_creates_automatic_child(
    tmp_path: Path,
) -> None:
    store, task_store, _session, parent = _seed_completed_fix_parent(
        tmp_path,
        requested_outcome="postprocessing",
    )
    runtime = _runtime(tmp_path, store, task_store)

    class _UnexpectedGateway:
        def begin_linked_fix(self, **_kwargs):
            raise AssertionError("ordinary postprocessing must not create a Fix child")

    runtime.annotation_gateway = _UnexpectedGateway()

    assert await runtime.recover_explicit_linked_fix_tasks_once() == 0
    assert task_store.get_linked_child(parent.task_id) is None
    assert store.get_outbox_by_idempotency_key(
        f"navigation_auto_linked_fix:{parent.task_id}"
    ) is None


@pytest.mark.asyncio
async def test_legacy_linked_fix_start_outbox_recovers_without_fake_turn_authority(
    tmp_path: Path,
) -> None:
    store, task_store, session, parent = _seed_completed_fix_parent(tmp_path)
    identity_digest = hashlib.sha256(
        f"trajectory_fix:{parent.task_id}".encode("utf-8")
    ).hexdigest()
    child_task_id = f"nav_fix_{identity_digest[:32]}"
    navigation_session_id = (
        f"{session.id}__task_{identity_digest[:32]}__navigation-data-agent"
    )
    legacy = store.create_task_binding(
        session.id,
        task_id=child_task_id,
        task_ref=f"DP-{identity_digest[:12].upper()}",
        navigation_session_id=navigation_session_id,
        outbox_payload={
            "request": "继续已完成后处理任务的三维轨迹 Fix",
            "target": "trajectory_review",
            "date": parent.date,
            "clips": list(parent.segments or []),
            "requested_outcome": "trajectory_fix",
            "navigation_session_id": navigation_session_id,
            "parent_navigation_task_id": parent.task_id,
        },
        outbox_idempotency_key=f"navigation_auto_linked_fix:{parent.task_id}",
    )
    assert legacy.outbox.kind == "navigation_start"
    assert legacy.outbox.turn_id is None
    runtime = _runtime(tmp_path, store, task_store)

    class _Gateway:
        calls = 0

        def begin_linked_fix(self, **_kwargs):
            self.calls += 1
            return {"ok": True, "eligible_review_count": 1}

    gateway = _Gateway()
    runtime.annotation_gateway = gateway

    assert await runtime.recover_contract_v1_outbox_once() == 1
    recovered_create = store.get_outbox(legacy.outbox.outbox_id)
    wake_outbox = store.get_outbox_by_idempotency_key(
        f"navigation_auto_linked_fix_wake:{parent.task_id}"
    )
    linked_child = task_store.get_linked_child(parent.task_id)
    assert recovered_create is not None and recovered_create.status == "completed"
    assert wake_outbox is not None
    assert wake_outbox.status == "pending"
    assert wake_outbox.turn_id is None
    assert linked_child is not None and linked_child.task_id == child_task_id
    assert gateway.calls == 1
    assert store.get_response_authority("") is None


@pytest.mark.asyncio
async def test_all_clips_cannot_smuggle_or_broaden_a_selected_clip_scope(
    tmp_path: Path,
) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    task_store = SqliteNavigationTaskStore(tmp_path / "navigation.sqlite")
    session = store.create_session("禁止扩大范围")
    store.begin_user_turn(
        session.id,
        "只处理 20270605 下的 20260605_152856。",
    )
    runtime = _runtime(tmp_path, store, task_store)
    _install_start_stubs(runtime, task_store)

    with pytest.raises(
        NavigationTaskEntryError,
        match="all_clips selection must not include a clips field",
    ):
        await runtime.start_navigation_agent_task_v1(
            web_session_id=session.id,
            router_session_id="router-broadened-scope",
            scope_source="interpreted_user_text",
            dataset_date="20270605",
            selection={
                "kind": "all_clips",
                "clips": ["20260605_152856"],
            },
            scene_mode=None,
        )

    assert store.list_task_bindings(session.id) == []
    with sqlite3.connect(task_store.db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM navigation_tasks").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_continue_rejects_router_snapshot_that_became_stale_without_writes(
    tmp_path: Path,
) -> None:
    session_db = tmp_path / "sessions.sqlite"
    store = WebSessionStore(session_db)
    task_store = SqliteNavigationTaskStore(tmp_path / "navigation.sqlite")
    session = store.create_session("陈旧继续")
    binding = store.create_task_binding(
        session.id,
        task_id="task-stale-continue",
        task_ref="DP-STALE-CONTINUE",
        navigation_session_id="navigation-stale-continue",
    ).binding
    active = task_store.create_task_attempt(
        task_id=binding.task_id,
        request="处理导航数据",
        target="navigation_data",
        date="20260720",
        segments=["clip-a"],
        scene_mode="out",
        dry_run=False,
        web_session_id=session.id,
        agentscope_session_id=binding.navigation_session_id,
    ).task
    waiting = _update_status(task_store, active, NavigationTaskStatus.WAITING_USER)
    store.update_task_binding(
        binding.task_id,
        expected_revision=binding.state_revision,
        status="waiting_user",
    )
    turn = store.begin_user_turn(session.id, "继续处理").turn
    runtime = _runtime(tmp_path, store, task_store)
    runtime.router_context_envelope(
        session.id,
        router_session_id="router-private-stale",
    )
    externally_changed = task_store.update_task_for_session(
        waiting.task_id,
        web_session_id=session.id,
        agentscope_session_id=binding.navigation_session_id,
        expected_state_revision=waiting.state_revision,
        guidance_revision=waiting.guidance_revision + 1,
    )

    with pytest.raises(RuntimeError, match="stale_context"):
        await runtime.continue_navigation_agent_task_v1(
            web_session_id=session.id,
            router_session_id="router-private-stale",
        )

    current = task_store.get_task(waiting.task_id)
    assert current is not None
    assert current.state_revision == externally_changed.state_revision
    assert current.guidance_revision == externally_changed.guidance_revision
    assert current.status == NavigationTaskStatus.WAITING_USER
    assert store.get_response_authority(turn.id).producer == "router"
    with sqlite3.connect(session_db) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM runtime_outbox WHERE kind = 'navigation_continue'"
        ).fetchone()[0] == 0


@pytest.mark.asyncio
async def test_control_rejects_router_snapshot_that_became_stale_without_writes(
    tmp_path: Path,
) -> None:
    session_db = tmp_path / "sessions.sqlite"
    store = WebSessionStore(session_db)
    task_store = SqliteNavigationTaskStore(tmp_path / "navigation.sqlite")
    session = store.create_session("陈旧控制")
    binding = store.create_task_binding(
        session.id,
        task_id="task-stale-control",
        task_ref="DP-STALE-CONTROL",
        navigation_session_id="navigation-stale-control",
    ).binding
    active = task_store.create_task_attempt(
        task_id=binding.task_id,
        request="处理导航数据",
        target="navigation_data",
        date="20260720",
        segments=["clip-a"],
        scene_mode="out",
        dry_run=False,
        web_session_id=session.id,
        agentscope_session_id=binding.navigation_session_id,
    ).task
    turn = store.begin_user_turn(session.id, "停止任务").turn
    runtime = _runtime(tmp_path, store, task_store)
    runtime.router_context_envelope(
        session.id,
        router_session_id="router-private-control",
    )
    externally_changed = task_store.update_task_for_session(
        active.task_id,
        web_session_id=session.id,
        agentscope_session_id=binding.navigation_session_id,
        expected_state_revision=active.state_revision,
        guidance_revision=active.guidance_revision + 1,
    )

    with pytest.raises(RuntimeError, match="stale_context"):
        await runtime.control_navigation_agent_task_v1(
            web_session_id=session.id,
            router_session_id="router-private-control",
            action="stop",
        )

    current = task_store.get_task(active.task_id)
    assert current is not None
    assert current.state_revision == externally_changed.state_revision
    assert current.status == NavigationTaskStatus.ACTIVE
    assert store.get_response_authority(turn.id).producer == "router"
    with sqlite3.connect(session_db) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM runtime_outbox WHERE task_id = ?",
            (active.task_id,),
        ).fetchone()[0] == 1  # only the original navigation_start binding outbox


@pytest.mark.asyncio
async def test_stop_rejects_waiting_user_instead_of_accepting_a_noop(tmp_path: Path) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    task_store = SqliteNavigationTaskStore(tmp_path / "navigation.sqlite")
    session = store.create_session("等待后处理选择")
    binding = store.create_task_binding(
        session.id,
        task_id="task-waiting-stop",
        task_ref="DP-WAITING-STOP",
        navigation_session_id="navigation-waiting-stop",
    ).binding
    active = task_store.create_task_attempt(
        task_id=binding.task_id,
        request="处理导航数据",
        target="navigation_data",
        date="20260720",
        segments=["clip-a"],
        scene_mode=None,
        dry_run=False,
        web_session_id=session.id,
        agentscope_session_id=binding.navigation_session_id,
    ).task
    waiting = _update_status(task_store, active, NavigationTaskStatus.WAITING_USER)
    binding = store.update_task_binding(
        binding.task_id,
        expected_revision=binding.state_revision,
        status="waiting_user",
        latest_public_update="等待确认是否继续后处理。",
    )
    turn = store.begin_user_turn(session.id, "先这样，不用继续了。").turn
    runtime = _runtime(tmp_path, store, task_store)
    runtime.router_context_envelope(
        session.id,
        router_session_id="router-private-waiting-stop",
    )

    with pytest.raises(RuntimeError, match="no running operation to stop"):
        await runtime.control_navigation_agent_task_v1(
            web_session_id=session.id,
            router_session_id="router-private-waiting-stop",
            action="stop",
        )

    current = task_store.get_task(waiting.task_id)
    current_binding = store.get_task_binding(binding.task_id)
    authority = store.get_response_authority(turn.id)
    assert current is not None and current.status == NavigationTaskStatus.WAITING_USER
    assert current_binding is not None and current_binding.status == "waiting_user"
    assert authority is not None and authority.producer == "router"
    assert [message for message in store.get_session(session.id).messages if message.role == "assistant"] == []


def test_specialist_completion_releases_open_binding(tmp_path: Path) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    task_store = SqliteNavigationTaskStore(tmp_path / "navigation.sqlite")
    session = store.create_session("正常收尾")
    binding = store.create_task_binding(
        session.id,
        task_id="task-normal-close",
        task_ref="DP-NORMAL-CLOSE",
        navigation_session_id="navigation-normal-close",
    ).binding
    active = task_store.create_task_attempt(
        task_id=binding.task_id,
        request="处理导航数据",
        target="navigation_data",
        date="20260720",
        segments=["clip-a"],
        scene_mode=None,
        dry_run=False,
        web_session_id=session.id,
        agentscope_session_id=binding.navigation_session_id,
    ).task
    completed = task_store.update_task_for_session(
        active.task_id,
        web_session_id=session.id,
        agentscope_session_id=binding.navigation_session_id,
        expected_state_revision=active.state_revision,
        status=NavigationTaskStatus.COMPLETED,
    )
    runtime = _runtime(tmp_path, store, task_store)

    synced = runtime._sync_task_state_on_specialist_final_v1(
        web_session_id=session.id,
        task=completed,
        binding=binding,
    )

    current_binding = store.get_task_binding(binding.task_id)
    assert synced.status == NavigationTaskStatus.COMPLETED
    assert current_binding is not None
    assert current_binding.status == "completed"
    assert current_binding.slot_state == "closed"
    assert current_binding.latest_public_update == "已按你的选择完成当前任务。"


def test_plain_specialist_final_cannot_strand_task_as_active(tmp_path: Path) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    task_store = SqliteNavigationTaskStore(tmp_path / "navigation.sqlite")
    session = store.create_session("缺少等待契约")
    binding = store.create_task_binding(
        session.id,
        task_id="task-missing-wait-contract",
        task_ref="DP-MISSING-WAIT-CONTRACT",
        navigation_session_id="navigation-missing-wait-contract",
    ).binding
    active = task_store.create_task_attempt(
        task_id=binding.task_id,
        request="检查已有产物并继续",
        target="navigation_data",
        date="20260720",
        segments=["clip-a"],
        scene_mode=None,
        dry_run=False,
        web_session_id=session.id,
        agentscope_session_id=binding.navigation_session_id,
    ).task
    runtime = _runtime(tmp_path, store, task_store)
    runtime._navigation_durable_state_anchor = (  # type: ignore[method-assign]
        lambda _session_id, **_kwargs: {"execution_status": None}
    )

    synced = runtime._sync_task_state_on_specialist_final_v1(
        web_session_id=session.id,
        task=active,
        binding=binding,
    )

    current_binding = store.get_task_binding(binding.task_id)
    assert synced.status == NavigationTaskStatus.NEEDS_REPLAN
    assert current_binding is not None
    assert current_binding.status == "needs_replan"
    assert current_binding.latest_public_update == "当前方案需要调整。"


def test_specialist_final_reconciles_binding_when_task_transition_already_committed(
    tmp_path: Path,
) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    task_store = SqliteNavigationTaskStore(tmp_path / "navigation.sqlite")
    session = store.create_session("终态跨库重放")
    binding = store.create_task_binding(
        session.id,
        task_id="task-final-reconcile",
        task_ref="DP-FINAL-RECONCILE",
        navigation_session_id="navigation-final-reconcile",
    ).binding
    active = task_store.create_task_attempt(
        task_id=binding.task_id,
        request="处理导航数据",
        target="navigation_data",
        date="20260720",
        segments=["clip-a"],
        scene_mode=None,
        dry_run=False,
        web_session_id=session.id,
        agentscope_session_id=binding.navigation_session_id,
    ).task
    needs_replan = _update_status(
        task_store,
        active,
        NavigationTaskStatus.NEEDS_REPLAN,
    )
    runtime = _runtime(tmp_path, store, task_store)
    runtime._navigation_durable_state_anchor = (  # type: ignore[method-assign]
        lambda _session_id, **_kwargs: {"execution_status": "needs_replan"}
    )

    synced = runtime._sync_task_state_on_specialist_final_v1(
        web_session_id=session.id,
        task=needs_replan,
        binding=binding,
    )

    repaired = store.get_task_binding(binding.task_id)
    assert synced.status == NavigationTaskStatus.NEEDS_REPLAN
    assert repaired is not None and repaired.status == "needs_replan"
    assert repaired.latest_public_update == "当前方案需要调整。"


def test_completed_nonfinal_phase_requires_explicit_await_user_disposition(
    tmp_path: Path,
) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    task_store = SqliteNavigationTaskStore(tmp_path / "navigation.sqlite")
    session = store.create_session("禁止隐式等待")
    binding = store.create_task_binding(
        session.id,
        task_id="task-no-implicit-wait",
        task_ref="DP-NO-IMPLICIT-WAIT",
        navigation_session_id="navigation-no-implicit-wait",
    ).binding
    active = task_store.create_task_attempt(
        task_id=binding.task_id,
        request="处理导航数据",
        target="navigation_data",
        date="20260720",
        segments=["clip-a"],
        scene_mode=None,
        dry_run=False,
        web_session_id=session.id,
        agentscope_session_id=binding.navigation_session_id,
    ).task
    task_store.update_task_for_session(
        active.task_id,
        web_session_id=session.id,
        agentscope_session_id=binding.navigation_session_id,
        expected_state_revision=active.state_revision,
        accepted_plan_phase="extract_sync",
    )
    active = task_store.get_task(active.task_id)
    assert active is not None
    runtime = _runtime(tmp_path, store, task_store)
    runtime._navigation_durable_state_anchor = (  # type: ignore[method-assign]
        lambda _session_id, **_kwargs: {"execution_status": "completed"}
    )

    synced = runtime._sync_task_state_on_specialist_final_v1(
        web_session_id=session.id,
        task=active,
        binding=binding,
    )

    repaired = store.get_task_binding(binding.task_id)
    assert synced.status == NavigationTaskStatus.NEEDS_REPLAN
    assert repaired is not None and repaired.status == "needs_replan"
    assert "_runtime_await_user" not in repaired.scope


def test_stale_router_error_returns_latest_safe_task_summary(tmp_path: Path) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    task_store = SqliteNavigationTaskStore(tmp_path / "navigation.sqlite")
    session = store.create_session("最新安全快照")
    binding = store.create_task_binding(
        session.id,
        task_id="task-stale-summary",
        task_ref="DP-STALE-SUMMARY",
        navigation_session_id="navigation-stale-summary",
    ).binding
    task_store.create_task_attempt(
        task_id=binding.task_id,
        request="处理导航数据",
        target="navigation_data",
        date="20260720",
        segments=["clip-a"],
        scene_mode="out",
        dry_run=False,
        web_session_id=session.id,
        agentscope_session_id=binding.navigation_session_id,
    )
    runtime = _runtime(tmp_path, store, task_store)

    result = runtime.safe_router_tool_error(
        RuntimeError("stale_context: navigation task revision changed"),
        action="continue",
        web_session_id=session.id,
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "stale_context"
    assert result["task_ref"] == binding.task_ref
    assert result["latest_task"]["task_ref"] == binding.task_ref
    assert result["latest_task"]["dataset_date"] == "20260720"
    assert result["latest_task"]["selection"] == {
        "kind": "selected_clips",
        "clips": ["clip-a"],
    }
    assert result["latest_task"]["scene_mode"] == "outdoor"
    assert "target" not in result["latest_task"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["task", "binding", "spawn"])
async def test_continue_outbox_recovery_failure_never_leaves_active_without_run(
    tmp_path: Path,
    failure_stage: str,
) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    task_store = SqliteNavigationTaskStore(tmp_path / "navigation.sqlite")
    session = store.create_session("继续恢复失败")
    binding_creation = store.create_task_binding(
        session.id,
        task_id="task-recovery-continue",
        task_ref="DP-RECOVERY-CONTINUE",
        navigation_session_id="navigation-recovery-continue",
    )
    binding = binding_creation.binding
    original = store.claim_outbox_item(
        binding_creation.outbox.outbox_id,
        worker_id="fixture",
    )
    assert original is not None
    store.complete_outbox(original.outbox_id, worker_id="fixture")
    active = task_store.create_task_attempt(
        task_id=binding.task_id,
        request="处理导航数据",
        target="navigation_data",
        date="20260720",
        segments=["clip-a"],
        scene_mode="out",
        dry_run=False,
        web_session_id=session.id,
        agentscope_session_id=binding.navigation_session_id,
    ).task
    waiting = _update_status(task_store, active, NavigationTaskStatus.WAITING_USER)
    binding = store.update_task_binding(
        binding.task_id,
        expected_revision=binding.state_revision,
        status="waiting_user",
    )
    turn = store.begin_user_turn(session.id, "补充信息后继续").turn
    store.bind_conversation_agent_session_to_turn(binding.navigation_session_id, turn.id)
    authority = store.get_response_authority(turn.id)
    assert authority is not None
    store.handover_response_authority_with_outbox(
        turn.id,
        expected_producer="router",
        expected_generation=authority.generation,
        new_producer="navigation",
        kind="navigation_continue",
        aggregate_type="navigation_task",
        aggregate_id=binding.task_id,
        payload={
            "original_user_message": "补充信息后继续",
            "navigation_session_id": binding.navigation_session_id,
            "task_ref": binding.task_ref,
            "accepted_task_revision": waiting.state_revision,
            "previous_task_status": "waiting_user",
        },
        idempotency_key=f"navigation_continue:{turn.id}:{binding.task_id}",
        web_session_id=session.id,
        task_id=binding.task_id,
    )
    runtime = _runtime(tmp_path, store, task_store)

    async def ensure_session(*_args, **kwargs):
        return kwargs.get("preallocated_session_id")

    spawn_calls = 0

    async def start_or_fail(**_kwargs):
        nonlocal spawn_calls
        spawn_calls += 1
        if failure_stage == "spawn":
            raise RuntimeError("spawn failed")
        raise AssertionError("Run started before both durable stores were active")

    async def publish_state(**_kwargs):
        return None

    runtime.ensure_web_session = ensure_session  # type: ignore[method-assign]
    runtime._start_agent_run = start_or_fail  # type: ignore[method-assign]
    runtime._publish_v1_task_state = publish_state  # type: ignore[method-assign]
    failed_once = False
    real_update_task = task_store.update_task_for_session
    real_update_binding = store.update_task_binding

    def update_task_with_failure(*args, **kwargs):
        nonlocal failed_once
        if (
            failure_stage == "task"
            and kwargs.get("status") == NavigationTaskStatus.ACTIVE
            and not failed_once
        ):
            failed_once = True
            raise RuntimeError("task activation failed")
        return real_update_task(*args, **kwargs)

    def update_binding_with_failure(*args, **kwargs):
        nonlocal failed_once
        if (
            failure_stage == "binding"
            and kwargs.get("status") == "active"
            and not failed_once
        ):
            failed_once = True
            raise RuntimeError("binding activation failed")
        return real_update_binding(*args, **kwargs)

    task_store.update_task_for_session = update_task_with_failure  # type: ignore[method-assign]
    store.update_task_binding = update_binding_with_failure  # type: ignore[method-assign]

    assert await runtime.recover_contract_v1_outbox_once() == 0

    current = task_store.get_task(binding.task_id)
    current_binding = store.get_task_binding(binding.task_id)
    expected_status = (
        NavigationTaskStatus.WAITING_USER
        if failure_stage == "task"
        else NavigationTaskStatus.NEEDS_REPLAN
    )
    assert current is not None and current.status == expected_status
    assert current_binding is not None and current_binding.status == expected_status.value
    assert spawn_calls == (1 if failure_stage == "spawn" else 0)


@pytest.mark.asyncio
async def test_continue_recovery_publish_failure_does_not_fail_accepted_run(
    tmp_path: Path,
) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    task_store = SqliteNavigationTaskStore(tmp_path / "navigation.sqlite")
    session = store.create_session("继续恢复通知失败")
    binding_creation = store.create_task_binding(
        session.id,
        task_id="task-recovery-publish",
        task_ref="DP-RECOVERY-PUBLISH",
        navigation_session_id="navigation-recovery-publish",
    )
    binding = binding_creation.binding
    original = store.claim_outbox_item(binding_creation.outbox.outbox_id, worker_id="fixture")
    assert original is not None
    store.complete_outbox(original.outbox_id, worker_id="fixture")
    active = task_store.create_task_attempt(
        task_id=binding.task_id,
        request="处理导航数据",
        target="navigation_data",
        date="20260720",
        segments=["clip-a"],
        scene_mode="out",
        dry_run=False,
        web_session_id=session.id,
        agentscope_session_id=binding.navigation_session_id,
    ).task
    waiting = _update_status(task_store, active, NavigationTaskStatus.WAITING_USER)
    binding = store.update_task_binding(
        binding.task_id,
        expected_revision=binding.state_revision,
        status="waiting_user",
    )
    turn = store.begin_user_turn(session.id, "继续").turn
    authority = store.get_response_authority(turn.id)
    assert authority is not None
    _, continuation_outbox = store.handover_response_authority_with_outbox(
        turn.id,
        expected_producer="router",
        expected_generation=authority.generation,
        new_producer="navigation",
        kind="navigation_continue",
        aggregate_type="navigation_task",
        aggregate_id=binding.task_id,
        payload={"original_user_message": "继续"},
        idempotency_key=f"navigation_continue:{turn.id}:{binding.task_id}",
        web_session_id=session.id,
        task_id=binding.task_id,
    )
    runtime = _runtime(tmp_path, store, task_store)

    async def ensure_session(*_args, **kwargs):
        return kwargs.get("preallocated_session_id")

    async def start_run(**_kwargs):
        return None

    async def fail_publish(**_kwargs):
        raise RuntimeError("notification failed")

    runtime.ensure_web_session = ensure_session  # type: ignore[method-assign]
    runtime._start_agent_run = start_run  # type: ignore[method-assign]
    runtime._publish_v1_task_state = fail_publish  # type: ignore[method-assign]

    assert await runtime.recover_contract_v1_outbox_once() == 0

    current = task_store.get_task(waiting.task_id)
    current_binding = store.get_task_binding(binding.task_id)
    current_authority = store.get_response_authority(turn.id)
    assert current is not None and current.status == NavigationTaskStatus.ACTIVE
    assert current_binding is not None and current_binding.status == "active"
    assert current_authority is not None and current_authority.producer == "navigation"
    assert current_authority.lease_state == "open"
    retriable = store.get_outbox(continuation_outbox.outbox_id)
    assert retriable is not None and retriable.status == "pending"
    detail = store.get_session(session.id)
    assert detail is not None
    assert [message for message in detail.messages if message.role == "assistant"] == []


@pytest.mark.asyncio
async def test_start_recovery_outbox_completion_failure_preserves_accepted_run(
    tmp_path: Path,
) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    task_store = SqliteNavigationTaskStore(tmp_path / "navigation.sqlite")
    session = store.create_session("启动恢复通知失败")
    turn = store.begin_user_turn(session.id, "处理导航数据").turn
    creation = store.create_task_binding(
        session.id,
        task_id="task-start-complete-failure",
        task_ref="DP-START-COMPLETE-FAILURE",
        navigation_session_id="navigation-start-complete-failure",
        outbox_payload={
            "origin_turn_id": turn.id,
            "request": "处理导航数据",
            "target": "navigation_data",
            "date": "20260720",
            "clips": ["clip-a"],
            "scene_mode": "outdoor",
            "response_language": "Chinese",
        },
    )
    runtime = _runtime(tmp_path, store, task_store)

    async def ensure_session(*_args, **kwargs):
        return kwargs.get("preallocated_session_id")

    async def start_run(**_kwargs):
        return None

    runtime.ensure_web_session = ensure_session  # type: ignore[method-assign]
    runtime._start_agent_run = start_run  # type: ignore[method-assign]
    real_complete = store.complete_outbox

    def fail_recovery_completion(outbox_id: str, *, worker_id: str, **kwargs):
        if worker_id.startswith("contract-v1-recovery"):
            raise RuntimeError("outbox completion failed")
        return real_complete(outbox_id, worker_id=worker_id, **kwargs)

    store.complete_outbox = fail_recovery_completion  # type: ignore[method-assign]

    assert await runtime.recover_contract_v1_outbox_once() == 0

    task = task_store.get_task(creation.binding.task_id)
    binding = store.get_task_binding(creation.binding.task_id)
    authority = store.get_response_authority(turn.id)
    assert task is not None and task.status == NavigationTaskStatus.ACTIVE
    assert binding is not None and binding.status == "active" and binding.slot_state == "open"
    assert authority is not None and authority.producer == "navigation"
    assert authority.lease_state == "open"
    retained = store.get_outbox(creation.outbox.outbox_id)
    assert retained is not None and retained.status == "claimed"
    detail = store.get_session(session.id)
    assert detail is not None
    assert [message for message in detail.messages if message.role == "assistant"] == []


def test_failed_router_reply_does_not_emit_generic_safe_failure_while_task_active(
    tmp_path: Path,
) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    task_store = SqliteNavigationTaskStore(tmp_path / "navigation.sqlite")
    session = store.create_session("任务期间问答失败")
    binding = store.create_task_binding(
        session.id,
        task_id="task-active-router-failure",
        task_ref="DP-ACTIVE-ROUTER-FAILURE",
        navigation_session_id="navigation-active-router-failure",
    ).binding
    task_store.create_task_attempt(
        task_id=binding.task_id,
        request="处理导航数据",
        target="navigation_data",
        date="20260720",
        segments=["clip-a"],
        scene_mode="out",
        dry_run=False,
        web_session_id=session.id,
        agentscope_session_id=binding.navigation_session_id,
    )
    turn = store.begin_user_turn(session.id, "顺便问一个无关问题").turn
    router = store.save_conversation_agent_session(
        session.id,
        agent_role="router",
        agent_id="main-router-agent",
        agentscope_session_id="router-active-task-failure",
    )
    store.bind_conversation_agent_session_to_turn(router.agentscope_session_id, turn.id)
    lease_id = store.register_pending_reply(
        turn_id=turn.id,
        agentscope_session_id=router.agentscope_session_id,
        agent_id=router.agent_id,
        source="user",
    )

    records = store.fail_reply_lease(lease_id)

    assert [record.type for record in records] == ["final", "turn_state"]
    detail = store.get_session(session.id)
    assert detail is not None
    reply = detail.messages[-1].content
    assert "未能生成可安全展示的回复" not in reply
    assert "仍为处理中" in reply
    current_binding = store.get_task_binding(binding.task_id)
    assert current_binding is not None and current_binding.status == "active"


def test_store_rechecks_v1_authority_before_any_public_event_insert(
    tmp_path: Path,
) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    session = store.create_session("事务内权限复核")
    turn = store.begin_user_turn(session.id, "启动导航任务").turn
    router = store.save_conversation_agent_session(
        session.id,
        agent_role="router",
        agent_id="main-router-agent",
        agentscope_session_id="router-authority-race",
    )
    binding = store.create_task_binding(
        session.id,
        task_id="task-authority-race",
        task_ref="DP-AUTHORITY-RACE",
        navigation_session_id="navigation-authority-race",
    ).binding
    store.bind_conversation_agent_session_to_turn(router.agentscope_session_id, turn.id)
    authority = store.get_response_authority(turn.id)
    assert authority is not None
    store.handover_response_authority(
        turn.id,
        expected_producer="router",
        expected_generation=authority.generation,
        new_producer="navigation",
    )

    records = store.append_projected_event_batch(
        web_session_id=session.id,
        agentscope_session_id=router.agentscope_session_id,
        entry_id="late-router-progress",
        events=[
            {
                "contract_version": 1,
                "type": "progress_delta",
                "payload": {"delta": "这条旧 Router 进度不应公开。"},
            }
        ],
        private_events=[
            {
                "type": "tool_start",
                "payload": {"tool": "stale_router_tool", "call_id": "late-call"},
            }
        ],
        raw_event_type="TOOL_CALL_START",
        reply_id="late-router-reply",
    )

    assert records == []
    detail = store.get_session(session.id)
    assert detail is not None
    assert all(
        "旧 Router" not in json.dumps(event.payload, ensure_ascii=False)
        for event in detail.events
    )
    mapping = store.get_conversation_agent_session_by_agentscope_session(
        router.agentscope_session_id
    )
    assert mapping is not None and mapping.event_cursor == "late-router-progress"
    store.bind_conversation_agent_session_to_turn(
        binding.navigation_session_id,
        turn.id,
    )
    store.append_projected_event_batch(
        web_session_id=session.id,
        agentscope_session_id=binding.navigation_session_id,
        entry_id="navigation-result-start",
        events=[],
        raw_event_type="REPLY_START",
        reply_id="navigation-result-reply",
    )
    final_records = store.append_projected_event_batch(
        web_session_id=session.id,
        agentscope_session_id=binding.navigation_session_id,
        entry_id="navigation-result-end",
        events=[{"type": "final", "payload": {"text": "专业结果"}}],
        raw_event_type="REPLY_END",
        reply_id="navigation-result-reply",
    )
    assert [record.type for record in final_records][-2:] == ["final", "turn_state"]
    final_detail = store.get_session(session.id)
    assert final_detail is not None
    assert final_detail.turns[-1].status == "completed"
    assert final_detail.messages[-1].content == "专业结果"


def test_public_projector_fails_closed_without_v1_store(tmp_path: Path) -> None:
    config = AgentScopeRuntimeConfig(
        user_id="test-user",
        redis_url="redis://localhost:6379/0",
        workspace_root=tmp_path,
        dashscope_api_key="test-key",
        dashscope_base_url=None,
        default_model="test-model",
        router_model="router-model",
        navigation_model="navigation-model",
    )
    runtime = AgentScopeRuntime(
        config=config,
        storage=None,
        message_bus=SimpleNamespace(),
        workspace_manager=None,
        app=SimpleNamespace(state=SimpleNamespace()),
        web_session_store=None,
    )

    with pytest.raises(RuntimeError, match="contract v1"):
        runtime.project_contract_v1_event_batch(
            web_session_id="missing-session",
            agentscope_session_id="private-session",
            entry_id="raw",
            events=(
                {
                    "type": "tool_start",
                    "payload": {"tool": "secret_internal_tool", "call_id": "secret"},
                },
            ),
        )


def test_empty_authorized_final_uses_active_task_semantic_fallback(tmp_path: Path) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    session = store.create_session("空回复兜底")
    binding = store.create_task_binding(
        session.id,
        task_id="task-empty-final",
        task_ref="DP-EMPTY-FINAL",
        navigation_session_id="navigation-empty-final",
    ).binding
    turn = store.begin_user_turn(session.id, "问答").turn
    authority = store.get_response_authority(turn.id)
    assert authority is not None

    committed = store.commit_authorized_final(
        turn.id,
        producer="router",
        response_generation=authority.generation,
        text="",
    )

    assert committed.terminal_status == "failed"
    assert "未能生成可安全展示的回复" not in committed.message.content
    assert "仍为处理中" in committed.message.content
    current_binding = store.get_task_binding(binding.task_id)
    assert current_binding is not None and current_binding.status == "active"


def test_empty_router_reply_end_uses_active_task_semantic_fallback(tmp_path: Path) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    session = store.create_session("Router 空回复兜底")
    binding = store.create_task_binding(
        session.id,
        task_id="task-empty-router-reply",
        task_ref="DP-EMPTY-ROUTER",
        navigation_session_id="navigation-empty-router",
    ).binding
    turn = store.begin_user_turn(session.id, "问答").turn
    router = store.save_conversation_agent_session(
        session.id,
        agent_role="router",
        agent_id="main-router-agent",
        agentscope_session_id="router-empty-reply",
    )
    store.bind_conversation_agent_session_to_turn(router.agentscope_session_id, turn.id)
    store.register_pending_reply(
        turn_id=turn.id,
        agentscope_session_id=router.agentscope_session_id,
        agent_id=router.agent_id,
        source="user",
    )
    store.append_projected_event_batch(
        web_session_id=session.id,
        agentscope_session_id=router.agentscope_session_id,
        entry_id="empty-router-start",
        events=[],
        raw_event_type="REPLY_START",
        reply_id="empty-router-reply-id",
    )

    records = store.append_projected_event_batch(
        web_session_id=session.id,
        agentscope_session_id=router.agentscope_session_id,
        entry_id="empty-router-end",
        events=[],
        raw_event_type="REPLY_END",
        reply_id="empty-router-reply-id",
    )

    assert [record.type for record in records][-2:] == ["final", "turn_state"]
    detail = store.get_session(session.id)
    assert detail is not None
    reply = detail.messages[-1].content
    assert "未能生成可安全展示的回复" not in reply
    assert "仍为处理中" in reply
    current_binding = store.get_task_binding(binding.task_id)
    assert current_binding is not None and current_binding.status == "active"

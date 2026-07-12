from __future__ import annotations

import asyncio
import json
import logging
import re
from contextlib import suppress
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import agentscope.app
from agentscope.app.message_bus import RedisMessageBus
from agentscope.app.storage import ChatModelConfig, RedisStorage, SessionConfig
from agentscope.app.workspace_manager import LocalWorkspaceManager
from agentscope.event import ExternalExecutionResultEvent
from agentscope.message import TextBlock, ToolCallState, ToolResultBlock, ToolResultState, UserMsg
from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import ToolBase, ToolChunk

from vla_data_juicer_agents.adapters.agentscope import AgentScopeEventAdapter
from vla_data_juicer_agents.core.cancellation import CancellationContext, bind_cancellation
from vla_data_juicer_agents.core.events import CallbackEventSink, EventEmitter
from vla_data_juicer_agents.navigation.agent_tools import resolve_navigation_agent_tools
from vla_data_juicer_agents.navigation.config import NavigationSettings
from vla_data_juicer_agents.navigation.plan_execution import (
    human_decision_key,
    submit_plan_human_decision,
)
from vla_data_juicer_agents.navigation.plan_store import SqliteNavigationPlanRepository
from vla_data_juicer_agents.navigation.services import (
    NavigationServices,
    build_navigation_services,
)
from vla_data_juicer_agents.navigation.task_reconciliation import (
    _navigation_scene_mode_for_request,
    _structured_handoff_payload_from_message,
    prepare_navigation_task_entry,
    reconcile_navigation_task,
)
from vla_data_juicer_agents.runtime.agentscope_bootstrap import bootstrap_agentscope_records
from vla_data_juicer_agents.runtime.agentscope_config import AgentScopeRuntimeConfig

_EVENT_STARTUP_GRACE_SECS = 1.0
_EVENT_IDLE_POLL_SECS = 0.03
_WAKEUP_RECOVERY_INTERVAL_SECS = 5.0
_WAKEUP_RECOVERY_RETRY_DELAYS = (0.2, 1.0)
_HUMAN_DECISION_TOOL_NAMES = {
    "request_human_decision",
}
_logger = logging.getLogger(__name__)


@dataclass
class AgentScopeRecoveryMetrics:
    redis_timeout_count: int = 0
    wakeup_queue_length: int | None = None
    inbox_residual_count: int | None = None
    event_loop_lag_seconds: float | None = None
    recovered_wakeup_runs: int = 0
    recovered_orphan_inbox_runs: int = 0


@dataclass
class AgentScopeRuntime:
    config: AgentScopeRuntimeConfig
    storage: Any
    message_bus: Any
    workspace_manager: Any
    app: Any
    web_sessions: dict[str, tuple[str, str]] = field(default_factory=dict)
    event_cursors: dict[str, str | None] = field(default_factory=dict)
    web_session_store: Any | None = None
    _active_human_decision_claims: set[str] = field(default_factory=set)
    _run_cancellations: dict[str, CancellationContext] = field(default_factory=dict)
    recovery_metrics: AgentScopeRecoveryMetrics = field(default_factory=AgentScopeRecoveryMetrics)
    bootstrapped: bool = False

    def __post_init__(self) -> None:
        self._bootstrap_lock = asyncio.Lock()

    async def ensure_bootstrapped(self) -> None:
        if self.bootstrapped:
            return

        async with self._bootstrap_lock:
            if self.bootstrapped:
                return
            await bootstrap_agentscope_records(self.storage, self.config)
            self.bootstrapped = True

    def set_web_session_store(self, store: Any) -> None:
        self.web_session_store = store

    def web_session_subscription_key(self, *, web_session_id: str) -> tuple[str, str] | None:
        return self._web_session_mapping(web_session_id)

    async def ensure_web_session(self, web_session_id: str, *, agent_id: str, model: str) -> str:
        existing = self.web_sessions.get(web_session_id)
        if existing and existing[0] == agent_id:
            return existing[1]

        persisted = self._load_web_session_mapping(web_session_id, agent_id=agent_id)
        if persisted and persisted[0] == agent_id:
            self.web_sessions[web_session_id] = persisted
            self._save_web_session_mapping(web_session_id, agent_id, persisted[1])
            return persisted[1]

        session_id = f"{web_session_id}__{agent_id}"
        session = await self.storage.upsert_session(
            self.config.user_id,
            agent_id,
            config=SessionConfig(
                workspace_id=f"workspace-{web_session_id}",
                name=web_session_id,
                chat_model_config=ChatModelConfig(
                    type="dashscope_chat",
                    credential_id=self.config.credential_id,
                    model=model,
                    parameters={"parallel_tool_calls": False},
                ),
            ),
            session_id=session_id,
        )
        self.web_sessions[web_session_id] = (agent_id, session.id)
        self._save_web_session_mapping(web_session_id, agent_id, session.id)
        return session.id

    async def submit_user_message(self, *, web_session_id: str, message: str) -> str:
        await self.ensure_bootstrapped()

        agent_id = self._agent_id_for_user_message(web_session_id=web_session_id)
        model = (
            self.config.navigation_model
            if agent_id == self.config.navigation_agent_id
            else self.config.router_model
        )
        await self._start_agent_run(
            web_session_id=web_session_id,
            agent_id=agent_id,
            model=model,
            message=message,
        )
        return f"turn_{uuid4()}"

    def _agent_id_for_user_message(self, *, web_session_id: str) -> str:
        mapped = self._web_session_mapping(web_session_id)
        if mapped is not None and mapped[0] == self.config.navigation_agent_id:
            return self.config.navigation_agent_id
        return self.config.main_router_agent_id

    async def start_navigation_agent_task(self, *, web_session_id: str, message: str) -> str:
        await self.ensure_bootstrapped()

        previous_mapping = self._web_session_mapping(web_session_id)
        try:
            session_id = await self.ensure_web_session(
                web_session_id,
                agent_id=self.config.navigation_agent_id,
                model=self.config.navigation_model,
            )
            services = self._navigation_services()
            prepare_navigation_task_entry(
                task_store=services.task_store,
                observation_store=services.observation_store,
                evidence_store=services.evidence_store,
                message=message,
                web_session_id=web_session_id,
                agentscope_session_id=session_id,
                settings=services.settings,
            )
        except Exception as entry_error:
            try:
                self._restore_web_session_mapping(web_session_id, previous_mapping)
            except Exception as compensation_error:
                entry_error.add_note(
                    "navigation entry session-mapping compensation failed: "
                    f"{compensation_error!r}"
                )
            raise
        session_id = await self._start_agent_run(
            web_session_id=web_session_id,
            agent_id=self.config.navigation_agent_id,
            model=self.config.navigation_model,
            message=message,
        )
        return session_id

    async def _start_agent_run(
        self,
        *,
        web_session_id: str,
        agent_id: str,
        model: str,
        message: str,
    ) -> str:
        chat_service = self.app.state.chat_service
        session_id = await self.ensure_web_session(
            web_session_id,
            agent_id=agent_id,
            model=model,
        )
        tail_cursor = await self._event_log_tail_cursor(session_id)
        cancellation = CancellationContext()
        previous_cancellation = self.run_cancellation(session_id)
        self.register_run_cancellation(session_id, cancellation)

        if agent_id == self.config.navigation_agent_id:
            anchor = self._navigation_durable_state_anchor(session_id)
            message = (
                f"{message}\n\nDurable navigation state anchor (authoritative): "
                f"{json.dumps(anchor, ensure_ascii=False, sort_keys=True)}"
            )

        async def run_with_cancellation() -> None:
            try:
                async with cancellation.track_agent(session_id):
                    with bind_cancellation(cancellation):
                        await chat_service.run(
                            user_id=self.config.user_id,
                            session_id=session_id,
                            agent_id=agent_id,
                            input_msg=UserMsg(name="user", content=message),
                        )
            finally:
                self.clear_run_cancellation(session_id, cancellation)

        try:
            self._spawn_chat_run(run_with_cancellation(), session_id=session_id)
        except Exception:
            if previous_cancellation is not None:
                self.register_run_cancellation(session_id, previous_cancellation)
            else:
                self.clear_run_cancellation(session_id, cancellation)
            raise
        if tail_cursor is not None:
            self._remember_event_cursor(session_id, tail_cursor)
        return session_id

    async def interrupt_web_session(self, *, web_session_id: str) -> bool:
        mapped = self._web_session_mapping(web_session_id)
        if mapped is None:
            return False

        _agent_id, agentscope_session_id = mapped
        interrupted = False
        cancellation = self.run_cancellation(agentscope_session_id)
        if cancellation is not None:
            interrupted = cancellation.cancel() or interrupted

        publish_cancel = getattr(self.message_bus, "session_publish_cancel", None)
        if callable(publish_cancel):
            await publish_cancel(agentscope_session_id)
            interrupted = True
        return interrupted

    def register_run_cancellation(
        self,
        agentscope_session_id: str,
        cancellation: CancellationContext,
    ) -> None:
        self._run_cancellations[agentscope_session_id] = cancellation

    def run_cancellation(self, agentscope_session_id: str) -> CancellationContext | None:
        return self._run_cancellations.get(agentscope_session_id)

    def clear_run_cancellation(
        self,
        agentscope_session_id: str,
        cancellation: CancellationContext,
    ) -> None:
        if self._run_cancellations.get(agentscope_session_id) is cancellation:
            self._run_cancellations.pop(agentscope_session_id, None)

    def record_navigation_handoff(self, payload: dict[str, Any]) -> None:
        _logger.info("Navigation handoff: %s", payload)

    def _navigation_services(self) -> NavigationServices:
        return build_navigation_services(self.config.workspace_root)

    def _navigation_task_store(self):
        return self._navigation_services().task_store

    def _navigation_observation_store(self):
        return self._navigation_services().observation_store

    def _navigation_evidence_store(self):
        return self._navigation_services().evidence_store

    def _navigation_plan_store(self):
        return self._navigation_services().plan_store

    def _navigation_durable_state_anchor(
        self,
        agentscope_session_id: str,
    ) -> dict[str, Any]:
        services = self._navigation_services()
        task = services.task_store.find_latest_by_agentscope_session(agentscope_session_id)
        if task is None:
            return {
                "task_id": None,
                "phase": None,
                "task_status": None,
                "observation_revision": None,
                "active_plan_id": None,
                "active_plan_revision": None,
                "current_step_id": None,
            }
        reconciled = reconcile_navigation_task(task, settings=services.settings)
        changes = reconciled.model_dump(mode="json")
        changes.pop("task_id", None)
        task = services.task_store.update_task(task.task_id, **changes)
        observation = services.observation_store.latest(task.task_id)
        plan = (
            services.plan_store.get_active(task.task_id, task.phase.value)
            if task.phase.value in {"extract_sync", "finish_processing"}
            else None
        )
        current = services.plan_store.get_current_step(plan.plan_id) if plan is not None else None
        return {
            "task_id": task.task_id,
            "phase": task.phase.value,
            "task_status": task.status.value,
            "observation_revision": observation.revision if observation is not None else None,
            "active_plan_id": plan.plan_id if plan is not None else None,
            "active_plan_revision": plan.plan_revision if plan is not None else None,
            "current_step_id": (
                current["step"]["step_id"] if current is not None else None
            ),
        }

    def _navigation_tools_for_session(
        self,
        *,
        web_session_id: str,
        agentscope_session_id: str,
    ) -> list[Any]:
        return resolve_navigation_agent_tools(
            services=self._navigation_services(),
            cancellation=self.run_cancellation(agentscope_session_id),
            agentscope_session_id=agentscope_session_id,
            web_session_id=web_session_id,
        )

    async def submit_human_decision(self, *, web_session_id: str, decision: dict[str, Any]) -> bool:
        plan_id = decision.get("plan_id")
        step_id = decision.get("step_id")
        plan_bound_handoff = isinstance(plan_id, str) and isinstance(step_id, str)
        plan_store = self._navigation_plan_store() if plan_bound_handoff else None
        decision_key = (
            human_decision_key(_durable_plan_decision(decision))
            if plan_bound_handoff
            else None
        )
        if plan_store is not None:
            plan = plan_store.get(plan_id)
            if plan is not None:
                task = self._navigation_task_store().get_task(plan.task_id)
                if (
                    task is None
                    or task.created_by_web_session_id != web_session_id
                ):
                    raise RuntimeError(
                        "human decision handoff does not belong to the requested "
                        "Web session"
                    )
        mapped = self._web_session_mapping(web_session_id)
        if mapped is None:
            if (
                plan_store is not None
                and plan_store.get_human_decision_handoff(plan_id, step_id) is not None
            ):
                self._raise_human_handoff_recovery_required(
                    plan_store,
                    plan_id,
                    step_id,
                    web_session_id=web_session_id,
                    reason_code="missing_web_mapping",
                )
            return False

        agent_id, agentscope_session_id = mapped
        claim_key = _human_decision_claim_key(agentscope_session_id, decision)
        claim = await self._try_acquire_human_decision_claim(claim_key)
        if claim is None:
            return False

        claim_handoff = False
        try:
            if plan_store is not None:
                existing = plan_store.get_human_decision_handoff(plan_id, step_id)
                if existing is not None and existing.status == "quarantined":
                    raise RuntimeError(
                        f"human decision handoff {plan_id}/{step_id} is quarantined; "
                        "submit_complete_plan instead of resubmitting the stale decision"
                    )
                if existing is not None and existing.delivery_status == "delivered":
                    if existing.decision_key != decision_key:
                        return False
                    if not plan_store.acknowledge_human_decision_handoff(
                        plan_id, step_id, decision_key
                    ):
                        return False
                    self._mark_human_decision_consumed(
                        agentscope_session_id=agentscope_session_id,
                        decision=decision,
                    )
                    return True
                if existing is not None:
                    external_state = await self._human_decision_external_call_state(
                        agent_id=agent_id,
                        agentscope_session_id=agentscope_session_id,
                        decision=decision,
                    )
                    if external_state == "consumed":
                        plan_store.mark_consumed_human_decision_delivery(
                            plan_id, step_id, decision_key
                        )
                        if not plan_store.acknowledge_human_decision_handoff(
                            plan_id, step_id, decision_key
                        ):
                            return False
                        self._mark_human_decision_consumed(
                            agentscope_session_id=agentscope_session_id,
                            decision=decision,
                        )
                        return True
                    if external_state not in {"submitted", "consumed"}:
                        self._raise_human_handoff_recovery_required(
                            plan_store,
                            plan_id,
                            step_id,
                            web_session_id=web_session_id,
                            reason_code=external_state,
                        )

            pending_tool_name = await self._pending_human_decision_tool_name(
                agent_id=agent_id,
                agentscope_session_id=agentscope_session_id,
                decision=decision,
            )
            if pending_tool_name is None:
                if plan_store is not None and existing is not None:
                    self._raise_human_handoff_recovery_required(
                        plan_store,
                        plan_id,
                        step_id,
                        web_session_id=web_session_id,
                        reason_code="missing_pending_tool_call",
                    )
                return False

            if plan_bound_handoff:
                transitioned = submit_plan_human_decision(
                    plan_store=self._navigation_plan_store(),
                    evidence_store=self._navigation_evidence_store(),
                    plan_id=plan_id,
                    step_id=step_id,
                    decision=decision,
                )
                if not transitioned:
                    return False
                delivery, delivery_token = plan_store.claim_human_decision_delivery(
                    plan_id,
                    step_id,
                    decision_key,
                    owner=agentscope_session_id,
                )
                if delivery == "delivered":
                    if not plan_store.acknowledge_human_decision_handoff(
                        plan_id, step_id, decision_key
                    ):
                        return False
                    self._mark_human_decision_consumed(
                        agentscope_session_id=agentscope_session_id,
                        decision=decision,
                    )
                    return True
                if delivery == "recovery_required":
                    raise RuntimeError(
                        self._human_handoff_recovery_message(
                            plan_id,
                            step_id,
                            "ambiguous_delivery_state",
                            web_session_id=web_session_id,
                        )
                    )
                if delivery != "claimed":
                    return False
                assert delivery_token is not None

            result = ToolResultBlock(
                id=decision["tool_call_id"],
                name=pending_tool_name,
                output=json.dumps(_human_decision_tool_output(pending_tool_name, decision), ensure_ascii=False),
                state=ToolResultState.SUCCESS,
            )
            input_msg = ExternalExecutionResultEvent(
                id=(
                    f"navigation-human-handoff:{plan_id}:{step_id}:{decision_key}"
                    if plan_bound_handoff
                    else uuid4().hex
                ),
                metadata=(
                    {
                        "idempotency_key": (
                            f"navigation-human-handoff:{plan_id}:{step_id}:{decision_key}"
                        )
                    }
                    if plan_bound_handoff
                    else {}
                ),
                reply_id=decision["reply_id"],
                execution_results=[result],
            )
            cancellation = CancellationContext()
            previous_cancellation = self.run_cancellation(agentscope_session_id)
            self.register_run_cancellation(agentscope_session_id, cancellation)

            async def run_with_claim() -> None:
                run_error: Exception | None = None
                try:
                    try:
                        async with cancellation.track_agent(agentscope_session_id):
                            with bind_cancellation(cancellation):
                                await self.app.state.chat_service.run(
                                    user_id=self.config.user_id,
                                    session_id=agentscope_session_id,
                                    agent_id=agent_id,
                                    input_msg=input_msg,
                                )
                    except Exception as error:
                        run_error = error
                    if plan_bound_handoff:
                        external_state = await self._human_decision_external_call_state(
                            agent_id=agent_id,
                            agentscope_session_id=agentscope_session_id,
                            decision=decision,
                        )
                        if external_state == "consumed":
                            completed = plan_store.finish_human_decision_delivery(
                                plan_id,
                                step_id,
                                decision_key,
                                token=delivery_token,
                                delivered=True,
                            )
                            if not completed:
                                completed = plan_store.mark_consumed_human_decision_delivery(
                                    plan_id, step_id, decision_key
                                )
                        elif external_state == "submitted":
                            plan_store.finish_human_decision_delivery(
                                plan_id,
                                step_id,
                                decision_key,
                                token=delivery_token,
                                delivered=False,
                            )
                            completed = False
                        else:
                            plan_store.mark_human_decision_recovery_required(
                                plan_id,
                                step_id,
                                reason_code=external_state,
                            )
                            completed = False
                        if external_state != "consumed":
                            if run_error is not None:
                                raise run_error
                            raise RuntimeError(
                                self._human_handoff_recovery_message(
                                    plan_id,
                                    step_id,
                                    external_state,
                                    web_session_id=web_session_id,
                                )
                            )
                        if not completed:
                            raise RuntimeError(
                                "plan-bound human decision delivery completion failed"
                            )
                        handoff = plan_store.get_human_decision_handoff(
                            plan_id,
                            step_id,
                        )
                        if handoff is None or not plan_store.acknowledge_human_decision_handoff(
                            plan_id,
                            step_id,
                            handoff.decision_key,
                        ):
                            raise RuntimeError(
                                "plan-bound human decision handoff acknowledgement failed"
                            )
                        self._mark_human_decision_consumed(
                            agentscope_session_id=agentscope_session_id,
                            decision=decision,
                        )
                    if run_error is not None:
                        raise run_error
                finally:
                    self.clear_run_cancellation(agentscope_session_id, cancellation)
                    await claim.release()

            run_coroutine = run_with_claim()
            try:
                self._spawn_chat_run(run_coroutine, session_id=agentscope_session_id)
            except Exception:
                if plan_bound_handoff:
                    plan_store.finish_human_decision_delivery(
                        plan_id,
                        step_id,
                        decision_key,
                        token=delivery_token,
                        delivered=False,
                    )
                if previous_cancellation is not None:
                    self.register_run_cancellation(
                        agentscope_session_id,
                        previous_cancellation,
                    )
                else:
                    self.clear_run_cancellation(agentscope_session_id, cancellation)
                raise
            if not plan_bound_handoff:
                self._mark_human_decision_consumed(
                    agentscope_session_id=agentscope_session_id,
                    decision=decision,
                )
            claim_handoff = True
            return True
        finally:
            if not claim_handoff:
                await claim.release()

    @staticmethod
    def _human_handoff_recovery_message(
        plan_id: str,
        step_id: str,
        reason_code: str,
        *,
        web_session_id: str,
    ) -> str:
        return (
            f"{reason_code}: human decision handoff {plan_id}/{step_id} requires "
            "controlled recovery via POST "
            f"/api/sessions/{web_session_id}/human-decisions/recovery"
        )

    def _raise_human_handoff_recovery_required(
        self,
        plan_store: SqliteNavigationPlanRepository,
        plan_id: str,
        step_id: str,
        *,
        web_session_id: str,
        reason_code: str,
    ) -> None:
        plan_store.mark_human_decision_recovery_required(
            plan_id,
            step_id,
            reason_code=reason_code,
        )
        raise RuntimeError(
            self._human_handoff_recovery_message(
                plan_id,
                step_id,
                reason_code,
                web_session_id=web_session_id,
            )
        )

    async def recover_human_decision_handoff(
        self,
        web_session_id: str,
        recovery: Any,
    ) -> dict[str, Any]:
        payload = (
            recovery.model_dump(mode="json")
            if hasattr(recovery, "model_dump")
            else dict(recovery)
        )
        if payload.get("action") != "quarantine_and_replan":
            raise ValueError("unsupported human decision recovery action")
        result = self._navigation_plan_store().quarantine_human_decision_handoff(
            str(payload.get("plan_id", "")),
            str(payload.get("step_id", "")),
            expected_web_session_id=web_session_id,
            reason=str(payload.get("reason", "")),
        )
        await self._mark_quarantined_human_decision_consumed(
            web_session_id=web_session_id,
            plan_id=result["plan_id"],
            step_id=result["step_id"],
        )
        return result

    async def _mark_quarantined_human_decision_consumed(
        self,
        *,
        web_session_id: str,
        plan_id: str,
        step_id: str,
    ) -> None:
        mapped = self._web_session_mapping(web_session_id)
        if mapped is None:
            return
        agent_id, agentscope_session_id = mapped
        get_session = getattr(self.storage, "get_session", None)
        if not callable(get_session):
            return
        record = await get_session(self.config.user_id, agent_id, agentscope_session_id)
        state = getattr(record, "state", None)
        reply_id = getattr(state, "reply_id", None)
        if not isinstance(reply_id, str):
            return
        for message in getattr(state, "context", []) or []:
            for tool_call in _tool_call_blocks(message):
                tool_input = _tool_call_input(tool_call)
                if tool_input.get("plan_id") != plan_id or tool_input.get("step_id") != step_id:
                    continue
                self._mark_human_decision_consumed(
                    agentscope_session_id=agentscope_session_id,
                    decision={
                        "reply_id": reply_id,
                        "tool_call_id": str(getattr(tool_call, "id", "")),
                        "action": "quarantined",
                        "request_id": f"{plan_id}:{step_id}",
                    },
                )
                return

    async def _pending_human_decision_tool_name(
        self,
        *,
        agent_id: str,
        agentscope_session_id: str,
        decision: dict[str, Any],
    ) -> str | None:
        get_session = getattr(self.storage, "get_session", None)
        if get_session is None:
            return None

        record = await get_session(self.config.user_id, agent_id, agentscope_session_id)
        if record is None:
            return None

        state = getattr(record, "state", None)
        if getattr(state, "reply_id", None) != decision["reply_id"]:
            return None

        for message in getattr(state, "context", []) or []:
            for tool_call in _tool_call_blocks(message):
                tool_name = getattr(tool_call, "name", None)
                if (
                    getattr(tool_call, "id", None) == decision["tool_call_id"]
                    and tool_name in _HUMAN_DECISION_TOOL_NAMES
                    and _state_value(getattr(tool_call, "state", None))
                    == ToolCallState.SUBMITTED.value
                ):
                    raw_input = getattr(tool_call, "input", None)
                    if isinstance(raw_input, str):
                        try:
                            raw_input = json.loads(raw_input)
                        except json.JSONDecodeError:
                            return None
                    is_plan_bound = isinstance(raw_input, dict) and (
                        "plan_id" in raw_input or "step_id" in raw_input
                    )
                    if is_plan_bound:
                        payload = _human_decision_payload_from_tool_call(
                            tool_call,
                            plan_store=self._navigation_plan_store(),
                        )
                        if payload is None:
                            return None
                        for field in ("plan_id", "step_id"):
                            if decision.get(field) != payload.get(field):
                                return None
                    return str(tool_name)
        return None

    async def _human_decision_external_call_state(
        self,
        *,
        agent_id: str,
        agentscope_session_id: str,
        decision: dict[str, Any],
    ) -> str:
        """Read AgentScope's durable external-call state as the consumption oracle."""
        get_session = getattr(self.storage, "get_session", None)
        if get_session is None:
            return "missing_agentscope_storage"
        record = await get_session(self.config.user_id, agent_id, agentscope_session_id)
        if record is None:
            return "missing_agentscope_session"
        state = getattr(record, "state", None)
        if getattr(state, "reply_id", None) != decision.get("reply_id"):
            return "missing_agentscope_reply"
        for message in getattr(state, "context", []) or []:
            for tool_call in _tool_call_blocks(message):
                if getattr(tool_call, "id", None) != decision.get("tool_call_id"):
                    continue
                if getattr(tool_call, "name", None) not in _HUMAN_DECISION_TOOL_NAMES:
                    return "missing_agentscope_tool_call"
                return (
                    "submitted"
                    if _state_value(getattr(tool_call, "state", None))
                    == ToolCallState.SUBMITTED.value
                    else "consumed"
                )
        return "missing_agentscope_tool_call"

    async def _try_acquire_human_decision_claim(
        self,
        claim_key: str,
    ) -> "_HumanDecisionClaim | None":
        acquire_lock = getattr(self.message_bus, "acquire_lock", None)
        if callable(acquire_lock):
            lock_cm = acquire_lock(claim_key, ttl_secs=600)
            try:
                await asyncio.wait_for(lock_cm.__aenter__(), timeout=0.1)
            except TimeoutError:
                return None

            async def release_distributed() -> None:
                await lock_cm.__aexit__(None, None, None)

            return _HumanDecisionClaim(release_distributed)

        if claim_key in self._active_human_decision_claims:
            return None
        self._active_human_decision_claims.add(claim_key)

        async def release_local() -> None:
            self._active_human_decision_claims.discard(claim_key)

        return _HumanDecisionClaim(release_local)

    def _spawn_chat_run(self, run_coroutine: Any, *, session_id: str) -> None:
        chat_run_registry = getattr(self.app.state, "chat_run_registry", None)
        if chat_run_registry is None:
            run_coroutine.close()
            raise RuntimeError("AgentScope chat_run_registry is not initialized")
        try:
            chat_run_registry.spawn(run_coroutine, session_id=session_id)
        except Exception:
            run_coroutine.close()
            raise

    async def recover_pending_agent_wakeups_once(
        self,
        *,
        max_count: int = 64,
        retry_delays: tuple[float, ...] = _WAKEUP_RECOVERY_RETRY_DELAYS,
    ) -> int:
        """Drain AgentScope wakeups with short retry and wake idle sessions."""
        dequeue_wakeups = getattr(self.message_bus, "dequeue_wakeups", None)
        if not callable(dequeue_wakeups):
            return 0
        try:
            wakeups = await _retry_async(
                lambda: dequeue_wakeups(max_count=max_count),
                retry_delays=retry_delays,
                operation="dequeue AgentScope wakeups",
                on_retry=self._record_redis_retry,
            )
        except Exception:
            _logger.exception("AgentScope wakeup recovery: dequeue failed after retries.")
            return 0

        recovered = 0
        for payload in wakeups:
            if not isinstance(payload, dict):
                continue
            user_id = payload.get("user_id")
            session_id = payload.get("session_id")
            agent_id = payload.get("agent_id")
            if not (
                isinstance(user_id, str)
                and isinstance(session_id, str)
                and isinstance(agent_id, str)
            ):
                continue
            if await self._spawn_idle_agent_run(
                user_id=user_id,
                session_id=session_id,
                agent_id=agent_id,
                source="wakeup_recovery",
            ):
                recovered += 1
        self.recovery_metrics.recovered_wakeup_runs += recovered
        return recovered

    async def recover_orphan_agent_inboxes_once(self) -> int:
        """Wake idle mapped AgentScope sessions that still have inbox entries."""
        session_ids = await self._pending_inbox_session_ids()
        recovered = 0
        for session_id in session_ids:
            agent_id = self._agent_id_for_agentscope_session(session_id)
            if agent_id is None:
                continue
            if await self._spawn_idle_agent_run(
                user_id=self.config.user_id,
                session_id=session_id,
                agent_id=agent_id,
                source="orphan_inbox_recovery",
            ):
                recovered += 1
        self.recovery_metrics.recovered_orphan_inbox_runs += recovered
        return recovered

    async def record_recovery_diagnostics_once(
        self,
        *,
        event_loop_lag_seconds: float,
    ) -> AgentScopeRecoveryMetrics:
        wakeup_queue_length = await self._wakeup_queue_length()
        inbox_residual_count = await self._inbox_residual_count()
        self.recovery_metrics.wakeup_queue_length = wakeup_queue_length
        self.recovery_metrics.inbox_residual_count = inbox_residual_count
        self.recovery_metrics.event_loop_lag_seconds = event_loop_lag_seconds
        _logger.info(
            "AgentScope recovery diagnostics: redis_timeouts=%d "
            "wakeup_queue_length=%s inbox_residual_count=%s "
            "event_loop_lag_seconds=%.3f recovered_wakeup_runs=%d "
            "recovered_orphan_inbox_runs=%d",
            self.recovery_metrics.redis_timeout_count,
            wakeup_queue_length,
            inbox_residual_count,
            event_loop_lag_seconds,
            self.recovery_metrics.recovered_wakeup_runs,
            self.recovery_metrics.recovered_orphan_inbox_runs,
        )
        return self.recovery_metrics

    async def run_agent_wakeup_recovery_loop(
        self,
        *,
        interval_secs: float = _WAKEUP_RECOVERY_INTERVAL_SECS,
    ) -> None:
        """Periodically recover stranded AgentScope wakeups and inbox entries."""
        loop = asyncio.get_running_loop()
        expected_wakeup = loop.time()
        while True:
            now = loop.time()
            event_loop_lag_seconds = max(0.0, now - expected_wakeup)
            try:
                await self.record_recovery_diagnostics_once(
                    event_loop_lag_seconds=event_loop_lag_seconds,
                )
                await self.recover_pending_agent_wakeups_once()
                await self.recover_orphan_agent_inboxes_once()
            except Exception:
                _logger.exception("AgentScope wakeup recovery loop failed.")
            expected_wakeup = loop.time() + interval_secs
            await asyncio.sleep(interval_secs)

    def _record_redis_retry(self, exc: BaseException, _attempt: int) -> None:
        if _looks_like_timeout_error(exc):
            self.recovery_metrics.redis_timeout_count += 1

    async def _spawn_idle_agent_run(
        self,
        *,
        user_id: str,
        session_id: str,
        agent_id: str,
        source: str,
    ) -> bool:
        if await self.message_bus.session_is_running(session_id):
            return False
        if self._is_local_agent_run_active(session_id):
            return False

        get_session = getattr(self.storage, "get_session", None)
        if callable(get_session):
            record = await get_session(user_id, agent_id, session_id)
            if record is None:
                return False

        try:
            self._spawn_chat_run(
                self.app.state.chat_service.run(
                    user_id=user_id,
                    session_id=session_id,
                    agent_id=agent_id,
                    input_msg=None,
                ),
                session_id=session_id,
            )
        except RuntimeError:
            _logger.debug(
                "AgentScope wakeup recovery skipped duplicate run: "
                "session_id=%s source=%s",
                session_id,
                source,
            )
            return False
        _logger.info(
            "AgentScope wakeup recovery spawned idle session run: "
            "session_id=%s agent_id=%s source=%s",
            session_id,
            agent_id,
            source,
        )
        return True

    async def _pending_inbox_session_ids(self) -> list[str]:
        list_inbox_session_ids = getattr(self.message_bus, "list_inbox_session_ids", None)
        if callable(list_inbox_session_ids):
            return [
                session_id
                for session_id in await list_inbox_session_ids()
                if isinstance(session_id, str) and session_id
            ]

        get_client = getattr(self.message_bus, "get_client", None)
        if not callable(get_client):
            return []
        client = get_client()
        if client is None:
            return []
        template = str(getattr(self.message_bus, "_INBOX_KEY", "agentscope:inbox:{sid}"))
        try:
            match_key = template.format(sid="*")
        except KeyError:
            return []

        session_ids: list[str] = []
        async for raw_key in client.scan_iter(match=match_key, count=100):
            key = raw_key.decode() if isinstance(raw_key, bytes) else str(raw_key)
            try:
                pending = int(await client.xlen(key))
            except Exception:
                _logger.exception("AgentScope orphan inbox recovery: XLEN failed for %s.", key)
                continue
            if pending <= 0:
                continue
            session_id = _session_id_from_inbox_key(key, template)
            if session_id is not None:
                session_ids.append(session_id)
        return session_ids

    async def _wakeup_queue_length(self) -> int | None:
        queue_length = getattr(self.message_bus, "wakeup_queue_length", None)
        if callable(queue_length):
            return int(await queue_length())
        get_client = getattr(self.message_bus, "get_client", None)
        if not callable(get_client):
            return None
        client = get_client()
        if client is None:
            return None
        key = str(getattr(self.message_bus, "_WAKEUP_QUEUE_KEY", "agentscope:wakeups"))
        return int(await client.xlen(key))

    async def _inbox_residual_count(self) -> int | None:
        residual_count = getattr(self.message_bus, "inbox_residual_count", None)
        if callable(residual_count):
            return int(await residual_count())
        get_client = getattr(self.message_bus, "get_client", None)
        if not callable(get_client):
            return None
        client = get_client()
        if client is None:
            return None
        template = str(getattr(self.message_bus, "_INBOX_KEY", "agentscope:inbox:{sid}"))
        try:
            match_key = template.format(sid="*")
        except KeyError:
            return None
        total = 0
        async for raw_key in client.scan_iter(match=match_key, count=100):
            key = raw_key.decode() if isinstance(raw_key, bytes) else str(raw_key)
            total += int(await client.xlen(key))
        return total

    def _agent_id_for_agentscope_session(self, agentscope_session_id: str) -> str | None:
        if self.web_session_store is not None:
            get_mapping = getattr(
                self.web_session_store,
                "get_agentscope_session_mapping_by_agentscope_session",
                None,
            )
            if callable(get_mapping):
                mapping = get_mapping(agentscope_session_id)
                if mapping is not None:
                    self.web_sessions[mapping.web_session_id] = (
                        mapping.agent_id,
                        mapping.agentscope_session_id,
                    )
                    return mapping.agent_id

        for agent_id in (
            self.config.navigation_agent_id,
            self.config.main_router_agent_id,
        ):
            if agentscope_session_id.endswith(f"__{agent_id}"):
                return agent_id
        return None

    async def _event_log_tail_cursor(self, agentscope_session_id: str) -> str | None:
        read_events = getattr(self.message_bus, "session_read_events", None)
        if read_events is None:
            return None
        entries = await read_events(
            agentscope_session_id,
            since=self._event_cursor(agentscope_session_id),
        )
        if not entries:
            return None
        return entries[-1][0]

    async def subscribe_web_session_events(self, *, web_session_id: str):
        mapped = self._web_session_mapping(web_session_id)
        if mapped is None:
            return

        agent_id, agentscope_session_id = mapped
        translated_events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        live_events: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        live_ready = asyncio.Event()
        scope = EventEmitter(CallbackEventSink(translated_events.put_nowait)).scope(
            "agentscope",
            run_id=agentscope_session_id,
        )
        adapter = AgentScopeEventAdapter(
            scope,
            emit_text_events=True,
            emit_final_events=True,
        )
        seen_entry_ids: set[str] = set()
        seen_pending_decision_states: dict[tuple[str, str], str] = {}
        saw_event = False
        saw_reply_end = False
        saw_running = False
        startup_deadline = asyncio.get_running_loop().time() + _EVENT_STARTUP_GRACE_SECS
        events_key = _session_events_key(self.message_bus, agentscope_session_id)

        async def feed_live_events() -> None:
            try:
                async for event in self.message_bus.subscribe(
                    events_key,
                    on_ready=live_ready.set,
                ):
                    await live_events.put(event)
            finally:
                await live_events.put(None)

        feeder_task = asyncio.create_task(
            feed_live_events(),
            name=f"agentscope-web-events:{agentscope_session_id}",
        )

        def accept_raw_event(raw_event: dict[str, Any]) -> list[dict[str, Any]]:
            adapter.accept(_to_attribute_event(_strip_internal_event_fields(raw_event)))
            events = []
            while not translated_events.empty():
                event = translated_events.get_nowait()
                payload = event.get("payload")
                if (
                    event.get("type") == "human_decision_required"
                    and isinstance(payload, dict)
                    and isinstance(payload.get("plan_id"), str)
                    and isinstance(payload.get("step_id"), str)
                ):
                    event = _enrich_plan_human_decision_event(
                        event,
                        plan_store=self._navigation_plan_store(),
                    )
                    if event is None:
                        continue
                    enriched_payload = event.get("payload")
                    if (
                        isinstance(enriched_payload, dict)
                        and enriched_payload.get("recovery_required") is True
                    ):
                        enriched_payload["submission_disabled"] = True
                        enriched_payload["recovery_endpoint"] = (
                            f"/api/sessions/{web_session_id}/human-decisions/recovery"
                        )
                events.append(event)
            return events

        def should_emit_human_decision(event: dict[str, Any] | None) -> bool:
            identity = _human_decision_event_key(event)
            if not identity:
                return event is not None
            payload = event.get("payload") if event is not None else None
            state = (
                "recovery_required"
                if isinstance(payload, dict) and payload.get("recovery_required") is True
                else "normal"
            )
            previous = seen_pending_decision_states.get(identity)
            if previous == state or (
                previous == "recovery_required" and state == "normal"
            ):
                return False
            seen_pending_decision_states[identity] = state
            return True

        try:
            with suppress(TimeoutError):
                await asyncio.wait_for(live_ready.wait(), timeout=_EVENT_STARTUP_GRACE_SECS)

            pending_event = await self._pending_human_decision_event(
                web_session_id=web_session_id,
                agent_id=agent_id,
                agentscope_session_id=agentscope_session_id,
            )
            if pending_event is not None and should_emit_human_decision(pending_event):
                yield pending_event

            cursor = self._event_cursor(agentscope_session_id)
            for entry_id, raw_event in await self.message_bus.session_read_events(
                agentscope_session_id,
                since=cursor,
            ):
                if not self._is_new_event(agentscope_session_id, entry_id):
                    continue
                seen_entry_ids.add(entry_id)
                saw_event = True
                if _raw_event_type(raw_event) == "REPLY_END":
                    saw_reply_end = True
                for event in accept_raw_event(raw_event):
                    decision_key = (
                        _human_decision_event_key(event)
                        if event.get("type") == "human_decision_required"
                        else ""
                    )
                    if decision_key:
                        if not should_emit_human_decision(event):
                            continue
                    yield event
                self._remember_event_cursor(
                    agentscope_session_id,
                    entry_id,
                )

            while True:
                running = bool(await self.message_bus.session_is_running(agentscope_session_id))
                local_running = self._is_local_agent_run_active(agentscope_session_id)
                saw_running = saw_running or running or local_running

                live_feed_finished = False
                try:
                    raw_event = await asyncio.wait_for(
                        live_events.get(),
                        timeout=_EVENT_IDLE_POLL_SECS,
                    )
                except TimeoutError:
                    raw_event = None
                else:
                    if raw_event is None:
                        live_feed_finished = True
                    else:
                        entry_id = _raw_event_entry_id(raw_event)
                        if entry_id and entry_id in seen_entry_ids:
                            continue
                        if entry_id:
                            if not self._is_new_event(agentscope_session_id, entry_id):
                                continue
                            seen_entry_ids.add(entry_id)
                        saw_event = True
                        if _raw_event_type(raw_event) == "REPLY_END":
                            saw_reply_end = True
                        for event in accept_raw_event(raw_event):
                            decision_key = (
                                _human_decision_event_key(event)
                                if event.get("type") == "human_decision_required"
                                else ""
                            )
                            if decision_key:
                                if not should_emit_human_decision(event):
                                    continue
                            yield event
                        if entry_id:
                            self._remember_event_cursor(
                                agentscope_session_id,
                                entry_id,
                            )
                        continue

                pending_event = await self._pending_human_decision_event(
                    web_session_id=web_session_id,
                    agent_id=agent_id,
                    agentscope_session_id=agentscope_session_id,
                )
                if pending_event is not None and should_emit_human_decision(pending_event):
                    saw_event = True
                    yield pending_event
                    continue
                if live_feed_finished:
                    break

                now = asyncio.get_running_loop().time()
                if running or local_running:
                    continue
                if saw_reply_end:
                    break
                if saw_event and saw_running:
                    break
                if now >= startup_deadline:
                    break
        finally:
            feeder_task.cancel()
            with suppress(asyncio.CancelledError):
                await feeder_task

    def _is_local_agent_run_active(self, agentscope_session_id: str) -> bool:
        if self.run_cancellation(agentscope_session_id) is not None:
            return True
        registry = getattr(self.app.state, "chat_run_registry", None)
        get_task = getattr(registry, "get", None)
        if not callable(get_task):
            return False
        task = get_task(agentscope_session_id)
        return bool(task is not None and not task.done())

    def _is_new_event(self, agentscope_session_id: str, entry_id: str) -> bool:
        cursor = self._event_cursor(agentscope_session_id)
        return cursor is None or _stream_id_is_newer(entry_id, cursor)

    def _remember_event_cursor(
        self,
        agentscope_session_id: str,
        entry_id: str,
    ) -> None:
        if self._is_new_event(agentscope_session_id, entry_id):
            self.event_cursors[agentscope_session_id] = entry_id
            self._save_web_session_event_cursor(agentscope_session_id, entry_id)

    def _event_cursor(self, agentscope_session_id: str) -> str | None:
        cursor = self.event_cursors.get(agentscope_session_id)
        if cursor is not None:
            return cursor
        mapping = self._web_session_mapping_for_agentscope_session(agentscope_session_id)
        if mapping is None:
            return None
        _agent_id, _session_id, persisted_cursor = mapping
        if persisted_cursor:
            self.event_cursors[agentscope_session_id] = persisted_cursor
        return persisted_cursor

    def _web_session_mapping(self, web_session_id: str) -> tuple[str, str] | None:
        mapped = self.web_sessions.get(web_session_id)
        if mapped is not None:
            return mapped
        persisted = self._load_web_session_mapping(web_session_id)
        if persisted is not None:
            self.web_sessions[web_session_id] = persisted
        return persisted

    def _load_web_session_mapping(
        self,
        web_session_id: str,
        *,
        agent_id: str | None = None,
    ) -> tuple[str, str] | None:
        if self.web_session_store is None:
            return None
        if agent_id is not None:
            get_mapping = getattr(
                self.web_session_store,
                "get_agentscope_session_mapping_for_agent",
                None,
            )
            if callable(get_mapping):
                mapping = get_mapping(web_session_id, agent_id)
                if mapping is None:
                    return None
                return mapping.agent_id, mapping.agentscope_session_id
        get_mapping = getattr(self.web_session_store, "get_agentscope_session_mapping", None)
        if not callable(get_mapping):
            return None
        mapping = get_mapping(web_session_id)
        if mapping is None:
            return None
        return mapping.agent_id, mapping.agentscope_session_id

    def _web_session_mapping_for_agentscope_session(
        self,
        agentscope_session_id: str,
    ) -> tuple[str, str, str | None] | None:
        if self.web_session_store is None:
            return None
        get_mapping = getattr(
            self.web_session_store,
            "get_agentscope_session_mapping_by_agentscope_session",
            None,
        )
        if not callable(get_mapping):
            return None
        mapping = get_mapping(agentscope_session_id)
        if mapping is None:
            return None
        return mapping.agent_id, mapping.agentscope_session_id, mapping.event_cursor

    def _save_web_session_mapping(
        self,
        web_session_id: str,
        agent_id: str,
        agentscope_session_id: str,
    ) -> None:
        if self.web_session_store is None:
            return
        save_mapping = getattr(self.web_session_store, "save_agentscope_session_mapping", None)
        if callable(save_mapping):
            save_mapping(web_session_id, agent_id=agent_id, agentscope_session_id=agentscope_session_id)

    def _restore_web_session_mapping(
        self,
        web_session_id: str,
        previous_mapping: tuple[str, str] | None,
    ) -> None:
        if previous_mapping is None:
            self.web_sessions.pop(web_session_id, None)
            agent_id = None
            agentscope_session_id = None
        else:
            self.web_sessions[web_session_id] = previous_mapping
            agent_id, agentscope_session_id = previous_mapping
        if self.web_session_store is None:
            return
        restore_mapping = getattr(
            self.web_session_store,
            "restore_agentscope_session_mapping",
            None,
        )
        if callable(restore_mapping):
            restore_mapping(
                web_session_id,
                agent_id=agent_id,
                agentscope_session_id=agentscope_session_id,
            )
        elif previous_mapping is not None:
            self._save_web_session_mapping(
                web_session_id,
                agent_id,
                agentscope_session_id,
            )

    def _save_web_session_event_cursor(self, agentscope_session_id: str, cursor: str) -> None:
        if self.web_session_store is None:
            return
        save_cursor = getattr(self.web_session_store, "save_agentscope_event_cursor", None)
        if callable(save_cursor):
            save_cursor(agentscope_session_id, cursor)

    async def _pending_human_decision_event(
        self,
        *,
        web_session_id: str,
        agent_id: str,
        agentscope_session_id: str,
    ) -> dict[str, Any] | None:
        get_session = getattr(self.storage, "get_session", None)
        if get_session is None:
            return None
        record = await get_session(self.config.user_id, agent_id, agentscope_session_id)
        if record is None:
            return None
        state = getattr(record, "state", None)
        reply_id = getattr(state, "reply_id", None)
        if not reply_id:
            return None
        for message in getattr(state, "context", []) or []:
            for tool_call in _tool_call_blocks(message):
                if (
                    getattr(tool_call, "name", None) in _HUMAN_DECISION_TOOL_NAMES
                    and _state_value(getattr(tool_call, "state", None))
                    == ToolCallState.SUBMITTED.value
                ):
                    payload = _human_decision_payload_from_tool_call(
                        tool_call,
                        plan_store=self._navigation_plan_store(),
                    )
                    if payload is None:
                        tool_input = _tool_call_input(tool_call)
                        plan_id = tool_input.get("plan_id")
                        step_id = tool_input.get("step_id")
                        if isinstance(plan_id, str) and isinstance(step_id, str):
                            handoff = self._navigation_plan_store().get_human_decision_handoff(
                                plan_id, step_id
                            )
                            if handoff is not None and handoff.status == "quarantined":
                                self._mark_human_decision_consumed(
                                    agentscope_session_id=agentscope_session_id,
                                    decision={
                                        "reply_id": reply_id,
                                        "tool_call_id": str(getattr(tool_call, "id", "")),
                                        "action": "quarantined",
                                        "request_id": f"{plan_id}:{step_id}",
                                    },
                                )
                        continue
                    claim_key = _human_decision_claim_key(
                        agentscope_session_id,
                        {
                            "reply_id": reply_id,
                            "tool_call_id": getattr(tool_call, "id", ""),
                        },
                    )
                    if self._is_human_decision_consumed(
                        agentscope_session_id=agentscope_session_id,
                        reply_id=reply_id,
                        tool_call_id=getattr(tool_call, "id", ""),
                    ):
                        continue
                    if await self._is_human_decision_claim_active(claim_key):
                        continue
                    payload["reply_id"] = reply_id
                    payload["tool_call_id"] = getattr(tool_call, "id", "")
                    if payload.get("recovery_required") is True:
                        payload["submission_disabled"] = True
                        payload["recovery_endpoint"] = (
                            f"/api/sessions/{web_session_id}/human-decisions/recovery"
                        )
                    return {
                        "type": "human_decision_required",
                        "source": "NavigationDataAgent",
                        "run_id": agentscope_session_id,
                        "parent_run_id": None,
                        "payload": payload,
                    }
        return None

    async def _is_human_decision_claim_active(self, claim_key: str) -> bool:
        if claim_key in self._active_human_decision_claims:
            return True
        is_locked = getattr(self.message_bus, "is_locked", None)
        if callable(is_locked):
            return bool(await is_locked(claim_key))
        return False

    def _mark_human_decision_consumed(
        self,
        *,
        agentscope_session_id: str,
        decision: dict[str, Any],
    ) -> None:
        if self.web_session_store is None:
            return
        mark_consumed = getattr(self.web_session_store, "mark_human_decision_consumed", None)
        if not callable(mark_consumed):
            return
        mark_consumed(
            agentscope_session_id=agentscope_session_id,
            reply_id=decision["reply_id"],
            tool_call_id=decision["tool_call_id"],
            action=decision["action"],
            request_id=decision.get("request_id"),
        )

    def _is_human_decision_consumed(
        self,
        *,
        agentscope_session_id: str,
        reply_id: str,
        tool_call_id: Any,
    ) -> bool:
        if self.web_session_store is None:
            return False
        is_consumed = getattr(self.web_session_store, "is_human_decision_consumed", None)
        if not callable(is_consumed):
            return False
        return bool(
            is_consumed(
                agentscope_session_id=agentscope_session_id,
                reply_id=reply_id,
                tool_call_id=str(tool_call_id),
            )
        )


def _to_attribute_event(value: Any) -> Any:
    if isinstance(value, dict):
        return SimpleNamespace(
            **{key: _to_attribute_event(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return [_to_attribute_event(item) for item in value]
    return value


class _HumanDecisionClaim:
    def __init__(self, release_callback: Any) -> None:
        self._release_callback = release_callback
        self._released = False

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        await self._release_callback()


def _tool_call_blocks(message: Any) -> list[Any]:
    get_content_blocks = getattr(message, "get_content_blocks", None)
    if callable(get_content_blocks):
        return list(get_content_blocks("tool_call"))
    return [
        block
        for block in getattr(message, "content", []) or []
        if getattr(block, "type", None) == "tool_call"
    ]


def _tool_call_input(tool_call: Any) -> dict[str, Any]:
    payload = getattr(tool_call, "input", None)
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return {}
    return payload if isinstance(payload, dict) else {}


def _state_value(value: Any) -> str:
    raw_value = getattr(value, "value", value)
    return str(raw_value)


def _human_decision_claim_key(agentscope_session_id: str, decision: dict[str, Any]) -> str:
    return (
        "vla:human-decision:"
        f"{agentscope_session_id}:{decision['reply_id']}:{decision['tool_call_id']}"
    )


def _durable_plan_decision(decision: dict[str, Any]) -> dict[str, Any]:
    return {
        "action": decision.get("action"),
        "text": decision.get("text"),
        "request_id": decision.get("request_id"),
        "plan_id": decision.get("plan_id"),
        "step_id": decision.get("step_id"),
    }


def _human_decision_payload_from_tool_call(
    tool_call: Any,
    *,
    plan_store: SqliteNavigationPlanRepository | None = None,
) -> dict[str, Any] | None:
    tool_name = getattr(tool_call, "name", None)
    tool_input = getattr(tool_call, "input", None)
    if isinstance(tool_input, str):
        try:
            tool_input = json.loads(tool_input)
        except json.JSONDecodeError:
            return None
    if not isinstance(tool_input, dict):
        return None

    plan_id = tool_input.get("plan_id")
    step_id = tool_input.get("step_id")
    if isinstance(plan_id, str) and isinstance(step_id, str):
        if plan_store is None:
            return None
        plan = plan_store.get(plan_id)
        current = plan_store.get_current_step(plan_id) if plan is not None else None
        handoff = plan_store.get_human_decision_handoff(plan_id, step_id)
        if handoff is not None and handoff.status == "quarantined":
            return None
        if (
            plan is None
            or (
                handoff is None
                and (
                    plan.status != "active"
                    or current is None
                    or current["step"]["step_id"] != step_id
                    or current["step"]["action"]
                    != "confirm_navigation_calibration_params"
                )
            )
        ):
            return None
        calibration = getattr(getattr(plan.plan, "decisions", None), "calibration", None)
        selected_source = getattr(calibration, "selected_sensor_source", None)
        if not isinstance(selected_source, str):
            return None
        payload = {
            "request_id": f"{plan_id}:{step_id}",
            "decision_type": "camera_params",
            "summary": (
                "请确认本计划选定的相机标定参数："
                f"{selected_source[:1000]}。确认后将继续执行下一计划步骤。"
            ),
            "plan_id": plan_id,
            "step_id": step_id,
        }
        if handoff is not None and handoff.status == "recovery_required":
            payload["recovery_required"] = True
            payload["recovery"] = {
                "action": "quarantine_and_replan",
                "plan_id": plan_id,
                "step_id": step_id,
            }
        return payload

    request_id = tool_input.get("request_id")
    summary = tool_input.get("summary")
    if not isinstance(request_id, str) or not isinstance(summary, str):
        return None
    decision_type = tool_input.get("decision_type")
    if not isinstance(decision_type, str) or not decision_type:
        decision_type = "other"
    return {
        "request_id": request_id,
        "decision_type": decision_type,
        "summary": summary,
    }


def _enrich_plan_human_decision_event(
    event: dict[str, Any],
    *,
    plan_store: SqliteNavigationPlanRepository,
) -> dict[str, Any] | None:
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return event
    plan_id = payload.get("plan_id")
    step_id = payload.get("step_id")
    if not isinstance(plan_id, str) or not isinstance(step_id, str):
        return event
    handoff = plan_store.get_human_decision_handoff(plan_id, step_id)
    if handoff is not None and handoff.status == "quarantined":
        return None
    metadata = _human_decision_payload_from_tool_call(
        SimpleNamespace(
            name="request_human_decision",
            input={"plan_id": plan_id, "step_id": step_id},
        ),
        plan_store=plan_store,
    )
    if metadata is None:
        return event
    enriched = dict(event)
    enriched["payload"] = {
        **metadata,
        "reply_id": payload.get("reply_id", ""),
        "tool_call_id": payload.get("tool_call_id", ""),
    }
    return enriched


def _human_decision_tool_output(tool_name: str, decision: dict[str, Any]) -> dict[str, Any]:
    output = {
        "action": decision["action"],
        "text": decision.get("text"),
        "request_id": decision["request_id"],
    }
    if isinstance(decision.get("plan_id"), str) and isinstance(decision.get("step_id"), str):
        output["plan_id"] = decision["plan_id"]
        output["step_id"] = decision["step_id"]
    if not _is_calibration_confirmation_decision(decision):
        return output
    output["decision_type"] = "camera_params"
    action = decision["action"]
    if action == "confirm":
        output.update(
            {
                "ok": True,
                "tool_name": "confirm_navigation_calibration_params",
                "message": "Camera parameters confirmed by user.",
            }
        )
    elif action == "stop":
        output.update(
            {
                "ok": False,
                "tool_name": "confirm_navigation_calibration_params",
                "message": "Navigation processing stopped by user before calibration confirmation.",
                "error_type": "calibration_params_not_confirmed",
            }
        )
    else:
        output.update(
            {
                "ok": False,
                "tool_name": "confirm_navigation_calibration_params",
                "message": "User provided guidance before calibration confirmation.",
                "error_type": "human_guidance_required",
            }
        )
    return output


def _is_calibration_confirmation_decision(decision: dict[str, Any]) -> bool:
    request_id = decision.get("request_id")
    return (
        (
            isinstance(decision.get("plan_id"), str)
            and isinstance(decision.get("step_id"), str)
        )
        or (
            isinstance(request_id, str)
            and request_id.startswith("confirm_navigation_calibration_params:")
        )
    )


def _human_decision_event_key(
    event: dict[str, Any] | None,
) -> tuple[str, str] | None:
    if event is None:
        return None
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return None
    reply_id = payload.get("reply_id")
    tool_call_id = payload.get("tool_call_id")
    if not isinstance(reply_id, str) or not reply_id:
        return None
    if not isinstance(tool_call_id, str) or not tool_call_id:
        return None
    return reply_id, tool_call_id


def _session_events_key(message_bus: Any, session_id: str) -> str:
    template = getattr(
        message_bus,
        "_SESSION_EVENTS_KEY",
        "agentscope:session:events:{sid}",
    )
    return str(template).format(sid=session_id)


async def _retry_async(
    operation_factory: Any,
    *,
    retry_delays: tuple[float, ...],
    operation: str,
    on_retry: Any | None = None,
) -> Any:
    for attempt in range(len(retry_delays) + 1):
        try:
            return await operation_factory()
        except Exception as exc:
            if attempt >= len(retry_delays):
                raise
            delay = retry_delays[attempt]
            if callable(on_retry):
                on_retry(exc, attempt + 1)
            _logger.warning(
                "Retrying %s after transient failure: attempt=%d delay=%.2fs",
                operation,
                attempt + 1,
                delay,
                exc_info=True,
            )
            if delay > 0:
                await asyncio.sleep(delay)
    raise RuntimeError("unreachable retry state")


def _looks_like_timeout_error(exc: BaseException) -> bool:
    return isinstance(exc, TimeoutError) or "Timeout" in type(exc).__name__


def _session_id_from_inbox_key(key: str, template: str) -> str | None:
    marker = "{sid}"
    if marker not in template:
        return None
    prefix, suffix = template.split(marker, 1)
    if not key.startswith(prefix):
        return None
    if suffix and not key.endswith(suffix):
        return None
    end = len(key) - len(suffix) if suffix else len(key)
    session_id = key[len(prefix):end]
    return session_id or None


def _strip_internal_event_fields(event: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in event.items() if key != "_entry_id"}


def _raw_event_entry_id(event: dict[str, Any]) -> str | None:
    entry_id = event.get("_entry_id")
    return entry_id if isinstance(entry_id, str) and entry_id else None


def _raw_event_type(event: dict[str, Any]) -> str:
    event_type = event.get("type")
    return str(event_type) if event_type is not None else ""


def _stream_id_is_newer(entry_id: str, cursor: str) -> bool:
    entry_parts = _stream_id_parts(entry_id)
    cursor_parts = _stream_id_parts(cursor)
    if entry_parts is None or cursor_parts is None:
        return entry_id > cursor
    return entry_parts > cursor_parts


def _stream_id_parts(entry_id: str) -> tuple[int, int] | None:
    try:
        first, second = entry_id.split("-", 1)
        return int(first), int(second)
    except ValueError:
        return None


def _web_session_id_from_agentscope_session(session_id: str, *, agent_id: str) -> str:
    suffix = f"__{agent_id}"
    if session_id.endswith(suffix):
        return session_id[: -len(suffix)]
    return session_id


def _handoff_error(message: str, payload: dict[str, Any]) -> ToolChunk:
    return ToolChunk(
        content=[TextBlock(text=message)],
        state=ToolResultState.ERROR,
        metadata=payload,
    )


def _date_from_navigation_target(target: str) -> str | None:
    stripped = target.strip()
    if re.fullmatch(r"[0-9]{8}", stripped):
        return stripped
    match = re.search(r"(?<![0-9])([0-9]{8})(?![0-9])", stripped)
    if match is None:
        return None
    return match.group(1)


def _dates_from_text(text: str) -> list[str]:
    return re.findall(r"(?<![0-9])([0-9]{8})(?![0-9])", text)


def _clip_prefix_dates(clips: list[str]) -> set[str]:
    return {
        match.group(1)
        for clip in clips
        if (match := re.match(r"^([0-9]{8})[_-]", clip.strip()))
    }


def _navigation_handoff_date(*, request: str, target: str, clips: list[str]) -> str | None:
    target_date = _date_from_navigation_target(target)
    request_dates = _dates_from_text(request)
    clip_dates = _clip_prefix_dates(clips)
    non_clip_request_dates = [date for date in request_dates if date not in clip_dates]
    if non_clip_request_dates:
        return non_clip_request_dates[0]
    return target_date or (request_dates[0] if request_dates else None)


def _navigation_handoff_message(
    *,
    request: str,
    target: str,
    date: str,
    scene_mode: str | None,
    clips: list[str],
    reason: str,
    response_language: str | None,
    dry_run: bool = False,
) -> str:
    clip_text = ", ".join(clips) if clips else "all"
    language = _resolve_response_language(response_language, request)
    scene_mode_text = scene_mode or "unknown"
    payload = {
        "request": request,
        "target": target,
        "date": date,
        "scene_mode": _navigation_scene_mode_for_request(scene_mode),
        "clips": clips,
        "segments": clips or None,
        "reason": reason,
        "response_language": language,
        "dry_run": bool(dry_run),
    }
    structured_lines = [
        "Structured handoff JSON:",
        json.dumps(payload, ensure_ascii=False),
    ]
    if language == "Chinese":
        return "\n".join(
            [
                "导航数据处理请求：",
                f"- 用户原始请求: {request}",
                f"- 处理目标: {target}",
                f"- 场景模式: {scene_mode_text}",
                f"- clips: {clip_text}",
                f"- 转交原因: {reason}",
                f"- 回复语言: {language}",
                "请始终使用中文回复用户。",
                *structured_lines,
            ]
        )
    return "\n".join(
        [
            "Navigation data processing request:",
            f"- request: {request}",
            f"- target: {target}",
            f"- scene_mode: {scene_mode_text}",
            f"- clips: {clip_text}",
            f"- reason: {reason}",
            f"- response_language: {language}",
            f"Always respond to the user in {language}.",
            *structured_lines,
        ]
    )


def _resolve_response_language(explicit: str | None, request: str) -> str:
    language = str(explicit or "").strip()
    if language:
        lowered = language.lower()
        if lowered in {"zh", "zh-cn", "chinese", "中文", "汉语"}:
            return "Chinese"
        if lowered in {"en", "en-us", "english", "英文", "英语"}:
            return "English"
        return language
    if any("\u4e00" <= char <= "\u9fff" for char in request):
        return "Chinese"
    return "English"


class NavigationHandoffTool(ToolBase):
    name = "start_navigation_data_task"
    description = (
        "Start the dedicated VLA navigation data processing agent after you "
        "have determined that the user wants to begin a concrete navigation "
        "data task. Do not use this for capability questions or ordinary "
        "conversation."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "request": {
                "type": "string",
                "description": "The user's concrete navigation task request with relevant context.",
            },
            "target": {
                "type": "string",
                "description": "The concrete date, path, or dataset target to process.",
            },
            "date": {
                "type": "string",
                "description": (
                    "The navigation dataset date in YYYYMMDD format. This is the "
                    "directory date under raw_data/clip_data/finish_data. Do not "
                    "derive it from clip name prefixes when the user's requested "
                    "dataset date is different."
                ),
            },
            "scene_mode": {
                "type": "string",
                "enum": ["indoor", "outdoor", "unknown"],
                "description": (
                    "Optional indoor/outdoor context. Use unknown or omit the field "
                    "when scene mode is not available; missing or unknown scene mode "
                    "must not block extract/sync."
                ),
            },
            "clips": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional specific clips to process. Omit or use an empty "
                    "array when the user did not specify clips."
                ),
            },
            "reason": {
                "type": "string",
                "description": "Brief reason why this should be handed off to the navigation data agent.",
            },
            "missing_fields": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": ["request", "target", "date", "clips", "other"],
                },
                "description": (
                    "Fields that are still missing. Do not include scene_mode; only "
                    "date/path/target style gaps should block handoff."
                ),
            },
            "confidence": {
                "type": "string",
                "enum": ["low", "medium", "high"],
                "description": "Confidence that this is a concrete navigation data processing task.",
            },
            "response_language": {
                "type": "string",
                "description": "The language the user is using and expects for responses, such as Chinese or English.",
            },
            "dry_run": {
                "type": "boolean",
                "description": "Whether the navigation task should execute in dry-run mode.",
            },
        },
        "required": [
            "request",
            "target",
            "date",
            "reason",
            "missing_fields",
            "confidence",
            "response_language",
        ],
        "additionalProperties": False,
    }
    is_concurrency_safe = False
    is_read_only = False
    is_external_tool = False

    def __init__(self, *, runtime: AgentScopeRuntime, web_session_id: str) -> None:
        self._runtime = runtime
        self._web_session_id = web_session_id

    async def check_permissions(
        self,
        tool_input: dict[str, Any],
        context: object,
    ) -> PermissionDecision:
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message="Navigation handoff is allowed.",
        )

    async def __call__(
        self,
        request: str,
        target: str,
        date: str,
        reason: str,
        missing_fields: list[str],
        confidence: str,
        response_language: str | None = None,
        clips: list[str] | None = None,
        scene_mode: str | None = None,
        dry_run: bool = False,
    ) -> ToolChunk:
        normalized_clips = list(clips or [])
        normalized_language = _resolve_response_language(response_language, request)
        normalized_scene_mode = scene_mode if scene_mode in {"indoor", "outdoor"} else "unknown"
        payload = {
            "web_session_id": self._web_session_id,
            "request": request,
            "target": target,
            "date": date,
            "scene_mode": normalized_scene_mode,
            "clips": normalized_clips,
            "reason": reason,
            "missing_fields": list(missing_fields),
            "confidence": confidence,
            "response_language": normalized_language,
            "dry_run": bool(dry_run),
            "started": False,
        }

        if confidence not in {"medium", "high"}:
            self._record_handoff(payload)
            return _handoff_error(
                "Navigation handoff rejected because confidence must be medium or high.",
                payload,
            )
        if missing_fields:
            self._record_handoff(payload)
            return _handoff_error(
                "Navigation handoff rejected because missing_fields is not empty.",
                payload,
            )
        if not target.strip():
            self._record_handoff(payload)
            return _handoff_error("Navigation handoff rejected because target is missing.", payload)
        if not re.fullmatch(r"[0-9]{8}", date.strip()):
            self._record_handoff(payload)
            return _handoff_error(
                "Navigation handoff rejected because date must be a YYYYMMDD dataset date.",
                payload,
            )

        navigation_request = _navigation_handoff_message(
            request=request,
            target=target,
            date=date.strip(),
            scene_mode=normalized_scene_mode,
            clips=normalized_clips,
            reason=reason,
            response_language=normalized_language,
            dry_run=dry_run,
        )
        await self._runtime.start_navigation_agent_task(
            web_session_id=self._web_session_id,
            message=navigation_request,
        )
        payload["started"] = True
        self._record_handoff(payload)
        return ToolChunk(
            content=[
                TextBlock(
                    text="Navigation data task started.",
                ),
            ],
            state=ToolResultState.SUCCESS,
            metadata=payload,
        )

    def _record_handoff(self, payload: dict[str, Any]) -> None:
        record = getattr(self._runtime, "record_navigation_handoff", None)
        if callable(record):
            record(payload)


def build_extra_agent_tools_factory(
    config: AgentScopeRuntimeConfig,
    *,
    runtime: AgentScopeRuntime | None = None,
):
    async def extra_agent_tools(_user_id: str, agent_id: str, _session_id: str) -> list[Any]:
        if agent_id == config.navigation_agent_id:
            web_session_id = _web_session_id_from_agentscope_session(
                _session_id,
                agent_id=config.navigation_agent_id,
            )
            if runtime is not None:
                session_tools = getattr(runtime, "_navigation_tools_for_session", None)
                if callable(session_tools):
                    return session_tools(
                        web_session_id=web_session_id,
                        agentscope_session_id=_session_id,
                    )
                run_cancellation = getattr(runtime, "run_cancellation", None)
                return resolve_navigation_agent_tools(
                    services=build_navigation_services(config.workspace_root),
                    cancellation=run_cancellation(_session_id) if callable(run_cancellation) else None,
                    agentscope_session_id=_session_id,
                    web_session_id=web_session_id,
                )
            return resolve_navigation_agent_tools(
                services=build_navigation_services(config.workspace_root),
                agentscope_session_id=_session_id,
                cancellation=None,
                web_session_id=web_session_id,
            )
        if agent_id == config.main_router_agent_id and runtime is not None:
            web_session_id = _web_session_id_from_agentscope_session(
                _session_id,
                agent_id=config.main_router_agent_id,
            )
            return [
                NavigationHandoffTool(
                    runtime=runtime,
                    web_session_id=web_session_id,
                )
            ]
        return []

    return extra_agent_tools


def create_agentscope_runtime(config: AgentScopeRuntimeConfig) -> AgentScopeRuntime:
    redis_kwargs = config.redis_connection_kwargs()
    storage = RedisStorage(**redis_kwargs)
    message_bus = RedisMessageBus(**redis_kwargs)
    workspace_manager = LocalWorkspaceManager(
        basedir=str(config.workspace_root / "agentscope-workspaces"),
    )
    runtime_holder: dict[str, AgentScopeRuntime] = {}

    async def extra_agent_tools(user_id: str, agent_id: str, session_id: str) -> list[Any]:
        runtime = runtime_holder.get("runtime")
        return await build_extra_agent_tools_factory(config, runtime=runtime)(
            user_id,
            agent_id,
            session_id,
        )

    app = agentscope.app.create_app(
        storage=storage,
        message_bus=message_bus,
        workspace_manager=workspace_manager,
        extra_agent_tools=extra_agent_tools,
        title="DataPilot AgentScope Runtime",
    )

    runtime = AgentScopeRuntime(
        config=config,
        storage=storage,
        message_bus=message_bus,
        workspace_manager=workspace_manager,
        app=app,
    )
    runtime_holder["runtime"] = runtime
    return runtime

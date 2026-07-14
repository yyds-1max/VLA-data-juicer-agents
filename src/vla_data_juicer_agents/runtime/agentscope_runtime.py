from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import re
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import agentscope.app
from agentscope.app.message_bus import MessageBusKeys, RedisMessageBus
from agentscope.app.storage import ChatModelConfig, RedisStorage, SessionConfig
from agentscope.app.workspace_manager import LocalWorkspaceManager
from agentscope.event import CustomEvent, ExternalExecutionResultEvent
from agentscope.message import TextBlock, ToolCallState, ToolResultBlock, ToolResultState, UserMsg
from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import ToolBase, ToolChunk

from vla_data_juicer_agents.core.cancellation import CancellationContext, bind_cancellation
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
from vla_data_juicer_agents.navigation.task_entry import (
    NavigationTaskEntryError,
    _structured_handoff_payload_from_message,
    parse_navigation_task_entry,
)
from vla_data_juicer_agents.navigation.task_store import normalize_segments
from vla_data_juicer_agents.runtime.agentscope_bootstrap import bootstrap_agentscope_records
from vla_data_juicer_agents.runtime.agentscope_config import AgentScopeRuntimeConfig
from vla_data_juicer_agents.runtime.datapilot_projection import (
    DataPilotReplyProjectionMiddleware,
    DataPilotToolOutcomeMiddleware,
    sanitize_agent_event,
)
from vla_data_juicer_agents.runtime.navigation_tool_surface import (
    NavigationToolSurfaceMiddleware,
)
from vla_data_juicer_agents.web.schemas import InterruptResponse

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


@dataclass(frozen=True)
class NavigationTaskStartResult:
    task_id: str
    agentscope_session_id: str


class NavigationDataBusyError(RuntimeError):
    """Raised when an overlapping navigation writer is already running."""


@dataclass
class AgentScopeRuntime:
    config: AgentScopeRuntimeConfig
    storage: Any
    message_bus: Any
    workspace_manager: Any
    app: Any
    web_sessions: dict[str, tuple[str, str]] = field(default_factory=dict)
    web_session_store: Any | None = None
    web_event_publisher: Any | None = None
    _active_human_decision_claims: set[str] = field(default_factory=set)
    _run_cancellations: dict[str, CancellationContext] = field(default_factory=dict)
    _tool_outcome_locks: dict[str, asyncio.Lock] = field(default_factory=dict)
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

    def set_web_transport(self, store: Any, publisher: Any | None) -> None:
        self.web_session_store = store
        self.web_event_publisher = publisher

    def projection_private_identities(self) -> set[str]:
        identities = {
            "MainRouterAgent",
            self.config.main_router_agent_id,
            "NavigationDataAgent",
            self.config.navigation_agent_id,
        }
        for agent_id, session_id in self.web_sessions.values():
            identities.update((agent_id, session_id))
        list_mappings = getattr(
            self.web_session_store,
            "list_agentscope_session_mappings",
            None,
        )
        if callable(list_mappings):
            for web_session_id in self.web_sessions:
                for mapping in list_mappings(web_session_id):
                    identities.update(
                        (mapping.agent_id, mapping.agentscope_session_id)
                    )
        return identities

    async def project_agent_event(
        self,
        agentscope_session_id: str,
        *,
        dedupe_key: str,
        event: dict[str, Any],
    ) -> Any | None:
        public_session_id = self._public_session_id(agentscope_session_id)
        if public_session_id is None:
            return None
        record = self.web_session_store.append_public_event(
            public_session_id,
            dedupe_key,
            event,
        )
        await self._publish_public_record(public_session_id, record)
        return record

    async def start_public_tool(
        self,
        agentscope_session_id: str,
        *,
        tool_call_id: str,
        tool_name: str,
    ) -> Any | None:
        public_session_id = self._public_session_id(agentscope_session_id)
        if public_session_id is None:
            return None
        return self.web_session_store.start_tool_run(
            public_session_id,
            tool_call_id,
            tool_name,
            datetime.now(UTC).isoformat(timespec="milliseconds"),
        )

    async def finish_public_tool(
        self,
        agentscope_session_id: str,
        *,
        tool_call_id: str,
        status: str,
        summary: str,
        error_type: str | None,
    ) -> Any | None:
        public_session_id = self._public_session_id(agentscope_session_id)
        if public_session_id is None:
            return None
        async with self._tool_outcome_lock(public_session_id):
            tool_run = self.web_session_store.finish_tool_run(
                public_session_id,
                tool_call_id,
                status=status,
                summary=summary,
                error_type=error_type,
            )
            if tool_run is None:
                return None
            event = CustomEvent(
                name="datapilot_tool_terminal",
                value={
                    "tool_call_id": tool_call_id,
                    "status": status,
                    "summary": summary,
                    "error_type": error_type,
                },
            ).model_dump(mode="json")
            identity = f"tool-terminal:{agentscope_session_id}:{tool_call_id}:{status}"
            record = self.web_session_store.append_public_event(
                public_session_id,
                hashlib.sha256(identity.encode("utf-8")).hexdigest(),
                event,
            )
        await self._publish_public_record(public_session_id, record)
        return tool_run

    def _tool_outcome_lock(self, public_session_id: str) -> asyncio.Lock:
        lock = self._tool_outcome_locks.get(public_session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._tool_outcome_locks[public_session_id] = lock
        return lock

    def _public_session_id(self, agentscope_session_id: str) -> str | None:
        if self.web_session_store is None:
            return None
        get_mapping = getattr(
            self.web_session_store,
            "get_agentscope_session_mapping_by_agentscope_session",
            None,
        )
        if callable(get_mapping):
            mapping = get_mapping(agentscope_session_id)
            if mapping is not None:
                return mapping.web_session_id
        for web_session_id, (_agent_id, session_id) in self.web_sessions.items():
            if session_id == agentscope_session_id:
                return web_session_id
        return None

    async def _publish_public_record(self, session_id: str, record: Any) -> None:
        if self.web_event_publisher is None:
            return
        try:
            result = self.web_event_publisher(session_id, record)
            if inspect.isawaitable(result):
                await result
        except Exception:  # pylint: disable=broad-except
            _logger.warning(
                "Live public event publish failed; persisted replay remains "
                "available: session_id=%s sequence=%s",
                session_id,
                getattr(record, "sequence", None),
                exc_info=True,
            )

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

    async def start_navigation_agent_task(
        self,
        *,
        web_session_id: str,
        message: str,
    ) -> NavigationTaskStartResult:
        await self.ensure_bootstrapped()

        entry = parse_navigation_task_entry(message)
        services = self._navigation_services()
        previous_mapping = self._web_session_mapping(web_session_id)
        if (
            previous_mapping is not None
            and previous_mapping[0] == self.config.navigation_agent_id
        ):
            existing = services.task_store.find_by_session(
                web_session_id=web_session_id,
                agentscope_session_id=previous_mapping[1],
            )
            if (
                existing is not None
                and existing.target == entry.target
                and existing.date == entry.date
                and existing.segments == normalize_segments(entry.segments)
            ):
                return NavigationTaskStartResult(
                    task_id=existing.task_id,
                    agentscope_session_id=previous_mapping[1],
                )
        writer = services.task_store.find_running_target_writer(
            date=entry.date,
            segments=entry.segments,
        )
        if writer is not None:
            raise NavigationDataBusyError(
                "an overlapping navigation data writer is already running"
            )

        creation = None
        session_id: str | None = None
        try:
            session_id = await self.ensure_web_session(
                web_session_id,
                agent_id=self.config.navigation_agent_id,
                model=self.config.navigation_model,
            )
            creation = services.task_store.create_task_attempt(
                request=entry.request,
                target=entry.target,
                date=entry.date,
                segments=entry.segments,
                scene_mode=entry.scene_mode,
                dry_run=self.config.navigation_dry_run,
                web_session_id=web_session_id,
                agentscope_session_id=session_id,
            )
            await self._start_agent_run(
                web_session_id=web_session_id,
                agent_id=self.config.navigation_agent_id,
                model=self.config.navigation_model,
                message=message,
            )
        except Exception as entry_error:
            try:
                self._restore_web_session_mapping(web_session_id, previous_mapping)
            except Exception as compensation_error:
                entry_error.add_note(
                    "navigation entry session-mapping compensation failed: "
                    f"{compensation_error!r}"
                )
            if creation is not None and creation.created and session_id is not None:
                try:
                    deleted = services.task_store.delete_task_if_current(
                        creation.task.task_id,
                        expected_state_revision=creation.task.state_revision,
                        expected_web_session_id=web_session_id,
                        expected_agentscope_session_id=session_id,
                    )
                    if not deleted:
                        entry_error.add_note(
                            "navigation attempt compensation skipped: task changed"
                        )
                except Exception as compensation_error:
                    entry_error.add_note(
                        "navigation attempt compensation failed: "
                        f"{compensation_error!r}"
                    )
            raise
        return NavigationTaskStartResult(
            task_id=creation.task.task_id,
            agentscope_session_id=session_id,
        )

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
        if agent_id == self.config.navigation_agent_id:
            anchor = self._navigation_durable_state_anchor(
                session_id,
                web_session_id=web_session_id,
            )
            message = (
                f"{message}\n\nDurable navigation state anchor (authoritative): "
                f"{json.dumps(anchor, ensure_ascii=False, sort_keys=True)}"
            )

        cancellation = CancellationContext()
        previous_cancellation = self.run_cancellation(session_id)
        self.register_run_cancellation(session_id, cancellation)

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
        return session_id

    async def interrupt_web_session(
        self,
        *,
        web_session_id: str,
    ) -> InterruptResponse:
        mappings = self._all_web_session_mappings(web_session_id)
        if not mappings and self.web_session_store is None:
            return InterruptResponse(interrupted=False)

        records = []
        async with self._tool_outcome_lock(web_session_id):
            interrupted = bool(mappings)
            for _agent_id, agentscope_session_id in mappings:
                cancellation = self.run_cancellation(agentscope_session_id)
                if cancellation is not None:
                    interrupted = cancellation.cancel() or interrupted

            failures: list[Exception] = []
            chat_service = self.app.state.chat_service
            for agent_id, agentscope_session_id in mappings:
                try:
                    await chat_service.interrupt(
                        self.config.user_id,
                        agentscope_session_id,
                        agent_id,
                    )
                except LookupError:
                    # Historical mappings can outlive an AgentScope session.
                    continue
                except Exception as exc:  # pylint: disable=broad-except
                    failures.append(exc)

            cancelled_task_ids: set[str] = set()
            for _agent_id, agentscope_session_id in mappings:
                try:
                    tasks = await self.message_bus.registry_getall(
                        MessageBusKeys.bg_tasks(agentscope_session_id),
                    )
                except Exception as exc:  # pylint: disable=broad-except
                    failures.append(exc)
                    continue
                for task_id in tasks:
                    if task_id in cancelled_task_ids:
                        continue
                    cancelled_task_ids.add(task_id)
                    try:
                        await self.message_bus.publish(
                            MessageBusKeys.task_cancel_channel(),
                            {"task_id": task_id},
                        )
                    except Exception as exc:  # pylint: disable=broad-except
                        failures.append(exc)

            if failures:
                raise RuntimeError("explicit stop cancellation failed") from failures[0]

            if self.web_session_store is None:
                return InterruptResponse(interrupted=interrupted)
            private_identities = self.projection_private_identities()

            def terminal_event(row: Any) -> tuple[str, dict[str, Any]]:
                event = CustomEvent(
                    name="datapilot_tool_terminal",
                    value={
                        "tool_call_id": row.tool_call_id,
                        "status": "stopped",
                        "summary": "已由用户停止",
                    },
                ).model_dump(mode="json")
                event = sanitize_agent_event(
                    event,
                    private_identities=private_identities,
                )
                identity = (
                    f"explicit-stop-tool-terminal:{web_session_id}:"
                    f"{row.tool_call_id}:stopped"
                )
                return hashlib.sha256(identity.encode("utf-8")).hexdigest(), event

            stopped, records = (
                self.web_session_store.stop_open_tool_runs_with_terminal_events(
                    web_session_id,
                    terminal_event,
                )
            )
        for record in records:
            await self._publish_public_record(web_session_id, record)
        return InterruptResponse(
            interrupted=interrupted or bool(stopped),
            stopped_tool_call_ids=[row.tool_call_id for row in stopped],
        )

    def _all_web_session_mappings(
        self,
        web_session_id: str,
    ) -> list[tuple[str, str]]:
        mappings: list[tuple[str, str]] = []
        list_mappings = getattr(
            self.web_session_store,
            "list_agentscope_session_mappings",
            None,
        )
        if callable(list_mappings):
            mappings.extend(
                (mapping.agent_id, mapping.agentscope_session_id)
                for mapping in list_mappings(web_session_id)
            )
        mapped = self.web_sessions.get(web_session_id)
        if mapped is not None and mapped not in mappings:
            mappings.append(mapped)
        return mappings

    async def delete_web_session(self, web_session_id: str) -> bool:
        if self.web_session_store is None:
            raise RuntimeError("Web session store is not configured")
        mappings = self.web_session_store.list_agentscope_session_mappings(web_session_id)
        session_service = self.app.state.session_service
        for mapping in mappings:
            await session_service.delete_session(
                self.config.user_id,
                mapping.agent_id,
                mapping.agentscope_session_id,
            )
        self._navigation_services().delete_control_state_for_web_session(web_session_id)
        self.web_sessions.pop(web_session_id, None)
        return True

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
        *,
        web_session_id: str,
    ) -> dict[str, Any]:
        services = self._navigation_services()
        task = services.task_store.find_by_session(
            web_session_id=web_session_id,
            agentscope_session_id=agentscope_session_id,
        )
        if task is None:
            return {
                "task_attempt_id": None,
                "observation_revision": None,
                "accepted_plan_id": None,
                "accepted_plan_revision": None,
                "current_ledger_step": None,
                "execution_status": None,
            }
        observation = services.observation_store.latest(task.task_id)
        phase_hint = task.accepted_plan_phase
        plan = (
            services.plan_store.get_active(task.task_id, phase_hint)
            if phase_hint is not None
            else None
        )
        current = services.plan_store.get_current_step(plan.plan_id) if plan is not None else None
        return {
            "task_attempt_id": task.task_id,
            "observation_revision": observation.revision if observation is not None else None,
            "accepted_plan_id": plan.plan_id if plan is not None else None,
            "accepted_plan_revision": plan.plan_revision if plan is not None else None,
            "current_ledger_step": (
                current["step"]["step_id"] if current is not None else None
            ),
            "execution_status": (
                current["step"]["status"]
                if current is not None
                else getattr(plan, "status", None)
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
        durable_agentscope_session_id: str | None = None
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
                durable_agentscope_session_id = task.agentscope_session_id
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
                    agentscope_session_id=durable_agentscope_session_id,
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
                        plan_id,
                        step_id,
                        decision_key,
                        expected_web_session_id=web_session_id,
                        expected_agentscope_session_id=agentscope_session_id,
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
                            plan_id,
                            step_id,
                            decision_key,
                            expected_web_session_id=web_session_id,
                            expected_agentscope_session_id=agentscope_session_id,
                        )
                        if not plan_store.acknowledge_human_decision_handoff(
                            plan_id,
                            step_id,
                            decision_key,
                            expected_web_session_id=web_session_id,
                            expected_agentscope_session_id=agentscope_session_id,
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
                            agentscope_session_id=agentscope_session_id,
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
                        agentscope_session_id=agentscope_session_id,
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
                    expected_web_session_id=web_session_id,
                    expected_agentscope_session_id=agentscope_session_id,
                )
                if not transitioned:
                    return False
                delivery, delivery_token = plan_store.claim_human_decision_delivery(
                    plan_id,
                    step_id,
                    decision_key,
                    owner=agentscope_session_id,
                    expected_web_session_id=web_session_id,
                    expected_agentscope_session_id=agentscope_session_id,
                )
                if delivery == "delivered":
                    if not plan_store.acknowledge_human_decision_handoff(
                        plan_id,
                        step_id,
                        decision_key,
                        expected_web_session_id=web_session_id,
                        expected_agentscope_session_id=agentscope_session_id,
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
                                expected_web_session_id=web_session_id,
                                expected_agentscope_session_id=agentscope_session_id,
                            )
                            if not completed:
                                completed = plan_store.mark_consumed_human_decision_delivery(
                                    plan_id,
                                    step_id,
                                    decision_key,
                                    expected_web_session_id=web_session_id,
                                    expected_agentscope_session_id=agentscope_session_id,
                                )
                        elif external_state == "submitted":
                            plan_store.finish_human_decision_delivery(
                                plan_id,
                                step_id,
                                decision_key,
                                token=delivery_token,
                                delivered=False,
                                expected_web_session_id=web_session_id,
                                expected_agentscope_session_id=agentscope_session_id,
                            )
                            completed = False
                        else:
                            plan_store.mark_human_decision_recovery_required(
                                plan_id,
                                step_id,
                                reason_code=external_state,
                                expected_web_session_id=web_session_id,
                                expected_agentscope_session_id=agentscope_session_id,
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
                            expected_web_session_id=web_session_id,
                            expected_agentscope_session_id=agentscope_session_id,
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
                        expected_web_session_id=web_session_id,
                        expected_agentscope_session_id=agentscope_session_id,
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
        agentscope_session_id: str | None,
        reason_code: str,
    ) -> None:
        plan_store.mark_human_decision_recovery_required(
            plan_id,
            step_id,
            reason_code=reason_code,
            expected_web_session_id=web_session_id,
            expected_agentscope_session_id=agentscope_session_id,
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

    def _is_local_agent_run_active(self, agentscope_session_id: str) -> bool:
        if self.run_cancellation(agentscope_session_id) is not None:
            return True
        registry = getattr(self.app.state, "chat_run_registry", None)
        get_task = getattr(registry, "get", None)
        if not callable(get_task):
            return False
        task = get_task(agentscope_session_id)
        return bool(task is not None and not task.done())

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

def _web_session_id_from_agentscope_session(session_id: str, *, agent_id: str) -> str:
    suffix = f"__{agent_id}"
    if session_id.endswith(suffix):
        return session_id[: -len(suffix)]
    return session_id


def _handoff_chunk(payload: dict[str, Any], *, state: ToolResultState) -> ToolChunk:
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return ToolChunk(
        content=[TextBlock(text=text)],
        state=state,
        metadata=payload,
    )


def _handoff_error(
    message: str,
    *,
    error_type: str = "invalid_navigation_handoff",
) -> ToolChunk:
    return _handoff_chunk(
        {
            "ok": False,
            "started": False,
            "error_type": error_type,
            "message": message,
        },
        state=ToolResultState.ERROR,
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
) -> str:
    clip_text = ", ".join(clips) if clips else "all"
    language = _resolve_response_language(response_language, request)
    scene_mode_text = scene_mode or "unknown"
    payload = {
        "request": request,
        "target": target,
        "date": date,
        "scene_mode": {
            "indoor": "in",
            "in": "in",
            "室内": "in",
            "outdoor": "out",
            "out": "out",
            "室外": "out",
        }.get(scene_mode or ""),
        "segments": clips or None,
        "response_language": language,
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
            "ok": False,
            "started": False,
        }

        if confidence not in {"medium", "high"}:
            self._record_handoff(payload)
            return _handoff_error(
                "Navigation handoff rejected because confidence must be medium or high.",
            )
        if missing_fields:
            self._record_handoff(payload)
            return _handoff_error(
                "Navigation handoff rejected because missing_fields is not empty.",
            )
        if not target.strip():
            self._record_handoff(payload)
            return _handoff_error("Navigation handoff rejected because target is missing.")
        if not re.fullmatch(r"[0-9]{8}", date.strip()):
            self._record_handoff(payload)
            return _handoff_error(
                "Navigation handoff rejected because date must be a YYYYMMDD dataset date.",
            )

        navigation_request = _navigation_handoff_message(
            request=request,
            target=target,
            date=date.strip(),
            scene_mode=normalized_scene_mode,
            clips=normalized_clips,
            reason=reason,
            response_language=normalized_language,
        )
        try:
            started = await self._runtime.start_navigation_agent_task(
                web_session_id=self._web_session_id,
                message=navigation_request,
            )
        except NavigationTaskEntryError as error:
            result = _handoff_error(str(error))
            payload["error_type"] = "invalid_navigation_handoff"
            self._record_handoff(payload)
            return result
        except NavigationDataBusyError:
            message = (
                "该目标当前有正在运行的数据写入操作。"
                if normalized_language == "Chinese"
                else "This target currently has a running data-writing action."
            )
            payload["error_type"] = "navigation_data_busy"
            self._record_handoff(payload)
            return _handoff_error(message, error_type="navigation_data_busy")
        except Exception:
            correlation_id = uuid4().hex
            _logger.exception(
                "Navigation handoff start failed; correlation_id=%s",
                correlation_id,
            )
            message = (
                f"导航数据任务启动失败。关联 ID: {correlation_id}"
                if normalized_language == "Chinese"
                else f"Navigation data task failed to start. Correlation ID: {correlation_id}"
            )
            payload["error_type"] = "navigation_start_failed"
            payload["correlation_id"] = correlation_id
            self._record_handoff(payload)
            return _handoff_error(message, error_type="navigation_start_failed")

        result_payload = {
            "ok": True,
            "started": True,
            "task_id": started.task_id,
            "message": (
                "导航数据任务已启动。"
                if normalized_language == "Chinese"
                else "Navigation data task started."
            ),
        }
        payload.update(
            {
                "ok": True,
                "started": True,
                "task_id": started.task_id,
            }
        )
        self._record_handoff(payload)
        return _handoff_chunk(
            result_payload,
            state=ToolResultState.SUCCESS,
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
            return []
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


def build_extra_agent_middlewares_factory(
    config: AgentScopeRuntimeConfig,
    *,
    runtime: AgentScopeRuntime | None = None,
):
    async def extra_agent_middlewares(
        _user_id: str,
        agent_id: str,
        session_id: str,
    ) -> list[Any]:
        if runtime is None:
            raise RuntimeError("navigation runtime is unavailable")
        middlewares: list[Any] = [
            DataPilotReplyProjectionMiddleware(session_id, runtime),
            DataPilotToolOutcomeMiddleware(session_id, runtime),
        ]
        if agent_id != config.navigation_agent_id:
            return middlewares
        web_session_id = _web_session_id_from_agentscope_session(
            session_id,
            agent_id=config.navigation_agent_id,
        )
        middlewares.append(
            NavigationToolSurfaceMiddleware(
                services=runtime._navigation_services(),
                web_session_id=web_session_id,
                agentscope_session_id=session_id,
                cancellation=runtime.run_cancellation(session_id),
            )
        )
        return middlewares

    return extra_agent_middlewares


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

    async def extra_agent_middlewares(
        user_id: str,
        agent_id: str,
        session_id: str,
    ) -> list[Any]:
        runtime = runtime_holder.get("runtime")
        return await build_extra_agent_middlewares_factory(config, runtime=runtime)(
            user_id,
            agent_id,
            session_id,
        )

    app = agentscope.app.create_app(
        storage=storage,
        message_bus=message_bus,
        workspace_manager=workspace_manager,
        extra_agent_middlewares=extra_agent_middlewares,
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

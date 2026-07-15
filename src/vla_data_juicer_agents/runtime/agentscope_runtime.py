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
from agentscope.message import (
    HintBlock,
    TextBlock,
    ToolCallState,
    ToolResultBlock,
    ToolResultState,
    UserMsg,
)
from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import ToolBase, ToolChunk
from pydantic import ValidationError

from vla_data_juicer_agents.core.cancellation import (
    CancellationContext,
    bind_cancellation,
    current_cancellation,
)
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
    DataPilotRunBoundaryMiddleware,
    DataPilotToolOutcomeMiddleware,
    sanitize_agent_event,
)
from vla_data_juicer_agents.runtime.navigation_tool_surface import (
    NavigationToolSurfaceMiddleware,
)
from vla_data_juicer_agents.runtime.stop_coordinator import OwnerLease, StopCoordinator
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
class _CancellationLease:
    cancellation: CancellationContext
    generation: int | None = None
    admission_baseline: tuple[int, int | None] | None = None
    admitted: bool = True
    admission_future: asyncio.Future[None] | None = None
    foreground_refs: int = 1
    tool_call_ids: set[str] = field(default_factory=set)
    quiescent: asyncio.Event = field(default_factory=asyncio.Event)
    owner_publication_suppressed: bool = False
    on_admitted: Any = None
    admission_callback_completed: bool = False

    def sync_quiescence(self) -> None:
        if self.foreground_refs == 0 and not self.tool_call_ids:
            self.quiescent.set()
        else:
            self.quiescent.clear()

    async def wait_for_quiescence(self, timeout: float) -> bool:
        deadline = asyncio.get_running_loop().time() + timeout
        try:
            await asyncio.wait_for(self.quiescent.wait(), timeout=timeout)
        except TimeoutError:
            return False
        remaining = max(0.0, deadline - asyncio.get_running_loop().time())
        return await self.cancellation.wait_for_quiescence(timeout=remaining)

    async def wait_until_quiescent(self) -> None:
        """Keep a durable turn fenced until all owned work has really exited."""
        await self.quiescent.wait()
        await self.cancellation.wait_for_quiescence()


class _StopAwareMessageBus:
    """Filter stopped ToolOffload results at AgentScope's inbox drain seam."""

    def __init__(self, wrapped: Any, runtime_provider: Any) -> None:
        self._wrapped = wrapped
        self._runtime_provider = runtime_provider

    def __getattr__(self, name: str) -> Any:
        return getattr(self._wrapped, name)

    async def __aenter__(self) -> "_StopAwareMessageBus":
        await self._wrapped.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: Any,
    ) -> None:
        await self._wrapped.__aexit__(exc_type, exc_value, traceback)

    async def queue_drain(
        self,
        key: str,
        max_count: int = 100,
    ) -> list[tuple[str, dict[str, Any]]]:
        entries = await self._wrapped.queue_drain(key, max_count=max_count)
        inbox_prefix = MessageBusKeys.inbox("")
        if not key.startswith(inbox_prefix):
            return entries
        runtime = self._runtime_provider()
        if runtime is None:
            return entries
        agentscope_session_id = key[len(inbox_prefix) :]
        try:
            return runtime.filter_stopped_tool_hints(
                agentscope_session_id,
                entries,
            )
        except Exception:  # pylint: disable=broad-except
            _logger.warning(
                "Stopped ToolOffload inbox filtering failed open: session_id=%s",
                agentscope_session_id,
                exc_info=True,
            )
            return entries


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
    _run_cancellations: dict[str, list[_CancellationLease]] = field(default_factory=dict)
    _tool_outcome_locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    _stop_locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    recovery_metrics: AgentScopeRecoveryMetrics = field(default_factory=AgentScopeRecoveryMetrics)
    bootstrapped: bool = False
    runtime_id: str = field(default_factory=lambda: uuid4().hex)
    admission_lease_ttl_seconds: float = 30.0
    admission_renew_interval_seconds: float = 5.0
    admission_release_retry_delays: tuple[float, ...] = (0.0, 0.05, 0.2)
    stop_coordinator: StopCoordinator | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self._bootstrap_lock = asyncio.Lock()
        required_bus_methods = (
            "publish",
            "subscribe",
            "registry_set",
            "registry_del",
            "registry_getall",
        )
        if all(callable(getattr(self.message_bus, name, None)) for name in required_bus_methods):
            self.stop_coordinator = StopCoordinator(
                self.message_bus,
                self._stop_owner_leases,
                runtime_id=self.runtime_id,
            )

    async def start_stop_coordinator(self) -> None:
        if self.stop_coordinator is not None:
            await self.stop_coordinator.start()

    async def stop_stop_coordinator(self) -> None:
        if self.stop_coordinator is not None:
            await self.stop_coordinator.stop()

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

    def projection_private_identities(
        self,
        web_session_id: str | None = None,
    ) -> set[str]:
        identities = {
            "MainRouterAgent",
            self.config.main_router_agent_id,
            "NavigationDataAgent",
            self.config.navigation_agent_id,
            self.config.user_id,
        }
        for agent_id, session_id in self.web_sessions.values():
            identities.update((agent_id, session_id))
        list_mappings = getattr(
            self.web_session_store,
            "list_agentscope_session_mappings",
            None,
        )
        list_all_mappings = getattr(
            self.web_session_store,
            "list_all_agentscope_session_mappings",
            None,
        )
        if web_session_id is None and callable(list_all_mappings):
            for mapping in list_all_mappings():
                identities.update(
                    (mapping.agent_id, mapping.agentscope_session_id)
                )
        elif callable(list_mappings):
            web_session_ids = (
                [web_session_id]
                if web_session_id is not None
                else list(self.web_sessions)
            )
            for mapped_web_session_id in web_session_ids:
                for mapping in list_mappings(mapped_web_session_id):
                    identities.update(
                        (mapping.agent_id, mapping.agentscope_session_id)
                    )
        return identities

    def _sanitize_public_tool_outcome(
        self,
        public_session_id: str,
        summary: str,
        error_type: str | None,
    ) -> tuple[str, str | None]:
        try:
            identities = self.projection_private_identities(public_session_id)
        except Exception:  # pylint: disable=broad-except
            _logger.exception("Public tool outcome identity lookup failed closed")
            return "Tool execution details unavailable.", "public_sanitization_failed"
        event = sanitize_agent_event(
            {"summary": summary, "error_type": error_type},
            private_identities=identities,
        )
        sanitized_summary = str(event.get("summary", ""))
        sanitized_error = event.get("error_type")
        if error_type is not None and error_type in identities:
            sanitized_error = "private_runtime_identity"
        elif sanitized_error is not None:
            sanitized_error = str(sanitized_error)
        return sanitized_summary, sanitized_error

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
        tool_run = self.web_session_store.start_tool_run(
            public_session_id,
            tool_call_id,
            tool_name,
            datetime.now(UTC).isoformat(timespec="milliseconds"),
        )
        cancellation = current_cancellation()
        if cancellation is not None:
            self.retain_tool_cancellation(
                agentscope_session_id,
                tool_call_id,
                cancellation,
            )
        return tool_run

    async def finish_public_tool(
        self,
        agentscope_session_id: str,
        *,
        tool_call_id: str,
        status: str,
        summary: str,
        error_type: str | None,
    ) -> Any | None:
        cancellation = current_cancellation()
        try:
            public_session_id = self._public_session_id(agentscope_session_id)
            if public_session_id is None:
                return None
            async with self._tool_outcome_lock(public_session_id):
                summary, error_type = self._sanitize_public_tool_outcome(
                    public_session_id,
                    summary,
                    error_type,
                )
                identity = f"tool-terminal:{agentscope_session_id}:{tool_call_id}:{status}"

                def terminal_event(tool_run: Any) -> tuple[str, dict[str, Any]]:
                    event = CustomEvent(
                        name="datapilot_tool_terminal",
                        value={
                            "tool_call_id": tool_run.tool_call_id,
                            "status": tool_run.status,
                            "summary": tool_run.summary,
                            "error_type": tool_run.error_type,
                        },
                    ).model_dump(mode="json")
                    return hashlib.sha256(identity.encode("utf-8")).hexdigest(), event

                result = self.web_session_store.finish_tool_run_with_terminal_event(
                    public_session_id,
                    tool_call_id,
                    status=status,
                    summary=summary,
                    error_type=error_type,
                    terminal_event_factory=terminal_event,
                )
                if result is None:
                    return None
                tool_run, record = result
            await self._publish_public_record(public_session_id, record)
            return tool_run
        finally:
            if cancellation is not None:
                self.release_tool_cancellation(
                    agentscope_session_id,
                    tool_call_id,
                    cancellation,
                )

    def should_suppress_tool_delivery(
        self,
        agentscope_session_id: str,
        tool_call_id: str,
    ) -> bool:
        public_session_id = self._public_session_id(agentscope_session_id)
        if public_session_id is None or self.web_session_store is None:
            return False
        status = self.web_session_store.tool_run_status(
            public_session_id,
            tool_call_id,
        )
        return status == "stopped" or self.web_session_store.execution_generation_is_fenced(
            public_session_id
        )

    def should_suppress_wakeup(self, agentscope_session_id: str) -> bool:
        public_session_id = self._public_session_id(agentscope_session_id)
        if public_session_id is None or self.web_session_store is None:
            return False
        return self.web_session_store.execution_generation_is_fenced(
            public_session_id
        )

    def filter_stopped_tool_hints(
        self,
        agentscope_session_id: str,
        entries: list[tuple[str, dict[str, Any]]],
    ) -> list[tuple[str, dict[str, Any]]]:
        public_session_id = self._public_session_id(agentscope_session_id)
        if public_session_id is None or self.web_session_store is None:
            return entries
        retained: list[tuple[str, dict[str, Any]]] = []
        for entry_id, payload in entries:
            try:
                hint = HintBlock.model_validate(payload)
            except ValidationError:  # AgentScope Inbox owns canonical validation.
                retained.append((entry_id, payload))
                continue
            source = hint.source
            try:
                source_payload = json.loads(source) if isinstance(source, str) else None
            except json.JSONDecodeError:
                source_payload = None
            if not (
                isinstance(source_payload, dict)
                and set(source_payload) == {"label", "sublabel"}
                and source_payload.get("label") == "tool_output"
                and isinstance(source_payload.get("sublabel"), str)
            ):
                retained.append((entry_id, payload))
                continue
            sublabel = source_payload["sublabel"]
            suppressed = (
                self.web_session_store.tool_delivery_sublabel_is_suppressed(
                    public_session_id,
                    sublabel,
                )
            )
            if suppressed:
                _logger.info(
                    "Dropped stopped ToolOffload inbox result: session_id=%s",
                    agentscope_session_id,
                )
            else:
                retained.append((entry_id, payload))
        return retained

    def _tool_outcome_lock(self, public_session_id: str) -> asyncio.Lock:
        lock = self._tool_outcome_locks.get(public_session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._tool_outcome_locks[public_session_id] = lock
        return lock

    def _stop_lock(self, public_session_id: str) -> asyncio.Lock:
        lock = self._stop_locks.get(public_session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._stop_locks[public_session_id] = lock
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

    async def _record_human_decision_resolution(
        self,
        web_session_id: str,
        *,
        request_id: str | None = None,
        all_pending: bool = False,
        reason: str,
    ) -> Any | None:
        record = self._append_human_decision_resolution(
            web_session_id,
            request_id=request_id,
            all_pending=all_pending,
            reason=reason,
        )
        if record is not None:
            await self._publish_public_record(web_session_id, record)
        return record

    def _append_human_decision_resolution(
        self,
        web_session_id: str,
        *,
        request_id: str | None = None,
        all_pending: bool = False,
        reason: str,
    ) -> Any | None:
        if self.web_session_store is None:
            return None
        value: dict[str, Any] = {"reason": reason}
        if all_pending:
            value["all"] = True
            public_identity = "all"
        else:
            normalized_request_id = (request_id or "").strip()
            if not normalized_request_id:
                raise ValueError("human decision resolution requires request_id")
            value["request_id"] = normalized_request_id
            public_identity = normalized_request_id
        event = CustomEvent(
            name="datapilot_human_decision_resolved",
            value=value,
        ).model_dump(mode="json")
        event = sanitize_agent_event(
            event,
            private_identities=self.projection_private_identities(),
        )
        identity = (
            f"human-decision-resolved:{web_session_id}:"
            f"{public_identity}:{reason}"
        )
        record = self.web_session_store.append_public_event(
            web_session_id,
            hashlib.sha256(identity.encode("utf-8")).hexdigest(),
            event,
        )
        return record

    async def ensure_web_session(
        self,
        web_session_id: str,
        *,
        agent_id: str,
        model: str,
        admission_ticket: str | None = None,
    ) -> str:
        if self.web_session_store is None:
            return await self._ensure_web_session_with_ticket(
                web_session_id,
                agent_id=agent_id,
                model=model,
                admission_ticket=admission_ticket,
            )
        if admission_ticket is not None:
            # Nested callers reuse the durable lease already claimed by their
            # outer admission boundary; do not claim or renew a second lease.
            return await self._ensure_web_session_with_ticket(
                web_session_id,
                agent_id=agent_id,
                model=model,
                admission_ticket=admission_ticket,
            )

        owned_ticket, _baseline = self.web_session_store.claim_session_run_admission(
            web_session_id,
            runtime_id=self.runtime_id,
            ttl_seconds=self.admission_lease_ttl_seconds,
        )
        ensure_task = asyncio.create_task(
            self._ensure_web_session_with_ticket(
                web_session_id,
                agent_id=agent_id,
                model=model,
                admission_ticket=owned_ticket,
            ),
            name=f"datapilot-session-ensure:{web_session_id}",
        )
        heartbeat_task = asyncio.create_task(
            self._heartbeat_session_run_admission(web_session_id, owned_ticket),
            name=f"datapilot-session-ensure-heartbeat:{web_session_id}",
        )
        try:
            done, _pending = await asyncio.wait(
                {ensure_task, heartbeat_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if ensure_task in done:
                return ensure_task.result()
            await asyncio.sleep(0)
            if ensure_task.done():
                return ensure_task.result()
            heartbeat_error = heartbeat_task.exception()
            ensure_task.cancel()
            await asyncio.gather(ensure_task, return_exceptions=True)
            raise RuntimeError("AgentScope session ensure lease renewal failed") from (
                heartbeat_error
                or RuntimeError("session ensure heartbeat exited unexpectedly")
            )
        finally:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
            if not ensure_task.done():
                ensure_task.cancel()
                await asyncio.gather(ensure_task, return_exceptions=True)
            await self._release_session_run_admission(web_session_id, owned_ticket)

    async def _ensure_web_session_with_ticket(
        self,
        web_session_id: str,
        *,
        agent_id: str,
        model: str,
        admission_ticket: str | None,
    ) -> str:
        existing = self.web_sessions.get(web_session_id)
        if existing and existing[0] == agent_id:
            return existing[1]

        persisted = self._load_web_session_mapping(web_session_id, agent_id=agent_id)
        if persisted and persisted[0] == agent_id:
            self.web_sessions[web_session_id] = persisted
            self._save_web_session_mapping(
                web_session_id,
                agent_id,
                persisted[1],
                admission_ticket=admission_ticket,
            )
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
        previous_mapping = self.web_sessions.get(web_session_id)
        self.web_sessions[web_session_id] = (agent_id, session.id)
        try:
            self._save_web_session_mapping(
                web_session_id,
                agent_id,
                session.id,
                admission_ticket=admission_ticket,
            )
        except Exception as mapping_error:
            if previous_mapping is None:
                self.web_sessions.pop(web_session_id, None)
            else:
                self.web_sessions[web_session_id] = previous_mapping
            raise
        return session.id

    async def submit_user_message(
        self,
        *,
        web_session_id: str,
        message: str,
        message_id: str | None = None,
        turn_id: str | None = None,
        on_admitted: Any = None,
    ) -> str:
        await self.ensure_bootstrapped()

        turn_id = turn_id or f"turn_{uuid4()}"
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
            turn_id=turn_id,
            message_id=message_id,
            on_admitted=on_admitted,
        )
        return turn_id

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
        turn_id: str | None = None,
        message_id: str | None = None,
        on_admitted: Any = None,
    ) -> str:
        admission_ticket: str | None = None
        admission_baseline: tuple[int, int | None] | None = None
        if self.web_session_store is not None:
            admission_ticket, admission_baseline = (
                self.web_session_store.claim_session_run_admission(
                    web_session_id,
                    runtime_id=self.runtime_id,
                    ttl_seconds=self.admission_lease_ttl_seconds,
                )
            )
        run_task: asyncio.Task | None = None
        heartbeat_task: asyncio.Task | None = None
        try:
            run_task = asyncio.create_task(
                self._start_agent_run_with_ticket(
                    web_session_id=web_session_id,
                    agent_id=agent_id,
                    model=model,
                    message=message,
                    turn_id=turn_id,
                    message_id=message_id,
                    admission_ticket=admission_ticket,
                    admission_baseline=admission_baseline,
                    on_admitted=on_admitted,
                ),
                name=f"datapilot-admission:{web_session_id}",
            )
            if admission_ticket is None:
                return await run_task
            heartbeat_task = asyncio.create_task(
                self._heartbeat_session_run_admission(
                    web_session_id,
                    admission_ticket,
                ),
                name=f"datapilot-admission-heartbeat:{web_session_id}",
            )
            done, _pending = await asyncio.wait(
                {run_task, heartbeat_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            # Boundary admission is the authoritative HTTP ACK. If both tasks
            # complete in the same loop turn, maintenance failure must not
            # reverse a successful admission and invite duplicate execution.
            if run_task in done:
                return run_task.result()
            if heartbeat_task in done:
                heartbeat_error = heartbeat_task.exception()
                # Admission_future may have been resolved in this same loop
                # turn. Give its waiter one scheduling opportunity before
                # deciding that maintenance failed pre-admission.
                await asyncio.sleep(0)
                if run_task.done():
                    return run_task.result()
                run_task.cancel()
                await asyncio.gather(run_task, return_exceptions=True)
                raise RuntimeError("AgentScope run admission lease renewal failed") from (
                    heartbeat_error
                    or RuntimeError("admission heartbeat exited unexpectedly")
                )
            return run_task.result()
        finally:
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                await asyncio.gather(heartbeat_task, return_exceptions=True)
            if run_task is not None and not run_task.done():
                run_task.cancel()
                await asyncio.gather(run_task, return_exceptions=True)
            if admission_ticket is not None:
                await self._release_session_run_admission(
                    web_session_id,
                    admission_ticket,
                )

    async def _heartbeat_session_run_admission(
        self,
        web_session_id: str,
        admission_ticket: str,
    ) -> None:
        while True:
            await asyncio.sleep(self.admission_renew_interval_seconds)
            self.web_session_store.renew_session_run_admission(
                web_session_id,
                admission_ticket,
                runtime_id=self.runtime_id,
                ttl_seconds=self.admission_lease_ttl_seconds,
            )

    async def _release_session_run_admission(
        self,
        web_session_id: str,
        admission_ticket: str,
    ) -> bool:
        failure: Exception | None = None
        for delay in self.admission_release_retry_delays:
            if delay:
                await asyncio.sleep(delay)
            try:
                self.web_session_store.release_session_run_admission(
                    web_session_id,
                    admission_ticket,
                )
                return True
            except Exception as exc:  # pylint: disable=broad-except
                failure = exc
        # Admission success/failure is already authoritative. Reversing it here
        # can duplicate a live run or hide its real exception. The expiring
        # lease remains a conservative delete fence until crash recovery reaps
        # it, so log the maintenance failure without changing the result.
        _logger.error(
            "AgentScope run admission lease release failed; awaiting expiry",
            exc_info=(
                type(failure),
                failure,
                failure.__traceback__,
            ) if failure is not None else None,
        )
        return False

    async def _start_agent_run_with_ticket(
        self,
        *,
        web_session_id: str,
        agent_id: str,
        model: str,
        message: str,
        turn_id: str | None = None,
        message_id: str | None = None,
        admission_ticket: str | None = None,
        admission_baseline: tuple[int, int | None] | None = None,
        on_admitted: Any = None,
    ) -> str:
        chat_service = self.app.state.chat_service
        session_id = await self.ensure_web_session(
            web_session_id,
            agent_id=agent_id,
            model=model,
            admission_ticket=admission_ticket,
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
        admission_future: asyncio.Future[None] | None = None
        if self.web_session_store is not None and admission_baseline is None:
            admission_baseline = self.web_session_store.execution_boundary_snapshot(
                web_session_id
            )
        self.register_run_cancellation(
            session_id,
            cancellation,
            generation=(admission_baseline[0] if admission_baseline else None),
        )
        lease = self._run_cancellation_lease(session_id, cancellation)
        if admission_baseline is not None:
            admission_future = asyncio.get_running_loop().create_future()
            lease.admission_baseline = admission_baseline
            lease.admitted = False
            lease.admission_future = admission_future
            lease.on_admitted = on_admitted

        if self.stop_coordinator is not None and self.stop_coordinator.started:
            try:
                # A submit is not launchable until its baseline owner is
                # visible to Stop; do not rely on the heartbeat interval.
                await self.stop_coordinator.refresh_owners()
            except Exception as exc:
                self.clear_run_cancellation(session_id, cancellation)
                with suppress(Exception):
                    await self.stop_coordinator.refresh_owners()
                raise RuntimeError(
                    "AgentScope run owner publication failed"
                ) from exc

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
                if (
                    self.stop_coordinator is not None
                    and self.stop_coordinator.started
                ):
                    try:
                        await self.stop_coordinator.refresh_owners()
                    except Exception:  # pylint: disable=broad-except
                        _logger.exception(
                            "Failed to remove completed AgentScope run owner"
                        )

        try:
            task = self._spawn_chat_run(
                run_with_cancellation(),
                session_id=session_id,
            )
        except Exception:
            self.clear_run_cancellation(session_id, cancellation)
            raise
        if turn_id is not None and on_admitted is None:
            self._attach_user_turn_terminal(
                task,
                web_session_id=web_session_id,
                turn_id=turn_id,
                message_id=message_id,
                lease=lease,
            )
        add_done_callback = getattr(task, "add_done_callback", None)
        if admission_future is not None and callable(add_done_callback):
            def reject_unadmitted(completed: asyncio.Task) -> None:
                if admission_future.done():
                    return
                if completed.cancelled():
                    error = RuntimeError(
                        "AgentScope run was stopped before admission"
                    )
                else:
                    error = completed.exception() or RuntimeError(
                        "AgentScope run exited before admission"
                    )
                admission_future.set_exception(error)

            add_done_callback(reject_unadmitted)
            try:
                await admission_future
            except asyncio.CancelledError:
                cancellation.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise
        if turn_id is not None and on_admitted is not None:
            self._attach_user_turn_terminal(
                task,
                web_session_id=web_session_id,
                turn_id=turn_id,
                message_id=message_id,
                lease=lease,
            )
        if message_id is not None and self.web_session_store is not None:
            self._attach_user_message_execution_heartbeat(
                task,
                web_session_id=web_session_id,
                message_id=message_id,
                cancellation=cancellation,
            )
        return session_id

    def _attach_user_turn_terminal(
        self,
        task: Any,
        *,
        web_session_id: str,
        turn_id: str,
        message_id: str | None = None,
        lease: _CancellationLease,
    ) -> None:
        add_done_callback = getattr(task, "add_done_callback", None)
        if not callable(add_done_callback):
            return

        def record_after_registry_cleanup(completed: asyncio.Task) -> None:
            if completed.cancelled():
                status = "stopped"
            else:
                status = "failure" if completed.exception() is not None else "success"
            record_task = completed.get_loop().create_task(
                self._record_user_turn_terminal(
                    web_session_id=web_session_id,
                    turn_id=turn_id,
                    message_id=message_id,
                    status=status,
                    lease=lease,
                ),
                name=f"datapilot-run-terminal:{turn_id}",
            )
            record_task.add_done_callback(self._log_run_terminal_failure)

        # AgentScope's registry installs its cleanup callback inside spawn().
        # Registering this callback afterwards guarantees the registry entry is
        # gone before the public terminal is persisted and published.
        add_done_callback(record_after_registry_cleanup)

    def _attach_user_message_execution_heartbeat(
        self,
        task: Any,
        *,
        web_session_id: str,
        message_id: str,
        cancellation: CancellationContext,
    ) -> None:
        add_done_callback = getattr(task, "add_done_callback", None)
        if not callable(add_done_callback):
            return

        async def heartbeat() -> None:
            while True:
                await asyncio.sleep(self.admission_renew_interval_seconds)
                self.web_session_store.renew_user_message(
                    web_session_id,
                    message_id,
                    runtime_id=self.runtime_id,
                    ttl_seconds=self.admission_lease_ttl_seconds,
                )

        heartbeat_task = asyncio.create_task(
            heartbeat(),
            name=f"datapilot-turn-heartbeat:{message_id}",
        )

        def stop_heartbeat(_completed: asyncio.Task) -> None:
            heartbeat_task.cancel()

        add_done_callback(stop_heartbeat)
        def handle_heartbeat_failure(completed: asyncio.Task) -> None:
            try:
                completed.result()
            except asyncio.CancelledError:
                return
            except Exception:  # pylint: disable=broad-except
                _logger.exception("Failed to renew DataPilot turn execution lease")
                cancellation.cancel()
                task.cancel()

        heartbeat_task.add_done_callback(handle_heartbeat_failure)

    @staticmethod
    def _log_run_terminal_failure(task: asyncio.Task) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception:  # pylint: disable=broad-except
            _logger.exception("Failed to persist DataPilot run terminal")

    async def _record_user_turn_terminal(
        self,
        *,
        web_session_id: str,
        turn_id: str,
        message_id: str | None,
        status: str,
        lease: _CancellationLease,
    ) -> Any | None:
        if self.web_session_store is None:
            return None
        # The foreground AgentScope task can finish while ToolOffload work and
        # shielded ``to_thread`` plan workers are still producing side effects.
        # Keep the exact-message admission authoritative until both the runtime
        # tool lease and the real worker registrations have reached quiescence.
        await lease.wait_until_quiescent()
        if message_id is not None:
            record = self.web_session_store.finish_user_message_turn_with_event(
                web_session_id,
                message_id,
                turn_id=turn_id,
                terminal_status=status,
            )
        else:
            event = CustomEvent(
                name="datapilot_run_terminal",
                value={"turn_id": turn_id, "status": status},
            ).model_dump(mode="json")
            identity = f"run-terminal:{web_session_id}:{turn_id}"
            record = self.web_session_store.append_public_event(
                web_session_id,
                hashlib.sha256(identity.encode("utf-8")).hexdigest(),
                event,
            )
        await self._publish_public_record(web_session_id, record)
        return record

    async def interrupt_web_session(
        self,
        *,
        web_session_id: str,
    ) -> InterruptResponse:
        async with self._stop_lock(web_session_id):
            return await self._interrupt_web_session_serialized(
                web_session_id=web_session_id,
            )

    async def _interrupt_web_session_serialized(
        self,
        *,
        web_session_id: str,
    ) -> InterruptResponse:
        mappings = self._all_web_session_mappings(web_session_id)
        active_user_turns = (
            self.web_session_store.admitted_user_message_turns(web_session_id)
            if self.web_session_store is not None
            else []
        )
        if not mappings and self.web_session_store is None:
            return InterruptResponse(interrupted=False)

        stop_request = (
            self.web_session_store.begin_or_resume_stop_request(web_session_id)
            if self.web_session_store is not None
            else None
        )
        coordinator_active = bool(
            stop_request is not None
            and stop_request.status == "pending"
            and self.stop_coordinator is not None
            and self.stop_coordinator.started
        )
        interrupted = bool(mappings or active_user_turns)
        expected_owners: dict[str, dict[str, Any]] | None = None
        if coordinator_active:
            assert stop_request is not None
            assert self.stop_coordinator is not None
            detail = self.web_session_store.get_session(web_session_id)
            require_owner = bool(
                detail is not None
                and any(row.status == "running" for row in detail.tool_runs)
            )
            expected_owners = await self.stop_coordinator.snapshot_expected_owners(
                [session_id for _agent_id, session_id in mappings],
                stop_request.generation,
            )

        local_cancellations = {
            lease.cancellation
            for _agent_id, agentscope_session_id in mappings
            for lease in self._run_cancellations.get(agentscope_session_id, [])
            if stop_request is None
            or lease.generation is None
            or lease.generation <= stop_request.generation
        }
        local_failures: list[Exception] = []
        for _agent_id, agentscope_session_id in mappings:
            try:
                interrupted = (
                    self.cancel_run_cancellations(agentscope_session_id)
                    or interrupted
                )
            except Exception as exc:  # pylint: disable=broad-except
                local_failures.append(exc)

        # Freeze the owner barrier before cancellation can release a lease,
        # then issue AgentScope's official cancellations before waiting for the
        # barrier. Pure-async background tools release their lease only from
        # the cancelled task's finally block.
        official_failure: Exception | None = None
        try:
            await self._request_official_web_session_cancellation(mappings)
        except Exception as exc:  # pylint: disable=broad-except
            official_failure = exc
        if local_failures:
            raise RuntimeError("explicit stop cancellation failed") from local_failures[0]
        if official_failure is not None:
            raise official_failure

        if coordinator_active:
            assert stop_request is not None
            assert self.stop_coordinator is not None
            try:
                await self.stop_coordinator.request_and_wait(
                    request_id=stop_request.request_id,
                    target_generation=stop_request.generation,
                    agentscope_session_ids=[
                        session_id for _agent_id, session_id in mappings
                    ],
                    require_owner=require_owner,
                    expected_owners=expected_owners,
                )
            except TimeoutError as exc:
                raise RuntimeError(
                    "explicit stop owner acknowledgement failed"
                ) from exc
        else:
            if local_cancellations:
                workers_done = await asyncio.gather(
                    *(
                        cancellation.wait_for_workers(timeout=10.0)
                        for cancellation in local_cancellations
                    )
                )
                if not all(workers_done):
                    raise RuntimeError("explicit stop worker did not quiesce")

        if self.web_session_store is None:
            for _agent_id, agentscope_session_id in mappings:
                self.discard_run_cancellations(agentscope_session_id)
            return InterruptResponse(interrupted=interrupted)

        records = []
        async with self._tool_outcome_lock(web_session_id):
            def terminal_event(row: Any) -> tuple[str, dict[str, Any]]:
                event = CustomEvent(
                    name="datapilot_tool_terminal",
                    value={
                        "tool_call_id": row.tool_call_id,
                        "status": "stopped",
                        "summary": "已由用户停止",
                    },
                ).model_dump(mode="json")
                identity = (
                    f"explicit-stop-tool-terminal:{web_session_id}:"
                    f"{row.tool_call_id}:stopped"
                )
                return hashlib.sha256(identity.encode("utf-8")).hexdigest(), event

            if stop_request is None:
                stopped, records = (
                    self.web_session_store.stop_open_tool_runs_with_terminal_events(
                        web_session_id,
                        terminal_event,
                    )
                )
            else:
                stopped, records = (
                    self.web_session_store.complete_stop_request_with_terminal_events(
                        web_session_id,
                        stop_request.request_id,
                        terminal_event,
                    )
                )
            for _agent_id, agentscope_session_id in mappings:
                self.discard_run_cancellations(agentscope_session_id)
            for message_id, turn_id in active_user_turns:
                try:
                    records.append(
                        self.web_session_store.finish_user_message_turn_with_event(
                            web_session_id,
                            message_id,
                            turn_id=turn_id,
                            terminal_status="stopped",
                        )
                    )
                except RuntimeError:
                    # A normal completion callback may win after the owner
                    # barrier. Its terminal is already the authoritative one.
                    continue
            if interrupted or stopped:
                resolution = self._append_human_decision_resolution(
                    web_session_id,
                    all_pending=True,
                    reason="stopped",
                )
                if resolution is not None:
                    records.append(resolution)
        for record in records:
            await self._publish_public_record(web_session_id, record)
        return InterruptResponse(
            interrupted=interrupted or bool(stopped),
            stopped_tool_call_ids=[row.tool_call_id for row in stopped],
        )

    async def _request_official_web_session_cancellation(
        self,
        mappings: list[tuple[str, str]],
    ) -> None:
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
        async with self._stop_lock(web_session_id):
            # This durable fence is visible to submitters in every process and
            # intentionally survives any partial destructive failure. The
            # manager removes it atomically with the final public session row;
            # retries are idempotent.
            self.web_session_store.begin_session_deletion(web_session_id)
            deadline = asyncio.get_running_loop().time() + 10.0
            while True:
                self.web_session_store.reap_expired_session_run_admissions(
                    web_session_id
                )
                if not self.web_session_store.session_run_admission_is_pending(
                    web_session_id
                ):
                    break
                if asyncio.get_running_loop().time() >= deadline:
                    raise RuntimeError(
                        "session run admission did not quiesce before deletion"
                    )
                await asyncio.sleep(0.01)
            mappings = self.web_session_store.list_agentscope_session_mappings(
                web_session_id
            )
            coordinator_active = bool(
                self.stop_coordinator is not None
                and self.stop_coordinator.started
            )
            if coordinator_active:
                # Do not destroy AgentScope or navigation state until every
                # frozen remote owner has acknowledged actual quiescence.
                await self._interrupt_web_session_serialized(
                    web_session_id=web_session_id,
                )
            else:
                cancellation_failures: list[Exception] = []
                for mapping in mappings:
                    try:
                        self.cancel_run_cancellations(
                            mapping.agentscope_session_id
                        )
                    except Exception as exc:  # pylint: disable=broad-except
                        cancellation_failures.append(exc)
                if cancellation_failures:
                    raise RuntimeError(
                        "AgentScope session cancellation failed"
                    ) from cancellation_failures[0]

            # Re-read only after admissions and every owner have quiesced. The
            # deletion fence rejects supported mapping writers, while this final
            # snapshot also covers a mapping committed by an older/external writer
            # during cancellation so its AgentScope session is not orphaned.
            mappings = self.web_session_store.list_agentscope_session_mappings(
                web_session_id
            )
            session_service = self.app.state.session_service
            for mapping in mappings:
                await session_service.delete_session(
                    self.config.user_id,
                    mapping.agent_id,
                    mapping.agentscope_session_id,
                )
            for mapping in mappings:
                self.discard_run_cancellations(mapping.agentscope_session_id)
            self._navigation_services().delete_control_state_for_web_session(
                web_session_id
            )
            self.web_sessions.pop(web_session_id, None)
            return True

    def register_run_cancellation(
        self,
        agentscope_session_id: str,
        cancellation: CancellationContext,
        *,
        generation: int | None = None,
    ) -> None:
        leases = self._run_cancellations.setdefault(agentscope_session_id, [])
        for lease in leases:
            if lease.cancellation is cancellation:
                lease.foreground_refs += 1
                lease.sync_quiescence()
                if generation is not None:
                    lease.generation = generation
                return
        leases.append(
            _CancellationLease(
                cancellation=cancellation,
                generation=generation,
            )
        )

    def run_cancellation(self, agentscope_session_id: str) -> CancellationContext | None:
        leases = self._run_cancellations.get(agentscope_session_id, [])
        return next(
            (
                lease.cancellation
                for lease in reversed(leases)
                if not lease.cancellation.cancelled
            ),
            None,
        )

    def admit_user_execution_generation(
        self,
        agentscope_session_id: str,
        cancellation: CancellationContext,
    ) -> int | None:
        """Advance a stopped boundary after AgentScope admits a user run.

        The run-boundary middleware calls this only for ``UserMsg`` inputs,
        after ChatService owns the distributed session-run lock.  A lease keeps
        the operation idempotent if middleware is re-entered for the same run.
        """
        public_session_id = self._public_session_id(agentscope_session_id)
        if public_session_id is None or self.web_session_store is None:
            return None
        lease = self._run_cancellation_lease(
            agentscope_session_id,
            cancellation,
        )
        if not lease.admitted:
            lease.generation = self.web_session_store.begin_execution_generation(
                public_session_id,
                expected_boundary=lease.admission_baseline,
            )
            lease.admitted = True
        elif lease.generation is None:
            lease.generation = self.web_session_store.begin_execution_generation(
                public_session_id
            )
        return lease.generation

    async def complete_user_execution_admission(
        self,
        agentscope_session_id: str,
        cancellation: CancellationContext,
    ) -> None:
        lease = self._run_cancellation_lease(
            agentscope_session_id,
            cancellation,
        )
        try:
            if self.stop_coordinator is not None and self.stop_coordinator.started:
                await self.stop_coordinator.refresh_owners()
            public_session_id = self._public_session_id(agentscope_session_id)
            if (
                public_session_id is not None
                and self.web_session_store is not None
                and self.web_session_store.execution_generation_is_fenced(
                    public_session_id
                )
            ):
                cancellation.cancel()
                raise RuntimeError("execution admission was fenced by stop")
        except asyncio.CancelledError:
            future = lease.admission_future
            if future is not None and not future.done():
                future.cancel()
            raise
        except Exception as exc:
            future = lease.admission_future
            if future is not None and not future.done():
                future.set_exception(
                    exc
                    if isinstance(exc, RuntimeError)
                    else RuntimeError("AgentScope run admission failed")
                )
            raise
        try:
            if lease.on_admitted is not None and not lease.admission_callback_completed:
                admitted_result = lease.on_admitted()
                if inspect.isawaitable(admitted_result):
                    await admitted_result
                lease.admission_callback_completed = True
        except asyncio.CancelledError:
            future = lease.admission_future
            if future is not None and not future.done():
                future.cancel()
            raise
        except Exception as exc:
            future = lease.admission_future
            if future is not None and not future.done():
                future.set_exception(exc)
            raise
        future = lease.admission_future
        if future is not None and not future.done():
            future.set_result(None)

    def _run_cancellation_lease(
        self,
        agentscope_session_id: str,
        cancellation: CancellationContext,
    ) -> _CancellationLease:
        lease = next(
            (
                item
                for item in self._run_cancellations.get(
                    agentscope_session_id,
                    [],
                )
                if item.cancellation is cancellation
            ),
            None,
        )
        if lease is None:
            raise RuntimeError(
                "AgentScope user run must register cancellation before admission"
            )
        return lease

    def retain_tool_cancellation(
        self,
        agentscope_session_id: str,
        tool_call_id: str,
        cancellation: CancellationContext,
    ) -> None:
        leases = self._run_cancellations.setdefault(agentscope_session_id, [])
        lease = next(
            (item for item in leases if item.cancellation is cancellation),
            None,
        )
        if lease is None:
            lease = _CancellationLease(
                cancellation=cancellation,
                foreground_refs=0,
            )
            leases.append(lease)
        if lease.generation is None:
            public_session_id = self._public_session_id(agentscope_session_id)
            current_generation = getattr(
                self.web_session_store,
                "current_execution_generation",
                None,
            )
            if public_session_id is not None and callable(current_generation):
                lease.generation = current_generation(public_session_id)
        lease.tool_call_ids.add(tool_call_id)
        lease.sync_quiescence()

    def _stop_owner_leases(self) -> list[OwnerLease]:
        return [
            OwnerLease(
                agentscope_session_id=agentscope_session_id,
                generation=lease.generation,
                cancellation=lease.cancellation,
                tool_call_ids=frozenset(lease.tool_call_ids),
                wait_for_quiescence=lease.wait_for_quiescence,
                publish_owner=not lease.owner_publication_suppressed,
                suppress_for_ack=lambda session_id=agentscope_session_id,
                item=lease,
                generation=lease.generation: self._suppress_run_cancellation_owner(
                    session_id,
                    item,
                    generation,
                ),
                restore_after_ack_failure=lambda session_id=agentscope_session_id,
                item=lease,
                generation=lease.generation: self._restore_run_cancellation_owner(
                    session_id,
                    item,
                    generation,
                ),
                on_acknowledged=lambda session_id=agentscope_session_id,
                item=lease,
                generation=lease.generation: self._discard_acknowledged_run_cancellation(
                    session_id,
                    item,
                    generation,
                ),
            )
            for agentscope_session_id, leases in self._run_cancellations.items()
            for lease in leases
            if lease.generation is not None
        ]

    def _suppress_run_cancellation_owner(
        self,
        agentscope_session_id: str,
        lease: _CancellationLease,
        generation: int,
    ) -> bool:
        current = self._run_cancellations.get(agentscope_session_id, [])
        if (
            not any(candidate is lease for candidate in current)
            or lease.generation != generation
            or lease.foreground_refs != 0
            or lease.tool_call_ids
        ):
            return False
        lease.owner_publication_suppressed = True
        return True

    def _restore_run_cancellation_owner(
        self,
        agentscope_session_id: str,
        lease: _CancellationLease,
        generation: int,
    ) -> None:
        if (
            any(
                candidate is lease
                for candidate in self._run_cancellations.get(
                    agentscope_session_id,
                    [],
                )
            )
            and lease.generation == generation
        ):
            lease.owner_publication_suppressed = False

    def _discard_acknowledged_run_cancellation(
        self,
        agentscope_session_id: str,
        acknowledged: _CancellationLease,
        generation: int,
    ) -> None:
        leases = self._run_cancellations.get(agentscope_session_id, [])
        remaining = [
            lease
            for lease in leases
            if not (
                lease is acknowledged
                and lease.generation == generation
                and lease.foreground_refs == 0
                and not lease.tool_call_ids
            )
        ]
        if remaining:
            self._run_cancellations[agentscope_session_id] = remaining
        else:
            self._run_cancellations.pop(agentscope_session_id, None)

    def release_tool_cancellation(
        self,
        agentscope_session_id: str,
        tool_call_id: str,
        cancellation: CancellationContext,
    ) -> None:
        for lease in self._run_cancellations.get(agentscope_session_id, []):
            if lease.cancellation is cancellation:
                lease.tool_call_ids.discard(tool_call_id)
                lease.sync_quiescence()
                break
        self._prune_run_cancellations(agentscope_session_id)

    def cancel_run_cancellations(self, agentscope_session_id: str) -> bool:
        cancelled = False
        failures: list[Exception] = []
        for lease in self._run_cancellations.get(agentscope_session_id, []):
            try:
                cancelled = lease.cancellation.cancel() or cancelled
            except Exception as exc:  # pylint: disable=broad-except
                failures.append(exc)
        if failures:
            raise RuntimeError("AgentScope cancellation lease failed") from failures[0]
        return cancelled

    def discard_run_cancellations(self, agentscope_session_id: str) -> None:
        self._run_cancellations.pop(agentscope_session_id, None)

    def clear_run_cancellation(
        self,
        agentscope_session_id: str,
        cancellation: CancellationContext,
    ) -> None:
        for lease in self._run_cancellations.get(agentscope_session_id, []):
            if lease.cancellation is cancellation:
                lease.foreground_refs = max(0, lease.foreground_refs - 1)
                lease.sync_quiescence()
                break
        self._prune_run_cancellations(agentscope_session_id)

    def _prune_run_cancellations(self, agentscope_session_id: str) -> None:
        public_session_id = self._public_session_id(agentscope_session_id)

        def pending_stop(lease: _CancellationLease) -> bool:
            return bool(
                public_session_id is not None
                and self.web_session_store is not None
                and lease.generation is not None
                and self.web_session_store.stop_request_is_pending(
                    public_session_id,
                    lease.generation,
                )
            )

        leases = [
            lease
            for lease in self._run_cancellations.get(agentscope_session_id, [])
            if lease.foreground_refs > 0 or lease.tool_call_ids or pending_stop(lease)
        ]
        if leases:
            self._run_cancellations[agentscope_session_id] = leases
        else:
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
            if self._is_human_decision_consumed(
                agentscope_session_id=agentscope_session_id,
                reply_id=decision["reply_id"],
                tool_call_id=decision["tool_call_id"],
            ):
                await self._record_human_decision_resolution(
                    web_session_id,
                    request_id=decision.get("request_id"),
                    reason="submitted",
                )
                return True
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
                    await self._record_human_decision_resolution(
                        web_session_id,
                        request_id=decision.get("request_id"),
                        reason="submitted",
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
                        await self._record_human_decision_resolution(
                            web_session_id,
                            request_id=decision.get("request_id"),
                            reason="submitted",
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
                    await self._record_human_decision_resolution(
                        web_session_id,
                        request_id=decision.get("request_id"),
                        reason="submitted",
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
            handoff_identity = (
                f"navigation-human-handoff:{plan_id}:{step_id}:{decision_key}"
                if plan_bound_handoff
                else _human_continuation_identity(
                    agentscope_session_id,
                    pending_tool_name,
                    decision,
                )
            )
            input_msg = ExternalExecutionResultEvent(
                id=handoff_identity,
                metadata={"idempotency_key": handoff_identity},
                reply_id=decision["reply_id"],
                execution_results=[result],
            )
            cancellation = CancellationContext()
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
                self.clear_run_cancellation(agentscope_session_id, cancellation)
                raise
            if not plan_bound_handoff:
                self._mark_human_decision_consumed(
                    agentscope_session_id=agentscope_session_id,
                    decision=decision,
                )
            claim_handoff = True
            await self._record_human_decision_resolution(
                web_session_id,
                request_id=decision.get("request_id"),
                reason="submitted",
            )
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

    def _spawn_chat_run(self, run_coroutine: Any, *, session_id: str) -> Any:
        chat_run_registry = getattr(self.app.state, "chat_run_registry", None)
        if chat_run_registry is None:
            run_coroutine.close()
            raise RuntimeError("AgentScope chat_run_registry is not initialized")
        try:
            return chat_run_registry.spawn(run_coroutine, session_id=session_id)
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

        cancellation = CancellationContext()
        self.register_run_cancellation(session_id, cancellation)

        async def run_with_cancellation() -> None:
            try:
                async with cancellation.track_agent(session_id):
                    with bind_cancellation(cancellation):
                        await self.app.state.chat_service.run(
                            user_id=user_id,
                            session_id=session_id,
                            agent_id=agent_id,
                            input_msg=None,
                        )
            finally:
                self.clear_run_cancellation(session_id, cancellation)

        try:
            self._spawn_chat_run(
                run_with_cancellation(),
                session_id=session_id,
            )
        except RuntimeError:
            self.clear_run_cancellation(session_id, cancellation)
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
        *,
        admission_ticket: str | None = None,
    ) -> None:
        if self.web_session_store is None:
            return
        save_mapping = getattr(self.web_session_store, "save_agentscope_session_mapping", None)
        if callable(save_mapping):
            save_mapping(
                web_session_id,
                agent_id=agent_id,
                agentscope_session_id=agentscope_session_id,
                admission_ticket=admission_ticket,
                runtime_id=self.runtime_id,
            )

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


def _human_continuation_identity(
    agentscope_session_id: str,
    tool_name: str,
    decision: dict[str, Any],
) -> str:
    canonical = json.dumps(
        {
            "agentscope_session_id": agentscope_session_id,
            "tool_name": tool_name,
            "reply_id": decision.get("reply_id"),
            "tool_call_id": decision.get("tool_call_id"),
            "action": decision.get("action"),
            "text": decision.get("text"),
            "request_id": decision.get("request_id"),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"human-handoff:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


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
            DataPilotRunBoundaryMiddleware(session_id, runtime),
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
    redis_message_bus = RedisMessageBus(**redis_kwargs)
    workspace_manager = LocalWorkspaceManager(
        basedir=str(config.workspace_root / "agentscope-workspaces"),
    )
    runtime_holder: dict[str, AgentScopeRuntime] = {}
    message_bus = _StopAwareMessageBus(
        redis_message_bus,
        lambda: runtime_holder.get("runtime"),
    )

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

"""Production-faithful, side-effect-contained AgentScope evaluation host."""

from __future__ import annotations

import asyncio
import inspect
from collections import defaultdict
from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from agentscope.app._manager import BackgroundTaskManager, SchedulerManager
from agentscope.app._service import _chat as chat_service_module
from agentscope.app._service._chat import ChatService
from agentscope.app.message_bus import MessageBus
from agentscope.app.storage import (
    ChatModelConfig,
    CredentialRecord,
    SessionConfig,
    SessionRecord,
    SessionSource,
    StorageBase,
)
from agentscope.app.storage._utils import _dump_with_secrets
from agentscope.app.workspace_manager import LocalWorkspaceManager
from agentscope.message import Msg, UserMsg
from agentscope.state import AgentState

from vla_data_juicer_agents.runtime.agentscope_bootstrap import (
    bootstrap_agentscope_records,
)
from vla_data_juicer_agents.runtime.agentscope_config import AgentScopeRuntimeConfig
from vla_data_juicer_agents.runtime.single_agent import (
    RouterContractV1Middleware,
    router_v1_tools,
)

from .trace import EvaluationSafetyMiddleware, TraceMiddleware, TraceRecorder


class InMemoryStorage(StorageBase):
    """Small complete StorageBase used by one isolated evaluation worker."""

    def __init__(self) -> None:
        self.credentials: dict[tuple[str, str], CredentialRecord] = {}
        self.agents: dict[tuple[str, str], Any] = {}
        self.sessions: dict[tuple[str, str, str], SessionRecord] = {}
        self.messages: dict[tuple[str, str], list[Msg]] = defaultdict(list)
        self.schedules: dict[tuple[str, str], Any] = {}
        self.teams: dict[tuple[str, str], Any] = {}

    async def upsert_credential(self, user_id: str, credential_data: Any) -> str:
        credential_id = credential_data.id or uuid4().hex
        old = self.credentials.get((user_id, credential_id))
        record = CredentialRecord(
            id=credential_id,
            user_id=user_id,
            data=_dump_with_secrets(credential_data),
        )
        if old is not None:
            record.created_at = old.created_at
        self.credentials[(user_id, credential_id)] = record
        return credential_id

    async def get_credential(
        self,
        user_id: str,
        credential_id: str,
    ) -> CredentialRecord | None:
        return self.credentials.get((user_id, credential_id))

    async def list_credentials(self, user_id: str) -> list[CredentialRecord]:
        return [record for (owner, _), record in self.credentials.items() if owner == user_id]

    async def delete_credential(self, user_id: str, credential_id: str) -> bool:
        return self.credentials.pop((user_id, credential_id), None) is not None

    async def upsert_agent(self, user_id: str, agent_record: Any) -> str:
        self.agents[(user_id, agent_record.id)] = agent_record
        return agent_record.id

    async def get_agent(self, user_id: str, agent_id: str) -> Any | None:
        return self.agents.get((user_id, agent_id))

    async def list_agents(self, user_id: str) -> list[Any]:
        return [record for (owner, _), record in self.agents.items() if owner == user_id]

    async def delete_agent(self, user_id: str, agent_id: str) -> bool:
        return self.agents.pop((user_id, agent_id), None) is not None

    async def upsert_session(
        self,
        user_id: str,
        agent_id: str,
        config: SessionConfig,
        state: AgentState | None = None,
        session_id: str | None = None,
        source: SessionSource = SessionSource.USER,
        source_schedule_id: str | None = None,
    ) -> SessionRecord:
        if session_id is not None:
            old = self.sessions.get((user_id, agent_id, session_id))
            if old is not None:
                record = old.model_copy(
                    update={
                        "config": config,
                        "state": state if state is not None else old.state,
                    },
                )
                self.sessions[(user_id, agent_id, session_id)] = record
                return record
        record = SessionRecord(
            id=session_id or uuid4().hex,
            user_id=user_id,
            agent_id=agent_id,
            config=config,
            state=state or AgentState(),
            source=source,
            source_schedule_id=source_schedule_id,
        )
        self.sessions[(user_id, agent_id, record.id)] = record
        return record

    async def get_session(
        self,
        user_id: str,
        agent_id: str,
        session_id: str,
    ) -> SessionRecord | None:
        record = self.sessions.get((user_id, agent_id, session_id))
        if record is None:
            return None
        state = AgentState.model_validate(record.state.model_dump(mode="json"))
        return record.model_copy(update={"state": state})

    async def list_sessions(self, user_id: str, agent_id: str) -> list[SessionRecord]:
        return [
            record
            for (owner, owner_agent, _), record in self.sessions.items()
            if owner == user_id and owner_agent == agent_id
        ]

    async def list_sessions_by_schedule(
        self,
        user_id: str,
        schedule_id: str,
    ) -> list[SessionRecord]:
        return [
            record
            for (owner, _, _), record in self.sessions.items()
            if owner == user_id and record.source_schedule_id == schedule_id
        ]

    async def update_session_state(
        self,
        user_id: str,
        agent_id: str,
        session_id: str,
        state: AgentState,
    ) -> None:
        key = (user_id, agent_id, session_id)
        persisted = AgentState.model_validate(state.model_dump(mode="json"))
        self.sessions[key] = self.sessions[key].model_copy(update={"state": persisted})

    async def set_session_team_id(
        self,
        user_id: str,
        session_id: str,
        team_id: str | None,
    ) -> None:
        for key, record in list(self.sessions.items()):
            if key[0] == user_id and key[2] == session_id:
                self.sessions[key] = record.model_copy(update={"team_id": team_id})

    async def delete_session(self, user_id: str, agent_id: str, session_id: str) -> bool:
        return self.sessions.pop((user_id, agent_id, session_id), None) is not None

    async def upsert_message(self, user_id: str, session_id: str, msg: Msg) -> None:
        items = self.messages[(user_id, session_id)]
        if items and items[-1].id == msg.id:
            items[-1] = msg
        else:
            items.append(msg)

    async def get_message(
        self,
        user_id: str,
        session_id: str,
        message_id: str,
    ) -> Msg | None:
        return next(
            (
                msg
                for msg in self.messages.get((user_id, session_id), [])
                if msg.id == message_id
            ),
            None,
        )

    async def list_messages(
        self,
        user_id: str,
        session_id: str,
        offset: int = 0,
        limit: int = 50,
    ) -> list[Msg]:
        return list(self.messages.get((user_id, session_id), []))[offset : offset + limit]

    async def upsert_schedule(self, user_id: str, record: Any) -> str:
        self.schedules[(user_id, record.id)] = record
        return record.id

    async def get_schedule(self, user_id: str, schedule_id: str) -> Any | None:
        return self.schedules.get((user_id, schedule_id))

    async def list_schedules(self, user_id: str) -> list[Any]:
        return [record for (owner, _), record in self.schedules.items() if owner == user_id]

    async def list_all_schedules(self) -> list[Any]:
        return list(self.schedules.values())

    async def delete_schedule(self, user_id: str, schedule_id: str) -> bool:
        return self.schedules.pop((user_id, schedule_id), None) is not None

    async def upsert_team(self, user_id: str, record: Any) -> Any:
        self.teams[(user_id, record.id)] = record
        return record

    async def get_team(self, user_id: str, team_id: str) -> Any | None:
        return self.teams.get((user_id, team_id))

    async def list_teams(self, user_id: str) -> list[Any]:
        return [record for (owner, _), record in self.teams.items() if owner == user_id]

    async def delete_team(self, user_id: str, team_id: str) -> bool:
        return self.teams.pop((user_id, team_id), None) is not None


class InMemoryMessageBus(MessageBus):
    """MessageBus implementation with an independent, filtered trace sink."""

    def __init__(self, recorder: TraceRecorder) -> None:
        self.recorder = recorder
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._logs: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
        self._queues: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
        self._subscribers: dict[str, list[asyncio.Queue[dict[str, Any]]]] = defaultdict(list)
        self._sequence = 0

    def _next_id(self) -> str:
        self._sequence += 1
        return f"{self._sequence}-0"

    @asynccontextmanager
    async def acquire_lock(self, key: str, *, ttl_secs: int = 600):
        del ttl_secs
        async with self._locks[key]:
            yield

    async def is_locked(self, key: str) -> bool:
        return self._locks[key].locked()

    async def log_append(
        self,
        key: str,
        payload: dict,
        *,
        ttl_secs: int | None = None,
        max_len: int | None = None,
    ) -> str:
        del ttl_secs
        entry_id = self._next_id()
        self._logs[key].append((entry_id, dict(payload)))
        if max_len is not None:
            self._logs[key] = self._logs[key][-max_len:]
        return entry_id

    async def log_read(
        self,
        key: str,
        since: str | None = None,
        max_count: int = 100,
    ) -> list[tuple[str, dict]]:
        rows = self._logs.get(key, [])
        if since is not None:
            rows = [row for row in rows if row[0] > since]
        return list(rows[:max_count])

    async def log_trim(self, key: str, before_id: str | None = None) -> None:
        if before_id is None:
            self._logs.pop(key, None)
        else:
            self._logs[key] = [row for row in self._logs.get(key, []) if row[0] >= before_id]

    async def publish(self, key: str, payload: dict) -> None:
        for queue in self._subscribers.get(key, []):
            await queue.put(dict(payload))

    async def subscribe(
        self,
        key: str,
        *,
        on_ready: Callable[[], None] | None = None,
    ) -> AsyncGenerator[dict, None]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._subscribers[key].append(queue)
        if on_ready is not None:
            on_ready()
        try:
            while True:
                yield await queue.get()
        finally:
            self._subscribers[key].remove(queue)

    async def queue_push(
        self,
        key: str,
        payload: dict,
        *,
        ttl_secs: int | None = None,
    ) -> str:
        del ttl_secs
        entry_id = self._next_id()
        self._queues[key].append((entry_id, dict(payload)))
        return entry_id

    async def queue_drain(self, key: str, max_count: int = 100) -> list[tuple[str, dict]]:
        rows = self._queues.get(key, [])[:max_count]
        self._queues[key] = self._queues.get(key, [])[len(rows) :]
        return list(rows)

    async def queue_delete(self, key: str) -> None:
        self._queues.pop(key, None)

    async def session_publish_event(self, session_id: str, event: dict) -> str:
        self.recorder.accept_event(event)
        return await super().session_publish_event(session_id, event)


class RecordingRouterRuntime:
    """Side-effect-free target for the production Router V1 tool surface."""

    def __init__(
        self,
        recorder: TraceRecorder,
        *,
        web_session_id: str,
        router_session_id: str,
        runtime_setup: Mapping[str, Any] | None = None,
    ) -> None:
        self.recorder = recorder
        self.web_session_id = web_session_id
        self.router_session_id = router_session_id
        self.operations: list[dict[str, Any]] = []
        self._terminal_tools: set[str] = set()
        self._context_revision = 0
        setup = dict(runtime_setup or {})
        focused = setup.get("focused_task")
        self.focused_task = dict(focused) if isinstance(focused, Mapping) else None
        request_context = setup.get("request_context")
        self.request_context = (
            dict(request_context) if isinstance(request_context, Mapping) else None
        )

    @staticmethod
    def _available_actions(status: str) -> list[str]:
        return {
            "active": ["stop", "cancel"],
            "waiting_user": ["provide_input", "cancel"],
            "paused": ["resume", "cancel"],
            "pausing": ["cancel"],
            "needs_replan": ["adjust", "cancel"],
        }.get(status, [])

    def _focused_summary(self) -> dict[str, Any] | None:
        if self.focused_task is None:
            return None
        status = str(self.focused_task.get("status") or "active")
        selection = self.focused_task.get("selection")
        clips = (
            list(selection.get("clips") or [])
            if isinstance(selection, Mapping)
            and selection.get("kind") == "selected_clips"
            else []
        )
        return {
            "task_ref": str(self.focused_task.get("task_ref") or "DP-EVAL-FOCUSED"),
            "domain": "navigation",
            "dataset_date": str(self.focused_task.get("dataset_date") or "20260718"),
            "selection": (
                {"kind": "selected_clips", "clips": clips}
                if clips
                else {"kind": "all_clips"}
            ),
            "scene_mode": self.focused_task.get("scene_mode"),
            "status": status,
            "phase": "waiting_input" if status == "waiting_user" else "preparing",
            "wait_cause": self.focused_task.get("wait_cause"),
            "latest_public_update": None,
            "available_actions": self._available_actions(status),
            "state_revision": self._context_revision,
        }

    def router_context_envelope(
        self,
        web_session_id: str,
        *,
        router_session_id: str | None = None,
    ) -> dict[str, Any]:
        del router_session_id
        if web_session_id != self.web_session_id:
            raise ValueError("evaluation session identity mismatch")
        focused = self._focused_summary()
        return {
            "contract_version": 1,
            "context_revision": self._context_revision,
            "focus_generation": int(focused is not None),
            "focused_task_ref": (focused or {}).get("task_ref"),
            "focused_task_summary": focused,
            "pending_interaction_summary": None,
            "latest_public_update": None,
            "attention_tasks": [],
            "request_context": self.request_context,
        }

    async def start_navigation_agent_task_v1(
        self,
        *,
        web_session_id: str,
        router_session_id: str,
        scope_source: str,
        dataset_date: str,
        selection: dict[str, Any],
        scene_mode: str | None,
    ) -> dict[str, Any]:
        self._assert_identity(web_session_id, router_session_id)
        if self.focused_task is not None:
            raise RuntimeError("an evaluation task already occupies the task slot")
        if self.request_context is not None:
            if scope_source != "request_context":
                raise RuntimeError(
                    "trusted shortcut scope must be used without reinterpretation",
                )
            expected_scope = {
                "dataset_date": self.request_context.get("dataset_date"),
                "selection": self.request_context.get("selection"),
            }
            supplied_scope = {
                "dataset_date": dataset_date,
                "selection": selection,
            }
            if supplied_scope != expected_scope:
                raise RuntimeError(
                    "navigation scope does not match the trusted shortcut selection",
                )
        elif scope_source == "request_context":
            raise RuntimeError("the evaluation turn has no trusted request context")
        payload = {
            "ok": True,
            "operation": "start",
            "accepted": True,
            "started": True,
            "task_ref": "DP-EVALUATION",
            "scope_source": scope_source,
            "dataset_date": dataset_date,
            "selection": dict(selection),
        }
        if scene_mode is not None:
            payload["scene_mode"] = scene_mode
        self.focused_task = {
            "task_ref": payload["task_ref"],
            "status": "active",
            "dataset_date": dataset_date,
            "selection": dict(selection),
            "scene_mode": scene_mode,
        }
        self._context_revision += 1
        self._record("start_navigation_data_task", payload)
        self.request_context = None
        return payload

    def begin_user_turn(self, turn_index: int) -> None:
        """Mirror the production rule that shortcut context belongs to one Turn."""

        if turn_index > 0:
            self.request_context = None

    async def continue_navigation_agent_task_v1(
        self,
        *,
        web_session_id: str,
        router_session_id: str,
    ) -> dict[str, Any]:
        self._assert_identity(web_session_id, router_session_id)
        if self.focused_task is None:
            raise RuntimeError("the evaluation has no focused task to continue")
        status = str(self.focused_task.get("status") or "")
        if status not in {"waiting_user", "paused", "needs_replan"}:
            raise RuntimeError(f"the evaluation task cannot continue from {status}")
        payload = {
            "ok": True,
            "operation": "continue",
            "accepted": True,
            "task_ref": str(self.focused_task.get("task_ref") or "DP-EVALUATION"),
            "status": "active",
        }
        if self.focused_task is not None:
            self.focused_task["status"] = "active"
        self._context_revision += 1
        self._record("continue_navigation_data_task", payload)
        return payload

    async def control_navigation_agent_task_v1(
        self,
        *,
        web_session_id: str,
        router_session_id: str,
        action: str,
    ) -> dict[str, Any]:
        self._assert_identity(web_session_id, router_session_id)
        if self.focused_task is None:
            raise RuntimeError("the evaluation has no focused task to control")
        current_status = str(self.focused_task.get("status") or "")
        if action == "stop" and current_status != "active":
            raise RuntimeError(f"the evaluation task cannot stop from {current_status}")
        payload = {
            "ok": True,
            "operation": action,
            "accepted": True,
            "task_ref": str(self.focused_task.get("task_ref") or "DP-EVALUATION"),
            "status": "pausing" if action == "stop" else "cancelling",
        }
        if self.focused_task is not None:
            self.focused_task["status"] = payload["status"]
        self._context_revision += 1
        self._record("control_navigation_data_task", payload)
        return payload

    @staticmethod
    def safe_router_tool_error(
        error: Exception,
        *,
        action: str,
        web_session_id: str | None = None,
    ) -> dict[str, Any]:
        del error, web_session_id
        return {
            "ok": False,
            "operation": action,
            "accepted": False,
            "error": {
                "code": "evaluation_runtime_error",
                "message": "The evaluation routing action was not accepted.",
                "retryable": False,
            },
        }

    def consume_router_terminal_tool(
        self,
        *,
        router_session_id: str,
        tool_name: str,
    ) -> bool:
        self._assert_router_session(router_session_id)
        if tool_name not in self._terminal_tools:
            return False
        self._terminal_tools.remove(tool_name)
        return True

    def _record(self, tool_name: str, payload: dict[str, Any]) -> None:
        self.operations.append(dict(payload))
        self._terminal_tools.add(tool_name)
        self.recorder.record_handoff(payload)

    def _assert_identity(self, web_session_id: str, router_session_id: str) -> None:
        if web_session_id != self.web_session_id:
            raise ValueError("evaluation web session identity mismatch")
        self._assert_router_session(router_session_id)

    def _assert_router_session(self, router_session_id: str) -> None:
        if router_session_id != self.router_session_id:
            raise ValueError("evaluation router session identity mismatch")


@dataclass(frozen=True)
class HostRunResult:
    session_id: str
    events: tuple[dict[str, Any], ...]
    model_calls: tuple[dict[str, Any], ...]
    tool_calls: tuple[dict[str, Any], ...]
    forbidden_calls: tuple[dict[str, Any], ...]
    handoffs: tuple[dict[str, Any], ...]
    final_text: str
    token_usage: dict[str, int]


class EvaluationHost:
    """Assemble and run the real router stack against in-memory infrastructure."""

    def __init__(
        self,
        *,
        config: AgentScopeRuntimeConfig,
        workspace_root: str | Path,
        model_factory: Callable[..., Any] | None = None,
        runtime_setup: Mapping[str, Any] | None = None,
    ) -> None:
        self.config = config
        self.workspace_root = Path(workspace_root)
        self.model_factory = model_factory
        self.runtime_setup = dict(runtime_setup or {})
        self.recorder = TraceRecorder.for_workspace(self.workspace_root)
        self.storage = InMemoryStorage()
        self.message_bus = InMemoryMessageBus(self.recorder)
        self.workspace_manager = LocalWorkspaceManager(
            basedir=str(self.workspace_root / "agentscope-workspaces"),
        )
        self.background_task_manager = BackgroundTaskManager()
        self.scheduler_manager = SchedulerManager(self.storage, self.message_bus)
        self._router_runtime: RecordingRouterRuntime | None = None

    async def _extra_tools(
        self,
        _user_id: str,
        agent_id: str,
        session_id: str,
    ) -> list[Any]:
        if agent_id != self.config.main_router_agent_id:
            return []
        web_session_id = session_id.removesuffix(
            f"__{self.config.main_router_agent_id}",
        )
        if self._router_runtime is None:
            self._router_runtime = RecordingRouterRuntime(
                self.recorder,
                web_session_id=web_session_id,
                router_session_id=session_id,
                runtime_setup=self.runtime_setup,
            )
        elif self._router_runtime.web_session_id != web_session_id:
            raise RuntimeError("one EvaluationHost may evaluate only one web session")
        return router_v1_tools(
            runtime=self._router_runtime,
            web_session_id=web_session_id,
            router_session_id=session_id,
        )

    async def _extra_middlewares(
        self,
        _user_id: str,
        agent_id: str,
        session_id: str,
    ) -> list[Any]:
        if agent_id != self.config.main_router_agent_id:
            return []
        web_session_id = session_id.removesuffix(
            f"__{self.config.main_router_agent_id}",
        )
        if self._router_runtime is None:
            self._router_runtime = RecordingRouterRuntime(
                self.recorder,
                web_session_id=web_session_id,
                router_session_id=session_id,
                runtime_setup=self.runtime_setup,
            )
        return [
            RouterContractV1Middleware(
                runtime=self._router_runtime,
                web_session_id=web_session_id,
                router_session_id=session_id,
            ),
            TraceMiddleware(self.recorder),
            EvaluationSafetyMiddleware(self.recorder),
        ]

    async def _injected_get_model(self, *args: Any, **kwargs: Any) -> Any:
        assert self.model_factory is not None
        value = self.model_factory(*args, **kwargs)
        return await value if inspect.isawaitable(value) else value

    async def run(
        self,
        message: str | Sequence[str],
        *,
        web_session_id: str = "eval",
    ) -> HostRunResult:
        await bootstrap_agentscope_records(self.storage, self.config)
        session_id = f"{web_session_id}__{self.config.main_router_agent_id}"
        await self.storage.upsert_session(
            self.config.user_id,
            self.config.main_router_agent_id,
            SessionConfig(
                workspace_id=f"workspace-{web_session_id}",
                name=web_session_id,
                chat_model_config=ChatModelConfig(
                    type="dashscope_chat",
                    credential_id=self.config.credential_id,
                    model=self.config.router_model,
                    parameters={"parallel_tool_calls": False},
                ),
            ),
            session_id=session_id,
        )
        service = ChatService(
            storage=self.storage,
            workspace_manager=self.workspace_manager,
            scheduler_manager=self.scheduler_manager,
            background_task_manager=self.background_task_manager,
            message_bus=self.message_bus,
            extra_agent_middlewares=self._extra_middlewares,
            extra_agent_tools=self._extra_tools,
        )

        original_get_model = chat_service_module.get_model
        if self.model_factory is not None:
            chat_service_module.get_model = self._injected_get_model
        try:
            messages = [message] if isinstance(message, str) else list(message)
            if not messages:
                raise ValueError("evaluation conversation must contain a user message")
            for turn_index, content in enumerate(messages):
                if self._router_runtime is not None:
                    self._router_runtime.begin_user_turn(turn_index)
                await service._run_impl(
                    self.config.user_id,
                    session_id,
                    self.config.main_router_agent_id,
                    UserMsg(name="user", content=content),
                )
        finally:
            chat_service_module.get_model = original_get_model
            await self.workspace_manager.__aexit__(None, None, None)

        return self.snapshot(session_id=session_id)

    def snapshot(self, *, session_id: str) -> HostRunResult:
        """Return the sanitized trace collected so far, including after failures."""

        snapshot = self.recorder.sanitized_snapshot()
        return HostRunResult(
            session_id=session_id,
            events=tuple(snapshot["events"]),
            model_calls=tuple(snapshot["model_calls"]),
            tool_calls=tuple(snapshot["tool_calls"]),
            forbidden_calls=tuple(snapshot["forbidden_calls"]),
            handoffs=tuple(snapshot["handoffs"]),
            final_text=str(snapshot["final_text"]),
            token_usage=dict(snapshot["token_usage"]),
        )


async def run_router_case(
    message: str,
    *,
    config: AgentScopeRuntimeConfig,
    workspace_root: str | Path,
    web_session_id: str = "eval",
    model_factory: Callable[..., Any] | None = None,
    runtime_setup: Mapping[str, Any] | None = None,
) -> HostRunResult:
    """Run one isolated router turn using production AgentScope assembly."""

    host = EvaluationHost(
        config=config,
        workspace_root=workspace_root,
        model_factory=model_factory,
        runtime_setup=runtime_setup,
    )
    return await host.run(message, web_session_id=web_session_id)

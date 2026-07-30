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
from agentscope.message import AssistantMsg, Msg, TextBlock, ToolResultState, UserMsg
from agentscope.middleware import MiddlewareBase
from agentscope.state import AgentState
from agentscope.tool import FunctionTool, ToolResponse

from vla_data_juicer_agents.navigation.catalog import (
    list_navigation_tool_capabilities,
)
from vla_data_juicer_agents.navigation.observation_models import (
    AnnotationJobFactsObservation,
    ArtifactStateObservation,
    CalibrationInventoryObservation,
    EvidenceDescriptor,
    GridmapArtifactsObservation,
    LocalizationSourcesObservation,
    NavigationObservationRevision,
    RawMetadataObservation,
    RuntimeAssetsObservation,
    SensorCandidatesObservation,
    TopicCandidatesObservation,
    UserGuidanceObservation,
)
from vla_data_juicer_agents.navigation.observation_projection import (
    compact_observation_payload,
)
from vla_data_juicer_agents.navigation.plan_models import (
    ExtractSyncPlanInput,
    FinishProcessingPlanInput,
    TrajectoryReviewPlanInput,
)
from vla_data_juicer_agents.navigation.plan_store import (
    project_actionable_plan_step,
)
from vla_data_juicer_agents.navigation.agent_tools import _TrustedNavigationTool
from vla_data_juicer_agents.navigation.plan_validation import (
    validate_navigation_plan,
)
from vla_data_juicer_agents.navigation.planning_context import (
    build_navigation_task_context,
    m2_annotation_ready_for_postprocessing,
    m2_finish_observations_complete,
)
from vla_data_juicer_agents.navigation.task_state import NavigationTask, utc_now
from vla_data_juicer_agents.runtime.agentscope_bootstrap import (
    bootstrap_agentscope_records,
)
from vla_data_juicer_agents.runtime.agentscope_config import AgentScopeRuntimeConfig
from vla_data_juicer_agents.runtime.single_agent import (
    RouterContractV1Middleware,
    router_v1_tools,
)
from vla_data_juicer_agents.runtime.agentscope_runtime import (
    _navigation_handoff_message,
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
    def _available_actions(
        status: str,
        *,
        completion_outcome: str | None = None,
    ) -> list[str]:
        if (
            status == "completed"
            and completion_outcome == "postprocessing_completed_fix_pending"
        ):
            return ["continue_fix"]
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
        completion_outcome = self.focused_task.get("completion_outcome")
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
            "phase": (
                "后处理已完成"
                if completion_outcome == "postprocessing_completed_fix_pending"
                else "waiting_input"
                if status == "waiting_user"
                else "preparing"
            ),
            "wait_cause": self.focused_task.get("wait_cause"),
            "latest_public_update": None,
            "available_actions": self._available_actions(
                status,
                completion_outcome=(
                    str(completion_outcome) if completion_outcome is not None else None
                ),
            ),
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
        requested_outcome: str = "auto",
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
        if requested_outcome != "auto":
            payload["requested_outcome"] = requested_outcome
        if scene_mode is not None:
            payload["scene_mode"] = scene_mode
        self.focused_task = {
            "task_ref": payload["task_ref"],
            "status": "active",
            "dataset_date": dataset_date,
            "selection": dict(selection),
            "scene_mode": scene_mode,
            "requested_outcome": requested_outcome,
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
        linked_fix = (
            status == "completed"
            and self.focused_task.get("completion_outcome")
            == "postprocessing_completed_fix_pending"
        )
        if status not in {"waiting_user", "paused", "needs_replan"} and not linked_fix:
            raise RuntimeError(f"the evaluation task cannot continue from {status}")
        payload = {
            "ok": True,
            "operation": "continue",
            "accepted": True,
            "task_ref": str(self.focused_task.get("task_ref") or "DP-EVALUATION"),
            "status": "active",
        }
        if linked_fix:
            payload["linked_fix"] = True
        if self.focused_task is not None:
            self.focused_task["status"] = "active"
            if linked_fix:
                self.focused_task["requested_outcome"] = "trajectory_fix"
                self.focused_task.pop("completion_outcome", None)
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


class RecordingNavigationRuntime:
    """Safe fact/Plan boundary for a real NavigationDataAgent model turn."""

    _INSPECTION_TOOL_NAMES = (
        "inspect_navigation_raw_metadata_tool",
        "inspect_navigation_sensor_candidates_tool",
        "inspect_navigation_topic_candidates_tool",
        "inspect_navigation_runtime_assets_tool",
        "inspect_navigation_calibration_inventory_tool",
        "inspect_navigation_localization_sources_tool",
        "inspect_navigation_annotation_job_facts_tool",
        "inspect_navigation_artifact_state_tool",
        "inspect_navigation_gridmap_artifacts_tool",
    )
    _POSTPROCESSING_PLANNING_TOOL_NAMES = frozenset(
        {
            "inspect_navigation_runtime_assets_tool",
            "inspect_navigation_calibration_inventory_tool",
            "inspect_navigation_localization_sources_tool",
            "inspect_navigation_annotation_job_facts_tool",
            "inspect_navigation_artifact_state_tool",
            "inspect_navigation_gridmap_artifacts_tool",
            "get_navigation_task_context_tool",
            "submit_finish_processing_plan_tool",
        }
    )
    _TRAJECTORY_REVIEW_PLANNING_TOOL_NAMES = frozenset(
        {
            "inspect_navigation_annotation_job_facts_tool",
            "get_navigation_task_context_tool",
            "submit_trajectory_review_plan_tool",
        }
    )
    _OBSERVATION_MODELS = {
        "inspect_navigation_raw_metadata_tool": RawMetadataObservation,
        "inspect_navigation_sensor_candidates_tool": SensorCandidatesObservation,
        "inspect_navigation_topic_candidates_tool": TopicCandidatesObservation,
        "inspect_navigation_runtime_assets_tool": RuntimeAssetsObservation,
        "inspect_navigation_calibration_inventory_tool": (
            CalibrationInventoryObservation
        ),
        "inspect_navigation_localization_sources_tool": (
            LocalizationSourcesObservation
        ),
        "inspect_navigation_annotation_job_facts_tool": (
            AnnotationJobFactsObservation
        ),
        "inspect_navigation_artifact_state_tool": ArtifactStateObservation,
        "inspect_navigation_gridmap_artifacts_tool": GridmapArtifactsObservation,
    }

    def __init__(
        self,
        recorder: TraceRecorder,
        *,
        runtime_setup: Mapping[str, Any],
    ) -> None:
        self.recorder = recorder
        task = runtime_setup.get("navigation_task")
        if not isinstance(task, Mapping):
            raise ValueError("navigation evaluation setup is unavailable")
        self.task = dict(task)
        self._context_revision = 1
        self._completed_kinds: set[str] = set()
        configured = self.task.get("tool_results")
        configured_results = dict(configured) if isinstance(configured, Mapping) else {}
        self._tool_results = self._default_tool_results()
        for name, payload in configured_results.items():
            if isinstance(payload, Mapping):
                self._tool_results[str(name)] = dict(payload)
        self._kind_revisions: dict[str, int] = {}
        self._payloads_by_kind: dict[str, Any] = {}
        self._evidence_by_kind: dict[str, EvidenceDescriptor] = {}
        self._submitted_plan: dict[str, Any] | None = None
        self._submitted_first_step_id: str | None = None
        self._background_running = False

    def _default_tool_results(self) -> dict[str, dict[str, Any]]:
        selection = dict(self.task.get("selection") or {"kind": "all_clips"})
        clips = list(selection.get("clips") or [])
        date = str(self.task.get("dataset_date") or "20260718")
        return {
            "inspect_navigation_raw_metadata_tool": {
                "kind": "raw_metadata",
                "segments": clips,
                "topics": [],
            },
            "inspect_navigation_sensor_candidates_tool": {
                "kind": "sensor_candidates",
                "candidates": [],
            },
            "inspect_navigation_topic_candidates_tool": {
                "kind": "topic_candidates",
                "available_topics": [],
                "suggested_role_names": {},
                "routes": [],
            },
            "inspect_navigation_runtime_assets_tool": {
                "kind": "runtime_assets",
                "pcd_gridmap_tool_available": True,
                "manual_annotation_gui_available": False,
                "projection_variants": {
                    "cjl_with_gridmap": True,
                    "cjl_0525_with_gridmap": True,
                },
                "noobscene_localization_variants": {
                    "odom": True,
                    "ins": False,
                },
                "speed_direction_variants": {
                    "odom": True,
                    "ins": False,
                },
                "scene_environment_affects_execution": False,
            },
            "inspect_navigation_calibration_inventory_tool": {
                "kind": "calibration_inventory",
                "sensor_sources": ["frozen_processing_snapshot"],
            },
            "inspect_navigation_localization_sources_tool": {
                "kind": "localization_sources",
                "available_sources": ["odom"],
                "conversion_available": True,
            },
            "inspect_navigation_annotation_job_facts_tool": {
                "kind": "annotation_job_facts",
                "job_status": (
                    "annotated"
                    if self.task.get("requested_outcome") == "trajectory_fix"
                    else "tracked"
                ),
                "segment_count": max(1, len(clips)),
                "tracked_count": max(1, len(clips)),
                "skipped_count": 0,
                "annotated_count": (
                    max(1, len(clips))
                    if self.task.get("requested_outcome") == "trajectory_fix"
                    else 0
                ),
                "ready_for_postprocessing": (
                    self.task.get("requested_outcome") != "trajectory_fix"
                ),
                "ready_for_trajectory_review": (
                    self.task.get("requested_outcome") == "trajectory_fix"
                ),
                "processing_calibration_snapshot_available": True,
                "reviews": {
                    "pending": (
                        max(1, len(clips))
                        if self.task.get("requested_outcome") == "trajectory_fix"
                        else 0
                    ),
                    "in_progress": 0,
                    "returned": 0,
                    "approved": 0,
                    "discarded": 0,
                },
            },
            "inspect_navigation_artifact_state_tool": {
                "kind": "artifact_state",
                "snapshot": {
                    "date": date,
                    "segments": clips or None,
                    "raw_input_exists": True,
                    "raw_temp_exists": True,
                    "sync_data_exists": True,
                    "sync_data_by_segment": {
                        clip: True for clip in clips
                    },
                    "finish_temp_samples_exists": True,
                    "final_outputs_exist": (
                        self.task.get("requested_outcome") == "trajectory_fix"
                    ),
                    "final_grid_map_exists": (
                        self.task.get("requested_outcome") == "trajectory_fix"
                    ),
                    "sync_image_samples": [],
                },
            },
            "inspect_navigation_gridmap_artifacts_tool": {
                "kind": "gridmap_artifacts",
                "existing_gridmap_paths": ["existing_gridmap"],
                "pcd_sources": ["synchronized_pointcloud"],
                "projection_ready": False,
            },
        }

    @staticmethod
    def _kind_for_tool(tool_name: str) -> str:
        return {
            "inspect_navigation_raw_metadata_tool": "raw_metadata",
            "inspect_navigation_sensor_candidates_tool": "sensor_candidates",
            "inspect_navigation_topic_candidates_tool": "topic_candidates",
            "inspect_navigation_runtime_assets_tool": "runtime_assets",
            "inspect_navigation_calibration_inventory_tool": "calibration_inventory",
            "inspect_navigation_localization_sources_tool": "localization_sources",
            "inspect_navigation_annotation_job_facts_tool": "annotation_job_facts",
            "inspect_navigation_artifact_state_tool": "artifact_state",
            "inspect_navigation_gridmap_artifacts_tool": "gridmap_artifacts",
        }[tool_name]

    def _inspect(self, tool_name: str) -> dict[str, Any]:
        self._context_revision += 1
        kind = self._kind_for_tool(tool_name)
        self._completed_kinds.add(kind)
        self._kind_revisions[kind] = self._context_revision
        payload = self._OBSERVATION_MODELS[tool_name].model_validate(
            self._tool_results[tool_name]
        )
        self._payloads_by_kind[kind] = payload
        descriptor = EvidenceDescriptor(
            ref=f"{kind}:eval",
            task_id="eval-navigation-task",
            observation_revision=self._context_revision,
            kind=kind,
            summary=f"Evaluation evidence for {kind}",
            byte_size=0,
            source_tool=tool_name,
            created_at=utc_now(),
        )
        self._evidence_by_kind[kind] = descriptor
        summary = compact_observation_payload(payload)
        summary.pop("kind", None)
        return {
            "ok": True,
            "observation_revision": self._context_revision,
            "observed_kind": kind,
            "summary": summary,
            "evidence_refs": [f"{kind}:eval"],
        }

    @property
    def background_running(self) -> bool:
        return self._background_running

    def visible_tool_names(self) -> frozenset[str]:
        if self._background_running:
            return frozenset()
        if self._submitted_plan is not None:
            actions = self._submitted_plan.get("step_actions") or []
            if actions:
                return frozenset(
                    {
                        "get_current_plan_step_tool",
                        f"{actions[0]}_tool",
                    }
                )
            return frozenset()
        requested_outcome = str(
            self.task.get("requested_outcome") or "postprocessing"
        )
        if requested_outcome == "trajectory_fix":
            return self._TRAJECTORY_REVIEW_PLANNING_TOOL_NAMES
        if requested_outcome in {"postprocessing", "postprocessing_and_fix"}:
            names = set(self._POSTPROCESSING_PLANNING_TOOL_NAMES)
            observation = self._observation_revision()
            annotation_ready = m2_annotation_ready_for_postprocessing(
                observation
            )
            submission_ready = m2_finish_observations_complete(
                observation
            ) and (
                annotation_ready
                or self.task.get("scene_mode") in {"in", "out"}
            )
            if not submission_ready:
                names.discard("submit_finish_processing_plan_tool")
            if (
                not annotation_ready
                and "annotation_job_facts" in observation.completed_kinds
                and self.task.get("scene_mode") not in {"in", "out"}
            ):
                names.add("record_navigation_user_guidance_tool")
            return frozenset(names)
        return frozenset(
            {
                *self._INSPECTION_TOOL_NAMES,
                "get_navigation_task_context_tool",
                "describe_processing_action_tool",
                "submit_extract_sync_plan_tool",
                "submit_finish_processing_plan_tool",
                "submit_trajectory_review_plan_tool",
            }
        )

    def _task_record(self) -> NavigationTask:
        selection = dict(self.task.get("selection") or {"kind": "all_clips"})
        segments = (
            list(selection.get("clips") or [])
            if selection.get("kind") == "selected_clips"
            else None
        )
        requested_outcome = str(
            self.task.get("requested_outcome") or "postprocessing"
        )
        return NavigationTask(
            task_id="eval-navigation-task",
            request=str(self.task.get("request") or ""),
            target=(
                "trajectory_review"
                if requested_outcome == "trajectory_fix"
                else "navigation_data"
            ),
            date=str(self.task["dataset_date"]),
            segments=segments,
            scene_mode=self.task.get("scene_mode"),
            dry_run=True,
            guidance_revision=int(self.task.get("guidance_revision") or 0),
            created_by_web_session_id="evaluation",
            agentscope_session_id="evaluation__navigation-data-agent",
        )

    def _observation_revision(self) -> NavigationObservationRevision:
        return NavigationObservationRevision(
            task_id="eval-navigation-task",
            revision=max(1, self._context_revision),
            completed_kinds=sorted(self._completed_kinds),
            payloads=[
                self._payloads_by_kind[kind]
                for kind in sorted(self._payloads_by_kind)
            ],
            evidence_refs=[
                self._evidence_by_kind[kind].ref
                for kind in sorted(self._evidence_by_kind)
            ],
        )

    def _planning_context(self) -> dict[str, Any]:
        observation = self._observation_revision()
        return build_navigation_task_context(
            task=self._task_record(),
            observation=observation,
            evidence=[
                self._evidence_by_kind[kind]
                for kind in sorted(self._evidence_by_kind)
            ],
            capabilities=list_navigation_tool_capabilities(),
        ).model_dump(mode="json")

    @staticmethod
    def _plan_summary(
        *,
        phase: str,
        plan: Any,
    ) -> dict[str, Any]:
        payload = (
            plan.model_dump(mode="json")
            if hasattr(plan, "model_dump")
            else dict(plan)
        )
        decisions = dict(payload.get("decisions") or {})
        decision_modes: dict[str, str] = {}
        for name, value in decisions.items():
            if not isinstance(value, Mapping):
                continue
            selected = value.get("mode")
            if not isinstance(selected, str):
                selected = value.get("source")
            if isinstance(selected, str):
                decision_modes[str(name)] = selected
        step_actions = [
            str(step.get("action"))
            for step in list(payload.get("steps") or [])
            if isinstance(step, Mapping) and isinstance(step.get("action"), str)
        ]
        step_variants = {
            str(step["action"]): str(step["variant"])
            for step in list(payload.get("steps") or [])
            if (
                isinstance(step, Mapping)
                and isinstance(step.get("action"), str)
                and isinstance(step.get("variant"), str)
            )
        }
        return {
            "operation": "submit_plan",
            "phase": phase,
            "decision_modes": decision_modes,
            "step_actions": step_actions,
            "step_variants": step_variants,
        }

    def _submit(
        self,
        *,
        phase: str,
        planning_context_revision: str,
        plan: Any,
    ) -> dict[str, Any]:
        if self._submitted_plan is not None:
            return {
                "ok": False,
                "error_type": "active_navigation_plan",
                "retry": "execute_current_plan",
            }
        expected = self._planning_context()["planning_context_revision"]
        if planning_context_revision != expected:
            return {
                "ok": False,
                "error_type": "planning_context_mismatch",
                "retry": "refresh_context_and_resubmit_complete_plan",
            }
        expected_phase = (
            "trajectory_review"
            if self.task["requested_outcome"] == "trajectory_fix"
            else "finish_processing"
        )
        if phase != expected_phase:
            return {
                "ok": False,
                "error_type": "unexpected_plan_phase",
                "allowed_values": [expected_phase],
            }
        plan_model = {
            "extract_sync": ExtractSyncPlanInput,
            "finish_processing": FinishProcessingPlanInput,
            "trajectory_review": TrajectoryReviewPlanInput,
        }[phase]
        canonical_plan = plan_model.model_validate(plan)
        validation = validate_navigation_plan(
            task=self._task_record(),
            observation=self._observation_revision(),
            plan=canonical_plan,
            evidence=[
                self._evidence_by_kind[kind]
                for kind in sorted(self._evidence_by_kind)
            ],
            capabilities=list_navigation_tool_capabilities(),
        )
        if not validation.ok:
            return {
                "ok": False,
                "error_type": "plan_validation_failed",
                "errors": [
                    issue.model_dump(mode="json")
                    for issue in validation.errors
                ],
                "retry": "resubmit_complete_plan",
            }
        summary = self._plan_summary(phase=phase, plan=canonical_plan)
        self._submitted_plan = summary
        self._submitted_first_step_id = (
            canonical_plan.steps[0].step_id if canonical_plan.steps else None
        )
        self.recorder.record_handoff(summary)
        first_action = summary["step_actions"][0] if summary["step_actions"] else None
        return {
            "ok": True,
            "plan_id": "eval-plan",
            "plan_revision": 1,
            "step_count": len(summary["step_actions"]),
            "status": "active",
            "next_action": first_action,
        }

    def tools(self) -> list[Any]:
        tools: list[Any] = []
        for tool_name in self._INSPECTION_TOOL_NAMES:
            def make_inspect_tool(name: str):
                def inspect_tool() -> dict[str, Any]:
                    """Inspect current bounded navigation facts for this task."""
                    return self._inspect(name)

                return inspect_tool

            inspect_tool = make_inspect_tool(tool_name)
            inspect_tool.__name__ = tool_name
            tool = FunctionTool(
                inspect_tool,
                name=tool_name,
                is_read_only=True,
            )
            tool.input_schema = {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            }
            tools.append(tool)

        def get_navigation_task_context_tool() -> dict[str, Any]:
            """Read the latest task facts and optimistic planning revision."""
            return self._planning_context()

        def describe_processing_action_tool(action: str) -> dict[str, Any]:
            """Read the bounded contract for one candidate processing action."""
            return {
                "action": action,
                "available": True,
                "model_selects_variant": True,
            }

        def record_navigation_user_guidance_tool(
            text: str,
            scene_mode: str | None = None,
        ) -> dict[str, Any]:
            """Record bounded user guidance for this in-memory task."""
            guidance = text.strip()
            if not guidance or len(guidance) > 4_000:
                return {
                    "ok": False,
                    "error_type": "invalid_navigation_user_guidance",
                }
            if scene_mode not in {None, "in", "out"}:
                return {
                    "ok": False,
                    "error_type": "invalid_navigation_user_guidance",
                }
            if scene_mode is not None:
                self.task["scene_mode"] = scene_mode
            self.task["guidance_revision"] = int(
                self.task.get("guidance_revision") or 0
            ) + 1
            self._context_revision += 1
            kind = "user_guidance"
            self._completed_kinds.add(kind)
            self._kind_revisions[kind] = self._context_revision
            self._payloads_by_kind[kind] = UserGuidanceObservation(
                guidance_revision=self.task["guidance_revision"],
                text=guidance,
            )
            self._evidence_by_kind[kind] = EvidenceDescriptor(
                ref=f"{kind}:eval",
                task_id="eval-navigation-task",
                observation_revision=self._context_revision,
                kind=kind,
                summary="Evaluation user guidance",
                byte_size=0,
                source_tool="record_navigation_user_guidance_tool",
                created_at=utc_now(),
            )
            return {
                "ok": True,
                "guidance_revision": self.task["guidance_revision"],
                "observation_revision": self._context_revision,
            }

        def get_current_plan_step_tool(plan_id: str) -> dict[str, Any]:
            """Read the accepted identity; use top-level plan_id and step_id unchanged."""
            if plan_id != "eval-plan" or self._submitted_plan is None:
                return {"ok": False, "error_type": "inactive_navigation_plan"}
            actions = self._submitted_plan.get("step_actions") or []
            if not actions or self._submitted_first_step_id is None:
                return {"ok": False, "error_type": "inactive_navigation_plan"}
            return project_actionable_plan_step({
                "plan_id": "eval-plan",
                "step": {
                    "id": "eval-step-record",
                    "step_id": self._submitted_first_step_id,
                    "action": actions[0],
                    "status": "pending",
                },
            })

        def submit_extract_sync_plan_tool(
            planning_context_revision: str,
            plan: dict[str, Any],
        ) -> dict[str, Any]:
            """Validate one complete extract/sync Plan."""
            return self._submit(
                phase="extract_sync",
                planning_context_revision=planning_context_revision,
                plan=plan,
            )

        def submit_finish_processing_plan_tool(
            planning_context_revision: str,
            plan: dict[str, Any],
        ) -> dict[str, Any]:
            """Validate one complete postprocessing Plan."""
            return self._submit(
                phase="finish_processing",
                planning_context_revision=planning_context_revision,
                plan=plan,
            )

        def submit_trajectory_review_plan_tool(
            planning_context_revision: str,
            plan: dict[str, Any],
        ) -> dict[str, Any]:
            """Validate one complete linked trajectory-review Plan."""
            return self._submit(
                phase="trajectory_review",
                planning_context_revision=planning_context_revision,
                plan=plan,
            )

        def _run_first_step(
            plan_id: str,
            step_id: str,
            *,
            expected_action: str,
        ) -> dict[str, Any]:
            if plan_id != "eval-plan" or self._submitted_plan is None:
                return {"ok": False, "error_type": "inactive_navigation_plan"}
            first_action = self._submitted_plan["step_actions"][0]
            if (
                first_action != expected_action
                or step_id != self._submitted_first_step_id
            ):
                return {
                    "ok": False,
                    "error_type": "step_action_mismatch",
                    "next_action": first_action,
                }
            self._background_running = True
            return {
                "ok": True,
                "status": "running_in_background",
                "step_id": step_id,
                "action": first_action,
            }

        def run_annotation_postprocessing_workflow_tool(
            plan_id: str,
            step_id: str,
        ) -> dict[str, Any]:
            """Start the accepted plan-bound postprocessing workflow."""
            return _run_first_step(
                plan_id,
                step_id,
                expected_action="run_annotation_postprocessing_workflow",
            )

        def open_trajectory_fix_workbench_tool(
            plan_id: str,
            step_id: str,
        ) -> dict[str, Any]:
            """Open the accepted durable human Fix workbench handoff."""
            return _run_first_step(
                plan_id,
                step_id,
                expected_action="open_trajectory_fix_workbench",
            )

        extract_plan_tool = FunctionTool(
            submit_extract_sync_plan_tool,
            is_concurrency_safe=False,
        )
        finish_plan_tool = FunctionTool(
            submit_finish_processing_plan_tool,
            is_concurrency_safe=False,
        )
        review_plan_tool = FunctionTool(
            submit_trajectory_review_plan_tool,
            is_concurrency_safe=False,
        )
        for plan_tool, model in (
            (extract_plan_tool, ExtractSyncPlanInput),
            (finish_plan_tool, FinishProcessingPlanInput),
            (review_plan_tool, TrajectoryReviewPlanInput),
        ):
            plan_schema = model.model_json_schema()
            plan_definitions = plan_schema.pop("$defs", {})
            plan_tool.input_schema = {
                "type": "object",
                "properties": {
                    "planning_context_revision": {"type": "string"},
                    "plan": plan_schema,
                },
                "required": ["planning_context_revision", "plan"],
                "additionalProperties": False,
            }
            if plan_definitions:
                plan_tool.input_schema["$defs"] = plan_definitions
        tools.extend(
            [
                FunctionTool(get_navigation_task_context_tool, is_read_only=True),
                FunctionTool(describe_processing_action_tool, is_read_only=True),
                FunctionTool(
                    record_navigation_user_guidance_tool,
                    is_concurrency_safe=False,
                ),
                extract_plan_tool,
                finish_plan_tool,
                review_plan_tool,
                FunctionTool(get_current_plan_step_tool, is_read_only=True),
                FunctionTool(
                    run_annotation_postprocessing_workflow_tool,
                    is_concurrency_safe=False,
                ),
                FunctionTool(
                    open_trajectory_fix_workbench_tool,
                    is_concurrency_safe=False,
                ),
            ],
        )
        if self.task.get("requested_outcome") == "trajectory_fix":
            trajectory_review_tool_names = {
                "inspect_navigation_annotation_job_facts_tool",
                "get_navigation_task_context_tool",
                "submit_trajectory_review_plan_tool",
                "get_current_plan_step_tool",
                "open_trajectory_fix_workbench_tool",
            }
            tools = [
                tool
                for tool in tools
                if tool.name in trajectory_review_tool_names
            ]
        for tool in tools:
            tool.input_schema["additionalProperties"] = False
        return [_TrustedNavigationTool(tool) for tool in tools]


class RecordingNavigationToolSurfaceMiddleware(MiddlewareBase):
    """Mirror the production phase-bound Navigation tool projection."""

    def __init__(self, runtime: RecordingNavigationRuntime) -> None:
        self._runtime = runtime

    async def on_reasoning(self, agent, input_kwargs, next_handler):
        if self._runtime.background_running:
            yield AssistantMsg(
                id=agent.state.reply_id,
                name=agent.name,
                content=(
                    "Background navigation processing is still running; "
                    "the session will resume automatically after completion."
                ),
            )
            return
        async for item in next_handler(**input_kwargs):
            yield item

    async def on_model_call(self, agent, input_kwargs, next_handler):
        del agent
        visible = self._runtime.visible_tool_names()
        tools = [
            schema
            for schema in input_kwargs.get("tools", [])
            if schema.get("function", {}).get("name") in visible
        ]
        return await next_handler(**{**input_kwargs, "tools": tools})

    async def on_acting(self, agent, input_kwargs, next_handler):
        del agent
        tool_call = input_kwargs["tool_call"]
        if tool_call.name not in self._runtime.visible_tool_names():
            yield ToolResponse(
                id=tool_call.id,
                content=[
                    TextBlock(
                        text="The navigation tool is not active in this phase."
                    )
                ],
                state=ToolResultState.ERROR,
                metadata={
                    "ok": False,
                    "error_type": "navigation_tool_not_active",
                },
            )
            return
        async for item in next_handler(**input_kwargs):
            yield item


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
        entrypoint: str = "router",
    ) -> None:
        self.config = config
        self.workspace_root = Path(workspace_root)
        self.model_factory = model_factory
        self.runtime_setup = dict(runtime_setup or {})
        if entrypoint not in {"router", "navigation"}:
            raise ValueError(f"unsupported evaluation entrypoint {entrypoint!r}")
        self.entrypoint = entrypoint
        self.recorder = TraceRecorder.for_workspace(self.workspace_root)
        self.storage = InMemoryStorage()
        self.message_bus = InMemoryMessageBus(self.recorder)
        self.workspace_manager = LocalWorkspaceManager(
            basedir=str(self.workspace_root / "agentscope-workspaces"),
        )
        self.background_task_manager = BackgroundTaskManager()
        self.scheduler_manager = SchedulerManager(self.storage, self.message_bus)
        self._router_runtime: RecordingRouterRuntime | None = None
        self._navigation_runtime: RecordingNavigationRuntime | None = None

    async def _extra_tools(
        self,
        _user_id: str,
        agent_id: str,
        session_id: str,
    ) -> list[Any]:
        if self.entrypoint == "router":
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
        if agent_id != self.config.navigation_agent_id:
            return []
        if self._navigation_runtime is None:
            self._navigation_runtime = RecordingNavigationRuntime(
                self.recorder,
                runtime_setup=self.runtime_setup,
            )
        return self._navigation_runtime.tools()

    async def _extra_middlewares(
        self,
        _user_id: str,
        agent_id: str,
        session_id: str,
    ) -> list[Any]:
        if self.entrypoint == "router":
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
        if agent_id != self.config.navigation_agent_id:
            return []
        if self._navigation_runtime is None:
            self._navigation_runtime = RecordingNavigationRuntime(
                self.recorder,
                runtime_setup=self.runtime_setup,
            )
        return [
            RecordingNavigationToolSurfaceMiddleware(
                self._navigation_runtime,
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
        agent_id = (
            self.config.main_router_agent_id
            if self.entrypoint == "router"
            else self.config.navigation_agent_id
        )
        model = (
            self.config.router_model
            if self.entrypoint == "router"
            else self.config.navigation_model
        )
        session_id = f"{web_session_id}__{agent_id}"
        await self.storage.upsert_session(
            self.config.user_id,
            agent_id,
            SessionConfig(
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
            if self.entrypoint == "navigation":
                navigation_task = self.runtime_setup.get("navigation_task")
                if isinstance(navigation_task, dict):
                    navigation_task.setdefault("request", messages[0])
            for turn_index, content in enumerate(messages):
                if self.entrypoint == "router" and self._router_runtime is not None:
                    self._router_runtime.begin_user_turn(turn_index)
                if self.entrypoint == "navigation" and turn_index == 0:
                    navigation_task = self.runtime_setup.get("navigation_task")
                    if not isinstance(navigation_task, Mapping):
                        raise ValueError("navigation evaluation task setup is unavailable")
                    selection = dict(navigation_task.get("selection") or {})
                    content = _navigation_handoff_message(
                        request=content,
                        date=str(navigation_task.get("dataset_date") or ""),
                        scene_mode=str(navigation_task.get("scene_mode") or "unknown"),
                        clips=list(selection.get("clips") or []),
                        response_language="Chinese",
                        requested_outcome=str(
                            navigation_task.get("requested_outcome") or "postprocessing"
                        ),
                    )
                await service._run_impl(
                    self.config.user_id,
                    session_id,
                    agent_id,
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


async def run_navigation_case(
    message: str,
    *,
    config: AgentScopeRuntimeConfig,
    workspace_root: str | Path,
    web_session_id: str = "eval",
    model_factory: Callable[..., Any] | None = None,
    runtime_setup: Mapping[str, Any],
) -> HostRunResult:
    """Run one isolated NavigationDataAgent turn against bounded fake facts."""

    host = EvaluationHost(
        config=config,
        workspace_root=workspace_root,
        model_factory=model_factory,
        runtime_setup=runtime_setup,
        entrypoint="navigation",
    )
    return await host.run(message, web_session_id=web_session_id)

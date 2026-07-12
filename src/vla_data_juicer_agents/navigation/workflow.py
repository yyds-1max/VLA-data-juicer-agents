import asyncio
import json
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Literal

from agentscope.message import UserMsg

from vla_data_juicer_agents.adapters.agentscope.events import AgentScopeEventAdapter, ProgressSummaryFilter
from vla_data_juicer_agents.core.cancellation import CancellationContext, TurnCancelled, current_cancellation
from vla_data_juicer_agents.core.events import EventEmitter, EventScope
from vla_data_juicer_agents.navigation.agent_tools import resolve_navigation_agent_tools
from vla_data_juicer_agents.navigation.agents import create_plan_agent
from vla_data_juicer_agents.navigation.models import NavigationRequest
from vla_data_juicer_agents.navigation.plan_models import NavigationPlanRecord
from vla_data_juicer_agents.navigation.run_state import WorkflowRunStore
from vla_data_juicer_agents.navigation.services import NavigationServices, build_navigation_services
from vla_data_juicer_agents.navigation.task_reconciliation import prepare_navigation_task_entry


PUBLIC_PROGRESS_PROMPT = (
    "Before each tool call emit one public `Progress:` line with an established fact and next action. "
    "Do not reveal private reasoning or raw tool results; use the registered SDK tool interface."
)


def _response_language_prompt(response_language: str | None) -> str:
    language = str(response_language or "").strip()
    if not language:
        return ""
    return (
        f"User-facing progress and final summaries must be written in {language}; "
        "keep the literal marker `Progress:` in English.\n\n"
    )


def prepare_direct_navigation_entry(
    *,
    run_dir: Path,
    request: NavigationRequest,
    settings: Any,
    agentscope_session_id: str,
    user_request: str = "",
) -> tuple[NavigationServices, Any]:
    """Build run-scoped durable services and perform the unconditional entry gate."""
    services = build_navigation_services(run_dir, settings=settings)
    handoff = {
        "date": request.date,
        "segments": request.segments,
        "scene_mode": request.scene_mode,
        "dry_run": request.dry_run,
        "request": user_request,
    }
    task = prepare_navigation_task_entry(
        task_store=services.task_store,
        observation_store=services.observation_store,
        evidence_store=services.evidence_store,
        message=f"Structured handoff JSON: {json.dumps(handoff, ensure_ascii=False)}",
        web_session_id=None,
        agentscope_session_id=agentscope_session_id,
        settings=services.settings,
    )
    return services, task


def _event_type(event: object) -> str:
    event_type = getattr(event, "type", None)
    if hasattr(event_type, "value"):
        return str(event_type.value)
    if event_type is not None:
        return str(event_type)
    return type(event).__name__


def _event_payload(event: object) -> dict[str, Any]:
    if hasattr(event, "model_dump"):
        return event.model_dump(mode="json")
    if hasattr(event, "__dict__"):
        return dict(vars(event))
    return {"repr": repr(event)}


def _event_text_delta(event: object) -> str:
    if _event_type(event) != "TEXT_BLOCK_DELTA":
        return ""
    delta = getattr(event, "delta", "")
    return delta if isinstance(delta, str) else str(delta)


def _event_tool_result_delta(event: object) -> str:
    if _event_type(event) != "TOOL_RESULT_TEXT_DELTA":
        return ""
    delta = getattr(event, "delta", "")
    return delta if isinstance(delta, str) else str(delta)


async def _run_agent_stream(
    agent,
    prompt: str,
    run_store: WorkflowRunStore | None = None,
    run_dir: Path | None = None,
    *,
    event_scope: EventScope | None = None,
    cancellation: CancellationContext | None = None,
    emit_tool_events: bool = True,
) -> str:
    del run_store, run_dir
    scope = event_scope or EventEmitter().scope("agent")
    adapter = AgentScopeEventAdapter(scope, emit_tool_events=emit_tool_events)
    progress_filter = ProgressSummaryFilter(scope)
    active_cancellation = cancellation or current_cancellation()
    output_chunks: list[str] = []
    tool_output_chunks: list[str] = []
    scope.emit("agent_start")
    try:
        async with AsyncExitStack() as stack:
            if active_cancellation is not None:
                await stack.enter_async_context(active_cancellation.track_agent(agent))
                active_cancellation.raise_if_cancelled()
            async for event in agent.reply_stream(UserMsg(name="user", content=prompt)):
                event_type = _event_type(event)
                if event_type != "TEXT_BLOCK_DELTA":
                    progress_filter.flush_progress_only()
                adapter.accept(event)
                rendered = progress_filter.consume_text_delta(_event_text_delta(event))
                if rendered:
                    scope.emit("assistant_delta", delta=rendered)
                output_chunks.append(rendered)
                tool_output_chunks.append(_event_tool_result_delta(event))
                if event_type == "REQUIRE_USER_CONFIRM":
                    adapter.close_active_tools("failed")
                    raise RuntimeError(
                        "AgentScope tool call requires user confirmation; use durable human-decision events."
                    )
            if active_cancellation is not None:
                active_cancellation.raise_if_cancelled()
            adapter.close_active_tools("completed")
            flushed = progress_filter.flush()
            if flushed:
                scope.emit("assistant_delta", delta=flushed)
            output_chunks.append(flushed)
            scope.emit("agent_end", status="completed")
            return "".join(output_chunks) or "".join(tool_output_chunks)
    except TurnCancelled:
        adapter.close_active_tools("interrupted")
        scope.emit("agent_end", status="interrupted")
        raise
    except asyncio.CancelledError as exc:
        adapter.close_active_tools("interrupted")
        scope.emit("agent_end", status="interrupted")
        if active_cancellation is not None and active_cancellation.cancelled:
            raise TurnCancelled("The current turn was interrupted.") from exc
        raise
    except BaseException:
        adapter.close_active_tools("failed")
        scope.emit("agent_end", status="failed")
        raise


async def run_plan_agent(
    agent,
    request: NavigationRequest,
    *,
    plan_store: Any,
    task_id: str,
    phase: Literal["extract_sync", "finish_processing"],
    run_store: WorkflowRunStore | None = None,
    run_dir: Path | None = None,
    event_scope: EventScope | None = None,
    cancellation: CancellationContext | None = None,
    response_language: str | None = None,
) -> NavigationPlanRecord:
    prompt = (
        "Investigate the active durable navigation phase with the resolved tools. "
        "When the factual checklist is complete, submit one complete strict JSON plan. "
        "Do not return plan JSON in assistant text.\n\n"
        f"{PUBLIC_PROGRESS_PROMPT}\n\n{_response_language_prompt(response_language)}"
        f"Task identity: {json.dumps({'task_id': task_id, 'phase': phase, 'date': request.date}, ensure_ascii=False)}"
    )
    await _run_agent_stream(
        agent,
        prompt,
        run_store=run_store,
        run_dir=run_dir,
        event_scope=event_scope,
        cancellation=cancellation,
    )
    plan = plan_store.get_active(task_id, phase)
    if plan is None:
        raise RuntimeError(
            f"Navigation planning agent did not submit a valid complete plan for task {task_id} phase {phase}."
        )
    return plan


async def run_direct_plan_until_submitted(
    *,
    services: NavigationServices,
    task: Any,
    request: NavigationRequest,
    agentscope_session_id: str,
    model: str | None = None,
    run_store: WorkflowRunStore | None = None,
    run_dir: Path | None = None,
    event_scope: EventScope | None = None,
    cancellation: CancellationContext | None = None,
    response_language: str | None = None,
    max_rounds: int = 10,
) -> NavigationPlanRecord:
    """Refresh phase tools between durable investigation and submission rounds."""
    if max_rounds < 1:
        raise ValueError("max_rounds must be positive")
    current = services.task_store.get_task(task.task_id) or task
    last_revision = current.state_revision
    last_error: RuntimeError | None = None
    for _ in range(max_rounds):
        tools = resolve_navigation_agent_tools(
            services=services,
            agentscope_session_id=agentscope_session_id,
            cancellation=cancellation,
        )
        agent = create_plan_agent(model=model, tools=tools)
        try:
            return await run_plan_agent(
                agent,
                request,
                plan_store=services.plan_store,
                task_id=current.task_id,
                phase=current.phase.value,
                run_store=run_store,
                run_dir=run_dir,
                event_scope=event_scope,
                cancellation=cancellation,
                response_language=response_language,
            )
        except RuntimeError as exc:
            if "did not submit a valid complete plan" not in str(exc):
                raise
            last_error = exc
        refreshed = services.task_store.get_task(current.task_id)
        if refreshed is None or refreshed.state_revision == last_revision:
            raise last_error
        current = refreshed
        last_revision = refreshed.state_revision
    assert last_error is not None
    raise last_error


async def run_executor_agent(
    agent,
    plan: NavigationPlanRecord,
    *,
    execution_overview: dict[str, Any],
    run_store: WorkflowRunStore | None = None,
    run_dir: Path | None = None,
    event_scope: EventScope | None = None,
    cancellation: CancellationContext | None = None,
    response_language: str | None = None,
    resume_from_checkpoint: bool = False,
) -> str:
    del resume_from_checkpoint
    anchor = {
        "plan_id": plan.plan_id,
        "plan_revision": plan.plan_revision,
        "execution": execution_overview,
    }
    prompt = (
        "Execute the current immutable plan using only plan-bound tools. Read the current step and call it "
        "with plan_id and step_id; canonical arguments remain server-side. Stop on failure or human handoff. "
        f"{PUBLIC_PROGRESS_PROMPT}\n\n{_response_language_prompt(response_language)}"
        f"Durable execution anchor: {json.dumps(anchor, ensure_ascii=False, sort_keys=True)}"
    )
    return await _run_agent_stream(
        agent,
        prompt,
        run_store=run_store,
        run_dir=run_dir,
        event_scope=event_scope,
        cancellation=cancellation,
    )

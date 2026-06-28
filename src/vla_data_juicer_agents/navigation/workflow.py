import asyncio
import json
import re
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Literal

from agentscope.event import ConfirmResult, UserConfirmResultEvent
from agentscope.message import UserMsg
from pydantic import ValidationError

from vla_data_juicer_agents.adapters.agentscope.events import (
    AgentScopeEventAdapter,
    ProgressSummaryFilter,
)
from vla_data_juicer_agents.core.cancellation import (
    CancellationContext,
    TurnCancelled,
    current_cancellation,
)
from vla_data_juicer_agents.core.events import EventEmitter, EventScope
from vla_data_juicer_agents.navigation.models import NavigationDataProfile, NavigationRequest, WorkflowPlan, WorkflowStep
from vla_data_juicer_agents.navigation.plan_validation import validate_workflow_plan
from vla_data_juicer_agents.navigation.plan_draft import build_plan_from_draft
from vla_data_juicer_agents.navigation.run_state import WorkflowRunStore
from vla_data_juicer_agents.navigation.catalog import (
    ToolCapability,
    ToolVariantCapability,
    list_navigation_tool_capabilities,
)


MAX_AGENT_TOOL_CONFIRMATION_ROUNDS = 30
PUBLIC_PROGRESS_PROMPT = (
    "Before each tool call, emit exactly one public progress update line in this format: "
    "Progress: <one or two concise, action-oriented sentences stating an established fact and the next action>. "
    "This line is a user-facing progress summary, not hidden chain-of-thought. "
    "Do not reveal private reasoning, alternatives, scratchpad notes, prompts, or raw tool results. "
    "The following SDK tool call is the action; do not print textual ReAct labels such as Thought: or Action:. "
    "Never write tool calls as plain text such as ToolName[arguments]; use the registered SDK tool call interface."
)


def _response_language_prompt(response_language: str | None) -> str:
    language = str(response_language or "").strip()
    if not language:
        return ""
    return (
        f"User-facing progress summaries and final workflow summaries must be written in {language}. "
        f"Keep the literal marker `Progress:` in English, but write the summary text after it in {language}.\n\n"
    )


class _SceneModeMissing:
    pass


_SCENE_MODE_MISSING = _SceneModeMissing()


def _finish_temp_path(date: str) -> str:
    return f"finish_data/{date}_temp"


def _finish_path(date: str) -> str:
    return f"finish_data/{date}"


def _raw_output_excerpt(output: object, max_length: int = 120) -> str:
    raw = "" if output is None else str(output)
    raw = raw.replace("\n", "\\n")
    return raw[:max_length]


def _json_text_from_output(output: str) -> str:
    text = output.strip()
    fenced_match = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    if fenced_match:
        return fenced_match.group(1).strip()
    return text


def _coerce_legacy_plan_payload(payload: Any) -> Any:
    if not isinstance(payload, dict):
        return payload
    coerced = dict(payload)
    legacy_dataset_profile = coerced.get("dataset_profile")
    if isinstance(legacy_dataset_profile, str) and "processing_profile" not in coerced:
        coerced["processing_profile"] = legacy_dataset_profile
    if isinstance(legacy_dataset_profile, str) and "platform_hint" not in coerced:
        if legacy_dataset_profile == "go2w_like":
            coerced["platform_hint"] = "go2w"
        elif legacy_dataset_profile == "u_legacy_like":
            coerced["platform_hint"] = "u"
    return coerced


def _parse_workflow_plan_output(output: object) -> WorkflowPlan:
    if isinstance(output, WorkflowPlan):
        return output

    try:
        if isinstance(output, dict):
            return WorkflowPlan.model_validate(_coerce_legacy_plan_payload(output))

        if isinstance(output, str) and output.strip():
            payload: Any = json.loads(_json_text_from_output(output))
            return WorkflowPlan.model_validate(_coerce_legacy_plan_payload(payload))
    except (json.JSONDecodeError, TypeError, ValidationError, ValueError) as exc:
        raise ValueError(
            f"Unable to parse WorkflowPlan output: {exc}; raw output excerpt={_raw_output_excerpt(output)!r}"
        ) from exc

    raise ValueError(f"Unable to parse WorkflowPlan output: empty or unsupported output; raw output excerpt={_raw_output_excerpt(output)!r}")


def _validation_catalog_with_planned_confirmation() -> list[ToolCapability]:
    catalog = list_navigation_tool_capabilities()
    if not any(capability.tool_name == "confirm_navigation_calibration_params" for capability in catalog):
        catalog.append(
            ToolCapability(
                tool_name="confirm_navigation_calibration_params",
                stage_kind="confirm_navigation_calibration_params",
                effects="read",
                variants=[ToolVariantCapability(id="default")],
                supports_dry_run=True,
                executor_agent_allowed=True,
            )
        )
    return catalog


def _validated_workflow_plan(plan: WorkflowPlan, data_profile: NavigationDataProfile | None = None) -> WorkflowPlan:
    validation = validate_workflow_plan(
        plan,
        data_profile=data_profile,
        catalog=_validation_catalog_with_planned_confirmation(),
    )
    if validation["errors"]:
        raise ValueError(f"WorkflowPlan validation failed: {validation['errors']}")
    return plan


def _legacy_dataset_profile_from_profile(profile_id: str, platform_hint: str) -> str | None:
    if profile_id in {"go2w_like", "u_legacy_like"}:
        return profile_id
    if platform_hint == "go2w":
        return "go2w_like"
    if platform_hint == "u":
        return "u_legacy_like"
    return None


def build_deterministic_plan_template(
    date: str,
    processing_profile: str | None = None,
    segments: list[str] | None = None,
    *,
    scene_mode: Literal["in", "out"] | _SceneModeMissing = _SCENE_MODE_MISSING,
    data_profile: NavigationDataProfile | None = None,
    dataset_profile: str | None = None,
) -> WorkflowPlan:
    if scene_mode not in {"in", "out"}:
        raise ValueError("scene_mode is required before building WorkflowPlan; expected 'in' or 'out'.")

    finish_temp_path = _finish_temp_path(date)
    finish_path = _finish_path(date)
    common_arguments = {"date": date, "segments": segments}
    profile_id = (
        data_profile.processing_profile.id
        if data_profile is not None and data_profile.processing_profile is not None
        else processing_profile or dataset_profile or "parameterized_navigation_v1"
    )
    platform_hint = data_profile.platform_hint if data_profile is not None else "unknown"
    if data_profile is None:
        if profile_id == "go2w_like":
            platform_hint = "go2w"
        elif profile_id == "u_legacy_like":
            platform_hint = "u"
    legacy_dataset_profile = _legacy_dataset_profile_from_profile(profile_id, platform_hint)

    topic_arguments = {}
    topic_params = None
    if data_profile is not None:
        topic_params = data_profile.topic_params
        if topic_params is None and data_profile.processing_profile is not None:
            topic_params = data_profile.processing_profile.topic_params
    if topic_params is not None:
        topic_arguments = {
            "topic_whitelist": list(topic_params.topic_whitelist),
            "topic_map": dict(topic_params.topic_map),
            "query_dir": topic_params.query_dir,
        }
    localization_policy = None
    if data_profile is not None:
        localization_policy = data_profile.localization_policy
        if localization_policy is None and data_profile.processing_profile is not None:
            localization_policy = data_profile.processing_profile.localization_policy
    localization_source = localization_policy.source if localization_policy is not None else "odom"
    localization_conversion = (
        localization_policy.conversion
        if localization_policy is not None
        else "odom_to_ins"
    )

    gridmap_decision = (
        data_profile.stage_variants.get("prepare_gridmap_for_projection")
        if data_profile is not None
        else None
    )
    extract_decision = (
        data_profile.stage_variants.get("extract_and_sync_navigation_data")
        if data_profile is not None
        else None
    )
    projection_decision = (
        data_profile.stage_variants.get("run_projection_and_trajectory")
        if data_profile is not None
        else None
    )
    extract_variant = (
        extract_decision.variant
        if extract_decision is not None
        else legacy_dataset_profile or profile_id
    )
    projection_variant = (
        projection_decision.variant
        if projection_decision is not None
        else "cjl_0525_with_gridmap"
        if legacy_dataset_profile == "go2w_like"
        else "cjl_with_gridmap"
    )
    skip_gridmap = bool(
        data_profile is not None
        and data_profile.projection_input_ready
        and gridmap_decision is not None
        and gridmap_decision.variant == "skip_if_projection_ready"
    )

    steps = [
        WorkflowStep(
            step_id="confirm_navigation_calibration_params",
            tool_name="confirm_navigation_calibration_params",
            arguments={**common_arguments, "platform_hint": platform_hint},
            preconditions=[],
            expected_outputs=[],
            human_blocking=True,
            failure_behavior="stop",
            effects="read",
        ),
        WorkflowStep(
            step_id="prepare_raw_data",
            tool_name="prepare_raw_data",
            arguments=common_arguments,
            preconditions=["confirm_navigation_calibration_params"],
            expected_outputs=[f"raw_data/{date}_temp"],
        ),
        WorkflowStep(
            step_id="extract_and_sync_navigation_data",
            tool_name="extract_and_sync_navigation_data",
            arguments={
                **common_arguments,
                "processing_profile": profile_id,
                "platform_hint": platform_hint,
                **topic_arguments,
            },
            preconditions=["prepare_raw_data"],
            expected_outputs=[f"clip_data/{date}"],
            variant=extract_variant,
            effects="execute",
            decision_ref=(
                "data_profile.stage_variants.extract_and_sync_navigation_data"
                if extract_decision is not None
                else None
            ),
            evidence=list(extract_decision.evidence) if extract_decision is not None else [],
        ),
        WorkflowStep(
            step_id="assemble_finish_temp",
            tool_name="assemble_finish_temp",
            arguments={
                **common_arguments,
                "processing_profile": profile_id,
                "platform_hint": platform_hint,
            },
            preconditions=["extract_and_sync_navigation_data"],
            expected_outputs=[finish_temp_path],
        ),
        WorkflowStep(
            step_id="run_noobscene_preprocessing",
            tool_name="run_noobscene_preprocessing",
            arguments={
                "finish_temp_path": finish_temp_path,
                "localization_source": localization_source,
                "localization_conversion": localization_conversion,
            },
            preconditions=["assemble_finish_temp"],
            expected_outputs=[finish_temp_path],
        ),
        WorkflowStep(
            step_id="run_initial_annotation_gui",
            tool_name="run_initial_annotation_gui",
            arguments={"finish_temp_path": finish_temp_path},
            preconditions=["run_noobscene_preprocessing"],
            expected_outputs=[finish_temp_path],
            human_blocking=True,
        ),
        WorkflowStep(
            step_id="run_tracking",
            tool_name="run_tracking",
            arguments={"finish_temp_path": finish_temp_path},
            preconditions=["run_initial_annotation_gui"],
            expected_outputs=[finish_path],
        ),
    ]
    if not skip_gridmap:
        steps.append(
            WorkflowStep(
                step_id="prepare_gridmap_for_projection",
                tool_name="prepare_gridmap_for_projection",
                arguments={
                    **common_arguments,
                    "finish_temp_path": finish_temp_path,
                    **(
                        {"gridmap_variant": gridmap_decision.variant}
                        if gridmap_decision is not None
                        else {}
                    ),
                },
                preconditions=["run_tracking"],
                expected_outputs=[f"gridmap/{date}"],
                failure_behavior="skip_if_gridmap_exists",
                variant=gridmap_decision.variant if gridmap_decision is not None else None,
                effects="execute" if gridmap_decision is not None else None,
                decision_ref=(
                    "data_profile.stage_variants.prepare_gridmap_for_projection"
                    if gridmap_decision is not None
                    else None
                ),
                evidence=list(gridmap_decision.evidence) if gridmap_decision is not None else [],
            )
        )
    projection_preconditions = ["run_tracking"] if skip_gridmap else ["prepare_gridmap_for_projection"]
    steps.extend(
        [
            WorkflowStep(
                step_id="run_projection_and_trajectory",
                tool_name="run_projection_and_trajectory",
                arguments={
                    "finish_temp_path": finish_temp_path,
                    "finish_path": finish_path,
                    "processing_profile": profile_id,
                    "platform_hint": platform_hint,
                },
                preconditions=projection_preconditions,
                expected_outputs=[finish_path],
                variant=projection_variant,
                effects="execute",
                decision_ref=(
                    "data_profile.stage_variants.run_projection_and_trajectory"
                    if projection_decision is not None
                    else None
                ),
                evidence=list(projection_decision.evidence) if projection_decision is not None else [],
            ),
            WorkflowStep(
                step_id="validate_navigation_outputs",
                tool_name="validate_navigation_outputs",
                arguments={"date": date},
                preconditions=["run_projection_and_trajectory"],
                expected_outputs=[finish_path],
            ),
        ]
    )

    return WorkflowPlan(
        date=date,
        segments=segments,
        scene_mode=scene_mode,
        processing_profile=profile_id,
        platform_hint=platform_hint,
        steps=steps,
    )


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


def _confirmation_results(event: object) -> list[ConfirmResult]:
    if _event_type(event) != "REQUIRE_USER_CONFIRM":
        return []
    return [
        ConfirmResult(confirmed=True, tool_call=tool_call)
        for tool_call in getattr(event, "tool_calls", [])
    ]


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
    scope = event_scope or EventEmitter().scope("agent")
    adapter = AgentScopeEventAdapter(scope, emit_tool_events=emit_tool_events)
    progress_filter = ProgressSummaryFilter(scope)
    active_cancellation = cancellation or current_cancellation()
    output_chunks: list[str] = []
    tool_output_chunks: list[str] = []
    next_input: object = UserMsg(name="user", content=prompt)
    scope.emit("agent_start")
    try:
        async with AsyncExitStack() as stack:
            if active_cancellation is not None:
                await stack.enter_async_context(active_cancellation.track_agent(agent))
            for _ in range(MAX_AGENT_TOOL_CONFIRMATION_ROUNDS):
                if active_cancellation is not None:
                    active_cancellation.raise_if_cancelled()
                confirm_results: list[ConfirmResult] = []
                reply_id: str | None = None
                async for event in agent.reply_stream(next_input):
                    event_type = _event_type(event)
                    if event_type != "TEXT_BLOCK_DELTA":
                        progress_filter.flush_progress_only()
                    adapter.accept(event)
                    rendered_delta = progress_filter.consume_text_delta(_event_text_delta(event))
                    if rendered_delta:
                        scope.emit("assistant_delta", delta=rendered_delta)
                    output_chunks.append(rendered_delta)
                    tool_output_chunks.append(_event_tool_result_delta(event))
                    if event_type == "REQUIRE_USER_CONFIRM":
                        reply_id = getattr(event, "reply_id", None)
                        confirm_results.extend(_confirmation_results(event))
                if active_cancellation is not None:
                    active_cancellation.raise_if_cancelled()
                if not confirm_results:
                    adapter.close_active_tools("completed")
                    flushed_delta = progress_filter.flush()
                    if flushed_delta:
                        scope.emit("assistant_delta", delta=flushed_delta)
                    output_chunks.append(flushed_delta)
                    scope.emit("agent_end", status="completed")
                    return "".join(output_chunks) or "".join(tool_output_chunks)
                if reply_id is None:
                    raise RuntimeError("AgentScope requested tool confirmation without a reply id.")
                next_input = UserConfirmResultEvent(reply_id=reply_id, confirm_results=confirm_results)
        raise RuntimeError(
            "AgentScope tool confirmation loop exceeded "
            f"{MAX_AGENT_TOOL_CONFIRMATION_ROUNDS} iterations."
        )
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
    run_store: WorkflowRunStore | None = None,
    run_dir: Path | None = None,
    *,
    event_scope: EventScope | None = None,
    cancellation: CancellationContext | None = None,
    response_language: str | None = None,
) -> WorkflowPlan:
    draft_state = getattr(agent, "workflow_plan_draft_state", None)
    draft_prompt = ""
    if draft_state is not None:
        draft_prompt = (
            "\n\nCurrent internal ReAct planning state panel. It includes the NavigationDataProfile schema, "
            "the current data_profile_draft, filled_fields, missing_fields, next_tool_candidates, and "
            "ready_to_finish:\n"
            f"{json.dumps(draft_state.schema_snapshot(), ensure_ascii=False)}"
        )
    prompt = (
        "You are a Navigation ReAct Plan-Agent. Use the inspect_raw_date_tool, "
        "infer_navigation_sensor_bindings_tool, infer_navigation_processing_profile_tool, "
        "infer_navigation_topic_params_tool, inspect_processing_state_tool, "
        "inspect_gridmap_artifacts_tool, inspect_runtime_assets_tool, and list_navigation_tool_capabilities_tool "
        "read-only tools to build a "
        "lightweight NavigationDataProfile and stage-one navigation WorkflowPlan.\n\n"
        "Strict planning loop:\n"
        "- Privately read the current NavigationDataProfile schema, data_profile_draft, filled_fields, "
        "missing_fields, validation_errors, and next_tool_candidates; decide exactly one next SDK tool call.\n"
        "- To inspect one missing fact, emit a Progress line, then call one read-only registered SDK tool. "
        "After the tool result, emit a Progress line, then call update_workflow_plan_draft_tool with "
        "data_profile_patch containing only newly learned NavigationDataProfile facts, plus observation_id "
        "and used_tool.\n"
        "- Finish by emitting a Progress line, then calling finalize_workflow_plan_tool, but only when "
        "ready_to_finish is true and missing_fields is empty.\n"
        "Do not output textual Thought: or Action: lines. Do not write ToolName[arguments] strings.\n\n"
        "The NavigationDataProfile schema is authoritative. data_profile_patch must be JSON-compatible and "
        "must use only schema fields and valid enum values. Do not output a complete data_profile unless it is "
        "already fully supported by previous tool observations. Do not write script-level steps; "
        "final strict WorkflowPlan JSON must come from finalize_workflow_plan_tool. Stage one covers "
        "prepare.sh, run_U.sh, and run_odom.sh only; do not include run_fix.sh. Default to all raw "
        "segments when segments are not specified. Use NavigationDataProfile.processing_profile and "
        "platform_hint rather than dataset_profile; infer sensor bindings and processing_profile from "
        "tool results, and do not require fixed u_legacy_like/go2w_like classification. Call "
        "infer_navigation_topic_params_tool before finalizing extract_and_sync_navigation_data; "
        "do not invent TOPIC_WHITELIST, topic_map, query_dir, localization policy, or calibration "
        "policy. Only finalize when processing_profile has no blocking_issues. Always include "
        "confirm_navigation_calibration_params as the first step before any processing and before "
        "prepare_raw_data. scene_mode is required "
        "and must be either in or out. Gridmap preparation must happen "
        "after run_tracking and before projection. Supported execution tool names include run_tracking, "
        "prepare_gridmap_for_projection, and run_projection_and_trajectory. The only human-blocking step "
        "besides calibration confirmation is gen_box.py via run_initial_annotation_gui. "
        f"{PUBLIC_PROGRESS_PROMPT}\n\n"
        f"{_response_language_prompt(response_language)}"
        f"NavigationRequest JSON:\n{request.model_dump_json()}"
        f"{draft_prompt}"
    )
    output = await _run_agent_stream(
        agent,
        prompt,
        run_store=run_store,
        run_dir=run_dir,
        event_scope=event_scope,
        cancellation=cancellation,
    )
    if draft_state is not None and draft_state.finalized_plan is not None:
        return _validated_workflow_plan(draft_state.finalized_plan, data_profile=draft_state.data_profile)
    if draft_state is not None and not output.strip() and draft_state.processing_profile is not None:
        return _validated_workflow_plan(build_plan_from_draft(draft_state), data_profile=draft_state.data_profile)
    return _validated_workflow_plan(_parse_workflow_plan_output(output))


async def run_executor_agent(
    agent,
    plan: WorkflowPlan,
    run_store: WorkflowRunStore | None = None,
    run_dir: Path | None = None,
    *,
    event_scope: EventScope | None = None,
    cancellation: CancellationContext | None = None,
    response_language: str | None = None,
    resume_from_checkpoint: bool = False,
) -> str:
    execution_context = (
        "This is a resumed execution from an already-confirmed checkpoint. "
        "confirm_navigation_calibration_params was completed in the previous turn and is intentionally omitted "
        "from the remaining WorkflowPlan. Do not call confirm_navigation_calibration_params again, do not wait "
        "for calibration confirmation again, and execute the supplied remaining WorkflowPlan as given. "
        "The gen_box.py GUI step remains human-blocking; wait until the human finishes before continuing. "
        if resume_from_checkpoint
        else (
            "The calibration confirmation and gen_box.py GUI steps are human-blocking; "
            "confirm_navigation_calibration_params must be the first step before prepare_raw_data or any "
            "processing, and wait until the human finishes before continuing. "
        )
    )
    prompt = (
        "Execute this WorkflowPlan JSON step-by-step using the matching execution tools. Stop on any "
        f"failed tool result. {execution_context}"
        "scene_mode is required and must be either in "
        "or out. Prepare gridmap after run_tracking and before run_projection_and_trajectory. Supported "
        "tool names include run_tracking, prepare_gridmap_for_projection, and run_projection_and_trajectory. "
        f"{PUBLIC_PROGRESS_PROMPT} Return a concise final execution summary.\n\n"
        f"{_response_language_prompt(response_language)}"
        f"WorkflowPlan JSON:\n{plan.model_dump_json()}"
    )
    return await _run_agent_stream(
        agent,
        prompt,
        run_store=run_store,
        run_dir=run_dir,
        event_scope=event_scope,
        cancellation=cancellation,
    )

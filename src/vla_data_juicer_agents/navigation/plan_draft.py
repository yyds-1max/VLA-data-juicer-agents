from __future__ import annotations

import json
from typing import Any

from agentscope.tool import FunctionTool
from pydantic import BaseModel, Field, model_validator

from vla_data_juicer_agents.navigation.models import NavigationDataProfile, NavigationRequest, WorkflowPlan


_INVESTIGATION_OBSERVATION_STEPS: tuple[dict[str, str], ...] = (
    {
        "observation_id": "raw_metadata",
        "used_tool": "inspect_raw_date_tool",
        "description": "Inspect raw ROS bag/db3 metadata topics before inferring sensor roles.",
    },
    {
        "observation_id": "sensor_bindings",
        "used_tool": "infer_navigation_sensor_bindings_tool",
        "description": "Infer role-based camera, lidar, odom, and Ins bindings from raw topics.",
    },
    {
        "observation_id": "navigation_processing_profile",
        "used_tool": "infer_navigation_processing_profile_tool",
        "description": "Infer processing profile, platform hint, localization policy, and calibration policy.",
    },
    {
        "observation_id": "navigation_topic_params",
        "used_tool": "infer_navigation_topic_params_tool",
        "description": "Infer explicit topic_whitelist, topic_map, and query_dir after sensor roles are known.",
    },
    {
        "observation_id": "processing_state",
        "used_tool": "inspect_processing_state_tool",
        "description": "Inspect existing intermediate outputs before selecting execution behavior.",
    },
    {
        "observation_id": "gridmap_artifacts",
        "used_tool": "inspect_gridmap_artifacts_tool",
        "description": "Inspect grid_map artifacts after topic and processing context is known.",
    },
    {
        "observation_id": "runtime_assets",
        "used_tool": "inspect_runtime_assets_tool",
        "description": "Inspect available scripts before selecting gridmap and projection variants.",
    },
    {
        "observation_id": "tool_capabilities",
        "used_tool": "list_navigation_tool_capabilities_tool",
        "description": "Confirm selected strategy variants are exposed by the navigation capability catalog.",
    },
)


def _observation_steps_for_phase(phase: str) -> tuple[dict[str, str], ...]:
    if phase == "extract_sync":
        return _INVESTIGATION_OBSERVATION_STEPS[:4]
    return _INVESTIGATION_OBSERVATION_STEPS


class WorkflowPlanDraftState(BaseModel):
    request: NavigationRequest
    plan_phase: str = "extract_sync"
    processing_profile: str | None = None
    platform_hint: str = "unknown"
    data_profile_draft: dict[str, Any] = Field(default_factory=dict)
    data_profile: NavigationDataProfile | None = None
    finalized_plan: WorkflowPlan | None = None
    validation_errors: list[str] = Field(default_factory=list)
    completed_observations: list[dict[str, str]] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _coerce_request_fields(cls, data: Any) -> Any:
        if not isinstance(data, dict) or "request" in data or "date" not in data:
            return data
        coerced = dict(data)
        coerced["request"] = {
            "date": coerced.get("date"),
            "segments": coerced.get("segments"),
            "scene_mode": coerced.get("scene_mode"),
            "dry_run": coerced.get("dry_run", False),
        }
        return coerced

    @property
    def date(self) -> str:
        return self.request.date

    @property
    def segments(self) -> list[str] | None:
        return self.request.segments

    @property
    def scene_mode(self) -> str | None:
        return self.request.scene_mode

    def required_observation_steps(self, *, phase: str | None = None) -> tuple[dict[str, str], ...]:
        return _observation_steps_for_phase(phase or self.plan_phase)

    def set_scene_mode(self, scene_mode: str) -> None:
        if scene_mode not in {"in", "out"}:
            raise ValueError("scene_mode must be 'in' or 'out'")
        self.request = self.request.model_copy(update={"scene_mode": scene_mode})
        self._refresh_data_profile_from_draft()

    def advance_to_finish_processing(self, *, scene_mode: str | None = None) -> None:
        if scene_mode in {"in", "out"}:
            self.request = self.request.model_copy(update={"scene_mode": scene_mode})
        self.plan_phase = "finish_processing"
        self._refresh_data_profile_from_draft(phase="finish_processing")

    def _request_data_profile_seed(self) -> dict[str, Any]:
        seed: dict[str, Any] = {
            "date": self.date,
            "segments": self.segments,
        }
        if self.scene_mode is not None:
            seed["scene_mode"] = self.scene_mode
        return seed

    def current_data_profile_draft(self) -> dict[str, Any]:
        draft = self._request_data_profile_seed()
        if self.platform_hint:
            draft["platform_hint"] = self.platform_hint
        _deep_merge(draft, self.data_profile_draft)
        return draft

    def filled_fields(self) -> list[str]:
        return sorted(_filled_paths(self.current_data_profile_draft()))

    def missing_fields(self, *, phase: str | None = None) -> list[str]:
        effective_phase = phase or self.plan_phase
        draft = self.current_data_profile_draft()
        missing: list[str] = []
        if effective_phase in {"finish_processing", "full"} and draft.get("scene_mode") not in {"in", "out"}:
            missing.append("scene_mode")
        processing_profile = draft.get("processing_profile")
        if not isinstance(processing_profile, dict) or not isinstance(processing_profile.get("id"), str):
            missing.append("processing_profile")
        elif processing_profile.get("blocking_issues"):
            missing.append("processing_profile.blocking_issues")
        topic_params = draft.get("topic_params")
        if not isinstance(topic_params, dict):
            missing.append("topic_params")
        elif topic_params.get("blocking_issues"):
            missing.append("topic_params.blocking_issues")
        if draft.get("blocking_issues"):
            missing.append("blocking_issues")
        if not isinstance(draft.get("localization_policy"), dict):
            missing.append("localization_policy")
        stage_variants = draft.get("stage_variants") or {}
        stage_names = ["extract_and_sync_navigation_data"]
        if effective_phase in {"finish_processing", "full"}:
            stage_names.extend(["prepare_gridmap_for_projection", "run_projection_and_trajectory"])
        for stage_name in stage_names:
            if stage_name not in stage_variants:
                missing.append(f"stage_variants.{stage_name}")
        return missing

    def ready_to_finish(self, *, phase: str | None = None) -> bool:
        effective_phase = phase or self.plan_phase
        return (
            self.processing_profile is not None
            and not self.missing_fields(phase=effective_phase)
            and not self.validation_errors
        )

    def next_tool_candidates(self) -> list[str]:
        next_observation = self.next_required_observation()
        if next_observation is not None:
            return [next_observation["used_tool"]]
        if self.ready_to_finish():
            if self.plan_phase == "extract_sync":
                return ["finalize_extract_sync_plan_tool"]
            if self.plan_phase == "finish_processing":
                return ["finalize_finish_processing_plan_tool"]
            return ["finalize_workflow_plan_tool"]
        return self._missing_field_tool_candidates()

    def next_required_observation(self) -> dict[str, str] | None:
        completed_observation_ids = {
            observation.get("observation_id")
            for observation in self.completed_observations
            if observation.get("observation_id")
        }
        completed_tools = {
            observation.get("used_tool")
            for observation in self.completed_observations
            if observation.get("used_tool")
        }
        for step in self.required_observation_steps():
            if step["observation_id"] in completed_observation_ids:
                continue
            if step["used_tool"] in completed_tools:
                continue
            return dict(step)
        return None

    def _missing_field_tool_candidates(self) -> list[str]:
        missing = set(self.missing_fields())
        candidates: list[str] = []
        if "stage_variants.extract_and_sync_navigation_data" in missing:
            candidates.append("infer_navigation_topic_params_tool")
        if (
            "processing_profile" in missing
            or "processing_profile.blocking_issues" in missing
            or "blocking_issues" in missing
            or "localization_policy" in missing
        ):
            candidates.append("infer_navigation_processing_profile_tool")
        if "topic_params" in missing or "topic_params.blocking_issues" in missing:
            candidates.append("infer_navigation_topic_params_tool")
        if "stage_variants.prepare_gridmap_for_projection" in missing:
            candidates.append("inspect_gridmap_artifacts_tool")
        if any(
            field.startswith("stage_variants.")
            and field != "stage_variants.extract_and_sync_navigation_data"
            for field in missing
        ):
            candidates.append("inspect_runtime_assets_tool")
            candidates.append("list_navigation_tool_capabilities_tool")
        if not candidates:
            candidates.append("get_workflow_plan_draft_tool")
        return list(dict.fromkeys(candidates))

    def schema_snapshot(self) -> dict[str, Any]:
        data_profile_draft = self.current_data_profile_draft()
        return {
            "date": data_profile_draft.get("date", self.date),
            "segments": data_profile_draft.get("segments"),
            "scene_mode": data_profile_draft.get("scene_mode"),
            "plan_phase": self.plan_phase,
            "processing_profile": self.processing_profile or "<processing_profile.id>",
            "platform_hint": self.platform_hint,
            "navigation_data_profile_schema": _navigation_data_profile_schema(),
            "data_profile_draft": data_profile_draft,
            "data_profile": self.data_profile.model_dump(mode="json") if self.data_profile is not None else None,
            "finalized_plan": self.finalized_plan.model_dump(mode="json") if self.finalized_plan is not None else None,
            "steps": "<generated by phase-aware finalize_* plan tools after processing_profile is complete>",
            "filled_fields": self.filled_fields(),
            "missing_fields": self.missing_fields(),
            "required_observations": [step["observation_id"] for step in self.required_observation_steps()],
            "planning_observation_order": [dict(step) for step in self.required_observation_steps()],
            "completed_observations": list(self.completed_observations),
            "next_required_observation": self.next_required_observation(),
            "next_tool_candidates": self.next_tool_candidates(),
            "ready_to_finish": self.ready_to_finish(),
            "ready_to_finalize": self.ready_to_finish(),
            "allowed_tool_order": [
                "prepare_raw_data",
                "extract_and_sync_navigation_data",
                "assemble_finish_temp",
                "run_noobscene_preprocessing",
                "run_initial_annotation_gui",
                "run_tracking",
                "prepare_gridmap_for_projection",
                "run_projection_and_trajectory",
                "validate_navigation_outputs",
            ],
        }

    def snapshot(self) -> dict[str, Any]:
        return self.schema_snapshot()

    def update(
        self,
        data_profile: dict[str, Any] | NavigationDataProfile | str | None = None,
        data_profile_patch: dict[str, Any] | str | None = None,
        observation_id: str | None = None,
        used_tool: str | None = None,
    ) -> dict[str, Any]:
        self.validation_errors.clear()
        patch: dict[str, Any] = {}
        if data_profile is not None:
            patch = _coerce_profile_patch(data_profile, field_name="data_profile", errors=self.validation_errors)
        if data_profile_patch is not None:
            _deep_merge(
                patch,
                _coerce_profile_patch(
                    data_profile_patch,
                    field_name="data_profile_patch",
                    errors=self.validation_errors,
                ),
            )

        records_observation = observation_id is not None or used_tool is not None
        if records_observation and not patch:
            self.validation_errors.append(
                "empty data_profile_patch cannot complete an observation"
            )
            self._refresh_data_profile_from_draft()
            return self.status()
        if self.validation_errors:
            self._refresh_data_profile_from_draft()
            return self.status()
        patch_processing_profile = patch.get("processing_profile")
        if isinstance(patch_processing_profile, dict):
            profile_id = patch_processing_profile.get("id")
            if isinstance(profile_id, str):
                self.processing_profile = profile_id
            nested_platform_hint = patch_processing_profile.get("platform_hint")
            if (
                isinstance(nested_platform_hint, str)
                and nested_platform_hint != "unknown"
                and patch.get("platform_hint", "unknown") == "unknown"
            ):
                self.platform_hint = nested_platform_hint
                patch["platform_hint"] = nested_platform_hint
        elif isinstance(patch_processing_profile, str):
            self.processing_profile = patch_processing_profile
            patch["processing_profile"] = {"id": patch_processing_profile}
        patch_platform_hint = patch.get("platform_hint")
        if isinstance(patch_platform_hint, str):
            self.platform_hint = patch_platform_hint
        if patch:
            _deep_merge(self.data_profile_draft, patch)
            _normalize_gridmap_generation_source(self.data_profile_draft)
        if records_observation:
            observation: dict[str, str] = {}
            if observation_id is not None:
                observation["observation_id"] = observation_id
            if used_tool is not None:
                observation["used_tool"] = used_tool
            self.completed_observations.append(observation)
        self._refresh_data_profile_from_draft()
        return self.status()

    def _refresh_data_profile_from_draft(self, *, phase: str | None = None) -> None:
        effective_phase = phase or self.plan_phase
        if self.missing_fields(phase=effective_phase):
            self.data_profile = None
            return
        if effective_phase == "extract_sync" and self.missing_fields(phase="finish_processing"):
            self.data_profile = None
            return
        try:
            parsed_profile = NavigationDataProfile.model_validate(self.current_data_profile_draft())
        except Exception as exc:
            self.data_profile = None
            self.validation_errors.append(f"invalid data_profile: {exc}")
        else:
            self.data_profile = parsed_profile
            if parsed_profile.processing_profile is not None:
                self.processing_profile = parsed_profile.processing_profile.id
                self.platform_hint = (
                    parsed_profile.processing_profile.platform_hint
                    if parsed_profile.platform_hint == "unknown"
                    else parsed_profile.platform_hint
                )

    def status(self) -> dict[str, Any]:
        return {
            "ok": not self.validation_errors,
            "draft": self.schema_snapshot(),
            "validation_errors": list(self.validation_errors),
        }


def _finish_processing_data_profile_from_draft(state: WorkflowPlanDraftState) -> NavigationDataProfile:
    state._refresh_data_profile_from_draft(phase="finish_processing")
    if state.data_profile is None:
        missing = ", ".join(state.missing_fields(phase="finish_processing")) or "invalid profile fields"
        raise ValueError(f"NavigationDataProfile draft is incomplete; missing: {missing}")
    if state.data_profile.processing_profile is None:
        raise ValueError("processing_profile is required before finalizing WorkflowPlan")
    if state.data_profile.topic_params is None:
        raise ValueError("topic_params is required before finalizing WorkflowPlan")
    if state.data_profile.localization_policy is None:
        raise ValueError("localization_policy is required before finalizing WorkflowPlan")
    if state.data_profile.processing_profile.blocking_issues:
        issues = ", ".join(issue.type for issue in state.data_profile.processing_profile.blocking_issues)
        raise ValueError(f"cannot finalize WorkflowPlan with blocking processing profile: {issues}")
    if state.data_profile.topic_params.blocking_issues:
        issues = ", ".join(issue.type for issue in state.data_profile.topic_params.blocking_issues)
        raise ValueError(f"cannot finalize WorkflowPlan with blocking topic params: {issues}")
    if state.data_profile.scene_mode not in {"in", "out"}:
        raise ValueError("scene_mode is required before finalizing WorkflowPlan; expected 'in' or 'out'.")
    if state.data_profile is not None and state.data_profile.blocking_issues:
        issues = ", ".join(issue.type for issue in state.data_profile.blocking_issues)
        raise ValueError(f"cannot finalize WorkflowPlan with blocking issues: {issues}")
    return state.data_profile


def build_plan_from_draft(state: WorkflowPlanDraftState) -> WorkflowPlan:
    from vla_data_juicer_agents.navigation.workflow import build_deterministic_plan_template

    data_profile = _finish_processing_data_profile_from_draft(state)
    plan = build_deterministic_plan_template(
        data_profile.date,
        data_profile.processing_profile.id,
        data_profile.segments,
        scene_mode=data_profile.scene_mode,
        data_profile=data_profile,
    )
    return plan.model_copy(
        update={
            "processing_profile": data_profile.processing_profile.id,
            "platform_hint": data_profile.platform_hint
            or data_profile.processing_profile.platform_hint,
        }
    )


def build_finish_processing_plan_from_draft(state: WorkflowPlanDraftState) -> WorkflowPlan:
    from vla_data_juicer_agents.navigation.workflow import build_finish_processing_plan_template

    data_profile = _finish_processing_data_profile_from_draft(state)
    plan = build_finish_processing_plan_template(
        data_profile.date,
        data_profile.processing_profile.id,
        data_profile.segments,
        scene_mode=data_profile.scene_mode,
        data_profile=data_profile,
    )
    return plan.model_copy(
        update={
            "processing_profile": data_profile.processing_profile.id,
            "platform_hint": data_profile.platform_hint
            or data_profile.processing_profile.platform_hint,
        }
    )


def build_extract_sync_plan_from_draft(state: WorkflowPlanDraftState) -> WorkflowPlan:
    from vla_data_juicer_agents.navigation.workflow import build_extract_sync_plan_template

    missing = state.missing_fields(phase="extract_sync")
    if missing:
        raise ValueError(f"extract-sync draft is incomplete; missing: {', '.join(missing)}")
    return build_extract_sync_plan_template(
        state.date,
        state.processing_profile,
        state.segments,
        data_profile_draft=state.current_data_profile_draft(),
    )


def build_plan_draft_tools(state: WorkflowPlanDraftState) -> list[FunctionTool]:
    def update_workflow_plan_draft_tool(
        data_profile: dict[str, Any] | str | None = None,
        data_profile_patch: dict[str, Any] | str | None = None,
        observation_id: str | None = None,
        used_tool: str | None = None,
    ) -> dict[str, Any]:
        """Merge one observed NavigationDataProfile patch into the internal draft."""
        return state.update(
            data_profile=data_profile,
            data_profile_patch=data_profile_patch,
            observation_id=observation_id,
            used_tool=used_tool,
        )

    def get_workflow_plan_draft_tool() -> dict[str, Any]:
        """Return the current WorkflowPlan draft schema, filled fields, missing fields, and validation errors."""
        return state.status()

    def finalize_workflow_plan_tool() -> dict[str, Any]:
        """Finalize and return strict WorkflowPlan JSON after the NavigationDataProfile draft is complete."""
        plan = build_plan_from_draft(state)
        state.finalized_plan = plan
        return {
            "ok": True,
            "workflow_plan_json": json.loads(plan.model_dump_json()),
            "draft": state.schema_snapshot(),
        }

    def finalize_extract_sync_plan_tool() -> dict[str, Any]:
        """Finalize and return an extract-sync WorkflowPlan before scene_mode is known."""
        plan = build_extract_sync_plan_from_draft(state)
        state.finalized_plan = plan
        return {
            "ok": True,
            "workflow_plan_json": json.loads(plan.model_dump_json()),
            "draft": state.schema_snapshot(),
        }

    def finalize_finish_processing_plan_tool() -> dict[str, Any]:
        """Finalize and return a finish-processing WorkflowPlan after scene_mode is known."""
        plan = build_finish_processing_plan_from_draft(state)
        state.finalized_plan = plan
        return {
            "ok": True,
            "workflow_plan_json": json.loads(plan.model_dump_json()),
            "draft": state.schema_snapshot(),
        }

    return [
        FunctionTool(update_workflow_plan_draft_tool, name="update_workflow_plan_draft_tool", is_read_only=False),
        FunctionTool(get_workflow_plan_draft_tool, name="get_workflow_plan_draft_tool", is_read_only=True),
        FunctionTool(finalize_extract_sync_plan_tool, name="finalize_extract_sync_plan_tool", is_read_only=False),
        FunctionTool(
            finalize_finish_processing_plan_tool,
            name="finalize_finish_processing_plan_tool",
            is_read_only=False,
        ),
        FunctionTool(finalize_workflow_plan_tool, name="finalize_workflow_plan_tool", is_read_only=False),
    ]


def _deep_merge(target: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_merge(target[key], value)
        else:
            target[key] = value
    return target


def _normalize_gridmap_generation_source(draft: dict[str, Any]) -> None:
    stage_variants = draft.get("stage_variants")
    if not isinstance(stage_variants, dict):
        return
    gridmap_variant = stage_variants.get("prepare_gridmap_for_projection")
    if not isinstance(gridmap_variant, dict):
        return
    if gridmap_variant.get("variant") != "generate_from_pcd":
        return
    if draft.get("gridmap_source") != "unknown":
        return
    if draft.get("pcd_gridmap_tool_available") is not True:
        return
    draft["gridmap_source"] = "generated_from_pcd"


def _coerce_profile_patch(
    value: dict[str, Any] | NavigationDataProfile | str,
    *,
    field_name: str,
    errors: list[str],
) -> dict[str, Any]:
    if isinstance(value, NavigationDataProfile):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return value
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        errors.append(f"invalid {field_name}: expected object or JSON object string: {exc}")
        return {}
    if not isinstance(payload, dict):
        errors.append(f"invalid {field_name}: expected JSON object string")
        return {}
    return payload


def _filled_paths(value: Any, prefix: str = "") -> set[str]:
    paths: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            paths.update(_filled_paths(child, child_prefix))
    elif value is not None:
        paths.add(prefix)
    return paths


def _navigation_data_profile_schema() -> dict[str, Any]:
    schema = NavigationDataProfile.model_json_schema()
    properties = schema.setdefault("properties", {})
    properties.setdefault("scene_mode", {})["enum"] = ["in", "out"]
    properties.setdefault("gridmap_source", {})["enum"] = [
        "existing_gridmap",
        "generated_from_pcd",
        "projection_ready",
        "unknown",
    ]
    return schema

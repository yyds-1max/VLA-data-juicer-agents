import json

import pytest
from pydantic import TypeAdapter, ValidationError

from vla_data_juicer_agents.navigation.plan_models import (
    ExtractSyncPlanInput,
    ExtractSyncStep,
    ExtractSyncStepInput,
    FinishProcessingPlanInput,
    NavigationPlanRecord,
    PlanSubmissionAttempt,
)
from vla_data_juicer_agents.navigation.task_state import NavigationTask, NavigationTaskStatus


def valid_extract_plan_payload() -> dict:
    return {
        "decisions": {
            "sensor_bindings": {
                "bindings": {
                    "fisheye_front": "/camera/front/image",
                    "lidar": "/lidar/points",
                    "odom": "/localization/odom",
                },
                "reason": "Observed matching message types and rates.",
                "evidence_refs": ["evidence:sensors"],
            },
            "topic_selection": {
                "topic_whitelist": ["/camera/front/image", "/lidar/points", "/localization/odom"],
                "topic_map": {
                    "/camera/front/image": "fisheye_front",
                    "/lidar/points": "lidar",
                    "/localization/odom": "odom",
                },
                "query_dir": "/data/query",
                "reason": "All selected topics were observed.",
                "evidence_refs": ["evidence:topics"],
            },
            "time_sync": {
                "reference_sensor": "lidar",
                "method": "nearest_timestamp",
                "tolerance_ms": 50,
                "reason": "Lidar timestamps cover the selected streams.",
                "evidence_refs": ["evidence:timing"],
            },
        },
        "steps": [
            {
                "step_id": "prepare_raw",
                "action": "prepare_raw_data",
                "variant": "default",
                "arguments": {},
                "depends_on": [],
                "failure_policy": "stop",
                "decision_refs": [],
            },
            {
                "step_id": "extract_sync",
                "action": "extract_and_sync_navigation_data",
                "variant": "explicit_topic_params",
                "depends_on": ["prepare_raw"],
                "failure_policy": "stop",
                "decision_refs": ["sensor_bindings", "topic_selection", "time_sync"],
                "arguments": {"processes_num": 8},
            },
        ],
    }


def valid_finish_plan_payload() -> dict:
    return {
        "decisions": {
            "localization": {
                "source": "odom",
                "conversion": "odom_to_ins",
                "reason": "Odom is the available localization source.",
                "evidence_refs": ["evidence:localization"],
            },
            "gridmap": {
                "source": "existing_gridmap",
                "reason": "A complete gridmap already exists.",
                "evidence_refs": ["evidence:gridmap"],
            },
            "calibration": {
                "mode": "hardcoded_with_user_confirmation",
                "selected_sensor_source": "fisheye_front",
                "requires_user_confirmation": True,
                "reason": "Runtime calibration inventory requires confirmation.",
                "evidence_refs": ["evidence:calibration"],
            },
        },
        "steps": [
            {
                "step_id": "confirm_calibration",
                "action": "confirm_navigation_calibration_params",
                "variant": "default",
                "arguments": {},
                "depends_on": [],
                "failure_policy": "stop",
                "decision_refs": ["calibration"],
            },
            {
                "step_id": "prepare_gridmap",
                "action": "prepare_gridmap_for_projection",
                "variant": "copy_existing_gridmap",
                "arguments": {},
                "depends_on": ["confirm_calibration"],
                "failure_policy": "stop",
                "decision_refs": ["gridmap"],
            },
            {
                "step_id": "projection",
                "action": "run_projection_and_trajectory",
                "variant": "cjl_with_gridmap",
                "arguments": {},
                "depends_on": ["prepare_gridmap"],
                "failure_policy": "stop",
                "decision_refs": ["localization", "gridmap"],
            },
        ],
    }


def test_extract_plan_has_one_source_for_topic_and_sync_decisions():
    schema = ExtractSyncPlanInput.model_json_schema()
    text = json.dumps(schema)

    assert "processing_profile" not in text
    assert "stage_variants" not in text
    assert "blocking_issues" not in text


def test_nested_plan_models_forbid_extra_fields():
    payload = valid_extract_plan_payload()
    payload["decisions"]["time_sync"]["invented"] = True

    with pytest.raises(ValidationError):
        ExtractSyncPlanInput.model_validate(payload)


@pytest.mark.parametrize(
    "field",
    ["action", "variant", "arguments", "depends_on", "failure_policy", "decision_refs"],
)
def test_extract_step_rejects_omitted_model_owned_field(field):
    step = valid_extract_plan_payload()["steps"][1]
    step.pop(field)

    with pytest.raises(ValidationError):
        ExtractSyncStep.model_validate(step)


def test_extract_step_rejects_omitted_processes_num():
    payload = valid_extract_plan_payload()
    payload["steps"][1]["arguments"].pop("processes_num")

    with pytest.raises(ValidationError):
        ExtractSyncPlanInput.model_validate(payload)


def test_extract_steps_use_action_discriminator():
    plan = ExtractSyncPlanInput.model_validate(valid_extract_plan_payload())

    assert isinstance(plan.steps[1], ExtractSyncStep)
    assert plan.steps[1].arguments.processes_num == 8

    with pytest.raises(ValidationError):
        TypeAdapter(ExtractSyncStepInput).validate_python(
            {
                "step_id": "tracking",
                "action": "run_tracking",
                "variant": "default",
            }
        )


def test_finish_plan_contains_only_normalized_decisions_and_steps():
    plan = FinishProcessingPlanInput.model_validate(valid_finish_plan_payload())
    payload = plan.model_dump(mode="json")

    assert set(payload) == {"decisions", "steps"}
    assert payload["decisions"]["localization"]["source"] == "odom"


def test_plan_record_and_submission_attempt_round_trip_strict_contracts():
    record = NavigationPlanRecord(
        plan_id="plan-1",
        task_id="nav-1",
        phase="extract_sync",
        plan_revision=1,
        contract_version="navigation-plan-v2",
        observation_revision=2,
        status="active",
        plan=valid_extract_plan_payload(),
        created_at="2026-07-10T12:00:00+00:00",
    )
    attempt = PlanSubmissionAttempt(
        attempt_id="attempt-1",
        task_id="nav-1",
        phase="extract_sync",
        planning_context_revision="context-2",
        candidate=valid_extract_plan_payload(),
        validation={"ok": False, "errors": [{"path": "steps.0", "code": "invalid", "message": "bad"}]},
        created_at="2026-07-10T12:00:00+00:00",
    )

    assert isinstance(record.plan, ExtractSyncPlanInput)
    assert attempt.validation.errors[0].code == "invalid"

    with pytest.raises(ValidationError):
        PlanSubmissionAttempt.model_validate({**attempt.model_dump(), "filesystem_path": "/tmp/attempt.json"})


def test_task_domain_carries_replanning_inputs_before_persistence_migration():
    task = NavigationTask(task_id="nav-1", date="20260710", dry_run=True, guidance_revision=3)

    assert NavigationTaskStatus.NEEDS_REPLAN == "needs_replan"
    assert task.dry_run is True
    assert task.guidance_revision == 3

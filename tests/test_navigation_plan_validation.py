from __future__ import annotations

import pytest

from vla_data_juicer_agents.navigation.catalog import (
    ToolCapability,
    ToolVariantCapability,
    list_navigation_tool_capabilities,
)
from vla_data_juicer_agents.navigation.observation_models import (
    ArtifactStateObservation,
    CalibrationInventoryObservation,
    EvidenceDescriptor,
    GridmapArtifactsObservation,
    LocalizationSourcesObservation,
    NavigationObservationRevision,
    RawMetadataObservation,
    RuntimeAssetsObservation,
    SensorCandidatesObservation,
    SensorRoleCandidate,
    TopicCandidatesObservation,
    TopicMeasurement,
)
from vla_data_juicer_agents.navigation.plan_models import (
    ExtractSyncPlanInput,
    FinishProcessingPlanInput,
    PlanValidationIssue,
)
from vla_data_juicer_agents.navigation.plan_validation import validate_navigation_plan
from vla_data_juicer_agents.navigation.task_state import (
    NavigationArtifactSnapshot,
    NavigationTask,
    NavigationTaskPhase,
)


def extract_task() -> NavigationTask:
    return NavigationTask(
        task_id="nav-plan-1",
        date="20260710",
        segments=["20260710_120000"],
        phase=NavigationTaskPhase.EXTRACT_SYNC,
    )


def finish_task() -> NavigationTask:
    return NavigationTask(
        task_id="nav-plan-1",
        date="20260710",
        segments=["20260710_120000"],
        scene_mode="out",
        phase=NavigationTaskPhase.FINISH_PROCESSING,
    )


def descriptor(
    ref: str,
    revision: int,
    *,
    task_id: str = "nav-plan-1",
    kind: str = "measured_fact",
    created_at: str = "2026-07-10T00:00:00+00:00",
) -> EvidenceDescriptor:
    return EvidenceDescriptor(
        ref=ref,
        task_id=task_id,
        observation_revision=revision,
        kind=kind,
        summary=f"Evidence for {ref}",
        byte_size=10,
        source_tool="inspect_navigation_test_tool",
        created_at=created_at,
    )


def extract_observation() -> NavigationObservationRevision:
    topics = ["/camera/front/image", "/lidar/points", "/localization/odom"]
    return NavigationObservationRevision(
        task_id="nav-plan-1",
        revision=4,
        phase=NavigationTaskPhase.EXTRACT_SYNC,
        completed_kinds=[
            "artifact_state",
            "raw_metadata",
            "sensor_candidates",
            "topic_candidates",
        ],
        payloads=[
            ArtifactStateObservation(
                snapshot=NavigationArtifactSnapshot(
                    date="20260710",
                    segments=["20260710_120000"],
                    raw_input_exists=True,
                )
            ),
            RawMetadataObservation(
                segments=["20260710_120000"],
                topics=[TopicMeasurement(topic=topic, message_count=100) for topic in topics],
            ),
            SensorCandidatesObservation(
                candidates=[
                    SensorRoleCandidate(
                        role="fisheye_front",
                        topic="/camera/front/image",
                        confidence=0.99,
                    ),
                    SensorRoleCandidate(
                        role="lidar", topic="/lidar/points", confidence=0.99
                    ),
                    SensorRoleCandidate(
                        role="odom", topic="/localization/odom", confidence=0.99
                    ),
                ]
            ),
            TopicCandidatesObservation(
                available_topics=topics,
                suggested_role_names={
                    "fisheye_front": ["/camera/front/image"],
                    "lidar": ["/lidar/points"],
                    "odom": ["/localization/odom"],
                },
            ),
        ],
    )


def finish_observation(
    *,
    pcd_tool_available: bool = True,
    conversion_available: bool = True,
) -> NavigationObservationRevision:
    return NavigationObservationRevision(
        task_id="nav-plan-1",
        revision=5,
        phase=NavigationTaskPhase.FINISH_PROCESSING,
        completed_kinds=[
            "artifact_state",
            "gridmap_artifacts",
            "runtime_assets",
            "calibration_inventory",
            "localization_sources",
        ],
        payloads=[
            ArtifactStateObservation(
                snapshot=NavigationArtifactSnapshot(
                    date="20260710",
                    segments=["20260710_120000"],
                    sync_data_exists=True,
                )
            ),
            GridmapArtifactsObservation(
                existing_gridmap_paths=["/data/grid_map.pcd"],
                pcd_sources=["/data/source.pcd"],
                projection_ready=False,
            ),
            RuntimeAssetsObservation(
                pcd_gridmap_tool_available=pcd_tool_available,
                manual_annotation_gui_available=True,
                projection_variants={
                    "cjl_with_gridmap": True,
                    "cjl_0525_with_gridmap": False,
                },
            ),
            CalibrationInventoryObservation(sensor_sources=["fisheye_front"]),
            LocalizationSourcesObservation(
                available_sources=["odom"],
                conversion_available=conversion_available,
            ),
        ],
    )


def extract_evidence() -> list[EvidenceDescriptor]:
    return [
        descriptor("evidence:sensors", 2, kind="sensor_candidates"),
        descriptor("evidence:topics", 3, kind="topic_candidates"),
        descriptor("evidence:timing", 4, kind="raw_metadata"),
    ]


def finish_evidence() -> list[EvidenceDescriptor]:
    return [
        descriptor("evidence:localization", 3, kind="localization_sources"),
        descriptor("evidence:gridmap", 4, kind="gridmap_artifacts"),
        descriptor("evidence:calibration", 5, kind="calibration_inventory"),
        descriptor("evidence:runtime", 5, kind="runtime_assets"),
    ]


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
                "topic_whitelist": [
                    "/camera/front/image",
                    "/lidar/points",
                    "/localization/odom",
                ],
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
                "arguments": {"processes_num": 8},
                "depends_on": ["prepare_raw"],
                "failure_policy": "stop",
                "decision_refs": ["sensor_bindings", "topic_selection", "time_sync"],
            },
        ],
    }


def valid_finish_plan_payload() -> dict:
    return {
        "decisions": {
            "localization": {
                "source": "odom",
                "conversion": "odom_to_ins",
                "reason": "Odom and its converter were observed.",
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
                "reason": "The selected calibration inventory needs confirmation.",
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
                "step_id": "tracking",
                "action": "run_tracking",
                "variant": "default",
                "arguments": {},
                "depends_on": ["confirm_calibration"],
                "failure_policy": "stop",
                "decision_refs": ["localization"],
            },
            {
                "step_id": "prepare_gridmap",
                "action": "prepare_gridmap_for_projection",
                "variant": "copy_existing_gridmap",
                "arguments": {},
                "depends_on": ["tracking"],
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
            {
                "step_id": "validate_outputs",
                "action": "validate_navigation_outputs",
                "variant": "expect_gridmap",
                "arguments": {},
                "depends_on": ["projection"],
                "failure_policy": "stop",
                "decision_refs": ["gridmap"],
            },
        ],
    }


def validate_extract(
    payload: dict | None = None,
    *,
    observation: NavigationObservationRevision | None = None,
    evidence: list[EvidenceDescriptor] | None = None,
):
    plan = ExtractSyncPlanInput.model_validate(payload or valid_extract_plan_payload())
    return validate_navigation_plan(
        task=extract_task(),
        observation=observation or extract_observation(),
        plan=plan,
        evidence=extract_evidence() if evidence is None else evidence,
        capabilities=list_navigation_tool_capabilities(),
    )


def validate_finish(
    payload: dict | None = None,
    *,
    observation: NavigationObservationRevision | None = None,
    evidence: list[EvidenceDescriptor] | None = None,
):
    plan = FinishProcessingPlanInput.model_validate(payload or valid_finish_plan_payload())
    return validate_navigation_plan(
        task=finish_task(),
        observation=observation or finish_observation(),
        plan=plan,
        evidence=finish_evidence() if evidence is None else evidence,
        capabilities=list_navigation_tool_capabilities(),
    )


def issue_codes(report) -> set[str]:
    return {issue.code for issue in report.errors}


def test_valid_complete_plans_accept_evidence_from_current_or_earlier_revisions():
    assert validate_extract().model_dump(mode="json") == {
        "ok": True,
        "errors": [],
        "warnings": [],
    }
    assert validate_finish().ok is True


def test_time_sync_reference_must_name_an_observed_bound_sensor_role():
    payload = valid_extract_plan_payload()
    payload["decisions"]["time_sync"]["reference_sensor"] = "gps"

    report = validate_extract(payload)

    assert report.errors[0] == PlanValidationIssue(
        path="plan.decisions.time_sync.reference_sensor",
        code="unknown_sensor_role",
        message="Referenced sensor role does not exist",
        allowed_values=["fisheye_front", "lidar", "odom"],
    )


def test_unknown_evidence_ref_is_rejected_at_its_decision_path():
    payload = valid_extract_plan_payload()
    payload["decisions"]["time_sync"]["evidence_refs"] = ["evidence:missing"]

    report = validate_extract(payload)

    assert report.errors[0].path == "plan.decisions.time_sync.evidence_refs.0"
    assert report.errors[0].code == "unknown_evidence_ref"


@pytest.mark.parametrize(
    ("replacement", "code"),
    [
        (descriptor("evidence:timing", 4, task_id="nav-other"), "evidence_task_mismatch"),
        (descriptor("evidence:timing", 5), "evidence_revision_mismatch"),
    ],
)
def test_evidence_ref_must_be_owned_by_task_and_not_from_a_future_revision(
    replacement: EvidenceDescriptor,
    code: str,
):
    evidence = [*extract_evidence()[:-1], replacement]

    report = validate_extract(evidence=evidence)

    assert report.errors[0].path == "plan.decisions.time_sync.evidence_refs.0"
    assert report.errors[0].code == code


def test_selected_topics_must_exist_in_measured_observations():
    payload = valid_extract_plan_payload()
    payload["decisions"]["topic_selection"]["topic_whitelist"][1] = "/invented"

    report = validate_extract(payload)

    assert PlanValidationIssue(
        path="plan.decisions.topic_selection.topic_whitelist.1",
        code="unobserved_topic",
        message="Selected topic was not observed",
        allowed_values=[
            "/camera/front/image",
            "/lidar/points",
            "/localization/odom",
        ],
    ) in report.errors


@pytest.mark.parametrize(
    ("source", "conversion"),
    [("odom", "none"), ("ins", "odom_to_ins")],
)
def test_localization_source_and_conversion_pair_must_be_valid(source: str, conversion: str):
    payload = valid_finish_plan_payload()
    payload["decisions"]["localization"].update(
        {"source": source, "conversion": conversion}
    )
    observation = finish_observation().model_copy(
        update={
            "payloads": [
                payload_item.model_copy(update={"available_sources": [source]})
                if isinstance(payload_item, LocalizationSourcesObservation)
                else payload_item
                for payload_item in finish_observation().payloads
            ]
        }
    )

    report = validate_finish(payload, observation=observation)

    assert "invalid_localization_conversion" in issue_codes(report)


def test_odom_conversion_requires_observed_converter_capability():
    report = validate_finish(observation=finish_observation(conversion_available=False))

    assert "localization_conversion_unavailable" in issue_codes(report)


@pytest.mark.parametrize(
    ("source", "variant", "observation", "code"),
    [
        (
            "projection_ready",
            "skip_if_projection_ready",
            finish_observation(),
            "unobserved_gridmap_source",
        ),
        (
            "generated_from_pcd",
            "generate_from_pcd",
            finish_observation(pcd_tool_available=False),
            "gridmap_capability_unavailable",
        ),
        (
            "existing_gridmap",
            "generate_from_pcd",
            finish_observation(),
            "variant_decision_mismatch",
        ),
    ],
)
def test_gridmap_source_must_match_observations_capability_and_selected_variant(
    source: str,
    variant: str,
    observation: NavigationObservationRevision,
    code: str,
):
    payload = valid_finish_plan_payload()
    payload["decisions"]["gridmap"]["source"] = source
    payload["steps"][2]["variant"] = variant

    report = validate_finish(payload, observation=observation)

    assert code in issue_codes(report)


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("action", "invented_action", "unknown_action"),
        ("variant", "invented_variant", "unknown_variant"),
        ("arguments", {"processes_num": 0}, "invalid_arguments"),
    ],
)
def test_action_variant_and_argument_contracts_are_validated_even_for_constructed_models(
    field: str,
    value: object,
    code: str,
):
    plan = ExtractSyncPlanInput.model_validate(valid_extract_plan_payload())
    plan.steps[1] = plan.steps[1].model_copy(update={field: value})

    report = validate_navigation_plan(
        task=extract_task(),
        observation=extract_observation(),
        plan=plan,
        evidence=extract_evidence(),
        capabilities=list_navigation_tool_capabilities(),
    )

    assert code in issue_codes(report)


def test_duplicate_step_ids_are_rejected_without_collapsing_the_graph():
    payload = valid_extract_plan_payload()
    payload["steps"][1]["step_id"] = "prepare_raw"

    report = validate_extract(payload)

    assert report.errors[0].path == "plan.steps.1.step_id"
    assert report.errors[0].code == "duplicate_step_id"


def test_unknown_dependencies_are_rejected_at_dependency_index():
    payload = valid_extract_plan_payload()
    payload["steps"][1]["depends_on"] = ["missing"]

    report = validate_extract(payload)

    assert report.errors[0].path == "plan.steps.1.depends_on.0"
    assert report.errors[0].code == "unknown_dependency"


def test_dependency_cycles_are_rejected():
    payload = valid_extract_plan_payload()
    payload["steps"][0]["depends_on"] = ["extract_sync"]

    report = validate_extract(payload)

    assert "dependency_cycle" in issue_codes(report)


def test_required_calibration_confirmation_step_cannot_be_omitted():
    payload = valid_finish_plan_payload()
    payload["steps"] = payload["steps"][1:]
    payload["steps"][0]["depends_on"] = []

    report = validate_finish(payload)

    assert "missing_calibration_confirmation" in issue_codes(report)


@pytest.mark.parametrize(
    ("first_action", "second_action", "code"),
    [
        ("prepare_gridmap_for_projection", "run_tracking", "gridmap_before_tracking"),
        ("run_projection_and_trajectory", "prepare_gridmap_for_projection", "projection_before_gridmap"),
    ],
)
def test_finish_business_stages_have_stable_order(
    first_action: str,
    second_action: str,
    code: str,
):
    payload = valid_finish_plan_payload()
    first = next(i for i, step in enumerate(payload["steps"]) if step["action"] == first_action)
    second = next(i for i, step in enumerate(payload["steps"]) if step["action"] == second_action)
    payload["steps"][first], payload["steps"][second] = (
        payload["steps"][second],
        payload["steps"][first],
    )

    report = validate_finish(payload)

    assert code in issue_codes(report)


def test_output_validation_must_be_the_last_step():
    payload = valid_finish_plan_payload()
    payload["steps"][-1], payload["steps"][-2] = payload["steps"][-2], payload["steps"][-1]

    report = validate_finish(payload)

    assert "validation_not_last" in issue_codes(report)


def test_observation_must_match_bound_task_phase_and_complete_required_kinds():
    observation = extract_observation().model_copy(
        update={
            "task_id": "nav-other",
            "phase": NavigationTaskPhase.FINISH_PROCESSING,
            "completed_kinds": ["raw_metadata"],
        }
    )

    report = validate_extract(observation=observation)

    assert issue_codes(report) == {
        "observation_task_mismatch",
        "observation_phase_mismatch",
        "missing_required_observation",
    }


def test_plan_type_must_match_the_bound_task_phase():
    task = extract_task().model_copy(
        update={"phase": NavigationTaskPhase.FINISH_PROCESSING}
    )
    plan = ExtractSyncPlanInput.model_validate(valid_extract_plan_payload())

    report = validate_navigation_plan(
        task=task,
        observation=extract_observation(),
        plan=plan,
        evidence=extract_evidence(),
        capabilities=list_navigation_tool_capabilities(),
    )

    assert report.errors == [
        PlanValidationIssue(
            path="task.phase",
            code="task_phase_mismatch",
            message="Plan type does not match the active task phase",
            allowed_values=["finish_processing"],
        )
    ]


def test_errors_are_deduplicated_sorted_and_capped_at_eight_public_issues():
    payload = valid_extract_plan_payload()
    payload["decisions"]["topic_selection"]["topic_whitelist"] = [
        f"/invented/{index}" for index in range(12)
    ]
    payload["decisions"]["topic_selection"]["topic_map"] = {
        "/invented/shared": "lidar"
    }

    report = validate_extract(payload)

    keys = [(issue.path, issue.code) for issue in report.errors]
    assert len(keys) == 8
    assert keys == sorted(set(keys))[:8]


def test_completed_markers_without_required_typed_payloads_are_incomplete():
    observation = finish_observation().model_copy(
        update={
            "payloads": [
                payload
                for payload in finish_observation().payloads
                if not isinstance(
                    payload,
                    (
                        CalibrationInventoryObservation,
                        LocalizationSourcesObservation,
                        RuntimeAssetsObservation,
                    ),
                )
            ]
        }
    )

    report = validate_finish(observation=observation)

    assert [
        (issue.path, issue.code)
        for issue in report.errors
        if issue.code == "missing_required_observation_payload"
    ] == [
        (
            "observation.payloads.calibration_inventory",
            "missing_required_observation_payload",
        ),
        (
            "observation.payloads.localization_sources",
            "missing_required_observation_payload",
        ),
        (
            "observation.payloads.runtime_assets",
            "missing_required_observation_payload",
        ),
    ]


def test_manual_annotation_action_requires_observed_gui_runtime_asset():
    payload = valid_finish_plan_payload()
    payload["steps"].insert(
        1,
        {
            "step_id": "initial_annotation",
            "action": "run_initial_annotation_gui",
            "variant": "human_gui",
            "arguments": {},
            "depends_on": ["confirm_calibration"],
            "failure_policy": "stop",
            "decision_refs": ["calibration"],
        },
    )
    observation = finish_observation().model_copy(
        update={
            "payloads": [
                item.model_copy(update={"manual_annotation_gui_available": False})
                if isinstance(item, RuntimeAssetsObservation)
                else item
                for item in finish_observation().payloads
            ]
        }
    )

    report = validate_finish(payload, observation=observation)

    assert PlanValidationIssue(
        path="plan.steps.1.action",
        code="runtime_action_unavailable",
        message="Manual annotation GUI is unavailable in observed runtime assets",
        allowed_values=["evidence_ref:evidence:runtime"],
    ) in report.errors


def test_manual_annotation_action_is_valid_when_gui_runtime_asset_is_available():
    payload = valid_finish_plan_payload()
    payload["steps"].insert(
        1,
        {
            "step_id": "initial_annotation",
            "action": "run_initial_annotation_gui",
            "variant": "human_gui",
            "arguments": {},
            "depends_on": ["confirm_calibration"],
            "failure_policy": "stop",
            "decision_refs": ["calibration"],
        },
    )

    assert validate_finish(payload).ok is True


def test_large_observed_inventory_uses_concrete_evidence_pointer():
    available_topics = [f"/topic/{index:03d}" for index in reversed(range(40))]
    observation = extract_observation().model_copy(
        update={
            "payloads": [
                item.model_copy(
                    update={
                        "available_topics": [
                            *item.available_topics,
                            *available_topics,
                            *available_topics,
                        ]
                    }
                )
                if isinstance(item, TopicCandidatesObservation)
                else item
                for item in extract_observation().payloads
            ]
        }
    )
    payload = valid_extract_plan_payload()
    payload["decisions"]["topic_selection"]["topic_whitelist"] = ["/not-observed"]

    report = validate_extract(payload, observation=observation)

    issue = next(issue for issue in report.errors if issue.code == "unobserved_topic")
    assert issue.allowed_values == ["evidence_ref:evidence:topics"]
    assert "see_observation_evidence" not in issue.allowed_values


def test_large_calibration_inventory_is_sorted_deduplicated_and_uses_evidence_pointer():
    sources = [f"sensor_{index:03d}" for index in reversed(range(40))]
    observation = finish_observation().model_copy(
        update={
            "payloads": [
                item.model_copy(update={"sensor_sources": [*sources, *sources]})
                if isinstance(item, CalibrationInventoryObservation)
                else item
                for item in finish_observation().payloads
            ]
        }
    )
    payload = valid_finish_plan_payload()
    payload["decisions"]["calibration"]["selected_sensor_source"] = "missing"

    report = validate_finish(payload, observation=observation)

    issue = next(issue for issue in report.errors if issue.code == "unobserved_calibration_source")
    assert issue.allowed_values == ["evidence_ref:evidence:calibration"]


def test_large_action_and_variant_sets_use_action_contract_pointers():
    capabilities = list_navigation_tool_capabilities()
    capabilities.extend(
        ToolCapability(
            tool_name=f"extra_action_{index:03d}",
            stage_kind="test",
            effects="execute",
            variants=[ToolVariantCapability(id="default")],
            executor_agent_allowed=True,
            phase="extract_sync",
            argument_model="EmptyArguments",
        )
        for index in reversed(range(40))
    )
    plan = ExtractSyncPlanInput.model_validate(valid_extract_plan_payload())
    plan.steps[1] = plan.steps[1].model_copy(update={"action": "invented_action"})

    action_report = validate_navigation_plan(
        task=extract_task(),
        observation=extract_observation(),
        plan=plan,
        evidence=extract_evidence(),
        capabilities=capabilities,
    )
    action_issue = next(issue for issue in action_report.errors if issue.code == "unknown_action")
    assert action_issue.allowed_values == sorted(set(action_issue.allowed_values))
    assert len(action_issue.allowed_values) <= 20
    assert all(value.startswith("action_contract:") for value in action_issue.allowed_values)

    capabilities = [
        capability.model_copy(
            update={
                "variants": [
                    ToolVariantCapability(id=f"variant_{index:03d}")
                    for index in reversed(range(40))
                ]
            }
        )
        if capability.tool_name == "extract_and_sync_navigation_data"
        else capability
        for capability in list_navigation_tool_capabilities()
    ]
    plan = ExtractSyncPlanInput.model_validate(valid_extract_plan_payload())
    variant_report = validate_navigation_plan(
        task=extract_task(),
        observation=extract_observation(),
        plan=plan,
        evidence=extract_evidence(),
        capabilities=capabilities,
    )
    variant_issue = next(issue for issue in variant_report.errors if issue.code == "unknown_variant")
    assert variant_issue.allowed_values == [
        "action_contract:extract_and_sync_navigation_data"
    ]


def test_invalid_arguments_point_to_the_selected_action_contract():
    plan = ExtractSyncPlanInput.model_validate(valid_extract_plan_payload())
    plan.steps[1] = plan.steps[1].model_copy(
        update={"arguments": {"processes_num": 0}}
    )

    report = validate_navigation_plan(
        task=extract_task(),
        observation=extract_observation(),
        plan=plan,
        evidence=extract_evidence(),
        capabilities=list_navigation_tool_capabilities(),
    )

    issue = next(issue for issue in report.errors if issue.code == "invalid_arguments")
    assert issue.allowed_values == [
        "action_contract:extract_and_sync_navigation_data"
    ]


def test_large_dependency_id_set_uses_plan_step_pointer_instead_of_echoing_ids():
    payload = valid_extract_plan_payload()
    prepare = payload["steps"][0]
    payload["steps"] = [
        {**prepare, "step_id": f"prepare_{index:03d}"}
        for index in reversed(range(40))
    ]
    payload["steps"].append(
        {
            **valid_extract_plan_payload()["steps"][1],
            "depends_on": ["missing_dependency"],
        }
    )

    report = validate_extract(payload)

    issue = next(issue for issue in report.errors if issue.code == "unknown_dependency")
    assert issue.allowed_values == ["plan.steps[*].step_id"]


def test_constructed_invalid_gridmap_source_returns_report_instead_of_raising():
    plan = FinishProcessingPlanInput.model_validate(valid_finish_plan_payload())
    plan.decisions.gridmap = plan.decisions.gridmap.model_copy(
        update={"source": "invented_source"}
    )

    report = validate_navigation_plan(
        task=finish_task(),
        observation=finish_observation(),
        plan=plan,
        evidence=finish_evidence(),
        capabilities=list_navigation_tool_capabilities(),
    )

    assert PlanValidationIssue(
        path="plan.decisions.gridmap.source",
        code="invalid_gridmap_source",
        message="Gridmap source is not supported",
        allowed_values=[
            "existing_gridmap",
            "generated_from_pcd",
            "projection_ready",
        ],
    ) in report.errors


def test_large_inventory_pointer_selects_latest_revision_then_created_at_then_ref():
    available_topics = [f"/topic/{index:03d}" for index in range(40)]
    observation = extract_observation().model_copy(
        update={
            "payloads": [
                item.model_copy(
                    update={"available_topics": [*item.available_topics, *available_topics]}
                )
                if isinstance(item, TopicCandidatesObservation)
                else item
                for item in extract_observation().payloads
            ]
        }
    )
    payload = valid_extract_plan_payload()
    payload["decisions"]["topic_selection"]["topic_whitelist"] = ["/missing"]
    evidence = [
        *extract_evidence(),
        descriptor(
            "zzzz-old-revision",
            3,
            kind="topic_candidates",
            created_at="2099-01-01T00:00:00+00:00",
        ),
        descriptor(
            "aaaa-current-older-created",
            4,
            kind="topic_candidates",
            created_at="2026-07-10T00:01:00+00:00",
        ),
        descriptor(
            "bbbb-current-latest-created",
            4,
            kind="topic_candidates",
            created_at="2026-07-10T00:02:00+00:00",
        ),
        descriptor(
            "cccc-current-latest-created",
            4,
            kind="topic_candidates",
            created_at="2026-07-10T00:02:00+00:00",
        ),
    ]

    report = validate_extract(payload, observation=observation, evidence=evidence)

    issue = next(issue for issue in report.errors if issue.code == "unobserved_topic")
    assert issue.allowed_values == [
        "evidence_ref:cccc-current-latest-created"
    ]


def test_mixed_topic_inventory_pointer_selects_the_payload_kind_that_carries_values():
    raw_topics = [f"/raw-only/{index:03d}" for index in range(40)]
    observation = extract_observation().model_copy(
        update={
            "payloads": [
                item.model_copy(
                    update={
                        "topics": [
                            *item.topics,
                            *[
                                TopicMeasurement(topic=topic, message_count=1)
                                for topic in raw_topics
                            ],
                        ]
                    }
                )
                if isinstance(item, RawMetadataObservation)
                else item
                for item in extract_observation().payloads
            ]
        }
    )
    payload = valid_extract_plan_payload()
    payload["decisions"]["topic_selection"]["topic_whitelist"] = ["/missing"]
    evidence = [
        *extract_evidence(),
        descriptor(
            "raw-inventory-current",
            4,
            kind="raw_metadata",
            created_at="2026-07-10T00:03:00+00:00",
        ),
        descriptor(
            "topic-inventory-current",
            4,
            kind="topic_candidates",
            created_at="2026-07-10T00:04:00+00:00",
        ),
    ]

    report = validate_extract(payload, observation=observation, evidence=evidence)

    issue = next(issue for issue in report.errors if issue.code == "unobserved_topic")
    assert issue.allowed_values == ["evidence_ref:raw-inventory-current"]


def test_large_inventory_without_matching_descriptor_uses_observation_path_pointer():
    sources = [f"sensor_{index:03d}" for index in range(40)]
    observation = finish_observation().model_copy(
        update={
            "payloads": [
                item.model_copy(update={"sensor_sources": sources})
                if isinstance(item, CalibrationInventoryObservation)
                else item
                for item in finish_observation().payloads
            ]
        }
    )
    payload = valid_finish_plan_payload()
    payload["decisions"]["calibration"]["selected_sensor_source"] = "missing"
    evidence = [
        item.model_copy(update={"kind": "decision_support"})
        if item.ref == "evidence:calibration"
        else item
        for item in finish_evidence()
    ]

    report = validate_finish(payload, observation=observation, evidence=evidence)

    issue = next(issue for issue in report.errors if issue.code == "unobserved_calibration_source")
    assert issue.allowed_values == [
        "observation.payloads[kind=calibration_inventory]"
    ]
    assert not any(source in issue.allowed_values for source in sources[:20])

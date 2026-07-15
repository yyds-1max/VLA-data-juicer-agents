from vla_data_juicer_agents.navigation.catalog import (
    CAPABILITY_CATALOG_REVISION,
    list_navigation_tool_capabilities,
    list_navigation_tool_capabilities_tool,
    navigation_tool_capabilities_payload,
)


def _capability_by_stage():
    return {cap.stage_kind: cap for cap in list_navigation_tool_capabilities()}


def test_catalog_exposes_known_stage_variants_for_plan_agent():
    capabilities = _capability_by_stage()

    assert capabilities["extract_and_sync_navigation_data"].tool_name == "extract_and_sync_navigation_data"
    assert {variant.id for variant in capabilities["extract_and_sync_navigation_data"].variants} == {
        "explicit_topic_params",
    }
    assert {variant.id for variant in capabilities["prepare_gridmap_for_projection"].variants} == {
        "copy_existing_gridmap",
        "generate_from_pcd",
        "skip_if_projection_ready",
    }
    assert {variant.id for variant in capabilities["run_projection_and_trajectory"].variants} == {
        "cjl_with_gridmap",
        "cjl_0525_with_gridmap",
    }


def test_catalog_marks_effects_and_plan_agent_visibility():
    capabilities = _capability_by_stage()

    assert capabilities["inspect_raw_date"].effects == "read"
    assert capabilities["inspect_raw_date"].plan_agent_allowed is True
    assert "classify_navigation_dataset" not in capabilities
    assert capabilities["run_tracking"].effects == "execute"
    assert capabilities["run_tracking"].executor_agent_allowed is True


def test_catalog_marks_every_data_mutating_processing_action_as_target_locking():
    capabilities = _capability_by_stage()
    mutating_executable_actions = {
        name
        for name, capability in capabilities.items()
        if capability.executor_agent_allowed
        and capability.effects in {"write", "execute", "external"}
    }
    locking_actions = {
        name
        for name, capability in capabilities.items()
        if capability.locks_navigation_target
    }

    assert locking_actions == mutating_executable_actions
    assert locking_actions == {
        "prepare_raw_data",
        "extract_and_sync_navigation_data",
        "assemble_finish_temp",
        "run_noobscene_preprocessing",
        "run_initial_annotation_gui",
        "run_tracking",
        "prepare_gridmap_for_projection",
        "run_projection_and_trajectory",
    }


def test_catalog_never_locks_read_validation_or_human_decision_actions():
    capabilities = _capability_by_stage()

    assert all(
        not capability.locks_navigation_target
        for capability in capabilities.values()
        if capability.effects == "read"
    )
    assert capabilities["confirm_navigation_calibration_params"].locks_navigation_target is False
    assert capabilities["validate_navigation_outputs"].locks_navigation_target is False


def test_catalog_exposes_calibration_confirmation_capability():
    capabilities = _capability_by_stage()
    capability = capabilities["confirm_navigation_calibration_params"]

    assert capability.tool_name == "confirm_navigation_calibration_params"
    assert capability.stage_kind == "confirm_navigation_calibration_params"
    assert capability.effects == "read"
    assert capability.human_blocking is True
    assert capability.supports_dry_run is True
    assert capability.executor_agent_allowed is True


def test_parameterized_navigation_variants_are_not_platform_buckets():
    capabilities = _capability_by_stage()
    selector_keys = {
        selector_key
        for stage_name in ("extract_and_sync_navigation_data", "run_projection_and_trajectory")
        for variant in capabilities[stage_name].variants
        for selector_key in variant.selectors
    }

    assert "dataset_profile" not in selector_keys
    assert "processing_profile" not in selector_keys
    assert "platform_hint" not in selector_keys


def test_catalog_tool_returns_json_serializable_payload():
    payload = navigation_tool_capabilities_payload()

    assert payload["scenario"] == "navigation_vla"
    assert any(item["stage_kind"] == "prepare_gridmap_for_projection" for item in payload["capabilities"])
    gridmap = next(item for item in payload["capabilities"] if item["stage_kind"] == "prepare_gridmap_for_projection")
    assert gridmap["variants"][0]["status"] == "available"
    assert list_navigation_tool_capabilities_tool.name == "list_navigation_tool_capabilities_tool"


def test_v3_catalog_exposes_factual_observation_capabilities():
    capabilities = _capability_by_stage()

    assert CAPABILITY_CATALOG_REVISION == "navigation-capabilities-v3"
    assert capabilities["inspect_navigation_sensor_candidates"].phase == "extract_sync"
    assert capabilities["inspect_navigation_sensor_candidates"].declared_output_kinds == ["sensor_candidates"]
    assert capabilities["inspect_navigation_topic_candidates"].phase == "extract_sync"
    assert capabilities["inspect_navigation_topic_candidates"].declared_output_kinds == ["topic_candidates"]
    assert capabilities["inspect_navigation_raw_metadata"].phase == "extract_sync"
    assert capabilities["inspect_navigation_raw_metadata"].declared_output_kinds == ["raw_metadata"]
    assert capabilities["inspect_navigation_artifact_state"].declared_output_kinds == ["artifact_state"]
    assert capabilities["inspect_navigation_gridmap_artifacts"].phase == "finish_processing"
    assert capabilities["inspect_navigation_gridmap_artifacts"].declared_output_kinds == ["gridmap_artifacts"]
    assert capabilities["inspect_navigation_runtime_assets"].phase == "finish_processing"
    assert capabilities["inspect_navigation_runtime_assets"].declared_output_kinds == ["runtime_assets"]
    assert capabilities["inspect_navigation_calibration_inventory"].phase == "finish_processing"
    assert capabilities["inspect_navigation_calibration_inventory"].declared_output_kinds == [
        "calibration_inventory"
    ]
    assert capabilities["inspect_navigation_localization_sources"].phase == "finish_processing"
    assert capabilities["inspect_navigation_localization_sources"].declared_output_kinds == [
        "localization_sources"
    ]


def test_v2_catalog_declares_argument_models_and_omits_combined_execution_tool():
    capabilities = list_navigation_tool_capabilities()

    by_stage = {capability.stage_kind: capability for capability in capabilities}
    assert by_stage["prepare_raw_data"].argument_model == "EmptyArguments"
    assert by_stage["extract_and_sync_navigation_data"].argument_model == "ExtractSyncArguments"
    assert by_stage["run_tracking"].argument_model == "EmptyArguments"

    payload = navigation_tool_capabilities_payload()
    assert payload["revision"] == CAPABILITY_CATALOG_REVISION

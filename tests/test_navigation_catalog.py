from vla_data_juicer_agents.navigation.catalog import (
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
        "u_legacy_like",
        "go2w_like",
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
    assert capabilities["classify_navigation_dataset"].effects == "read"
    assert capabilities["classify_navigation_dataset"].plan_agent_allowed is False
    assert capabilities["infer_navigation_sensor_bindings"].effects == "read"
    assert capabilities["infer_navigation_sensor_bindings"].plan_agent_allowed is True
    assert capabilities["infer_navigation_processing_profile"].effects == "read"
    assert capabilities["infer_navigation_processing_profile"].plan_agent_allowed is True
    assert capabilities["run_tracking"].effects == "execute"
    assert capabilities["run_tracking"].executor_agent_allowed is True


def test_catalog_exposes_calibration_confirmation_capability():
    capabilities = _capability_by_stage()
    capability = capabilities["confirm_navigation_calibration_params"]

    assert capability.tool_name == "confirm_navigation_calibration_params"
    assert capability.stage_kind == "confirm_navigation_calibration_params"
    assert capability.effects == "read"
    assert capability.human_blocking is True
    assert capability.supports_dry_run is True
    assert capability.executor_agent_allowed is True


def test_parameterized_navigation_selectors_do_not_use_dataset_profile():
    capabilities = _capability_by_stage()
    selector_keys = {
        selector_key
        for stage_name in ("extract_and_sync_navigation_data", "run_projection_and_trajectory")
        for variant in capabilities[stage_name].variants
        for selector_key in variant.selectors
    }

    assert "dataset_profile" not in selector_keys
    assert "processing_profile" in selector_keys
    assert "platform_hint" in selector_keys


def test_catalog_tool_returns_json_serializable_payload():
    payload = navigation_tool_capabilities_payload()

    assert payload["scenario"] == "navigation_vla"
    assert any(item["stage_kind"] == "prepare_gridmap_for_projection" for item in payload["capabilities"])
    gridmap = next(item for item in payload["capabilities"] if item["stage_kind"] == "prepare_gridmap_for_projection")
    assert gridmap["variants"][0]["status"] == "available"
    assert list_navigation_tool_capabilities_tool.name == "list_navigation_tool_capabilities_tool"

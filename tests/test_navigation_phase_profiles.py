from vla_data_juicer_agents.navigation import models
from vla_data_juicer_agents.navigation.models import NavigationRequest
from vla_data_juicer_agents.navigation.plan_draft import (
    WorkflowPlanDraftState,
    build_extract_sync_plan_from_draft,
)


def _extract_topic_params() -> dict:
    return {
        "profile_hint": "mixed",
        "confidence": 1.0,
        "topic_whitelist": ["/cam", "/lidar_points", "/sport_odom"],
        "topic_map": {
            "cam": "fisheye_front",
            "lidar_points": "r32_rslidar_points",
            "sport_odom": "odom",
        },
        "query_dir": "lidar_points",
    }


def test_legacy_navigation_data_profile_model_is_removed():
    assert not hasattr(models, "NavigationDataProfile")
    assert hasattr(models, "NavigationExtractSyncProfile")
    assert hasattr(models, "NavigationFinishProcessingProfile")


def test_extract_sync_profile_finalizes_without_finish_processing_facts():
    state = WorkflowPlanDraftState(request=NavigationRequest(date="20270605"))
    for observation_id, used_tool in [
        ("raw_metadata", "inspect_raw_date_tool"),
        ("sensor_bindings", "infer_navigation_sensor_bindings_tool"),
        ("navigation_topic_params", "infer_navigation_topic_params_tool"),
    ]:
        state.update(
            data_profile_patch={"evidence": {observation_id: [used_tool]}},
            observation_id=observation_id,
            used_tool=used_tool,
        )
    state.update(
        data_profile_patch={
            "topic_params": _extract_topic_params(),
            "stage_variants": {
                "extract_and_sync_navigation_data": {
                    "variant": "explicit_topic_params",
                    "reason": "topic params were inferred from metadata",
                    "evidence": ["infer_navigation_topic_params_tool"],
                }
            },
        }
    )

    assert state.missing_fields(phase="extract_sync") == []
    assert state.ready_to_finish(phase="extract_sync") is True
    assert state.next_tool_candidates() == ["finalize_extract_sync_plan_tool"]

    plan = build_extract_sync_plan_from_draft(state)

    assert plan.phase == "extract_sync"
    assert [step.tool_name for step in plan.steps] == [
        "prepare_raw_data",
        "extract_and_sync_navigation_data",
    ]
    extract_step = plan.steps[1]
    assert extract_step.variant == "explicit_topic_params"
    assert extract_step.arguments["topic_whitelist"] == ["/cam", "/lidar_points", "/sport_odom"]

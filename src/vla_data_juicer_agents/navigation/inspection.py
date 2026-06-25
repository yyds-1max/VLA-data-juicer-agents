import re
import json
from pathlib import Path
from typing import Literal

import yaml
from agentscope.tool import FunctionTool

from vla_data_juicer_agents.navigation.config import NavigationSettings
from vla_data_juicer_agents.navigation.models import (
    NavigationCalibrationPolicy,
    NavigationGridmapPolicy,
    NavigationLocalizationPolicy,
    NavigationProcessingProfile,
    NavigationSensorBinding,
    NavigationSensorBindings,
    NavigationTopicParams,
    PlanIssue,
    RawDateInspection,
    SegmentInspection,
    TopicInfo,
)
from vla_data_juicer_agents.navigation.profiles import (
    TOPIC_OUTPUT_MAP,
    classify_topics,
    topics_for_role,
)


DATE_RE = re.compile(r"^[0-9]{8}$")
RootKind = Literal["raw_data", "clip_data", "finish_data"]

_TOPIC_PARAM_PATTERNS = [
    {
        "profile_hint": "u_like",
        "topics": [
            "/cam_video5/csi_cam/image_raw/compressed",
            "/lidar_points",
            "/utlidar/robot_odom_systime",
        ],
        "topic_map": {
            "cam_video5": "fisheye_front",
            "lidar_points": "r32_rslidar_points",
            "utlidar": "odom",
        },
        "query_dir": "lidar_points",
    },
    {
        "profile_hint": "go2w_like",
        "topics": [
            "/cam_video4/csi_cam/image_raw/compressed",
            "/rs32_lidar_points",
            "/sport_odom",
        ],
        "topic_map": {
            "cam_video4": "fisheye_front",
            "rs32_lidar_points": "r32_rslidar_points",
            "sport_odom": "odom",
        },
        "query_dir": "rs32_lidar_points",
    },
    {
        "profile_hint": "hybrid",
        "topics": [
            "/cam_video5/csi_cam/image_raw/compressed",
            "/lidar_points",
            "/sport_odom",
        ],
        "topic_map": {
            "cam_video5": "fisheye_front",
            "lidar_points": "r32_rslidar_points",
            "sport_odom": "odom",
        },
        "query_dir": "lidar_points",
    },
]

_KNOWN_TOPIC_ORDER = [
    "/cam_video5/csi_cam/image_raw/compressed",
    "/cam_video4/csi_cam/image_raw/compressed",
    "/lidar_points",
    "/rs32_lidar_points",
    "/utlidar/robot_odom_systime",
    "/sport_odom",
]


def _normalize_segments(segments: list[str] | str | None) -> list[str] | None:
    if segments is None:
        return None
    if isinstance(segments, str):
        stripped = segments.strip()
        if stripped.startswith("["):
            payload = json.loads(stripped)
            if isinstance(payload, list) and all(isinstance(item, str) for item in payload):
                return payload
        return [stripped] if stripped else None
    return segments


def _root_for(root_kind: RootKind, settings: NavigationSettings) -> Path:
    if root_kind == "raw_data":
        return settings.raw_data_root
    if root_kind == "clip_data":
        return settings.clip_data_root
    if root_kind == "finish_data":
        return settings.finish_data_root
    raise ValueError(f"unsupported navigation root kind: {root_kind}")


def list_navigation_dates(root_kind: RootKind, settings: NavigationSettings | None = None) -> list[str]:
    settings = settings or NavigationSettings()
    root = _root_for(root_kind, settings)
    if not root.exists():
        return []

    return sorted(path.name for path in root.iterdir() if path.is_dir() and DATE_RE.match(path.name))


def _parse_metadata(metadata_path: Path) -> list[TopicInfo]:
    payload = yaml.safe_load(metadata_path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError("metadata must be a mapping")

    bag_info = payload.get("rosbag2_bagfile_information")
    if not isinstance(bag_info, dict):
        raise ValueError("missing rosbag2_bagfile_information")

    topic_entries = bag_info.get("topics_with_message_count")
    if not isinstance(topic_entries, list):
        raise ValueError("missing or invalid topics_with_message_count")

    topics: list[TopicInfo] = []
    for index, entry in enumerate(topic_entries):
        if not isinstance(entry, dict):
            raise ValueError(f"topic entry {index} must be a mapping")

        topic_metadata = entry.get("topic_metadata")
        if not isinstance(topic_metadata, dict):
            raise ValueError(f"topic entry {index} missing topic_metadata")

        name = topic_metadata.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError(f"topic entry {index} missing topic name")

        message_count = entry.get("message_count")
        if isinstance(message_count, bool) or not isinstance(message_count, int) or message_count < 0:
            raise ValueError(f"topic entry {index} has invalid message_count")

        topics.append(
            TopicInfo(
                name=name,
                type=topic_metadata.get("type"),
                message_count=message_count,
            )
        )
    return topics


def inspect_raw_date(date: str, settings: NavigationSettings | None = None) -> RawDateInspection:
    settings = settings or NavigationSettings()
    raw_dir = settings.raw_data_root / date
    result = RawDateInspection(date=date, path=raw_dir, exists=raw_dir.exists())

    if not raw_dir.exists():
        result.errors.append(f"raw data date directory not found: {raw_dir}")
        return result

    for segment_dir in sorted(path for path in raw_dir.iterdir() if path.is_dir()):
        metadata_path = segment_dir / "metadata.yaml"
        segment = SegmentInspection(name=segment_dir.name, path=segment_dir, metadata_path=metadata_path)
        if not metadata_path.exists():
            segment.errors.append(f"metadata.yaml not found: {metadata_path}")
        else:
            try:
                segment.topics = _parse_metadata(metadata_path)
            except Exception as exc:
                segment.errors.append(f"failed to parse metadata.yaml: {exc}")
        result.segments.append(segment)

    return result


def classify_navigation_dataset(
    date: str,
    segments: list[str] | None = None,
    settings: NavigationSettings | None = None,
):
    inspection = inspect_raw_date(date, settings=settings)
    selected_segments = inspection.segments
    if segments is not None:
        selected_names = set(segments)
        existing_names = {segment.name for segment in inspection.segments}
        missing_names = sorted(selected_names - existing_names)
        if missing_names:
            missing_text = ", ".join(missing_names)
            raise FileNotFoundError(f"requested raw segment(s) not found for {date}: {missing_text}")
        selected_segments = [segment for segment in inspection.segments if segment.name in selected_names]

    topic_names = {topic.name for segment in selected_segments for topic in segment.topics}
    return classify_topics(topic_names)


def _select_inspection_segments(
    inspection: RawDateInspection,
    segments: list[str] | None,
) -> list[SegmentInspection]:
    if segments is None:
        return inspection.segments
    selected_names = set(segments)
    existing_names = {segment.name for segment in inspection.segments}
    missing_names = sorted(selected_names - existing_names)
    if missing_names:
        missing_text = ", ".join(missing_names)
        raise FileNotFoundError(f"requested raw segment(s) not found for {inspection.date}: {missing_text}")
    return [segment for segment in inspection.segments if segment.name in selected_names]


def infer_navigation_topic_params(
    date: str,
    segments: list[str] | None = None,
    settings: NavigationSettings | None = None,
) -> NavigationTopicParams:
    inspection = inspect_raw_date(date, settings=settings)
    selected_segments = _select_inspection_segments(inspection, segments)
    topic_names = {topic.name for segment in selected_segments for topic in segment.topics}
    evidence = [
        str(segment.metadata_path)
        for segment in selected_segments
        if segment.metadata_path is not None and segment.topics
    ]
    warnings = [
        PlanIssue(
            type="raw_metadata_error",
            message=error,
            evidence=[str(segment.metadata_path)] if segment.metadata_path is not None else [segment.name],
        )
        for segment in selected_segments
        for error in segment.errors
    ]
    warnings.extend(
        PlanIssue(type="raw_date_error", message=error, evidence=[str(inspection.path)])
        for error in inspection.errors
    )
    blocking_issues = list(warnings)

    full_matches = [
        pattern for pattern in _TOPIC_PARAM_PATTERNS if set(pattern["topics"]).issubset(topic_names)
    ]
    if blocking_issues:
        known_existing = [topic for topic in _KNOWN_TOPIC_ORDER if topic in topic_names]
        return NavigationTopicParams(
            profile_hint=None,
            confidence=0.0,
            topic_whitelist=known_existing,
            topic_map={},
            query_dir=(
                "lidar_points"
                if "/lidar_points" in topic_names and "/rs32_lidar_points" not in topic_names
                else "rs32_lidar_points"
                if "/rs32_lidar_points" in topic_names and "/lidar_points" not in topic_names
                else None
            ),
            evidence=evidence,
            warnings=warnings,
            blocking_issues=blocking_issues,
        )

    if len(full_matches) == 1:
        match = full_matches[0]
        return NavigationTopicParams(
            profile_hint=str(match["profile_hint"]),
            confidence=1.0,
            topic_whitelist=list(match["topics"]),
            topic_map=dict(match["topic_map"]),
            query_dir=str(match["query_dir"]),
            evidence=evidence,
            warnings=warnings,
        )

    known_existing = [topic for topic in _KNOWN_TOPIC_ORDER if topic in topic_names]
    partial_scores = [
        len(set(pattern["topics"]).intersection(topic_names)) / len(pattern["topics"])
        for pattern in _TOPIC_PARAM_PATTERNS
    ]
    blocking_message = "Could not infer a unique camera/lidar/odom topic parameter set from raw metadata."
    if len(full_matches) > 1:
        blocking_message = "Multiple navigation topic parameter patterns matched raw metadata."
    return NavigationTopicParams(
        profile_hint=None,
        confidence=max(partial_scores, default=0.0),
        topic_whitelist=known_existing,
        topic_map={},
        query_dir=(
            "lidar_points"
            if "/lidar_points" in topic_names and "/rs32_lidar_points" not in topic_names
            else "rs32_lidar_points"
            if "/rs32_lidar_points" in topic_names and "/lidar_points" not in topic_names
            else None
        ),
        evidence=evidence,
        warnings=warnings,
        blocking_issues=[
            PlanIssue(
                type="missing_navigation_topic_params",
                message=blocking_message,
                evidence=known_existing or evidence,
            )
        ],
    )


def _topic_type_map(selected_segments: list[SegmentInspection]) -> dict[str, str | None]:
    return {
        topic.name: topic.type
        for segment in selected_segments
        for topic in segment.topics
    }


def _unique_role_binding(
    role: str,
    candidates: list[str],
    topic_types: dict[str, str | None],
    *,
    kind: str,
) -> tuple[NavigationSensorBinding | None, PlanIssue | None]:
    if len(candidates) == 1:
        topic = candidates[0]
        return (
            NavigationSensorBinding(
                role=role,
                topic=topic,
                message_type=topic_types.get(topic),
                kind=kind,
            ),
            None,
        )
    if len(candidates) > 1:
        return (
            NavigationSensorBinding(role=role, kind=kind, candidates=candidates),
            PlanIssue(
                type="ambiguous_navigation_topic_role",
                message=f"Multiple topics match navigation role {role}.",
                evidence=candidates,
            ),
        )
    return (
        None,
        PlanIssue(
            type="missing_navigation_topic_role",
            message=f"No topic found for required navigation role {role}.",
        ),
    )


def infer_navigation_sensor_bindings(
    date: str,
    segments: list[str] | None = None,
    settings: NavigationSettings | None = None,
) -> NavigationSensorBindings:
    inspection = inspect_raw_date(date, settings=settings)
    selected_segments = _select_inspection_segments(inspection, segments)
    topic_types = _topic_type_map(selected_segments)
    topic_names = set(topic_types)
    evidence = [
        str(segment.metadata_path)
        for segment in selected_segments
        if segment.metadata_path is not None and segment.topics
    ]
    warnings = [
        PlanIssue(
            type="raw_metadata_error",
            message=error,
            evidence=[str(segment.metadata_path)] if segment.metadata_path is not None else [segment.name],
        )
        for segment in selected_segments
        for error in segment.errors
    ]
    warnings.extend(
        PlanIssue(type="raw_date_error", message=error, evidence=[str(inspection.path)])
        for error in inspection.errors
    )

    blocking_issues: list[PlanIssue] = list(warnings)
    fisheye_front, issue = _unique_role_binding(
        "fisheye_front",
        topics_for_role(topic_names, "fisheye_front"),
        topic_types,
        kind="camera",
    )
    if issue is not None:
        blocking_issues.append(issue)

    lidar, issue = _unique_role_binding(
        "lidar",
        topics_for_role(topic_names, "lidar"),
        topic_types,
        kind="lidar",
    )
    if issue is not None:
        blocking_issues.append(issue)

    odom, odom_issue = _unique_role_binding(
        "odom",
        topics_for_role(topic_names, "odom"),
        topic_types,
        kind="odom",
    )
    ins, ins_issue = _unique_role_binding(
        "ins",
        topics_for_role(topic_names, "ins"),
        topic_types,
        kind="ins",
    )

    localization: NavigationSensorBinding | None = None
    if ins is not None and ins.topic is not None:
        localization = NavigationSensorBinding(
            role="localization",
            topic=ins.topic,
            message_type=ins.message_type,
            kind="ins",
        )
        if odom_issue is not None and odom_issue.type == "ambiguous_navigation_topic_role":
            warnings.append(odom_issue)
    elif ins_issue is not None and ins_issue.type == "ambiguous_navigation_topic_role":
        blocking_issues.append(ins_issue)
    elif odom is not None and odom.topic is not None:
        localization = NavigationSensorBinding(
            role="localization",
            topic=odom.topic,
            message_type=odom.message_type,
            kind="odom",
        )
    elif odom_issue is not None and odom_issue.type == "ambiguous_navigation_topic_role":
        blocking_issues.append(odom_issue)
    else:
        blocking_issues.append(
            PlanIssue(
                type="missing_navigation_topic_role",
                message="No unique odom or ins topic found for navigation localization.",
            )
        )

    return NavigationSensorBindings(
        fisheye_front=fisheye_front,
        lidar=lidar,
        odom=odom,
        ins=ins,
        localization=localization,
        warnings=warnings,
        blocking_issues=blocking_issues,
        evidence=evidence,
    )


def _topic_map_entry(topic: str) -> tuple[str, str]:
    return TOPIC_OUTPUT_MAP[topic]


def _platform_hint_from_topics(topic_whitelist: list[str]) -> str:
    topics = set(topic_whitelist)
    if {
        "/cam_video4/csi_cam/image_raw/compressed",
        "/rs32_lidar_points",
        "/sport_odom",
    }.issubset(topics):
        return "go2w"
    if {
        "/cam_video5/csi_cam/image_raw/compressed",
        "/lidar_points",
        "/utlidar/robot_odom_systime",
    }.issubset(topics):
        return "u"
    if "/drivers/ins/Ins" in topics:
        return "shanmao"
    return "unknown"


def infer_navigation_processing_profile(
    date: str,
    segments: list[str] | None = None,
    settings: NavigationSettings | None = None,
) -> NavigationProcessingProfile:
    sensor_bindings = infer_navigation_sensor_bindings(date, segments=segments, settings=settings)
    ordered_bindings = (
        sensor_bindings.fisheye_front,
        sensor_bindings.lidar,
        sensor_bindings.localization,
    )
    topic_whitelist = [
        binding.topic
        for binding in ordered_bindings
        if binding is not None and binding.topic is not None
    ]
    topic_map = dict(_topic_map_entry(topic) for topic in topic_whitelist)
    # query_dir is read as tmp_dir/<query_dir> before topic_map remapping, so keep the source key.
    lidar_query_dir = (
        _topic_map_entry(sensor_bindings.lidar.topic)[0]
        if sensor_bindings.lidar is not None and sensor_bindings.lidar.topic is not None
        else None
    )
    platform_hint = _platform_hint_from_topics(topic_whitelist)
    localization_source = (
        sensor_bindings.localization.kind
        if sensor_bindings.localization is not None and sensor_bindings.localization.kind in {"odom", "ins"}
        else "unknown"
    )
    blocking_issues = list(sensor_bindings.blocking_issues)

    return NavigationProcessingProfile(
        id="parameterized_navigation_v1",
        platform_hint=platform_hint,
        sensor_bindings=sensor_bindings,
        topic_params=NavigationTopicParams(
            profile_hint=platform_hint,
            confidence=1.0 if not blocking_issues else 0.0,
            topic_whitelist=topic_whitelist,
            topic_map=topic_map,
            query_dir=lidar_query_dir,
            evidence=list(sensor_bindings.evidence),
            warnings=list(sensor_bindings.warnings),
            blocking_issues=blocking_issues,
        ),
        localization_policy=NavigationLocalizationPolicy(
            source=localization_source,
            conversion="odom_to_ins" if localization_source == "odom" else "none",
        ),
        gridmap_policy=NavigationGridmapPolicy(source="generated_from_pcd"),
        calibration_policy=NavigationCalibrationPolicy(
            mode="hardcoded_with_user_confirmation",
            requires_user_confirmation=True,
        ),
        warnings=list(sensor_bindings.warnings),
        blocking_issues=blocking_issues,
        evidence={"metadata": list(sensor_bindings.evidence)},
    )


def _selected_segment_names(date_root: Path, segments: list[str] | None) -> list[str]:
    if segments is not None:
        return segments
    if not date_root.exists():
        return []
    return sorted(path.name for path in date_root.iterdir() if path.is_dir())


def inspect_processing_state(
    date: str,
    segments: list[str] | None = None,
    settings: NavigationSettings | None = None,
) -> dict:
    settings = settings or NavigationSettings()
    raw_temp_root = settings.raw_data_root / f"{date}_temp"
    clip_date_root = settings.clip_data_root / date
    finish_temp_samples = settings.finish_data_root / f"{date}_temp" / "samples" / date
    final_root = settings.finish_data_root / date
    selected = _selected_segment_names(raw_temp_root if raw_temp_root.exists() else settings.raw_data_root / date, segments)

    has_raw_temp = raw_temp_root.exists() and (
        not selected or all((raw_temp_root / segment).exists() for segment in selected)
    )
    has_clip_sync_data = any(
        (clip_date_root / segment / "sync_data").exists()
        for segment in (selected or _selected_segment_names(clip_date_root, None))
    )
    has_finish_temp_samples = finish_temp_samples.exists() and any(finish_temp_samples.iterdir())
    has_final_outputs = final_root.exists()
    has_final_grid_map = any(final_root.glob("*/*/grid_map")) if final_root.exists() else False

    return {
        "date": date,
        "segments": selected or segments,
        "has_raw_temp": has_raw_temp,
        "has_clip_sync_data": has_clip_sync_data,
        "has_finish_temp_samples": has_finish_temp_samples,
        "has_final_outputs": has_final_outputs,
        "has_final_grid_map": has_final_grid_map,
    }


def inspect_gridmap_artifacts(
    date: str,
    segments: list[str] | None = None,
    settings: NavigationSettings | None = None,
) -> dict:
    settings = settings or NavigationSettings()
    finish_temp_samples = settings.finish_data_root / f"{date}_temp" / "samples" / date
    projection_ready_paths = sorted(path for path in finish_temp_samples.glob("*/grid_map") if path.is_dir())
    if projection_ready_paths:
        return {
            "date": date,
            "segments": segments,
            "gridmap_source": "projection_ready",
            "projection_input_ready": True,
            "available_gridmap_paths": [str(path) for path in projection_ready_paths],
        }

    clip_date_root = settings.clip_data_root / date
    selected = _selected_segment_names(clip_date_root, segments)
    search_roots = [clip_date_root / segment / "sync_data" for segment in selected] if selected else []
    gridmap_paths = sorted(
        path
        for root in search_roots
        if root.exists()
        for path in root.glob("*/grid_map")
        if path.is_dir()
    )
    return {
        "date": date,
        "segments": selected or segments,
        "gridmap_source": "existing_gridmap" if gridmap_paths else "unknown",
        "projection_input_ready": False,
        "available_gridmap_paths": [str(path) for path in gridmap_paths],
    }


def inspect_runtime_assets(settings: NavigationSettings | None = None) -> dict:
    settings = settings or NavigationSettings()
    pt_project = settings.processing_root / "2_pt_project"
    return {
        "pcd_gridmap_tool_available": settings.pcd_to_grid_script.exists(),
        "manual_annotation_gui_available": settings.gen_box_script.exists(),
        "projection_variants": {
            "cjl_with_gridmap": (pt_project / "2_othermethod_cjl.py").exists(),
            "cjl_0525_with_gridmap": (pt_project / "2_othermethod_cjl_0525.py").exists(),
        },
    }


def _make_function_tool(func, name: str):
    return FunctionTool(func, name=name, is_read_only=True)


def _list_navigation_dates_tool(root_kind: RootKind) -> dict:
    """List available navigation dataset dates under a VLADatasets root kind."""
    return {"dates": list_navigation_dates(root_kind)}


def _inspect_raw_date_tool(date: str) -> dict:
    """Inspect raw navigation metadata for one date and report segment topics or errors."""
    return inspect_raw_date(date).model_dump(mode="json")


def _classify_navigation_dataset_tool(date: str, segments: list[str] | str | None = None) -> dict:
    """Classify a raw navigation date using all segments, or a selected segment list when provided."""
    return classify_navigation_dataset(date, segments=_normalize_segments(segments)).model_dump(mode="json")


def _infer_navigation_topic_params_tool(date: str, segments: list[str] | str | None = None) -> dict:
    """Infer TOPIC_WHITELIST, topic_map, and query_dir from raw navigation metadata topics."""
    params = infer_navigation_topic_params(date, segments=_normalize_segments(segments))
    payload = params.model_dump(mode="json")
    payload["ok"] = not params.blocking_issues
    return payload


def _infer_navigation_sensor_bindings_tool(date: str, segments: list[str] | str | None = None) -> dict:
    """Infer role-based navigation sensor bindings from raw navigation metadata topics."""
    return infer_navigation_sensor_bindings(date, segments=_normalize_segments(segments)).model_dump(mode="json")


def _infer_navigation_processing_profile_tool(date: str, segments: list[str] | str | None = None) -> dict:
    """Infer a parameterized navigation processing profile from raw navigation metadata topics."""
    return infer_navigation_processing_profile(date, segments=_normalize_segments(segments)).model_dump(mode="json")


def _inspect_processing_state_tool(date: str, segments: list[str] | str | None = None) -> dict:
    """Inspect existing navigation intermediate outputs without modifying data."""
    return inspect_processing_state(date, segments=_normalize_segments(segments))


def _inspect_gridmap_artifacts_tool(date: str, segments: list[str] | str | None = None) -> dict:
    """Inspect existing grid_map artifacts that can drive gridmap workflow variant selection."""
    return inspect_gridmap_artifacts(date, segments=_normalize_segments(segments))


def _inspect_runtime_assets_tool() -> dict:
    """Inspect script assets that determine available navigation workflow variants."""
    return inspect_runtime_assets()


list_navigation_dates_tool = _make_function_tool(_list_navigation_dates_tool, "list_navigation_dates_tool")
inspect_raw_date_tool = _make_function_tool(_inspect_raw_date_tool, "inspect_raw_date_tool")
classify_navigation_dataset_tool = _make_function_tool(
    _classify_navigation_dataset_tool,
    "classify_navigation_dataset_tool",
)
infer_navigation_topic_params_tool = _make_function_tool(
    _infer_navigation_topic_params_tool,
    "infer_navigation_topic_params_tool",
)
infer_navigation_sensor_bindings_tool = _make_function_tool(
    _infer_navigation_sensor_bindings_tool,
    "infer_navigation_sensor_bindings_tool",
)
infer_navigation_processing_profile_tool = _make_function_tool(
    _infer_navigation_processing_profile_tool,
    "infer_navigation_processing_profile_tool",
)
inspect_processing_state_tool = _make_function_tool(_inspect_processing_state_tool, "inspect_processing_state_tool")
inspect_gridmap_artifacts_tool = _make_function_tool(_inspect_gridmap_artifacts_tool, "inspect_gridmap_artifacts_tool")
inspect_runtime_assets_tool = _make_function_tool(_inspect_runtime_assets_tool, "inspect_runtime_assets_tool")

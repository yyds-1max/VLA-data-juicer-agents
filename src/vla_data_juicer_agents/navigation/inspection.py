import re
import json
from pathlib import Path
from typing import Literal

import yaml

from vla_data_juicer_agents.navigation.config import NavigationSettings
from vla_data_juicer_agents.navigation.models import RawDateInspection, SegmentInspection, TopicInfo
from vla_data_juicer_agents.navigation.observation_models import (
    CalibrationInventoryObservation,
    LocalizationSourcesObservation,
    SensorCandidatesObservation,
    SensorRoleCandidate,
    TopicCandidatesObservation,
    TopicRouteCandidate,
)
from vla_data_juicer_agents.navigation.profiles import topic_route, topics_for_role


DATE_RE = re.compile(r"^[0-9]{8}$")
RootKind = Literal["raw_data", "clip_data", "finish_data"]

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


def _topic_type_map(selected_segments: list[SegmentInspection]) -> dict[str, str | None]:
    return {
        topic.name: topic.type
        for segment in selected_segments
        for topic in segment.topics
    }


def _candidate_roles(topic: str, message_type: str | None, topic_names: set[str]) -> list[tuple[str, float]]:
    known_roles = [
        role
        for role in ("fisheye_front", "lidar", "odom", "ins", "gridmap")
        if topic in topics_for_role(topic_names, role)
    ]
    if known_roles:
        return [(role, 1.0) for role in known_roles]

    type_name = message_type or ""
    topic_lower = topic.lower()
    if "CompressedImage" in type_name or type_name.endswith("/Image"):
        return [("fisheye_front", 0.5)]
    if "PointCloud2" in type_name:
        return [("lidar", 0.8)]
    if "Odometry" in type_name:
        return [("odom", 0.9)]
    if type_name.endswith("/Ins") or topic.endswith("/Ins"):
        return [("ins", 0.8)]
    if "gridmap" in topic_lower or "grid_map" in topic_lower:
        return [("gridmap", 0.7)]
    return []


def _sensor_candidates(topic_types: dict[str, str | None]) -> list[SensorRoleCandidate]:
    topic_names = set(topic_types)
    candidates: list[SensorRoleCandidate] = []
    for topic in sorted(topic_names):
        message_type = topic_types[topic]
        roles = _candidate_roles(topic, message_type, topic_names)
        for role, confidence in roles:
            candidates.append(
                SensorRoleCandidate(
                    role=role,
                    topic=topic,
                    message_type=message_type,
                    confidence=confidence,
                )
            )
            if role in {"odom", "ins"}:
                candidates.append(
                    SensorRoleCandidate(
                        role="localization",
                        topic=topic,
                        message_type=message_type,
                        confidence=confidence,
                    )
                )
    return candidates


def inspect_navigation_sensor_candidates(
    date: str,
    segments: list[str] | None = None,
    settings: NavigationSettings | None = None,
) -> SensorCandidatesObservation:
    inspection = inspect_raw_date(date, settings=settings)
    selected_segments = _select_inspection_segments(inspection, segments)
    topic_types = _topic_type_map(selected_segments)
    return SensorCandidatesObservation(candidates=_sensor_candidates(topic_types))


def inspect_navigation_topic_candidates(
    date: str,
    segments: list[str] | None = None,
    settings: NavigationSettings | None = None,
) -> TopicCandidatesObservation:
    inspection = inspect_raw_date(date, settings=settings)
    selected_segments = _select_inspection_segments(inspection, segments)
    topic_types = _topic_type_map(selected_segments)
    candidates = _sensor_candidates(topic_types)
    topic_names = set(topic_types)
    suggested_role_names = {
        role: sorted({candidate.topic for candidate in candidates if candidate.role == role})
        for role in ("fisheye_front", "lidar", "odom", "ins", "localization", "gridmap")
    }
    routes = []
    for candidate in candidates:
        extracted_dir, output_dir = topic_route(
            candidate.topic,
            candidate.role,
            message_type=candidate.message_type,
        )
        routes.append(
            TopicRouteCandidate(
                role=candidate.role,
                topic=candidate.topic,
                extracted_dir=extracted_dir,
                output_dir=output_dir,
                sync_reference_eligible=candidate.role in {"lidar", "gridmap"},
            )
        )
    return TopicCandidatesObservation(
        available_topics=sorted(topic_names),
        suggested_role_names=suggested_role_names,
        routes=routes,
    )


def inspect_navigation_calibration_inventory(
    settings: NavigationSettings | None = None,
) -> CalibrationInventoryObservation:
    settings = settings or NavigationSettings()
    params_root = settings.processing_root / "NoobScenes" / "params"
    required_files = ("fisheye_front.json", "r32_rslidar_points.json")
    sources = sorted(
        path.relative_to(settings.processing_root).as_posix()
        for path in params_root.glob("*/sensors")
        if path.is_dir() and all((path / name).is_file() for name in required_files)
    )
    return CalibrationInventoryObservation(sensor_sources=sources)


def inspect_navigation_localization_sources(
    date: str,
    segments: list[str] | None = None,
    settings: NavigationSettings | None = None,
) -> LocalizationSourcesObservation:
    settings = settings or NavigationSettings()
    candidates = inspect_navigation_sensor_candidates(
        date,
        segments=segments,
        settings=settings,
    )
    available_sources = sorted(
        {
            candidate.role
            for candidate in candidates.candidates
            if candidate.role in {"odom", "ins"}
        }
    )
    return LocalizationSourcesObservation(
        available_sources=available_sources,
        conversion_available=(
            settings.processing_root
            / "NoobScenes"
            / "include"
            / "1_odom_convert.py"
        ).is_file(),
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
    clip_date_root = settings.clip_data_root / date
    selected = _selected_segment_names(clip_date_root, segments)
    search_roots = [clip_date_root / segment / "sync_data" for segment in selected] if selected else []
    pcd_sources = sorted(
        str(path)
        for root in search_roots
        if root.exists()
        for path in root.glob("**/*.pcd")
        if path.is_file()
    )
    finish_temp_samples = settings.finish_data_root / f"{date}_temp" / "samples" / date
    projection_ready_paths = sorted(
        path
        for path in finish_temp_samples.glob("*/grid_map")
        if _has_gridmap_json(path)
    )
    if projection_ready_paths:
        return {
            "date": date,
            "segments": segments,
            "gridmap_source": "projection_ready",
            "projection_input_ready": True,
            "available_gridmap_paths": [str(path) for path in projection_ready_paths],
            "pcd_sources": pcd_sources,
        }

    gridmap_paths = sorted(
        path
        for root in search_roots
        if root.exists()
        for path in root.glob("*/grid_map")
        if _has_gridmap_json(path)
    )
    return {
        "date": date,
        "segments": selected or segments,
        "gridmap_source": "existing_gridmap" if gridmap_paths else "unknown",
        "projection_input_ready": False,
        "available_gridmap_paths": [str(path) for path in gridmap_paths],
        "pcd_sources": pcd_sources,
    }


def _has_gridmap_json(path: Path) -> bool:
    return path.is_dir() and any(child.is_file() for child in path.glob("*.json"))


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
        "noobscene_localization_variants": {
            "ins": (settings.processing_root / "NoobScenes" / "main_smart.py").exists(),
            "odom": (
                settings.processing_root / "NoobScenes" / "main_smart_odom.py"
            ).exists(),
        },
        "speed_direction_variants": {
            "ins": (pt_project / "4_speed_direction_Ins.py").exists(),
            "odom": (pt_project / "4_speed_direction_odom.py").exists(),
        },
        "scene_environment_affects_execution": False,
    }

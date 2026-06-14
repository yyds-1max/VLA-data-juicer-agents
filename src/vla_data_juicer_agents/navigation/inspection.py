import re
from pathlib import Path
from typing import Literal

import yaml
from agents import function_tool

from vla_data_juicer_agents.navigation.config import NavigationSettings
from vla_data_juicer_agents.navigation.models import RawDateInspection, SegmentInspection, TopicInfo
from vla_data_juicer_agents.navigation.profiles import classify_topics


DATE_RE = re.compile(r"^[0-9]{8}$")
RootKind = Literal["raw_data", "clip_data", "finish_data"]


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
    bag_info = payload.get("rosbag2_bagfile_information") or {}
    topic_entries = bag_info.get("topics_with_message_count") or []

    topics: list[TopicInfo] = []
    for entry in topic_entries:
        topic_metadata = entry.get("topic_metadata") or {}
        name = topic_metadata.get("name")
        if not name:
            continue
        topics.append(
            TopicInfo(
                name=name,
                type=topic_metadata.get("type"),
                message_count=entry.get("message_count") or 0,
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
        selected_segments = [segment for segment in inspection.segments if segment.name in selected_names]

    topic_names = {topic.name for segment in selected_segments for topic in segment.topics}
    return classify_topics(topic_names)


@function_tool
def list_navigation_dates_tool(root_kind: RootKind) -> dict:
    return {"dates": list_navigation_dates(root_kind)}


@function_tool
def inspect_raw_date_tool(date: str) -> dict:
    return inspect_raw_date(date).model_dump(mode="json")


@function_tool
def classify_navigation_dataset_tool(date: str, segments: list[str] | None = None) -> dict:
    return classify_navigation_dataset(date, segments=segments).model_dump(mode="json")

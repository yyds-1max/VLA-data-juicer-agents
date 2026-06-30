from __future__ import annotations

from pathlib import Path, PurePath
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator

from vla_data_juicer_agents.navigation.config import NavigationSettings
from vla_data_juicer_agents.navigation.models import _validate_date


ClipStatus = Literal["raw_only", "extracted", "synced", "error"]
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}
EXPECTED_SCAN_ERRORS = (
    OSError,
    ValueError,
    KeyError,
    TypeError,
    yaml.YAMLError,
)


class TopicSummary(BaseModel):
    name: str
    type: str
    message_count: int


class SyncFrameCounts(BaseModel):
    image: int = 0
    pointcloud: int = 0
    odom: int = 0
    grid_map: int = 0

    def plus(self, other: "SyncFrameCounts") -> "SyncFrameCounts":
        return SyncFrameCounts(
            image=self.image + other.image,
            pointcloud=self.pointcloud + other.pointcloud,
            odom=self.odom + other.odom,
            grid_map=self.grid_map + other.grid_map,
        )

    @property
    def total(self) -> int:
        return self.image + self.pointcloud + self.odom + self.grid_map


class SyncSequenceSummary(BaseModel):
    sequence: str
    frame_counts: SyncFrameCounts = Field(default_factory=SyncFrameCounts)


class ClipSummary(BaseModel):
    date: str
    clip: str
    duration_ns: int = 0
    raw_message_count: int = 0
    topics: list[TopicSummary] = Field(default_factory=list)
    has_tmp_dir: bool = False
    has_sync_data: bool = False
    sequences: list[SyncSequenceSummary] = Field(default_factory=list)
    sync_frame_counts: SyncFrameCounts = Field(default_factory=SyncFrameCounts)
    status: ClipStatus
    errors: list[str] = Field(default_factory=list)

    @field_validator("date")
    @classmethod
    def validate_date(cls, value: str) -> str:
        return _validate_date(value)


class DateSummary(BaseModel):
    date: str
    clip_count: int = 0
    total_duration_ns: int = 0
    raw_message_count: int = 0
    extracted_clip_count: int = 0
    synced_clip_count: int = 0
    sync_frame_counts: SyncFrameCounts = Field(default_factory=SyncFrameCounts)
    status: ClipStatus = "raw_only"
    clips: list[ClipSummary] = Field(default_factory=list)

    @field_validator("date")
    @classmethod
    def validate_date(cls, value: str) -> str:
        return _validate_date(value)


class DatasetTotals(BaseModel):
    date_count: int = 0
    clip_count: int = 0
    total_duration_ns: int = 0
    raw_message_count: int = 0
    extracted_clip_count: int = 0
    synced_clip_count: int = 0


class DatasetSummary(BaseModel):
    totals: DatasetTotals
    sync_distribution: SyncFrameCounts = Field(default_factory=SyncFrameCounts)
    dates: list[DateSummary] = Field(default_factory=list)


class SyncImageSequence(BaseModel):
    sequence: str
    images: list[str] = Field(default_factory=list)


class SyncImageListing(BaseModel):
    date: str
    clip: str
    sequences: list[SyncImageSequence] = Field(default_factory=list)

    @field_validator("date")
    @classmethod
    def validate_date(cls, value: str) -> str:
        return _validate_date(value)


def scan_navigation_dataset(settings: NavigationSettings | None = None) -> DatasetSummary:
    settings = settings or NavigationSettings()
    dates = [
        scan_navigation_date(path.name, settings)
        for path in _visible_dirs(settings.raw_data_root)
        if _is_valid_date_name(path.name)
    ]
    totals = DatasetTotals(
        date_count=len(dates),
        clip_count=sum(date.clip_count for date in dates),
        total_duration_ns=sum(date.total_duration_ns for date in dates),
        raw_message_count=sum(date.raw_message_count for date in dates),
        extracted_clip_count=sum(date.extracted_clip_count for date in dates),
        synced_clip_count=sum(date.synced_clip_count for date in dates),
    )
    sync_distribution = _sum_counts(date.sync_frame_counts for date in dates)
    return DatasetSummary(totals=totals, sync_distribution=sync_distribution, dates=dates)


def scan_navigation_date(date: str, settings: NavigationSettings | None = None) -> DateSummary:
    _validate_date(date)
    settings = settings or NavigationSettings()
    raw_date_dir = settings.raw_data_root / date
    clips = [
        _scan_clip(date, raw_clip_dir.name, raw_clip_dir, settings.clip_data_root / date / raw_clip_dir.name)
        for raw_clip_dir in _visible_dirs(raw_date_dir)
    ]
    sync_frame_counts = _sum_counts(clip.sync_frame_counts for clip in clips)
    return DateSummary(
        date=date,
        clip_count=len(clips),
        total_duration_ns=sum(clip.duration_ns for clip in clips),
        raw_message_count=sum(clip.raw_message_count for clip in clips),
        extracted_clip_count=sum(1 for clip in clips if clip.status in {"extracted", "synced"}),
        synced_clip_count=sum(1 for clip in clips if clip.status == "synced"),
        sync_frame_counts=sync_frame_counts,
        status=_date_status(clips),
        clips=clips,
    )


def list_sync_images(
    date: str,
    clip: str,
    settings: NavigationSettings | None = None,
) -> SyncImageListing:
    settings = settings or NavigationSettings()
    _validate_date(date)
    _validate_safe_component("clip", clip)
    _require_raw_clip(settings, date, clip)
    sync_data_dir = settings.clip_data_root / date / clip / "sync_data"
    sequences = []
    for sequence_dir in _visible_dirs(sync_data_dir):
        images = [
            path.name
            for path in _visible_files(sequence_dir / "fisheye_front")
            if _is_image_file(path)
        ]
        if images:
            sequences.append(SyncImageSequence(sequence=sequence_dir.name, images=images))
    return SyncImageListing(date=date, clip=clip, sequences=sequences)


def resolve_sync_image_path(
    date: str,
    clip: str,
    sequence: str,
    filename: str,
    settings: NavigationSettings | None = None,
) -> Path:
    settings = settings or NavigationSettings()
    _validate_date(date)
    _validate_safe_component("clip", clip)
    _validate_safe_component("sequence", sequence)
    _validate_safe_component("filename", filename)
    if not _is_image_name(filename):
        raise ValueError("filename must use .jpg, .jpeg, or .png extension")
    _require_raw_clip(settings, date, clip)

    path = settings.clip_data_root / date / clip / "sync_data" / sequence / "fisheye_front" / filename
    _ensure_relative_to(path, settings.clip_data_root)
    if not path.is_file():
        raise FileNotFoundError(path)
    return path


def _scan_clip(date: str, clip: str, raw_clip_dir: Path, clip_data_dir: Path) -> ClipSummary:
    errors: list[str] = []
    duration_ns = 0
    raw_message_count = 0
    topics: list[TopicSummary] = []
    try:
        duration_ns, raw_message_count, topics = _read_metadata(raw_clip_dir / "metadata.yaml")
    except EXPECTED_SCAN_ERRORS as exc:
        errors.append(f"metadata.yaml: {exc}")

    has_tmp_dir = False
    has_sync_data = False
    sequences: list[SyncSequenceSummary] = []
    sync_frame_counts = SyncFrameCounts()
    try:
        has_tmp_dir = (clip_data_dir / "tmp_dir").is_dir()
        has_sync_data = (clip_data_dir / "sync_data").is_dir()
        sequences = _scan_sync_sequences(clip_data_dir / "sync_data")
        sync_frame_counts = _sum_counts(sequence.frame_counts for sequence in sequences)
    except EXPECTED_SCAN_ERRORS as exc:
        errors.append(f"sync_data: {exc}")

    if clip_data_dir.exists() and not has_tmp_dir and sync_frame_counts.total == 0:
        errors.append("clip_data exists without tmp_dir or synced frames")

    if errors:
        status: ClipStatus = "error"
    elif sync_frame_counts.total > 0:
        status = "synced"
    elif has_tmp_dir:
        status = "extracted"
    else:
        status = "raw_only"

    return ClipSummary(
        date=date,
        clip=clip,
        duration_ns=duration_ns,
        raw_message_count=raw_message_count,
        topics=topics,
        has_tmp_dir=has_tmp_dir,
        has_sync_data=has_sync_data,
        sequences=sequences,
        sync_frame_counts=sync_frame_counts,
        status=status,
        errors=errors,
    )


def _read_metadata(metadata_path: Path) -> tuple[int, int, list[TopicSummary]]:
    with metadata_path.open("r", encoding="utf-8") as metadata_file:
        payload = yaml.safe_load(metadata_file)
    if not isinstance(payload, dict):
        raise ValueError("metadata is not a mapping")
    info = payload["rosbag2_bagfile_information"]
    if not isinstance(info, dict):
        raise ValueError("rosbag2_bagfile_information is not a mapping")

    duration = info["duration"]
    if not isinstance(duration, dict):
        raise ValueError("duration is not a mapping")
    duration_ns = int(duration["nanoseconds"])
    message_count = int(info["message_count"])
    topics = [_parse_topic(topic) for topic in info.get("topics_with_message_count", [])]
    return duration_ns, message_count, topics


def _parse_topic(raw_topic: Any) -> TopicSummary:
    if not isinstance(raw_topic, dict):
        raise ValueError("topic entry is not a mapping")
    topic_metadata = raw_topic.get("topic_metadata")
    if not isinstance(topic_metadata, dict):
        raise ValueError("topic_metadata is not a mapping")
    return TopicSummary(
        name=str(topic_metadata["name"]),
        type=str(topic_metadata["type"]),
        message_count=int(raw_topic["message_count"]),
    )


def _scan_sync_sequences(sync_data_dir: Path) -> list[SyncSequenceSummary]:
    return [
        SyncSequenceSummary(
            sequence=sequence_dir.name,
            frame_counts=SyncFrameCounts(
                image=sum(1 for path in _visible_files(sequence_dir / "fisheye_front") if _is_image_file(path)),
                pointcloud=sum(1 for path in _visible_files(sequence_dir / "r32_rslidar_points") if path.suffix.lower() == ".pcd"),
                odom=sum(1 for _ in _visible_files(sequence_dir / "odom")),
                grid_map=sum(1 for path in _visible_files(sequence_dir / "grid_map") if path.suffix.lower() == ".json"),
            ),
        )
        for sequence_dir in _visible_dirs(sync_data_dir)
    ]


def _visible_dirs(path: Path) -> list[Path]:
    if path.is_symlink() or not path.is_dir():
        return []
    return sorted(
        (
            child
            for child in path.iterdir()
            if child.is_dir() and not child.is_symlink() and _is_visible_name(child.name)
        ),
        key=lambda child: child.name,
    )


def _visible_files(path: Path) -> list[Path]:
    if path.is_symlink() or not path.is_dir():
        return []
    return sorted(
        (
            child
            for child in path.iterdir()
            if child.is_file() and not child.is_symlink() and _is_visible_name(child.name)
        ),
        key=lambda child: child.name,
    )


def _is_visible_name(name: str) -> bool:
    return not name.startswith(".")


def _is_valid_date_name(name: str) -> bool:
    try:
        _validate_date(name)
    except ValueError:
        return False
    return True


def _is_image_file(path: Path) -> bool:
    return _is_image_name(path.name)


def _is_image_name(name: str) -> bool:
    return Path(name).suffix.lower() in IMAGE_EXTENSIONS


def _sum_counts(counts: Any) -> SyncFrameCounts:
    total = SyncFrameCounts()
    for count in counts:
        total = total.plus(count)
    return total


def _date_status(clips: list[ClipSummary]) -> ClipStatus:
    if any(clip.status == "error" for clip in clips):
        return "error"
    if any(clip.status == "synced" for clip in clips):
        return "synced"
    if any(clip.status == "extracted" for clip in clips):
        return "extracted"
    return "raw_only"


def _validate_safe_component(label: str, value: str) -> None:
    pure = PurePath(value)
    if value in {"", ".", ".."} or pure.is_absolute() or len(pure.parts) != 1:
        raise ValueError(f"{label} must be a single safe path component")
    if value.startswith("._"):
        raise ValueError(f"{label} must not be a macOS resource fork name")


def _require_raw_clip(settings: NavigationSettings, date: str, clip: str) -> Path:
    raw_clip_dir = settings.raw_data_root / date / clip
    _ensure_relative_to(raw_clip_dir, settings.raw_data_root)
    if not raw_clip_dir.is_dir():
        raise FileNotFoundError(raw_clip_dir)
    return raw_clip_dir


def _ensure_relative_to(path: Path, root: Path) -> None:
    path.resolve(strict=False).relative_to(root.resolve(strict=False))

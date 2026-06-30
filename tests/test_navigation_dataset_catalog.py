from __future__ import annotations

from pathlib import Path

import pytest

from vla_data_juicer_agents.navigation import dataset_catalog
from vla_data_juicer_agents.navigation.config import NavigationSettings
from vla_data_juicer_agents.navigation.dataset_catalog import (
    list_sync_images,
    resolve_sync_image_path,
    scan_navigation_dataset,
    scan_navigation_date,
)


def settings_for(root: Path) -> NavigationSettings:
    return NavigationSettings(vladatasets_root=root)


def write_metadata(
    clip_dir: Path,
    *,
    duration_ns: int = 2_500_000_000,
    message_count: int = 15,
) -> None:
    clip_dir.mkdir(parents=True)
    (clip_dir / "metadata.yaml").write_text(
        f"""
rosbag2_bagfile_information:
  version: 4
  storage_identifier: sqlite3
  relative_file_paths:
    - sample_0.db3
  duration:
    nanoseconds: {duration_ns}
  starting_time:
    nanoseconds_since_epoch: 1778812189469693651
  message_count: {message_count}
  topics_with_message_count:
    - topic_metadata:
        name: /cam_video5/csi_cam/image_raw/compressed
        type: sensor_msgs/msg/CompressedImage
        serialization_format: cdr
      message_count: 3
    - topic_metadata:
        name: /lidar_points
        type: sensor_msgs/msg/PointCloud2
        serialization_format: cdr
      message_count: 2
    - topic_metadata:
        name: /utlidar/robot_odom_systime
        type: nav_msgs/msg/Odometry
        serialization_format: cdr
      message_count: 10
  compression_format: ""
  compression_mode: ""
""".lstrip(),
        encoding="utf-8",
    )


def touch_files(root: Path, names: list[str]) -> None:
    root.mkdir(parents=True)
    for name in names:
        (root / name).write_text("x", encoding="utf-8")


def test_summary_uses_raw_data_as_source_of_truth_and_ignores_orphan_clip_data(tmp_path: Path):
    root = tmp_path / "VLADatasets"
    write_metadata(root / "raw_data" / "20270605" / "clip_a", duration_ns=100, message_count=7)
    write_metadata(root / "raw_data" / "20270605" / "clip_b", duration_ns=200, message_count=8)
    touch_files(root / "clip_data" / "20270605" / "clip_a" / "sync_data" / "0001" / "fisheye_front", ["1.jpg"])
    touch_files(
        root / "clip_data" / "20270605" / "orphan_clip" / "sync_data" / "0001" / "fisheye_front",
        ["orphan.jpg"],
    )
    write_metadata(root / "raw_data" / "._20270606" / "ignored_clip")
    write_metadata(root / "raw_data" / "20270605" / "._ignored_clip")

    summary = scan_navigation_dataset(settings_for(root))

    assert summary.totals.date_count == 1
    assert summary.totals.clip_count == 2
    assert summary.totals.total_duration_ns == 300
    assert summary.totals.raw_message_count == 15
    assert summary.sync_distribution.image == 1
    assert [date.date for date in summary.dates] == ["20270605"]
    assert [clip.clip for clip in summary.dates[0].clips] == ["clip_a", "clip_b"]


def test_scan_ignores_symlinked_raw_clip_directory_that_escapes_dataset_root(tmp_path: Path):
    root = tmp_path / "VLADatasets"
    write_metadata(root / "raw_data" / "20270515" / "real_clip", duration_ns=100, message_count=7)
    outside = tmp_path / "outside_dataset" / "symlink_clip"
    write_metadata(outside, duration_ns=900, message_count=99)
    (root / "raw_data" / "20270515").mkdir(parents=True, exist_ok=True)
    (root / "raw_data" / "20270515" / "symlink_clip").symlink_to(outside, target_is_directory=True)

    date_summary = scan_navigation_date("20270515", settings_for(root))

    assert date_summary.clip_count == 1
    assert [clip.clip for clip in date_summary.clips] == ["real_clip"]
    assert date_summary.total_duration_ns == 100
    assert date_summary.raw_message_count == 7


def test_date_scan_reports_raw_only_extracted_and_synced_statuses(tmp_path: Path):
    root = tmp_path / "VLADatasets"
    write_metadata(root / "raw_data" / "20270605" / "raw_clip")
    write_metadata(root / "raw_data" / "20270605" / "extracted_clip")
    write_metadata(root / "raw_data" / "20270605" / "synced_clip")
    (root / "clip_data" / "20270605" / "extracted_clip" / "tmp_dir").mkdir(parents=True)
    (root / "clip_data" / "20270605" / "synced_clip" / "tmp_dir").mkdir(parents=True)
    touch_files(root / "clip_data" / "20270605" / "synced_clip" / "sync_data" / "0001" / "fisheye_front", ["1.JPG", "2.png", "note.txt"])
    touch_files(root / "clip_data" / "20270605" / "synced_clip" / "sync_data" / "0001" / "r32_rslidar_points", ["1.pcd", "._2.pcd"])
    touch_files(root / "clip_data" / "20270605" / "synced_clip" / "sync_data" / "0001" / "odom", ["1", "._2"])
    touch_files(root / "clip_data" / "20270605" / "synced_clip" / "sync_data" / "0001" / "grid_map", ["1.json", "2.txt"])

    date_summary = scan_navigation_date("20270605", settings_for(root))
    by_clip = {clip.clip: clip for clip in date_summary.clips}

    assert by_clip["raw_clip"].status == "raw_only"
    assert by_clip["extracted_clip"].status == "extracted"
    assert by_clip["synced_clip"].status == "synced"
    assert by_clip["synced_clip"].has_tmp_dir is True
    assert by_clip["synced_clip"].has_sync_data is True
    assert by_clip["synced_clip"].sync_frame_counts.model_dump() == {
        "image": 2,
        "pointcloud": 1,
        "odom": 1,
        "grid_map": 1,
    }
    assert date_summary.extracted_clip_count == 2
    assert date_summary.synced_clip_count == 1
    assert date_summary.sync_frame_counts.image == 2


def test_invalid_metadata_marks_clip_error_without_failing_summary_or_date_scan(tmp_path: Path):
    root = tmp_path / "VLADatasets"
    write_metadata(root / "raw_data" / "20270605" / "good_clip", duration_ns=42, message_count=4)
    bad_clip = root / "raw_data" / "20270605" / "bad_clip"
    bad_clip.mkdir(parents=True)
    (bad_clip / "metadata.yaml").write_text("rosbag2_bagfile_information: [", encoding="utf-8")

    date_summary = scan_navigation_date("20270605", settings_for(root))
    summary = scan_navigation_dataset(settings_for(root))
    by_clip = {clip.clip: clip for clip in date_summary.clips}

    assert by_clip["bad_clip"].status == "error"
    assert by_clip["bad_clip"].errors
    assert by_clip["good_clip"].status == "raw_only"
    assert date_summary.status == "error"
    assert date_summary.total_duration_ns == 42
    assert summary.totals.clip_count == 2
    assert summary.totals.total_duration_ns == 42


def test_existing_clip_data_without_tmp_dir_or_sync_counts_is_error_not_raw_only(tmp_path: Path):
    root = tmp_path / "VLADatasets"
    write_metadata(root / "raw_data" / "20270605" / "incomplete_clip")
    (root / "clip_data" / "20270605" / "incomplete_clip").mkdir(parents=True)

    date_summary = scan_navigation_date("20270605", settings_for(root))
    clip = date_summary.clips[0]

    assert clip.status == "error"
    assert "clip_data exists without tmp_dir or synced frames" in clip.errors
    assert date_summary.status == "error"


def test_sync_scan_error_marks_clip_error_without_failing_date_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root = tmp_path / "VLADatasets"
    write_metadata(root / "raw_data" / "20270605" / "bad_sync_clip")
    write_metadata(root / "raw_data" / "20270605" / "good_clip")
    (root / "clip_data" / "20270605" / "bad_sync_clip" / "sync_data" / "0001").mkdir(parents=True)

    def fail_for_sequence(path: Path) -> list[Path]:
        if path.name == "fisheye_front":
            raise OSError("cannot read fisheye_front")
        return []

    monkeypatch.setattr(dataset_catalog, "_visible_files", fail_for_sequence)

    date_summary = scan_navigation_date("20270605", settings_for(root))
    by_clip = {clip.clip: clip for clip in date_summary.clips}

    assert by_clip["bad_sync_clip"].status == "error"
    assert any("sync_data" in error and "cannot read fisheye_front" in error for error in by_clip["bad_sync_clip"].errors)
    assert by_clip["good_clip"].status == "raw_only"
    assert date_summary.status == "error"


def test_sync_image_file_symlinks_are_ignored_for_counts_and_listing(tmp_path: Path):
    root = tmp_path / "VLADatasets"
    write_metadata(root / "raw_data" / "20270605" / "clip_a")
    fisheye_dir = root / "clip_data" / "20270605" / "clip_a" / "sync_data" / "seq_a" / "fisheye_front"
    touch_files(fisheye_dir, ["real.jpg"])
    outside_image = tmp_path / "outside_dataset" / "linked.jpg"
    outside_image.parent.mkdir(parents=True)
    outside_image.write_text("outside", encoding="utf-8")
    try:
        (fisheye_dir / "linked.jpg").symlink_to(outside_image)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlink creation unsupported: {exc}")

    date_summary = scan_navigation_date("20270605", settings_for(root))
    listing = list_sync_images("20270605", "clip_a", settings_for(root))

    assert date_summary.clips[0].sync_frame_counts.image == 1
    assert [(seq.sequence, seq.images) for seq in listing.sequences] == [("seq_a", ["real.jpg"])]


def test_sync_images_list_by_sequence_and_safe_path_resolution_rejects_missing_or_unsafe_paths(tmp_path: Path):
    root = tmp_path / "VLADatasets"
    write_metadata(root / "raw_data" / "20270605" / "clip_a")
    touch_files(root / "clip_data" / "20270605" / "clip_a" / "sync_data" / "0002" / "fisheye_front", ["b.png", "a.jpeg", "note.txt"])
    touch_files(root / "clip_data" / "20270605" / "clip_a" / "sync_data" / "0001" / "fisheye_front", ["c.jpg", "._hidden.jpg"])

    listing = list_sync_images("20270605", "clip_a", settings_for(root))
    resolved = resolve_sync_image_path("20270605", "clip_a", "0002", "a.jpeg", settings_for(root))

    assert [(seq.sequence, seq.images) for seq in listing.sequences] == [
        ("0001", ["c.jpg"]),
        ("0002", ["a.jpeg", "b.png"]),
    ]
    assert resolved == root / "clip_data" / "20270605" / "clip_a" / "sync_data" / "0002" / "fisheye_front" / "a.jpeg"
    with pytest.raises(ValueError):
        resolve_sync_image_path("2027-06-05", "clip_a", "0002", "a.jpeg", settings_for(root))
    with pytest.raises(ValueError):
        resolve_sync_image_path("20270605", "clip_a", "0002", "../a.jpeg", settings_for(root))
    with pytest.raises(FileNotFoundError):
        resolve_sync_image_path("20270605", "missing_raw_clip", "0002", "a.jpeg", settings_for(root))
    with pytest.raises(FileNotFoundError):
        resolve_sync_image_path("20270605", "clip_a", "0002", "missing.jpeg", settings_for(root))
    with pytest.raises(ValueError):
        resolve_sync_image_path("20270605", "clip_a", "0002", "note.txt", settings_for(root))

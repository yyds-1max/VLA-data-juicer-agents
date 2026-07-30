from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from vla_data_juicer_agents.annotation.catalog import CalibrationCatalog


REQUIRED_CONTENT = {
    "fisheye_front.json": b'{"camera":"front"}\n',
    "r32_rslidar_points.json": b'{"lidar":"r32"}\n',
}


def _entry(
    profile_ref: str,
    filename: str,
    content: bytes,
    *,
    stage: str,
) -> dict[str, object]:
    return {
        "root_alias": "NAVIGATION_ODOM_V1_SOURCE",
        "relative_path": (
            f"NoobScenes/params/{profile_ref}/sensors/{filename}"
        ),
        "kind": "frozen_file",
        "role": "selectable_profile",
        "stage": stage,
        "sha256": hashlib.sha256(content).hexdigest(),
        "size": len(content),
        "executable": False,
    }


def _write_profile(
    source_root: Path,
    profile_ref: str,
    *,
    stage: str = "calibration",
    contents: dict[str, bytes] | None = None,
) -> list[dict[str, object]]:
    values = dict(REQUIRED_CONTENT if contents is None else contents)
    sensors = source_root / "NoobScenes" / "params" / profile_ref / "sensors"
    sensors.mkdir(parents=True)
    entries: list[dict[str, object]] = []
    for filename, content in values.items():
        (sensors / filename).write_bytes(content)
        entries.append(_entry(profile_ref, filename, content, stage=stage))
    return entries


def _write_manifest(
    path: Path,
    entries: list[dict[str, object]],
) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "runtime_id": "navigation_odom_test",
                "root_aliases": ["NAVIGATION_ODOM_V1_SOURCE"],
                "entries": entries,
            }
        ),
        encoding="utf-8",
    )
    return path


def test_catalog_discovers_all_manifest_attested_direct_profiles_for_each_purpose(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    entries = [
        *_write_profile(source, "20260320"),
        *_write_profile(source, "20260409_U", stage="fix_calibration"),
        *_write_profile(source, "future_device"),
    ]
    manifest = _write_manifest(tmp_path / "manifest.json", entries)

    catalog = CalibrationCatalog(manifest, source_root=source)
    processing = catalog.list_profiles(purpose="processing")
    fix = catalog.list_profiles(purpose="fix")

    assert [item["profile_ref"] for item in processing] == [
        "20260320",
        "20260409_U",
        "future_device",
    ]
    assert fix == processing
    processing_profile = catalog.get(
        "20260409_U",
        processing[1]["content_sha256"],
        purpose="processing",
    )
    fix_profile = catalog.get(
        "20260409_U",
        fix[1]["content_sha256"],
        purpose="fix",
    )
    assert processing_profile is not fix_profile

    processing_destination = tmp_path / "processing-snapshot"
    fix_destination = tmp_path / "fix-snapshot"
    processing_files, processing_sha = catalog.snapshot(
        processing_profile,
        processing_destination,
    )
    fix_files, fix_sha = catalog.snapshot(
        fix_profile,
        fix_destination,
    )
    assert processing_files == fix_files
    assert processing_sha == fix_sha
    assert processing_destination != fix_destination


def test_catalog_rejects_profile_missing_required_sensor_file(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    contents = {"fisheye_front.json": REQUIRED_CONTENT["fisheye_front.json"]}
    entries = _write_profile(source, "incomplete", contents=contents)
    manifest = _write_manifest(tmp_path / "manifest.json", entries)

    with pytest.raises(RuntimeError, match="missing required sensor files"):
        CalibrationCatalog(manifest, source_root=source)


def test_catalog_rejects_unattested_profile_directory(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    entries = _write_profile(source, "attested")
    _write_profile(source, "not_attested")
    manifest = _write_manifest(tmp_path / "manifest.json", entries)

    with pytest.raises(RuntimeError, match="differs from the frozen manifest"):
        CalibrationCatalog(manifest, source_root=source)


@pytest.mark.parametrize(
    "unsafe_kind",
    ("source", "profile", "sensors", "file"),
)
def test_catalog_rejects_symlinked_calibration_paths(
    tmp_path: Path,
    unsafe_kind: str,
) -> None:
    real_source = tmp_path / "real-source"
    entries = _write_profile(real_source, "selected")
    manifest = _write_manifest(tmp_path / "manifest.json", entries)
    source = real_source

    if unsafe_kind == "source":
        source = tmp_path / "source-link"
        source.symlink_to(real_source, target_is_directory=True)
    elif unsafe_kind == "profile":
        profile = real_source / "NoobScenes" / "params" / "selected"
        target = tmp_path / "profile-target"
        profile.rename(target)
        profile.symlink_to(target, target_is_directory=True)
    elif unsafe_kind == "sensors":
        sensors = (
            real_source
            / "NoobScenes"
            / "params"
            / "selected"
            / "sensors"
        )
        target = tmp_path / "sensors-target"
        sensors.rename(target)
        sensors.symlink_to(target, target_is_directory=True)
    else:
        sensor_file = (
            real_source
            / "NoobScenes"
            / "params"
            / "selected"
            / "sensors"
            / "fisheye_front.json"
        )
        target = tmp_path / "sensor-target.json"
        sensor_file.rename(target)
        sensor_file.symlink_to(target)

    with pytest.raises(RuntimeError, match="unsafe"):
        CalibrationCatalog(manifest, source_root=source)


def test_catalog_rejects_special_file_in_profile(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    entries = _write_profile(source, "selected")
    manifest = _write_manifest(tmp_path / "manifest.json", entries)
    special = (
        source
        / "NoobScenes"
        / "params"
        / "selected"
        / "sensors"
        / "runtime.pipe"
    )
    os.mkfifo(special)

    with pytest.raises(RuntimeError, match="file is unsafe"):
        CalibrationCatalog(manifest, source_root=source)


def test_catalog_rejects_invalid_profile_directory_name(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    entries = _write_profile(source, "selected")
    _write_profile(source, "not.valid")
    manifest = _write_manifest(tmp_path / "manifest.json", entries)

    with pytest.raises(RuntimeError, match="profile name is invalid"):
        CalibrationCatalog(manifest, source_root=source)


def test_catalog_rejects_malformed_selectable_manifest_entry(
    tmp_path: Path,
) -> None:
    content = b"{}\n"
    malformed = _entry(
        "selected",
        "fisheye_front.json",
        content,
        stage="calibration",
    )
    malformed["relative_path"] = "unexpected/selected/fisheye_front.json"
    manifest = _write_manifest(tmp_path / "manifest.json", [malformed])

    with pytest.raises(RuntimeError, match="manifest path is invalid"):
        CalibrationCatalog(manifest)


def test_catalog_rejects_mixed_manifest_stages_within_one_profile(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    entries = _write_profile(source, "mixed")
    entries[1]["stage"] = "fix_calibration"
    manifest = _write_manifest(tmp_path / "manifest.json", entries)

    with pytest.raises(RuntimeError, match="stage is inconsistent"):
        CalibrationCatalog(manifest, source_root=source)


def test_catalog_rejects_file_hash_or_size_drift_at_inventory_load(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    entries = _write_profile(source, "selected")
    manifest = _write_manifest(tmp_path / "manifest.json", entries)
    changed = (
        source
        / "NoobScenes"
        / "params"
        / "selected"
        / "sensors"
        / "fisheye_front.json"
    )
    changed.write_bytes(b'{"camera":"changed"}\n')

    with pytest.raises(RuntimeError, match="differs from the frozen manifest"):
        CalibrationCatalog(manifest, source_root=source)

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
import os
from pathlib import Path
import shutil
import stat
import struct
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

import vla_data_juicer_agents.annotation.runtime as runtime_module
from vla_data_juicer_agents.annotation.runtime import (
    CalibrationSnapshotFile,
    CheckpointVerificationRequest,
    EXPECTED_XVFB_VERSION,
    NavigationAnnotationRuntimeAdapter,
    NavigationAnnotationRuntimeConfig,
    NavigationAnnotationRuntimeDriver,
    NavigationTrackingRuntime,
    PreparationRequest,
    RuntimeExecutionError,
    TrackingInputValidationRequest,
    TrackingRequest,
    TrackingTarget,
    tracking_target_sort_key,
)
from vla_data_juicer_agents.navigation.subprocess_runner import run_command
from vla_data_juicer_agents.navigation.writer_lock import (
    clear_navigation_writer_quarantine,
    ensure_navigation_writer_quarantine,
    navigation_writer_lock,
    quarantine_active_writer,
)


def _png(width: int = 1920, height: int = 1536) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x02\x00\x00\x00"
    )


def _executable(path: Path, content: bytes = b"#!/bin/sh\nexit 0\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(0o755)


def _config(tmp_path: Path) -> NavigationAnnotationRuntimeConfig:
    source = tmp_path / "runtime-source"
    work = tmp_path / "work"
    clip = tmp_path / "datasets" / "clip_data"
    binary_data = tmp_path / "embedded-binary-root" / "Data"
    legacy_data = runtime_module._legacy_yaml_data_root()
    for directory in (
        source / "NoobScenes" / "samples",
        source / "NoobScenes" / "v1.0-develop",
        source / "Data" / "3_param",
        source / "0_1th_box",
        source / "1_onnx_tam",
        work,
        clip,
        binary_data,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    work.chmod(0o700)
    (source / "NoobScenes" / "maps").mkdir()
    (source / "NoobScenes" / "maps" / "map.png").write_bytes(_png(1, 1))
    script_contents = {
        "include/0_creat_box.py": b"# frozen\n",
        "include/1_odom_convert.py": (
            f"clip_data = {str(clip)!r}\n".encode("utf-8")
        ),
        "include/2_resize.py": b"# frozen\n",
        "main_smart_odom.py": b"# frozen\n",
        **{
            f"include/{filename}": b"# frozen\n"
            for filename in (
                "__init__.py",
                "attribute.py",
                "calibrated_sensor.py",
                "category.py",
                "dataset.py",
                "ego_pose.py",
                "instance.py",
                "lidarseg.py",
                "log.py",
                "map.py",
                "sample.py",
                "sample_annotation.py",
                "sample_data.py",
                "scene.py",
                "sensor.py",
                "utils.py",
                "visibility.py",
            )
        },
    }
    for filename, content in script_contents.items():
        path = source / "NoobScenes" / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    (source / "NoobScenes" / "include" / "1_odom_convert.py").chmod(0o755)
    (source / "0_1th_box" / "img2video.py").write_text(
        "# frozen\n",
        encoding="utf-8",
    )
    for filename in ("ost.yaml", "camera_extrinsics.yaml"):
        (source / "Data" / "3_param" / filename).write_text(
            "{}\n",
            encoding="utf-8",
        )
    tracking_binary = source / "1_onnx_tam" / "bin" / "main"
    _executable(
        tracking_binary,
        b"\x00".join(
            (
                b"fixture-binary",
                str(binary_data / "3_param" / "dog.yaml").encode("ascii"),
                str(
                    binary_data / "1_img_output" / "img_points.txt",
                ).encode("ascii"),
                (
                    str(binary_data / "1_img_output" / "tracking_img") + "/"
                ).encode("ascii"),
                b"",
            ),
        ),
    )
    model_contents = {
        f"1_onnx_tam/models/etam/{name}.onnx": (
            f"frozen-{name}\n".encode("ascii")
        )
        for name in (
            "image_encoder",
            "memory_attention",
            "image_decoder",
            "memory_encoder",
        )
    }
    for relative_path, content in model_contents.items():
        path = source / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    data_python = tmp_path / "python3.8"
    setup = tmp_path / "setup.sh"
    bwrap = tmp_path / "bwrap"
    xvfb = tmp_path / "xvfb-run"
    xvfb_server = tmp_path / "Xvfb"
    xvfb_deb = tmp_path / "xvfb.deb"
    dependency_summary = tmp_path / "runtime-dependencies.json"
    writer_lock_parent = tmp_path / "writer-lock"
    writer_lock_parent.mkdir(mode=0o700)
    _executable(data_python)
    _executable(setup)
    _executable(bwrap)
    _executable(xvfb)
    _executable(xvfb_server)
    xvfb_deb.write_bytes(b"frozen-xvfb-deb")
    system_package_versions = {
        "bubblewrap": "0.4.0-1ubuntu4.1",
        "xvfb:amd64": EXPECTED_XVFB_VERSION,
    }
    dependency_summary.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "runtime_id": "navigation_odom_v1",
                "platform": {
                    "architecture": "amd64",
                    "distribution": "Ubuntu",
                    "release": "20.04",
                },
                "packages": [
                    {
                        "name": name,
                        "version": version,
                        "architecture": "amd64",
                    }
                    for name, version in system_package_versions.items()
                ],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "runtime_id": "navigation_odom_v1",
                "root_aliases": [
                    "NAVIGATION_ODOM_V1_SOURCE",
                    "DATA_RUNTIME_ENV",
                    "SYSTEM_RUNTIME",
                ],
                "entries": [
                    {
                        "root_alias": "NAVIGATION_ODOM_V1_SOURCE",
                        "relative_path": "NoobScenes/main_smart_odom.py",
                        "kind": "frozen_file",
                        "role": "active_runtime",
                        "stage": "metadata",
                        "sha256": hashlib.sha256(b"# frozen\n").hexdigest(),
                        "size": len(b"# frozen\n"),
                        "executable": False,
                    },
                    {
                        "root_alias": "NAVIGATION_ODOM_V1_SOURCE",
                        "relative_path": "NoobScenes/include/1_odom_convert.py",
                        "kind": "frozen_file",
                        "role": "active_runtime",
                        "stage": "preprocess",
                        "sha256": hashlib.sha256(
                            script_contents["include/1_odom_convert.py"],
                        ).hexdigest(),
                        "size": len(
                            script_contents["include/1_odom_convert.py"],
                        ),
                        "executable": True,
                    },
                    *[
                        {
                            "root_alias": "NAVIGATION_ODOM_V1_SOURCE",
                            "relative_path": f"NoobScenes/{filename}",
                            "kind": "frozen_file",
                            "role": "active_runtime",
                            "stage": (
                                "preprocess"
                                if filename
                                in {
                                    "include/0_creat_box.py",
                                    "include/2_resize.py",
                                }
                                else "metadata"
                            ),
                            "sha256": hashlib.sha256(content).hexdigest(),
                            "size": len(content),
                            "executable": False,
                        }
                        for filename, content in script_contents.items()
                        if filename
                        not in {
                            "include/1_odom_convert.py",
                            "main_smart_odom.py",
                        }
                    ],
                    *[
                        {
                            "root_alias": "NAVIGATION_ODOM_V1_SOURCE",
                            "relative_path": relative_path,
                            "kind": "frozen_file",
                            "role": role,
                            "stage": stage,
                            "sha256": hashlib.sha256(
                                (source / relative_path).read_bytes(),
                            ).hexdigest(),
                            "size": (source / relative_path).stat().st_size,
                            "executable": False,
                        }
                        for relative_path, role, stage in (
                            (
                                "NoobScenes/maps/map.png",
                                "active_static_asset",
                                "metadata",
                            ),
                            (
                                "0_1th_box/img2video.py",
                                "active_runtime",
                                "initial_annotation",
                            ),
                        )
                    ],
                    {
                        "root_alias": "NAVIGATION_ODOM_V1_SOURCE",
                        "relative_path": "1_onnx_tam/bin/main",
                        "kind": "frozen_file",
                        "role": "active_binary",
                        "stage": "tracking",
                        "sha256": hashlib.sha256(
                            tracking_binary.read_bytes(),
                        ).hexdigest(),
                        "size": tracking_binary.stat().st_size,
                        "executable": True,
                    },
                    *[
                        {
                            "root_alias": "NAVIGATION_ODOM_V1_SOURCE",
                            "relative_path": relative_path,
                            "kind": "frozen_file",
                            "role": "active_model",
                            "stage": "tracking",
                            "sha256": hashlib.sha256(content).hexdigest(),
                            "size": len(content),
                            "executable": False,
                        }
                        for relative_path, content in model_contents.items()
                    ],
                    *[
                        {
                            "root_alias": "NAVIGATION_ODOM_V1_SOURCE",
                            "relative_path": f"Data/3_param/{filename}",
                            "kind": "frozen_file",
                            "role": "tracking_compatibility_config",
                            "stage": "tracking",
                            "sha256": hashlib.sha256(
                                (
                                    source
                                    / "Data"
                                    / "3_param"
                                    / filename
                                ).read_bytes(),
                            ).hexdigest(),
                            "size": (
                                source
                                / "Data"
                                / "3_param"
                                / filename
                            ).stat().st_size,
                            "executable": False,
                        }
                        for filename in (
                            "ost.yaml",
                            "camera_extrinsics.yaml",
                        )
                    ],
                    {
                        "root_alias": "DATA_RUNTIME_ENV",
                        "relative_path": "setup.sh",
                        "kind": "external_runtime",
                        "role": "active_environment_setup",
                        "stage": "all",
                        "sha256": hashlib.sha256(setup.read_bytes()).hexdigest(),
                        "size": setup.stat().st_size,
                        "executable": True,
                    },
                    {
                        "root_alias": "SYSTEM_RUNTIME",
                        "relative_path": "python3.8",
                        "kind": "external_runtime",
                        "role": "active_python_interpreter",
                        "stage": "all",
                        "version": "3.8.10",
                        "sha256": hashlib.sha256(
                            data_python.read_bytes(),
                        ).hexdigest(),
                        "size": data_python.stat().st_size,
                        "executable": True,
                    },
                    {
                        "root_alias": "SYSTEM_RUNTIME",
                        "relative_path": "packages/xvfb.deb",
                        "kind": "external_runtime",
                        "role": "xvfb_deb_package",
                        "stage": "all",
                        "version": EXPECTED_XVFB_VERSION,
                        "sha256": hashlib.sha256(
                            xvfb_deb.read_bytes(),
                        ).hexdigest(),
                        "size": xvfb_deb.stat().st_size,
                        "executable": False,
                    },
                    {
                        "root_alias": "SYSTEM_RUNTIME",
                        "relative_path": "usr/bin/Xvfb",
                        "kind": "external_runtime",
                        "role": "xvfb_server_binary",
                        "stage": "all",
                        "sha256": hashlib.sha256(
                            xvfb_server.read_bytes(),
                        ).hexdigest(),
                        "size": xvfb_server.stat().st_size,
                        "executable": True,
                    },
                    {
                        "root_alias": "SYSTEM_RUNTIME",
                        "relative_path": "usr/bin/xvfb-run",
                        "kind": "external_runtime",
                        "role": "xvfb_launcher",
                        "stage": "all",
                        "sha256": hashlib.sha256(
                            xvfb.read_bytes(),
                        ).hexdigest(),
                        "size": xvfb.stat().st_size,
                        "executable": True,
                    },
                    {
                        "root_alias": "SYSTEM_RUNTIME",
                        "relative_path": "usr/bin/bwrap",
                        "kind": "external_runtime",
                        "role": "sandbox_binary",
                        "stage": "all",
                        "sha256": hashlib.sha256(
                            bwrap.read_bytes(),
                        ).hexdigest(),
                        "size": bwrap.stat().st_size,
                        "executable": True,
                    },
                    {
                        "root_alias": "SYSTEM_RUNTIME",
                        "relative_path": "install/runtime-dependencies.json",
                        "kind": "external_runtime",
                        "role": "runtime_dependency_summary",
                        "stage": "all",
                        "sha256": hashlib.sha256(
                            dependency_summary.read_bytes(),
                        ).hexdigest(),
                        "size": dependency_summary.stat().st_size,
                        "executable": False,
                    },
                ],
            },
        ),
        encoding="utf-8",
    )
    return NavigationAnnotationRuntimeConfig(
        runtime_source_root=source,
        work_root=work,
        clip_data_root=clip,
        data_python=data_python,
        data_env_setup=setup,
        manifest_path=manifest,
        bwrap_path=bwrap,
        xvfb_run_path=xvfb,
        xvfb_path=xvfb_server,
        xvfb_deb_path=xvfb_deb,
        runtime_dependency_summary_path=dependency_summary,
        tracking_binary_data_root=binary_data,
        legacy_tracking_data_root=legacy_data,
        legacy_clip_data_root=clip,
        writer_lock_path=writer_lock_parent / "navigation.lock",
        minimum_free_bytes=1,
        timeout_seconds=300,
        version_probe=lambda package: (
            EXPECTED_XVFB_VERSION
            if package == "xvfb"
            else system_package_versions.get(package, "")
        ),
        package_probe=lambda _names: {},
        gpu_probe=lambda: True,
        overlay_target_probe=lambda path: (
            path == runtime_module._LEGACY_YAML_SANDBOX_ROOT
            or runtime_module._safe_absolute_directory(path)
        ),
    )


def _refresh_frozen_manifest_entry(
    config: NavigationAnnotationRuntimeConfig,
    relative_path: str,
) -> None:
    assert config.runtime_source_root is not None
    path = config.runtime_source_root / relative_path
    manifest = json.loads(config.manifest_path.read_text(encoding="utf-8"))
    entry = next(
        item
        for item in manifest["entries"]
        if item.get("root_alias") == "NAVIGATION_ODOM_V1_SOURCE"
        and item.get("relative_path") == relative_path
    )
    entry.update(
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        size=path.stat().st_size,
        executable=bool(path.stat().st_mode & 0o111),
    )
    config.manifest_path.write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )


def _write_dependency_summary(
    config: NavigationAnnotationRuntimeConfig,
    payload: object,
) -> None:
    assert config.runtime_dependency_summary_path is not None
    content = payload if isinstance(payload, str) else json.dumps(payload)
    config.runtime_dependency_summary_path.write_text(
        content + ("\n" if not content.endswith("\n") else ""),
        encoding="utf-8",
    )
    manifest = json.loads(config.manifest_path.read_text(encoding="utf-8"))
    entry = next(
        item
        for item in manifest["entries"]
        if item.get("role") == "runtime_dependency_summary"
    )
    content_bytes = config.runtime_dependency_summary_path.read_bytes()
    entry.update(
        sha256=hashlib.sha256(content_bytes).hexdigest(),
        size=len(content_bytes),
        executable=False,
    )
    config.manifest_path.write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )


def _tracking_target(
    segment_root: Path,
    yaml_path: Path,
    identity: str,
) -> TrackingTarget:
    content = yaml_path.read_bytes() if yaml_path.is_file() else b"{}\n"
    return TrackingTarget(
        segment_root=segment_root,
        yaml_path=yaml_path,
        identity=identity,
        expected_yaml_sha256=hashlib.sha256(content).hexdigest(),
    )


def _sync_input(config: NavigationAnnotationRuntimeConfig, *, with_odom: bool = True) -> Path:
    assert config.clip_data_root is not None
    sync_root = (
        config.clip_data_root
        / "20270605"
        / "20260605_160904"
        / "sync_data"
    )
    sequence = sync_root / "20260605_160904_zhigu_wuhan_0"
    for modality in ("fisheye_front", "r32_rslidar_points"):
        (sequence / modality).mkdir(parents=True)
    (sequence / "fisheye_front" / "000001.png").write_bytes(_png())
    (sequence / "r32_rslidar_points" / "000001.pcd").write_bytes(b"pcd")
    (sync_root / "times.json").write_text("{}\n", encoding="utf-8")
    if with_odom:
        (sequence / "odom").mkdir()
        (sequence / "odom" / "000001.json").write_text(
            "{}",
            encoding="utf-8",
        )
    return sequence


def _calibration_fields(
    config: NavigationAnnotationRuntimeConfig,
    job_ref: str,
) -> dict[str, object]:
    assert config.work_root is not None
    root = config.work_root / "jobs" / job_ref / "calibration"
    root.mkdir(parents=True)
    (root / "fisheye_front.json").write_text("{}", encoding="utf-8")
    (root / "r32_rslidar_points.json").write_text("{}", encoding="utf-8")
    files = tuple(
        CalibrationSnapshotFile(
            relative_path=path.name,
            size=path.stat().st_size,
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in sorted(root.iterdir(), key=lambda item: item.name)
    )
    aggregate = hashlib.sha256(
        json.dumps(
            [
                {
                    "relative_path": item.relative_path,
                    "sha256": item.sha256,
                    "size": item.size,
                }
                for item in files
            ],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8"),
    ).hexdigest()
    return {
        "calibration_snapshot_dir": root,
        "calibration_snapshot_files": files,
        "calibration_snapshot_sha256": aggregate,
    }


def test_runtime_capabilities_fail_closed_and_verify_frozen_payload(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    adapter = NavigationAnnotationRuntimeAdapter(config)
    assert adapter.capabilities().available is True

    changed = config.runtime_source_root / "NoobScenes" / "main_smart_odom.py"  # type: ignore[operator]
    changed.write_text("# changed\n", encoding="utf-8")
    capability = adapter.capabilities()
    assert capability.available is False
    assert capability.reason is not None
    assert capability.reason.code == "runtime_payload_mismatch"

    missing = NavigationAnnotationRuntimeConfig(
        runtime_source_root=None,
        work_root=None,
        clip_data_root=None,
        data_python=None,
        data_env_setup=None,
        manifest_path=config.manifest_path,
    )
    unavailable = NavigationAnnotationRuntimeAdapter(missing).capabilities()
    assert unavailable.available is False
    assert unavailable.reason is not None
    assert unavailable.reason.code == "runtime_not_configured"


@pytest.mark.parametrize(
    "relative_path",
    [
        "NoobScenes/include/0_creat_box.py",
        "NoobScenes/include/attribute.py",
        "NoobScenes/maps/map.png",
        "0_1th_box/img2video.py",
        "1_onnx_tam/bin/main",
        "1_onnx_tam/models/etam/image_encoder.onnx",
        "Data/3_param/ost.yaml",
    ],
)
def test_runtime_capabilities_require_every_active_frozen_input(
    tmp_path: Path,
    relative_path: str,
) -> None:
    config = _config(tmp_path)
    manifest = json.loads(config.manifest_path.read_text(encoding="utf-8"))
    manifest["entries"] = [
        entry
        for entry in manifest["entries"]
        if entry.get("relative_path") != relative_path
    ]
    config.manifest_path.write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )

    capability = NavigationAnnotationRuntimeAdapter(config).capabilities()

    assert capability.available is False
    assert capability.reason is not None
    assert capability.reason.code == "runtime_manifest_incomplete"


@pytest.mark.parametrize(
    ("relative_path", "field", "wrong_value"),
    [
        ("Data/3_param/ost.yaml", "role", "active_runtime"),
        (
            "Data/3_param/camera_extrinsics.yaml",
            "stage",
            "preprocess",
        ),
        ("1_onnx_tam/bin/main", "role", "tracking_compatibility_config"),
        (
            "NoobScenes/include/1_odom_convert.py",
            "stage",
            "tracking",
        ),
        (
            "NoobScenes/include/attribute.py",
            "role",
            "active_static_asset",
        ),
        (
            "NoobScenes/maps/map.png",
            "stage",
            "preprocess",
        ),
        (
            "0_1th_box/img2video.py",
            "stage",
            "metadata",
        ),
        (
            "1_onnx_tam/models/etam/image_encoder.onnx",
            "role",
            "active_runtime",
        ),
    ],
)
def test_runtime_capabilities_require_exact_frozen_entry_metadata(
    tmp_path: Path,
    relative_path: str,
    field: str,
    wrong_value: str,
) -> None:
    config = _config(tmp_path)
    manifest = json.loads(config.manifest_path.read_text(encoding="utf-8"))
    entry = next(
        item
        for item in manifest["entries"]
        if item.get("relative_path") == relative_path
    )
    entry[field] = wrong_value
    config.manifest_path.write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )

    capability = NavigationAnnotationRuntimeAdapter(config).capabilities()

    assert capability.available is False
    assert capability.reason is not None
    assert capability.reason.code == "runtime_manifest_incomplete"


@pytest.mark.parametrize("timeout_seconds", [None, 0, -1, True])
def test_runtime_capabilities_require_positive_command_timeout(
    tmp_path: Path,
    timeout_seconds: object,
) -> None:
    config = replace(
        _config(tmp_path),
        timeout_seconds=timeout_seconds,  # type: ignore[arg-type]
    )

    capability = NavigationAnnotationRuntimeAdapter(config).capabilities()

    assert capability.available is False
    assert capability.reason is not None
    assert capability.reason.code == "runtime_timeout_not_configured"


@pytest.mark.parametrize("minimum_free_bytes", [None, 0, -1, True])
def test_runtime_capabilities_require_positive_capacity_margin(
    tmp_path: Path,
    minimum_free_bytes: object,
) -> None:
    config = replace(
        _config(tmp_path),
        minimum_free_bytes=minimum_free_bytes,  # type: ignore[arg-type]
    )

    capability = NavigationAnnotationRuntimeAdapter(config).capabilities()

    assert capability.available is False
    assert capability.reason is not None
    assert capability.reason.code == "runtime_capacity_margin_invalid"


def test_runtime_capabilities_require_distinct_absolute_tracking_targets(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)

    missing = NavigationAnnotationRuntimeAdapter(
        replace(config, tracking_binary_data_root=None),
    ).capabilities()
    assert missing.available is False
    assert missing.reason is not None
    assert missing.reason.code == "runtime_not_configured"

    relative = NavigationAnnotationRuntimeAdapter(
        replace(
            config,
            tracking_binary_data_root=Path("binary-data"),
        ),
    ).capabilities()
    assert relative.available is False
    assert relative.reason is not None
    assert relative.reason.code == "sandbox_target_unavailable"

    conflict = NavigationAnnotationRuntimeAdapter(
        replace(
            config,
            tracking_binary_data_root=config.legacy_tracking_data_root,
        ),
    ).capabilities()
    assert conflict.available is False
    assert conflict.reason is not None
    assert conflict.reason.code == "sandbox_target_conflict"


def test_runtime_capabilities_keep_legacy_yaml_target_sandbox_only(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    assert config.legacy_tracking_data_root is not None
    probed: list[Path] = []

    def probe(path: Path) -> bool:
        probed.append(path)
        return (
            path == runtime_module._LEGACY_YAML_SANDBOX_ROOT
            or runtime_module._safe_absolute_directory(path)
        )

    capability = NavigationAnnotationRuntimeAdapter(
        replace(config, overlay_target_probe=probe),
    ).capabilities()

    assert capability.available is True
    assert runtime_module._LEGACY_YAML_SANDBOX_ROOT in probed
    assert config.tracking_binary_data_root in probed
    assert config.legacy_tracking_data_root not in probed


def test_runtime_capabilities_still_require_binary_target_on_host(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    assert config.tracking_binary_data_root is not None
    config.tracking_binary_data_root.rmdir()

    capability = NavigationAnnotationRuntimeAdapter(config).capabilities()

    assert capability.available is False
    assert capability.reason is not None
    assert capability.reason.code == "sandbox_target_unavailable"


def test_runtime_capabilities_require_safe_legacy_sandbox_root(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    assert config.legacy_tracking_data_root is not None
    probed: list[Path] = []

    def probe(path: Path) -> bool:
        probed.append(path)
        return (
            path != runtime_module._LEGACY_YAML_SANDBOX_ROOT
            and runtime_module._safe_absolute_directory(path)
        )

    capability = NavigationAnnotationRuntimeAdapter(
        replace(config, overlay_target_probe=probe),
    ).capabilities()

    assert capability.available is False
    assert capability.reason is not None
    assert capability.reason.code == "sandbox_target_unavailable"
    assert runtime_module._LEGACY_YAML_SANDBOX_ROOT in probed
    assert config.legacy_tracking_data_root not in probed


def test_runtime_capabilities_reject_nested_and_protected_tracking_targets(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    parent = tmp_path / "tracking-target"
    child = parent / "nested"
    child.mkdir(parents=True)

    nested = NavigationAnnotationRuntimeAdapter(
        replace(
            config,
            tracking_binary_data_root=parent,
            legacy_tracking_data_root=child,
        ),
    ).capabilities()
    assert nested.available is False
    assert nested.reason is not None
    assert nested.reason.code == "sandbox_target_conflict"

    assert config.work_root is not None
    protected_target = config.work_root / "tracking-target"
    protected_target.mkdir()
    protected = NavigationAnnotationRuntimeAdapter(
        replace(
            config,
            tracking_binary_data_root=protected_target,
        ),
    ).capabilities()
    assert protected.available is False
    assert protected.reason is not None
    assert protected.reason.code == "sandbox_target_conflict"


@pytest.mark.parametrize(
    "protected_root",
    ["dataset", "clip", "runtime_source"],
)
def test_runtime_capabilities_reject_work_root_overlap(
    tmp_path: Path,
    protected_root: str,
) -> None:
    config = _config(tmp_path)
    assert config.clip_data_root is not None
    assert config.runtime_source_root is not None
    selected = {
        "dataset": config.clip_data_root.parent,
        "clip": config.clip_data_root,
        "runtime_source": config.runtime_source_root,
    }[protected_root]
    selected.chmod(0o700)

    capability = NavigationAnnotationRuntimeAdapter(
        replace(config, work_root=selected),
    ).capabilities()

    assert capability.available is False
    assert capability.reason is not None
    assert capability.reason.code == "work_root_conflict"


def test_runtime_capabilities_accept_private_work_root(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    assert config.work_root is not None
    config.work_root.chmod(0o700)

    capability = NavigationAnnotationRuntimeAdapter(config).capabilities()

    assert capability.available is True


def test_private_tree_hardening_does_not_use_path_chmod_no_follow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_root = tmp_path / "private"
    private_root.mkdir(mode=0o755)
    payload = private_root / "payload.json"
    payload.write_text("{}\n", encoding="utf-8")
    os.chmod(payload, 0o644)

    def unsupported_path_chmod(*_args, **_kwargs) -> None:
        raise NotImplementedError(
            "chmod: follow_symlinks unavailable on this platform",
        )

    monkeypatch.setattr(Path, "chmod", unsupported_path_chmod)

    runtime_module._harden_private_tree(private_root)

    assert stat.S_IMODE(private_root.stat().st_mode) == 0o700
    assert stat.S_IMODE(payload.stat().st_mode) == 0o600


def test_private_mode_rejects_symlink_without_changing_target(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.write_text("outside\n", encoding="utf-8")
    os.chmod(outside, 0o640)
    linked = tmp_path / "linked"
    linked.symlink_to(outside)

    with pytest.raises(RuntimeExecutionError) as error:
        runtime_module._set_private_mode(
            linked,
            0o600,
            expect_directory=False,
            error_code="unsafe_runtime_output",
            error_message="unsafe test entry",
        )

    assert error.value.code == "unsafe_runtime_output"
    assert stat.S_IMODE(outside.stat().st_mode) == 0o640


def test_private_mode_detects_inode_replacement_and_closes_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "payload"
    target.write_text("original\n", encoding="utf-8")
    os.chmod(target, 0o640)
    displaced = tmp_path / "displaced"
    original_fchmod = os.fchmod
    descriptors: list[int] = []

    def replace_after_fchmod(descriptor: int, mode: int) -> None:
        descriptors.append(descriptor)
        original_fchmod(descriptor, mode)
        target.rename(displaced)
        target.write_text("replacement\n", encoding="utf-8")

    monkeypatch.setattr(runtime_module.os, "fchmod", replace_after_fchmod)

    with pytest.raises(RuntimeExecutionError) as error:
        runtime_module._set_private_mode(
            target,
            0o600,
            expect_directory=False,
            error_code="unsafe_runtime_output",
            error_message="unsafe test entry",
        )

    assert error.value.code == "unsafe_runtime_output"
    assert descriptors
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_private_mode_requires_no_follow_platform_support(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "payload"
    target.write_text("payload\n", encoding="utf-8")
    monkeypatch.delattr(runtime_module.os, "O_NOFOLLOW")

    with pytest.raises(RuntimeExecutionError) as error:
        runtime_module._set_private_mode(
            target,
            0o600,
            expect_directory=False,
            error_code="unsafe_runtime_path",
            error_message="unsafe test entry",
        )

    assert error.value.code == "unsafe_runtime_path"


def test_private_file_mode_open_is_nonblocking_and_no_follow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "payload"
    target.write_text("payload\n", encoding="utf-8")
    original_open = os.open
    observed_flags: list[int] = []

    def record_open(path, flags, *args, **kwargs):
        observed_flags.append(flags)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(runtime_module.os, "open", record_open)

    runtime_module._set_private_mode(
        target,
        0o600,
        expect_directory=False,
        error_code="unsafe_runtime_path",
        error_message="unsafe test entry",
    )

    assert len(observed_flags) == 1
    assert observed_flags[0] & os.O_NOFOLLOW
    assert observed_flags[0] & os.O_NONBLOCK
    assert observed_flags[0] & os.O_CLOEXEC


def test_manifest_publish_race_still_hardens_matching_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "manifest-bound.json"
    content = b'{"runtime":"frozen"}\n'
    entry = {
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }

    def race_with_matching_file(path: Path, payload: bytes) -> None:
        path.write_bytes(payload)
        os.chmod(path, 0o666)
        raise FileExistsError(path)

    monkeypatch.setattr(
        runtime_module,
        "_write_bytes_exclusive",
        race_with_matching_file,
    )

    runtime_module._write_or_reuse_manifest_file(
        destination,
        content,
        entry,
    )

    assert stat.S_IMODE(destination.stat().st_mode) == 0o600


@pytest.mark.parametrize("mode", [0o755, 0o775, 0o777])
def test_runtime_capabilities_reject_nonprivate_work_root_mode(
    tmp_path: Path,
    mode: int,
) -> None:
    config = _config(tmp_path)
    assert config.work_root is not None
    config.work_root.chmod(mode)

    capability = NavigationAnnotationRuntimeAdapter(config).capabilities()

    assert capability.available is False
    assert capability.reason is not None
    assert capability.reason.code == "work_root_unavailable"


def test_runtime_capabilities_reject_noncanonical_work_root(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    assert config.work_root is not None
    noncanonical = (
        config.work_root
        / ".."
        / config.work_root.name
    )

    capability = NavigationAnnotationRuntimeAdapter(
        replace(config, work_root=noncanonical),
    ).capabilities()

    assert capability.available is False
    assert capability.reason is not None
    assert capability.reason.code == "work_root_unavailable"


def test_runtime_capabilities_reject_work_root_with_symlink_ancestor(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    real_parent = tmp_path / "real-work-parent"
    real_work_root = real_parent / "work"
    real_work_root.mkdir(parents=True, mode=0o700)
    alias_parent = tmp_path / "work-parent-alias"
    alias_parent.symlink_to(real_parent, target_is_directory=True)

    capability = NavigationAnnotationRuntimeAdapter(
        replace(config, work_root=alias_parent / "work"),
    ).capabilities()

    assert capability.available is False
    assert capability.reason is not None
    assert capability.reason.code == "work_root_unavailable"


def test_runtime_capabilities_reject_wrong_owner_work_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    assert config.work_root is not None
    work_root = config.work_root
    real_lstat = Path.lstat

    def wrong_owner_lstat(path: Path) -> os.stat_result:
        metadata = real_lstat(path)
        if path != work_root:
            return metadata
        fields = list(metadata)
        fields[4] = metadata.st_uid + 1
        return os.stat_result(fields)

    monkeypatch.setattr(Path, "lstat", wrong_owner_lstat)

    capability = NavigationAnnotationRuntimeAdapter(config).capabilities()

    assert capability.available is False
    assert capability.reason is not None
    assert capability.reason.code == "work_root_unavailable"


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "source_root",
        "payload_directory",
        "overlay_directory",
        "active_file",
    ],
)
def test_runtime_capabilities_reject_peer_writable_active_payload(
    tmp_path: Path,
    unsafe_path: str,
) -> None:
    config = _config(tmp_path)
    assert config.runtime_source_root is not None
    path = {
        "source_root": config.runtime_source_root,
        "payload_directory": (
            config.runtime_source_root / "1_onnx_tam" / "models"
        ),
        "overlay_directory": (
            config.runtime_source_root / "NoobScenes" / "samples"
        ),
        "active_file": (
            config.runtime_source_root
            / "1_onnx_tam"
            / "models"
            / "etam"
            / "image_encoder.onnx"
        ),
    }[unsafe_path]
    path.chmod(
        0o775 if path.is_dir() else 0o664,
        follow_symlinks=False,
    )

    capability = NavigationAnnotationRuntimeAdapter(config).capabilities()

    assert capability.available is False
    assert capability.reason is not None
    assert capability.reason.code == "runtime_payload_permissions_unsafe"


def test_runtime_capabilities_bind_tracking_targets_to_frozen_evidence(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    arbitrary_binary_target = tmp_path / "arbitrary-binary" / "Data"
    arbitrary_yaml_target = tmp_path / "arbitrary-yaml" / "Data"
    arbitrary_clip_target = tmp_path / "arbitrary-clip"
    for path in (
        arbitrary_binary_target,
        arbitrary_yaml_target,
        arbitrary_clip_target,
    ):
        path.mkdir(parents=True)

    for changed in (
        replace(
            config,
            tracking_binary_data_root=arbitrary_binary_target,
        ),
        replace(
            config,
            legacy_tracking_data_root=arbitrary_yaml_target,
        ),
        replace(
            config,
            legacy_clip_data_root=arbitrary_clip_target,
        ),
    ):
        capability = NavigationAnnotationRuntimeAdapter(
            changed,
        ).capabilities()
        assert capability.available is False
        assert capability.reason is not None
        assert capability.reason.code == "sandbox_target_mismatch"


def test_runtime_capabilities_scan_manifest_frozen_embedded_paths(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    assert config.runtime_source_root is not None
    other_binary_data = tmp_path / "other-binary-root" / "Data"
    other_clip_data = tmp_path / "other-clip"
    other_binary_data.mkdir(parents=True)
    other_clip_data.mkdir()

    binary = config.runtime_source_root / "1_onnx_tam" / "bin" / "main"
    binary.write_bytes(
        b"\x00".join(
            (
                str(
                    other_binary_data / "3_param" / "dog.yaml",
                ).encode("ascii"),
                str(
                    other_binary_data / "1_img_output" / "img_points.txt",
                ).encode("ascii"),
                (
                    str(
                        other_binary_data
                        / "1_img_output"
                        / "tracking_img",
                    )
                    + "/"
                ).encode("ascii"),
                b"",
            ),
        ),
    )
    binary.chmod(0o755)
    _refresh_frozen_manifest_entry(config, "1_onnx_tam/bin/main")

    binary_capability = NavigationAnnotationRuntimeAdapter(
        config,
    ).capabilities()
    assert binary_capability.available is False
    assert binary_capability.reason is not None
    assert binary_capability.reason.code == "sandbox_target_mismatch"

    odom_convert = (
        config.runtime_source_root
        / "NoobScenes"
        / "include"
        / "1_odom_convert.py"
    )
    odom_convert.write_text(
        f"clip_data = {str(other_clip_data)!r}\n",
        encoding="utf-8",
    )
    odom_convert.chmod(0o755)
    _refresh_frozen_manifest_entry(
        config,
        "NoobScenes/include/1_odom_convert.py",
    )

    clip_capability = NavigationAnnotationRuntimeAdapter(
        replace(config, tracking_binary_data_root=other_binary_data),
    ).capabilities()
    assert clip_capability.available is False
    assert clip_capability.reason is not None
    assert clip_capability.reason.code == "sandbox_target_mismatch"


def test_runtime_capabilities_require_explicit_safe_writer_lock(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)

    missing = NavigationAnnotationRuntimeAdapter(
        replace(config, writer_lock_path=None),
    ).capabilities()
    assert missing.available is False
    assert missing.reason is not None
    assert missing.reason.code == "writer_lock_not_configured"

    relative = NavigationAnnotationRuntimeAdapter(
        replace(config, writer_lock_path=Path("navigation.lock")),
    ).capabilities()
    assert relative.available is False
    assert relative.reason is not None
    assert relative.reason.code == "writer_lock_path_unsafe"

    shared_parent = tmp_path / "shared-lock"
    shared_parent.mkdir(mode=0o777)
    shared_parent.chmod(0o777)
    shared = NavigationAnnotationRuntimeAdapter(
        replace(
            config,
            writer_lock_path=shared_parent / "navigation.lock",
        ),
    ).capabilities()
    assert shared.available is False
    assert shared.reason is not None
    assert shared.reason.code == "writer_lock_path_unsafe"


def test_runtime_capabilities_fail_closed_for_durable_writer_quarantine(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    assert config.writer_lock_path is not None
    ensure_navigation_writer_quarantine(config.writer_lock_path)

    capability = NavigationAnnotationRuntimeAdapter(config).capabilities()

    assert capability.available is False
    assert capability.reason is not None
    assert capability.reason.code == "runtime_coordination_unavailable"
    assert clear_navigation_writer_quarantine(
        config.writer_lock_path,
        all_writer_process_groups_absent=True,
    )


def test_runtime_capabilities_distinguish_healthy_busy_and_stale_active(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    assert config.writer_lock_path is not None
    adapter = NavigationAnnotationRuntimeAdapter(config)
    entered = threading.Event()
    release = threading.Event()

    def healthy_writer() -> None:
        with navigation_writer_lock(lock_path=config.writer_lock_path):
            entered.set()
            release.wait(timeout=2)

    thread = threading.Thread(target=healthy_writer)
    thread.start()
    assert entered.wait(timeout=2)
    assert adapter.capabilities().available is True
    release.set()
    thread.join(timeout=2)

    with navigation_writer_lock(lock_path=config.writer_lock_path):
        quarantine_active_writer()
    stale = adapter.capabilities()
    assert stale.available is False
    assert stale.reason is not None
    assert stale.reason.code == "runtime_coordination_unavailable"
    assert clear_navigation_writer_quarantine(
        config.writer_lock_path,
        all_writer_process_groups_absent=True,
    )


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [
        (None, None),
        ("300", 300),
        ("0", None),
        ("-1", None),
        ("+1", None),
        (" 1", None),
        ("１", None),
        ("not-a-number", None),
    ],
)
def test_runtime_timeout_environment_is_strict_ascii_positive_integer(
    monkeypatch: pytest.MonkeyPatch,
    raw_value: str | None,
    expected: int | None,
) -> None:
    if raw_value is None:
        monkeypatch.delenv(
            "VLA_ANNOTATION_RUNTIME_TIMEOUT_SECONDS",
            raising=False,
        )
    else:
        monkeypatch.setenv(
            "VLA_ANNOTATION_RUNTIME_TIMEOUT_SECONDS",
            raw_value,
        )

    config = NavigationAnnotationRuntimeConfig.from_env()

    assert config.timeout_seconds == expected


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [
        (None, None),
        ("300", 300),
        ("0", None),
        ("-1", None),
        ("+1", None),
        (" 1", None),
        ("１", None),
        ("not-a-number", None),
    ],
)
def test_runtime_capacity_environment_is_strict_ascii_positive_integer(
    monkeypatch: pytest.MonkeyPatch,
    raw_value: str | None,
    expected: int | None,
) -> None:
    if raw_value is None:
        monkeypatch.delenv(
            "VLA_ANNOTATION_MINIMUM_FREE_BYTES",
            raising=False,
        )
    else:
        monkeypatch.setenv(
            "VLA_ANNOTATION_MINIMUM_FREE_BYTES",
            raw_value,
        )

    config = NavigationAnnotationRuntimeConfig.from_env()

    assert config.minimum_free_bytes == expected


def test_runtime_capacity_margin_is_asserted_before_all_arithmetic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = replace(_config(tmp_path), minimum_free_bytes=None)
    adapter = NavigationAnnotationRuntimeAdapter(config)
    tracking = NavigationTrackingRuntime(config)
    monkeypatch.setattr(adapter, "_require_available", lambda: None)
    monkeypatch.setattr(tracking, "_require_available", lambda: None)

    with pytest.raises(AssertionError):
        adapter.preflight_capacity("20270605", ("clip",))
    with pytest.raises(AssertionError):
        adapter.prepare(
            PreparationRequest(
                job_ref="job_" + "1" * 32,
                run_ref="run_" + "2" * 32,
                attempt=1,
                dataset_date="20270605",
                source_clips=("clip",),
                calibration_snapshot_dir=tmp_path,
                calibration_snapshot_files=(),
                calibration_snapshot_sha256="a" * 64,
            ),
        )
    with pytest.raises(AssertionError):
        tracking.track(
            TrackingRequest(
                job_ref="job_" + "1" * 32,
                run_ref="run_" + "2" * 32,
                attempt=1,
                staging_root=tmp_path,
                targets=(),
                attestation_targets=(),
                expected_runtime_manifest_sha256="a" * 64,
                expected_prepared_artifact_tree_sha256="b" * 64,
            ),
        )


def test_runtime_tracking_targets_are_explicit_environment_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VLA_TRACKING_BINARY_DATA_ROOT", raising=False)
    monkeypatch.delenv("VLA_TRACKING_LEGACY_DATA_ROOT", raising=False)
    missing = NavigationAnnotationRuntimeConfig.from_env()
    assert missing.tracking_binary_data_root is None
    assert missing.legacy_tracking_data_root is None

    binary_data = tmp_path / "binary-data"
    yaml_data = tmp_path / "yaml-data"
    monkeypatch.setenv(
        "VLA_TRACKING_BINARY_DATA_ROOT",
        str(binary_data),
    )
    monkeypatch.setenv(
        "VLA_TRACKING_LEGACY_DATA_ROOT",
        str(yaml_data),
    )
    configured = NavigationAnnotationRuntimeConfig.from_env()
    assert configured.tracking_binary_data_root == binary_data
    assert configured.legacy_tracking_data_root == yaml_data
    assert not binary_data.exists()
    assert not yaml_data.exists()


def test_runtime_command_uses_the_approved_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    adapter = NavigationAnnotationRuntimeAdapter(config)
    assert config.work_root is not None
    assert config.runtime_source_root is not None
    staging = (
        config.work_root
        / "jobs"
        / ("job_" + "a" * 32)
        / "attempts"
        / ("run_" + "b" * 32)
        / "20270605_temp"
    )
    staging.mkdir(parents=True)
    observed: dict[str, object] = {}

    def fake_run_command(command, **kwargs):
        observed["command"] = command
        observed.update(kwargs)
        return SimpleNamespace(
            return_code=0,
            stdout="",
            stderr="",
        )

    monkeypatch.setattr(runtime_module, "run_command", fake_run_command)

    adapter._run_checked(
        staging_root=staging,
        argv=["/bin/true"],
        cwd=config.runtime_source_root,
        error_code="test_command_failed",
    )

    assert observed["timeout_seconds"] == 300
    assert isinstance(observed["command"], list)


def test_runtime_command_failure_preserves_private_bounded_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    adapter = NavigationAnnotationRuntimeAdapter(config)
    assert config.work_root is not None
    assert config.runtime_source_root is not None
    staging = (
        config.work_root
        / "jobs"
        / ("job_" + "c" * 32)
        / "attempts"
        / ("run_" + "d" * 32)
        / "20270605_temp"
    )
    staging.mkdir(parents=True)
    private_marker = str(tmp_path / "private-output")

    monkeypatch.setattr(
        runtime_module,
        "run_command",
        lambda *_args, **_kwargs: SimpleNamespace(
            return_code=23,
            stdout=("x" * 9000) + private_marker,
            stderr="private stderr",
        ),
    )

    with pytest.raises(RuntimeExecutionError) as error:
        adapter._run_checked(
            staging_root=staging,
            argv=["/bin/false"],
            cwd=config.runtime_source_root,
            error_code="test_command_failed",
        )

    assert error.value.return_code == 23
    assert error.value.diagnostic_kind == "nonzero_exit"
    assert error.value.private_detail is not None
    assert "return_code=23" in error.value.private_detail
    assert private_marker in error.value.private_detail
    assert "private stderr" in error.value.private_detail
    assert "/bin/false" not in error.value.private_detail
    assert len(error.value.private_detail) < 16_200


def test_runtime_command_timeout_has_safe_diagnostic_kind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    adapter = NavigationAnnotationRuntimeAdapter(config)
    assert config.work_root is not None
    assert config.runtime_source_root is not None
    staging = (
        config.work_root
        / "jobs"
        / ("job_" + "e" * 32)
        / "attempts"
        / ("run_" + "f" * 32)
        / "20270605_temp"
    )
    staging.mkdir(parents=True)

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(
            ["/private/command"],
            300,
            output="bounded stdout",
            stderr="bounded stderr",
        )

    monkeypatch.setattr(runtime_module, "run_command", timeout)

    with pytest.raises(RuntimeExecutionError) as error:
        adapter._run_checked(
            staging_root=staging,
            argv=["/bin/false"],
            cwd=config.runtime_source_root,
            error_code="test_command_failed",
        )

    assert error.value.code == "runtime_command_timeout"
    assert error.value.return_code is None
    assert error.value.diagnostic_kind == "timeout"
    assert error.value.private_detail is not None
    assert "diagnostic_kind=timeout" in error.value.private_detail
    assert "bounded stdout" in error.value.private_detail
    assert "/private/command" not in error.value.private_detail


def test_runtime_capabilities_verify_bound_environment_packages_and_gpu(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    adapter = NavigationAnnotationRuntimeAdapter(config)
    assert config.data_env_setup is not None
    original_setup = config.data_env_setup.read_bytes()
    config.data_env_setup.write_bytes(original_setup + b"# changed\n")
    capability = adapter.capabilities()
    assert capability.available is False
    assert capability.reason is not None
    assert capability.reason.code == "data_runtime_mismatch"

    config.data_env_setup.write_bytes(original_setup)
    payload = json.loads(config.manifest_path.read_text(encoding="utf-8"))
    payload["entries"].append(
        {
            "root_alias": "DATA_RUNTIME_ENV",
            "relative_path": "python/packages/numpy",
            "kind": "external_runtime",
            "role": "python_package",
            "stage": "tracking",
            "version": "1.24.4",
        },
    )
    config.manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    dependency_config = replace(
        config,
        package_probe=lambda names: {
            name: "0.0.0"
            for name in names
        },
    )
    capability = NavigationAnnotationRuntimeAdapter(
        dependency_config,
    ).capabilities()
    assert capability.available is False
    assert capability.reason is not None
    assert capability.reason.code == "data_dependency_mismatch"

    gpu_config = replace(
        config,
        package_probe=lambda names: {
            name: "1.24.4"
            for name in names
        },
        gpu_probe=lambda: False,
    )
    capability = NavigationAnnotationRuntimeAdapter(gpu_config).capabilities()
    assert capability.available is False
    assert capability.reason is not None
    assert capability.reason.code == "gpu_runtime_unavailable"


def test_runtime_capabilities_require_installation_attestation(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    payload = json.loads(config.manifest_path.read_text(encoding="utf-8"))
    payload["entries"] = [
        entry
        for entry in payload["entries"]
        if entry["role"] != "runtime_dependency_summary"
    ]
    config.manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    capability = NavigationAnnotationRuntimeAdapter(config).capabilities()
    assert capability.available is False
    assert capability.reason is not None
    assert capability.reason.code == "runtime_installation_manifest_incomplete"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda document: {
            **document,
            "schema_version": 2,
        },
        lambda document: {
            **document,
            "unexpected": True,
        },
        lambda document: {
            **document,
            "platform": {
                **document["platform"],
                "unexpected": "value",
            },
        },
        lambda document: {
            **document,
            "packages": [],
        },
        lambda document: {
            **document,
            "packages": list(reversed(document["packages"])),
        },
        lambda document: {
            **document,
            "packages": [
                document["packages"][0],
                document["packages"][0],
            ],
        },
        lambda document: {
            **document,
            "packages": [
                {
                    **document["packages"][0],
                    "unexpected": "value",
                },
                *document["packages"][1:],
            ],
        },
        lambda document: {
            **document,
            "packages": [
                {
                    **document["packages"][0],
                    "name": "Invalid Package",
                },
                *document["packages"][1:],
            ],
        },
    ],
    ids=(
        "schema-version",
        "top-level-shape",
        "platform-shape",
        "empty-packages",
        "unsorted-packages",
        "duplicate-package",
        "package-shape",
        "package-name",
    ),
)
def test_runtime_capabilities_reject_malformed_dependency_summary(
    tmp_path: Path,
    mutate,
) -> None:
    config = _config(tmp_path)
    assert config.runtime_dependency_summary_path is not None
    document = json.loads(
        config.runtime_dependency_summary_path.read_text(encoding="utf-8"),
    )
    _write_dependency_summary(config, mutate(document))

    capability = NavigationAnnotationRuntimeAdapter(config).capabilities()

    assert capability.available is False
    assert capability.reason is not None
    assert capability.reason.code == "runtime_dependency_summary_invalid"


def test_runtime_capabilities_reject_duplicate_dependency_summary_key(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    _write_dependency_summary(
        config,
        (
            '{"schema_version":1,"schema_version":1,'
            '"runtime_id":"navigation_odom_v1",'
            '"platform":{"architecture":"amd64",'
            '"distribution":"Ubuntu","release":"20.04"},'
            '"packages":[{"name":"xvfb:amd64",'
            f'"version":"{EXPECTED_XVFB_VERSION}",'
            '"architecture":"amd64"}]}'
        ),
    )

    capability = NavigationAnnotationRuntimeAdapter(config).capabilities()

    assert capability.available is False
    assert capability.reason is not None
    assert capability.reason.code == "runtime_dependency_summary_invalid"


def test_runtime_capabilities_verify_each_system_dependency_version(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    probed: list[str] = []
    expected = {
        "xvfb": EXPECTED_XVFB_VERSION,
        "bubblewrap": "0.4.0-1ubuntu4.1",
        "xvfb:amd64": EXPECTED_XVFB_VERSION,
    }

    def probe(package: str) -> str:
        probed.append(package)
        return expected[package]

    capability = NavigationAnnotationRuntimeAdapter(
        replace(config, version_probe=probe),
    ).capabilities()

    assert capability.available is True
    assert probed == ["xvfb", "bubblewrap", "xvfb:amd64"]


def test_runtime_capabilities_reject_system_dependency_version_drift(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)

    def probe(package: str) -> str:
        if package == "bubblewrap":
            return "changed"
        return EXPECTED_XVFB_VERSION

    capability = NavigationAnnotationRuntimeAdapter(
        replace(config, version_probe=probe),
    ).capabilities()

    assert capability.available is False
    assert capability.reason is not None
    assert capability.reason.code == "system_dependency_mismatch"


def test_runtime_capabilities_fail_closed_when_system_probe_fails(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)

    def probe(package: str) -> str:
        if package == "bubblewrap":
            raise OSError("probe failed")
        return EXPECTED_XVFB_VERSION

    capability = NavigationAnnotationRuntimeAdapter(
        replace(config, version_probe=probe),
    ).capabilities()

    assert capability.available is False
    assert capability.reason is not None
    assert capability.reason.code == "system_dependency_probe_failed"


def test_sandbox_command_uses_fixed_xvfb_and_read_only_host_root(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    adapter = NavigationAnnotationRuntimeAdapter(config)
    staging = config.work_root / "jobs" / ("job_" + "a" * 32) / "20270605_temp"  # type: ignore[operator]
    staging.mkdir(parents=True)
    command = adapter._sandbox_command(
        staging_root=staging,
        argv=["./bin/main"],
        cwd=config.runtime_source_root / "1_onnx_tam",  # type: ignore[operator]
    )
    shell = command[2]
    assert str(config.xvfb_run_path) in shell
    assert "-screen 0 1920x1536x24 -nolisten tcp -noreset" in shell
    assert str(config.bwrap_path) in shell
    assert "--ro-bind / /" in shell
    assert "--dev-bind /dev /dev" in shell
    assert "--die-with-parent" in shell
    assert "--unshare-net" in shell
    assert "XQuartz" not in shell


def test_sandbox_command_creates_legacy_target_only_inside_namespace(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    adapter = NavigationAnnotationRuntimeAdapter(config)
    assert config.work_root is not None
    assert config.runtime_source_root is not None
    assert config.tracking_binary_data_root is not None
    assert config.legacy_tracking_data_root is not None
    staging = (
        config.work_root
        / "jobs"
        / ("job_" + "b" * 32)
        / "20270605_temp"
    )
    private_data = staging / ".runtime" / "runs" / ("run_" + "c" * 32) / "Data"
    private_data.mkdir(parents=True)

    command = adapter._sandbox_command(
        staging_root=staging,
        argv=["./bin/main"],
        cwd=config.runtime_source_root / "1_onnx_tam",
        writable_bindings=(
            (private_data, config.tracking_binary_data_root),
        ),
        sandbox_only_writable_bindings=(
            (private_data, config.legacy_tracking_data_root),
        ),
    )

    shell = command[2]
    ro_root = shell.index("--ro-bind / /")
    tmpfs = shell.index("--tmpfs /mnt")
    directory_positions = [
        shell.index(f"--dir {directory}")
        for directory in runtime_module._sandbox_only_directory_chain(
            config.legacy_tracking_data_root,
            mount_root=runtime_module._LEGACY_YAML_SANDBOX_ROOT,
        )
    ]
    virtual_bind = shell.index(
        f"--bind {private_data} {config.legacy_tracking_data_root}",
    )
    assert ro_root < tmpfs < min(directory_positions)
    assert directory_positions == sorted(directory_positions)
    assert max(directory_positions) < virtual_bind
    assert (
        shell.count(
            f"--bind {private_data} {config.legacy_tracking_data_root}",
        )
        == 1
    )
    assert (
        shell.count(
            f"--bind {private_data} {config.tracking_binary_data_root}",
        )
        == 1
    )


def test_sandbox_command_rejects_unregistered_sandbox_only_target(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    adapter = NavigationAnnotationRuntimeAdapter(config)
    assert config.work_root is not None
    assert config.runtime_source_root is not None
    staging = (
        config.work_root
        / "jobs"
        / ("job_" + "d" * 32)
        / "20270605_temp"
    )
    private_data = staging / ".runtime" / "runs" / ("run_" + "e" * 32) / "Data"
    private_data.mkdir(parents=True)

    with pytest.raises(RuntimeExecutionError) as error:
        adapter._sandbox_command(
            staging_root=staging,
            argv=["./bin/main"],
            cwd=config.runtime_source_root / "1_onnx_tam",
            sandbox_only_writable_bindings=(
                (private_data, Path("/mnt/unregistered/Data")),
            ),
        )

    assert error.value.code == "unsafe_runtime_path"


def test_sandbox_command_rejects_configured_but_unfrozen_virtual_target(
    tmp_path: Path,
) -> None:
    config = replace(
        _config(tmp_path),
        legacy_tracking_data_root=Path("/mnt/arbitrary/Data"),
    )
    adapter = NavigationAnnotationRuntimeAdapter(config)
    assert config.work_root is not None
    assert config.runtime_source_root is not None
    staging = (
        config.work_root
        / "jobs"
        / ("job_" + "f" * 32)
        / "20270605_temp"
    )
    private_data = staging / ".runtime" / "runs" / ("run_" + "1" * 32) / "Data"
    private_data.mkdir(parents=True)

    with pytest.raises(RuntimeExecutionError) as error:
        adapter._sandbox_command(
            staging_root=staging,
            argv=["./bin/main"],
            cwd=config.runtime_source_root / "1_onnx_tam",
            sandbox_only_writable_bindings=(
                (private_data, config.legacy_tracking_data_root),
            ),
        )

    assert error.value.code == "unsafe_runtime_path"


def test_sandbox_command_rejects_other_binding_under_virtual_root(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    adapter = NavigationAnnotationRuntimeAdapter(config)
    assert config.work_root is not None
    assert config.runtime_source_root is not None
    assert config.legacy_tracking_data_root is not None
    staging = (
        config.work_root
        / "jobs"
        / ("job_" + "2" * 32)
        / "20270605_temp"
    )
    private_data = staging / ".runtime" / "runs" / ("run_" + "3" * 32) / "Data"
    private_data.mkdir(parents=True)

    with pytest.raises(RuntimeExecutionError) as error:
        adapter._sandbox_command(
            staging_root=staging,
            argv=["./bin/main"],
            cwd=config.runtime_source_root / "1_onnx_tam",
            writable_bindings=((private_data, Path("/mnt/other")),),
            sandbox_only_writable_bindings=(
                (private_data, config.legacy_tracking_data_root),
            ),
        )

    assert error.value.code == "unsafe_runtime_path"


def test_prepare_copies_inputs_and_preserves_frozen_business_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    source_sequence = _sync_input(config)
    source_frame = source_sequence / "fisheye_front" / "000001.png"
    source_hash = hashlib.sha256(source_frame.read_bytes()).hexdigest()
    adapter = NavigationAnnotationRuntimeAdapter(config)
    monkeypatch.setattr(adapter, "_require_available", lambda: None)
    executed: list[str] = []

    def fake_run_checked(**kwargs) -> None:
        code = kwargs["error_code"]
        executed.append(code)
        staging = kwargs["staging_root"]
        if code == "metadata_generate_failed":
            (
                staging
                / ".runtime"
                / "NoobScenes"
                / "v1.0-develop"
                / "sample.json"
            ).write_text("{}", encoding="utf-8")
        if code == "video_prepare_failed":
            for segment in (
                staging / "samples" / "20270605"
            ).iterdir():
                (segment / "dog.mp4").write_bytes(b"video")

    monkeypatch.setattr(adapter, "_run_checked", fake_run_checked)
    job_ref = "job_" + "a" * 32
    calibration_fields = _calibration_fields(config, job_ref)
    result = adapter.prepare(
        PreparationRequest(
            job_ref=job_ref,
            run_ref="run_" + "1" * 32,
            attempt=1,
            dataset_date="20270605",
            source_clips=("20260605_160904",),
            **calibration_fields,
        ),
    )

    assert executed == [
        "preprocess_create_box_failed",
        "preprocess_odom_convert_failed",
        "preprocess_resize_failed",
        "metadata_generate_failed",
        "video_prepare_failed",
    ]
    assert len(result.segments) == 1
    prepared_frame = result.segments[0].first_frame_path
    assert result.segments[0].private_segment_key.endswith("_0")
    assert result.segments[0].width == 1920
    assert result.segments[0].height == 1536
    assert result.segments[0].sha256 == source_hash
    assert source_frame.read_bytes() == prepared_frame.read_bytes()
    assert source_frame.stat().st_ino != prepared_frame.stat().st_ino
    assert not source_frame.is_symlink()
    assert (
        result.staging_root / "v1.0-trainval" / "sample.json"
    ).is_file()
    assert (result.staging_root / "maps" / "map.png").is_file()
    assert result.staging_ref == (
        "jobs/"
        + "job_"
        + "a" * 32
        + "/attempts/"
        + "run_"
        + "1" * 32
        + "/20270605_temp"
    )
    assert len(result.input_tree_sha256) == 64
    assert len(result.prepared_artifact_tree_sha256) == 64
    assert (
        result.calibration_snapshot_sha256
        == calibration_fields["calibration_snapshot_sha256"]
    )
    private_ancestors = [
        config.work_root,
        config.work_root / "jobs",  # type: ignore[operator]
        config.work_root / "jobs" / job_ref,  # type: ignore[operator]
        config.work_root / "jobs" / job_ref / "attempts",  # type: ignore[operator]
        result.staging_root.parent,
        result.staging_root,
    ]
    assert all(
        stat.S_IMODE(path.stat(follow_symlinks=False).st_mode) == 0o700
        for path in private_ancestors
    )
    for path in result.staging_root.rglob("*"):
        mode = stat.S_IMODE(path.stat(follow_symlinks=False).st_mode)
        assert mode == (0o700 if path.is_dir() else 0o600)


@pytest.mark.parametrize(
    "relative_path",
    [
        "NoobScenes/include/attribute.py",
        "NoobScenes/maps/map.png",
    ],
)
def test_prepare_rechecks_active_payload_around_each_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative_path: str,
) -> None:
    config = _config(tmp_path)
    _sync_input(config)
    adapter = NavigationAnnotationRuntimeAdapter(config)
    assert adapter.capabilities().available is True
    monkeypatch.setattr(adapter, "_require_available", lambda: None)
    assert config.runtime_source_root is not None
    changed = config.runtime_source_root / relative_path
    command_count = 0

    def replace_active_input(**_kwargs) -> None:
        nonlocal command_count
        command_count += 1
        changed.write_text("# replaced during prepare\n", encoding="utf-8")

    monkeypatch.setattr(adapter, "_run_checked", replace_active_input)
    job_ref = "job_" + "0" * 32

    with pytest.raises(RuntimeExecutionError) as error:
        adapter.prepare(
            PreparationRequest(
                job_ref=job_ref,
                run_ref="run_" + "0" * 32,
                attempt=1,
                dataset_date="20270605",
                source_clips=("20260605_160904",),
                **_calibration_fields(config, job_ref),
            ),
        )

    assert error.value.code == "runtime_input_changed"
    assert command_count == 1


def test_prepare_rejects_ins_only_data_instead_of_switching_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    _sync_input(config, with_odom=False)
    adapter = NavigationAnnotationRuntimeAdapter(config)
    monkeypatch.setattr(adapter, "_require_available", lambda: None)
    job_ref = "job_" + "b" * 32

    with pytest.raises(RuntimeExecutionError) as error:
        adapter.prepare(
            PreparationRequest(
                job_ref=job_ref,
                run_ref="run_" + "2" * 32,
                attempt=1,
                dataset_date="20270605",
                source_clips=("20260605_160904",),
                **_calibration_fields(config, job_ref),
            ),
        )
    assert error.value.code == "unsupported_runtime_variant"


def test_prepare_revalidates_calibration_snapshot_ledger_before_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    _sync_input(config)
    adapter = NavigationAnnotationRuntimeAdapter(config)
    monkeypatch.setattr(adapter, "_require_available", lambda: None)
    job_ref = "job_" + "2" * 32
    fields = _calibration_fields(config, job_ref)
    snapshot_root = fields["calibration_snapshot_dir"]
    assert isinstance(snapshot_root, Path)
    (snapshot_root / "fisheye_front.json").write_text(
        '{"changed":true}',
        encoding="utf-8",
    )

    with pytest.raises(RuntimeExecutionError) as error:
        adapter.prepare(
            PreparationRequest(
                job_ref=job_ref,
                run_ref="run_" + "2" * 32,
                attempt=1,
                dataset_date="20270605",
                source_clips=("20260605_160904",),
                **fields,
            ),
        )

    assert error.value.code == "calibration_snapshot_mismatch"
    assert not (
        config.work_root
        / "jobs"
        / job_ref
        / "attempts"
    ).exists()  # type: ignore[operator]


def test_prepare_rejects_non_legacy_internal_segment_naming(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    sequence = _sync_input(config)
    unsupported = sequence.with_name(
        "20270605_160904_zhigu_wuhan_0",
    )
    sequence.rename(unsupported)
    adapter = NavigationAnnotationRuntimeAdapter(config)
    monkeypatch.setattr(adapter, "_require_available", lambda: None)
    job_ref = "job_" + "3" * 32

    with pytest.raises(RuntimeExecutionError) as error:
        adapter.prepare(
            PreparationRequest(
                job_ref=job_ref,
                run_ref="run_" + "3" * 32,
                attempt=1,
                dataset_date="20270605",
                source_clips=("20260605_160904",),
                **_calibration_fields(config, job_ref),
            ),
        )
    assert error.value.code == "unsupported_runtime_variant"


def test_prepare_odom_view_contains_only_selected_clips(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    selected = _sync_input(config)
    assert config.clip_data_root is not None
    unselected = (
        config.clip_data_root
        / "20270605"
        / "20260605_999999"
        / "sync_data"
        / selected.name
    )
    shutil.copytree(selected, unselected)
    adapter = NavigationAnnotationRuntimeAdapter(config)
    monkeypatch.setattr(adapter, "_require_available", lambda: None)
    odom_binding_sources: list[Path] = []

    def fake_run_checked(**kwargs) -> None:
        staging = kwargs["staging_root"]
        if kwargs["error_code"] == "preprocess_odom_convert_failed":
            bindings = kwargs["readonly_bindings"]
            assert len(bindings) == 1
            odom_binding_sources.append(bindings[0][0])
        if kwargs["error_code"] == "metadata_generate_failed":
            (
                staging
                / ".runtime"
                / "NoobScenes"
                / "v1.0-develop"
                / "sample.json"
            ).write_text("{}", encoding="utf-8")
        if kwargs["error_code"] == "video_prepare_failed":
            for segment in (staging / "samples" / "20270605").iterdir():
                (segment / "dog.mp4").write_bytes(b"video")

    monkeypatch.setattr(adapter, "_run_checked", fake_run_checked)
    job_ref = "job_" + "4" * 32
    result = adapter.prepare(
        PreparationRequest(
            job_ref=job_ref,
            run_ref="run_" + "4" * 32,
            attempt=1,
            dataset_date="20270605",
            source_clips=("20260605_160904",),
            **_calibration_fields(config, job_ref),
        ),
    )

    assert len(odom_binding_sources) == 1
    private_view = odom_binding_sources[0]
    assert (
        private_view
        / "20270605"
        / "20260605_160904"
        / "sync_data"
        / selected.name
        / "odom"
    ).is_dir()
    assert not (
        private_view / "20270605" / "20260605_999999"
    ).exists()
    copied_odom = (
        private_view
        / "20270605"
        / "20260605_160904"
        / "sync_data"
        / selected.name
        / "odom"
        / "000001.json"
    )
    source_odom = selected / "odom" / "000001.json"
    assert copied_odom.read_bytes() == source_odom.read_bytes()
    assert copied_odom.stat().st_ino != source_odom.stat().st_ino
    assert result.input_tree_sha256


def test_byte_copy_rejects_source_replaced_by_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    payload = source / "payload.bin"
    payload.write_bytes(b"trusted")
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    destination = tmp_path / "destination"
    real_open = runtime_module.os.open
    replaced = False

    def replace_before_open(path, flags, *args, **kwargs):
        nonlocal replaced
        if (
            path == "payload.bin"
            and kwargs.get("dir_fd") is not None
            and not replaced
            and not flags & os.O_WRONLY
        ):
            replaced = True
            payload.unlink()
            payload.symlink_to(outside)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(runtime_module.os, "open", replace_before_open)
    with pytest.raises(RuntimeExecutionError) as error:
        runtime_module._copy_tree_bytes(source, destination)
    assert error.value.code == "unsafe_runtime_input"
    assert not (destination / "payload.bin").exists()


def test_byte_copy_rejects_mixed_timepoint_source_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    payload = source / "payload.bin"
    payload.write_bytes(b"before")
    destination = tmp_path / "destination"
    real_copy = runtime_module._copy_directory_entries
    changed = False

    def copy_then_change(**kwargs) -> None:
        nonlocal changed
        real_copy(**kwargs)
        if not changed:
            changed = True
            payload.write_bytes(b"after")

    monkeypatch.setattr(
        runtime_module,
        "_copy_directory_entries",
        copy_then_change,
    )
    with pytest.raises(RuntimeExecutionError) as error:
        runtime_module._copy_tree_bytes(source, destination)
    assert error.value.code == "runtime_input_changed"
    assert (destination / "payload.bin").read_bytes() == b"before"


def test_prepare_rejects_duplicate_internal_segment_across_source_clips(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    sequence = _sync_input(config)
    assert config.clip_data_root is not None
    duplicate = (
        config.clip_data_root
        / "20270605"
        / "20260605_160905"
        / "sync_data"
        / sequence.name
    )
    shutil.copytree(sequence, duplicate)
    adapter = NavigationAnnotationRuntimeAdapter(config)
    monkeypatch.setattr(adapter, "_require_available", lambda: None)
    job_ref = "job_" + "f" * 32

    with pytest.raises(RuntimeExecutionError) as error:
        adapter.prepare(
            PreparationRequest(
                job_ref=job_ref,
                run_ref="run_" + "6" * 32,
                attempt=1,
                dataset_date="20270605",
                source_clips=("20260605_160904", "20260605_160905"),
                **_calibration_fields(config, job_ref),
            ),
        )
    assert error.value.code == "duplicate_internal_segment"


def test_prepare_attempts_use_independent_staging_and_legacy_image_suffixes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    sequence = _sync_input(config)
    image_root = sequence / "fisheye_front"
    (image_root / "000001.png").unlink()
    (image_root / "000000.JPG").write_bytes(_png())
    (image_root / "000001.jpeg").write_bytes(_png())
    (image_root / "000002.jpg").write_bytes(_png())
    adapter = NavigationAnnotationRuntimeAdapter(config)
    monkeypatch.setattr(adapter, "_require_available", lambda: None)

    def fake_run_checked(**kwargs) -> None:
        staging = kwargs["staging_root"]
        if kwargs["error_code"] == "metadata_generate_failed":
            (
                staging
                / ".runtime"
                / "NoobScenes"
                / "v1.0-develop"
                / "sample.json"
            ).write_text("{}", encoding="utf-8")
        if kwargs["error_code"] == "video_prepare_failed":
            for segment in (staging / "samples" / "20270605").iterdir():
                (segment / "dog.mp4").write_bytes(b"video")

    monkeypatch.setattr(adapter, "_run_checked", fake_run_checked)
    job_ref = "job_" + "9" * 32
    calibration_fields = _calibration_fields(config, job_ref)

    def prepare(run_digit: str, attempt: int):
        return adapter.prepare(
            PreparationRequest(
                job_ref=job_ref,
                run_ref="run_" + run_digit * 32,
                attempt=attempt,
                dataset_date="20270605",
                source_clips=("20260605_160904",),
                **calibration_fields,
            ),
        )

    first = prepare("7", 1)
    second = prepare("8", 2)
    assert first.staging_root != second.staging_root
    assert first.staging_root.is_dir() and second.staging_root.is_dir()
    assert first.segments[0].first_frame_path.name == "000000.JPG"
    assert second.segments[0].first_frame_path.name == "000000.JPG"
    assert first.input_tree_sha256 == second.input_tree_sha256
    assert (
        first.prepared_artifact_tree_sha256
        == second.prepared_artifact_tree_sha256
    )


def test_capacity_preflight_rejects_symlinked_clip_ancestor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    assert config.clip_data_root is not None
    outside_date = tmp_path / "outside" / "20270605"
    sequence = (
        outside_date
        / "20260605_160904"
        / "sync_data"
        / "segment_0"
    )
    for modality in ("fisheye_front", "r32_rslidar_points", "odom"):
        (sequence / modality).mkdir(parents=True)
        (sequence / modality / "000001.bin").write_bytes(b"x")
    (config.clip_data_root / "20270605").symlink_to(
        outside_date,
        target_is_directory=True,
    )
    adapter = NavigationAnnotationRuntimeAdapter(config)
    monkeypatch.setattr(adapter, "_require_available", lambda: None)

    with pytest.raises(RuntimeExecutionError) as error:
        adapter.preflight_capacity(
            "20270605",
            ("20260605_160904",),
        )
    assert error.value.code == "unsafe_runtime_input"


def test_capacity_preflight_accepts_times_metadata_and_appledouble_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    sequence = _sync_input(config)
    sync_root = sequence.parent
    (sync_root / "._finder").write_bytes(b"metadata")
    adapter = NavigationAnnotationRuntimeAdapter(config)
    monkeypatch.setattr(adapter, "_require_available", lambda: None)
    assert adapter.preflight_capacity(
        "20270605",
        ("20260605_160904",),
    ).available is True

    (sync_root / "unexpected.json").write_text("{}", encoding="utf-8")
    with pytest.raises(RuntimeExecutionError) as error:
        adapter.preflight_capacity(
            "20270605",
            ("20260605_160904",),
        )
    assert error.value.code == "unsafe_runtime_input"


@pytest.mark.parametrize("metadata_kind", ["directory", "symlink"])
def test_capacity_preflight_rejects_unsafe_times_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    metadata_kind: str,
) -> None:
    config = _config(tmp_path)
    sequence = _sync_input(config)
    times_path = sequence.parent / "times.json"
    times_path.unlink()
    if metadata_kind == "directory":
        times_path.mkdir()
    else:
        outside = tmp_path / "outside-times.json"
        outside.write_text("{}\n", encoding="utf-8")
        times_path.symlink_to(outside)
    adapter = NavigationAnnotationRuntimeAdapter(config)
    monkeypatch.setattr(adapter, "_require_available", lambda: None)

    with pytest.raises(RuntimeExecutionError) as error:
        adapter.preflight_capacity(
            "20270605",
            ("20260605_160904",),
        )

    assert error.value.code == "unsafe_runtime_input"


def test_capacity_preflight_preserves_frozen_internal_year_filter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    sequence = _sync_input(config)
    sequence.rename(
        sequence.with_name("20270605_160904_zhigu_wuhan_0"),
    )
    adapter = NavigationAnnotationRuntimeAdapter(config)
    monkeypatch.setattr(adapter, "_require_available", lambda: None)

    with pytest.raises(RuntimeExecutionError) as error:
        adapter.preflight_capacity(
            "20270605",
            ("20260605_160904",),
        )
    assert error.value.code == "unsupported_runtime_variant"


def test_capacity_estimate_includes_odom_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    sequence = _sync_input(config)
    (sequence / "odom" / "000002.json").write_bytes(b"extra-odom")
    adapter = NavigationAnnotationRuntimeAdapter(config)
    monkeypatch.setattr(adapter, "_require_available", lambda: None)

    estimate = adapter.preflight_capacity(
        "20270605",
        ("20260605_160904",),
    )

    expected = sum(
        path.stat().st_size
        for modality in ("fisheye_front", "r32_rslidar_points", "odom")
        for path in (sequence / modality).iterdir()
        if path.is_file()
    )
    assert estimate.estimated_input_bytes == expected


def test_tracking_uses_private_data_overlay_and_commits_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    runtime = NavigationTrackingRuntime(config)
    monkeypatch.setattr(runtime, "_require_available", lambda: None)
    staging = config.work_root / "jobs" / ("job_" + "c" * 32) / "20270605_temp"  # type: ignore[operator]
    segment = staging / "samples" / "20270605" / "segment_0"
    segment.mkdir(parents=True)
    (staging / ".runtime").mkdir()
    yaml_path = segment / "master_green_gray_white.yaml"
    yaml_path.write_text("{}\n", encoding="utf-8")
    target = _tracking_target(
        segment,
        yaml_path,
        "master_green_gray_white",
    )
    captured_bindings: list[tuple[Path, Path]] = []
    captured_sandbox_only_bindings: list[tuple[Path, Path]] = []

    def fake_run_checked(**kwargs) -> None:
        captured_bindings.extend(kwargs["writable_bindings"])
        captured_sandbox_only_bindings.extend(
            kwargs["sandbox_only_writable_bindings"],
        )
        output = (
            staging
            / ".runtime"
            / "runs"
            / ("run_" + "3" * 32)
            / "Data"
            / "1_img_output"
        )
        (output / "tracking_img" / "000001.png").write_bytes(_png())
        (output / "img_points.txt").write_text("1 2\n", encoding="utf-8")

    monkeypatch.setattr(runtime, "_run_checked", fake_run_checked)
    result = runtime.track(
        TrackingRequest(
            job_ref="job_" + "c" * 32,
            run_ref="run_" + "3" * 32,
            attempt=1,
            staging_root=staging,
            targets=(target,),
            attestation_targets=(target,),
            expected_runtime_manifest_sha256=hashlib.sha256(
                config.manifest_path.read_bytes(),
            ).hexdigest(),
            expected_prepared_artifact_tree_sha256=(
                runtime_module.prepared_staging_artifact_sha256(
                    staging,
                    (target,),
                )
            ),
        ),
    )

    assert len(result.checkpoints) == 1
    assert result.checkpoints[0].output_dir.is_dir()
    assert result.checkpoints[0].points_path.is_file()
    assert len(result.checkpoints[0].artifact_sha256) == 64
    private_data = (
        staging
        / ".runtime"
        / "runs"
        / ("run_" + "3" * 32)
        / "Data"
    )
    assert (private_data, config.tracking_binary_data_root) in captured_bindings
    assert (
        private_data,
        config.legacy_tracking_data_root,
    ) in captured_sandbox_only_bindings
    assert config.tracking_binary_data_root != config.legacy_tracking_data_root
    evidence = CheckpointVerificationRequest(
        job_ref="job_" + "c" * 32,
        staging_root=staging,
        segment_root=segment,
        identity="master_green_gray_white",
        artifact_sha256=result.checkpoints[0].artifact_sha256,
    )
    assert runtime.verify_checkpoint(evidence) is True
    result.checkpoints[0].points_path.write_text("changed\n", encoding="utf-8")
    assert runtime.verify_checkpoint(evidence) is False


def test_tracking_reuses_exact_private_configs_for_same_run_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    runtime = NavigationTrackingRuntime(config)
    monkeypatch.setattr(runtime, "_require_available", lambda: None)
    run_ref = "run_" + "5" * 32
    staging = (
        config.work_root
        / "jobs"
        / ("job_" + "5" * 32)
        / "attempts"
        / run_ref
        / "20270605_temp"
    )  # type: ignore[operator]
    segment = staging / "samples" / "20270605" / "segment_0"
    segment.mkdir(parents=True)
    (staging / ".runtime").mkdir()
    (segment / "000001.png").write_bytes(_png())
    yaml_contents = {
        "master_green_gray_white": b"identity: master\n",
        "other1_green_gray_white": b"identity: other1\n",
    }
    targets = tuple(
        TrackingTarget(
            segment_root=segment,
            yaml_path=segment / f"{identity}.yaml",
            identity=identity,
            expected_yaml_sha256=hashlib.sha256(content).hexdigest(),
        )
        for identity, content in yaml_contents.items()
    )
    expected_prepared = runtime_module.prepared_staging_artifact_sha256(
        staging,
        targets,
    )
    for target in targets:
        target.yaml_path.write_bytes(yaml_contents[target.identity])
    execution: list[str] = []

    def fake_run_checked(**kwargs) -> None:
        private_data = kwargs["writable_bindings"][0][0]
        execution.append(
            (private_data / "3_param" / "dog.yaml").read_text(
                encoding="utf-8",
            ).strip(),
        )
        output = private_data / "1_img_output"
        (output / "tracking_img" / "000001.png").write_bytes(_png())
        (output / "img_points.txt").write_text("1 2\n", encoding="utf-8")

    monkeypatch.setattr(runtime, "_run_checked", fake_run_checked)
    config_inodes: list[tuple[int, int]] = []
    for target in targets:
        result = runtime.track(
            TrackingRequest(
                job_ref="job_" + "5" * 32,
                run_ref=run_ref,
                attempt=1,
                staging_root=staging,
                targets=(target,),
                attestation_targets=targets,
                expected_runtime_manifest_sha256=hashlib.sha256(
                    config.manifest_path.read_bytes(),
                ).hexdigest(),
                expected_prepared_artifact_tree_sha256=expected_prepared,
            ),
        )
        assert len(result.checkpoints) == 1
        private_param = (
            staging / ".runtime" / "runs" / run_ref / "Data" / "3_param"
        )
        config_inodes.append(
            (
                (private_param / "ost.yaml").stat().st_ino,
                (private_param / "camera_extrinsics.yaml").stat().st_ino,
            ),
        )

    assert execution == ["identity: master", "identity: other1"]
    assert config_inodes[0] == config_inodes[1]
    private_param = (
        staging / ".runtime" / "runs" / run_ref / "Data" / "3_param"
    )
    (private_param / "ost.yaml").write_text(
        "changed: true\n",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeExecutionError) as error:
        runtime.track(
            TrackingRequest(
                job_ref="job_" + "5" * 32,
                run_ref=run_ref,
                attempt=1,
                staging_root=staging,
                targets=(targets[0],),
                attestation_targets=targets,
                expected_runtime_manifest_sha256=hashlib.sha256(
                    config.manifest_path.read_bytes(),
                ).hexdigest(),
                expected_prepared_artifact_tree_sha256=expected_prepared,
            ),
        )
    assert error.value.code == "runtime_input_changed"
    assert execution == ["identity: master", "identity: other1"]


def test_tracking_rejects_yaml_changed_after_render(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    runtime = NavigationTrackingRuntime(config)
    monkeypatch.setattr(runtime, "_require_available", lambda: None)
    staging = (
        config.work_root
        / "jobs"
        / ("job_" + "4" * 32)
        / "20270605_temp"
    )  # type: ignore[operator]
    segment = staging / "samples" / "20270605" / "segment_0"
    segment.mkdir(parents=True)
    (staging / ".runtime").mkdir()
    yaml_path = segment / "master_green_gray_white.yaml"
    yaml_path.write_text("{}\n", encoding="utf-8")
    target = _tracking_target(
        segment,
        yaml_path,
        "master_green_gray_white",
    )
    expected_prepared = runtime_module.prepared_staging_artifact_sha256(
        staging,
        (target,),
    )
    yaml_path.write_text("changed: true\n", encoding="utf-8")
    binary_start_count = 0

    def unexpected_run(**_kwargs) -> None:
        nonlocal binary_start_count
        binary_start_count += 1

    monkeypatch.setattr(runtime, "_run_checked", unexpected_run)

    with pytest.raises(RuntimeExecutionError) as error:
        runtime.track(
            TrackingRequest(
                job_ref="job_" + "4" * 32,
                run_ref="run_" + "4" * 32,
                attempt=1,
                staging_root=staging,
                targets=(target,),
                attestation_targets=(target,),
                expected_runtime_manifest_sha256=hashlib.sha256(
                    config.manifest_path.read_bytes(),
                ).hexdigest(),
                expected_prepared_artifact_tree_sha256=expected_prepared,
            ),
        )

    assert error.value.code == "tracking_yaml_changed"
    assert binary_start_count == 0


@pytest.mark.parametrize(
    "tamper_kind",
    ["compatibility_yaml", "binary", "model"],
)
def test_tracking_rechecks_frozen_inputs_before_binary_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tamper_kind: str,
) -> None:
    config = _config(tmp_path)
    assert config.work_root is not None
    assert config.runtime_source_root is not None
    runtime = NavigationTrackingRuntime(config)
    assert runtime.capabilities().available is True
    monkeypatch.setattr(runtime, "_require_available", lambda: None)
    staging = (
        config.work_root
        / "jobs"
        / ("job_" + "6" * 32)
        / "20270605_temp"
    )
    segment = staging / "samples" / "20270605" / "segment_0"
    segment.mkdir(parents=True)
    yaml_path = segment / "master_green_gray_white.yaml"
    yaml_path.write_text("{}\n", encoding="utf-8")
    target = _tracking_target(
        segment,
        yaml_path,
        "master_green_gray_white",
    )
    binary_start_count = 0

    if tamper_kind == "compatibility_yaml":
        source = (
            config.runtime_source_root / "Data" / "3_param" / "ost.yaml"
        )
        replacement_path = source.with_name("ost.replacement")
        replacement_path.write_text("changed\n", encoding="utf-8")
        os.replace(replacement_path, source)
    elif tamper_kind == "binary":
        source = config.runtime_source_root / "1_onnx_tam/bin/main"
        replacement_path = source.with_name("main.replacement")
        replacement_path.write_bytes(b"changed binary")
        replacement_path.chmod(0o755)
        os.replace(replacement_path, source)
    else:
        source = (
            config.runtime_source_root
            / "1_onnx_tam"
            / "models"
            / "etam"
            / "image_encoder.onnx"
        )
        replacement_path = source.with_name("image_encoder.replacement")
        replacement_path.write_bytes(b"changed model")
        os.replace(replacement_path, source)
    assert source.is_file()
    assert source.is_symlink() is False

    def unexpected_run(**_kwargs) -> None:
        nonlocal binary_start_count
        binary_start_count += 1

    monkeypatch.setattr(runtime, "_run_checked", unexpected_run)

    with pytest.raises(RuntimeExecutionError) as error:
        runtime.track(
            TrackingRequest(
                job_ref="job_" + "6" * 32,
                run_ref="run_" + "7" * 32,
                attempt=1,
                staging_root=staging,
                targets=(target,),
                attestation_targets=(target,),
                expected_runtime_manifest_sha256=hashlib.sha256(
                    config.manifest_path.read_bytes(),
                ).hexdigest(),
                expected_prepared_artifact_tree_sha256=(
                    runtime_module.prepared_staging_artifact_sha256(
                        staging,
                        (target,),
                    )
                ),
            ),
        )

    assert error.value.code == "runtime_input_changed"
    assert binary_start_count == 0


def test_tracking_rechecks_active_payload_after_binary_returns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    runtime = NavigationTrackingRuntime(config)
    monkeypatch.setattr(runtime, "_require_available", lambda: None)
    assert config.runtime_source_root is not None
    staging = (
        config.work_root
        / "jobs"
        / ("job_" + "3" * 32)
        / "20270605_temp"
    )  # type: ignore[operator]
    segment = staging / "samples" / "20270605" / "segment_0"
    segment.mkdir(parents=True)
    (staging / ".runtime").mkdir()
    yaml_path = segment / "master_green_gray_white.yaml"
    yaml_path.write_text("{}\n", encoding="utf-8")
    target = _tracking_target(
        segment,
        yaml_path,
        "master_green_gray_white",
    )
    expected_prepared = runtime_module.prepared_staging_artifact_sha256(
        staging,
        (target,),
    )
    model = (
        config.runtime_source_root
        / "1_onnx_tam"
        / "models"
        / "etam"
        / "memory_encoder.onnx"
    )

    def replace_model_after_run(**kwargs) -> None:
        private_data = kwargs["writable_bindings"][0][0]
        output = private_data / "1_img_output"
        (output / "tracking_img" / "000001.png").write_bytes(_png())
        (output / "img_points.txt").write_text("1 2\n", encoding="utf-8")
        model.write_bytes(b"replaced during Tracking")

    monkeypatch.setattr(runtime, "_run_checked", replace_model_after_run)

    with pytest.raises(RuntimeExecutionError) as error:
        runtime.track(
            TrackingRequest(
                job_ref="job_" + "3" * 32,
                run_ref="run_" + "2" * 32,
                attempt=1,
                staging_root=staging,
                targets=(target,),
                attestation_targets=(target,),
                expected_runtime_manifest_sha256=hashlib.sha256(
                    config.manifest_path.read_bytes(),
                ).hexdigest(),
                expected_prepared_artifact_tree_sha256=expected_prepared,
            ),
        )

    assert error.value.code == "runtime_input_changed"
    assert not (
        segment / "tracking_img_master_green_gray_white"
    ).exists()


def _tracking_validation_request(
    config: NavigationAnnotationRuntimeConfig,
    staging: Path,
    target: TrackingTarget,
) -> TrackingInputValidationRequest:
    return TrackingInputValidationRequest(
        job_ref="job_" + "9" * 32,
        staging_root=staging,
        targets=(target,),
        expected_runtime_manifest_sha256=runtime_module._sha256_file(
            config.manifest_path,
        ),
        expected_prepared_artifact_tree_sha256=runtime_module._tree_sha256(
            staging,
        ),
    )


def test_tracking_revalidates_prepare_tree_before_yaml_and_allows_retry_outputs(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    runtime = NavigationTrackingRuntime(config)
    staging = (
        config.work_root
        / "jobs"
        / ("job_" + "9" * 32)
        / "attempts"
        / ("run_" + "8" * 32)
        / "20270605_temp"
    )  # type: ignore[operator]
    segment = staging / "samples" / "20270605" / "segment_0"
    segment.mkdir(parents=True)
    (staging / ".runtime").mkdir()
    (segment / "000001.png").write_bytes(_png())
    target = _tracking_target(
        segment,
        segment / "master_green_gray_white.yaml",
        "master_green_gray_white",
    )
    request = _tracking_validation_request(config, staging, target)

    initial = runtime.validate_tracking_inputs(request)
    assert (
        initial.prepared_artifact_tree_sha256
        == request.expected_prepared_artifact_tree_sha256
    )

    target.yaml_path.write_text("{}\n", encoding="utf-8")
    (segment / "tracking_img_master_green_gray_white").mkdir()
    (segment / "tracking_img_master_green_gray_white" / "000001.png").write_bytes(
        _png(),
    )
    (segment / "img_master_green_gray_white.txt").write_text(
        "1 2\n",
        encoding="utf-8",
    )
    prior_scratch = staging / ".runtime" / "runs" / ("run_" + "7" * 32)
    prior_scratch.mkdir(parents=True)
    (prior_scratch / "checkpoint.bin").write_bytes(b"private")

    retried = runtime.validate_tracking_inputs(request)
    assert retried == initial


@pytest.mark.parametrize(
    "mutation",
    ["modified", "added", "deleted", "symlink"],
)
def test_tracking_rejects_prepare_tree_tampering_before_yaml(
    tmp_path: Path,
    mutation: str,
) -> None:
    config = _config(tmp_path)
    runtime = NavigationTrackingRuntime(config)
    staging = (
        config.work_root
        / "jobs"
        / ("job_" + "9" * 32)
        / "attempts"
        / ("run_" + "8" * 32)
        / "20270605_temp"
    )  # type: ignore[operator]
    segment = staging / "samples" / "20270605" / "segment_0"
    segment.mkdir(parents=True)
    baseline = segment / "000001.png"
    baseline.write_bytes(_png())
    target = _tracking_target(
        segment,
        segment / "master_green_gray_white.yaml",
        "master_green_gray_white",
    )
    request = _tracking_validation_request(config, staging, target)

    if mutation == "modified":
        baseline.write_bytes(_png(1280, 720))
    elif mutation == "added":
        (segment / "unexpected.bin").write_bytes(b"unexpected")
    elif mutation == "deleted":
        baseline.unlink()
    else:
        baseline.unlink()
        baseline.symlink_to(segment / "missing.png")

    with pytest.raises(RuntimeExecutionError) as error:
        runtime.validate_tracking_inputs(request)
    assert error.value.code == "prepared_staging_changed"


def test_tracking_rejects_runtime_manifest_switch_after_prepare(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    runtime = NavigationTrackingRuntime(config)
    staging = (
        config.work_root
        / "jobs"
        / ("job_" + "9" * 32)
        / "attempts"
        / ("run_" + "8" * 32)
        / "20270605_temp"
    )  # type: ignore[operator]
    segment = staging / "samples" / "20270605" / "segment_0"
    segment.mkdir(parents=True)
    (segment / "000001.png").write_bytes(_png())
    target = _tracking_target(
        segment,
        segment / "master_green_gray_white.yaml",
        "master_green_gray_white",
    )
    request = _tracking_validation_request(config, staging, target)

    payload = json.loads(config.manifest_path.read_text(encoding="utf-8"))
    config.manifest_path.write_text(
        json.dumps(payload, indent=2),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeExecutionError) as error:
        runtime.validate_tracking_inputs(request)
    assert error.value.code == "runtime_manifest_changed"


def test_tracking_required_entries_are_bound_to_one_stable_manifest_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    runtime = NavigationTrackingRuntime(config)
    original = config.manifest_path.read_bytes()
    expected = hashlib.sha256(original).hexdigest()
    swapped_document = json.loads(original)
    swapped_document["entries"][0]["stage"] = "tracking"
    swapped = json.dumps(swapped_document).encode("utf-8")
    real_read = runtime_module._read_stable_regular_bytes

    def swapped_read(path: Path) -> bytes:
        if path == config.manifest_path:
            return swapped
        return real_read(path)

    monkeypatch.setattr(
        runtime_module,
        "_read_stable_regular_bytes",
        swapped_read,
    )

    with pytest.raises(RuntimeExecutionError) as error:
        runtime._current_required_frozen_entries(expected)

    assert error.value.code == "runtime_manifest_changed"


def test_tracking_required_entries_reject_wrong_expected_manifest_hash(
    tmp_path: Path,
) -> None:
    runtime = NavigationTrackingRuntime(_config(tmp_path))

    with pytest.raises(RuntimeExecutionError) as error:
        runtime._current_required_frozen_entries("0" * 64)

    assert error.value.code == "runtime_manifest_changed"


def test_tracking_executes_legacy_lexical_segment_and_yaml_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    runtime = NavigationTrackingRuntime(config)
    monkeypatch.setattr(runtime, "_require_available", lambda: None)
    staging = (
        config.work_root
        / "jobs"
        / ("job_" + "6" * 32)
        / "attempts"
        / ("run_" + "6" * 32)
        / "20270605_temp"
    )  # type: ignore[operator]
    segments = [
        staging / "samples" / "20270605" / f"20260605_segment_{index}"
        for index in (1, 0)
    ]
    targets: list[TrackingTarget] = []
    (staging / ".runtime").mkdir(parents=True)
    for segment in segments:
        segment.mkdir(parents=True)
        for identity in [
            "master_green_gray_white",
            *[
                f"other{index}_green_gray_white"
                for index in range(1, 12)
            ],
        ]:
            yaml_path = segment / f"{identity}.yaml"
            yaml_path.write_text(
                f"identity: {identity}\n",
                encoding="utf-8",
            )
            targets.append(
                _tracking_target(
                    segment,
                    yaml_path,
                    identity,
                ),
            )
    reversed_targets = tuple(reversed(targets))
    expected = sorted(reversed_targets, key=tracking_target_sort_key)
    execution: list[str] = []

    def fake_run_checked(**kwargs) -> None:
        private_data = kwargs["writable_bindings"][0][0]
        dog_yaml = private_data / "3_param" / "dog.yaml"
        execution.append(
            dog_yaml.read_text(encoding="utf-8").strip().split(": ", 1)[1],
        )
        output = private_data / "1_img_output"
        (output / "tracking_img" / "000001.png").write_bytes(_png())
        (output / "img_points.txt").write_text("1 2\n", encoding="utf-8")

    monkeypatch.setattr(runtime, "_run_checked", fake_run_checked)
    result = runtime.track(
        TrackingRequest(
            job_ref="job_" + "6" * 32,
            run_ref="run_" + "6" * 32,
            attempt=1,
            staging_root=staging,
            targets=reversed_targets,
            attestation_targets=reversed_targets,
            expected_runtime_manifest_sha256=hashlib.sha256(
                config.manifest_path.read_bytes(),
            ).hexdigest(),
            expected_prepared_artifact_tree_sha256=(
                runtime_module.prepared_staging_artifact_sha256(
                    staging,
                    reversed_targets,
                )
            ),
        ),
    )

    assert execution == [target.identity for target in expected]
    assert [item.identity for item in result.checkpoints] == execution
    first_segment = [
        target.yaml_path.name
        for target in expected
        if target.segment_root.name == "20260605_segment_0"
    ]
    assert first_segment[:5] == [
        "master_green_gray_white.yaml",
        "other10_green_gray_white.yaml",
        "other11_green_gray_white.yaml",
        "other1_green_gray_white.yaml",
        "other2_green_gray_white.yaml",
    ]


def test_tracking_rechecks_capacity_immediately_before_creating_scratch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    runtime = NavigationTrackingRuntime(config)
    monkeypatch.setattr(runtime, "_require_available", lambda: None)
    staging = (
        config.work_root
        / "jobs"
        / ("job_" + "7" * 32)
        / "attempts"
        / ("run_" + "7" * 32)
        / "20270605_temp"
    )  # type: ignore[operator]
    segment = staging / "samples" / "20270605" / "segment_0"
    segment.mkdir(parents=True)
    (staging / ".runtime").mkdir()
    yaml_path = segment / "master_green_gray_white.yaml"
    yaml_path.write_text("{}\n", encoding="utf-8")
    target = _tracking_target(
        segment,
        yaml_path,
        "master_green_gray_white",
    )
    monkeypatch.setattr(
        "vla_data_juicer_agents.annotation.runtime.shutil.disk_usage",
        lambda _path: shutil._ntuple_diskusage(100, 100, 0),
    )

    with pytest.raises(RuntimeExecutionError) as error:
        runtime.track(
            TrackingRequest(
                job_ref="job_" + "7" * 32,
                run_ref="run_" + "7" * 32,
                attempt=1,
                staging_root=staging,
                targets=(target,),
                attestation_targets=(target,),
                expected_runtime_manifest_sha256=hashlib.sha256(
                    config.manifest_path.read_bytes(),
                ).hexdigest(),
                expected_prepared_artifact_tree_sha256="d" * 64,
                estimated_input_bytes=100,
                active_reserved_bytes=50,
            ),
        )
    assert error.value.code == "insufficient_work_space"
    assert not (staging / ".runtime" / "runs").exists()


@pytest.mark.parametrize("failure_point", ("second_move", "harden"))
def test_tracking_publication_finalization_requires_manual_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    config = _config(tmp_path)
    runtime = NavigationTrackingRuntime(config)
    monkeypatch.setattr(runtime, "_require_available", lambda: None)
    staging = config.work_root / "jobs" / ("job_" + "1" * 32) / "20270605_temp"  # type: ignore[operator]
    segment = staging / "samples" / "20270605" / "segment_0"
    segment.mkdir(parents=True)
    (staging / ".runtime").mkdir()
    yaml_path = segment / "master_green_gray_white.yaml"
    yaml_path.write_text("{}\n", encoding="utf-8")
    target = _tracking_target(
        segment,
        yaml_path,
        "master_green_gray_white",
    )
    run_ref = "run_" + "a" * 32

    def fake_run_checked(**_kwargs) -> None:
        output = (
            staging
            / ".runtime"
            / "runs"
            / run_ref
            / "Data"
            / "1_img_output"
        )
        (output / "tracking_img" / "000001.png").write_bytes(_png())
        (output / "img_points.txt").write_text("1 2\n", encoding="utf-8")

    monkeypatch.setattr(runtime, "_run_checked", fake_run_checked)
    original_move = shutil.move
    calls = 0

    def interrupted_move(source, target):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated publication interruption")
        return original_move(source, target)

    if failure_point == "second_move":
        monkeypatch.setattr(
            "vla_data_juicer_agents.annotation.runtime.shutil.move",
            interrupted_move,
        )
    else:
        monkeypatch.setattr(
            runtime_module,
            "_harden_private_tree",
            lambda _path: (_ for _ in ()).throw(
                OSError("simulated publication hardening failure"),
            ),
        )
    with pytest.raises(RuntimeExecutionError) as error:
        runtime.track(
            TrackingRequest(
                job_ref="job_" + "1" * 32,
                run_ref=run_ref,
                attempt=1,
                staging_root=staging,
                targets=(target,),
                attestation_targets=(target,),
                expected_runtime_manifest_sha256=hashlib.sha256(
                    config.manifest_path.read_bytes(),
                ).hexdigest(),
                expected_prepared_artifact_tree_sha256=(
                    runtime_module.prepared_staging_artifact_sha256(
                        staging,
                        (target,),
                    )
                ),
            ),
        )
    assert error.value.code == "recovery_required"
    assert (segment / "tracking_img_master_green_gray_white").is_dir()
    assert (
        (segment / "img_master_green_gray_white.txt").exists()
        is (failure_point == "harden")
    )


def test_runtime_driver_cancel_reaches_bound_cancellation_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    driver = NavigationAnnotationRuntimeDriver(config)
    entered = threading.Event()
    stopped = threading.Event()
    failure: list[Exception] = []

    def blocking_prepare(_request) -> None:
        from vla_data_juicer_agents.core.cancellation import current_cancellation

        entered.set()
        while True:
            cancellation = current_cancellation()
            assert cancellation is not None
            cancellation.raise_if_cancelled()
            time.sleep(0.01)

    monkeypatch.setattr(driver._preparation, "prepare", blocking_prepare)
    job_ref = "job_" + "d" * 32
    request = PreparationRequest(
        job_ref=job_ref,
        run_ref="run_" + "4" * 32,
        attempt=1,
        dataset_date="20270605",
        source_clips=("20260605_160904",),
        **_calibration_fields(config, job_ref),
    )

    def invoke() -> None:
        try:
            driver.prepare(request)
        except Exception as exc:
            failure.append(exc)
        finally:
            stopped.set()

    thread = threading.Thread(target=invoke)
    thread.start()
    assert entered.wait(timeout=2)
    driver.cancel(request.job_ref)
    assert stopped.wait(timeout=2)
    thread.join(timeout=2)
    assert failure
    assert isinstance(failure[0], RuntimeExecutionError)
    assert failure[0].code == "runtime_cancelled"


def test_runtime_driver_remembers_cancel_before_worker_entry(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    driver = NavigationAnnotationRuntimeDriver(config)
    job_ref = "job_" + "e" * 32
    driver.cancel(job_ref)

    with pytest.raises(RuntimeExecutionError) as error:
        driver.prepare(
            PreparationRequest(
                job_ref=job_ref,
                run_ref="run_" + "5" * 32,
                attempt=1,
                dataset_date="20270605",
                source_clips=("20260605_160904",),
                **_calibration_fields(config, job_ref),
            ),
        )
    assert error.value.code == "runtime_cancelled"


def test_runtime_driver_cancel_terminates_bound_subprocess_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    driver = NavigationAnnotationRuntimeDriver(config)
    entered = threading.Event()
    stopped = threading.Event()
    failures: list[Exception] = []

    def process_prepare(_request) -> None:
        entered.set()
        run_command(
            [
                sys.executable,
                "-c",
                (
                    "import subprocess,sys,time;"
                    "subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);"
                    "time.sleep(30)"
                ),
            ],
        )

    monkeypatch.setattr(driver._preparation, "prepare", process_prepare)
    job_ref = "job_" + "0" * 32
    request = PreparationRequest(
        job_ref=job_ref,
        run_ref="run_" + "0" * 32,
        attempt=1,
        dataset_date="20270605",
        source_clips=("20260605_160904",),
        **_calibration_fields(config, job_ref),
    )

    def invoke() -> None:
        try:
            driver.prepare(request)
        except Exception as exc:
            failures.append(exc)
        finally:
            stopped.set()

    thread = threading.Thread(target=invoke)
    thread.start()
    assert entered.wait(timeout=2)
    time.sleep(0.1)
    driver.cancel(request.job_ref)
    assert stopped.wait(timeout=3)
    thread.join(timeout=2)
    assert failures
    assert isinstance(failures[0], RuntimeExecutionError)
    assert failures[0].code == "runtime_cancelled"

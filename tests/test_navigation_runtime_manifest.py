from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from vla_data_juicer_agents.navigation.runtime_manifest import (
    ManifestValidationError,
    load_manifest,
    main,
    validate_manifest,
)


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _manifest(content: bytes = b"frozen\n") -> dict[str, object]:
    return {
        "schema_version": 1,
        "runtime_id": "navigation_odom_v1",
        "root_aliases": ["PROCESSING_ROOT", "SYSTEM_RUNTIME"],
        "entries": [
            {
                "root_alias": "PROCESSING_ROOT",
                "relative_path": "scripts/run_odom.sh",
                "kind": "frozen_file",
                "role": "production entrypoint",
                "stage": "postprocessing",
                "sha256": _sha256(content),
                "size": len(content),
                "executable": True,
            },
            {
                "root_alias": "PROCESSING_ROOT",
                "relative_path": "Data/3_param/dog.yaml",
                "kind": "generated_mutable",
                "role": "tracking scratch",
                "stage": "tracking",
                "concurrency": "global_serial",
                "cleanup": "job_scoped",
            },
            {
                "root_alias": "SYSTEM_RUNTIME",
                "relative_path": "python3.8",
                "kind": "external_runtime",
                "role": "legacy Python",
                "stage": "all",
                "version": "3.8",
            },
        ],
    }


def _write_manifest(path: Path, manifest: dict[str, object]) -> None:
    path.write_text(
        json.dumps(manifest, ensure_ascii=False),
        encoding="utf-8",
    )


def test_validate_manifest_accepts_all_three_entry_kinds() -> None:
    manifest = _manifest()

    assert validate_manifest(manifest) is manifest


def test_checked_in_navigation_odom_manifest_is_valid_and_auditable() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    manifest = load_manifest(
        repository_root / "runtime/navigation_odom_v1/manifest.json",
    )

    entries = manifest["entries"]
    frozen_paths = {
        entry["relative_path"]
        for entry in entries
        if entry["kind"] == "frozen_file"
    }
    assert {
        "run_odom.sh",
        "run_fix.sh",
        "1_onnx_tam/bin/main",
        "Data/3_param/camera_extrinsics.yaml",
        "Data/3_param/ost.yaml",
        "other_code/pcd_to_grid.py",
        "other_code/fix_trajectory_five_add_SF_odom_gridmap_0525.py",
    }.issubset(frozen_paths)
    required_active_metadata = {
        "NoobScenes/include/0_creat_box.py": (
            "active_runtime",
            "preprocess",
        ),
        "NoobScenes/include/1_odom_convert.py": (
            "active_runtime",
            "preprocess",
        ),
        "NoobScenes/include/2_resize.py": (
            "active_runtime",
            "preprocess",
        ),
        "NoobScenes/main_smart_odom.py": (
            "active_runtime",
            "metadata",
        ),
        **{
            f"NoobScenes/include/{filename}": (
                "active_runtime",
                "metadata",
            )
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
        "NoobScenes/maps/map.png": (
            "active_static_asset",
            "metadata",
        ),
        "0_1th_box/img2video.py": (
            "active_runtime",
            "initial_annotation",
        ),
        "1_onnx_tam/bin/main": ("active_binary", "tracking"),
        **{
            f"1_onnx_tam/models/etam/{name}.onnx": (
                "active_model",
                "tracking",
            )
            for name in (
                "image_encoder",
                "memory_attention",
                "image_decoder",
                "memory_encoder",
            )
        },
        "Data/3_param/camera_extrinsics.yaml": (
            "tracking_compatibility_config",
            "tracking",
        ),
        "Data/3_param/ost.yaml": (
            "tracking_compatibility_config",
            "tracking",
        ),
    }
    assert {
        entry["relative_path"]: (entry["role"], entry["stage"])
        for entry in entries
        if entry["relative_path"] in required_active_metadata
    } == required_active_metadata
    required_runtime_entries = {
        entry["relative_path"]: entry
        for entry in entries
        if entry["relative_path"]
        in {
            "NoobScenes/include/1_odom_convert.py",
            "1_onnx_tam/bin/main",
            "Data/3_param/ost.yaml",
            "Data/3_param/camera_extrinsics.yaml",
        }
    }
    assert {
        path: (
            entry["root_alias"],
            entry["kind"],
            entry["role"],
            entry["stage"],
            entry["sha256"],
            entry["size"],
        )
        for path, entry in required_runtime_entries.items()
    } == {
        "NoobScenes/include/1_odom_convert.py": (
            "NAVIGATION_ODOM_V1_SOURCE",
            "frozen_file",
            "active_runtime",
            "preprocess",
            "0428998fa18149ec646103a9beea837518bb4f8ba941ed8da09f34304ae4161b",
            4010,
        ),
        "1_onnx_tam/bin/main": (
            "NAVIGATION_ODOM_V1_SOURCE",
            "frozen_file",
            "active_binary",
            "tracking",
            "3bbb8eebd30e72ac1482e6a20858f1c7df5e4561b972a78943f88d7b897647e5",
            2853760,
        ),
        "Data/3_param/ost.yaml": (
            "NAVIGATION_ODOM_V1_SOURCE",
            "frozen_file",
            "tracking_compatibility_config",
            "tracking",
            "9a1d25967da58715f917736577f2542277f873baea9370eaf0219f6e9e36fa36",
            392,
        ),
        "Data/3_param/camera_extrinsics.yaml": (
            "NAVIGATION_ODOM_V1_SOURCE",
            "frozen_file",
            "tracking_compatibility_config",
            "tracking",
            "61672119470f3cd5bac55e7ad93774c3f4b552af2e059909b34ccdd2a43a078c",
            341,
        ),
    }
    assert any(
        entry["kind"] == "generated_mutable"
        and entry.get("concurrency") == "global_serial"
        for entry in entries
    )
    assert any(
        entry["kind"] == "external_runtime"
        and entry["role"] == "dynamic_dependency_unfrozen"
        for entry in entries
    )
    assert any(
        entry["kind"] == "external_runtime"
        and entry["root_alias"] == "DATA_RUNTIME_ENV"
        and entry["relative_path"] == "setup_data_runtime.sh"
        and entry["sha256"]
        == "88382e1a0b59c3b3ebda6191299aaa8e60c937284e43d911257cdbda9a5f29a1"
        for entry in entries
    )
    assert any(
        entry["kind"] == "external_runtime"
        and entry["role"] == "active_python_interpreter"
        and entry["relative_path"] == "usr/bin/python3.8"
        and entry["version"] == "3.8.10"
        for entry in entries
    )
    package_versions = {
        entry["relative_path"]: entry["version"]
        for entry in entries
        if entry["kind"] == "external_runtime"
        and entry["role"]
        in {"python_package", "python_package_direct_dependency"}
    }
    assert package_versions == {
        "python/packages/numpy": "1.23.5",
        "python/packages/opencv-python": "4.12.0.88",
        "python/packages/scipy": "1.10.1",
        "python/packages/Pillow": "10.0.0",
        "python/packages/PyYAML": "6.0.1",
        "python/packages/open3d": "0.13.0",
        "python/packages/matplotlib": "3.1.2",
        "python/packages/similaritymeasures": "1.4.0",
        "python/packages/mmcv": "2.2.0",
        "python/packages/nuscenes-devkit": "1.1.11",
        "python/packages/pyquaternion": "0.9.9",
        "python/packages/pypcd": "0.1.1",
        "python/packages/numba": "0.58.1",
    }


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda manifest: manifest.update(schema_version=2),
            "schema_version",
        ),
        (
            lambda manifest: manifest["entries"][0].update(sha256="A" * 64),
            "lowercase",
        ),
        (
            lambda manifest: manifest["entries"][0].update(size=True),
            "non-negative integer",
        ),
        (
            lambda manifest: manifest["entries"][0].update(executable=1),
            "boolean",
        ),
        (
            lambda manifest: manifest["entries"][0].update(
                root_alias="UNDECLARED"
            ),
            "not declared",
        ),
        (
            lambda manifest: manifest["entries"][0].update(extra="no"),
            "unknown field",
        ),
        (
            lambda manifest: manifest["entries"][2].pop("version"),
            "version or hash metadata",
        ),
    ],
)
def test_validate_manifest_rejects_invalid_schema(
    mutate,
    message: str,
) -> None:
    manifest = _manifest()
    mutate(manifest)

    with pytest.raises(ManifestValidationError, match=message):
        validate_manifest(manifest)


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "/media/company/runtime/run_odom.sh",
        "../runtime/run_odom.sh",
        "scripts/../run_odom.sh",
        "scripts//run_odom.sh",
        "C:\\Users\\person\\run_odom.sh",
        "home/person/run_odom.sh",
        "Users/person/run_odom.sh",
    ],
)
def test_validate_manifest_rejects_unsafe_relative_paths(
    unsafe_path: str,
) -> None:
    manifest = _manifest()
    manifest["entries"][0]["relative_path"] = unsafe_path

    with pytest.raises(ManifestValidationError):
        validate_manifest(manifest)


def test_validate_manifest_rejects_absolute_path_in_non_path_field() -> None:
    manifest = _manifest()
    manifest["entries"][0]["role"] = "copied from /home/person/runtime"

    with pytest.raises(ManifestValidationError, match="absolute path"):
        validate_manifest(manifest)


def test_validate_manifest_command_rejects_duplicate_json_keys(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        '{"schema_version":1,"schema_version":1}',
        encoding="utf-8",
    )

    assert main(
        ["validate-manifest", "--manifest", str(manifest_path)]
    ) == 2
    assert "duplicate JSON key" in capsys.readouterr().err


def test_verify_root_returns_zero_for_matching_frozen_files_and_skips_others(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    content = b"frozen\n"
    root = tmp_path / "private-root"
    script = root / "scripts" / "run_odom.sh"
    script.parent.mkdir(parents=True)
    script.write_bytes(content)
    script.chmod(0o755)
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path, _manifest(content))

    result = main(
        [
            "verify-root",
            "--manifest",
            str(manifest_path),
            "--root-alias",
            "PROCESSING_ROOT",
            "--root",
            str(root),
        ]
    )

    captured = capsys.readouterr()
    assert result == 0
    assert "1 frozen file(s)" in captured.out
    assert str(root) not in captured.out
    assert str(root) not in captured.err


def test_verify_root_does_not_claim_external_only_alias_was_verified(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "system-runtime"
    root.mkdir()
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path, _manifest())

    assert main(
        [
            "verify-root",
            "--manifest",
            str(manifest_path),
            "--root-alias",
            "SYSTEM_RUNTIME",
            "--root",
            str(root),
        ],
    ) == 2
    captured = capsys.readouterr()
    assert "no frozen files" in captured.err
    assert "external entries were not verified" in captured.err
    assert str(root) not in captured.err


@pytest.mark.parametrize(
    ("replacement", "expected"),
    [
        (b"changed\n", "size mismatch"),
        (b"frozem\n", "sha256 mismatch"),
    ],
)
def test_verify_root_returns_one_for_content_mismatch_without_root_leak(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    replacement: bytes,
    expected: str,
) -> None:
    content = b"frozen\n"
    root = tmp_path / "sensitive-username" / "runtime"
    script = root / "scripts" / "run_odom.sh"
    script.parent.mkdir(parents=True)
    script.write_bytes(replacement)
    script.chmod(0o755)
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path, _manifest(content))

    result = main(
        [
            "verify-root",
            "--manifest",
            str(manifest_path),
            "--root-alias",
            "PROCESSING_ROOT",
            "--root",
            str(root),
        ]
    )

    captured = capsys.readouterr()
    assert result == 1
    assert expected in captured.out
    assert "PROCESSING_ROOT:scripts/run_odom.sh" in captured.out
    assert str(root) not in captured.out
    assert str(root) not in captured.err


def test_verify_root_returns_one_for_executable_mismatch(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    content = b"frozen\n"
    root = tmp_path / "runtime"
    script = root / "scripts" / "run_odom.sh"
    script.parent.mkdir(parents=True)
    script.write_bytes(content)
    script.chmod(0o644)
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path, _manifest(content))

    assert main(
        [
            "verify-root",
            "--manifest",
            str(manifest_path),
            "--root-alias",
            "PROCESSING_ROOT",
            "--root",
            str(root),
        ]
    ) == 1
    assert "executable mismatch" in capsys.readouterr().out


def test_verify_root_rejects_symlink_without_following_it(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    content = b"frozen\n"
    root = tmp_path / "runtime"
    outside = tmp_path / "outside.sh"
    outside.write_bytes(content)
    outside.chmod(0o755)
    script = root / "scripts" / "run_odom.sh"
    script.parent.mkdir(parents=True)
    script.symlink_to(outside)
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path, _manifest(content))

    result = main(
        [
            "verify-root",
            "--manifest",
            str(manifest_path),
            "--root-alias",
            "PROCESSING_ROOT",
            "--root",
            str(root),
        ]
    )

    captured = capsys.readouterr()
    assert result == 1
    assert "symlink is not allowed" in captured.out
    assert str(root) not in captured.out
    assert str(outside) not in captured.out


def test_verify_root_returns_two_for_invalid_alias_without_root_leak(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "sensitive-root"
    root.mkdir()
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path, _manifest())

    result = main(
        [
            "verify-root",
            "--manifest",
            str(manifest_path),
            "--root-alias",
            "UNKNOWN_ROOT",
            "--root",
            str(root),
        ]
    )

    captured = capsys.readouterr()
    assert result == 2
    assert "requested root alias is not declared" in captured.err
    assert str(root) not in captured.err


def test_verify_root_does_not_modify_verified_file(tmp_path: Path) -> None:
    content = b"frozen\n"
    root = tmp_path / "runtime"
    script = root / "scripts" / "run_odom.sh"
    script.parent.mkdir(parents=True)
    script.write_bytes(content)
    script.chmod(0o755)
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path, _manifest(content))
    before = (script.read_bytes(), script.stat().st_mtime_ns)

    assert main(
        [
            "verify-root",
            "--manifest",
            str(manifest_path),
            "--root-alias",
            "PROCESSING_ROOT",
            "--root",
            str(root),
        ]
    ) == 0

    assert (script.read_bytes(), script.stat().st_mtime_ns) == before

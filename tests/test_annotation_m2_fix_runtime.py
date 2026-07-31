from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from vla_data_juicer_agents.annotation.fix_runtime import (
    FixCompatibilityPublisher,
    NavigationFixRuntime,
)
from vla_data_juicer_agents.annotation.legacy_fix_driver import (
    _apply_command_log,
    _write_preview_state,
)
from vla_data_juicer_agents.annotation.runtime import (
    NavigationAnnotationRuntimeConfig,
    RuntimeExecutionError,
)


class _PreflightFixRuntime(NavigationFixRuntime):
    def __init__(self, config: NavigationAnnotationRuntimeConfig) -> None:
        super().__init__(config)
        self.revalidated: list[str] = []

    def _require_available(self) -> None:
        return None

    def _revalidate_fix_input(self, expected_manifest_sha256: str) -> None:
        self.revalidated.append(expected_manifest_sha256)


class _FakeFrozenEditor:
    def __init__(self) -> None:
        self.timestamps = ["1.0", "2.0"]
        self.current_index = 0
        self.target_files = {"master": "master.txt"}
        self.modified_target_points = {
            "1.0": {"master": (1.0, 2.0)},
            "2.0": {"master": None},
        }
        self.original_target_points = {
            "1.0": {"master": (1.0, 2.0)},
            "2.0": {"master": None},
        }
        self.added_target_info = {
            "1.0": {"master": {"pos": (1.0, 2.0), "dir": 0.0}},
            "2.0": {"master": None},
        }
        self.modified_trajectory = {
            "1.0": {"pass": False, "master": {"traj": []}},
            "2.0": {"pass": False, "master": {"traj": []}},
        }
        self.speed_direction_data: dict[str, Any] = {}
        self.sensor_params = {"image_size": [1920, 1536]}
        self.fisheye_folder = "/missing/fisheye_front"
        self.clip_path = "/missing/segment"
        self.target_speed_inputs = {
            "1.0": {"master": SimpleNamespace(text="1.0")},
            "2.0": {"master": SimpleNamespace(text="1.0")},
        }
        self.recomputed: list[tuple[int, str]] = []

    def project_lidar_to_image(
        self,
        points: list[list[float]],
        _image_size: list[int],
        _sensor_params: dict[str, Any],
    ) -> list[list[float]]:
        return [[point[0] * 10, point[1] * 10] for point in points]

    def on_ok_click(self, _event: object) -> None:
        timestamp = self.timestamps[self.current_index]
        speed = self.target_speed_inputs[timestamp]["master"].text
        self.recomputed.append((self.current_index, speed))

    def on_back_click(self, _event: object) -> None:
        timestamp = self.timestamps[self.current_index]
        self.modified_target_points[timestamp] = self.original_target_points[
            timestamp
        ].copy()
        self.modified_trajectory[timestamp]["pass"] = False
        self.target_speed_inputs[timestamp]["master"] = SimpleNamespace(
            text="1.0"
        )

    def on_pass_click(self, _event: object) -> None:
        timestamp = self.timestamps[self.current_index]
        current = bool(self.modified_trajectory[timestamp]["pass"])
        self.modified_trajectory[timestamp]["pass"] = not current

    def on_delete_target(self, target_type: str) -> None:
        timestamp = self.timestamps[self.current_index]
        self.modified_target_points[timestamp][target_type] = None

    def on_add_missing_target(self, target_type: str) -> None:
        timestamp = self.timestamps[self.current_index]
        previous = self.timestamps[self.current_index - 1]
        position = self.modified_target_points[previous][target_type]
        self.modified_target_points[timestamp][target_type] = position
        self.added_target_info[timestamp][target_type] = {
            "pos": position,
            "dir": 0.0,
        }


def test_fix_driver_replays_domain_commands_through_frozen_operations() -> None:
    editor = _FakeFrozenEditor()
    target_ref = "target_" + "1" * 32

    _apply_command_log(
        editor,
        bindings={target_ref: "master"},
        commands=[
            {
                "kind": "set_position",
                "frame_index": 0,
                "target_ref": target_ref,
                "x": 3.0,
                "y": 4.0,
            },
            {
                "kind": "set_direction",
                "frame_index": 0,
                "target_ref": target_ref,
                "direction": 1.5,
            },
            {
                "kind": "set_speed",
                "frame_index": 0,
                "target_ref": target_ref,
                "speed": 2.5,
            },
            {
                "kind": "toggle_pass",
                "frame_index": 0,
                "value": True,
            },
            {
                "kind": "add_missing_target",
                "frame_index": 1,
                "target_ref": target_ref,
            },
            {
                "kind": "delete_target",
                "frame_index": 1,
                "target_ref": target_ref,
            },
        ],
    )

    assert editor.modified_target_points["1.0"]["master"] == (3.0, 4.0)
    assert editor.added_target_info["1.0"]["master"]["dir"] == 1.5
    assert editor.modified_trajectory["1.0"]["pass"] is True
    assert editor.modified_target_points["2.0"]["master"] is None
    assert editor.recomputed == [
        (0, "1.0"),
        (0, "1.0"),
        (0, "2.5"),
        (1, "1.0"),
    ]


def test_fix_driver_restore_clears_frame_speed_override() -> None:
    editor = _FakeFrozenEditor()
    target_ref = "target_" + "1" * 32

    _apply_command_log(
        editor,
        bindings={target_ref: "master"},
        commands=[
            {
                "kind": "set_speed",
                "frame_index": 0,
                "target_ref": target_ref,
                "speed": 2.5,
            },
            {"kind": "restore_frame", "frame_index": 0},
            {
                "kind": "set_position",
                "frame_index": 0,
                "target_ref": target_ref,
                "x": 5.0,
                "y": 6.0,
            },
        ],
    )

    assert editor.recomputed == [(0, "2.5"), (0, "1.0")]


def test_fix_driver_writes_authoritative_candidate_preview(
    tmp_path: Path,
) -> None:
    editor = _FakeFrozenEditor()
    target_ref = "target_" + "1" * 32
    editor.modified_trajectory["1.0"]["master"]["traj"] = [
        [1.0, 2.0, 0.0],
        [3.0, 4.0, 0.0],
    ]
    output = tmp_path / ".system_fix_preview.json"

    _write_preview_state(
        editor,
        bindings={target_ref: "master"},
        speed_overrides={(0, "master"): 2.5},
        output_path=output,
    )

    preview = json.loads(output.read_text(encoding="utf-8"))
    target = preview["frames"][0]["targets"][target_ref]
    assert target["position"] == [1.0, 2.0]
    assert target["speed"] == 2.5
    assert target["camera_position"] == [10.0, 20.0]
    assert target["camera_trajectory_points"] == [
        [10.0, 20.0],
        [30.0, 40.0],
    ]


def test_approved_fix_publication_is_atomic_and_hash_bound(
    tmp_path: Path,
) -> None:
    source = tmp_path / "candidate" / "clip_trajectory_fix_five.json"
    target_root = tmp_path / "published-segment"
    journal = tmp_path / "journal"
    source.parent.mkdir()
    target_root.mkdir()
    journal.mkdir(mode=0o700)
    source.write_text('{"frame":{}}\n', encoding="utf-8")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()

    result = FixCompatibilityPublisher().publish(
        review_ref="review_" + "1" * 32,
        revision_ref="fix_revision_" + "2" * 32,
        source_path=source,
        expected_sha256=digest,
        target_segment_root=target_root,
        journal_root=journal,
    )

    assert result.content_sha256 == digest
    assert result.published_path.read_bytes() == source.read_bytes()
    assert b'"state":"committed"' in result.journal_path.read_bytes()


def test_approved_fix_publication_never_overwrites_difference(
    tmp_path: Path,
) -> None:
    source = tmp_path / "candidate" / "clip_trajectory_fix_five.json"
    target_root = tmp_path / "published-segment"
    journal = tmp_path / "journal"
    source.parent.mkdir()
    target_root.mkdir()
    journal.mkdir(mode=0o700)
    source.write_text('{"new":true}\n', encoding="utf-8")
    target = target_root / source.name
    target.write_text('{"old":true}\n', encoding="utf-8")
    old = target.read_bytes()

    with pytest.raises(RuntimeExecutionError) as failure:
        FixCompatibilityPublisher().publish(
            review_ref="review_" + "1" * 32,
            revision_ref="fix_revision_" + "2" * 32,
            source_path=source,
            expected_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            target_segment_root=target_root,
            journal_root=journal,
        )

    assert failure.value.code == "publication_conflict"
    assert target.read_bytes() == old


def test_fix_preflight_attests_frozen_manifest(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_bytes(b'{"schema_version":1}\n')
    runtime = _PreflightFixRuntime(
        NavigationAnnotationRuntimeConfig(
            runtime_source_root=None,
            work_root=None,
            clip_data_root=None,
            data_python=None,
            data_env_setup=None,
            manifest_path=manifest,
        )
    )
    expected = hashlib.sha256(manifest.read_bytes()).hexdigest()

    assert runtime.preflight() == expected
    assert runtime.revalidated == [expected]

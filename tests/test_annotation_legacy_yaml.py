from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from vla_data_juicer_agents.annotation.legacy_yaml import (
    CLOTHING_COLORS,
    LegacyYamlAdapter,
    LegacyYamlError,
)


def _target(
    target_ref: str,
    *,
    colors: tuple[str, str, str] = ("green", "gray", "white"),
) -> dict[str, object]:
    return {
        "target_ref": target_ref,
        "bbox": [10, 20, 30, 40],
        "point": [25, 35],
        "upper_color": colors[0],
        "lower_color": colors[1],
        "shoes_color": colors[2],
    }


def test_legacy_yaml_preserves_identity_order_paths_and_document_shape(
    tmp_path: Path,
) -> None:
    segment = (tmp_path / "segment").resolve()
    segment.mkdir()
    rendered = LegacyYamlAdapter().render(
        segment,
        [
            _target("target_master_1234"),
            _target("target_other_12345", colors=("blue", "black", "gray")),
        ],
    )

    assert [item.filename for item in rendered] == [
        "master_green_gray_white.yaml",
        "other1_blue_black_gray.yaml",
    ]
    first = yaml.safe_load(rendered[0].content)
    assert list(first) == ["box", "paths", "point"]
    assert first["box"] == [[10, 20, 30, 40]]
    assert first["point"] == [[25, 35]]
    assert first["paths"]["img2video_mp4"] == str(segment / "dog.mp4")
    assert first["paths"]["intri"].endswith("/Data/3_param/ost.yaml")
    assert first["paths"]["extri"].endswith(
        "/Data/3_param/camera_extrinsics.yaml",
    )
    assert rendered[0].content == (
        "box:\n"
        "- - 10\n"
        "  - 20\n"
        "  - 30\n"
        "  - 40\n"
        "paths:\n"
        "  extri: /mnt/data1/gh/tracking_1/Data/3_param/"
        "camera_extrinsics.yaml\n"
        f"  img2video_mp4: {segment / 'dog.mp4'}\n"
        "  intri: /mnt/data1/gh/tracking_1/Data/3_param/ost.yaml\n"
        "point:\n"
        "- - 25\n"
        "  - 35\n"
    )


def test_legacy_yaml_freezes_all_fourteen_colors() -> None:
    assert CLOTHING_COLORS == (
        "black",
        "white",
        "gray",
        "red",
        "yellow",
        "blue",
        "green",
        "pink",
        "purple",
        "brown",
        "orange",
        "camouflage",
        "beige",
        "khaki",
    )


def test_legacy_yaml_rejects_missing_target_duplicate_ref_and_unknown_color(
    tmp_path: Path,
) -> None:
    segment = (tmp_path / "segment").resolve()
    segment.mkdir()
    adapter = LegacyYamlAdapter()

    with pytest.raises(LegacyYamlError, match="at least one"):
        adapter.render(segment, [])
    with pytest.raises(LegacyYamlError, match="unique"):
        adapter.render(segment, [_target("target_123456789"), _target("target_123456789")])
    with pytest.raises(LegacyYamlError, match="unsupported"):
        adapter.render(
            segment,
            [_target("target_123456789", colors=("cyan", "black", "white"))],
        )


def test_legacy_yaml_write_is_segment_local_and_atomic(tmp_path: Path) -> None:
    segment = tmp_path / "segment"
    segment.mkdir()
    paths = LegacyYamlAdapter().write(
        segment,
        [_target("target_123456789")],
    )

    assert paths == (segment / "master_green_gray_white.yaml",)
    assert not list(segment.glob(".*.tmp"))
    assert yaml.safe_load(paths[0].read_text(encoding="utf-8"))["box"] == [
        [10, 20, 30, 40],
    ]

    # An identical retry reuses the immutable file.
    inode = paths[0].stat().st_ino
    assert LegacyYamlAdapter().write(
        segment,
        [_target("target_123456789")],
    ) == paths
    assert paths[0].stat().st_ino == inode


def test_legacy_yaml_never_overwrites_conflicting_or_symlink_yaml(
    tmp_path: Path,
) -> None:
    segment = tmp_path / "segment"
    segment.mkdir()
    destination = segment / "master_green_gray_white.yaml"
    destination.write_text("different: true\n", encoding="utf-8")
    adapter = LegacyYamlAdapter()

    with pytest.raises(LegacyYamlError, match="conflicts"):
        adapter.write(segment, [_target("target_123456789")])
    assert destination.read_text(encoding="utf-8") == "different: true\n"

    destination.unlink()
    outside = tmp_path / "outside.yaml"
    outside.write_text("outside: true\n", encoding="utf-8")
    destination.symlink_to(outside)
    with pytest.raises(LegacyYamlError, match="conflicts"):
        adapter.write(segment, [_target("target_123456789")])
    assert outside.read_text(encoding="utf-8") == "outside: true\n"

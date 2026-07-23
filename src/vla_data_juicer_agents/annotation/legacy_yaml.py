"""Compatibility adapter for the frozen Tracking YAML contract.

This module replaces only the desktop geometry-entry GUI.  It intentionally
keeps the YAML document, identity ordering, clothing vocabulary, and
hard-coded Tracking compatibility paths used by ``gen_box.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import stat
from typing import Iterable, Mapping, Sequence

import yaml


CLOTHING_COLORS: tuple[str, ...] = (
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
_COLOR_SET = frozenset(CLOTHING_COLORS)
_TARGET_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")

DEFAULT_INTRINSICS_PATH = (
    "/mnt/data1/gh/tracking_1/Data/3_param/ost.yaml"
)
DEFAULT_EXTRINSICS_PATH = (
    "/mnt/data1/gh/tracking_1/Data/3_param/camera_extrinsics.yaml"
)


class LegacyYamlError(ValueError):
    """An annotation cannot be represented by the frozen YAML contract."""


@dataclass(frozen=True)
class LegacyAnnotationTarget:
    target_ref: str
    bbox: tuple[int, int, int, int]
    foreground_point: tuple[int, int]
    upper_color: str
    lower_color: str
    shoes_color: str


@dataclass(frozen=True)
class RenderedLegacyYaml:
    target_ref: str
    identity: str
    filename: str
    content: str
    sha256: str


def _regular_file_sha256(path: Path) -> str | None:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            return None
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _integer_tuple(
    value: Sequence[object],
    *,
    length: int,
    label: str,
) -> tuple[int, ...]:
    if len(value) != length:
        raise LegacyYamlError(f"{label} must contain exactly {length} integers")
    if any(isinstance(item, bool) or not isinstance(item, int) for item in value):
        raise LegacyYamlError(f"{label} must contain only integers")
    return tuple(value)  # type: ignore[return-value]


def _coerce_target(value: LegacyAnnotationTarget | Mapping[str, object]) -> LegacyAnnotationTarget:
    if isinstance(value, LegacyAnnotationTarget):
        return value
    try:
        clothing = value.get("clothing")
        if isinstance(clothing, Mapping):
            upper = clothing["upper"]
            lower = clothing["lower"]
            shoes = clothing["shoes"]
        else:
            upper = value["upper_color"]
            lower = value["lower_color"]
            shoes = value["shoes_color"]
        point = value.get("foreground_point", value.get("point"))
        if not isinstance(point, Sequence) or isinstance(point, (str, bytes)):
            raise LegacyYamlError("foreground_point must contain two integers")
        bbox = value["bbox"]
        if not isinstance(bbox, Sequence) or isinstance(bbox, (str, bytes)):
            raise LegacyYamlError("bbox must contain four integers")
        return LegacyAnnotationTarget(
            target_ref=str(value["target_ref"]),
            bbox=_integer_tuple(bbox, length=4, label="bbox"),  # type: ignore[arg-type]
            foreground_point=_integer_tuple(
                point,
                length=2,
                label="foreground_point",
            ),  # type: ignore[arg-type]
            upper_color=str(upper),
            lower_color=str(lower),
            shoes_color=str(shoes),
        )
    except KeyError as exc:
        raise LegacyYamlError(f"missing target field: {exc.args[0]}") from exc


class LegacyYamlAdapter:
    """Render Web geometry into the exact document consumed by Tracking."""

    def __init__(
        self,
        *,
        intrinsics_path: str = DEFAULT_INTRINSICS_PATH,
        extrinsics_path: str = DEFAULT_EXTRINSICS_PATH,
    ) -> None:
        if not Path(intrinsics_path).is_absolute():
            raise LegacyYamlError("intrinsics_path must be absolute")
        if not Path(extrinsics_path).is_absolute():
            raise LegacyYamlError("extrinsics_path must be absolute")
        self._intrinsics_path = intrinsics_path
        self._extrinsics_path = extrinsics_path

    def render(
        self,
        segment_root: Path,
        targets: Iterable[LegacyAnnotationTarget | Mapping[str, object]],
    ) -> tuple[RenderedLegacyYaml, ...]:
        segment_root = Path(segment_root)
        if not segment_root.is_absolute():
            raise LegacyYamlError("segment_root must be absolute")
        video_path = segment_root / "dog.mp4"
        normalized = tuple(_coerce_target(target) for target in targets)
        if not normalized:
            raise LegacyYamlError("at least one target is required")

        seen_refs: set[str] = set()
        rendered: list[RenderedLegacyYaml] = []
        for index, target in enumerate(normalized):
            if not _TARGET_REF_RE.fullmatch(target.target_ref):
                raise LegacyYamlError("target_ref is not a safe opaque identifier")
            if target.target_ref in seen_refs:
                raise LegacyYamlError("target_ref values must be unique")
            seen_refs.add(target.target_ref)
            x, y, width, height = _integer_tuple(
                target.bbox,
                length=4,
                label="bbox",
            )
            point_x, point_y = _integer_tuple(
                target.foreground_point,
                length=2,
                label="foreground_point",
            )
            colors = (
                target.upper_color,
                target.lower_color,
                target.shoes_color,
            )
            if any(color not in _COLOR_SET for color in colors):
                raise LegacyYamlError("target contains an unsupported clothing color")
            identity = "master" if index == 0 else f"other{index}"
            filename = f"{identity}_{colors[0]}_{colors[1]}_{colors[2]}.yaml"
            document = {
                "paths": {
                    "intri": self._intrinsics_path,
                    "extri": self._extrinsics_path,
                    "img2video_mp4": str(video_path),
                },
                "box": [[x, y, width, height]],
                "point": [[point_x, point_y]],
            }
            content = yaml.safe_dump(
                document,
                allow_unicode=True,
                # Frozen gen_box.py uses PyYAML's default key ordering.
                sort_keys=True,
            )
            rendered.append(
                RenderedLegacyYaml(
                    target_ref=target.target_ref,
                    identity=identity,
                    filename=filename,
                    content=content,
                    sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                ),
            )
        return tuple(rendered)

    def write(
        self,
        segment_root: Path,
        targets: Iterable[LegacyAnnotationTarget | Mapping[str, object]],
    ) -> tuple[Path, ...]:
        """Atomically publish rendered YAMLs inside a private segment staging."""

        root = Path(segment_root)
        if root.is_symlink():
            raise LegacyYamlError("segment_root cannot be a symlink")
        try:
            metadata = root.stat(follow_symlinks=False)
        except OSError as exc:
            raise LegacyYamlError(
                f"segment_root is unavailable: {type(exc).__name__}",
            ) from exc
        if not stat.S_ISDIR(metadata.st_mode):
            raise LegacyYamlError("segment_root must be a directory")

        rendered = self.render(root.resolve(strict=True), targets)
        destinations: list[Path] = []
        for item in rendered:
            destination = root / item.filename
            if destination.exists() or destination.is_symlink():
                try:
                    metadata = destination.lstat()
                except OSError as exc:
                    raise LegacyYamlError(
                        f"cannot inspect existing YAML: {type(exc).__name__}",
                    ) from exc
                if (
                    stat.S_ISREG(metadata.st_mode)
                    and not stat.S_ISLNK(metadata.st_mode)
                    and _regular_file_sha256(destination) == item.sha256
                ):
                    destinations.append(destination)
                    continue
                raise LegacyYamlError(
                    "existing Tracking YAML conflicts with this revision",
                )
            temporary = root / f".{item.filename}.{os.getpid()}.tmp"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                descriptor = os.open(temporary, flags, 0o600)
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    stream.write(item.content)
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    os.link(
                        temporary,
                        destination,
                        follow_symlinks=False,
                    )
                except FileExistsError:
                    if (
                        not destination.is_symlink()
                        and destination.is_file()
                        and _regular_file_sha256(destination) == item.sha256
                    ):
                        pass
                    else:
                        raise LegacyYamlError(
                            "existing Tracking YAML conflicts with this revision",
                        )
                finally:
                    temporary.unlink(missing_ok=True)
            except Exception:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
                raise
            destinations.append(destination)
        return tuple(destinations)

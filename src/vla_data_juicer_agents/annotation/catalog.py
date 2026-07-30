from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vla_data_juicer_agents.annotation.models import (
    AnnotationConflictError,
    AnnotationValidationError,
)
from vla_data_juicer_agents.navigation.runtime_manifest import load_manifest


_PROFILE_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$")
_PROFILE_ROOT_ALIAS = "NAVIGATION_ODOM_V1_SOURCE"
_PROFILE_STAGES = frozenset({"calibration", "fix_calibration"})
_REQUIRED_SENSOR_FILES = frozenset(
    {"fisheye_front.json", "r32_rslidar_points.json"}
)


@dataclass(frozen=True)
class CalibrationProfile:
    profile_ref: str
    label: str
    content_sha256: str
    files: tuple[dict[str, Any], ...]

    def public_projection(self) -> dict[str, str]:
        return {
            "profile_ref": self.profile_ref,
            "label": self.label,
            "content_sha256": self.content_sha256,
        }


class CalibrationCatalog:
    """Manifest-backed processing and Fix calibration inventories.

    Paths remain private.  Public callers receive only a profile ref, display
    label, and an aggregate content hash.
    """

    def __init__(
        self,
        manifest_path: Path,
        *,
        source_root: Path | None = None,
    ) -> None:
        self.manifest_path = Path(manifest_path)
        self.source_root = (
            Path(source_root)
            if source_root is not None
            else _optional_path("VLA_NAVIGATION_ODOM_V1_SOURCE")
        )
        self._profiles_by_purpose = self._load()
        # Retain the M1 private attribute for compatibility with existing
        # deployment checks while routing all new reads through purpose.
        self._profiles = self._profiles_by_purpose["processing"]

    @classmethod
    def default(cls) -> "CalibrationCatalog":
        repository_root = Path(__file__).resolve().parents[3]
        return cls(repository_root / "runtime" / "navigation_odom_v1" / "manifest.json")

    def list_profiles(
        self,
        *,
        purpose: str = "processing",
    ) -> list[dict[str, str]]:
        profiles = self._purpose_profiles(purpose)
        return [
            profiles[key].public_projection()
            for key in sorted(profiles)
        ]

    def get(
        self,
        profile_ref: str,
        expected_sha256: str,
        *,
        purpose: str = "processing",
    ) -> CalibrationProfile:
        profile = self._purpose_profiles(purpose).get(profile_ref)
        if profile is None:
            raise AnnotationValidationError(
                "unknown_calibration_profile",
                f"The selected {purpose} calibration is not available.",
            )
        if profile.content_sha256 != expected_sha256:
            raise AnnotationConflictError(
                "calibration_profile_changed",
                "The selected calibration changed; refresh the profile list.",
                current=profile.public_projection(),
            )
        return profile

    def snapshot(
        self,
        profile: CalibrationProfile,
        destination: Path,
    ) -> tuple[list[dict[str, Any]], str]:
        if self.source_root is None:
            raise AnnotationValidationError(
                "runtime_source_unavailable",
                "The frozen runtime source is not configured.",
            )
        if destination.exists() or destination.is_symlink():
            raise AnnotationValidationError(
                "unsafe_snapshot_destination",
                "Calibration snapshot destination already exists.",
            )
        destination.mkdir(parents=True, mode=0o700)
        destination.chmod(0o700)
        destination.parent.chmod(0o700)
        captured: list[dict[str, Any]] = []
        for entry in profile.files:
            relative = Path(str(entry["relative_path"]))
            source = self.source_root / relative
            _require_regular_file(source, root=self.source_root)
            content, _source_mode, source_metadata = _read_frozen_regular_file(
                source,
                root=self.source_root,
            )
            actual = hashlib.sha256(content).hexdigest()
            if actual != entry["sha256"]:
                raise AnnotationConflictError(
                    "calibration_profile_changed",
                    "The selected calibration no longer matches the frozen manifest.",
                    current=profile.public_projection(),
                )
            target = destination / source.name
            _write_exclusive(target, content, mode=0o600)
            try:
                final_source_metadata = source.lstat()
            except OSError as exc:
                raise AnnotationConflictError(
                    "calibration_profile_changed",
                    "The selected calibration changed during snapshotting.",
                    current=profile.public_projection(),
                ) from exc
            if (
                stat.S_ISLNK(final_source_metadata.st_mode)
                or final_source_metadata.st_dev != source_metadata.st_dev
                or final_source_metadata.st_ino != source_metadata.st_ino
                or final_source_metadata.st_size != source_metadata.st_size
                or final_source_metadata.st_mtime_ns
                != source_metadata.st_mtime_ns
            ):
                raise AnnotationConflictError(
                    "calibration_profile_changed",
                    "The selected calibration changed during snapshotting.",
                    current=profile.public_projection(),
                )
            captured.append(
                {
                    "relative_path": target.name,
                    "sha256": actual,
                    "size": target.stat().st_size,
                }
            )
        aggregate = _canonical_sha256(captured)
        if aggregate != profile.content_sha256:
            raise AnnotationConflictError(
                "calibration_profile_changed",
                "The selected calibration snapshot is inconsistent.",
                current=profile.public_projection(),
            )
        return captured, aggregate

    def _purpose_profiles(self, purpose: str) -> dict[str, CalibrationProfile]:
        if purpose not in {"processing", "fix"}:
            raise AnnotationValidationError(
                "unsupported_calibration_inventory",
                "The selected calibration inventory is unavailable.",
            )
        return self._profiles_by_purpose[purpose]

    def _load(self) -> dict[str, dict[str, CalibrationProfile]]:
        try:
            manifest = load_manifest(self.manifest_path)
        except (RuntimeError, ValueError) as exc:
            raise RuntimeError("unable to read navigation runtime manifest") from exc
        grouped: dict[str, list[dict[str, Any]]] = {}
        for entry in manifest["entries"]:
            if entry["role"] != "selectable_profile":
                continue
            if (
                entry["kind"] != "frozen_file"
                or entry["root_alias"] != _PROFILE_ROOT_ALIAS
                or entry["stage"] not in _PROFILE_STAGES
            ):
                raise RuntimeError(
                    "selectable calibration manifest entry is invalid"
                )
            parts = Path(entry["relative_path"]).parts
            if (
                len(parts) != 5
                or parts[:2] != ("NoobScenes", "params")
                or parts[-2] != "sensors"
                or _PROFILE_REF_RE.fullmatch(parts[-3]) is None
            ):
                raise RuntimeError(
                    "selectable calibration manifest path is invalid"
                )
            profile_ref = parts[-3]
            grouped.setdefault(profile_ref, []).append(entry)
        if not grouped:
            raise RuntimeError("calibration inventory is empty")
        manifest_fingerprints: dict[str, dict[str, dict[str, Any]]] = {}
        for profile_ref, entries in grouped.items():
            if len({str(entry["stage"]) for entry in entries}) != 1:
                raise RuntimeError(
                    "calibration profile manifest stage is inconsistent"
                )
            filenames = {
                Path(str(entry["relative_path"])).name for entry in entries
            }
            if not _REQUIRED_SENSOR_FILES.issubset(filenames):
                raise RuntimeError(
                    "calibration profile is missing required sensor files"
                )
            if len(filenames) != len(entries):
                raise RuntimeError(
                    "calibration profile contains duplicate sensor files"
                )
            manifest_fingerprints[profile_ref] = {
                Path(str(entry["relative_path"])).name: {
                    "sha256": str(entry["sha256"]),
                    "size": int(entry["size"]),
                }
                for entry in entries
            }
        if self.source_root is not None:
            discovered = self._scan_frozen_profiles()
            if discovered != manifest_fingerprints:
                raise RuntimeError(
                    "calibration directory inventory differs from the frozen manifest"
                )
        result: dict[str, dict[str, CalibrationProfile]] = {
            "processing": {},
            "fix": {},
        }
        for purpose in ("processing", "fix"):
            for profile_ref, entries in grouped.items():
                ordered = sorted(entries, key=lambda item: item["relative_path"])
                file_fingerprints = [
                    {
                        "relative_path": Path(item["relative_path"]).name,
                        "sha256": item["sha256"],
                        "size": item["size"],
                    }
                    for item in ordered
                ]
                result[purpose][profile_ref] = CalibrationProfile(
                    profile_ref=profile_ref,
                    label=profile_ref,
                    content_sha256=_canonical_sha256(file_fingerprints),
                    files=tuple(ordered),
                )
        return result

    def _scan_frozen_profiles(
        self,
    ) -> dict[str, dict[str, dict[str, Any]]]:
        assert self.source_root is not None
        source_root = self.source_root.absolute()
        params_root = source_root / "NoobScenes" / "params"
        _require_real_directory(
            source_root,
            error="frozen runtime source is unsafe",
        )
        _require_real_directory(
            source_root / "NoobScenes",
            error="frozen calibration parent is unsafe",
        )
        _require_real_directory(
            params_root,
            error="frozen calibration directory is unsafe",
        )
        try:
            children = sorted(params_root.iterdir(), key=lambda item: item.name)
        except OSError as exc:
            raise RuntimeError(
                "unable to scan frozen calibration directory"
            ) from exc
        discovered: dict[str, dict[str, dict[str, Any]]] = {}
        for profile_root in children:
            if _PROFILE_REF_RE.fullmatch(profile_root.name) is None:
                raise RuntimeError("frozen calibration profile name is invalid")
            _require_real_directory(
                profile_root,
                error="frozen calibration profile is unsafe",
            )
            sensors_root = profile_root / "sensors"
            _require_real_directory(
                sensors_root,
                error="frozen calibration sensors are unsafe",
            )
            try:
                sensor_entries = sorted(
                    sensors_root.iterdir(),
                    key=lambda item: item.name,
                )
            except OSError as exc:
                raise RuntimeError(
                    "unable to scan frozen calibration sensors"
                ) from exc
            files: dict[str, dict[str, Any]] = {}
            for sensor_file in sensor_entries:
                try:
                    metadata = sensor_file.lstat()
                except OSError as exc:
                    raise RuntimeError(
                        "unable to inspect frozen calibration file"
                    ) from exc
                if (
                    stat.S_ISLNK(metadata.st_mode)
                    or not stat.S_ISREG(metadata.st_mode)
                ):
                    raise RuntimeError("frozen calibration file is unsafe")
                try:
                    content, _mode, _metadata = _read_frozen_regular_file(
                        sensor_file,
                        root=source_root,
                    )
                except (
                    AnnotationValidationError,
                    AnnotationConflictError,
                ) as exc:
                    raise RuntimeError(
                        "frozen calibration file is unsafe"
                    ) from exc
                files[sensor_file.name] = {
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "size": len(content),
                }
            if not _REQUIRED_SENSOR_FILES.issubset(files):
                raise RuntimeError(
                    "frozen calibration profile is missing required sensor files"
                )
            discovered[profile_root.name] = files
        if not discovered:
            raise RuntimeError("frozen calibration inventory is empty")
        return discovered


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _require_real_directory(path: Path, *, error: str) -> None:
    path_lexical = path.absolute()
    try:
        metadata = path_lexical.lstat()
        resolved = path_lexical.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError(error) from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or resolved != path_lexical
    ):
        raise RuntimeError(error)


def _require_regular_file(path: Path, *, root: Path) -> None:
    root_lexical = root.absolute()
    path_lexical = path.absolute()
    try:
        relative = path_lexical.relative_to(root_lexical)
        root_metadata = root_lexical.lstat()
    except (ValueError, OSError) as exc:
        raise AnnotationValidationError(
            "unsafe_calibration_path",
            "Calibration path escaped the frozen runtime.",
        ) from exc
    if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
        raise AnnotationValidationError(
            "unsafe_calibration_path",
            "The frozen runtime root must be a real directory.",
        )
    cursor = root_lexical
    for component in relative.parts:
        cursor = cursor / component
        try:
            metadata = cursor.lstat()
        except OSError as exc:
            raise AnnotationValidationError(
                "calibration_file_unavailable",
                "A selected calibration file is unavailable.",
            ) from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise AnnotationValidationError(
                "unsafe_calibration_file",
                "Calibration paths cannot contain symlinks.",
            )
    root_resolved = root_lexical.resolve(strict=True)
    try:
        path_resolved = path_lexical.resolve(strict=True)
    except OSError as exc:
        raise AnnotationValidationError(
            "calibration_file_unavailable",
            "A selected calibration file is unavailable.",
        ) from exc
    if path_resolved == root_resolved or root_resolved not in path_resolved.parents:
        raise AnnotationValidationError(
            "unsafe_calibration_path",
            "Calibration path escaped the frozen runtime.",
        )
    metadata = path_lexical.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        raise AnnotationValidationError(
            "unsafe_calibration_file",
            "Calibration files must be regular files.",
        )


def _read_frozen_regular_file(
    path: Path,
    *,
    root: Path,
) -> tuple[bytes, int, os.stat_result]:
    root_lexical = root.absolute()
    path_lexical = path.absolute()
    try:
        relative = path_lexical.relative_to(root_lexical)
    except ValueError as exc:
        raise AnnotationValidationError(
            "unsafe_calibration_path",
            "Calibration path escaped the frozen runtime.",
        ) from exc
    directory_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        directory_flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
    file_flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        file_flags |= os.O_NOFOLLOW
    descriptors: list[int] = []
    try:
        directory_fd = os.open(root_lexical, directory_flags)
        descriptors.append(directory_fd)
        for component in relative.parts[:-1]:
            directory_fd = os.open(
                component,
                directory_flags,
                dir_fd=directory_fd,
            )
            descriptors.append(directory_fd)
        file_fd = os.open(relative.parts[-1], file_flags, dir_fd=directory_fd)
        before = os.fstat(file_fd)
        if not stat.S_ISREG(before.st_mode):
            os.close(file_fd)
            raise AnnotationValidationError(
                "unsafe_calibration_file",
                "Calibration files must be regular files.",
            )
        with os.fdopen(file_fd, "rb") as handle:
            content = handle.read()
            after = os.fstat(handle.fileno())
        if (
            before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or len(content) != before.st_size
        ):
            raise AnnotationConflictError(
                "calibration_profile_changed",
                "The selected calibration changed during snapshotting.",
            )
        return content, stat.S_IMODE(before.st_mode), before
    except (AnnotationValidationError, AnnotationConflictError):
        raise
    except OSError as exc:
        raise AnnotationValidationError(
            "calibration_file_unavailable",
            "A selected calibration file is unavailable.",
        ) from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _write_exclusive(path: Path, content: bytes, *, mode: int) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, mode)
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise AnnotationValidationError(
            "unsafe_snapshot_destination",
            "Calibration snapshot destination already exists.",
        ) from exc


def _optional_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value) if value else None

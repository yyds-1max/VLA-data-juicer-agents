from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Mapping

from .execution import VERSION_MARKER


VERSION_MARKER_CONTRACT = "datapilot_training_version_v1"
_SAFE_REF = re.compile(r"[A-Za-z0-9_.:-]{1,255}\Z")
_STAGE_DIRECTORY = re.compile(r"stage-(?:0[1-9]|10)\Z")
_MAX_MARKER_BYTES = 16 * 1024


class ArtifactInspectionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def inspect_training_artifact(payload: Mapping[str, object]) -> dict[str, object]:
    """Inspect a managed version artifact without reading model files."""

    artifact_ref = _required_ref(payload.get("artifact_ref"), "artifact_ref")
    run_ref = _required_ref(payload.get("run_ref"), "run_ref")
    version_ref = _required_ref(payload.get("version_ref"), "version_ref")
    version_label = _required_ref(payload.get("version_label"), "version_label")
    output_root = _required_path(payload.get("output_root"), "output_root")
    artifact_path = _required_path(payload.get("artifact_path"), "artifact_path")
    identity = {
        "artifact_ref": artifact_ref,
        "version_ref": version_ref,
    }

    relative_parts = _artifact_relative_parts(
        artifact_path, output_root, version_label
    )
    if relative_parts is None:
        return _availability(identity, "unsafe", "artifact_path_mismatch")
    family_ref = relative_parts[0]

    path_safety = _path_components_status(output_root, artifact_path)
    if path_safety != "safe":
        return _availability(identity, path_safety, f"artifact_{path_safety}")
    if not os.access(artifact_path, os.R_OK | os.X_OK):
        return _availability(identity, "unreadable", "artifact_unreadable")

    version_root = output_root / family_ref / version_label
    marker_status = _verify_version_marker(version_root, run_ref, version_label)
    if marker_status is not None:
        return _availability(identity, marker_status, f"artifact_marker_{marker_status}")

    try:
        file_count, total_bytes = _inventory_regular_files(artifact_path)
    except _UnsafeArtifact:
        return _availability(identity, "unsafe", "artifact_contains_symlink")
    except PermissionError:
        return _availability(identity, "unreadable", "artifact_unreadable")
    except OSError as exc:
        raise ArtifactInspectionError(
            "artifact_inspection_failed", "The Worker could not inspect the artifact."
        ) from exc
    return {
        "status": "succeeded",
        **identity,
        "availability": "available",
        "file_count": file_count,
        "total_bytes": total_bytes,
    }


def _required_ref(value: object, label: str) -> str:
    if not isinstance(value, str) or not _SAFE_REF.fullmatch(value):
        raise ArtifactInspectionError(
            "artifact_inspection_request_invalid",
            f"{label} is invalid.",
        )
    return value


def _required_path(value: object, label: str) -> Path:
    if not isinstance(value, str):
        raise ArtifactInspectionError(
            "artifact_inspection_request_invalid", f"{label} is invalid."
        )
    if (
        not value.startswith("/")
        or len(value) > 4096
        or any(character in value for character in ("\x00", "\r", "\n"))
        or any(part in {".", ".."} for part in PurePosixPath(value).parts)
    ):
        raise ArtifactInspectionError(
            "artifact_inspection_request_invalid",
            f"{label} must be a safe absolute path.",
        )
    return Path(value)


def _artifact_relative_parts(
    artifact_path: Path, output_root: Path, version_label: str
) -> tuple[str, str, str] | None:
    try:
        parts = artifact_path.relative_to(output_root).parts
    except ValueError:
        return None
    if (
        len(parts) != 3
        or not _SAFE_REF.fullmatch(parts[0])
        or parts[1] != version_label
        or not _STAGE_DIRECTORY.fullmatch(parts[2])
    ):
        return None
    return parts[0], parts[1], parts[2]


def _path_components_status(output_root: Path, artifact_path: Path) -> str:
    current = Path(output_root.anchor)
    parts = (*output_root.parts[1:], *artifact_path.relative_to(output_root).parts)
    for part in parts:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            return "missing"
        except PermissionError:
            return "unreadable"
        except OSError as exc:
            raise ArtifactInspectionError(
                "artifact_inspection_failed", "The Worker could not inspect the artifact."
            ) from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            return "unsafe"
    return "safe"


def _verify_version_marker(
    version_root: Path, run_ref: str, version_label: str
) -> str | None:
    marker_path = version_root / VERSION_MARKER
    try:
        metadata = marker_path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            return "unsafe"
        if not os.access(marker_path, os.R_OK):
            return "unreadable"
        if metadata.st_size > _MAX_MARKER_BYTES:
            return "unsafe"
        raw_marker = marker_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "unsafe"
    except PermissionError:
        return "unreadable"
    except (OSError, UnicodeDecodeError):
        return "unsafe"
    try:
        marker = json.loads(raw_marker)
    except json.JSONDecodeError:
        return "unsafe"
    if not isinstance(marker, dict):
        return "unsafe"
    if (
        marker.get("contract") != VERSION_MARKER_CONTRACT
        or marker.get("run_ref") != run_ref
        or marker.get("version_label") != version_label
    ):
        return "unsafe"
    return None


class _UnsafeArtifact(RuntimeError):
    pass


def _inventory_regular_files(root: Path) -> tuple[int, int]:
    file_count = 0
    total_bytes = 0
    pending = [root]
    while pending:
        directory = pending.pop()
        if not os.access(directory, os.R_OK | os.X_OK):
            raise PermissionError
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.is_symlink():
                    raise _UnsafeArtifact
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                    continue
                if entry.is_file(follow_symlinks=False):
                    stat_result = entry.stat(follow_symlinks=False)
                    file_count += 1
                    total_bytes += stat_result.st_size
    return file_count, total_bytes


def _availability(
    identity: Mapping[str, str], availability: str, reason: str
) -> dict[str, object]:
    return {
        "status": "succeeded",
        **identity,
        "availability": availability,
        "reason": reason,
        "file_count": None,
        "total_bytes": None,
    }

"""M2 adapter for the frozen navigation trajectory Fix implementation."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
from typing import Any

from vla_data_juicer_agents.annotation.postprocessing_runtime import (
    _fsync_directory,
    _write_journal,
)
from vla_data_juicer_agents.annotation.runtime import (
    CalibrationSnapshotFile,
    NavigationAnnotationRuntimeConfig,
    RuntimeExecutionError,
    RuntimeStepObserver,
    _OPAQUE_REF_RE,
    _RuntimeBase,
    _active_payload_permissions_safe,
    _copy_file_bytes,
    _copy_tree_bytes,
    _ensure_private_directory,
    _ensure_private_directory_chain,
    _harden_private_tree,
    _manifest_object_without_duplicate_keys,
    _observed_runtime_step,
    _read_manifest_frozen_bytes,
    _read_stable_regular_bytes,
    _regular_directory,
    _regular_file,
    _safe_component,
    _sha256_file,
    _tree_sha256,
    _write_bytes_exclusive,
)
from vla_data_juicer_agents.navigation.runtime_manifest import validate_manifest
from vla_data_juicer_agents.navigation.writer_lock import (
    navigation_writer_lock,
)
from vla_data_juicer_agents.annotation.models import FixRuntimeState


FIX_COMMAND_STEP = "fix_candidate"
FIX_PUBLICATION_STEP = "fix_compatibility_publish"
_FIX_MODULE_PATH = (
    "other_code/fix_trajectory_five_add_SF_odom_gridmap_0525.py"
)
_FIX_MODULE_ROLE = "active_runtime_legacy_gui"
_FIX_MODULE_STAGE = "fix"
_TARGET_REF_RE = re.compile(r"^target_[0-9a-f]{32}$")
_TARGET_TYPE_RE = re.compile(r"^(?:master|other[0-9]+)$")
_HEX_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REQUIRED_SENSOR_FILES = (
    "fisheye_front.json",
    "r32_rslidar_points.json",
)


@dataclass(frozen=True)
class FixRequest:
    review_ref: str
    run_ref: str
    attempt: int
    base_segment_root: Path
    expected_base_tree_sha256: str
    calibration_snapshot_ref: str
    calibration_snapshot_dir: Path
    calibration_snapshot_files: tuple[CalibrationSnapshotFile, ...]
    calibration_snapshot_sha256: str
    target_bindings: dict[str, str]
    commands: tuple[dict[str, Any], ...]
    expected_runtime_manifest_sha256: str
    step_observer: RuntimeStepObserver | None = field(
        default=None,
        repr=False,
        compare=False,
    )


@dataclass(frozen=True)
class FixResult:
    attempt_root: Path
    candidate_segment_root: Path
    fix_trajectory_path: Path
    fix_trajectory_sha256: str
    base_tree_sha256: str
    calibration_snapshot_sha256: str
    command_log_sha256: str
    adapter_sha256: str
    runtime_manifest_sha256: str
    command_steps: tuple[str, ...] = (FIX_COMMAND_STEP,)


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


class CommandLogFixDraftAdapter:
    """Persist exact Web commands without reimplementing legacy trajectory math.

    The frozen legacy implementation is invoked only by ``NavigationFixRuntime``
    when a revision is requested.  Draft autosave therefore remains cheap and
    deterministic while the training artifact is still produced exclusively
    by the frozen business code.
    """

    @staticmethod
    def initialize(
        trajectory_state: dict[str, Any],
        *,
        calibration_snapshot: dict[str, Any],
    ) -> FixRuntimeState:
        bindings = trajectory_state.get("target_bindings")
        if not isinstance(bindings, dict) or not bindings:
            raise ValueError("trajectory state lacks target bindings")
        state = {
            "schema_version": 1,
            "trajectory_revision_state_sha256": hashlib.sha256(
                _canonical_json_bytes(trajectory_state),
            ).hexdigest(),
            "calibration_snapshot_ref": calibration_snapshot["snapshot_ref"],
            "calibration_profile_ref": calibration_snapshot["profile_ref"],
            "commands": [],
        }
        return FixRuntimeState(
            state=state,
            content_sha256=hashlib.sha256(
                _canonical_json_bytes(state),
            ).hexdigest(),
        )

    @staticmethod
    def apply(
        current_state: dict[str, Any],
        command: Any,
    ) -> FixRuntimeState:
        if (
            current_state.get("schema_version") != 1
            or not isinstance(current_state.get("commands"), list)
        ):
            raise ValueError("Fix draft state is invalid")
        command_payload = (
            command.model_dump(mode="json")
            if hasattr(command, "model_dump")
            else command
        )
        if not isinstance(command_payload, dict):
            raise ValueError("Fix command is invalid")
        state = json.loads(_canonical_json_bytes(current_state))
        state["commands"].append(
            json.loads(_canonical_json_bytes(command_payload)),
        )
        return FixRuntimeState(
            state=state,
            content_sha256=hashlib.sha256(
                _canonical_json_bytes(state),
            ).hexdigest(),
        )


def _require_sha256(value: str, *, label: str) -> None:
    if _HEX_SHA256_RE.fullmatch(value) is None:
        raise RuntimeExecutionError(
            "invalid_runtime_request",
            f"{label} is invalid.",
        )


def _read_fix_calibration_snapshot(
    *,
    config: NavigationAnnotationRuntimeConfig,
    request: FixRequest,
) -> dict[str, bytes]:
    assert config.work_root is not None
    _require_sha256(
        request.calibration_snapshot_sha256,
        label="Fix calibration snapshot hash",
    )
    expected_root = (
        config.work_root
        / "reviews"
        / request.review_ref
        / "calibration"
        / request.calibration_snapshot_ref
    )
    try:
        if (
            request.calibration_snapshot_dir.absolute()
            != expected_root.absolute()
            or request.calibration_snapshot_dir.resolve(strict=True)
            != expected_root.resolve(strict=True)
        ):
            raise RuntimeExecutionError(
                "calibration_snapshot_mismatch",
                "The Fix calibration snapshot is not owned by this review.",
            )
    except OSError as exc:
        raise RuntimeExecutionError(
            "calibration_snapshot_mismatch",
            "The Fix calibration snapshot is unavailable.",
        ) from exc
    expected_names = tuple(sorted(_REQUIRED_SENSOR_FILES))
    files = request.calibration_snapshot_files
    if (
        tuple(item.relative_path for item in files) != expected_names
        or len(files) != len(expected_names)
    ):
        raise RuntimeExecutionError(
            "calibration_snapshot_mismatch",
            "The Fix calibration snapshot ledger is incomplete.",
        )
    actual_names = tuple(sorted(path.name for path in expected_root.iterdir()))
    if actual_names != expected_names:
        raise RuntimeExecutionError(
            "calibration_snapshot_mismatch",
            "The Fix calibration snapshot differs from its ledger.",
        )
    contents: dict[str, bytes] = {}
    ledger: list[dict[str, str | int]] = []
    for item in files:
        if (
            Path(item.relative_path).name != item.relative_path
            or isinstance(item.size, bool)
            or not isinstance(item.size, int)
            or item.size < 0
        ):
            raise RuntimeExecutionError(
                "calibration_snapshot_mismatch",
                "A Fix calibration snapshot ledger entry is invalid.",
            )
        _require_sha256(item.sha256, label="Fix calibration file hash")
        path = expected_root / item.relative_path
        try:
            content = _read_stable_regular_bytes(path)
        except OSError as exc:
            raise RuntimeExecutionError(
                "calibration_snapshot_mismatch",
                "A Fix calibration snapshot file cannot be verified.",
            ) from exc
        if len(content) != item.size or hashlib.sha256(content).hexdigest() != (
            item.sha256
        ):
            raise RuntimeExecutionError(
                "calibration_snapshot_mismatch",
                "A Fix calibration snapshot file differs from its ledger.",
            )
        contents[item.relative_path] = content
        ledger.append(
            {
                "relative_path": item.relative_path,
                "sha256": item.sha256,
                "size": item.size,
            }
        )
    if hashlib.sha256(_canonical_json_bytes(ledger)).hexdigest() != (
        request.calibration_snapshot_sha256
    ):
        raise RuntimeExecutionError(
            "calibration_snapshot_mismatch",
            "The Fix calibration snapshot aggregate differs from its ledger.",
        )
    return contents


def _remove_staged_sensors(segment_root: Path) -> None:
    sensors = segment_root / "sensors"
    if not sensors.exists() and not sensors.is_symlink():
        return
    metadata = sensors.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeExecutionError(
            "unsafe_runtime_output",
            "The staged calibration location is unsafe.",
        )
    shutil.rmtree(sensors)


def _fix_output(segment_root: Path) -> Path:
    candidates = sorted(
        path
        for path in segment_root.glob("*_trajectory_fix_five.json")
        if path.is_file() and not path.is_symlink()
    )
    if len(candidates) != 1:
        raise RuntimeExecutionError(
            "fix_outputs_missing",
            "The frozen Fix Runtime did not produce exactly one candidate.",
        )
    return candidates[0]


class NavigationFixRuntime(_RuntimeBase):
    def preflight(self) -> str:
        """Prove the frozen Fix payload before creating a durable session/run."""

        self._require_available()
        try:
            content = _read_stable_regular_bytes(self.config.manifest_path)
        except OSError as exc:
            raise RuntimeExecutionError(
                "runtime_manifest_changed",
                "The frozen Runtime manifest cannot be safely read.",
            ) from exc
        manifest_sha256 = hashlib.sha256(content).hexdigest()
        self._revalidate_fix_input(manifest_sha256)
        return manifest_sha256

    def _revalidate_fix_input(self, expected_manifest_sha256: str) -> None:
        config = self.config
        assert config.runtime_source_root is not None
        try:
            content = _read_stable_regular_bytes(config.manifest_path)
            if hashlib.sha256(content).hexdigest() != expected_manifest_sha256:
                raise ValueError("manifest changed")
            document = json.loads(
                content.decode("utf-8"),
                object_pairs_hook=_manifest_object_without_duplicate_keys,
            )
            manifest = validate_manifest(document)
            matching = [
                entry
                for entry in manifest["entries"]
                if entry.get("root_alias") == "NAVIGATION_ODOM_V1_SOURCE"
                and entry.get("kind") == "frozen_file"
                and entry.get("relative_path") == _FIX_MODULE_PATH
            ]
            if (
                len(matching) != 1
                or matching[0].get("role") != _FIX_MODULE_ROLE
                or matching[0].get("stage") != _FIX_MODULE_STAGE
            ):
                raise ValueError("frozen Fix entry is invalid")
            entries = {_FIX_MODULE_PATH: matching[0]}
            if not _active_payload_permissions_safe(
                config.runtime_source_root,
                entries,
            ):
                raise ValueError("frozen Fix permissions are unsafe")
            _read_manifest_frozen_bytes(
                config.runtime_source_root,
                matching[0],
            )
        except (KeyError, OSError, TypeError, ValueError) as exc:
            raise RuntimeExecutionError(
                "runtime_input_changed",
                "The frozen Fix Runtime changed before execution.",
            ) from exc

    def run(self, request: FixRequest) -> FixResult:
        self._require_available()
        config = self.config
        assert config.work_root is not None
        assert config.runtime_source_root is not None
        assert config.data_python is not None
        _safe_component(
            request.review_ref,
            label="review_ref",
            pattern=_OPAQUE_REF_RE,
        )
        _safe_component(request.run_ref, label="run_ref", pattern=_OPAQUE_REF_RE)
        _safe_component(
            request.calibration_snapshot_ref,
            label="calibration snapshot ref",
            pattern=_OPAQUE_REF_RE,
        )
        if request.attempt < 1:
            raise RuntimeExecutionError(
                "invalid_runtime_request",
                "Fix Runtime attempt must be positive.",
            )
        _require_sha256(
            request.expected_runtime_manifest_sha256,
            label="Runtime manifest hash",
        )
        _require_sha256(
            request.expected_base_tree_sha256,
            label="Base trajectory artifact hash",
        )
        base_root = self._assert_under(
            request.base_segment_root,
            config.work_root,
            label="base trajectory artifact",
        )
        if _tree_sha256(
            base_root,
            unsafe_code="trajectory_revision_changed",
        ) != request.expected_base_tree_sha256:
            raise RuntimeExecutionError(
                "trajectory_revision_changed",
                "The base TrajectoryRevision artifact changed before Fix.",
            )
        if (
            not request.target_bindings
            or any(
                _TARGET_REF_RE.fullmatch(target_ref) is None
                or _TARGET_TYPE_RE.fullmatch(target_type) is None
                for target_ref, target_type in request.target_bindings.items()
            )
            or len(set(request.target_bindings.values()))
            != len(request.target_bindings)
        ):
            raise RuntimeExecutionError(
                "invalid_runtime_request",
                "Fix target bindings are invalid.",
            )
        try:
            commands_payload = [
                json.loads(_canonical_json_bytes(command))
                for command in request.commands
            ]
            command_log = _canonical_json_bytes(commands_payload)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeExecutionError(
                "invalid_runtime_request",
                "The Fix command log is invalid.",
            ) from exc
        if len(command_log) > 2 * 1024 * 1024:
            raise RuntimeExecutionError(
                "invalid_runtime_request",
                "The Fix command log is too large.",
            )
        calibration_contents = _read_fix_calibration_snapshot(
            config=config,
            request=request,
        )
        runtime_manifest_sha256 = self._current_runtime_manifest_sha256(
            request.expected_runtime_manifest_sha256,
        )
        self._revalidate_fix_input(request.expected_runtime_manifest_sha256)

        attempt_root = (
            config.work_root
            / "reviews"
            / request.review_ref
            / "fix"
            / request.run_ref
        )
        if attempt_root.exists() or attempt_root.is_symlink():
            raise RuntimeExecutionError(
                "recovery_required",
                "This Fix attempt already exists and requires review.",
            )
        _ensure_private_directory_chain(
            config.work_root,
            ("reviews", request.review_ref, "fix"),
        )
        _ensure_private_directory(attempt_root, create=True)
        candidate_segment = attempt_root / "segment"
        _copy_tree_bytes(base_root, candidate_segment)
        _remove_staged_sensors(candidate_segment)
        sensors = _ensure_private_directory_chain(
            candidate_segment,
            ("sensors",),
        )
        for filename in _REQUIRED_SENSOR_FILES:
            _write_bytes_exclusive(sensors / filename, calibration_contents[filename])
        # Presence here removes the frozen editor's legacy silent fallback to
        # 20260409_U; the selected snapshot is always explicit and auditable.
        if not _regular_file(sensors / "fisheye_front.json"):
            raise RuntimeExecutionError(
                "calibration_snapshot_mismatch",
                "The selected Fix calibration was not staged.",
            )

        driver_path = Path(__file__).with_name("legacy_fix_driver.py")
        try:
            adapter_sha256 = _sha256_file(driver_path)
        except OSError as exc:
            raise RuntimeExecutionError(
                "fix_adapter_changed",
                "The system Fix adapter cannot be verified.",
            ) from exc
        driver_request = {
            "schema_version": 1,
            "legacy_module_path": str(
                config.runtime_source_root / _FIX_MODULE_PATH
            ),
            "segment_root": str(candidate_segment),
            "target_bindings": dict(sorted(request.target_bindings.items())),
            "commands": commands_payload,
        }
        request_path = attempt_root / "request.json"
        _write_bytes_exclusive(
            request_path,
            _canonical_json_bytes(driver_request) + b"\n",
        )

        with navigation_writer_lock(lock_path=config.writer_lock_path):
            with _observed_runtime_step(
                request.step_observer,
                FIX_COMMAND_STEP,
            ):
                self._revalidate_fix_input(
                    request.expected_runtime_manifest_sha256
                )
                if _sha256_file(driver_path) != adapter_sha256:
                    raise RuntimeExecutionError(
                        "fix_adapter_changed",
                        "The system Fix adapter changed before execution.",
                    )
                self._run_checked(
                    staging_root=attempt_root,
                    argv=[
                        config.data_python,
                        driver_path,
                        "--request",
                        request_path,
                    ],
                    cwd=driver_path.parent,
                    error_code="fix_runtime_failed",
                )
                self._revalidate_fix_input(
                    request.expected_runtime_manifest_sha256
                )
                if _sha256_file(driver_path) != adapter_sha256:
                    raise RuntimeExecutionError(
                        "fix_adapter_changed",
                        "The system Fix adapter changed during execution.",
                    )

        output = _fix_output(candidate_segment)
        output_sha256 = _sha256_file(output)
        _harden_private_tree(attempt_root)
        return FixResult(
            attempt_root=attempt_root,
            candidate_segment_root=candidate_segment,
            fix_trajectory_path=output,
            fix_trajectory_sha256=output_sha256,
            base_tree_sha256=request.expected_base_tree_sha256,
            calibration_snapshot_sha256=request.calibration_snapshot_sha256,
            command_log_sha256=hashlib.sha256(command_log).hexdigest(),
            adapter_sha256=adapter_sha256,
            runtime_manifest_sha256=runtime_manifest_sha256,
        )


@dataclass(frozen=True)
class FixPublicationResult:
    published_path: Path
    content_sha256: str
    journal_path: Path
    command_steps: tuple[str, ...] = (FIX_PUBLICATION_STEP,)


class FixCompatibilityPublisher:
    """Publish an approved FixRevision without replacing a differing revision."""

    def publish_bound_revision(
        self,
        *,
        review_ref: str,
        revision_ref: str,
        candidate_segment_root: Path,
        expected_candidate_tree_sha256: str,
        expected_fix_sha256: str,
        target_segment_root: Path,
        journal_root: Path,
        writer_lock_path: Path,
    ) -> dict[str, str]:
        if _tree_sha256(
            candidate_segment_root,
            unsafe_code="fix_revision_changed",
        ) != expected_candidate_tree_sha256:
            raise RuntimeExecutionError(
                "fix_revision_changed",
                "The approved FixRevision artifact changed before publication.",
            )
        source = _fix_output(candidate_segment_root)
        if _sha256_file(source) != expected_fix_sha256:
            raise RuntimeExecutionError(
                "fix_revision_changed",
                "The approved FixRevision file changed before publication.",
            )
        _ensure_private_directory(journal_root, create=True)
        with navigation_writer_lock(lock_path=writer_lock_path):
            result = self.publish(
                review_ref=review_ref,
                revision_ref=revision_ref,
                source_path=source,
                expected_sha256=expected_fix_sha256,
                target_segment_root=target_segment_root,
                journal_root=journal_root,
            )
        return {
            "content_sha256": result.content_sha256,
            "private_artifact_path": str(result.published_path),
        }

    def publish(
        self,
        *,
        review_ref: str,
        revision_ref: str,
        source_path: Path,
        expected_sha256: str,
        target_segment_root: Path,
        journal_root: Path,
    ) -> FixPublicationResult:
        _safe_component(review_ref, label="review_ref", pattern=_OPAQUE_REF_RE)
        _safe_component(
            revision_ref,
            label="revision_ref",
            pattern=_OPAQUE_REF_RE,
        )
        _require_sha256(expected_sha256, label="FixRevision hash")
        if not _regular_file(source_path):
            raise RuntimeExecutionError(
                "fix_revision_unavailable",
                "The approved FixRevision artifact is unavailable.",
            )
        if _sha256_file(source_path) != expected_sha256:
            raise RuntimeExecutionError(
                "fix_revision_changed",
                "The approved FixRevision artifact changed before publication.",
            )
        if not _regular_directory(target_segment_root):
            raise RuntimeExecutionError(
                "publication_target_unavailable",
                "The compatibility segment root is unavailable.",
            )
        _ensure_private_directory(journal_root)
        journal_path = journal_root / f"fix-publication-{revision_ref}.json"
        if journal_path.exists() or journal_path.is_symlink():
            raise RuntimeExecutionError(
                "publication_recovery_required",
                "This Fix publication already has a journal.",
            )
        target = target_segment_root / source_path.name
        journal: dict[str, object] = {
            "schema_version": 1,
            "review_ref": review_ref,
            "revision_ref": revision_ref,
            "sha256": expected_sha256,
            "state": "intent",
        }
        _write_journal(journal_path, journal)
        if target.exists() or target.is_symlink():
            if (
                not _regular_file(target)
                or _sha256_file(target) != expected_sha256
            ):
                raise RuntimeExecutionError(
                    "publication_conflict",
                    "An existing training compatibility file differs.",
                )
        else:
            temporary = target_segment_root / f".{source_path.name}.{revision_ref}"
            if temporary.exists() or temporary.is_symlink():
                raise RuntimeExecutionError(
                    "publication_recovery_required",
                    "A Fix publication temporary file already exists.",
                )
            _copy_file_bytes(source_path, temporary)
            if _sha256_file(temporary) != expected_sha256:
                raise RuntimeExecutionError(
                    "fix_revision_changed",
                    "The FixRevision changed while being published.",
                )
            os.replace(temporary, target)
            _fsync_directory(target_segment_root)
        journal["state"] = "committed"
        _write_journal(journal_path, journal)
        return FixPublicationResult(
            published_path=target,
            content_sha256=expected_sha256,
            journal_path=journal_path,
        )

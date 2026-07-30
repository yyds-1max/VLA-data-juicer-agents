"""Isolated M2 postprocessing Runtime for navigation annotation jobs.

The module deliberately contains orchestration only.  Projection, motion,
gridmap conversion, trajectory generation, and final layout remain implemented
by the frozen ``navigation_odom_v1`` payload.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Iterator, Literal, Sequence

from vla_data_juicer_agents.annotation.runtime import (
    NavigationAnnotationRuntimeConfig,
    RuntimeExecutionError,
    RuntimeStepObserver,
    TrackingTarget,
    _CLIP_RE,
    _DATE_RE,
    _OPAQUE_REF_RE,
    _RuntimeBase,
    _active_payload_permissions_safe,
    _copy_tree_bytes,
    _ensure_private_directory,
    _ensure_private_directory_chain,
    _harden_private_tree,
    _manifest_object_without_duplicate_keys,
    _observed_runtime_step,
    _regular_directory,
    _read_manifest_frozen_bytes,
    _read_stable_regular_bytes,
    _safe_component,
    _sha256_file,
    _tree_sha256,
    prepared_staging_artifact_sha256,
)
from vla_data_juicer_agents.navigation.runtime_manifest import validate_manifest
from vla_data_juicer_agents.navigation.config import NavigationSettings
from vla_data_juicer_agents.navigation.execution_tools import (
    validate_navigation_outputs,
)
from vla_data_juicer_agents.navigation.writer_lock import (
    active_writer_lock_fds,
    navigation_writer_lock,
)


GridmapDecision = Literal[
    "copy_existing_gridmap",
    "generate_from_pcd",
    "skip_if_projection_ready",
]
LocalizationKind = Literal["odom", "ins"]
TrajectoryVariant = Literal["cjl_with_gridmap", "cjl_0525_with_gridmap"]


@contextmanager
def _require_active_writer_lease() -> Iterator[None]:
    """Assert that the outer Runtime wrapper owns the one continuous lease."""

    if not active_writer_lock_fds():
        raise RuntimeExecutionError(
            "writer_lock_not_held",
            "The navigation writer safety lease is unavailable.",
        )
    yield

POSTPROCESSING_COMMAND_STEPS = (
    "postprocess_input_snapshot",
    "postprocess_metadata",
    "postprocess_gridmap",
    "postprocess_projection",
    "postprocess_world_coordinates",
    "postprocess_speed_direction",
    "postprocess_gridmap_transform",
    "postprocess_trajectory",
    "postprocess_final_candidate",
    "postprocess_validate_outputs",
)

_POSTPROCESSING_FROZEN_METADATA = {
    "NuscenesAanlysis_smart_pts_project/main.py": (
        "active_runtime",
        "projection",
    ),
    "NuscenesAanlysis_smart_pts_project/helper.py": (
        "active_runtime",
        "projection",
    ),
    "NuscenesAanlysis_smart_pts_project/tool/__init__.py": (
        "active_runtime",
        "projection",
    ),
    "NuscenesAanlysis_smart_pts_project/tool/cam2pixel_model.py": (
        "active_runtime",
        "projection",
    ),
    "2_pt_project/0_img2world.py": ("active_runtime", "projection"),
    "2_pt_project/4_speed_direction_odom.py": ("active_runtime", "motion"),
    "other_code/pcd_to_grid.py": ("active_auxiliary", "gridmap_prepare"),
    "other_code/cp_gridmap.py": ("active_runtime", "gridmap_transform"),
    "2_pt_project/2_othermethod_cjl_0525.py": (
        "active_runtime",
        "trajectory",
    ),
    "2_pt_project/3_move_dir.py": ("active_runtime", "publish"),
}

_IDENTITY_RE = re.compile(
    r"^(?:master|other[0-9]+)_[a-z]+_[a-z]+_[a-z]+$",
)
_HEX_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class PostprocessingSegmentInput:
    """One tracked internal segment bound by the Annotation Store."""

    segment_ref: str
    source_clip: str
    private_segment_key: str
    tracked_segment_root: Path
    expected_tree_sha256: str
    tracking_identities: tuple[str, ...]


@dataclass(frozen=True)
class PostprocessingRequest:
    job_ref: str
    run_ref: str
    attempt: int
    dataset_date: str
    tracked_staging_root: Path
    segments: tuple[PostprocessingSegmentInput, ...]
    gridmap_decision: GridmapDecision
    localization_kind: LocalizationKind
    trajectory_variant: TrajectoryVariant
    expected_runtime_manifest_sha256: str
    expected_prepared_artifact_tree_sha256: str
    step_observer: RuntimeStepObserver | None = field(
        default=None,
        repr=False,
        compare=False,
    )


@dataclass(frozen=True)
class TrajectoryCandidate:
    segment_ref: str
    source_clip: str
    private_segment_key: str
    candidate_segment_root: Path
    trajectory_path: Path
    trajectory_sha256: str
    speed_direction_path: Path
    speed_direction_sha256: str


@dataclass(frozen=True)
class PostprocessingResult:
    attempt_root: Path
    finish_temp_root: Path
    final_candidate_root: Path
    trajectories: tuple[TrajectoryCandidate, ...]
    runtime_manifest_sha256: str
    input_tree_sha256: str
    candidate_tree_sha256: str
    command_steps: tuple[str, ...] = POSTPROCESSING_COMMAND_STEPS


def _require_hash(value: str, *, label: str) -> None:
    if _HEX_SHA256_RE.fullmatch(value) is None:
        raise RuntimeExecutionError(
            "invalid_runtime_request",
            f"{label} is invalid.",
        )


def _require_real_empty_destination(path: Path) -> None:
    if path.exists() or path.is_symlink():
        raise RuntimeExecutionError(
            "recovery_required",
            "This postprocessing attempt already exists and requires review.",
        )


def _validate_tracking_artifacts(
    segment: PostprocessingSegmentInput,
) -> None:
    identities = segment.tracking_identities
    if (
        not identities
        or len(set(identities)) != len(identities)
        or any(_IDENTITY_RE.fullmatch(identity) is None for identity in identities)
    ):
        raise RuntimeExecutionError(
            "invalid_runtime_request",
            "The submitted Tracking identity set is invalid.",
        )
    expected_points = {f"img_{identity}.txt" for identity in identities}
    expected_images = {f"tracking_img_{identity}" for identity in identities}
    actual_points = {
        path.name
        for path in segment.tracked_segment_root.glob("img_*.txt")
        if path.is_file() and not path.is_symlink()
    }
    actual_images = {
        path.name
        for path in segment.tracked_segment_root.glob("tracking_img_*")
        if path.is_dir() and not path.is_symlink()
    }
    if actual_points != expected_points or actual_images != expected_images:
        raise RuntimeExecutionError(
            "tracked_checkpoint_scope_mismatch",
            "The tracked segment does not match its committed target set.",
        )
    # Projection scripts historically interpret arbitrary root-level txt files
    # as target tracks.  A reused legacy staging directory containing
    # ``distance.txt`` therefore creates a false target.  A system-owned M2
    # attempt accepts only the exact committed ``img_<identity>.txt`` set.
    unrelated_text = {
        path.name
        for path in segment.tracked_segment_root.glob("*.txt")
        if path.is_file()
        and not path.is_symlink()
        and path.name not in expected_points
    }
    if unrelated_text:
        raise RuntimeExecutionError(
            "unexpected_tracking_target_artifact",
            "The tracked input contains an uncommitted target artifact.",
        )


def _copy_selected_sync_inputs(
    *,
    config: NavigationAnnotationRuntimeConfig,
    request: PostprocessingRequest,
    private_clip_root: Path,
) -> None:
    assert config.clip_data_root is not None
    for segment in request.segments:
        source = (
            config.clip_data_root
            / request.dataset_date
            / segment.source_clip
            / "sync_data"
            / segment.private_segment_key
        )
        destination_parent = _ensure_private_directory_chain(
            private_clip_root,
            (
                request.dataset_date,
                segment.source_clip,
                "sync_data",
            ),
        )
        _copy_tree_bytes(source, destination_parent / segment.private_segment_key)


def _copy_shared_tracked_inputs(
    *,
    tracked_staging_root: Path,
    finish_temp_root: Path,
) -> None:
    maps = tracked_staging_root / "maps"
    if not _regular_directory(maps):
        raise RuntimeExecutionError(
            "missing_runtime_input",
            "The tracked staging map input is unavailable.",
        )
    _copy_tree_bytes(maps, finish_temp_root / "maps")


def _copy_tracked_metadata(
    *,
    tracked_staging_root: Path,
    finish_temp_root: Path,
) -> None:
    """Reuse the M1 metadata instead of executing its business step twice."""

    metadata = tracked_staging_root / "v1.0-trainval"
    if not _regular_directory(metadata):
        raise RuntimeExecutionError(
            "missing_runtime_input",
            "The tracked staging metadata input is unavailable.",
        )
    _copy_tree_bytes(metadata, finish_temp_root / "v1.0-trainval")


def _has_gridmap_json(path: Path) -> bool:
    return _regular_directory(path) and any(
        child.is_file()
        and not child.is_symlink()
        and child.suffix == ".json"
        for child in path.iterdir()
    )


def _validate_gridmap_decision_input(
    *,
    request: PostprocessingRequest,
    finish_temp_root: Path,
    private_clip_root: Path,
) -> None:
    for segment in request.segments:
        if request.gridmap_decision == "copy_existing_gridmap":
            grid_map = (
                private_clip_root
                / request.dataset_date
                / segment.source_clip
                / "sync_data"
                / segment.private_segment_key
                / "grid_map"
            )
        elif request.gridmap_decision == "skip_if_projection_ready":
            grid_map = (
                finish_temp_root
                / "samples"
                / request.dataset_date
                / segment.private_segment_key
                / "grid_map"
            )
        else:
            raise RuntimeExecutionError(
                "invalid_runtime_request",
                "Gridmap input validation requires a reusable gridmap decision.",
            )
        if not _has_gridmap_json(grid_map):
            message = (
                "The accepted projection-ready input is unavailable."
                if request.gridmap_decision == "skip_if_projection_ready"
                else "The accepted gridmap decision no longer matches its input."
            )
            raise RuntimeExecutionError(
                "gridmap_decision_precondition_failed",
                message,
            )


def _trajectory_file(segment_root: Path) -> Path:
    candidates = sorted(
        path
        for path in segment_root.glob("*_trajectory.json")
        if path.is_file()
        and not path.is_symlink()
        and not path.name.endswith("_trajectory_fix_five.json")
    )
    if len(candidates) != 1:
        raise RuntimeExecutionError(
            "postprocessing_outputs_missing",
            "The final candidate does not contain exactly one trajectory.",
        )
    return candidates[0]


def _speed_direction_file(segment_root: Path) -> Path:
    candidates = sorted(
        path
        for path in segment_root.glob("*_speed_direction.json")
        if path.is_file() and not path.is_symlink()
    )
    if len(candidates) != 1:
        raise RuntimeExecutionError(
            "postprocessing_outputs_missing",
            "The final candidate does not contain exactly one motion result.",
        )
    return candidates[0]


class NavigationPostprocessingRuntime(_RuntimeBase):
    """Execute the frozen post-Tracking half of ``_01/run_odom.sh``."""

    def preflight(
        self,
        *,
        localization_kind: str | None = None,
        gridmap_decision: str | None = None,
        trajectory_variant: str | None = None,
    ) -> str:
        """Prove the selected M2 variant and frozen payload before queuing."""

        decision = (
            localization_kind,
            gridmap_decision,
            trajectory_variant,
        )
        if any(value is not None for value in decision):
            if any(value is None for value in decision):
                raise RuntimeExecutionError(
                    "invalid_runtime_request",
                    "The postprocessing Runtime decision is incomplete.",
                )
            if (
                localization_kind != "odom"
                or trajectory_variant != "cjl_0525_with_gridmap"
            ):
                raise RuntimeExecutionError(
                    "unsupported_runtime_variant",
                    "The frozen M2 Runtime does not support this localization variant.",
                )
            if gridmap_decision not in {
                "copy_existing_gridmap",
                "generate_from_pcd",
                "skip_if_projection_ready",
            }:
                raise RuntimeExecutionError(
                    "invalid_runtime_request",
                    "The gridmap decision is invalid.",
                )
        self._require_available()
        try:
            content = _read_stable_regular_bytes(self.config.manifest_path)
        except OSError as exc:
            raise RuntimeExecutionError(
                "runtime_manifest_changed",
                "The frozen Runtime manifest cannot be safely read.",
            ) from exc
        manifest_sha256 = hashlib.sha256(content).hexdigest()
        self._revalidate_postprocessing_inputs(manifest_sha256)
        return manifest_sha256

    def _revalidate_postprocessing_inputs(
        self,
        expected_manifest_sha256: str,
    ) -> None:
        config = self.config
        assert config.runtime_source_root is not None
        try:
            content = _read_stable_regular_bytes(config.manifest_path)
            if hashlib.sha256(content).hexdigest() != expected_manifest_sha256:
                raise ValueError("manifest hash changed")
            document = json.loads(
                content.decode("utf-8"),
                object_pairs_hook=_manifest_object_without_duplicate_keys,
            )
            manifest = validate_manifest(document)
            entries = {
                str(entry["relative_path"]): entry
                for entry in manifest["entries"]
                if entry.get("root_alias") == "NAVIGATION_ODOM_V1_SOURCE"
                and entry.get("kind") == "frozen_file"
                and entry.get("relative_path")
                in _POSTPROCESSING_FROZEN_METADATA
            }
            if set(entries) != set(_POSTPROCESSING_FROZEN_METADATA):
                raise ValueError("required M2 payload is incomplete")
            for relative_path, (role, stage) in (
                _POSTPROCESSING_FROZEN_METADATA.items()
            ):
                entry = entries[relative_path]
                if entry.get("role") != role or entry.get("stage") != stage:
                    raise ValueError("required M2 payload metadata changed")
            if not _active_payload_permissions_safe(
                config.runtime_source_root,
                entries,
            ):
                raise ValueError("required M2 payload permissions are unsafe")
            for entry in entries.values():
                _read_manifest_frozen_bytes(
                    config.runtime_source_root,
                    entry,
                )
        except (KeyError, OSError, TypeError, ValueError) as exc:
            raise RuntimeExecutionError(
                "runtime_input_changed",
                "A frozen M2 Runtime input changed before execution.",
            ) from exc

    def _validate_request(self, request: PostprocessingRequest) -> Path:
        self._require_available()
        config = self.config
        assert config.work_root is not None
        _safe_component(request.job_ref, label="job_ref", pattern=_OPAQUE_REF_RE)
        _safe_component(request.run_ref, label="run_ref", pattern=_OPAQUE_REF_RE)
        _safe_component(
            request.dataset_date,
            label="dataset_date",
            pattern=_DATE_RE,
        )
        if request.attempt < 1 or not request.segments:
            raise RuntimeExecutionError(
                "invalid_runtime_request",
                "Postprocessing requires a positive attempt and tracked segments.",
            )
        if (
            request.localization_kind != "odom"
            or request.trajectory_variant != "cjl_0525_with_gridmap"
        ):
            raise RuntimeExecutionError(
                "unsupported_runtime_variant",
                "The frozen M2 Runtime does not support this localization variant.",
            )
        if request.gridmap_decision not in {
            "copy_existing_gridmap",
            "generate_from_pcd",
            "skip_if_projection_ready",
        }:
            raise RuntimeExecutionError(
                "invalid_runtime_request",
                "The gridmap decision is invalid.",
            )
        _require_hash(
            request.expected_runtime_manifest_sha256,
            label="Runtime manifest hash",
        )
        _require_hash(
            request.expected_prepared_artifact_tree_sha256,
            label="Prepared artifact hash",
        )
        staging_root = self._assert_under(
            request.tracked_staging_root,
            config.work_root,
            label="tracked staging",
        )
        seen_refs: set[str] = set()
        seen_keys: set[str] = set()
        for segment in request.segments:
            _safe_component(
                segment.segment_ref,
                label="segment_ref",
                pattern=_OPAQUE_REF_RE,
            )
            _safe_component(
                segment.source_clip,
                label="source clip",
                pattern=_CLIP_RE,
            )
            _safe_component(
                segment.private_segment_key,
                label="private segment key",
                pattern=_CLIP_RE,
            )
            _require_hash(
                segment.expected_tree_sha256,
                label="Tracked segment hash",
            )
            if (
                segment.segment_ref in seen_refs
                or segment.private_segment_key in seen_keys
            ):
                raise RuntimeExecutionError(
                    "invalid_runtime_request",
                    "Postprocessing segment bindings must be unique.",
                )
            seen_refs.add(segment.segment_ref)
            seen_keys.add(segment.private_segment_key)
            resolved = self._assert_under(
                segment.tracked_segment_root,
                staging_root,
                label="tracked segment",
            )
            expected = (
                staging_root
                / "samples"
                / request.dataset_date
                / segment.private_segment_key
            ).resolve(strict=True)
            if resolved != expected:
                raise RuntimeExecutionError(
                    "tracked_checkpoint_scope_mismatch",
                    "A tracked segment is not at its bound staging location.",
                )
            actual_hash = _tree_sha256(
                resolved,
                unsafe_code="tracked_staging_changed",
            )
            if actual_hash != segment.expected_tree_sha256:
                raise RuntimeExecutionError(
                    "tracked_staging_changed",
                    "A tracked segment changed before postprocessing.",
                )
            _validate_tracking_artifacts(segment)
        attestation_targets = tuple(
            TrackingTarget(
                segment_root=segment.tracked_segment_root,
                yaml_path=(
                    segment.tracked_segment_root / f"{identity}.yaml"
                ),
                identity=identity,
                # The hash is not used by prepared_staging_artifact_sha256;
                # YAML identity/content was already frozen in the Tracking
                # run ledger.  This reconstruction only defines the exact
                # exclusions needed to recompute the prepared staging hash.
                expected_yaml_sha256="0" * 64,
            )
            for segment in request.segments
            for identity in segment.tracking_identities
        )
        actual_prepared_sha256 = prepared_staging_artifact_sha256(
            staging_root,
            attestation_targets,
        )
        if (
            actual_prepared_sha256
            != request.expected_prepared_artifact_tree_sha256
        ):
            raise RuntimeExecutionError(
                "prepared_staging_changed",
                "The prepared M1 staging changed before postprocessing.",
            )
        return staging_root

    def _run_step(
        self,
        *,
        request: PostprocessingRequest,
        attempt_root: Path,
        argv: Sequence[str | Path],
        cwd: Path,
        safe_step_code: str,
        error_code: str,
        writable_bindings: Sequence[tuple[Path, Path]] = (),
        readonly_bindings: Sequence[tuple[Path, Path]] = (),
    ) -> None:
        with _observed_runtime_step(request.step_observer, safe_step_code):
            self._revalidate_postprocessing_inputs(
                request.expected_runtime_manifest_sha256
            )
            # The sandbox root is mounted read-only.  Libraries imported by
            # the frozen business scripts (notably matplotlib) still require
            # a writable temporary directory during normal initialization.
            # Keep that mutable state inside the attempt instead of exposing
            # the host /tmp or changing the frozen scripts.
            private_tmp_root = _ensure_private_directory_chain(
                attempt_root,
                (".runtime", "tmp"),
            )
            self._run_checked(
                staging_root=attempt_root,
                argv=argv,
                cwd=cwd,
                writable_bindings=(
                    *writable_bindings,
                    (private_tmp_root, Path("/tmp")),
                ),
                readonly_bindings=readonly_bindings,
                error_code=error_code,
            )
            self._revalidate_postprocessing_inputs(
                request.expected_runtime_manifest_sha256
            )

    def run(self, request: PostprocessingRequest) -> PostprocessingResult:
        # The single lease begins before any source or tracked input byte is
        # read for the attempt and remains held through candidate hardening.
        # Publication happens only after this method returns and therefore
        # acquires a separate, non-nested lease in the Worker.
        with navigation_writer_lock(lock_path=self.config.writer_lock_path):
            return self._run_with_writer_lock(request)

    def _run_with_writer_lock(
        self,
        request: PostprocessingRequest,
    ) -> PostprocessingResult:
        tracked_staging_root = self._validate_request(request)
        config = self.config
        assert config.work_root is not None
        assert config.runtime_source_root is not None
        assert config.clip_data_root is not None
        assert config.data_python is not None

        runtime_manifest_sha256 = self._current_runtime_manifest_sha256(
            request.expected_runtime_manifest_sha256,
        )
        attempt_root = (
            config.work_root
            / "jobs"
            / request.job_ref
            / "postprocessing"
            / request.run_ref
        )
        _require_real_empty_destination(attempt_root)
        run_parent = _ensure_private_directory_chain(
            config.work_root,
            ("jobs", request.job_ref, "postprocessing"),
        )
        _ensure_private_directory(attempt_root, create=True)
        if attempt_root.parent.resolve(strict=True) != run_parent.resolve(
            strict=True
        ):
            raise RuntimeExecutionError(
                "unsafe_runtime_path",
                "The postprocessing attempt path is invalid.",
            )
        finish_temp_root = _ensure_private_directory_chain(
            attempt_root,
            ("finish_temp",),
        )
        samples_date = _ensure_private_directory_chain(
            finish_temp_root,
            ("samples", request.dataset_date),
        )
        private_clip_root = _ensure_private_directory_chain(
            attempt_root,
            (".runtime", "clip_data"),
        )

        with _observed_runtime_step(
            request.step_observer,
            "postprocess_input_snapshot",
        ):
            input_records: list[dict[str, str]] = []
            for segment in request.segments:
                destination = samples_date / segment.private_segment_key
                _copy_tree_bytes(segment.tracked_segment_root, destination)
                copied_hash = _tree_sha256(
                    destination,
                    unsafe_code="unsafe_runtime_output",
                )
                if copied_hash != segment.expected_tree_sha256:
                    raise RuntimeExecutionError(
                        "tracked_staging_changed",
                        "A tracked input changed while creating the M2 attempt.",
                    )
                input_records.append(
                    {
                        "segment_ref": segment.segment_ref,
                        "content_sha256": copied_hash,
                    }
                )
            _copy_selected_sync_inputs(
                config=config,
                request=request,
                private_clip_root=private_clip_root,
            )
            _copy_shared_tracked_inputs(
                tracked_staging_root=tracked_staging_root,
                finish_temp_root=finish_temp_root,
            )

        with _observed_runtime_step(
            request.step_observer,
            "postprocess_metadata",
        ):
            _copy_tracked_metadata(
                tracked_staging_root=tracked_staging_root,
                finish_temp_root=finish_temp_root,
            )
            # Bind the complete private input view, not only the tracked
            # segment list.  This includes the copied synchronized inputs,
            # shared maps, and the M1-attested metadata that every frozen M2
            # business step will consume.
            input_tree_sha256 = hashlib.sha256(
                json.dumps(
                    {
                        "tracked_segments": input_records,
                        "finish_temp_tree_sha256": _tree_sha256(
                            finish_temp_root,
                            unsafe_code="unsafe_runtime_input",
                        ),
                        "private_clip_tree_sha256": _tree_sha256(
                            private_clip_root,
                            unsafe_code="unsafe_runtime_input",
                        ),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()

        # Do not reacquire the non-reentrant system lock.  This assertion
        # documents and enforces the continuous lease owned by ``run``.
        with _require_active_writer_lease():
            if request.gridmap_decision == "generate_from_pcd":
                selected_outer = tuple(
                    dict.fromkeys(segment.source_clip for segment in request.segments)
                )
                self._run_step(
                    request=request,
                    attempt_root=attempt_root,
                    argv=[
                        config.data_python,
                        config.runtime_source_root / "other_code" / "pcd_to_grid.py",
                        "--base-path",
                        private_clip_root,
                        "--date",
                        request.dataset_date,
                        "--segments",
                        *selected_outer,
                    ],
                    cwd=config.runtime_source_root / "other_code",
                    safe_step_code="postprocess_gridmap",
                    error_code="postprocess_gridmap_failed",
                    writable_bindings=(
                        (private_clip_root, config.legacy_clip_data_root),
                    ),
                )
            elif request.gridmap_decision == "copy_existing_gridmap":
                with _observed_runtime_step(
                    request.step_observer,
                    "postprocess_gridmap",
                ):
                    _validate_gridmap_decision_input(
                        request=request,
                        finish_temp_root=finish_temp_root,
                        private_clip_root=private_clip_root,
                    )
            else:
                # ``projection_ready`` is a distinct observed fact: the
                # prepared/tracked finish_temp segment already owns the
                # grid_map that the trajectory step will consume.  It must
                # not be reinterpreted as "copy from clip_data", because that
                # would silently change a Plan decision when the synchronized
                # source itself has no gridmap.
                with _observed_runtime_step(
                    request.step_observer,
                    "postprocess_gridmap",
                ):
                    _validate_gridmap_decision_input(
                        request=request,
                        finish_temp_root=finish_temp_root,
                        private_clip_root=private_clip_root,
                    )

            readonly_clip_binding = (
                (private_clip_root, config.legacy_clip_data_root),
            )
            projection_root = (
                config.runtime_source_root
                / "NuscenesAanlysis_smart_pts_project"
            )
            pt_project = config.runtime_source_root / "2_pt_project"
            self._run_step(
                request=request,
                attempt_root=attempt_root,
                argv=[
                    config.data_python,
                    projection_root / "main.py",
                    "--data_root",
                    finish_temp_root,
                ],
                cwd=projection_root,
                safe_step_code="postprocess_projection",
                error_code="postprocess_projection_failed",
            )
            self._run_step(
                request=request,
                attempt_root=attempt_root,
                argv=[
                    config.data_python,
                    pt_project / "0_img2world.py",
                    finish_temp_root,
                ],
                cwd=pt_project,
                safe_step_code="postprocess_world_coordinates",
                error_code="postprocess_world_coordinates_failed",
            )
            self._run_step(
                request=request,
                attempt_root=attempt_root,
                argv=[
                    config.data_python,
                    pt_project / "4_speed_direction_odom.py",
                    finish_temp_root,
                ],
                cwd=pt_project,
                safe_step_code="postprocess_speed_direction",
                error_code="postprocess_speed_direction_failed",
            )
            self._run_step(
                request=request,
                attempt_root=attempt_root,
                argv=[
                    config.data_python,
                    config.runtime_source_root / "other_code" / "cp_gridmap.py",
                    "--root_data",
                    finish_temp_root,
                ],
                cwd=config.runtime_source_root / "other_code",
                safe_step_code="postprocess_gridmap_transform",
                error_code="postprocess_gridmap_transform_failed",
                readonly_bindings=readonly_clip_binding,
            )
            self._run_step(
                request=request,
                attempt_root=attempt_root,
                argv=[
                    config.data_python,
                    pt_project / "2_othermethod_cjl_0525.py",
                    finish_temp_root,
                ],
                cwd=pt_project,
                safe_step_code="postprocess_trajectory",
                error_code="postprocess_trajectory_failed",
            )

            private_vla_root = _ensure_private_directory_chain(
                attempt_root,
                (".runtime", "VLADatasets"),
            )
            finish_data = _ensure_private_directory_chain(
                private_vla_root,
                ("finish_data",),
            )
            final_candidate_root = finish_data / request.dataset_date
            self._run_step(
                request=request,
                attempt_root=attempt_root,
                argv=[
                    config.data_python,
                    pt_project / "3_move_dir.py",
                    "--root_path",
                    final_candidate_root,
                    "--temp_path",
                    finish_temp_root,
                ],
                cwd=pt_project,
                safe_step_code="postprocess_final_candidate",
                error_code="postprocess_final_candidate_failed",
                readonly_bindings=readonly_clip_binding,
            )

        with _observed_runtime_step(
            request.step_observer,
            "postprocess_validate_outputs",
        ):
            settings = NavigationSettings(vladatasets_root=private_vla_root)
            selected_outer = list(
                dict.fromkeys(segment.source_clip for segment in request.segments)
            )
            validation = validate_navigation_outputs(
                request.dataset_date,
                segments=selected_outer,
                settings=settings,
            )
            if not validation.ok:
                raise RuntimeExecutionError(
                    "postprocessing_outputs_missing",
                    "The existing navigation output validation failed.",
                )

        trajectories: list[TrajectoryCandidate] = []
        for segment in request.segments:
            candidate_segment = (
                final_candidate_root
                / segment.source_clip
                / segment.private_segment_key
            )
            if not _regular_directory(candidate_segment):
                raise RuntimeExecutionError(
                    "postprocessing_outputs_missing",
                    "A bound final segment candidate is unavailable.",
                )
            trajectory = _trajectory_file(candidate_segment)
            speed_direction = _speed_direction_file(candidate_segment)
            trajectories.append(
                TrajectoryCandidate(
                    segment_ref=segment.segment_ref,
                    source_clip=segment.source_clip,
                    private_segment_key=segment.private_segment_key,
                    candidate_segment_root=candidate_segment,
                    trajectory_path=trajectory,
                    trajectory_sha256=_sha256_file(trajectory),
                    speed_direction_path=speed_direction,
                    speed_direction_sha256=_sha256_file(speed_direction),
                )
            )

        _harden_private_tree(attempt_root)
        return PostprocessingResult(
            attempt_root=attempt_root,
            finish_temp_root=finish_temp_root,
            final_candidate_root=final_candidate_root,
            trajectories=tuple(trajectories),
            runtime_manifest_sha256=runtime_manifest_sha256,
            input_tree_sha256=input_tree_sha256,
            candidate_tree_sha256=_tree_sha256(
                final_candidate_root,
                unsafe_code="unsafe_runtime_output",
            ),
        )


@dataclass(frozen=True)
class PublicationItem:
    source_clip: str
    candidate_root: Path
    expected_tree_sha256: str


@dataclass(frozen=True)
class PublicationResult:
    committed_source_clips: tuple[str, ...]
    journal_path: Path


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_journal(path: Path, payload: dict[str, object]) -> None:
    encoded = (
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        + b"\n"
    )
    temporary = path.with_name(f".{path.name}.tmp")
    if temporary.exists() or temporary.is_symlink():
        raise RuntimeExecutionError(
            "publication_recovery_required",
            "A publication journal temporary file already exists.",
        )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, 0o600)
    try:
        os.write(descriptor, encoded)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    _fsync_directory(path.parent)


class CompatibilityPublisher:
    """Journaled, per-outer-clip publication into compatibility finish_data."""

    def __init__(self, finish_data_root: Path) -> None:
        self.finish_data_root = finish_data_root

    def preflight(self) -> None:
        """Fail before heavy processing when the compatibility root is unsafe."""

        if not _regular_directory(self.finish_data_root) or not os.access(
            self.finish_data_root,
            os.W_OK | os.X_OK,
        ):
            raise RuntimeExecutionError(
                "publication_target_unavailable",
                "The compatibility finish root is unavailable.",
            )

    def publish(
        self,
        *,
        job_ref: str,
        run_ref: str,
        dataset_date: str,
        items: Sequence[PublicationItem],
        journal_root: Path,
    ) -> PublicationResult:
        _safe_component(job_ref, label="job_ref", pattern=_OPAQUE_REF_RE)
        _safe_component(run_ref, label="run_ref", pattern=_OPAQUE_REF_RE)
        _safe_component(dataset_date, label="dataset_date", pattern=_DATE_RE)
        if not items:
            raise RuntimeExecutionError(
                "invalid_runtime_request",
                "Publication requires at least one candidate.",
            )
        _ensure_private_directory(journal_root)
        journal_path = journal_root / f"publication-{run_ref}.json"
        if journal_path.exists() or journal_path.is_symlink():
            raise RuntimeExecutionError(
                "publication_recovery_required",
                "This publication already has a journal; recover it first.",
            )
        if not _regular_directory(self.finish_data_root):
            raise RuntimeExecutionError(
                "publication_target_unavailable",
                "The compatibility finish root is unavailable.",
            )
        date_root = self.finish_data_root / dataset_date
        if date_root.exists():
            if date_root.is_symlink() or not date_root.is_dir():
                raise RuntimeExecutionError(
                    "publication_target_unsafe",
                    "The compatibility date root is unsafe.",
                )
        else:
            date_root.mkdir(mode=0o750)
        _fsync_directory(self.finish_data_root)

        normalized: list[PublicationItem] = []
        seen: set[str] = set()
        for item in items:
            _safe_component(
                item.source_clip,
                label="source clip",
                pattern=_CLIP_RE,
            )
            _require_hash(item.expected_tree_sha256, label="Candidate hash")
            if item.source_clip in seen or not _regular_directory(
                item.candidate_root
            ):
                raise RuntimeExecutionError(
                    "invalid_runtime_request",
                    "Publication candidates must be unique real directories.",
                )
            seen.add(item.source_clip)
            actual = _tree_sha256(
                item.candidate_root,
                unsafe_code="unsafe_runtime_output",
            )
            if actual != item.expected_tree_sha256:
                raise RuntimeExecutionError(
                    "publication_candidate_changed",
                    "A publication candidate changed before commit.",
                )
            normalized.append(item)

        publication_plan: list[
            tuple[PublicationItem, Path, Path | None, bool]
        ] = []
        for index, item in enumerate(normalized):
            target = date_root / item.source_clip
            if target.exists() or target.is_symlink():
                if (
                    not _regular_directory(target)
                    or _tree_sha256(
                        target,
                        unsafe_code="publication_target_unsafe",
                    )
                    != item.expected_tree_sha256
                ):
                    raise RuntimeExecutionError(
                        "publication_conflict",
                        "An existing compatibility output differs from the candidate.",
                    )
                publication_plan.append((item, target, None, True))
                continue
            temporary = date_root / f".annotation-{run_ref}-{index}"
            if temporary.exists() or temporary.is_symlink():
                raise RuntimeExecutionError(
                    "publication_recovery_required",
                    "A publication temporary directory already exists.",
                )
            publication_plan.append((item, target, temporary, False))

        # Every candidate and destination is checked before the first target is
        # written.  Cross-clip publication is then recoverable through this
        # journal; each individual clip is committed with one directory rename.
        journal: dict[str, object] = {
            "schema_version": 1,
            "job_ref": job_ref,
            "run_ref": run_ref,
            "dataset_date": dataset_date,
            "state": "intent",
            "items": [
                {
                    "source_clip": item.source_clip,
                    "sha256": item.expected_tree_sha256,
                    "state": (
                        "preexisting"
                        if publication_plan[index][3]
                        else "pending"
                    ),
                }
                for index, item in enumerate(normalized)
            ],
        }
        _write_journal(journal_path, journal)
        committed: list[str] = []
        try:
            # Stage every missing clip before committing any of them.  A
            # changed candidate or a copy failure therefore cannot leave an
            # earlier clip published.
            for index, (item, _target, temporary, preexisting) in enumerate(
                publication_plan
            ):
                if preexisting:
                    continue
                assert temporary is not None
                _copy_tree_bytes(item.candidate_root, temporary)
                copied_hash = _tree_sha256(
                    temporary,
                    unsafe_code="unsafe_runtime_output",
                )
                if copied_hash != item.expected_tree_sha256:
                    raise RuntimeExecutionError(
                        "publication_candidate_changed",
                        "A candidate changed while being staged for publication.",
                    )
                cast_items = journal["items"]
                assert isinstance(cast_items, list)
                cast_item = cast_items[index]
                assert isinstance(cast_item, dict)
                cast_item["state"] = "staged"
                journal["state"] = "staged"
                _write_journal(journal_path, journal)

            # Recheck the complete missing-target set after staging and before
            # the first directory rename.  A concurrent writer is never
            # silently overwritten.
            for _item, target, _temporary, preexisting in publication_plan:
                if not preexisting and (target.exists() or target.is_symlink()):
                    raise RuntimeExecutionError(
                        "publication_recovery_required",
                        "A compatibility target changed during publication staging.",
                    )

            for index, (item, target, temporary, preexisting) in enumerate(
                publication_plan
            ):
                if preexisting:
                    if (
                        not _regular_directory(target)
                        or _tree_sha256(
                            target,
                            unsafe_code="publication_target_unsafe",
                        )
                        != item.expected_tree_sha256
                    ):
                        raise RuntimeExecutionError(
                            "publication_recovery_required",
                            "A pre-existing compatibility target changed during publication.",
                        )
                else:
                    assert temporary is not None
                    if target.exists() or target.is_symlink():
                        raise RuntimeExecutionError(
                            "publication_recovery_required",
                            "A compatibility target changed before commit.",
                        )
                    copied_hash = _tree_sha256(
                        temporary,
                        unsafe_code="unsafe_runtime_output",
                    )
                    if copied_hash != item.expected_tree_sha256:
                        raise RuntimeExecutionError(
                            "publication_candidate_changed",
                            "A staged publication candidate changed before commit.",
                        )
                    os.replace(temporary, target)
                    _fsync_directory(date_root)
                committed.append(item.source_clip)
                cast_items = journal["items"]
                assert isinstance(cast_items, list)
                cast_item = cast_items[index]
                assert isinstance(cast_item, dict)
                cast_item["state"] = "committed"
                journal["state"] = (
                    "committed"
                    if len(committed) == len(normalized)
                    else "committing"
                )
                _write_journal(journal_path, journal)
        except Exception as exc:
            if isinstance(exc, RuntimeExecutionError):
                raise
            raise RuntimeExecutionError(
                "publication_recovery_required",
                "Compatibility publication requires operator recovery.",
            ) from exc
        return PublicationResult(
            committed_source_clips=tuple(committed),
            journal_path=journal_path,
        )

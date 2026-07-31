from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import threading

import pytest

import vla_data_juicer_agents.annotation.postprocessing_runtime as postprocessing_module
from vla_data_juicer_agents.annotation.postprocessing_runtime import (
    POSTPROCESSING_COMMAND_STEPS,
    CompatibilityPublisher,
    NavigationPostprocessingRuntime,
    PostprocessingRequest,
    PostprocessingSegmentInput,
    PublicationItem,
    _POSTPROCESSING_FROZEN_METADATA,
    _POSTPROCESSING_REQUIRED_DISTRIBUTIONS,
    _legacy_finish_temp_name,
    _validate_gridmap_decision_input,
)
from vla_data_juicer_agents.annotation.runtime import (
    NavigationAnnotationRuntimeConfig,
    RuntimeExecutionError,
    TrackingTarget,
    _tree_sha256,
    prepared_staging_artifact_sha256,
)
from vla_data_juicer_agents.navigation.writer_lock import (
    active_writer_lock_fds,
    navigation_writer_lock,
    writer_active_path,
)


class _ValidationRuntime(NavigationPostprocessingRuntime):
    def _require_available(self) -> None:
        return None


class _PreflightRuntime(_ValidationRuntime):
    def __init__(self, config: NavigationAnnotationRuntimeConfig) -> None:
        super().__init__(config)
        self.revalidated: list[str] = []
        self.dependencies_revalidated: list[str] = []

    def _revalidate_postprocessing_inputs(
        self,
        expected_manifest_sha256: str,
    ) -> None:
        self.revalidated.append(expected_manifest_sha256)

    def _revalidate_postprocessing_dependencies(
        self,
        expected_manifest_sha256: str,
    ) -> None:
        self.dependencies_revalidated.append(expected_manifest_sha256)


class _SnapshotProbeRuntime(_ValidationRuntime):
    def _current_runtime_manifest_sha256(
        self,
        expected_manifest_sha256: str,
    ) -> str:
        return expected_manifest_sha256

    def _revalidate_postprocessing_dependencies(
        self,
        expected_manifest_sha256: str,
    ) -> None:
        return None


class _RunStepProbeRuntime(_PreflightRuntime):
    def __init__(self, config: NavigationAnnotationRuntimeConfig) -> None:
        super().__init__(config)
        self.checked_calls: list[dict[str, object]] = []

    def _run_checked(self, **kwargs) -> None:
        self.checked_calls.append(dict(kwargs))


def _config(tmp_path: Path) -> NavigationAnnotationRuntimeConfig:
    work_root = tmp_path / "work"
    clip_root = tmp_path / "clip_data"
    work_root.mkdir(mode=0o700, parents=True)
    clip_root.mkdir(parents=True)
    return NavigationAnnotationRuntimeConfig(
        runtime_source_root=None,
        work_root=work_root,
        clip_data_root=clip_root,
        data_python=None,
        data_env_setup=None,
        manifest_path=tmp_path / "manifest.json",
    )


def _request(
    tmp_path: Path,
    *,
    dirty: bool = False,
    localization_kind: str = "odom",
    trajectory_variant: str = "cjl_0525_with_gridmap",
) -> PostprocessingRequest:
    config = _config(tmp_path)
    assert config.work_root is not None
    staging = (
        config.work_root
        / "jobs"
        / ("job_" + "1" * 32)
        / "attempts"
        / ("run_" + "2" * 32)
        / "20270605_temp"
    )
    segment = staging / "samples" / "20270605" / "20260605_160904_0"
    tracking = segment / "tracking_img_master_black_black_black"
    tracking.mkdir(parents=True)
    (tracking / "000001.jpg").write_bytes(b"frame")
    (segment / "img_master_black_black_black.txt").write_text(
        "1 2\n",
        encoding="utf-8",
    )
    if dirty:
        (segment / "distance.txt").write_text("", encoding="utf-8")
    binding = PostprocessingSegmentInput(
        segment_ref="segment_" + "3" * 32,
        source_clip="20260605_160904",
        private_segment_key="20260605_160904_0",
        tracked_segment_root=segment,
        expected_tree_sha256=_tree_sha256(segment),
        tracking_identities=("master_black_black_black",),
    )
    prepared_sha256 = prepared_staging_artifact_sha256(
        staging,
        (
            TrackingTarget(
                segment_root=segment,
                yaml_path=segment / "master_black_black_black.yaml",
                identity="master_black_black_black",
                expected_yaml_sha256="0" * 64,
            ),
        ),
    )
    return PostprocessingRequest(
        job_ref="job_" + "1" * 32,
        run_ref="run_" + "4" * 32,
        attempt=1,
        dataset_date="20270605",
        tracked_staging_root=staging,
        segments=(binding,),
        gridmap_decision="copy_existing_gridmap",
        localization_kind=localization_kind,  # type: ignore[arg-type]
        trajectory_variant=trajectory_variant,  # type: ignore[arg-type]
        expected_runtime_manifest_sha256="a" * 64,
        expected_prepared_artifact_tree_sha256=prepared_sha256,
    )


def test_postprocessing_rejects_historical_false_target(tmp_path: Path) -> None:
    request = _request(tmp_path, dirty=True)
    runtime = _ValidationRuntime(_config(tmp_path / "other"))

    # Use the request's own work root in the Runtime under test.
    runtime.config = NavigationAnnotationRuntimeConfig(
        runtime_source_root=None,
        work_root=request.tracked_staging_root.parents[4],
        clip_data_root=tmp_path / "unused-clip-data",
        data_python=None,
        data_env_setup=None,
        manifest_path=tmp_path / "manifest.json",
    )

    with pytest.raises(RuntimeExecutionError) as failure:
        runtime._validate_request(request)

    assert failure.value.code == "unexpected_tracking_target_artifact"


def test_postprocessing_current_runtime_fails_closed_for_ins(
    tmp_path: Path,
) -> None:
    request = _request(
        tmp_path,
        localization_kind="ins",
        trajectory_variant="cjl_with_gridmap",
    )
    runtime = _ValidationRuntime(_config(tmp_path / "other"))

    with pytest.raises(RuntimeExecutionError) as failure:
        runtime._validate_request(request)

    assert failure.value.code == "unsupported_runtime_variant"


def test_postprocessing_command_contract_preserves_frozen_order() -> None:
    assert POSTPROCESSING_COMMAND_STEPS == (
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
    assert "NoobScenes/main_smart_odom.py" not in (
        _POSTPROCESSING_FROZEN_METADATA
    )


def test_postprocessing_step_delegates_tmp_lifecycle_to_runtime_base(
    tmp_path: Path,
) -> None:
    runtime = _RunStepProbeRuntime(_config(tmp_path))
    request = _request(tmp_path / "request")
    attempt_root = tmp_path / "attempt"
    attempt_root.mkdir(mode=0o700)

    runtime._run_step(
        request=request,
        attempt_root=attempt_root,
        argv=(Path("/usr/bin/python3"), Path("/runtime/main.py")),
        cwd=tmp_path,
        safe_step_code="postprocess_projection",
        error_code="postprocess_projection_failed",
    )

    assert len(runtime.checked_calls) == 1
    writable = runtime.checked_calls[0]["writable_bindings"]
    assert writable == ()
    assert not (attempt_root / ".runtime" / "tmp").exists()


def test_postprocessing_xvfb_starts_after_private_tmp_is_mounted(
    tmp_path: Path,
) -> None:
    runtime_source = tmp_path / "runtime"
    runtime_source.mkdir()
    data_env_setup = tmp_path / "data-env.sh"
    data_env_setup.write_text("", encoding="utf-8")
    config = replace(
        _config(tmp_path),
        runtime_source_root=runtime_source,
        data_env_setup=data_env_setup,
    )
    runtime = NavigationPostprocessingRuntime(config)
    attempt_root = config.work_root / "jobs" / ("job_" + "a" * 32)  # type: ignore[operator]
    attempt_root.mkdir(parents=True)
    private_tmp = runtime._create_command_private_tmp(attempt_root)

    command = runtime._sandbox_command(
        staging_root=attempt_root,
        private_tmp_root=private_tmp,
        argv=(Path("/usr/bin/python3"), Path("/runtime/trajectory.py")),
        cwd=config.runtime_source_root,  # type: ignore[arg-type]
    )

    shell = command[2]
    assert shell.index(str(config.bwrap_path)) < shell.index(
        str(config.xvfb_run_path)
    )
    assert shell.index(f"--bind {private_tmp} /tmp") < shell.index(
        str(config.xvfb_run_path)
    )
    assert not private_tmp.is_relative_to(attempt_root)


def test_postprocessing_reuses_only_the_attested_m1_staging(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    (request.tracked_staging_root / "unexpected-mutation").write_text(
        "changed",
        encoding="utf-8",
    )
    runtime = _ValidationRuntime(
        NavigationAnnotationRuntimeConfig(
            runtime_source_root=None,
            work_root=request.tracked_staging_root.parents[4],
            clip_data_root=tmp_path / "unused-clip-data",
            data_python=None,
            data_env_setup=None,
            manifest_path=tmp_path / "manifest.json",
        )
    )

    with pytest.raises(RuntimeExecutionError) as failure:
        runtime._validate_request(request)

    assert failure.value.code == "prepared_staging_changed"


def test_projection_ready_is_validated_in_tracked_finish_temp_not_clip_mirror(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    request = PostprocessingRequest(
        **{
            **request.__dict__,
            "gridmap_decision": "skip_if_projection_ready",
        }
    )
    finish_temp = tmp_path / "candidate"
    private_clip = tmp_path / "private-clip"
    projection_gridmap = (
        finish_temp
        / "samples"
        / request.dataset_date
        / request.segments[0].private_segment_key
        / "grid_map"
    )
    projection_gridmap.mkdir(parents=True)
    (projection_gridmap / "000001.json").write_text("{}", encoding="utf-8")

    _validate_gridmap_decision_input(
        request=request,
        finish_temp_root=finish_temp,
        private_clip_root=private_clip,
    )


def test_existing_gridmap_does_not_accept_projection_ready_only(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    finish_temp = tmp_path / "candidate"
    private_clip = tmp_path / "private-clip"
    projection_gridmap = (
        finish_temp
        / "samples"
        / request.dataset_date
        / request.segments[0].private_segment_key
        / "grid_map"
    )
    projection_gridmap.mkdir(parents=True)
    (projection_gridmap / "000001.json").write_text("{}", encoding="utf-8")

    with pytest.raises(RuntimeExecutionError) as failure:
        _validate_gridmap_decision_input(
            request=request,
            finish_temp_root=finish_temp,
            private_clip_root=private_clip,
        )

    assert failure.value.code == "gridmap_decision_precondition_failed"


def test_generated_gridmap_is_revalidated_in_private_clip_mirror(
    tmp_path: Path,
) -> None:
    request = replace(
        _request(tmp_path),
        gridmap_decision="generate_from_pcd",
    )
    finish_temp = tmp_path / "candidate"
    private_clip = tmp_path / "private-clip"
    generated = (
        private_clip
        / request.dataset_date
        / request.segments[0].source_clip
        / "sync_data"
        / request.segments[0].private_segment_key
        / "grid_map"
    )
    generated.mkdir(parents=True)
    (generated / "000001.json").write_text("{}", encoding="utf-8")

    _validate_gridmap_decision_input(
        request=request,
        finish_temp_root=finish_temp,
        private_clip_root=private_clip,
    )

    (generated / "000001.json").unlink()
    with pytest.raises(RuntimeExecutionError) as failure:
        _validate_gridmap_decision_input(
            request=request,
            finish_temp_root=finish_temp,
            private_clip_root=private_clip,
        )
    assert failure.value.code == "gridmap_decision_precondition_failed"


def test_compatibility_publication_is_journaled_and_idempotent_by_hash(
    tmp_path: Path,
) -> None:
    finish_root = tmp_path / "finish_data"
    journal_root = tmp_path / "journal"
    candidate = tmp_path / "candidate" / "20260605_160904"
    finish_root.mkdir()
    journal_root.mkdir(mode=0o700)
    candidate.mkdir(parents=True)
    (candidate / "artifact.json").write_text('{"ok":true}\n', encoding="utf-8")
    digest = _tree_sha256(candidate)
    publisher = CompatibilityPublisher(finish_root)

    result = publisher.publish(
        job_ref="job_" + "1" * 32,
        run_ref="run_" + "2" * 32,
        dataset_date="20270605",
        items=(
            PublicationItem(
                source_clip="20260605_160904",
                candidate_root=candidate,
                expected_tree_sha256=digest,
            ),
        ),
        journal_root=journal_root,
    )

    published = finish_root / "20270605" / "20260605_160904"
    assert result.committed_source_clips == ("20260605_160904",)
    assert _tree_sha256(published) == digest
    assert b'"state":"committed"' in result.journal_path.read_bytes()

    second_journal = tmp_path / "journal-2"
    second_journal.mkdir(mode=0o700)
    again = publisher.publish(
        job_ref="job_" + "1" * 32,
        run_ref="run_" + "3" * 32,
        dataset_date="20270605",
        items=(
            PublicationItem(
                source_clip="20260605_160904",
                candidate_root=candidate,
                expected_tree_sha256=digest,
            ),
        ),
        journal_root=second_journal,
    )
    assert again.committed_source_clips == ("20260605_160904",)


def test_compatibility_publication_stops_on_existing_difference(
    tmp_path: Path,
) -> None:
    finish_root = tmp_path / "finish_data"
    journal_root = tmp_path / "journal"
    candidate = tmp_path / "candidate" / "20260605_160904"
    existing = finish_root / "20270605" / "20260605_160904"
    candidate.mkdir(parents=True)
    existing.mkdir(parents=True)
    journal_root.mkdir(mode=0o700)
    (candidate / "artifact.json").write_text('{"new":true}\n', encoding="utf-8")
    (existing / "artifact.json").write_text('{"old":true}\n', encoding="utf-8")

    with pytest.raises(RuntimeExecutionError) as failure:
        CompatibilityPublisher(finish_root).publish(
            job_ref="job_" + "1" * 32,
            run_ref="run_" + "2" * 32,
            dataset_date="20270605",
            items=(
                PublicationItem(
                    source_clip="20260605_160904",
                    candidate_root=candidate,
                    expected_tree_sha256=_tree_sha256(candidate),
                ),
            ),
            journal_root=journal_root,
        )

    assert failure.value.code == "publication_conflict"
    assert hashlib.sha256(
        (existing / "artifact.json").read_bytes()
    ).hexdigest() == hashlib.sha256(b'{"old":true}\n').hexdigest()


def test_invalid_publication_candidate_does_not_create_empty_date_root(
    tmp_path: Path,
) -> None:
    finish_root = tmp_path / "finish_data"
    journal_root = tmp_path / "journal"
    candidate = tmp_path / "candidate" / "20260605_160904"
    finish_root.mkdir()
    journal_root.mkdir(mode=0o700)
    candidate.mkdir(parents=True)
    (candidate / "artifact.json").write_text(
        '{"candidate":true}\n',
        encoding="utf-8",
    )

    with pytest.raises(RuntimeExecutionError) as failure:
        CompatibilityPublisher(finish_root).publish(
            job_ref="job_" + "1" * 32,
            run_ref="run_" + "2" * 32,
            dataset_date="20270605",
            items=(
                PublicationItem(
                    source_clip="20260605_160904",
                    candidate_root=candidate,
                    expected_tree_sha256="0" * 64,
                ),
            ),
            journal_root=journal_root,
        )

    assert failure.value.code == "publication_candidate_changed"
    assert not (finish_root / "20270605").exists()
    assert list(journal_root.iterdir()) == []


def test_publication_failure_after_journal_requires_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    finish_root = tmp_path / "finish_data"
    journal_root = tmp_path / "journal"
    candidate = tmp_path / "candidate" / "20260605_160904"
    finish_root.mkdir()
    journal_root.mkdir(mode=0o700)
    candidate.mkdir(parents=True)
    (candidate / "artifact.json").write_text(
        '{"candidate":true}\n',
        encoding="utf-8",
    )

    def fail_copy(_source: Path, _destination: Path) -> None:
        raise OSError("copy failed")

    monkeypatch.setattr(postprocessing_module, "_copy_tree_bytes", fail_copy)
    with pytest.raises(RuntimeExecutionError) as failure:
        CompatibilityPublisher(finish_root).publish(
            job_ref="job_" + "1" * 32,
            run_ref="run_" + "2" * 32,
            dataset_date="20270605",
            items=(
                PublicationItem(
                    source_clip="20260605_160904",
                    candidate_root=candidate,
                    expected_tree_sha256=_tree_sha256(candidate),
                ),
            ),
            journal_root=journal_root,
        )

    assert failure.value.code == "publication_recovery_required"
    assert not (
        finish_root / "20270605" / "20260605_160904"
    ).exists()
    journals = list(journal_root.glob("publication-*.json"))
    assert len(journals) == 1
    assert b'"state":"intent"' in journals[0].read_bytes()


def test_publication_revalidates_outer_clip_after_atomic_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    finish_root = tmp_path / "finish_data"
    journal_root = tmp_path / "journal"
    candidate = tmp_path / "candidate" / "20260605_160904"
    finish_root.mkdir()
    journal_root.mkdir(mode=0o700)
    candidate.mkdir(parents=True)
    (candidate / "artifact.json").write_text(
        '{"candidate":true}\n',
        encoding="utf-8",
    )
    real_replace = postprocessing_module.os.replace

    def replace_then_tamper(source: Path, destination: Path) -> None:
        real_replace(source, destination)
        if Path(source).name.startswith(".annotation-"):
            (Path(destination) / "unexpected.txt").write_text(
                "changed",
                encoding="utf-8",
            )

    monkeypatch.setattr(
        postprocessing_module.os,
        "replace",
        replace_then_tamper,
    )
    with pytest.raises(RuntimeExecutionError) as failure:
        CompatibilityPublisher(finish_root).publish(
            job_ref="job_" + "1" * 32,
            run_ref="run_" + "2" * 32,
            dataset_date="20270605",
            items=(
                PublicationItem(
                    source_clip="20260605_160904",
                    candidate_root=candidate,
                    expected_tree_sha256=_tree_sha256(candidate),
                ),
            ),
            journal_root=journal_root,
        )

    assert failure.value.code == "publication_recovery_required"
    assert (
        finish_root
        / "20270605"
        / "20260605_160904"
        / "unexpected.txt"
    ).is_file()
    journals = list(journal_root.glob("publication-*.json"))
    assert len(journals) == 1
    journal = json.loads(journals[0].read_text(encoding="utf-8"))
    assert journal["state"] == "staged"
    assert journal["items"][0]["state"] == "staged"


def test_multi_clip_publication_preflights_every_target_before_first_write(
    tmp_path: Path,
) -> None:
    finish_root = tmp_path / "finish_data"
    journal_root = tmp_path / "journal"
    first = tmp_path / "candidate" / "clip-first"
    second = tmp_path / "candidate" / "clip-second"
    conflicting = finish_root / "20270605" / "clip-second"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    conflicting.mkdir(parents=True)
    journal_root.mkdir(mode=0o700)
    (first / "artifact.json").write_text('{"clip":1}\n', encoding="utf-8")
    (second / "artifact.json").write_text('{"clip":2}\n', encoding="utf-8")
    (conflicting / "artifact.json").write_text(
        '{"existing":"different"}\n',
        encoding="utf-8",
    )

    with pytest.raises(RuntimeExecutionError) as failure:
        CompatibilityPublisher(finish_root).publish(
            job_ref="job_" + "1" * 32,
            run_ref="run_" + "2" * 32,
            dataset_date="20270605",
            items=(
                PublicationItem(
                    source_clip="clip-first",
                    candidate_root=first,
                    expected_tree_sha256=_tree_sha256(first),
                ),
                PublicationItem(
                    source_clip="clip-second",
                    candidate_root=second,
                    expected_tree_sha256=_tree_sha256(second),
                ),
            ),
            journal_root=journal_root,
        )

    assert failure.value.code == "publication_conflict"
    assert not (finish_root / "20270605" / "clip-first").exists()
    assert list(journal_root.iterdir()) == []


def test_postprocessing_preflight_attests_manifest_and_rejects_ins(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    config.manifest_path.write_bytes(b'{"schema_version":1}\n')
    runtime = _PreflightRuntime(config)
    expected = hashlib.sha256(config.manifest_path.read_bytes()).hexdigest()

    assert runtime.preflight(
        localization_kind="odom",
        gridmap_decision="generate_from_pcd",
        trajectory_variant="cjl_0525_with_gridmap",
    ) == expected
    assert runtime.revalidated == [expected]
    assert runtime.dependencies_revalidated == [expected]

    with pytest.raises(RuntimeExecutionError) as failure:
        runtime.preflight(
            localization_kind="ins",
            gridmap_decision="copy_existing_gridmap",
            trajectory_variant="cjl_with_gridmap",
        )
    assert failure.value.code == "unsupported_runtime_variant"
    assert runtime.revalidated == [expected]
    assert runtime.dependencies_revalidated == [expected]


def test_postprocessing_dependency_contract_includes_frozen_script_imports(
    tmp_path: Path,
) -> None:
    manifest_path = (
        Path(__file__).resolve().parents[1]
        / "runtime"
        / "navigation_odom_v1"
        / "manifest.json"
    )
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    available = {
        str(entry["relative_path"]).rsplit("/", 1)[-1]: str(entry["version"])
        for entry in document["entries"]
        if entry["kind"] == "external_runtime"
        and entry["role"]
        in {"python_package", "python_package_direct_dependency"}
    }
    probes: list[tuple[str, ...]] = []

    def package_probe(distributions: tuple[str, ...]) -> dict[str, str]:
        probes.append(distributions)
        return {name: available[name] for name in distributions}

    config = replace(
        _config(tmp_path),
        manifest_path=manifest_path,
        package_probe=package_probe,
    )
    runtime = _ValidationRuntime(config)
    expected_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()

    runtime._revalidate_postprocessing_dependencies(expected_sha256)

    assert probes == [tuple(sorted(_POSTPROCESSING_REQUIRED_DISTRIBUTIONS))]
    assert {"matplotlib", "pypcd", "similaritymeasures"}.issubset(probes[0])

    mismatched = _ValidationRuntime(
        replace(
            config,
            package_probe=lambda distributions: {
                name: (
                    "unexpected"
                    if name == "matplotlib"
                    else available[name]
                )
                for name in distributions
            },
        )
    )
    with pytest.raises(RuntimeExecutionError) as failure:
        mismatched._revalidate_postprocessing_dependencies(expected_sha256)
    assert failure.value.code == "postprocessing_dependency_mismatch"

    unavailable = _ValidationRuntime(
        replace(
            config,
            package_probe=lambda _distributions: (_ for _ in ()).throw(
                RuntimeError("probe failed")
            ),
        )
    )
    with pytest.raises(RuntimeExecutionError) as failure:
        unavailable._revalidate_postprocessing_dependencies(expected_sha256)
    assert failure.value.code == "postprocessing_dependency_mismatch"


def test_compatibility_publisher_preflight_rejects_symlink_root(
    tmp_path: Path,
) -> None:
    real_root = tmp_path / "real-finish"
    real_root.mkdir()
    linked_root = tmp_path / "linked-finish"
    linked_root.symlink_to(real_root, target_is_directory=True)

    with pytest.raises(RuntimeExecutionError) as failure:
        CompatibilityPublisher(linked_root).preflight()

    assert failure.value.code == "publication_target_unavailable"


class _SnapshotProbeComplete(RuntimeError):
    pass


class _LegacyPathProbeComplete(RuntimeError):
    pass


class _LegacyPathProbeRuntime(_SnapshotProbeRuntime):
    def __init__(self, config: NavigationAnnotationRuntimeConfig) -> None:
        super().__init__(config)
        self.step_calls: list[dict[str, object]] = []

    def _run_step(self, **kwargs) -> None:
        self.step_calls.append(dict(kwargs))
        if kwargs["safe_step_code"] == "postprocess_gridmap":
            private_clip_root = Path(kwargs["writable_bindings"][0][0])
            for segment_root in private_clip_root.glob("*/*/sync_data/*"):
                grid_map = segment_root / "grid_map"
                grid_map.mkdir()
                (grid_map / "000001.json").write_text(
                    '{"data":[0]}\n',
                    encoding="utf-8",
                )
        if kwargs["safe_step_code"] == "postprocess_gridmap_transform":
            root = Path(kwargs["argv"][-1])
            samples = root / "samples"
            for segment_root in samples.glob("*/*"):
                grid_map = segment_root / "grid_map"
                grid_map.mkdir()
                (grid_map / "000001.json").write_text(
                    json.dumps({"data": [0] * 40000}),
                    encoding="utf-8",
                )
        postcondition = kwargs.get("postcondition")
        if postcondition is not None:
            postcondition()
        if kwargs["safe_step_code"] == "postprocess_final_candidate":
            raise _LegacyPathProbeComplete


class _SilentGridmapTransformProbeRuntime(_SnapshotProbeRuntime):
    def __init__(self, config: NavigationAnnotationRuntimeConfig) -> None:
        super().__init__(config)
        self.called_steps: list[str] = []

    def _run_step(self, **kwargs) -> None:
        self.called_steps.append(str(kwargs["safe_step_code"]))
        postcondition = kwargs.get("postcondition")
        if postcondition is not None:
            postcondition()


class _DependencyDriftProbeRuntime(_SnapshotProbeRuntime):
    def _revalidate_postprocessing_dependencies(
        self,
        expected_manifest_sha256: str,
    ) -> None:
        raise RuntimeExecutionError(
            "postprocessing_dependency_mismatch",
            "The frozen M2 Python dependencies failed verification.",
        )


def _runtime_for_snapshot_probe(
    tmp_path: Path,
) -> tuple[_SnapshotProbeRuntime, PostprocessingRequest, Path, Path]:
    request = _request(tmp_path)
    work_root = request.tracked_staging_root.parents[4]
    clip_root = tmp_path / "clip_data"
    source = (
        clip_root
        / request.dataset_date
        / request.segments[0].source_clip
        / "sync_data"
        / request.segments[0].private_segment_key
    )
    source.mkdir(parents=True)
    (source / "source.bin").write_bytes(b"synchronized-source")
    grid_map = source / "grid_map"
    grid_map.mkdir()
    (grid_map / "000001.json").write_text("{}", encoding="utf-8")
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    lock_parent = tmp_path / "writer-coordination"
    lock_parent.mkdir(mode=0o700)
    lock_path = lock_parent / "navigation-writer.lock"
    runtime = _SnapshotProbeRuntime(
        NavigationAnnotationRuntimeConfig(
            runtime_source_root=runtime_root,
            work_root=work_root,
            clip_data_root=clip_root,
            data_python=Path("/usr/bin/python3"),
            data_env_setup=None,
            manifest_path=tmp_path / "manifest.json",
            writer_lock_path=lock_path,
        )
    )
    return runtime, request, clip_root, lock_path


def _refresh_request_attestations(
    request: PostprocessingRequest,
) -> PostprocessingRequest:
    segments = tuple(
        replace(
            segment,
            expected_tree_sha256=_tree_sha256(segment.tracked_segment_root),
        )
        for segment in request.segments
    )
    targets = tuple(
        TrackingTarget(
            segment_root=segment.tracked_segment_root,
            yaml_path=segment.tracked_segment_root / f"{identity}.yaml",
            identity=identity,
            expected_yaml_sha256="0" * 64,
        )
        for segment in segments
        for identity in segment.tracking_identities
    )
    return replace(
        request,
        segments=segments,
        expected_prepared_artifact_tree_sha256=(
            prepared_staging_artifact_sha256(
                request.tracked_staging_root,
                targets,
            )
        ),
    )


def _add_shared_tracked_inputs(request: PostprocessingRequest) -> None:
    maps = request.tracked_staging_root / "maps"
    metadata = request.tracked_staging_root / "v1.0-trainval"
    maps.mkdir()
    metadata.mkdir()
    (maps / "map.png").write_bytes(b"map")
    (metadata / "scene.json").write_text("[]", encoding="utf-8")


def test_frozen_postprocessing_receives_legacy_date_named_temp_root(
    tmp_path: Path,
) -> None:
    runtime, request, _clip_root, _lock_path = _runtime_for_snapshot_probe(
        tmp_path
    )
    _add_shared_tracked_inputs(request)
    tracked_gridmap = request.segments[0].tracked_segment_root / "grid_map"
    tracked_gridmap.mkdir()
    (tracked_gridmap / "000001.json").write_text(
        '{"data":[1]}\n',
        encoding="utf-8",
    )
    request = _refresh_request_attestations(request)
    probe = _LegacyPathProbeRuntime(runtime.config)

    with pytest.raises(_LegacyPathProbeComplete):
        probe.run(request)

    expected_name = _legacy_finish_temp_name(request.dataset_date)
    expected_steps = [
        "postprocess_projection",
        "postprocess_world_coordinates",
        "postprocess_speed_direction",
        "postprocess_gridmap_transform",
        "postprocess_trajectory",
        "postprocess_final_candidate",
    ]
    assert [
        str(call["safe_step_code"]) for call in probe.step_calls
    ] == expected_steps
    calls = {
        str(call["safe_step_code"]): call for call in probe.step_calls
    }
    for step in (
        "postprocess_projection",
        "postprocess_world_coordinates",
        "postprocess_speed_direction",
        "postprocess_gridmap_transform",
        "postprocess_trajectory",
    ):
        argv = calls[step]["argv"]
        assert Path(argv[-1]).name == expected_name
    final_argv = calls["postprocess_final_candidate"]["argv"]
    assert Path(final_argv[-1]).name == expected_name
    assert Path(final_argv[-3]).name == request.dataset_date

    runtime_root = probe.config.runtime_source_root
    assert runtime_root is not None
    assert calls["postprocess_projection"]["cwd"] == (
        runtime_root / "NuscenesAanlysis_smart_pts_project"
    )
    for step in (
        "postprocess_world_coordinates",
        "postprocess_speed_direction",
        "postprocess_trajectory",
        "postprocess_final_candidate",
    ):
        assert calls[step]["cwd"] == runtime_root / "2_pt_project"
    assert calls["postprocess_gridmap_transform"]["cwd"] == (
        runtime_root / "other_code"
    )
    data_python = probe.config.data_python
    assert calls["postprocess_projection"]["argv"] == [
        data_python,
        runtime_root / "NuscenesAanlysis_smart_pts_project" / "main.py",
        "--data_root",
        calls["postprocess_projection"]["argv"][-1],
    ]
    assert calls["postprocess_world_coordinates"]["argv"] == [
        data_python,
        runtime_root / "2_pt_project" / "0_img2world.py",
        calls["postprocess_world_coordinates"]["argv"][-1],
    ]
    assert calls["postprocess_speed_direction"]["argv"] == [
        data_python,
        runtime_root / "2_pt_project" / "4_speed_direction_odom.py",
        calls["postprocess_speed_direction"]["argv"][-1],
    ]
    assert calls["postprocess_gridmap_transform"]["argv"] == [
        data_python,
        runtime_root / "other_code" / "cp_gridmap.py",
        "--root_data",
        calls["postprocess_gridmap_transform"]["argv"][-1],
    ]
    assert calls["postprocess_trajectory"]["argv"] == [
        data_python,
        runtime_root / "2_pt_project" / "2_othermethod_cjl_0525.py",
        calls["postprocess_trajectory"]["argv"][-1],
    ]
    assert calls["postprocess_final_candidate"]["argv"] == [
        data_python,
        runtime_root / "2_pt_project" / "3_move_dir.py",
        "--root_path",
        calls["postprocess_final_candidate"]["argv"][-3],
        "--temp_path",
        calls["postprocess_final_candidate"]["argv"][-1],
    ]

    gridmap_bindings = calls["postprocess_gridmap_transform"][
        "readonly_bindings"
    ]
    final_bindings = calls["postprocess_final_candidate"][
        "readonly_bindings"
    ]
    assert gridmap_bindings == final_bindings
    assert len(gridmap_bindings) == 1
    private_clip_root, legacy_clip_root = gridmap_bindings[0]
    assert Path(private_clip_root).parts[-2:] == (".runtime", "clip_data")
    assert legacy_clip_root == probe.config.legacy_clip_data_root


def test_runtime_dependency_drift_stops_before_attempt_creation(
    tmp_path: Path,
) -> None:
    runtime, request, _clip_root, _lock_path = _runtime_for_snapshot_probe(
        tmp_path
    )
    _add_shared_tracked_inputs(request)
    request = _refresh_request_attestations(request)
    probe = _DependencyDriftProbeRuntime(runtime.config)

    with pytest.raises(RuntimeExecutionError) as failure:
        probe.run(request)

    assert failure.value.code == "postprocessing_dependency_mismatch"
    attempt_root = (
        probe.config.work_root
        / "jobs"
        / request.job_ref
        / "postprocessing"
        / request.run_ref
    )
    assert not attempt_root.exists()


def test_gridmap_transform_exit_zero_without_outputs_stops_before_trajectory(
    tmp_path: Path,
) -> None:
    runtime, request, _clip_root, _lock_path = _runtime_for_snapshot_probe(
        tmp_path
    )
    _add_shared_tracked_inputs(request)
    tracked_gridmap = request.segments[0].tracked_segment_root / "grid_map"
    tracked_gridmap.mkdir()
    (tracked_gridmap / "000001.json").write_text(
        '{"data":[1]}\n',
        encoding="utf-8",
    )
    request = _refresh_request_attestations(request)
    probe = _SilentGridmapTransformProbeRuntime(runtime.config)

    with pytest.raises(RuntimeExecutionError) as failure:
        probe.run(request)

    assert failure.value.code == "postprocess_gridmap_transform_failed"
    assert probe.called_steps == [
        "postprocess_projection",
        "postprocess_world_coordinates",
        "postprocess_speed_direction",
        "postprocess_gridmap_transform",
    ]
    attempt_root = (
        probe.config.work_root
        / "jobs"
        / request.job_ref
        / "postprocessing"
        / request.run_ref
    )
    backup = (
        attempt_root
        / ".runtime"
        / "gridmap-before-transform"
        / request.segments[0].segment_ref
    )
    assert _tree_sha256(backup) == _tree_sha256(tracked_gridmap)


def test_generated_gridmap_exit_zero_without_output_stops_before_projection(
    tmp_path: Path,
) -> None:
    runtime, request, clip_root, _lock_path = _runtime_for_snapshot_probe(
        tmp_path
    )
    source_gridmap = (
        clip_root
        / request.dataset_date
        / request.segments[0].source_clip
        / "sync_data"
        / request.segments[0].private_segment_key
        / "grid_map"
    )
    (source_gridmap / "000001.json").unlink()
    source_gridmap.rmdir()
    _add_shared_tracked_inputs(request)
    request = _refresh_request_attestations(
        replace(request, gridmap_decision="generate_from_pcd")
    )
    probe = _SilentGridmapTransformProbeRuntime(runtime.config)

    with pytest.raises(RuntimeExecutionError) as failure:
        probe.run(request)

    assert failure.value.code == "gridmap_decision_precondition_failed"
    assert probe.called_steps == ["postprocess_gridmap"]


def test_generated_gridmap_uses_private_writable_legacy_overlay(
    tmp_path: Path,
) -> None:
    runtime, request, clip_root, _lock_path = _runtime_for_snapshot_probe(
        tmp_path
    )
    source_gridmap = (
        clip_root
        / request.dataset_date
        / request.segments[0].source_clip
        / "sync_data"
        / request.segments[0].private_segment_key
        / "grid_map"
    )
    (source_gridmap / "000001.json").unlink()
    source_gridmap.rmdir()
    _add_shared_tracked_inputs(request)
    request = _refresh_request_attestations(
        replace(request, gridmap_decision="generate_from_pcd")
    )
    probe = _LegacyPathProbeRuntime(runtime.config)

    with pytest.raises(_LegacyPathProbeComplete):
        probe.run(request)

    gridmap_call = probe.step_calls[0]
    assert gridmap_call["safe_step_code"] == "postprocess_gridmap"
    assert gridmap_call["argv"][:2] == [
        probe.config.data_python,
        probe.config.runtime_source_root / "other_code" / "pcd_to_grid.py",
    ]
    assert gridmap_call["argv"][2:6] == [
        "--base-path",
        gridmap_call["argv"][3],
        "--date",
        request.dataset_date,
    ]
    assert gridmap_call["argv"][6:] == [
        "--segments",
        request.segments[0].source_clip,
    ]
    writable = gridmap_call["writable_bindings"]
    assert len(writable) == 1
    assert Path(writable[0][0]).parts[-2:] == (".runtime", "clip_data")
    assert writable[0][1] == probe.config.legacy_clip_data_root


def test_projection_ready_does_not_recopy_or_retransform_gridmap(
    tmp_path: Path,
) -> None:
    runtime, request, _clip_root, _lock_path = _runtime_for_snapshot_probe(
        tmp_path
    )
    _add_shared_tracked_inputs(request)
    grid_map = request.segments[0].tracked_segment_root / "grid_map"
    grid_map.mkdir()
    (grid_map / "000001.json").write_text("{}", encoding="utf-8")
    request = _refresh_request_attestations(
        replace(request, gridmap_decision="skip_if_projection_ready")
    )
    probe = _LegacyPathProbeRuntime(runtime.config)

    with pytest.raises(_LegacyPathProbeComplete):
        probe.run(request)

    called_steps = {
        str(call["safe_step_code"]) for call in probe.step_calls
    }
    assert "postprocess_gridmap_transform" not in called_steps
    assert "postprocess_trajectory" in called_steps


def test_clip_snapshot_copy_holds_writer_lock_and_exception_releases_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, request, clip_root, lock_path = _runtime_for_snapshot_probe(
        tmp_path
    )
    original_copy = postprocessing_module._copy_tree_bytes
    copied_source = threading.Event()

    def copy_probe(source: Path, destination: Path) -> None:
        if source.is_relative_to(clip_root):
            assert active_writer_lock_fds()
            original_copy(source, destination)
            assert active_writer_lock_fds()
            copied_source.set()
            raise _SnapshotProbeComplete
        original_copy(source, destination)

    monkeypatch.setattr(
        postprocessing_module,
        "_copy_tree_bytes",
        copy_probe,
    )

    with pytest.raises(_SnapshotProbeComplete):
        runtime.run(request)

    assert copied_source.is_set()
    assert active_writer_lock_fds() == ()
    assert not writer_active_path(lock_path).exists()
    with navigation_writer_lock(lock_path=lock_path):
        assert active_writer_lock_fds()


def test_navigation_writer_blocks_annotation_source_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, request, clip_root, lock_path = _runtime_for_snapshot_probe(
        tmp_path
    )
    original_copy = postprocessing_module._copy_tree_bytes
    navigation_holds_lock = threading.Event()
    release_navigation = threading.Event()
    copied_source = threading.Event()
    annotation_finished = threading.Event()
    failures: list[BaseException] = []

    def copy_probe(source: Path, destination: Path) -> None:
        if source.is_relative_to(clip_root):
            assert active_writer_lock_fds()
            original_copy(source, destination)
            copied_source.set()
            raise _SnapshotProbeComplete
        original_copy(source, destination)

    def hold_navigation_writer() -> None:
        with navigation_writer_lock(lock_path=lock_path):
            navigation_holds_lock.set()
            assert release_navigation.wait(timeout=5)

    def run_annotation() -> None:
        try:
            runtime.run(request)
        except _SnapshotProbeComplete:
            pass
        except BaseException as exc:  # pragma: no cover - assertion surface
            failures.append(exc)
        finally:
            annotation_finished.set()

    monkeypatch.setattr(
        postprocessing_module,
        "_copy_tree_bytes",
        copy_probe,
    )
    navigation_thread = threading.Thread(target=hold_navigation_writer)
    annotation_thread = threading.Thread(target=run_annotation)
    navigation_thread.start()
    assert navigation_holds_lock.wait(timeout=5)
    annotation_thread.start()

    assert not copied_source.wait(timeout=0.2)
    assert not annotation_finished.is_set()
    release_navigation.set()
    navigation_thread.join(timeout=5)
    annotation_thread.join(timeout=5)

    assert not navigation_thread.is_alive()
    assert not annotation_thread.is_alive()
    assert copied_source.is_set()
    assert annotation_finished.is_set()
    assert failures == []
    assert not writer_active_path(lock_path).exists()

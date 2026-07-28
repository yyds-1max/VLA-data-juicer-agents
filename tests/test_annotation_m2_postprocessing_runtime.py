from __future__ import annotations

import hashlib
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

    def _revalidate_postprocessing_inputs(
        self,
        expected_manifest_sha256: str,
    ) -> None:
        self.revalidated.append(expected_manifest_sha256)


class _SnapshotProbeRuntime(_ValidationRuntime):
    def _current_runtime_manifest_sha256(
        self,
        expected_manifest_sha256: str,
    ) -> str:
        return expected_manifest_sha256


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

    with pytest.raises(RuntimeExecutionError) as failure:
        runtime.preflight(
            localization_kind="ins",
            gridmap_decision="copy_existing_gridmap",
            trajectory_variant="cjl_with_gridmap",
        )
    assert failure.value.code == "unsupported_runtime_variant"
    assert runtime.revalidated == [expected]


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

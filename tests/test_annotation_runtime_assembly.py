from __future__ import annotations

from pathlib import Path

from vla_data_juicer_agents.annotation.fix_runtime import (
    FixCompatibilityPublisher,
    NavigationFixRuntime,
)
from vla_data_juicer_agents.annotation.postprocessing_runtime import (
    CompatibilityPublisher,
    NavigationPostprocessingRuntime,
)
from vla_data_juicer_agents.annotation.runtime import (
    NavigationAnnotationRuntimeConfig,
    NavigationAnnotationRuntimeDriver,
    RuntimeCapabilities,
)
from vla_data_juicer_agents.annotation.worker import AnnotationWorker


def _runtime_config(tmp_path: Path) -> NavigationAnnotationRuntimeConfig:
    return NavigationAnnotationRuntimeConfig(
        runtime_source_root=None,
        work_root=None,
        clip_data_root=None,
        data_python=None,
        data_env_setup=None,
        manifest_path=tmp_path / "manifest.json",
    )


def test_default_worker_assembles_all_m2_runtimes_from_driver_config(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = _runtime_config(tmp_path)
    finish_data_root = tmp_path / "VLADatasets" / "finish_data"
    finish_data_root.mkdir(parents=True)
    monkeypatch.setenv("VLA_VLADATASETS_ROOT", str(finish_data_root.parent))
    monkeypatch.setattr(
        NavigationAnnotationRuntimeConfig,
        "from_env",
        classmethod(lambda cls: config),
    )

    worker = AnnotationWorker(object())

    assert isinstance(worker.runtime, NavigationAnnotationRuntimeDriver)
    assert worker.runtime.config is config
    assert isinstance(
        worker.postprocessing_runtime,
        NavigationPostprocessingRuntime,
    )
    assert worker.postprocessing_runtime.config is config
    assert isinstance(worker.postprocessing_publisher, CompatibilityPublisher)
    assert worker.postprocessing_publisher.finish_data_root == finish_data_root
    assert isinstance(worker.fix_runtime, NavigationFixRuntime)
    assert worker.fix_runtime.config is config
    assert isinstance(worker.fix_publisher, FixCompatibilityPublisher)


def test_default_worker_m2_preflight_reaches_assembled_real_objects(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = _runtime_config(tmp_path)
    finish_data_root = tmp_path / "VLADatasets" / "finish_data"
    finish_data_root.mkdir(parents=True)
    monkeypatch.setenv("VLA_VLADATASETS_ROOT", str(finish_data_root.parent))
    monkeypatch.setattr(
        NavigationAnnotationRuntimeConfig,
        "from_env",
        classmethod(lambda cls: config),
    )
    postprocessing_calls: list[dict[str, str | None]] = []
    fix_calls: list[NavigationFixRuntime] = []

    def postprocessing_preflight(
        runtime: NavigationPostprocessingRuntime,
        *,
        localization_kind: str | None = None,
        gridmap_decision: str | None = None,
        trajectory_variant: str | None = None,
    ) -> str:
        assert runtime.config is config
        postprocessing_calls.append(
            {
                "localization_kind": localization_kind,
                "gridmap_decision": gridmap_decision,
                "trajectory_variant": trajectory_variant,
            }
        )
        return "a" * 64

    def fix_preflight(runtime: NavigationFixRuntime) -> str:
        assert runtime.config is config
        fix_calls.append(runtime)
        return "b" * 64

    monkeypatch.setattr(
        NavigationPostprocessingRuntime,
        "preflight",
        postprocessing_preflight,
    )
    monkeypatch.setattr(NavigationFixRuntime, "preflight", fix_preflight)
    worker = AnnotationWorker(object())
    monkeypatch.setattr(
        worker.runtime,
        "capabilities",
        lambda: RuntimeCapabilities(available=True),
    )

    decision = {
        "localization_kind": "odom",
        "gridmap_decision": "generate_from_pcd",
        "trajectory_variant": "cjl_0525_with_gridmap",
    }
    assert worker.preflight_runtime_stage(
        "postprocessing",
        decision=decision,
    ) == {
        "available": True,
        "runtime_id": "navigation_odom_v1",
        "runtime_manifest_sha256": "a" * 64,
    }
    assert postprocessing_calls == [decision]
    assert worker.preflight_runtime_stage("fix") == {
        "available": True,
        "runtime_id": "navigation_odom_v1",
        "runtime_manifest_sha256": "b" * 64,
    }
    assert fix_calls == [worker.fix_runtime]

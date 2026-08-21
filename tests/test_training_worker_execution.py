from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import sys
import time

import pytest

from vla_data_juicer_agents.training_worker.client import OfflineCenterClient
from vla_data_juicer_agents.training_worker.datasets import (
    DATASET_MARKER_CONTRACT,
    DATASET_MARKER_NAME,
)
from vla_data_juicer_agents.training_worker.execution import (
    TrainingExecutionError,
    TrainingExecutionManager,
    VERSION_MARKER,
    _parse_event,
)
from vla_data_juicer_agents.training_worker.identity import load_or_create_identity
from vla_data_juicer_agents.training_worker.ledger import WorkerLedger


class _GpuCollector:
    def collect(self) -> dict[str, object]:
        return {
            "gpus": [
                {
                    "uuid": "GPU-one",
                    "index": 3,
                    "memory_used_bytes": 1024 * 1024,
                    "utilization_percent": 25.0,
                    "temperature_celsius": 41.0,
                },
                {
                    "uuid": "GPU-two",
                    "index": 7,
                    "memory_used_bytes": 2 * 1024 * 1024,
                    "utilization_percent": 50.0,
                    "temperature_celsius": 45.0,
                },
            ]
        }


class _RecordingCenter(OfflineCenterClient):
    def __init__(self) -> None:
        self.updates: list[tuple[str, dict[str, object]]] = []

    def publish_run_updates(self, identity, run_ref, payload):  # type: ignore[no-untyped-def]
        self.updates.append((run_ref, dict(payload)))
        return {"accepted": True}


def _manager(tmp_path: Path) -> tuple[TrainingExecutionManager, WorkerLedger, _RecordingCenter]:
    state_dir = tmp_path / "worker-state"
    center = _RecordingCenter()
    ledger = WorkerLedger(state_dir / "worker-ledger.sqlite")
    manager = TrainingExecutionManager(
        identity=load_or_create_identity(state_dir),
        ledger=ledger,
        center_client=center,
        resource_collector=_GpuCollector(),  # type: ignore[arg-type]
        state_dir=state_dir,
        stop_timeout_seconds=0.5,
    )
    return manager, ledger, center


def _start_action(
    tmp_path: Path,
    *,
    code: str,
    monitoring_format: str = "transformers",
    extras: dict[str, object] | None = None,
) -> dict[str, object]:
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    (project / "train.py").write_text(code + "\n", encoding="utf-8")
    output_root = tmp_path / "outputs"
    payload: dict[str, object] = {
        "stage_ref": "stage_one",
        "stage_number": 1,
        "version_label": "v1-20260820",
        "family_ref": "family_navila",
        "working_directory": str(project),
        "entrypoint": "train.py",
        "output_root": str(output_root),
        "output_directory": str(
            output_root / "family_navila" / "v1-20260820" / "stage-01"
        ),
        "argv": [sys.executable, "train.py"],
        "gpu_uuids": ["GPU-two", "GPU-one"],
        "runtime_environment": {"kind": "system"},
        "monitoring": {"source": "stdout", "format": monitoring_format},
    }
    payload.update(extras or {})
    return {
        "action_ref": "action_start",
        "claim_token": "claim_" + "a" * 32,
        "kind": "start_training_stage",
        "run_ref": "run_real_one",
        "owner_epoch": 1,
        "payload": payload,
    }


def test_real_executor_launches_without_shell_and_reports_log_metric_and_exit(
    tmp_path: Path,
) -> None:
    manager, ledger, center = _manager(tmp_path)
    action = _start_action(
        tmp_path,
        code=(
            "import json,os; "
            "print(json.dumps({'loss':0.5,'learning_rate':1e-5,'epoch':1.0,'step':1,'total_steps':1})); "
            "print(os.environ['CUDA_VISIBLE_DEVICES']); print('private-token')"
        ),
        extras={"redactions": ["private-token"]},
    )

    result = manager.handle_action(action)
    assert result["status"] == "succeeded"

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        manager.tick()
        row = ledger.get_run("run_real_one")
        if row is not None and row["state"] == "succeeded":
            break
        time.sleep(0.05)

    row = ledger.get_run("run_real_one")
    assert row is not None
    assert row["state"] == "succeeded"
    assert row["gpu_uuids"] == ["GPU-one", "GPU-two"]
    version_root = tmp_path / "outputs" / "family_navila" / "v1-20260820"
    assert json.loads((version_root / VERSION_MARKER).read_text())["run_ref"] == "run_real_one"
    log = (version_root / "stage-01" / ".datapilot-training.log").read_text()
    assert "7,3" in log
    assert "private-token" in log
    updates = [item for _, envelope in center.updates for item in envelope["updates"]]  # type: ignore[index]
    assert {item["kind"] for item in updates} >= {"accepted", "started", "log", "metric", "exited"}
    metric = next(
        item for item in updates if item["kind"] == "metric" and "loss" in item
    )
    assert metric["loss"] == 0.5
    assert metric["step"] == 1
    gpu_metric = next(item for item in updates if item.get("gpus"))
    assert {gpu["uuid"] for gpu in gpu_metric["gpus"]} == {"GPU-one", "GPU-two"}
    assert {gpu["gpu_memory_mib"] for gpu in gpu_metric["gpus"]} == {1.0, 2.0}
    public_logs = "\n".join(
        str(item["message"]) for item in updates if item["kind"] == "log"
    )
    assert "private-token" not in public_logs
    assert "********" in public_logs
    assert ledger.pending_run_refs() == []


def test_real_executor_validates_and_writes_managed_dataset_manifest(
    tmp_path: Path,
) -> None:
    manager, ledger, _center = _manager(tmp_path)
    replica = tmp_path / "data" / "datapilot-managed" / "20260416-release12"
    replica.mkdir(parents=True)
    digest = "d" * 64
    release_ref = "release123456789012"
    (replica / DATASET_MARKER_NAME).write_text(
        json.dumps(
            {
                "contract": DATASET_MARKER_CONTRACT,
                "release_ref": release_ref,
                "dataset_date": "20260416",
                "inventory_sha256": digest,
            }
        ),
        encoding="utf-8",
    )
    version_root = tmp_path / "outputs" / "family_navila" / "v1-20260820"
    manifest = {
        "contract": "datapilot_dataset_manifest_v1",
        "snapshot_ref": "snapshot_one",
        "run_ref": "run_real_one",
        "family_ref": "family_navila",
        "splits": {
            "train": [
                {
                    "dataset_date": "20260416",
                    "release_ref": release_ref,
                    "replica_ref": "replica_one",
                    "local_root": str(replica),
                    "inventory_sha256": digest,
                }
            ],
            "test": [],
        },
    }
    action = _start_action(
        tmp_path,
        code="print('ok')",
        monitoring_format="plain",
        extras={
            "dataset_manifest_path": str(version_root / "dataset-manifest.json"),
            "dataset_manifest": manifest,
            "dataset_replicas": [
                {
                    "local_root": str(replica),
                    "release_ref": release_ref,
                    "dataset_date": "20260416",
                    "inventory_sha256": digest,
                }
            ],
        },
    )

    assert manager.handle_action(action)["status"] == "succeeded"
    assert json.loads((version_root / "dataset-manifest.json").read_text()) == manifest

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and ledger.get_run("run_real_one")["state"] != "succeeded":  # type: ignore[index]
        manager.tick()
        time.sleep(0.05)
    assert ledger.get_run("run_real_one")["state"] == "succeeded"  # type: ignore[index]


def test_real_executor_stop_terminates_process_group_and_reports_cancelled(
    tmp_path: Path,
) -> None:
    manager, ledger, center = _manager(tmp_path)
    assert manager.handle_action(
        _start_action(tmp_path, code="import time; print('ready', flush=True); time.sleep(60)")
    )["status"] == "succeeded"

    result = manager.handle_action(
        {
            "action_ref": "action_stop",
            "claim_token": "claim_" + "b" * 32,
            "kind": "stop_training_run",
            "run_ref": "run_real_one",
            "owner_epoch": 1,
            "payload": {},
        }
    )

    assert result["status"] == "succeeded"
    assert ledger.get_run("run_real_one")["state"] == "cancelled"  # type: ignore[index]
    updates = [item for _, envelope in center.updates for item in envelope["updates"]]  # type: ignore[index]
    assert any(item["kind"] == "exited" and item["status"] == "cancelled" for item in updates)


def test_running_supervisor_that_disappears_is_reported_lost(tmp_path: Path) -> None:
    manager, ledger, center = _manager(tmp_path)
    state_path = tmp_path / "supervisor.json"
    missing_pid = 2_000_000_000
    state_path.write_text(
        json.dumps(
            {
                "contract": "datapilot_training_supervisor_v1",
                "status": "running",
                "supervisor_pid": missing_pid,
                "child_pid": missing_pid + 1,
                "exit_code": None,
            }
        ),
        encoding="utf-8",
    )
    ledger.record_process_observation(
        run_ref="run_missing_supervisor",
        state="running",
        pid=missing_pid,
        stage_ref="stage_missing",
        owner_epoch=1,
        supervisor_state_path=str(state_path),
    )

    manager.tick()

    assert ledger.get_run("run_missing_supervisor")["state"] == "unknown"  # type: ignore[index]
    updates = [item for _, envelope in center.updates for item in envelope["updates"]]  # type: ignore[index]
    assert any(
        item == {
            "kind": "reconciliation",
            "stage_ref": "stage_missing",
            "status": "lost",
            "reason": "supervisor_missing",
        }
        for item in updates
    )


def test_worker_ledger_migrates_v1_to_v2_and_persists_outbox(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite"
    ledger = WorkerLedger(path)
    ledger.record_process_observation(
        run_ref="run_outbox",
        state="accepted",
        owner_epoch=3,
        stage_ref="stage_1",
    )
    sequence = ledger.enqueue_update(
        "run_outbox", 3, {"kind": "accepted", "stage_ref": "stage_1"}
    )

    assert sequence == 1
    assert ledger.pending_updates("run_outbox") == [
        {
            "owner_epoch": 3,
            "worker_seq": 1,
            "kind": "accepted",
            "stage_ref": "stage_1",
        }
    ]
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT value FROM worker_ledger_metadata WHERE key='schema_version'"
        ).fetchone()[0] == "2"


def test_conda_runtime_is_a_fixed_no_capture_wrapper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, _ledger, _center = _manager(tmp_path)
    conda = tmp_path / "conda"
    conda.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    conda.chmod(0o700)
    monkeypatch.setenv("DATAPILOT_CONDA_EXECUTABLE", str(conda))

    assert manager._runtime_argv(  # noqa: SLF001 - execution contract unit test
        {"kind": "conda", "conda_environment": "navila"},
        ["torchrun", "train.py"],
        tmp_path,
    ) == [
        str(conda),
        "run",
        "--no-capture-output",
        "-n",
        "navila",
        "torchrun",
        "train.py",
    ]


def test_system_runtime_resolves_relative_executable_from_working_directory(
    tmp_path: Path,
) -> None:
    manager, _ledger, _center = _manager(tmp_path)
    workdir = tmp_path / "project"
    executable = workdir / "venv" / "bin" / "python"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)

    assert manager._runtime_argv(  # noqa: SLF001 - execution contract unit test
        {"kind": "system"},
        ["./venv/bin/python", "train.py"],
        workdir,
    ) == [str(executable.resolve()), "train.py"]


def test_system_runtime_rejects_relative_executable_symlink_escape(
    tmp_path: Path,
) -> None:
    manager, _ledger, _center = _manager(tmp_path)
    workdir = tmp_path / "project"
    workdir.mkdir()
    outside = tmp_path / "outside-python"
    outside.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    outside.chmod(0o700)
    (workdir / "python").symlink_to(outside)

    with pytest.raises(TrainingExecutionError) as raised:
        manager._runtime_argv(  # noqa: SLF001 - execution contract unit test
            {"kind": "system"},
            ["./python", "train.py"],
            workdir,
        )

    assert raised.value.code == "training_executable_unsafe"


def test_jsonl_checkpoint_requires_safe_relative_path() -> None:
    assert _parse_event(
        'DATAPILOT_EVENT {"contract":"datapilot_training_event_v1","type":"checkpoint","step":10,"relative_path":"checkpoint-10"}',
        "jsonl",
    ) == {"kind": "checkpoint", "step": 10, "relative_path": "checkpoint-10"}
    assert _parse_event(
        '{"contract":"datapilot_training_event_v1","type":"checkpoint","relative_path":"../outside"}',
        "jsonl",
    ) is None
    assert _parse_event(
        '{"contract":"datapilot_training_event_v1","type":"checkpoint","step":"10","relative_path":"checkpoint-10"}',
        "jsonl",
    ) == {"kind": "checkpoint", "relative_path": "checkpoint-10"}


def test_transformers_metric_accepts_tqdm_prefixed_python_dict() -> None:
    assert _parse_event(
        "\r100%|██████████| 2/2 [00:03<00:00, 1.81s/it] "
        "{'loss': 0.25, 'learning_rate': 1e-5, 'epoch': 0.5, "
        "'step': 2, 'total_steps': 2}",
        "transformers",
    ) == {
        "kind": "metric",
        "step": 2,
        "total_steps": 2,
        "epoch": 0.5,
        "loss": 0.25,
        "learning_rate": 1e-5,
    }


def test_transformers_metric_uses_tqdm_progress_when_dict_omits_step() -> None:
    assert _parse_event(
        "\r  1%|▏         | 1/100 [00:03<05:20, 3.24s/it] "
        "{'loss': 0.5, 'learning_rate': 9e-6, 'epoch': 0.03}",
        "transformers",
    ) == {
        "kind": "metric",
        "step": 1,
        "total_steps": 100,
        "epoch": 0.03,
        "loss": 0.5,
        "learning_rate": 9e-6,
    }


def test_jsonl_metric_does_not_accept_prefixed_payload() -> None:
    assert _parse_event(
        'progress {"contract":"datapilot_training_event_v1",'
        '"type":"metric","step":1,"loss":0.5}',
        "jsonl",
    ) is None


def test_real_executor_rejects_output_root_symlink_escape(tmp_path: Path) -> None:
    manager, _ledger, _center = _manager(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    output_root = tmp_path / "outputs"
    output_root.symlink_to(outside, target_is_directory=True)
    action = _start_action(tmp_path, code="print('must not run')")

    result = manager.handle_action(action)

    assert result["status"] == "failed"
    assert result["error"]["code"] == "training_output_unsafe"  # type: ignore[index]
    assert list(outside.iterdir()) == []


def test_stop_without_local_process_is_unresolved_not_fake_cancelled(
    tmp_path: Path,
) -> None:
    manager, _ledger, _center = _manager(tmp_path)

    result = manager.handle_action(
        {
            "action_ref": "action_stop_unknown",
            "claim_token": "c" * 43,
            "kind": "stop_training_run",
            "run_ref": "run_unknown",
            "owner_epoch": 1,
            "payload": {},
        }
    )

    assert result["status"] == "failed"
    assert result["error"]["code"] == "training_stop_unresolved"  # type: ignore[index]


def test_launch_intent_recovers_after_crash_without_starting_a_second_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, ledger, _center = _manager(tmp_path)
    launches = tmp_path / "launches.txt"
    action = _start_action(
        tmp_path,
        code=(
            "from pathlib import Path; import os,time; "
            f"Path({str(launches)!r}).open('a').write(str(os.getpid())+'\\n'); "
            "time.sleep(60)"
        ),
    )
    original_attach = ledger.attach_launch_supervisor

    def crash_before_attach(*args: object, **kwargs: object) -> None:
        raise SystemExit("simulated worker SIGKILL window")

    monkeypatch.setattr(ledger, "attach_launch_supervisor", crash_before_attach)
    with pytest.raises(SystemExit, match="SIGKILL window"):
        manager.handle_action(action)
    intent = ledger.get_run("run_real_one")
    assert intent is not None and intent["state"] == "accepted"
    assert not launches.exists()

    monkeypatch.setattr(ledger, "attach_launch_supervisor", original_attach)
    recovered = TrainingExecutionManager(
        identity=manager.identity,
        ledger=ledger,
        center_client=manager.center_client,
        resource_collector=_GpuCollector(),  # type: ignore[arg-type]
        state_dir=manager.state_dir,
        stop_timeout_seconds=0.5,
    )
    assert recovered.handle_action(action)["status"] == "succeeded"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not launches.exists():
        time.sleep(0.05)
    assert len(launches.read_text(encoding="utf-8").splitlines()) == 1

    stop = recovered.handle_action(
        {
            "action_ref": "action_stop_recovered",
            "claim_token": "d" * 43,
            "kind": "stop_training_run",
            "run_ref": "run_real_one",
            "owner_epoch": 1,
            "payload": {},
        }
    )
    assert stop["status"] == "succeeded"


def test_stop_cancels_durable_launch_intent_before_child_is_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, ledger, _center = _manager(tmp_path)
    started = tmp_path / "must-not-start.txt"
    action = _start_action(
        tmp_path,
        code=f"from pathlib import Path; Path({str(started)!r}).write_text('started')",
    )

    def crash_before_attach(*args: object, **kwargs: object) -> None:
        raise SystemExit("simulated worker crash")

    monkeypatch.setattr(ledger, "attach_launch_supervisor", crash_before_attach)
    with pytest.raises(SystemExit):
        manager.handle_action(action)

    result = manager.handle_action(
        {
            "action_ref": "action_stop_launch_intent",
            "claim_token": "e" * 43,
            "kind": "stop_training_run",
            "run_ref": "run_real_one",
            "owner_epoch": 1,
            "payload": {},
        }
    )

    assert result["status"] == "succeeded"
    assert ledger.get_run("run_real_one")["state"] == "cancelled"  # type: ignore[index]
    time.sleep(0.2)
    assert not started.exists()


def test_non_finite_metric_does_not_block_terminal_update(tmp_path: Path) -> None:
    manager, ledger, center = _manager(tmp_path)
    action = _start_action(
        tmp_path,
        code="print('{\"loss\": NaN, \"grad_norm\": Infinity}')",
    )
    assert manager.handle_action(action)["status"] == "succeeded"

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        manager.tick()
        row = ledger.get_run("run_real_one")
        if row is not None and row["state"] == "succeeded":
            break
        time.sleep(0.05)
    updates = [item for _, envelope in center.updates for item in envelope["updates"]]  # type: ignore[index]
    assert not any(
        item["kind"] == "metric" and ("loss" in item or "grad_norm" in item)
        for item in updates
    )
    assert any(
        item["kind"] == "exited" and item["status"] == "succeeded"
        for item in updates
    )

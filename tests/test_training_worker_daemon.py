from __future__ import annotations

import json
from io import StringIO
import os
from pathlib import Path
import ssl
import stat
from urllib.error import HTTPError

import pytest

from vla_data_juicer_agents.training_worker.client import (
    CenterClientError,
    HttpCenterClient,
    OfflineCenterClient,
)
from vla_data_juicer_agents.training_worker.daemon import TrainingWorkerDaemon
from vla_data_juicer_agents.training_worker.identity import (
    load_or_create_identity,
    load_worker_token,
    store_worker_token,
)
from vla_data_juicer_agents.training_worker.ledger import (
    ProcessObservation,
    ProcessProbeResult,
    WorkerLedger,
)
from vla_data_juicer_agents.training_worker.resources import (
    CommandResult,
    NVIDIA_SMI_COMMAND,
    ResourceCollector,
)
import vla_data_juicer_agents.training_worker.resources as worker_resources
from vla_data_juicer_agents.training_worker.cli import (
    _center_ssl_context,
    _read_enrollment_token,
)


def test_worker_identity_is_stable_private_and_not_publicly_serialized(tmp_path: Path) -> None:
    state_dir = tmp_path / "worker-state"

    first = load_or_create_identity(state_dir)
    second = load_or_create_identity(state_dir)

    assert first == second
    assert first.worker_id.startswith("worker-")
    assert len(first.credential) >= 32
    assert "credential" not in first.public_payload()
    if os.name == "posix":
        identity_mode = stat.S_IMODE((state_dir / "identity.json").stat().st_mode)
        assert identity_mode == 0o600


def test_worker_identity_rejects_symlink(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    (state_dir / "identity.json").symlink_to(target)

    with pytest.raises(RuntimeError, match="symbolic link"):
        load_or_create_identity(state_dir)


def test_worker_token_is_private_and_stable(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    token = "worker_" + "a" * 48

    path = store_worker_token(state_dir, token)

    assert load_worker_token(state_dir) == token
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


class _StaticProbe:
    def __init__(self, result: ProcessProbeResult) -> None:
        self.result = result
        self.observations: list[ProcessObservation] = []

    def inspect(self, observation: ProcessObservation) -> ProcessProbeResult:
        self.observations.append(observation)
        return self.result


def test_ledger_conservatively_marks_missing_process_unknown(tmp_path: Path) -> None:
    ledger = WorkerLedger(tmp_path / "ledger.sqlite")
    ledger.record_process_observation(
        run_ref="run-1",
        state="running",
        pid=12345,
        process_start_marker="9001",
        argv_digest="abc123",
        gpu_uuids=["GPU-b", "GPU-a", "GPU-a"],
        working_directory="/work/project",
    )
    probe = _StaticProbe(ProcessProbeResult("missing", "not found"))

    results = ledger.reconcile_active_runs(probe)

    assert [result.to_payload() for result in results] == [
        {
            "run_ref": "run-1",
            "status": "missing",
            "previous_state": "running",
            "current_state": "unknown",
            "detail": "not found",
        }
    ]
    row = ledger.get_run("run-1")
    assert row is not None
    assert row["state"] == "unknown"
    assert row["last_reconciliation"] == "missing"
    assert row["gpu_uuids"] == ["GPU-a", "GPU-b"]
    assert probe.observations == [ProcessObservation(12345, "9001", "abc123")]


def test_ledger_keeps_verified_process_running(tmp_path: Path) -> None:
    ledger = WorkerLedger(tmp_path / "ledger.sqlite")
    ledger.record_process_observation(
        run_ref="run-verified",
        state="running",
        pid=23456,
        process_start_marker="20",
        argv_digest="digest",
    )

    result = ledger.reconcile_active_runs(
        _StaticProbe(ProcessProbeResult("matched", "identity matches"))
    )[0]

    assert result.current_state == "running"
    assert ledger.get_run("run-verified")["last_reconciliation"] == "matched"  # type: ignore[index]


def test_active_ledger_observation_requires_pid(tmp_path: Path) -> None:
    ledger = WorkerLedger(tmp_path / "ledger.sqlite")

    with pytest.raises(ValueError, match="positive pid"):
        ledger.record_process_observation(run_ref="run-no-pid", state="running")


def test_resource_collector_uses_only_fixed_nvidia_smi_argv(tmp_path: Path) -> None:
    calls: list[tuple[tuple[str, ...], float]] = []

    def runner(command: tuple[str, ...], timeout: float) -> CommandResult:
        calls.append((command, timeout))
        return CommandResult(
            0,
            "GPU-001, 0, NVIDIA A100-SXM4-80GB, 81920, 2048, 17, 41\n",
            "",
        )

    collector = ResourceCollector(disk_paths=[tmp_path], gpu_command_runner=runner)
    resources = collector.collect()

    assert calls == [(NVIDIA_SMI_COMMAND, 5.0)]
    assert resources["gpu_collection"] == {"source": "nvidia-smi", "error": None}
    assert resources["gpus"] == [
        {
            "uuid": "GPU-001",
            "index": 0,
            "name": "NVIDIA A100-SXM4-80GB",
            "memory_total_bytes": 81920 * 1024 * 1024,
            "memory_used_bytes": 2048 * 1024 * 1024,
            "utilization_percent": 17.0,
            "temperature_celsius": 41.0,
        }
    ]
    assert resources["host"]["hostname"]  # type: ignore[index]
    assert resources["cpu"]["logical_cores"] >= 1  # type: ignore[index]
    assert "load_1m" in resources["cpu"]  # type: ignore[operator]
    assert resources["disks"][0]["mount"] == str(tmp_path.resolve())  # type: ignore[index]
    assert resources["disks"][0]["available_bytes"] >= 0  # type: ignore[index]


def test_nvidia_smi_fallback_never_uses_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> object:
        recorded["command"] = command
        recorded.update(kwargs)

        class Completed:
            returncode = 0
            stdout = ""
            stderr = ""

        return Completed()

    monkeypatch.setattr(worker_resources.subprocess, "run", fake_run)

    result = worker_resources._run_fixed_nvidia_smi(NVIDIA_SMI_COMMAND, 5.0)

    assert result.returncode == 0
    assert recorded["command"] == list(NVIDIA_SMI_COMMAND)
    assert recorded["shell"] is False
    assert recorded["timeout"] == 5.0

    with pytest.raises(ValueError, match="fixed nvidia-smi"):
        worker_resources._run_fixed_nvidia_smi(("echo", "unsafe"), 5.0)


def test_daemon_health_payload_has_no_secret_and_no_execution(tmp_path: Path) -> None:
    state_dir = tmp_path / "worker-state"
    identity = load_or_create_identity(state_dir)
    ledger = WorkerLedger(state_dir / "ledger.sqlite")

    def no_gpu(command: tuple[str, ...], timeout: float) -> CommandResult:
        return CommandResult(1, "", "driver unavailable")

    daemon = TrainingWorkerDaemon(
        identity=identity,
        ledger=ledger,
        resource_collector=ResourceCollector(
            disk_paths=[tmp_path],
            gpu_command_runner=no_gpu,
        ),
        center_client=OfflineCenterClient(),
        monotonic_clock=lambda: 100.0,
    )

    payload = daemon.run_once()
    serialized = json.dumps(payload)

    assert payload["worker_id"] == identity.worker_id
    assert payload["sequence"] == 1
    assert payload["health"]["status"] == "degraded"  # type: ignore[index]
    assert payload["health"]["execution_enabled"] is False  # type: ignore[index]
    assert payload["capabilities"]["training_execution"] is False  # type: ignore[index]
    assert payload["capabilities"]["arbitrary_command_execution"] is False  # type: ignore[index]
    assert identity.credential not in serialized


def test_enrollment_token_is_read_from_stdin_and_never_needs_argv() -> None:
    token = "enroll_" + "x" * 48

    assert _read_enrollment_token(StringIO(token + "\n")) == token
    with pytest.raises(SystemExit, match="valid enrollment token"):
        _read_enrollment_token(StringIO("not-a-token\n"))


def test_worker_loads_configured_center_ca_certificate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    certificate = Path(__file__).parent / "fixtures" / "training_center_ca.pem"
    monkeypatch.setenv("DATAPILOT_CENTER_CA_CERT_PATH", str(certificate))

    context = _center_ssl_context()

    assert context is not None
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_worker_rejects_unavailable_center_ca_certificate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATAPILOT_CENTER_CA_CERT_PATH", "/missing/center-ca.pem")

    with pytest.raises(SystemExit, match="could not be loaded"):
        _center_ssl_context()


class _FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, limit: int) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


class _RecordingOpener:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = responses
        self.requests: list[tuple[object, float]] = []

    def open(self, request: object, timeout: float) -> _FakeResponse:
        self.requests.append((request, timeout))
        return _FakeResponse(self.responses.pop(0))


def test_http_center_protocol_enrolls_then_sends_bearer_heartbeat(tmp_path: Path) -> None:
    identity = load_or_create_identity(tmp_path / "state")
    client = HttpCenterClient(center_base_url="https://center.example/base", timeout_seconds=4)
    opener = _RecordingOpener(
        [
            {"node": {"node_ref": "node_123"}, "worker_token": "worker_" + "z" * 48},
            {"node_ref": "node_123", "status": "online"},
        ]
    )
    client._opener = opener  # type: ignore[assignment]
    capabilities = {
        "hostname": "train-host",
        "operating_system": "Linux 6",
        "architecture": "x86_64",
        "python_version": "3.11",
        "nvidia_driver_version": None,
        "cuda_version": None,
        "conda_environments": [],
        "worker_features": ["resource_inventory"],
    }

    enrollment = client.enroll(identity, "enroll_" + "x" * 48, capabilities)
    heartbeat_response = client.publish_heartbeat(
        identity,
        {
            "health": {"status": "healthy"},
            "resources": {
                "host": {
                    "hostname": "train-host",
                    "os": "Linux",
                    "os_release": "6",
                    "architecture": "x86_64",
                },
                "cpu": {"logical_cores": 8, "load_1m": 1.25},
                "memory": {"total_bytes": 1000, "available_bytes": 500},
                "disks": [{"mount": "/", "total_bytes": 2000, "available_bytes": 1000}],
                "gpus": [
                    {
                        "uuid": "GPU-1",
                        "index": 0,
                        "name": "A100",
                        "memory_total_bytes": 100,
                        "memory_used_bytes": 50,
                        "utilization_percent": 12.0,
                        "temperature_celsius": 40.0,
                    }
                ],
                "gpu_collection": {"source": "nvml", "error": None},
            },
        },
    )

    assert enrollment.node_ref == "node_123"
    assert heartbeat_response["status"] == "online"
    enroll_request = opener.requests[0][0]
    heartbeat_request = opener.requests[1][0]
    assert enroll_request.full_url == "https://center.example/base/api/training/nodes/enroll"
    assert enroll_request.get_header("Authorization") is None
    assert json.loads(enroll_request.data)["enrollment_token"].startswith("enroll_")
    assert heartbeat_request.full_url.endswith("/api/training/nodes/node_123/heartbeat")
    assert heartbeat_request.get_header("Authorization") == "Bearer worker_" + "z" * 48
    heartbeat_body = json.loads(heartbeat_request.data)
    assert heartbeat_body["resources"]["cpu"] == {"logical_cores": 8, "load_1m": 1.25}
    assert heartbeat_body["resources"]["gpus"][0]["temperature_celsius"] == 40.0
    assert "worker_token" not in heartbeat_body
    assert [timeout for _, timeout in opener.requests] == [4, 4]

    # Keep the independent worker package pinned to the control-plane wire
    # models without importing those models in production worker code.
    from vla_data_juicer_agents.training.api import (
        EnrollTrainingNodeRequest,
        TrainingNodeHeartbeatRequest,
    )

    EnrollTrainingNodeRequest.model_validate(json.loads(enroll_request.data))
    TrainingNodeHeartbeatRequest.model_validate(heartbeat_body)


def test_http_center_client_rejects_redirect_without_leaking_token(tmp_path: Path) -> None:
    identity = load_or_create_identity(tmp_path / "state")
    secret = "worker_" + "s" * 48
    client = HttpCenterClient(
        center_base_url="https://center.example",
        worker_token=secret,
        node_ref="node_123",
    )

    class RedirectingOpener:
        def open(self, request: object, timeout: float) -> object:
            raise HTTPError(request.full_url, 302, "Found", {}, None)

    client._opener = RedirectingOpener()  # type: ignore[assignment]

    with pytest.raises(CenterClientError) as captured:
        client.publish_heartbeat(
            identity,
            {
                "health": {"status": "healthy"},
                "resources": {
                    "host": {"hostname": "h", "os": "Linux", "os_release": "6", "architecture": "x86"},
                    "cpu": {"logical_cores": 1, "load_1m": None},
                    "memory": {"total_bytes": 1, "available_bytes": 0},
                    "disks": [],
                    "gpus": [],
                    "gpu_collection": {"source": "unavailable", "error": None},
                },
            },
        )
    assert secret not in str(captured.value)
    assert "HTTP 302" in str(captured.value)

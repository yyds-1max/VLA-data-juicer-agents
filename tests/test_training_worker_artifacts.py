from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import vla_data_juicer_agents.training_worker.artifacts as artifact_module
from vla_data_juicer_agents.training_worker.artifacts import (
    ArtifactInspectionError,
    inspect_training_artifact,
)
from vla_data_juicer_agents.training_worker.client import OfflineCenterClient
from vla_data_juicer_agents.training_worker.daemon import TrainingWorkerDaemon
from vla_data_juicer_agents.training_worker.identity import load_or_create_identity
from vla_data_juicer_agents.training_worker.ledger import WorkerLedger
from vla_data_juicer_agents.training_worker.resources import (
    CommandResult,
    ResourceCollector,
)


def _managed_artifact(tmp_path: Path) -> tuple[dict[str, object], Path]:
    output_root = tmp_path / "outputs"
    version_root = output_root / "family_1" / "v3-20260821"
    artifact = version_root / "stage-02"
    nested = artifact / "checkpoint-2"
    nested.mkdir(parents=True)
    (artifact / "config.json").write_bytes(b"{}")
    (nested / "model.safetensors").write_bytes(b"weights")
    (version_root / ".datapilot-training-version.json").write_text(
        json.dumps(
            {
                "contract": "datapilot_training_version_v1",
                "run_ref": "run_1",
                "family_ref": "family_1",
                "version_label": "v3-20260821",
            }
        ),
        encoding="utf-8",
    )
    return (
        {
            "artifact_ref": "artifact_1",
            "run_ref": "run_1",
            "version_ref": "version_1",
            "version_label": "v3-20260821",
            "output_root": str(output_root),
            "artifact_path": str(artifact),
        },
        artifact,
    )


def test_artifact_inspection_counts_regular_files_without_reading_them(
    tmp_path: Path,
) -> None:
    payload, _artifact = _managed_artifact(tmp_path)

    result = inspect_training_artifact(payload)

    assert result == {
        "status": "succeeded",
        "artifact_ref": "artifact_1",
        "version_ref": "version_1",
        "availability": "available",
        "file_count": 2,
        "total_bytes": 9,
    }


@pytest.mark.parametrize(
    ("mutation", "availability"),
    [
        ("missing", "missing"),
        ("marker_mismatch", "unsafe"),
        ("internal_symlink", "unsafe"),
    ],
)
def test_artifact_inspection_returns_safe_availability_states(
    tmp_path: Path, mutation: str, availability: str
) -> None:
    payload, artifact = _managed_artifact(tmp_path)
    if mutation == "missing":
        payload["artifact_path"] = str(artifact.parent / "stage-03")
    elif mutation == "marker_mismatch":
        marker = artifact.parent / ".datapilot-training-version.json"
        marker.write_text(
            json.dumps(
                {
                    "contract": "datapilot_training_version_v1",
                    "run_ref": "run_other",
                    "version_label": "v3-20260821",
                }
            ),
            encoding="utf-8",
        )
    else:
        (artifact / "unsafe-link").symlink_to(tmp_path / "outside")

    result = inspect_training_artifact(payload)

    assert result["status"] == "succeeded"
    assert result["availability"] == availability
    assert result["file_count"] is None
    assert result["total_bytes"] is None


def test_artifact_inspection_rejects_symlink_path_component(tmp_path: Path) -> None:
    payload, artifact = _managed_artifact(tmp_path)
    alternate = tmp_path / "alternate"
    alternate.mkdir()
    symlink_root = tmp_path / "output-link"
    symlink_root.symlink_to(alternate, target_is_directory=True)
    payload["output_root"] = str(symlink_root)
    payload["artifact_path"] = str(
        symlink_root / "family_1" / "v3-20260821" / "stage-02"
    )

    result = inspect_training_artifact(payload)

    assert result["availability"] == "unsafe"
    assert artifact.exists()


def test_artifact_inspection_reports_unreadable_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload, artifact = _managed_artifact(tmp_path)
    original_access = os.access

    def fake_access(path: object, mode: int) -> bool:
        if Path(path) == artifact:
            return False
        return original_access(path, mode)

    monkeypatch.setattr(artifact_module.os, "access", fake_access)

    result = inspect_training_artifact(payload)

    assert result["status"] == "succeeded"
    assert result["availability"] == "unreadable"


def test_artifact_inspection_rejects_invalid_request(tmp_path: Path) -> None:
    payload, _artifact = _managed_artifact(tmp_path)
    payload["artifact_path"] = "relative/stage-01"

    with pytest.raises(
        ArtifactInspectionError, match="safe absolute path"
    ) as captured:
        inspect_training_artifact(payload)

    assert captured.value.code == "artifact_inspection_request_invalid"


def test_daemon_publishes_artifact_inspection_result_with_claim_token(
    tmp_path: Path,
) -> None:
    payload, _artifact = _managed_artifact(tmp_path)

    class RecordingCenter(OfflineCenterClient):
        result: tuple[str, dict[str, object]] | None = None

        def publish_command_result(self, identity, command_ref, result):  # type: ignore[no-untyped-def]
            self.result = (command_ref, dict(result))
            return {"accepted": True}

    center = RecordingCenter()
    state_dir = tmp_path / "state"
    daemon = TrainingWorkerDaemon(
        identity=load_or_create_identity(state_dir),
        ledger=WorkerLedger(state_dir / "worker-ledger.sqlite"),
        resource_collector=ResourceCollector(
            disk_paths=[tmp_path],
            gpu_command_runner=lambda _command, _timeout: CommandResult(
                1, "", "unavailable"
            ),
        ),
        center_client=center,
    )

    daemon._handle_command(
        {
            "command_ref": "command_1",
            "claim_token": "claim_" + "a" * 40,
            "kind": "inspect_training_artifact",
            "payload": payload,
        }
    )

    assert center.result is not None
    assert center.result[0] == "command_1"
    assert center.result[1]["claim_token"] == "claim_" + "a" * 40
    assert center.result[1]["status"] == "succeeded"
    assert center.result[1]["availability"] == "available"


from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import threading
import time

import pytest

from vla_data_juicer_agents.training_worker.client import HttpCenterClient
from vla_data_juicer_agents.training_worker.daemon import TrainingWorkerDaemon
from vla_data_juicer_agents.training_worker.datasets import (
    DATASET_MARKER_CONTRACT,
    DATASET_MARKER_NAME,
    DatasetCommandError,
    DatasetFileChunk,
    DatasetTransferManager,
    list_directories,
    load_inventory,
    remove_dataset_replica,
)


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


class _MemoryDatasetSource:
    def __init__(
        self,
        files: dict[str, tuple[str, bytes]],
        *,
        dataset_date: str = "20260806",
        release_ref: str = "release_12345678",
    ) -> None:
        self.files = files
        self.dataset_date = dataset_date
        self.release_ref = release_ref
        self.requests: list[tuple[str, int, int]] = []

    def fetch_dataset_manifest_page(self, release_ref, *, cursor, limit):  # type: ignore[no-untyped-def]
        assert release_ref == self.release_ref
        assert cursor is None
        manifest_files = [
            {
                "file_ref": file_ref,
                "relative_path": relative_path,
                "size_bytes": len(content),
                "sha256": _sha256(content),
            }
            for file_ref, (relative_path, content) in self.files.items()
        ]
        return {
            "release_ref": release_ref,
            "dataset_date": self.dataset_date,
            "inventory_sha256": "a" * 64,
            "total_bytes": sum(len(content) for _, content in self.files.values()),
            "files": manifest_files,
            "next_cursor": None,
        }

    def fetch_dataset_file_chunk(self, file_ref, *, offset, max_bytes):  # type: ignore[no-untyped-def]
        self.requests.append((file_ref, offset, max_bytes))
        content = self.files[file_ref][1]
        data = content[offset : offset + max_bytes]
        return DatasetFileChunk(
            data=data,
            offset=offset,
            total_size=len(content),
            complete=offset + len(data) >= len(content),
        )


def test_inventory_follows_nonnegative_integer_cursors() -> None:
    class PagedSource:
        cursors: list[int | None] = []

        def fetch_dataset_manifest_page(self, release_ref, *, cursor, limit):  # type: ignore[no-untyped-def]
            self.cursors.append(cursor)
            files = (
                [
                    {
                        "file_ref": "file_1",
                        "relative_path": "first.bin",
                        "size_bytes": 1,
                        "sha256": _sha256(b"a"),
                    }
                ]
                if cursor is None
                else [
                    {
                        "file_ref": "file_2",
                        "relative_path": "second.bin",
                        "size_bytes": 1,
                        "sha256": _sha256(b"b"),
                    }
                ]
            )
            return {
                "release_ref": release_ref,
                "dataset_date": "20260806",
                "inventory_sha256": "a" * 64,
                "total_bytes": 2,
                "files": files,
                "next_cursor": 1 if cursor is None else None,
            }

        def fetch_dataset_file_chunk(self, file_ref, *, offset, max_bytes):  # type: ignore[no-untyped-def]
            raise AssertionError("not used")

    source = PagedSource()

    inventory = load_inventory(source, "release_1")

    assert source.cursors == [None, 1]
    assert [item.relative_path for item in inventory.files] == [
        "first.bin",
        "second.bin",
    ]


def test_directory_browser_only_returns_enterable_directories_and_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    visible = tmp_path / "visible"
    visible.mkdir()
    (tmp_path / "ordinary.txt").write_text("not a directory", encoding="utf-8")
    (tmp_path / "directory-link").symlink_to(visible, target_is_directory=True)
    hidden = tmp_path / "hidden"
    hidden.mkdir()
    real_access = os.access

    def access(path: object, mode: int) -> bool:
        if Path(path) == hidden:
            return False
        return real_access(path, mode)

    monkeypatch.setattr(os, "access", access)

    result = list_directories(str(tmp_path))

    assert result["path"] == str(tmp_path.resolve())
    assert result["parent_path"] == str(tmp_path.resolve().parent)
    assert result["writable"] is True
    assert isinstance(result["free_bytes"], int)
    assert result["directories"] == [
        {"name": "visible", "path": str(visible), "writable": True}
    ]


def test_transfer_resumes_partial_file_checksums_and_atomically_publishes_marker(
    tmp_path: Path,
) -> None:
    content = b"abcdefghij"
    empty = b""
    source = _MemoryDatasetSource(
        {
            "file_main": ("nested/sample.bin", content),
            "file_empty": ("empty.jsonl", empty),
        }
    )
    destination = tmp_path / "data"
    destination.mkdir()
    temporary_root = (
        destination
        / "datapilot-managed"
        / ".20260806-12345678.part"
    )
    partial = temporary_root / "nested" / "sample.bin"
    partial.parent.mkdir(parents=True)
    partial.write_bytes(content[:4])
    results: list[tuple[str, dict[str, object]]] = []
    manager = DatasetTransferManager(
        source_client=source,
        publish_result=lambda command_ref, payload: results.append(
            (command_ref, dict(payload))
        ),
    )

    manager.start(
        "command_transfer",
        {
            "transfer_ref": "transfer_1",
            "release_ref": source.release_ref,
            "dataset_date": source.dataset_date,
            "destination_parent": str(destination),
        },
        claim_token="claim_" + "a" * 40,
    )

    assert manager.wait(5)
    final_root = destination / "datapilot-managed" / "20260806-12345678"
    assert not temporary_root.exists()
    assert (final_root / "nested" / "sample.bin").read_bytes() == content
    assert (final_root / "empty.jsonl").read_bytes() == empty
    marker = json.loads((final_root / DATASET_MARKER_NAME).read_text(encoding="utf-8"))
    assert marker["contract"] == DATASET_MARKER_CONTRACT
    assert marker["release_ref"] == source.release_ref
    assert source.requests[0][1] == 4
    assert results[-1][0] == "command_transfer"
    assert results[-1][1]["status"] == "succeeded"
    assert results[-1][1]["claim_token"] == "claim_" + "a" * 40
    assert results[-1][1]["replica"]["local_root"] == str(final_root)  # type: ignore[index]


def test_existing_managed_replica_is_verified_and_reused_without_file_download(
    tmp_path: Path,
) -> None:
    content = b"already-present"
    source = _MemoryDatasetSource({"file_1": ("sample.bin", content)})
    destination = tmp_path / "data"
    final_root = destination / "datapilot-managed" / "20260806-12345678"
    final_root.mkdir(parents=True)
    (final_root / "sample.bin").write_bytes(content)
    (final_root / DATASET_MARKER_NAME).write_text(
        json.dumps(
            {
                "contract": DATASET_MARKER_CONTRACT,
                "transfer_ref": "transfer_original",
                "release_ref": source.release_ref,
                "dataset_date": source.dataset_date,
                "inventory_sha256": "a" * 64,
                "total_bytes": len(content),
                "file_count": 1,
            }
        ),
        encoding="utf-8",
    )
    results: list[dict[str, object]] = []
    manager = DatasetTransferManager(
        source_client=source,
        publish_result=lambda _ref, payload: results.append(dict(payload)),
    )

    manager.start(
        "command_recovery",
        {
            "transfer_ref": "transfer_recovery",
            "release_ref": source.release_ref,
            "dataset_date": source.dataset_date,
            "destination_parent": str(destination),
            "recovery_replica_ref": "replica_original",
        },
        claim_token="claim_" + "r" * 40,
    )

    assert manager.wait(5)
    assert source.requests == []
    assert results[-1]["status"] == "succeeded"
    assert results[-1]["replica"]["local_root"] == str(final_root)  # type: ignore[index]


def test_existing_managed_replica_with_internal_symlink_is_never_recovered(
    tmp_path: Path,
) -> None:
    source = _MemoryDatasetSource({"file_1": ("sample.bin", b"data")})
    destination = tmp_path / "data"
    final_root = destination / "datapilot-managed" / "20260806-12345678"
    final_root.mkdir(parents=True)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"secret")
    (final_root / "sample.bin").symlink_to(outside)
    (final_root / DATASET_MARKER_NAME).write_text(
        json.dumps(
            {
                "contract": DATASET_MARKER_CONTRACT,
                "release_ref": source.release_ref,
                "dataset_date": source.dataset_date,
                "inventory_sha256": "a" * 64,
                "total_bytes": 4,
                "file_count": 1,
            }
        ),
        encoding="utf-8",
    )
    results: list[dict[str, object]] = []
    manager = DatasetTransferManager(
        source_client=source,
        publish_result=lambda _ref, payload: results.append(dict(payload)),
    )

    manager.start(
        "command_recovery",
        {
            "transfer_ref": "transfer_recovery",
            "release_ref": source.release_ref,
            "dataset_date": source.dataset_date,
            "destination_parent": str(destination),
        },
    )

    assert manager.wait(5)
    assert results[-1]["status"] == "failed"
    assert results[-1]["error"]["code"] == "dataset_destination_unsafe"  # type: ignore[index]
    assert outside.read_bytes() == b"secret"


def test_checksum_failure_keeps_partial_data_but_never_publishes_replica(
    tmp_path: Path,
) -> None:
    source = _MemoryDatasetSource({"file_1": ("sample.bin", b"correct")})
    original_manifest = source.fetch_dataset_manifest_page

    def bad_manifest(release_ref, *, cursor, limit):  # type: ignore[no-untyped-def]
        manifest = original_manifest(release_ref, cursor=cursor, limit=limit)
        manifest["files"][0]["sha256"] = "b" * 64  # type: ignore[index]
        return manifest

    source.fetch_dataset_manifest_page = bad_manifest  # type: ignore[method-assign]
    destination = tmp_path / "data"
    destination.mkdir()
    results: list[dict[str, object]] = []
    manager = DatasetTransferManager(
        source_client=source,
        publish_result=lambda _ref, payload: results.append(dict(payload)),
    )

    manager.start(
        "command_transfer",
        {
            "transfer_ref": "transfer_1",
            "release_ref": source.release_ref,
            "dataset_date": source.dataset_date,
            "destination_parent": str(destination),
        },
    )

    assert manager.wait(5)
    assert results[-1]["status"] == "failed"
    assert results[-1]["error"]["code"] == "dataset_file_checksum_mismatch"  # type: ignore[index]
    assert not (destination / "datapilot-managed" / "20260806-12345678").exists()
    assert (destination / "datapilot-managed" / ".20260806-12345678.part").is_dir()

    source.fetch_dataset_manifest_page = original_manifest  # type: ignore[method-assign]
    manager.start(
        "command_retry",
        {
            "transfer_ref": "transfer_1",
            "release_ref": source.release_ref,
            "dataset_date": source.dataset_date,
            "destination_parent": str(destination),
        },
    )
    assert manager.wait(5)
    assert results[-1]["status"] == "succeeded"
    assert (destination / "datapilot-managed" / "20260806-12345678").is_dir()


def test_transfer_can_be_paused_while_manifest_is_being_loaded(
    tmp_path: Path,
) -> None:
    source = _MemoryDatasetSource({"file_1": ("sample.bin", b"content")})
    entered = threading.Event()
    continue_manifest = threading.Event()
    original_manifest = source.fetch_dataset_manifest_page

    def blocking_manifest(release_ref, *, cursor, limit):  # type: ignore[no-untyped-def]
        entered.set()
        assert continue_manifest.wait(5)
        return original_manifest(release_ref, cursor=cursor, limit=limit)

    source.fetch_dataset_manifest_page = blocking_manifest  # type: ignore[method-assign]
    destination = tmp_path / "data"
    destination.mkdir()
    results: list[dict[str, object]] = []
    manager = DatasetTransferManager(
        source_client=source,
        publish_result=lambda _ref, payload: results.append(dict(payload)),
    )
    manager.start(
        "command_transfer",
        {
            "transfer_ref": "transfer_cancel",
            "release_ref": source.release_ref,
            "dataset_date": source.dataset_date,
            "destination_parent": str(destination),
        },
    )
    assert entered.wait(5)

    manager.cancel("transfer_cancel")
    continue_manifest.set()

    assert manager.wait(5)
    assert results[-1] == {
        "status": "paused",
        "transfer_ref": "transfer_cancel",
    }
    assert manager.active_transfer_ref is None


def test_cancelling_transfer_removes_hidden_partial_data(tmp_path: Path) -> None:
    source = _MemoryDatasetSource({"file_1": ("sample.bin", b"content")})
    entered = threading.Event()
    continue_manifest = threading.Event()
    original_manifest = source.fetch_dataset_manifest_page

    def blocking_manifest(release_ref, *, cursor, limit):  # type: ignore[no-untyped-def]
        entered.set()
        assert continue_manifest.wait(5)
        return original_manifest(release_ref, cursor=cursor, limit=limit)

    source.fetch_dataset_manifest_page = blocking_manifest  # type: ignore[method-assign]
    destination = tmp_path / "data"
    destination.mkdir()
    temporary_root = destination / "datapilot-managed" / ".20260806-12345678.part"
    temporary_root.mkdir(parents=True)
    (temporary_root / "partial.bin").write_bytes(b"partial")
    results: list[dict[str, object]] = []
    manager = DatasetTransferManager(
        source_client=source,
        publish_result=lambda _ref, payload: results.append(dict(payload)),
    )
    manager.start(
        "command_transfer",
        {
            "transfer_ref": "transfer_cancel",
            "release_ref": source.release_ref,
            "dataset_date": source.dataset_date,
            "destination_parent": str(destination),
        },
    )
    assert entered.wait(5)

    manager.cancel("transfer_cancel", discard_partial=True)
    continue_manifest.set()

    assert manager.wait(5)
    assert results[-1] == {"status": "cancelled", "transfer_ref": "transfer_cancel"}
    assert not temporary_root.exists()


def test_transfer_source_errors_do_not_leak_paths_or_credentials(tmp_path: Path) -> None:
    secret = "worker_super_secret"
    private_path = "/private/training/source"

    class FailingSource(_MemoryDatasetSource):
        def fetch_dataset_manifest_page(self, release_ref, *, cursor, limit):  # type: ignore[no-untyped-def]
            raise RuntimeError(f"{secret} at {private_path}")

    destination = tmp_path / "data"
    destination.mkdir()
    results: list[dict[str, object]] = []
    source = FailingSource({})
    manager = DatasetTransferManager(
        source_client=source,
        publish_result=lambda _ref, payload: results.append(dict(payload)),
    )

    manager.start(
        "command_transfer",
        {
            "transfer_ref": "transfer_1",
            "release_ref": source.release_ref,
            "dataset_date": source.dataset_date,
            "destination_parent": str(destination),
        },
    )

    assert manager.wait(5)
    serialized = json.dumps(results[-1])
    assert results[-1]["status"] == "failed"
    assert secret not in serialized
    assert private_path not in serialized


def test_remove_replica_requires_exact_platform_marker(tmp_path: Path) -> None:
    replica = tmp_path / "datapilot-managed" / "20260806-12345678"
    replica.mkdir(parents=True)
    (replica / "data.bin").write_bytes(b"data")
    marker = {
        "contract": DATASET_MARKER_CONTRACT,
        "release_ref": "release_12345678",
        "dataset_date": "20260806",
        "inventory_sha256": "a" * 64,
    }
    (replica / DATASET_MARKER_NAME).write_text(json.dumps(marker), encoding="utf-8")

    with pytest.raises(DatasetCommandError, match="does not match"):
        remove_dataset_replica(
            {
                "replica_ref": "replica_1",
                "local_root": str(replica),
                "release_ref": "release_wrong",
                "inventory_sha256": "a" * 64,
            }
        )
    assert replica.exists()

    result = remove_dataset_replica(
        {
            "replica_ref": "replica_1",
            "local_root": str(replica),
            "release_ref": "release_12345678",
            "inventory_sha256": "a" * 64,
        }
    )

    assert result == {"status": "succeeded", "replica_ref": "replica_1"}
    assert not replica.exists()


def test_remove_replica_never_deletes_unmarked_directory(tmp_path: Path) -> None:
    ordinary = tmp_path / "datapilot-managed" / "20260806-12345678"
    ordinary.mkdir(parents=True)
    (ordinary / "important.txt").write_text("keep", encoding="utf-8")

    with pytest.raises(DatasetCommandError, match="marker"):
        remove_dataset_replica(
            {
                "replica_ref": "replica_1",
                "local_root": str(ordinary),
                "release_ref": "release_12345678",
                "inventory_sha256": "a" * 64,
            }
        )

    assert (ordinary / "important.txt").read_text(encoding="utf-8") == "keep"


class _RangeResponse:
    status = 206
    headers = {"Content-Range": "bytes 4-6/7"}

    def __enter__(self):  # type: ignore[no-untyped-def]
        return self

    def __exit__(self, *args):  # type: ignore[no-untyped-def]
        return None

    def getcode(self) -> int:
        return self.status

    def read(self, limit: int) -> bytes:
        return b"efg"


def test_http_client_downloads_only_fixed_origin_range() -> None:
    client = HttpCenterClient(
        center_base_url="https://center.example/base",
        worker_token="worker_" + "a" * 48,
        node_ref="node_1",
    )

    class Opener:
        request = None

        def open(self, request, timeout):  # type: ignore[no-untyped-def]
            self.request = request
            return _RangeResponse()

    opener = Opener()
    client._opener = opener  # type: ignore[assignment]

    chunk = client.fetch_dataset_file_chunk("file_1", offset=4, max_bytes=3)

    assert chunk == DatasetFileChunk(b"efg", 4, 7, True)
    assert opener.request.full_url == (  # type: ignore[union-attr]
        "https://center.example/base/api/training/nodes/node_1/"
        "dataset-files/file_1/content"
    )
    assert opener.request.get_header("Range") == "bytes=4-6"  # type: ignore[union-attr]
    assert opener.request.get_header("Authorization") == "Bearer worker_" + "a" * 48  # type: ignore[union-attr]


def test_worker_dataset_results_match_control_plane_wire_models(tmp_path: Path) -> None:
    from vla_data_juicer_agents.training.api import WorkerCommandResultRequest

    listing = list_directories(str(tmp_path))
    WorkerCommandResultRequest.model_validate(
        {
            "worker_instance_id": "worker-1",
            "claim_token": "claim_" + "a" * 40,
            "status": "succeeded",
            **listing,
        }
    )
    WorkerCommandResultRequest.model_validate(
        {
            "worker_instance_id": "worker-1",
            "claim_token": "claim_" + "b" * 40,
            "status": "running",
            "transfer_ref": "transfer_1",
            "progress": {
                "bytes_transferred": 4,
                "total_bytes": 10,
                "files_completed": 0,
                "total_files": 1,
            },
        }
    )
    WorkerCommandResultRequest.model_validate(
        {
            "worker_instance_id": "worker-1",
            "claim_token": "claim_" + "c" * 40,
            "status": "succeeded",
            "replica_ref": "replica_1",
        }
    )


def test_long_polling_thread_does_not_block_heartbeat(tmp_path: Path) -> None:
    from vla_data_juicer_agents.training_worker.client import OfflineCenterClient
    from vla_data_juicer_agents.training_worker.identity import load_or_create_identity
    from vla_data_juicer_agents.training_worker.ledger import WorkerLedger
    from vla_data_juicer_agents.training_worker.resources import CommandResult, ResourceCollector

    poll_entered = threading.Event()

    class BlockingCenter(OfflineCenterClient):
        heartbeat_count = 0

        def publish_heartbeat(self, identity, payload):  # type: ignore[no-untyped-def]
            self.heartbeat_count += 1
            return {}

        def poll_commands(self, identity, *, wait_seconds=25, limit=1):  # type: ignore[no-untyped-def]
            poll_entered.set()
            time.sleep(0.1)
            return {"commands": []}

    center = BlockingCenter()
    daemon = TrainingWorkerDaemon(
        identity=load_or_create_identity(tmp_path / "state"),
        ledger=WorkerLedger(tmp_path / "state" / "ledger.sqlite"),
        resource_collector=ResourceCollector(
            disk_paths=[tmp_path],
            gpu_command_runner=lambda _command, _timeout: CommandResult(1, "", "none"),
        ),
        center_client=center,
        interval_seconds=0.01,
    )
    daemon_thread = threading.Thread(target=daemon.run_forever)
    daemon_thread.start()
    assert poll_entered.wait(1)
    time.sleep(0.05)

    daemon.stop()
    daemon_thread.join(1)

    assert not daemon_thread.is_alive()
    assert center.heartbeat_count >= 2

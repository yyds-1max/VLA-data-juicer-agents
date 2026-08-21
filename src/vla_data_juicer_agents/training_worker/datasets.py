from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import threading
import time
from typing import Callable, Iterable, Mapping, Protocol


DATASET_MARKER_NAME = ".datapilot-dataset.json"
DATASET_MARKER_CONTRACT = "datapilot_dataset_replica_v1"
DOWNLOAD_CHUNK_BYTES = 4 * 1024 * 1024
_SAFE_REF = re.compile(r"[A-Za-z0-9_.:-]{1,255}\Z")
_DATASET_DATE = re.compile(r"[0-9]{8}\Z")


class DatasetCommandError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class DatasetTransferCancelled(DatasetCommandError):
    def __init__(self) -> None:
        super().__init__("dataset_transfer_cancelled", "Dataset transfer was cancelled.")


@dataclass(frozen=True, slots=True)
class DatasetFile:
    file_ref: str
    relative_path: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class DatasetInventory:
    release_ref: str
    dataset_date: str
    inventory_sha256: str
    total_bytes: int
    files: tuple[DatasetFile, ...]


@dataclass(frozen=True, slots=True)
class DatasetFileChunk:
    data: bytes
    offset: int
    total_size: int
    complete: bool


class DatasetSourceClient(Protocol):
    def fetch_dataset_manifest_page(
        self,
        release_ref: str,
        *,
        cursor: int | None,
        limit: int,
    ) -> Mapping[str, object]: ...

    def fetch_dataset_file_chunk(
        self,
        file_ref: str,
        *,
        offset: int,
        max_bytes: int,
    ) -> DatasetFileChunk: ...


def list_directories(raw_path: object) -> dict[str, object]:
    path = _existing_directory(raw_path)
    directories: list[dict[str, object]] = []
    try:
        entries: Iterable[os.DirEntry[str]] = os.scandir(path)
        with entries as scan:
            for entry in scan:
                try:
                    if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                        continue
                    child = Path(entry.path)
                    if not os.access(child, os.R_OK | os.X_OK):
                        continue
                    directories.append(
                        {
                            "name": entry.name,
                            "path": str(child),
                            "writable": os.access(child, os.W_OK | os.X_OK),
                        }
                    )
                except OSError:
                    continue
    except OSError as exc:
        raise DatasetCommandError(
            "directory_not_readable", "The Worker cannot read this directory."
        ) from exc
    directories.sort(key=lambda item: str(item["name"]).casefold())
    try:
        free_bytes = shutil.disk_usage(path).free
    except OSError as exc:
        raise DatasetCommandError(
            "directory_storage_unavailable",
            "The Worker cannot inspect storage for this directory.",
        ) from exc
    parent = None if path.parent == path else str(path.parent)
    return {
        "path": str(path),
        "parent_path": parent,
        "writable": os.access(path, os.W_OK | os.X_OK),
        "free_bytes": free_bytes,
        "directories": directories,
    }


def load_inventory(client: DatasetSourceClient, release_ref: str) -> DatasetInventory:
    _validated_ref(release_ref, "release_ref")
    cursor: int | None = None
    seen_cursors: set[int] = set()
    files: list[DatasetFile] = []
    release_date: str | None = None
    inventory_sha256: str | None = None
    total_bytes: int | None = None
    for _page_number in range(100_000):
        page = client.fetch_dataset_manifest_page(
            release_ref, cursor=cursor, limit=500
        )
        page_release = page.get("release_ref")
        page_date = page.get("dataset_date")
        page_digest = page.get("inventory_sha256")
        page_total = page.get("total_bytes")
        page_files = page.get("files")
        if page_release != release_ref:
            raise DatasetCommandError(
                "dataset_manifest_invalid", "Dataset manifest release does not match."
            )
        if not isinstance(page_date, str) or not _DATASET_DATE.fullmatch(page_date):
            raise DatasetCommandError(
                "dataset_manifest_invalid", "Dataset manifest date is invalid."
            )
        if not isinstance(page_digest, str) or not _is_sha256(page_digest):
            raise DatasetCommandError(
                "dataset_manifest_invalid", "Dataset manifest digest is invalid."
            )
        if not isinstance(page_total, int) or isinstance(page_total, bool) or page_total < 0:
            raise DatasetCommandError(
                "dataset_manifest_invalid", "Dataset manifest size is invalid."
            )
        if not isinstance(page_files, list):
            raise DatasetCommandError(
                "dataset_manifest_invalid", "Dataset manifest files are invalid."
            )
        if release_date is None:
            release_date = page_date
            inventory_sha256 = page_digest
            total_bytes = page_total
        elif (
            page_date != release_date
            or page_digest != inventory_sha256
            or page_total != total_bytes
        ):
            raise DatasetCommandError(
                "dataset_manifest_changed", "Dataset manifest changed while downloading."
            )
        files.extend(_parse_manifest_file(item) for item in page_files)
        next_cursor = page.get("next_cursor")
        if next_cursor is None:
            break
        if (
            not isinstance(next_cursor, int)
            or isinstance(next_cursor, bool)
            or next_cursor < 0
            or next_cursor in seen_cursors
        ):
            raise DatasetCommandError(
                "dataset_manifest_invalid", "Dataset manifest cursor is invalid."
            )
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    else:
        raise DatasetCommandError(
            "dataset_manifest_too_large", "Dataset manifest has too many pages."
        )
    assert release_date is not None
    assert inventory_sha256 is not None
    assert total_bytes is not None
    relative_paths = [item.relative_path for item in files]
    if len(relative_paths) != len(set(relative_paths)):
        raise DatasetCommandError(
            "dataset_manifest_invalid", "Dataset manifest contains duplicate paths."
        )
    if sum(item.size_bytes for item in files) != total_bytes:
        raise DatasetCommandError(
            "dataset_manifest_invalid", "Dataset manifest total size does not match."
        )
    return DatasetInventory(
        release_ref=release_ref,
        dataset_date=release_date,
        inventory_sha256=inventory_sha256,
        total_bytes=total_bytes,
        files=tuple(files),
    )


class DatasetTransferManager:
    def __init__(
        self,
        *,
        source_client: DatasetSourceClient,
        publish_result: Callable[[str, Mapping[str, object]], None],
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.source_client = source_client
        self.publish_result = publish_result
        self._clock = monotonic_clock
        self._lock = threading.Lock()
        self._active_transfer_ref: str | None = None
        self._cancel_event: threading.Event | None = None
        self._discard_partial = False
        self._thread: threading.Thread | None = None

    @property
    def active_transfer_ref(self) -> str | None:
        with self._lock:
            return self._active_transfer_ref

    def start(
        self,
        command_ref: str,
        payload: Mapping[str, object],
        *,
        claim_token: str | None = None,
    ) -> None:
        transfer_ref, release_ref, dataset_date, destination_parent = (
            _parse_transfer_payload(payload)
        )
        with self._lock:
            if self._active_transfer_ref is not None:
                raise DatasetCommandError(
                    "dataset_transfer_busy",
                    "This Worker is already transferring another dataset.",
                )
            cancel_event = threading.Event()
            self._active_transfer_ref = transfer_ref
            self._cancel_event = cancel_event
            self._discard_partial = False
            self._thread = threading.Thread(
                target=self._run_transfer,
                args=(
                    command_ref,
                    transfer_ref,
                    release_ref,
                    dataset_date,
                    destination_parent,
                    cancel_event,
                    claim_token,
                ),
                name="datapilot-dataset-transfer",
                daemon=True,
            )
            self._thread.start()

    def cancel(self, transfer_ref: object, *, discard_partial: bool = False) -> None:
        transfer_ref = _validated_ref(transfer_ref, "transfer_ref")
        with self._lock:
            if (
                self._active_transfer_ref != transfer_ref
                or self._cancel_event is None
            ):
                raise DatasetCommandError(
                    "dataset_transfer_not_active", "This dataset transfer is not active."
                )
            self._discard_partial = discard_partial
            self._cancel_event.set()

    def wait(self, timeout: float | None = None) -> bool:
        with self._lock:
            thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def _run_transfer(
        self,
        command_ref: str,
        transfer_ref: str,
        release_ref: str,
        dataset_date: str,
        destination_parent: Path,
        cancel_event: threading.Event,
        claim_token: str | None,
    ) -> None:
        temporary_root: Path | None = None

        def publish(payload: Mapping[str, object]) -> None:
            result = dict(payload)
            if claim_token is not None:
                result["claim_token"] = claim_token
            self.publish_result(command_ref, result)

        try:
            inventory = load_inventory(self.source_client, release_ref)
            if inventory.dataset_date != dataset_date:
                raise DatasetCommandError(
                    "dataset_date_mismatch",
                    "The selected dataset date does not match its release.",
                )
            local_root = _final_replica_root(
                destination_parent, dataset_date, release_ref
            )
            existing = _matching_replica(local_root, inventory)
            if existing:
                publish(_transfer_success_payload(transfer_ref, inventory, local_root))
                return
            temporary_root = local_root.parent / f".{local_root.name}.part"
            _prepare_temporary_root(temporary_root, local_root)
            completed_bytes = 0
            completed_files = 0
            for item in inventory.files:
                _raise_if_cancelled(cancel_event)
                target = _safe_file_target(temporary_root, item.relative_path)
                if target.exists() and target.is_symlink():
                    raise DatasetCommandError(
                        "dataset_target_unsafe", "Dataset target contains a symlink."
                    )
                existing_size = target.stat().st_size if target.exists() else 0
                if existing_size > item.size_bytes:
                    target.unlink()
                    existing_size = 0
                if (
                    target.exists()
                    and existing_size == item.size_bytes
                    and _file_sha256(target) != item.sha256
                ):
                    target.unlink()
                    existing_size = 0
                completed_bytes += existing_size
                last_report = self._clock()
                publish(
                    _transfer_progress_payload(
                        transfer_ref,
                        completed_bytes,
                        inventory.total_bytes,
                        completed_files,
                        len(inventory.files),
                    ),
                )
                while existing_size < item.size_bytes:
                    _raise_if_cancelled(cancel_event)
                    chunk = self.source_client.fetch_dataset_file_chunk(
                        item.file_ref,
                        offset=existing_size,
                        max_bytes=min(DOWNLOAD_CHUNK_BYTES, item.size_bytes - existing_size),
                    )
                    if chunk.offset != existing_size or chunk.total_size != item.size_bytes:
                        raise DatasetCommandError(
                            "dataset_file_range_invalid",
                            "Center returned an invalid dataset file range.",
                        )
                    if not chunk.data:
                        raise DatasetCommandError(
                            "dataset_file_incomplete",
                            "Center returned an incomplete dataset file.",
                        )
                    if len(chunk.data) > item.size_bytes - existing_size:
                        raise DatasetCommandError(
                            "dataset_file_range_invalid",
                            "Center returned too much dataset file data.",
                        )
                    _ensure_safe_parent(temporary_root, target.parent)
                    with target.open("ab") as stream:
                        stream.write(chunk.data)
                        stream.flush()
                    existing_size += len(chunk.data)
                    completed_bytes += len(chunk.data)
                    now = self._clock()
                    if now - last_report >= 1.0 or existing_size == item.size_bytes:
                        publish(
                            _transfer_progress_payload(
                                transfer_ref,
                                completed_bytes,
                                inventory.total_bytes,
                                completed_files,
                                len(inventory.files),
                            ),
                        )
                        last_report = now
                if item.size_bytes == 0 and not target.exists():
                    _ensure_safe_parent(temporary_root, target.parent)
                    target.touch(mode=0o640)
                if _file_sha256(target) != item.sha256:
                    target.unlink(missing_ok=True)
                    raise DatasetCommandError(
                        "dataset_file_checksum_mismatch",
                        "A transferred dataset file failed checksum verification.",
                    )
                completed_files += 1
            _raise_if_cancelled(cancel_event)
            marker = {
                "contract": DATASET_MARKER_CONTRACT,
                "transfer_ref": transfer_ref,
                "release_ref": release_ref,
                "dataset_date": dataset_date,
                "inventory_sha256": inventory.inventory_sha256,
                "total_bytes": inventory.total_bytes,
                "file_count": len(inventory.files),
            }
            _write_marker(temporary_root, marker)
            _raise_if_cancelled(cancel_event)
            if local_root.exists():
                raise DatasetCommandError(
                    "dataset_destination_exists",
                    "The final dataset destination already exists.",
                )
            os.replace(temporary_root, local_root)
            publish(_transfer_success_payload(transfer_ref, inventory, local_root))
        except DatasetTransferCancelled:
            with self._lock:
                discard_partial = self._discard_partial
            if discard_partial and temporary_root is not None:
                try:
                    _remove_partial_root(temporary_root)
                except (DatasetCommandError, OSError):
                    publish(
                        {
                            "status": "failed",
                            "transfer_ref": transfer_ref,
                            "error": {
                                "code": "dataset_partial_remove_failed",
                                "message": "The Worker could not remove the partial dataset.",
                            },
                        }
                    )
                    return
            publish(
                {
                    "status": "cancelled" if discard_partial else "paused",
                    "transfer_ref": transfer_ref,
                }
            )
        except DatasetCommandError as exc:
            publish(
                {
                    "status": "failed",
                    "transfer_ref": transfer_ref,
                    "error": {"code": exc.code, "message": exc.message},
                },
            )
        except OSError:
            publish(
                {
                    "status": "failed",
                    "transfer_ref": transfer_ref,
                    "error": {
                        "code": "dataset_transfer_io_error",
                        "message": "The Worker could not read or write dataset files.",
                    },
                },
            )
        except Exception:
            publish(
                {
                    "status": "failed",
                    "transfer_ref": transfer_ref,
                    "error": {
                        "code": "dataset_transfer_source_unavailable",
                        "message": "The Worker could not retrieve the dataset from the center.",
                    },
                },
            )
        finally:
            with self._lock:
                self._active_transfer_ref = None
                self._cancel_event = None
                self._discard_partial = False


def discard_partial_dataset(payload: Mapping[str, object]) -> dict[str, object]:
    transfer_ref, release_ref, dataset_date, destination_parent = (
        _parse_transfer_payload(payload)
    )
    local_root = _final_replica_root(destination_parent, dataset_date, release_ref)
    temporary_root = local_root.parent / f".{local_root.name}.part"
    _remove_partial_root(temporary_root)
    return {"status": "succeeded", "transfer_ref": transfer_ref}


def _remove_partial_root(temporary_root: Path) -> None:
    if not temporary_root.exists():
        return
    if temporary_root.is_symlink() or not temporary_root.is_dir():
        raise DatasetCommandError(
            "dataset_destination_unsafe", "Dataset temporary directory is not safe."
        )
    shutil.rmtree(temporary_root)


def remove_dataset_replica(payload: Mapping[str, object]) -> dict[str, object]:
    replica_ref = _validated_ref(payload.get("replica_ref"), "replica_ref")
    release_ref = _validated_ref(payload.get("release_ref"), "release_ref")
    digest = payload.get("inventory_sha256")
    if not isinstance(digest, str) or not _is_sha256(digest):
        raise DatasetCommandError(
            "dataset_replica_request_invalid", "Replica inventory digest is invalid."
        )
    raw_root = payload.get("local_root")
    if not isinstance(raw_root, str) or not raw_root.startswith("/"):
        raise DatasetCommandError(
            "dataset_replica_request_invalid", "Replica path must be absolute."
        )
    root = Path(raw_root)
    if not root.exists():
        return {"status": "succeeded", "replica_ref": replica_ref}
    if (
        root.is_symlink()
        or not root.is_dir()
        or root.parent == root
        or root.resolve(strict=True) != root
        or root.parent.name != "datapilot-managed"
    ):
        raise DatasetCommandError(
            "dataset_replica_marker_invalid", "Replica directory is not safe to remove."
        )
    marker = _read_marker(root)
    marker_date = marker.get("dataset_date")
    if (
        marker.get("contract") != DATASET_MARKER_CONTRACT
        or marker.get("release_ref") != release_ref
        or marker.get("inventory_sha256") != digest
        or not isinstance(marker_date, str)
        or not _DATASET_DATE.fullmatch(marker_date)
        or root.name != f"{marker_date}-{_short_ref(release_ref)}"
    ):
        raise DatasetCommandError(
            "dataset_replica_marker_invalid",
            "Replica marker does not match the registered dataset.",
        )
    shutil.rmtree(root)
    return {"status": "succeeded", "replica_ref": replica_ref}


def _parse_transfer_payload(
    payload: Mapping[str, object],
) -> tuple[str, str, str, Path]:
    transfer_ref = _validated_ref(payload.get("transfer_ref"), "transfer_ref")
    release_ref = _validated_ref(payload.get("release_ref"), "release_ref")
    dataset_date = payload.get("dataset_date")
    if not isinstance(dataset_date, str) or not _DATASET_DATE.fullmatch(dataset_date):
        raise DatasetCommandError(
            "dataset_transfer_request_invalid", "Dataset date must use YYYYMMDD."
        )
    destination_parent = _existing_directory(payload.get("destination_parent"))
    if not os.access(destination_parent, os.W_OK | os.X_OK):
        raise DatasetCommandError(
            "dataset_destination_not_writable",
            "The Worker cannot write to the selected destination.",
        )
    return transfer_ref, release_ref, dataset_date, destination_parent


def _existing_directory(raw_path: object) -> Path:
    if (
        not isinstance(raw_path, str)
        or not raw_path.startswith("/")
        or len(raw_path) > 4096
        or any(character in raw_path for character in ("\x00", "\r", "\n"))
    ):
        raise DatasetCommandError(
            "directory_path_invalid", "Directory path must be an absolute path."
        )
    try:
        path = Path(raw_path).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise DatasetCommandError(
            "directory_not_found", "The selected directory does not exist."
        ) from exc
    if not path.is_dir() or not os.access(path, os.R_OK | os.X_OK):
        raise DatasetCommandError(
            "directory_not_readable", "The Worker cannot enter this directory."
        )
    return path


def _parse_manifest_file(raw: object) -> DatasetFile:
    if not isinstance(raw, dict):
        raise DatasetCommandError(
            "dataset_manifest_invalid", "Dataset manifest file is invalid."
        )
    file_ref = _validated_ref(raw.get("file_ref"), "file_ref")
    relative_path = raw.get("relative_path")
    size_bytes = raw.get("size_bytes")
    sha256 = raw.get("sha256")
    if not isinstance(relative_path, str) or not _safe_relative_path(relative_path):
        raise DatasetCommandError(
            "dataset_manifest_invalid", "Dataset file path is unsafe."
        )
    if not isinstance(size_bytes, int) or isinstance(size_bytes, bool) or size_bytes < 0:
        raise DatasetCommandError(
            "dataset_manifest_invalid", "Dataset file size is invalid."
        )
    if not isinstance(sha256, str) or not _is_sha256(sha256):
        raise DatasetCommandError(
            "dataset_manifest_invalid", "Dataset file checksum is invalid."
        )
    return DatasetFile(file_ref, relative_path, size_bytes, sha256)


def _safe_relative_path(value: str) -> bool:
    if not value or len(value) > 4096 or "\\" in value or "\x00" in value:
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and all(part not in {"", ".", ".."} for part in path.parts)


def _validated_ref(value: object, label: str) -> str:
    if not isinstance(value, str) or not _SAFE_REF.fullmatch(value):
        raise DatasetCommandError(
            "dataset_command_invalid", f"{label} is invalid."
        )
    return value


def _is_sha256(value: str) -> bool:
    return re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _short_ref(release_ref: str) -> str:
    suffix = release_ref.rsplit("_", 1)[-1]
    candidate = re.sub(r"[^A-Za-z0-9]", "", suffix)[:8]
    return candidate or hashlib.sha256(release_ref.encode("utf-8")).hexdigest()[:8]


def _final_replica_root(parent: Path, dataset_date: str, release_ref: str) -> Path:
    managed_root = parent / "datapilot-managed"
    if managed_root.exists() and (managed_root.is_symlink() or not managed_root.is_dir()):
        raise DatasetCommandError(
            "dataset_destination_unsafe", "Managed dataset directory is not safe."
        )
    managed_root.mkdir(mode=0o750, exist_ok=True)
    return managed_root / f"{dataset_date}-{_short_ref(release_ref)}"


def _prepare_temporary_root(temporary_root: Path, local_root: Path) -> None:
    if local_root.exists():
        raise DatasetCommandError(
            "dataset_destination_exists", "The final dataset destination already exists."
        )
    if temporary_root.exists() and (
        temporary_root.is_symlink() or not temporary_root.is_dir()
    ):
        raise DatasetCommandError(
            "dataset_destination_unsafe", "Dataset temporary directory is not safe."
        )
    temporary_root.mkdir(mode=0o750, parents=False, exist_ok=True)


def _safe_file_target(root: Path, relative_path: str) -> Path:
    if not _safe_relative_path(relative_path):
        raise DatasetCommandError(
            "dataset_manifest_invalid", "Dataset file path is unsafe."
        )
    target = root.joinpath(*PurePosixPath(relative_path).parts)
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise DatasetCommandError(
            "dataset_manifest_invalid", "Dataset file path escaped its root."
        ) from exc
    return target


def _ensure_safe_parent(root: Path, target_parent: Path) -> None:
    relative = target_parent.relative_to(root)
    current = root
    if current.is_symlink():
        raise DatasetCommandError(
            "dataset_target_unsafe", "Dataset target contains a symlink."
        )
    for part in relative.parts:
        current = current / part
        if current.exists():
            if current.is_symlink() or not current.is_dir():
                raise DatasetCommandError(
                    "dataset_target_unsafe", "Dataset target contains an unsafe path."
                )
        else:
            current.mkdir(mode=0o750)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _write_marker(root: Path, payload: Mapping[str, object]) -> None:
    marker = root / DATASET_MARKER_NAME
    temporary_marker = root / f"{DATASET_MARKER_NAME}.tmp"
    temporary_marker.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    os.replace(temporary_marker, marker)


def _read_marker(root: Path) -> dict[str, object]:
    marker = root / DATASET_MARKER_NAME
    try:
        if marker.is_symlink() or not marker.is_file() or marker.stat().st_size > 16_384:
            raise ValueError
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise DatasetCommandError(
            "dataset_replica_marker_invalid", "Replica marker is missing or invalid."
        ) from exc
    if not isinstance(payload, dict):
        raise DatasetCommandError(
            "dataset_replica_marker_invalid", "Replica marker is missing or invalid."
        )
    return payload


def _matching_replica(root: Path, inventory: DatasetInventory) -> bool:
    if not root.exists():
        return False
    if (
        root.is_symlink()
        or not root.is_dir()
        or not root.is_absolute()
        or root.parent.name != "datapilot-managed"
        or root.resolve(strict=True) != root
        or root.name
        != f"{inventory.dataset_date}-{_short_ref(inventory.release_ref)}"
        or not os.access(root, os.R_OK | os.X_OK)
    ):
        raise DatasetCommandError(
            "dataset_destination_exists", "The final dataset destination already exists."
        )
    marker = _read_marker(root)
    if (
        marker.get("contract") == DATASET_MARKER_CONTRACT
        and marker.get("release_ref") == inventory.release_ref
        and marker.get("dataset_date") == inventory.dataset_date
        and marker.get("inventory_sha256") == inventory.inventory_sha256
        and marker.get("file_count") == len(inventory.files)
        and marker.get("total_bytes") == inventory.total_bytes
    ):
        _verify_existing_replica_tree(root)
        return True
    raise DatasetCommandError(
        "dataset_destination_exists", "The final dataset destination already exists."
    )


def _verify_existing_replica_tree(root: Path) -> None:
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            entries: Iterable[os.DirEntry[str]] = os.scandir(directory)
            with entries as scan:
                for entry in scan:
                    if entry.is_symlink():
                        raise DatasetCommandError(
                            "dataset_destination_unsafe",
                            "The existing managed dataset contains a symbolic link.",
                        )
                    if entry.is_dir(follow_symlinks=False):
                        child = Path(entry.path)
                        if not os.access(child, os.R_OK | os.X_OK):
                            raise DatasetCommandError(
                                "dataset_destination_unreadable",
                                "The Worker cannot read the existing managed dataset.",
                            )
                        pending.append(child)
                    elif not entry.is_file(follow_symlinks=False):
                        raise DatasetCommandError(
                            "dataset_destination_unsafe",
                            "The existing managed dataset contains an unsupported file type.",
                        )
        except DatasetCommandError:
            raise
        except OSError as exc:
            raise DatasetCommandError(
                "dataset_destination_unreadable",
                "The Worker cannot read the existing managed dataset.",
            ) from exc


def _raise_if_cancelled(cancel_event: threading.Event) -> None:
    if cancel_event.is_set():
        raise DatasetTransferCancelled()


def _transfer_progress_payload(
    transfer_ref: str,
    bytes_transferred: int,
    total_bytes: int,
    files_completed: int,
    total_files: int,
) -> dict[str, object]:
    return {
        "status": "running",
        "transfer_ref": transfer_ref,
        "progress": {
            "bytes_transferred": bytes_transferred,
            "total_bytes": total_bytes,
            "files_completed": files_completed,
            "total_files": total_files,
        },
    }


def _transfer_success_payload(
    transfer_ref: str, inventory: DatasetInventory, local_root: Path
) -> dict[str, object]:
    return {
        "status": "succeeded",
        "transfer_ref": transfer_ref,
        "replica": {
            "local_root": str(local_root),
            "inventory_sha256": inventory.inventory_sha256,
            "total_bytes": inventory.total_bytes,
            "file_count": len(inventory.files),
            "marker": DATASET_MARKER_NAME,
        },
    }

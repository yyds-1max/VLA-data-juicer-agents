from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Protocol

from vla_data_juicer_agents.navigation.config import NavigationSettings

from .errors import TrainingConflictError, TrainingNotFoundError
from .errors import TrainingError


logger = logging.getLogger(__name__)


class DatasetReleaseCatalog(Protocol):
    """Read-only boundary between Training and immutable published data."""

    def list_releases(self) -> list[dict[str, Any]]: ...

    def build_inventory(self, release_ref: str) -> dict[str, Any]: ...


class PublishedDatasetCatalog:
    """Expose released navigation dates without coupling Training to Annotation.

    The annotation database remains the source of truth for release metadata and
    ``finish_data/<date>`` remains the source of bytes.  Training stores a
    private immutable inventory before it gives a Worker access to a release.
    """

    def __init__(
        self,
        annotation_database_path: str | Path,
        finish_data_root: str | Path | None = None,
    ) -> None:
        self.annotation_database_path = Path(annotation_database_path)
        self.finish_data_root = Path(
            finish_data_root
            if finish_data_root is not None
            else NavigationSettings().finish_data_root
        )

    def list_releases(self) -> list[dict[str, Any]]:
        if not self.annotation_database_path.exists():
            return []
        connection = sqlite3.connect(
            self.annotation_database_path, timeout=30, isolation_level=None
        )
        connection.row_factory = sqlite3.Row
        try:
            table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='dataset_releases'"
            ).fetchone()
            if table is None:
                return []
            rows = connection.execute(
                """SELECT release_ref,dataset_date,source_clip_count,
                total_duration_ns,released_at FROM dataset_releases
                WHERE domain='navigation' ORDER BY dataset_date DESC"""
            ).fetchall()
        finally:
            connection.close()
        return [
            {
                "release_ref": str(row["release_ref"]),
                "dataset_date": str(row["dataset_date"]),
                "status": "released",
                "source_clip_count": int(row["source_clip_count"]),
                "total_duration_ns": int(row["total_duration_ns"]),
                "released_at": str(row["released_at"]),
            }
            for row in rows
        ]

    def build_inventory(self, release_ref: str) -> dict[str, Any]:
        release = next(
            (item for item in self.list_releases() if item["release_ref"] == release_ref),
            None,
        )
        if release is None:
            raise TrainingNotFoundError(
                "dataset_release_not_found", "The released dataset date was not found."
            )
        source_root = self.finish_data_root / release["dataset_date"]
        try:
            root_stat = source_root.lstat()
            resolved_root = source_root.resolve(strict=True)
        except OSError as exc:
            raise TrainingConflictError(
                "dataset_release_source_unavailable",
                "The released dataset files are not available on the center server.",
            ) from exc
        if (
            not source_root.is_absolute()
            or not source_root.is_dir()
            or os.path.islink(source_root)
            or not root_stat
            or resolved_root != source_root
        ):
            raise TrainingConflictError(
                "dataset_release_source_unsafe",
                "The released dataset directory is not a safe regular directory.",
            )

        files: list[dict[str, Any]] = []
        total_bytes = 0
        for path in sorted(source_root.rglob("*")):
            if path.is_symlink():
                raise TrainingConflictError(
                    "dataset_release_contains_symlink",
                    "Released training data cannot contain symbolic links.",
                )
            if path.is_dir():
                continue
            if not path.is_file():
                raise TrainingConflictError(
                    "dataset_release_contains_special_file",
                    "Released training data can contain regular files only.",
                )
            relative_path = path.relative_to(source_root).as_posix()
            digest = hashlib.sha256()
            size = 0
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
                    size += len(chunk)
            total_bytes += size
            files.append(
                {
                    "relative_path": relative_path,
                    "size_bytes": size,
                    "sha256": digest.hexdigest(),
                }
            )
        inventory_digest = hashlib.sha256()
        for item in files:
            inventory_digest.update(item["relative_path"].encode("utf-8"))
            inventory_digest.update(b"\0")
            inventory_digest.update(str(item["size_bytes"]).encode("ascii"))
            inventory_digest.update(b"\0")
            inventory_digest.update(item["sha256"].encode("ascii"))
            inventory_digest.update(b"\n")
        return {
            **release,
            "source_root": str(source_root),
            "files": files,
            "file_count": len(files),
            "total_bytes": total_bytes,
            "inventory_sha256": inventory_digest.hexdigest(),
        }


class DatasetManifestPreparationWorker:
    """Small lifecycle-managed worker for center-side inventory hashing."""

    def __init__(self, store: Any, catalog: DatasetReleaseCatalog, *, tick_seconds: float = 0.25) -> None:
        self.store = store
        self.catalog = catalog
        self.tick_seconds = max(0.05, float(tick_seconds))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="training-dataset-manifest-worker",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(0.0, timeout))

    def run_once(self) -> bool:
        request = self.store.claim_source_manifest_preparation()
        if request is None:
            return False
        try:
            inventory = self.catalog.build_inventory(request["release_ref"])
            self.store.complete_source_manifest(inventory)
        except Exception as exc:
            logger.exception("Dataset source inventory generation failed")
            message = (
                exc.message
                if isinstance(exc, TrainingError)
                else "Dataset inventory generation failed on the center server."
            )
            self.store.fail_source_manifest_preparation(
                request["manifest_ref"],
                "dataset_source_inventory_failed",
                message,
            )
        return True

    def _run(self) -> None:
        while not self._stop.is_set():
            if not self.run_once():
                self._stop.wait(self.tick_seconds)

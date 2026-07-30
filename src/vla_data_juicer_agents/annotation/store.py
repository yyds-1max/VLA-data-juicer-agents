from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator
from uuid import uuid4

from vla_data_juicer_agents.annotation.migrations import (
    AnnotationOfflineMigrationRequiredError,
    LATEST_ANNOTATION_SCHEMA_VERSION,
    UnsupportedAnnotationSchemaVersionError,
    apply_annotation_migrations,
    prepare_annotation_migration_ledger,
)
from vla_data_juicer_agents.annotation.maintenance import (
    acquire_annotation_maintenance,
    annotation_maintenance_lock_path,
)
from vla_data_juicer_agents.annotation.models import (
    AnnotationConflictError,
    AnnotationNotFoundError,
    AnnotationValidationError,
    DraftAnnotationTarget,
)
from vla_data_juicer_agents.navigation.writer_lock import (
    configured_writer_lock_path,
    ensure_navigation_writer_quarantine,
    navigation_writer_marker_state,
    navigation_writer_quarantine_clearance,
    navigation_writer_quarantine_present,
)

_SAFE_RUNTIME_STEP_CODES = frozenset(
    {
        "processing_calibration_snapshot",
        "assemble_finish_temp",
        "preprocess_create_box",
        "preprocess_odom_convert",
        "preprocess_resize",
        "metadata_generate",
        "map_publish",
        "video_prepare",
        "initial_annotation",
        "tracking",
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
        "compatibility_publish",
        "fix_initialize",
        "fix_apply",
        "fix_candidate",
        "fix_compatibility_publish",
    }
)
_SAFE_RUNTIME_DIAGNOSTIC_KINDS = frozenset(
    {"nonzero_exit", "timeout", "cancelled", "error"}
)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _new_ref(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _payload_hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _valid_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _require_safe_handoff_payload(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).lower()
            if any(
                token in normalized
                for token in ("path", "command", "script", "database_id")
            ):
                raise AnnotationValidationError(
                    "unsafe_handoff_payload",
                    "Workflow handoffs cannot contain private implementation fields.",
                )
            _require_safe_handoff_payload(item)
        return
    if isinstance(value, list):
        for item in value:
            _require_safe_handoff_payload(item)
        return
    if isinstance(value, str):
        if (
            value.startswith(("/", "\\"))
            or "/Users/" in value
            or "/media/" in value
            or "\r" in value
            or "\n" in value
        ):
            raise AnnotationValidationError(
                "unsafe_handoff_payload",
                "Workflow handoffs cannot contain private paths.",
            )
        if len(value) > 2000:
            raise AnnotationValidationError(
                "unsafe_handoff_payload",
                "Workflow handoff values are too large.",
            )
        return
    if value is None or isinstance(value, (bool, int, float)):
        return
    raise AnnotationValidationError(
        "unsafe_handoff_payload",
        "Workflow handoff payload types are unsupported.",
    )


def _secure_sqlite_storage(db_path: Path) -> None:
    """Protect the database before SQLite creates WAL sidecars."""

    try:
        parent_metadata = db_path.parent.lstat()
    except OSError as exc:
        raise RuntimeError("annotation database parent is unavailable") from exc
    if (
        stat.S_ISLNK(parent_metadata.st_mode)
        or not stat.S_ISDIR(parent_metadata.st_mode)
        or parent_metadata.st_mode & stat.S_IWGRP
        or parent_metadata.st_mode & stat.S_IWOTH
    ):
        raise RuntimeError(
            "annotation database parent must be a real, non-shared-writable directory",
        )

    _secure_sqlite_file(db_path, create=True)
    for suffix in ("-wal", "-shm"):
        _secure_sqlite_file(Path(f"{db_path}{suffix}"), create=False)


def _secure_sqlite_file(path: Path, *, create: bool) -> None:
    flags = os.O_RDWR
    if create:
        flags |= os.O_CREAT
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileNotFoundError:
        if create:
            raise
        return
    except OSError as exc:
        raise RuntimeError("annotation database storage is unsafe") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise RuntimeError("annotation database storage must be regular files")
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)


def _existing_regular_sqlite_path(db_path: Path) -> Path:
    """Resolve an already-created SQLite database without following a file link."""

    try:
        metadata = db_path.lstat()
    except OSError as exc:
        raise RuntimeError("annotation database must already exist") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError(
            "annotation database must be a real regular file",
        )
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{db_path}{suffix}")
        try:
            sidecar_metadata = sidecar.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise RuntimeError("annotation database sidecar is unavailable") from exc
        if (
            stat.S_ISLNK(sidecar_metadata.st_mode)
            or not stat.S_ISREG(sidecar_metadata.st_mode)
        ):
            raise RuntimeError(
                "annotation database sidecars must be real regular files",
            )
    try:
        return db_path.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError("annotation database is unavailable") from exc


def _private_mutable_sqlite_identity(
    db_path: Path,
) -> tuple[Path, tuple[int, int]]:
    """Validate an existing writable Store without creating or chmodding it."""

    if not db_path.is_absolute():
        raise RuntimeError("annotation database path must be absolute")
    resolved = _existing_regular_sqlite_path(db_path)
    if resolved != db_path:
        raise RuntimeError("annotation database path must be canonical")
    try:
        parent_metadata = resolved.parent.lstat()
    except OSError as exc:
        raise RuntimeError("annotation database parent is unavailable") from exc
    if (
        stat.S_ISLNK(parent_metadata.st_mode)
        or not stat.S_ISDIR(parent_metadata.st_mode)
        or parent_metadata.st_uid != os.geteuid()
        or parent_metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise RuntimeError("annotation database parent is unsafe")

    flags = os.O_RDWR
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(resolved, flags)
        metadata = os.fstat(descriptor)
        current = resolved.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
            or current.st_dev != metadata.st_dev
            or current.st_ino != metadata.st_ino
        ):
            raise RuntimeError("annotation database storage is unsafe")
        identity = (metadata.st_dev, metadata.st_ino)
    except OSError as exc:
        raise RuntimeError("annotation database must remain writable") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)

    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{resolved}{suffix}")
        try:
            metadata = sidecar.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise RuntimeError(
                "annotation database sidecar is unavailable",
            ) from exc
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
        ):
            raise RuntimeError("annotation database sidecar is unsafe")
    return resolved, identity


def _existing_annotation_schema_versions(db_path: Path) -> list[int] | None:
    """Inspect an existing file without creating tables or changing journal mode."""

    try:
        if db_path.stat().st_size == 0:
            return None
        connection = sqlite3.connect(
            f"{db_path.resolve(strict=True).as_uri()}?mode=rw",
            timeout=10,
            uri=True,
        )
    except OSError as exc:
        raise RuntimeError("annotation database is unavailable") from exc
    try:
        ledger = connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'annotation_schema_migrations'
            """
        ).fetchone()
        if ledger is None:
            tables = connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                LIMIT 1
                """
            ).fetchone()
            if tables is None:
                return None
            raise RuntimeError(
                "annotation database is not an initialized AnnotationStore",
            )
        return [
            int(row[0])
            for row in connection.execute(
                """
                SELECT version FROM annotation_schema_migrations
                ORDER BY version
                """
            ).fetchall()
        ]
    except sqlite3.Error as exc:
        raise RuntimeError("annotation database schema cannot be inspected") from exc
    finally:
        connection.close()


def _annotation_migration_safety_status(
    connection: sqlite3.Connection,
) -> str:
    try:
        rows = connection.execute(
            """
            SELECT schema_version, status
            FROM annotation_migration_safety
            WHERE singleton = 1
            """
        ).fetchall()
    except sqlite3.Error as exc:
        raise RuntimeError(
            "annotation database migration safety marker is unavailable"
        ) from exc
    if len(rows) != 1 or int(rows[0][0]) != LATEST_ANNOTATION_SCHEMA_VERSION:
        raise RuntimeError(
            "annotation database migration safety marker is invalid"
        )
    status = str(rows[0][1])
    if status not in {"pending_integrity_check", "verified"}:
        raise RuntimeError(
            "annotation database migration safety marker is invalid"
        )
    return status


def _require_verified_annotation_migration_safety(
    connection: sqlite3.Connection,
) -> None:
    if _annotation_migration_safety_status(connection) != "verified":
        raise RuntimeError(
            "annotation database migration safety verification is incomplete"
        )


def _annotation_database_integrity_results(
    connection: sqlite3.Connection,
) -> tuple[list[Any], list[str]]:
    foreign_key_violations = connection.execute(
        "PRAGMA foreign_key_check"
    ).fetchall()
    integrity = [
        str(row[0])
        for row in connection.execute("PRAGMA integrity_check").fetchall()
    ]
    return foreign_key_violations, integrity


def _verify_and_mark_annotation_migration_safety(
    connection: sqlite3.Connection,
) -> None:
    status = _annotation_migration_safety_status(connection)
    if status == "verified":
        return
    foreign_key_violations, integrity = _annotation_database_integrity_results(
        connection
    )
    migrated_versions = [
        int(row[0])
        for row in connection.execute(
            """
            SELECT version FROM annotation_schema_migrations
            ORDER BY version
            """
        ).fetchall()
    ]
    if foreign_key_violations or integrity != ["ok"]:
        raise RuntimeError(
            "annotation database failed post-migration integrity checks"
        )
    if migrated_versions != list(
        range(1, LATEST_ANNOTATION_SCHEMA_VERSION + 1)
    ):
        raise RuntimeError(
            "annotation database migration ledger is incomplete"
        )
    connection.execute("BEGIN IMMEDIATE")
    updated = connection.execute(
        """
        UPDATE annotation_migration_safety
        SET status = 'verified', verified_at = ?
        WHERE singleton = 1
          AND schema_version = ?
          AND status = 'pending_integrity_check'
        """,
        (_now(), LATEST_ANNOTATION_SCHEMA_VERSION),
    )
    if updated.rowcount != 1:
        connection.rollback()
        raise RuntimeError(
            "annotation database migration safety marker changed"
        )
    connection.commit()


def _require_verified_annotation_migration_safety_on_path(
    db_path: Path,
) -> None:
    try:
        connection = sqlite3.connect(
            f"{db_path.resolve(strict=True).as_uri()}?mode=rw",
            timeout=10,
            uri=True,
        )
    except (OSError, sqlite3.Error) as exc:
        raise RuntimeError("annotation database is unavailable") from exc
    try:
        _require_verified_annotation_migration_safety(connection)
    finally:
        connection.close()


def _require_existing_store_ready_for_open(db_path: Path) -> None:
    versions = _existing_annotation_schema_versions(db_path)
    if versions is None:
        return
    if versions and versions[-1] > LATEST_ANNOTATION_SCHEMA_VERSION:
        raise UnsupportedAnnotationSchemaVersionError(
            "annotation database schema version "
            f"{versions[-1]} is newer than supported version "
            f"{LATEST_ANNOTATION_SCHEMA_VERSION}"
        )
    expected = list(range(1, (versions[-1] if versions else 0) + 1))
    if versions != expected:
        raise RuntimeError(
            f"annotation database has a non-contiguous migration ledger: {versions}"
        )
    if versions != list(range(1, LATEST_ANNOTATION_SCHEMA_VERSION + 1)):
        raise AnnotationOfflineMigrationRequiredError(
            "annotation database requires an explicit offline schema migration"
        )
    _require_verified_annotation_migration_safety_on_path(db_path)


def migrate_annotation_store_offline(
    db_path: Path | str,
    *,
    backup_root: Path | str | None = None,
    maintenance_lease: Any | None = None,
) -> dict[str, Any]:
    """Back up and migrate an existing AnnotationStore under maintenance lock."""

    database = Path(db_path)
    if not database.is_absolute():
        database = (Path.cwd() / database).absolute()
    if maintenance_lease is None:
        maintenance_context: Any = acquire_annotation_maintenance(
            database,
            create_parent=False,
            create_lock_file=True,
        )
    else:
        expected_lock = annotation_maintenance_lock_path(database)
        if (
            bool(getattr(maintenance_lease, "closed", True))
            or getattr(maintenance_lease, "path", None) != expected_lock
        ):
            raise RuntimeError("annotation migration maintenance lease is invalid")
        maintenance_context = nullcontext(maintenance_lease)
    with maintenance_context:
        resolved, _identity = _private_mutable_sqlite_identity(database)
        versions = _existing_annotation_schema_versions(resolved)
        if versions is None:
            raise RuntimeError("annotation database is not initialized")
        if versions and versions[-1] > LATEST_ANNOTATION_SCHEMA_VERSION:
            raise UnsupportedAnnotationSchemaVersionError(
                "annotation database schema is newer than this migration tool"
            )
        expected = list(range(1, (versions[-1] if versions else 0) + 1))
        if versions != expected:
            raise RuntimeError(
                f"annotation database has a non-contiguous migration ledger: {versions}"
            )
        if versions == list(range(1, LATEST_ANNOTATION_SCHEMA_VERSION + 1)):
            raise RuntimeError("annotation database schema is already current")

        destination = (
            Path(backup_root)
            if backup_root is not None
            else resolved.parent
            / (
                "annotation-migration-backup-"
                + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
                + "-"
                + uuid4().hex
            )
        )
        if not destination.is_absolute():
            destination = (Path.cwd() / destination).absolute()
        if destination.exists() or destination.is_symlink():
            raise RuntimeError("annotation migration backup destination already exists")
        try:
            parent_metadata = destination.parent.lstat()
            parent_resolved = destination.parent.resolve(strict=True)
        except OSError as exc:
            raise RuntimeError(
                "annotation migration backup parent is unavailable"
            ) from exc
        if (
            stat.S_ISLNK(parent_metadata.st_mode)
            or not stat.S_ISDIR(parent_metadata.st_mode)
            or parent_metadata.st_uid != os.geteuid()
            or parent_metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            or parent_resolved != destination.parent
        ):
            raise RuntimeError("annotation migration backup parent is unsafe")
        destination.mkdir(mode=0o700)

        copied: list[dict[str, Any]] = []
        try:
            for source in (
                resolved,
                Path(f"{resolved}-wal"),
                Path(f"{resolved}-shm"),
            ):
                try:
                    metadata = source.lstat()
                except FileNotFoundError:
                    continue
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(
                    metadata.st_mode
                ):
                    raise RuntimeError(
                        "annotation migration backup source is unsafe"
                    )
                target = destination / source.name
                shutil.copyfile(source, target, follow_symlinks=False)
                target.chmod(0o600)
                copied.append(
                    {
                        "name": source.name,
                        "size": target.stat().st_size,
                        "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                    }
                )
            manifest = {
                "database_name": resolved.name,
                "source_schema_versions": versions,
                "target_schema_version": LATEST_ANNOTATION_SCHEMA_VERSION,
                "files": copied,
            }
            manifest_path = destination / "backup-manifest.json"
            manifest_path.write_text(
                _canonical_json(manifest),
                encoding="utf-8",
            )
            manifest_path.chmod(0o600)
        except BaseException:
            # The directory is intentionally preserved if any backup copy
            # fails; operators can inspect the partial evidence safely.
            raise

        connection = sqlite3.connect(
            f"{resolved.as_uri()}?mode=rw",
            timeout=10,
            uri=True,
        )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 10000")
            prepare_annotation_migration_ledger(connection)
            apply_annotation_migrations(connection, applied_at=_now())
            _verify_and_mark_annotation_migration_safety(connection)
        finally:
            connection.close()
        return {
            "status": "migrated",
            "from_version": versions[-1] if versions else 0,
            "to_version": LATEST_ANNOTATION_SCHEMA_VERSION,
            "backup_root": str(destination),
            "backup_manifest_sha256": hashlib.sha256(
                (destination / "backup-manifest.json").read_bytes()
            ).hexdigest(),
        }


class AnnotationStore:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        database_preexisted = self.db_path.exists() or self.db_path.is_symlink()
        self._read_only = False
        self._existing_mutable = False
        self._bound_db_identity: tuple[int, int] | None = None
        self.deployment_instance = os.environ.get(
            "VLA_DEPLOYMENT_INSTANCE",
            "deployment_unconfigured",
        )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # SQLite derives new -wal/-shm modes from the database file. Tighten
        # the main file before enabling WAL, then also secure pre-existing
        # sidecars left by an older process.
        _secure_sqlite_storage(self.db_path)
        if database_preexisted:
            _require_existing_store_ready_for_open(self.db_path)
        self._init_schema()
        _secure_sqlite_storage(self.db_path)

    @classmethod
    def open_existing_read_only(cls, db_path: Path | str) -> "AnnotationStore":
        """Open a committed Store for comparison without creating or migrating it."""

        instance = cls.__new__(cls)
        instance.db_path = _existing_regular_sqlite_path(Path(db_path))
        instance._read_only = True
        instance._existing_mutable = False
        metadata = instance.db_path.lstat()
        instance._bound_db_identity = (metadata.st_dev, metadata.st_ino)
        instance.deployment_instance = os.environ.get(
            "VLA_DEPLOYMENT_INSTANCE",
            "deployment_unconfigured",
        )
        instance._require_current_schema()
        return instance

    @classmethod
    def open_existing_mutable(cls, db_path: Path | str) -> "AnnotationStore":
        """Open the current Store read-write without creating or migrating it."""

        inspected = cls.open_existing_read_only(db_path)
        resolved, identity = _private_mutable_sqlite_identity(Path(db_path))
        if identity != inspected._bound_db_identity:
            raise RuntimeError("annotation database identity changed")
        instance = cls.__new__(cls)
        instance.db_path = resolved
        instance._read_only = False
        instance._existing_mutable = True
        instance._bound_db_identity = identity
        instance.deployment_instance = os.environ.get(
            "VLA_DEPLOYMENT_INSTANCE",
            "deployment_unconfigured",
        )
        # The read-only inspection above verifies the complete migration ledger
        # against this exact inode. Recheck through the bound mode=rw path so a
        # same-inode migration race cannot cross the mutable-open boundary. No
        # schema initializer is called here.
        instance._assert_bound_database()
        instance._require_current_schema()
        return instance

    @staticmethod
    def _require_current_schema_on_connection(
        connection: sqlite3.Connection,
    ) -> None:
        try:
            versions = [
                int(row["version"])
                for row in connection.execute(
                    """
                    SELECT version
                    FROM annotation_schema_migrations
                    ORDER BY version
                    """,
                ).fetchall()
            ]
        except sqlite3.Error as exc:
            raise RuntimeError(
                "annotation database is not an initialized AnnotationStore",
            ) from exc
        expected = list(range(1, LATEST_ANNOTATION_SCHEMA_VERSION + 1))
        if versions != expected:
            raise RuntimeError(
                "annotation database migration ledger is not current",
            )
        _require_verified_annotation_migration_safety(connection)

    def _require_current_schema(self) -> None:
        with self._connect() as connection:
            self._require_current_schema_on_connection(connection)

    def _assert_bound_database(self) -> None:
        expected = self._bound_db_identity
        if expected is None:
            return
        try:
            metadata = self.db_path.lstat()
        except OSError as exc:
            raise RuntimeError("annotation database identity changed") from exc
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or (metadata.st_dev, metadata.st_ino) != expected
        ):
            raise RuntimeError("annotation database identity changed")

    def _connect(self) -> sqlite3.Connection:
        self._assert_bound_database()
        if self._read_only:
            connection = sqlite3.connect(
                f"{self.db_path.as_uri()}?mode=ro",
                timeout=10,
                uri=True,
            )
        elif self._existing_mutable:
            connection = sqlite3.connect(
                f"{self.db_path.as_uri()}?mode=rw",
                timeout=10,
                uri=True,
            )
        else:
            connection = sqlite3.connect(self.db_path, timeout=10)
        try:
            self._assert_bound_database()
        except BaseException:
            connection.close()
            raise
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        if self._read_only:
            connection.execute("PRAGMA query_only = ON")
        else:
            connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            prepare_annotation_migration_ledger(connection)
            apply_annotation_migrations(connection, applied_at=_now())
            _verify_and_mark_annotation_migration_safety(connection)

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        if self._read_only:
            raise RuntimeError("read-only AnnotationStore cannot mutate state")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_bound_database()
            self._require_current_schema_on_connection(connection)
            yield connection
            self._assert_bound_database()
            connection.commit()
            self._assert_bound_database()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def mutate(
        self,
        *,
        idempotency_key: str,
        operation: str,
        request_payload: Any,
        callback: Callable[[sqlite3.Connection], dict[str, Any]],
        actor_kind: str = "manual_web",
    ) -> dict[str, Any]:
        if not idempotency_key or len(idempotency_key) > 200:
            raise AnnotationValidationError(
                "invalid_idempotency_key",
                "Idempotency-Key must contain between 1 and 200 characters.",
            )
        request_sha = _payload_hash({"operation": operation, "payload": request_payload})
        if actor_kind not in {"manual_web", "datapilot", "system_worker"}:
            raise AnnotationValidationError(
                "invalid_actor_kind",
                "The annotation mutation actor is unsupported.",
            )
        with self._write() as connection:
            receipt = connection.execute(
                """
                SELECT operation, request_sha256, response_json
                FROM annotation_mutation_receipts
                WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
            if receipt is not None:
                if (
                    receipt["operation"] != operation
                    or receipt["request_sha256"] != request_sha
                ):
                    raise AnnotationConflictError(
                        "idempotency_key_reused",
                        "Idempotency-Key was already used for a different request.",
                    )
                return json.loads(receipt["response_json"])
            response = callback(connection)
            connection.execute(
                """
                INSERT INTO annotation_mutation_receipts (
                    idempotency_key, operation, request_sha256, response_json,
                    actor_kind, deployment_instance, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    idempotency_key,
                    operation,
                    request_sha,
                    _canonical_json(response),
                    actor_kind,
                    self.deployment_instance,
                    _now(),
                ),
            )
            return response

    def replay_receipt(
        self,
        *,
        idempotency_key: str,
        operation: str,
        request_payload: Any,
    ) -> dict[str, Any] | None:
        request_sha = _payload_hash({"operation": operation, "payload": request_payload})
        with self._connect() as connection:
            receipt = connection.execute(
                """
                SELECT operation, request_sha256, response_json
                FROM annotation_mutation_receipts WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
        if receipt is None:
            return None
        if receipt["operation"] != operation or receipt["request_sha256"] != request_sha:
            raise AnnotationConflictError(
                "idempotency_key_reused",
                "Idempotency-Key was already used for a different request.",
            )
        return json.loads(receipt["response_json"])

    def recover_interrupted_runs(
        self,
        *,
        current_owner_epoch: str | None = None,
        writer_lock_path: Path | None = None,
    ) -> int:
        """Fail closed on work whose side effects cannot be inferred after restart."""

        with self._write() as connection:
            return self._recover_interrupted_runs_conn(
                connection,
                current_owner_epoch=current_owner_epoch,
                writer_lock_path=writer_lock_path,
            )

    def has_recovery_quarantine(self) -> bool:
        with self._connect() as connection:
            return self._has_recovery_quarantine_conn(connection)

    def runtime_control_state(self, *, run_id: int, worker_id: str) -> str:
        """Return durable cancellation/lease state for an executing worker."""

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT r.status AS run_status, r.failure_code,
                       j.cancel_requested,
                       l.worker_id AS lease_worker, l.expires_at
                FROM runtime_runs r
                JOIN annotation_jobs j ON j.id = r.job_id
                LEFT JOIN runtime_leases l ON l.run_id = r.id
                WHERE r.id = ?
                """,
                (run_id,),
            ).fetchone()
            if row is None:
                return "not_running"
            if row["run_status"] != "running":
                return (
                    "recovery_required"
                    if row["run_status"] == "failed"
                    and row["failure_code"] == "recovery_required"
                    else "finished"
                )
            if bool(row["cancel_requested"]):
                return "cancel_requested"
            if (
                row["lease_worker"] != worker_id
                or row["expires_at"] is None
                or str(row["expires_at"]) <= _now()
            ):
                return "lease_lost"
            return "continue"

    def active_reserved_bytes(self, *, excluding_job_id: int | None = None) -> int:
        with self._connect() as connection:
            return self._active_reserved_bytes_conn(
                connection,
                excluding_job_id=excluding_job_id,
            )

    def has_job(self, job_ref: str) -> bool:
        with self._connect() as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM annotation_jobs WHERE job_ref = ?",
                    (job_ref,),
                ).fetchone()
                is not None
            )

    def job_has_running_run(self, job_ref: str) -> bool:
        with self._connect() as connection:
            job_id = self._job_id(connection, job_ref)
            return (
                connection.execute(
                    """
                    SELECT 1 FROM runtime_runs
                    WHERE job_id = ? AND status = 'running'
                    LIMIT 1
                    """,
                    (job_id,),
                ).fetchone()
                is not None
            )

    def job_status_for_run(self, run_id: int) -> str:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT j.status
                FROM runtime_runs r
                JOIN annotation_jobs j ON j.id = r.job_id
                WHERE r.id = ?
                """,
                (run_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError("runtime run not found")
            return str(row["status"])

    def cancellation_requested_for_run(self, run_id: int) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT j.cancel_requested
                FROM runtime_runs r
                JOIN annotation_jobs j ON j.id = r.job_id
                WHERE r.id = ?
                """,
                (run_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError("runtime run not found")
            return bool(row["cancel_requested"])

    def create_job(
        self,
        *,
        job_ref: str,
        dataset_date: str,
        source_clips: list[str],
        calibration: dict[str, Any],
        snapshot_dir: Path,
        snapshot_files: list[dict[str, Any]],
        reserved_bytes: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        payload = {
            "dataset_date": dataset_date,
            "source_clips": source_clips,
            "calibration_profile_ref": calibration["profile_ref"],
            "calibration_content_sha256": calibration["content_sha256"],
        }

        def create(connection: sqlite3.Connection) -> dict[str, Any]:
            placeholders = ",".join("?" for _ in source_clips)
            conflicts = connection.execute(
                f"""
                SELECT j.job_ref, l.source_clip
                FROM annotation_source_leases l
                JOIN annotation_jobs j ON j.id = l.job_id
                WHERE l.dataset_date = ?
                  AND l.source_clip IN ({placeholders})
                ORDER BY l.source_clip
                """,
                (dataset_date, *source_clips),
            ).fetchall()
            if conflicts:
                raise AnnotationConflictError(
                    "annotation_scope_conflict",
                    "One or more selected clips already belong to an annotation job.",
                    current={
                        "job_ref": conflicts[0]["job_ref"],
                        "source_clips": [row["source_clip"] for row in conflicts],
                    },
                )
            timestamp = _now()
            cursor = connection.execute(
                """
                INSERT INTO annotation_jobs (
                    job_ref, dataset_date, status, reserved_bytes,
                    created_at, updated_at
                ) VALUES (?, ?, 'preparing', ?, ?, ?)
                """,
                (
                    job_ref,
                    dataset_date,
                    reserved_bytes,
                    timestamp,
                    timestamp,
                ),
            )
            job_id = int(cursor.lastrowid)
            snapshot_ref = _new_ref("calibration")
            snapshot_cursor = connection.execute(
                """
                INSERT INTO calibration_snapshots (
                    snapshot_ref, job_id, profile_ref, label, content_sha256,
                    private_snapshot_dir, files_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_ref,
                    job_id,
                    calibration["profile_ref"],
                    calibration["label"],
                    calibration["content_sha256"],
                    str(snapshot_dir),
                    _canonical_json(snapshot_files),
                    timestamp,
                ),
            )
            connection.execute(
                "UPDATE annotation_jobs SET calibration_snapshot_id = ? WHERE id = ?",
                (int(snapshot_cursor.lastrowid), job_id),
            )
            for ordinal, clip in enumerate(source_clips, start=1):
                connection.execute(
                    """
                    INSERT INTO annotation_job_source_clips (job_id, ordinal, source_clip)
                    VALUES (?, ?, ?)
                    """,
                    (job_id, ordinal, clip),
                )
                connection.execute(
                    """
                    INSERT INTO annotation_source_leases (
                        dataset_date, source_clip, job_id, acquired_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (dataset_date, clip, job_id, timestamp),
                )
            self._enqueue_run(connection, job_id=job_id, kind="prepare")
            return self._job_projection(connection, job_id)

        return self.mutate(
            idempotency_key=idempotency_key,
            operation="create_job",
            request_payload=payload,
            callback=create,
        )

    def list_jobs(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id FROM annotation_jobs ORDER BY created_at DESC"
            ).fetchall()
            return [
                self._job_projection(connection, int(row["id"]), include_segments=False)
                for row in rows
            ]

    def public_event_cursor(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(seq), 0) AS cursor "
                "FROM annotation_public_events"
            ).fetchone()
            return int(row["cursor"])

    def list_public_events_after(
        self,
        *,
        after_seq: int,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        if isinstance(after_seq, bool) or not isinstance(after_seq, int) or after_seq < 0:
            raise ValueError("after_seq must be a non-negative integer")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValueError("event limit must be between 1 and 200")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    seq, event_ref, event_kind, aggregate_kind,
                    job_ref, segment_ref, review_ref,
                    state_revision, status, occurred_at
                FROM annotation_public_events
                WHERE seq > ?
                ORDER BY seq
                LIMIT ?
                """,
                (after_seq, limit),
            ).fetchall()
            return [
                {
                    "seq": int(row["seq"]),
                    "event_ref": str(row["event_ref"]),
                    "event_kind": str(row["event_kind"]),
                    "aggregate_kind": str(row["aggregate_kind"]),
                    **(
                        {"job_ref": str(row["job_ref"])}
                        if row["job_ref"] is not None
                        else {}
                    ),
                    **(
                        {"segment_ref": str(row["segment_ref"])}
                        if row["segment_ref"] is not None
                        else {}
                    ),
                    **(
                        {"review_ref": str(row["review_ref"])}
                        if row["review_ref"] is not None
                        else {}
                    ),
                    "state_revision": int(row["state_revision"]),
                    "status": str(row["status"]),
                    "occurred_at": str(row["occurred_at"]),
                }
                for row in rows
            ]

    def get_job(self, job_ref: str) -> dict[str, Any]:
        with self._connect() as connection:
            return self._job_projection(connection, self._job_id(connection, job_ref))

    def get_segment(self, job_ref: str, segment_ref: str) -> dict[str, Any]:
        with self._connect() as connection:
            job_id = self._job_id(connection, job_ref)
            row = self._segment_row(connection, job_id, segment_ref)
            return self._segment_projection(connection, row, include_draft=True)

    def begin_postprocessing(
        self,
        *,
        job_ref: str,
        expected_job_revision: int,
        spec: dict[str, Any],
        idempotency_key: str,
        processing_navigation_task_ref: str | None = None,
        actor_kind: str = "datapilot",
    ) -> dict[str, Any]:
        payload = {
            "job_ref": job_ref,
            "expected_job_revision": expected_job_revision,
            "spec": spec,
        }
        if processing_navigation_task_ref is not None:
            payload["processing_navigation_task_ref"] = (
                processing_navigation_task_ref
            )

        def begin(connection: sqlite3.Connection) -> dict[str, Any]:
            job_id = self._job_id(connection, job_ref)
            job = self._job_row(connection, job_id)
            self._require_job_revision(job, expected_job_revision, connection)
            if job["status"] != "tracked":
                self._invalid_job_action(connection, job_id)
            segment_rows = connection.execute(
                """
                SELECT * FROM annotation_segments
                WHERE job_id = ? ORDER BY ordinal
                """,
                (job_id,),
            ).fetchall()
            tracked = [row for row in segment_rows if row["status"] == "tracked"]
            if not tracked or any(
                row["status"] not in {"tracked", "skipped"} for row in segment_rows
            ):
                raise AnnotationConflictError(
                    "postprocessing_inputs_not_ready",
                    "The tracked annotation inputs are not ready for postprocessing.",
                    current=self._job_projection(connection, job_id),
                )
            expected_variant = {
                "odom": "cjl_0525_with_gridmap",
                "ins": "cjl_with_gridmap",
            }.get(str(spec.get("localization_kind")))
            if expected_variant is None or spec.get("trajectory_variant") != expected_variant:
                raise AnnotationValidationError(
                    "invalid_postprocessing_spec",
                    "The postprocessing decision is inconsistent.",
                )
            if processing_navigation_task_ref is not None:
                processing_link = connection.execute(
                    """
                    SELECT id
                    FROM annotation_task_links
                    WHERE job_id = ?
                      AND navigation_task_ref = ?
                      AND link_kind = 'processing'
                    """,
                    (job_id, processing_navigation_task_ref),
                ).fetchone()
                if processing_link is None:
                    raise AnnotationValidationError(
                        "invalid_navigation_task_binding",
                        "The postprocessing attempt is not linked to this workflow.",
                    )
                authority = connection.execute(
                    """
                    SELECT link_id
                    FROM annotation_processing_authorities
                    WHERE job_id = ?
                    """,
                    (job_id,),
                ).fetchone()
                if (
                    authority is None
                    or int(authority["link_id"]) != int(processing_link["id"])
                ):
                    raise AnnotationConflictError(
                        "annotation_processing_attempt_superseded",
                        "This processing attempt is no longer authoritative.",
                    )
            spec_json = _canonical_json(spec)
            timestamp = _now()
            connection.execute(
                """
                INSERT INTO postprocessing_specs (
                    spec_ref, job_id, localization_kind, gridmap_decision,
                    trajectory_variant, plan_sha256, observations_sha256,
                    content_sha256, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _new_ref("postprocessing_spec"),
                    job_id,
                    spec["localization_kind"],
                    spec["gridmap_decision"],
                    spec["trajectory_variant"],
                    spec["plan_sha256"],
                    spec["observations_sha256"],
                    hashlib.sha256(spec_json.encode("utf-8")).hexdigest(),
                    timestamp,
                ),
            )
            connection.execute(
                """
                UPDATE annotation_segments
                SET status = 'postprocessing',
                    state_revision = state_revision + 1,
                    updated_at = ?
                WHERE job_id = ? AND status = 'tracked'
                """,
                (timestamp, job_id),
            )
            connection.execute(
                """
                UPDATE annotation_jobs
                SET status = 'postprocessing',
                    state_revision = state_revision + 1,
                    updated_at = ?
                WHERE id = ?
                """,
                (timestamp, job_id),
            )
            self._enqueue_run(
                connection,
                job_id=job_id,
                kind="postprocessing",
            )
            return self._job_projection(connection, job_id)

        return self.mutate(
            idempotency_key=idempotency_key,
            operation="begin_postprocessing",
            request_payload=payload,
            callback=begin,
            actor_kind=actor_kind,
        )

    def complete_postprocessing(
        self,
        *,
        job_ref: str,
        expected_job_revision: int,
        trajectories: list[dict[str, Any]],
        idempotency_key: str,
        actor_kind: str = "system_worker",
    ) -> dict[str, Any]:
        public_trajectories = [
            {
                "segment_ref": item.get("segment_ref"),
                "content_sha256": item.get("content_sha256"),
                "artifact_manifest_ref": item.get("artifact_manifest_ref"),
            }
            for item in trajectories
        ]
        payload = {
            "job_ref": job_ref,
            "expected_job_revision": expected_job_revision,
            "trajectories": public_trajectories,
        }

        def complete(connection: sqlite3.Connection) -> dict[str, Any]:
            job_id = self._job_id(connection, job_ref)
            job = self._job_row(connection, job_id)
            self._require_job_revision(job, expected_job_revision, connection)
            if job["status"] != "postprocessing":
                self._invalid_job_action(connection, job_id)
            postprocessing_run = connection.execute(
                """
                SELECT * FROM runtime_runs
                WHERE job_id = ? AND kind = 'postprocessing'
                ORDER BY attempt DESC LIMIT 1
                """,
                (job_id,),
            ).fetchone()
            if (
                postprocessing_run is None
                or postprocessing_run["status"] not in {"queued", "running"}
            ):
                raise AnnotationConflictError(
                    "postprocessing_run_unavailable",
                    "The postprocessing Runtime run cannot be finalized.",
                    current=self._job_projection(connection, job_id),
                )
            result = self._complete_postprocessing_conn(
                connection,
                job_id=job_id,
                trajectories=trajectories,
            )
            timestamp = _now()
            connection.execute(
                """
                UPDATE runtime_runs
                SET status = 'succeeded', finished_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (timestamp, timestamp, postprocessing_run["id"]),
            )
            connection.execute(
                "DELETE FROM runtime_leases WHERE run_id = ?",
                (postprocessing_run["id"],),
            )
            return result

        return self.mutate(
            idempotency_key=idempotency_key,
            operation="complete_postprocessing",
            request_payload=payload,
            callback=complete,
            actor_kind=actor_kind,
        )

    def postprocessing_inputs(self, job_id: int) -> dict[str, Any]:
        """Resolve private, Store-owned inputs for one running M2 RuntimeRun."""

        with self._connect() as connection:
            job = self._job_row(connection, job_id)
            if job["status"] != "postprocessing":
                raise RuntimeError("postprocessing inputs require an active job")
            spec = connection.execute(
                """
                SELECT * FROM postprocessing_specs
                WHERE job_id = ?
                """,
                (job_id,),
            ).fetchone()
            tracking_manifest_row = connection.execute(
                """
                SELECT a.manifest_json
                FROM artifact_manifests a
                JOIN runtime_runs r ON r.id = a.run_id
                WHERE a.job_id = ? AND a.stage = 'tracking'
                  AND r.kind = 'tracking' AND r.status = 'succeeded'
                ORDER BY a.id DESC LIMIT 1
                """,
                (job_id,),
            ).fetchone()
            if spec is None or tracking_manifest_row is None:
                raise RuntimeError(
                    "postprocessing job lacks a committed spec or Tracking manifest"
                )
            tracking_manifest = json.loads(tracking_manifest_row["manifest_json"])
            runtime_manifest_sha256 = tracking_manifest.get(
                "runtime_manifest_sha256",
            )
            prepared_artifact_tree_sha256 = tracking_manifest.get(
                "prepared_artifact_tree_sha256",
            )
            if (
                not isinstance(runtime_manifest_sha256, str)
                or len(runtime_manifest_sha256) != 64
                or not isinstance(prepared_artifact_tree_sha256, str)
                or not _valid_sha256(prepared_artifact_tree_sha256)
            ):
                raise RuntimeError(
                    "Tracking manifest lacks a valid Runtime or staging hash"
                )
            segments: list[dict[str, Any]] = []
            rows = connection.execute(
                """
                SELECT * FROM annotation_segments
                WHERE job_id = ? AND status = 'postprocessing'
                ORDER BY ordinal
                """,
                (job_id,),
            ).fetchall()
            if not rows:
                raise RuntimeError("postprocessing job has no eligible segments")
            for segment in rows:
                revision = connection.execute(
                    """
                    SELECT targets_json, content_sha256
                    FROM initial_annotation_revisions
                    WHERE segment_id = ? AND revision_number = ?
                    """,
                    (segment["id"], segment["submitted_revision"]),
                ).fetchone()
                if revision is None:
                    raise RuntimeError(
                        "postprocessing segment lacks its annotation revision"
                    )
                targets = json.loads(revision["targets_json"])
                checkpoint_rows = connection.execute(
                    """
                    SELECT target_ref, identity, artifact_sha256
                    FROM tracking_checkpoints
                    WHERE job_id = ? AND segment_id = ?
                      AND revision_sha256 = ?
                    ORDER BY id
                    """,
                    (
                        job_id,
                        segment["id"],
                        revision["content_sha256"],
                    ),
                ).fetchall()
                checkpoints = {
                    str(row["target_ref"]): row for row in checkpoint_rows
                }
                if len(checkpoints) != len(targets):
                    raise RuntimeError(
                        "postprocessing segment has an incomplete checkpoint set"
                    )
                target_bindings: dict[str, str] = {}
                tracking_identities: list[str] = []
                for ordinal, target in enumerate(targets):
                    target_ref = str(target.get("target_ref", ""))
                    checkpoint = checkpoints.get(target_ref)
                    if checkpoint is None:
                        raise RuntimeError(
                            "postprocessing checkpoint target mapping changed"
                        )
                    identity = str(checkpoint["identity"])
                    expected_type = "master" if ordinal == 0 else f"other{ordinal}"
                    if identity.split("_", 1)[0] != expected_type:
                        raise RuntimeError(
                            "postprocessing checkpoint identity order changed"
                        )
                    target_bindings[target_ref] = expected_type
                    tracking_identities.append(identity)
                segments.append(
                    {
                        "segment_ref": str(segment["segment_ref"]),
                        "source_clip": str(segment["source_clip"]),
                        "private_segment_key": str(
                            segment["private_segment_key"]
                        ),
                        "private_segment_root": str(
                            segment["private_segment_root"]
                        ),
                        "annotation_revision_sha256": str(
                            revision["content_sha256"]
                        ),
                        "target_bindings": target_bindings,
                        "tracking_identities": tracking_identities,
                    }
                )
            return {
                "job_ref": str(job["job_ref"]),
                "dataset_date": str(job["dataset_date"]),
                "tracked_staging_root": str(job["staging_root"]),
                "runtime_manifest_sha256": runtime_manifest_sha256,
                "prepared_artifact_tree_sha256": (
                    prepared_artifact_tree_sha256
                ),
                "spec": {
                    "spec_ref": str(spec["spec_ref"]),
                    "localization_kind": str(spec["localization_kind"]),
                    "gridmap_decision": str(spec["gridmap_decision"]),
                    "trajectory_variant": str(spec["trajectory_variant"]),
                    "plan_sha256": str(spec["plan_sha256"]),
                    "observations_sha256": str(spec["observations_sha256"]),
                    "content_sha256": str(spec["content_sha256"]),
                },
                "segments": segments,
            }

    def complete_postprocessing_run(
        self,
        *,
        run_id: int,
        trajectories: list[dict[str, Any]],
        manifest: dict[str, Any],
    ) -> dict[str, Any]:
        """Atomically close the Runtime ledger and create review work."""

        with self._write() as connection:
            run = self._running_run(connection, run_id)
            if run["kind"] != "postprocessing":
                raise RuntimeError(
                    "postprocessing completion belongs to a different run kind"
                )
            job = self._job_row(connection, int(run["job_id"]))
            if job["status"] == "cancelled" or bool(job["cancel_requested"]):
                self._finish_run(connection, run_id, "cancelled")
                self._finalize_cancelled_job(
                    connection,
                    int(run["job_id"]),
                    completion_outcome="cancelled_by_user",
                )
                return self._job_projection(connection, int(run["job_id"]))
            if job["status"] != "postprocessing":
                raise RuntimeError(
                    "postprocessing run no longer owns its annotation job"
                )
            self._require_runtime_step_ledger(
                connection,
                run_id,
                manifest.get("command_steps"),
            )
            manifest_ref = self._insert_manifest(
                connection,
                run,
                "postprocessing",
                manifest,
            )
            for item in trajectories:
                if item.get("artifact_manifest_ref") not in {None, manifest_ref}:
                    raise RuntimeError(
                        "trajectory result references a different manifest"
                    )
                item["artifact_manifest_ref"] = manifest_ref
            result = self._complete_postprocessing_conn(
                connection,
                job_id=int(run["job_id"]),
                trajectories=trajectories,
            )
            self._finish_run(connection, run_id, "succeeded")
            return result

    def _complete_postprocessing_conn(
        self,
        connection: sqlite3.Connection,
        *,
        job_id: int,
        trajectories: list[dict[str, Any]],
    ) -> dict[str, Any]:
        eligible_rows = connection.execute(
            """
            SELECT * FROM annotation_segments
            WHERE job_id = ? AND status = 'postprocessing'
            ORDER BY ordinal
            """,
            (job_id,),
        ).fetchall()
        expected_refs = [str(row["segment_ref"]) for row in eligible_rows]
        supplied_refs = [
            str(item.get("segment_ref", "")) for item in trajectories
        ]
        if (
            not expected_refs
            or supplied_refs != expected_refs
            or len(set(supplied_refs)) != len(supplied_refs)
        ):
            raise AnnotationValidationError(
                "postprocessing_result_scope_mismatch",
                "Postprocessing results must cover every eligible segment in order.",
            )
        timestamp = _now()
        review_refs: list[str] = []
        for segment, item in zip(eligible_rows, trajectories, strict=True):
            state = item.get("state")
            if not isinstance(state, dict):
                raise AnnotationValidationError(
                    "invalid_trajectory_result",
                    "A trajectory result must contain an object state.",
                )
            state_json = _canonical_json(state)
            state_sha = hashlib.sha256(state_json.encode("utf-8")).hexdigest()
            supplied_state_sha = item.get("content_sha256")
            if supplied_state_sha is not None and supplied_state_sha != state_sha:
                raise AnnotationValidationError(
                    "trajectory_result_hash_mismatch",
                    "A trajectory result does not match its declared state hash.",
                )
            private_artifact_path = item.get("private_artifact_path")
            private_compatibility_path = item.get(
                "private_compatibility_path",
            )
            artifact_sha256 = item.get("artifact_sha256")
            artifact_manifest_ref = item.get("artifact_manifest_ref")
            if (
                not isinstance(private_artifact_path, str)
                or not Path(private_artifact_path).is_absolute()
                or not isinstance(private_compatibility_path, str)
                or not Path(private_compatibility_path).is_absolute()
                or not isinstance(artifact_sha256, str)
                or len(artifact_sha256) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in artifact_sha256
                )
                or not isinstance(artifact_manifest_ref, str)
                or not artifact_manifest_ref.startswith("artifact_manifest_")
            ):
                raise AnnotationValidationError(
                    "invalid_trajectory_artifact",
                    "A trajectory result lacks its immutable artifact evidence.",
                )
            revision_number_row = connection.execute(
                """
                SELECT COALESCE(MAX(revision_number), 0) + 1 AS next_revision
                FROM trajectory_revisions WHERE segment_id = ?
                """,
                (segment["id"],),
            ).fetchone()
            trajectory_ref = _new_ref("trajectory_revision")
            cursor = connection.execute(
                """
                INSERT INTO trajectory_revisions (
                    revision_ref, job_id, segment_id, revision_number,
                    content_sha256, private_artifact_path,
                    private_compatibility_path, artifact_sha256,
                    private_state_json, artifact_manifest_ref, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trajectory_ref,
                    job_id,
                    segment["id"],
                    int(revision_number_row["next_revision"]),
                    state_sha,
                    private_artifact_path,
                    private_compatibility_path,
                    artifact_sha256,
                    state_json,
                    artifact_manifest_ref,
                    timestamp,
                ),
            )
            review_ref = _new_ref("review")
            connection.execute(
                """
                INSERT INTO trajectory_review_tasks (
                    review_ref, trajectory_revision_id, status,
                    state_revision, created_at, updated_at
                ) VALUES (?, ?, 'pending', 0, ?, ?)
                """,
                (review_ref, int(cursor.lastrowid), timestamp, timestamp),
            )
            review_refs.append(review_ref)
            connection.execute(
                """
                UPDATE annotation_segments
                SET status = 'annotated',
                    state_revision = state_revision + 1,
                    updated_at = ?
                WHERE id = ?
                """,
                (timestamp, segment["id"]),
            )
        connection.execute(
            """
            UPDATE annotation_jobs
            SET status = 'annotated',
                completion_outcome = 'postprocessing_complete',
                state_revision = state_revision + 1,
                updated_at = ?
            WHERE id = ?
            """,
            (timestamp, job_id),
        )
        job_ref = str(self._job_row(connection, job_id)["job_ref"])
        handoff_payload = {
            "job_ref": job_ref,
            "review_count": len(review_refs),
        }
        connection.execute(
            """
            INSERT INTO workflow_handoffs (
                handoff_ref, job_id, kind, payload_json,
                content_sha256, created_at
            ) VALUES (?, ?, 'fix_ready', ?, ?, ?)
            """,
            (
                _new_ref("handoff"),
                job_id,
                _canonical_json(handoff_payload),
                _payload_hash(handoff_payload),
                timestamp,
            ),
        )
        connection.execute(
            """
            INSERT INTO workflow_handoffs (
                handoff_ref, job_id, kind, payload_json,
                content_sha256, created_at
            ) VALUES (?, ?, 'postprocessing_completed', ?, ?, ?)
            """,
            (
                _new_ref("handoff"),
                job_id,
                _canonical_json(handoff_payload),
                _payload_hash(handoff_payload),
                timestamp,
            ),
        )
        result = self._job_projection(connection, job_id)
        result["review_summary"] = {
            "total": len(review_refs),
            "pending": len(review_refs),
        }
        return result

    def list_reviews(
        self,
        *,
        status: str | None = None,
        dataset_date: str | None = None,
        source_clip: str | None = None,
    ) -> list[dict[str, Any]]:
        allowed = {"pending", "in_progress", "returned", "approved", "discarded"}
        if status is not None and status not in allowed:
            raise AnnotationValidationError(
                "invalid_review_status",
                "The requested trajectory review status is unsupported.",
            )
        with self._connect() as connection:
            clauses: list[str] = []
            arguments: list[Any] = []
            if status is not None:
                clauses.append("r.status = ?")
                arguments.append(status)
            if dataset_date is not None:
                clauses.append("j.dataset_date = ?")
                arguments.append(dataset_date)
            if source_clip is not None:
                clauses.append("s.source_clip = ?")
                arguments.append(source_clip)
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            rows = connection.execute(
                f"""
                SELECT r.id
                FROM trajectory_review_tasks r
                JOIN trajectory_revisions t ON t.id = r.trajectory_revision_id
                JOIN annotation_jobs j ON j.id = t.job_id
                JOIN annotation_segments s ON s.id = t.segment_id
                {where}
                ORDER BY r.updated_at DESC, r.id DESC
                """,
                arguments,
            ).fetchall()
            return [
                self._review_projection(connection, int(row["id"]))
                for row in rows
            ]

    def get_review(self, review_ref: str) -> dict[str, Any]:
        with self._connect() as connection:
            return self._review_projection(
                connection,
                self._review_id(connection, review_ref),
            )

    def review_evidence_private(self, review_ref: str) -> dict[str, Any]:
        """Return private artifact bindings for the in-process evidence service."""

        with self._connect() as connection:
            review_id = self._review_id(connection, review_ref)
            row = connection.execute(
                """
                SELECT r.review_ref, r.status, r.state_revision,
                       t.revision_ref, t.content_sha256,
                       t.private_artifact_path, t.artifact_sha256,
                       t.private_state_json
                FROM trajectory_review_tasks r
                JOIN trajectory_revisions t
                  ON t.id = r.trajectory_revision_id
                WHERE r.id = ?
                """,
                (review_id,),
            ).fetchone()
            if row is None:
                raise AnnotationNotFoundError("trajectory review not found")
            try:
                state = json.loads(row["private_state_json"])
            except (TypeError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    "trajectory revision state cannot be decoded"
                ) from exc
            if (
                not isinstance(state, dict)
                or _payload_hash(state) != row["content_sha256"]
            ):
                raise RuntimeError("trajectory revision state hash changed")
            draft_row = connection.execute(
                """
                SELECT state_json, content_sha256, draft_revision
                FROM fix_drafts
                WHERE review_id = ?
                """,
                (review_id,),
            ).fetchone()
            draft_state = None
            draft_revision = None
            if draft_row is not None:
                try:
                    draft_state = json.loads(draft_row["state_json"])
                except (TypeError, json.JSONDecodeError) as exc:
                    raise RuntimeError("Fix draft state cannot be decoded") from exc
                if (
                    not isinstance(draft_state, dict)
                    or _payload_hash(draft_state) != draft_row["content_sha256"]
                ):
                    raise RuntimeError("Fix draft state hash changed")
                draft_revision = int(draft_row["draft_revision"])
            return {
                "review_ref": str(row["review_ref"]),
                "status": str(row["status"]),
                "state_revision": int(row["state_revision"]),
                "trajectory_revision_ref": str(row["revision_ref"]),
                "trajectory_state": state,
                "private_artifact_path": str(row["private_artifact_path"]),
                "artifact_sha256": str(row["artifact_sha256"]),
                "draft_state": draft_state,
                "draft_revision": draft_revision,
            }

    def fix_runtime_input(self, review_ref: str) -> dict[str, Any]:
        """Return private state solely for a configured Fix adapter."""

        with self._connect() as connection:
            review_id = self._review_id(connection, review_ref)
            row = connection.execute(
                """
                SELECT r.status, r.state_revision,
                       t.private_state_json AS trajectory_state_json,
                       d.draft_revision, d.state_json AS draft_state_json,
                       d.original_state_json, d.content_sha256 AS draft_sha256,
                       d.draft_ref,
                       c.snapshot_ref AS calibration_snapshot_ref,
                       c.profile_ref, c.content_sha256 AS calibration_sha256,
                       c.private_snapshot_dir, c.files_json AS calibration_files_json,
                       c.difference_reason
                FROM trajectory_review_tasks r
                JOIN trajectory_revisions t ON t.id = r.trajectory_revision_id
                LEFT JOIN fix_drafts d ON d.id = r.active_fix_draft_id
                LEFT JOIN fix_calibration_snapshots c
                    ON c.id = d.calibration_snapshot_id
                WHERE r.id = ?
                """,
                (review_id,),
            ).fetchone()
            if row is None:
                raise AnnotationNotFoundError("trajectory review not found")
            return {
                "status": str(row["status"]),
                "state_revision": int(row["state_revision"]),
                "trajectory_state": json.loads(row["trajectory_state_json"]),
                "draft": (
                    {
                        "draft_ref": row["draft_ref"],
                        "revision": int(row["draft_revision"]),
                        "state": json.loads(row["draft_state_json"]),
                        "original_state": json.loads(row["original_state_json"]),
                        "content_sha256": row["draft_sha256"],
                        "calibration": {
                            "profile_ref": row["profile_ref"],
                            "content_sha256": row["calibration_sha256"],
                            "snapshot_ref": row["calibration_snapshot_ref"],
                            "private_snapshot_dir": row["private_snapshot_dir"],
                            "files": json.loads(row["calibration_files_json"]),
                            "difference_reason": row["difference_reason"],
                        },
                    }
                    if row["draft_ref"] is not None
                    else None
                ),
            }

    def fix_revision_runtime_input(
        self,
        review_ref: str,
        fix_revision_ref: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            review_id = self._review_id(connection, review_ref)
            row = connection.execute(
                """
                SELECT f.state_json, f.content_sha256, f.revision_ref,
                       f.private_artifact_path, f.artifact_sha256,
                       f.artifact_manifest_ref,
                       t.private_compatibility_path,
                       r.state_revision, r.status
                FROM fix_revisions f
                JOIN trajectory_review_tasks r ON r.id = f.review_id
                JOIN trajectory_revisions t
                  ON t.id = f.base_trajectory_revision_id
                WHERE f.review_id = ? AND f.revision_ref = ?
                """,
                (review_id, fix_revision_ref),
            ).fetchone()
            if row is None:
                raise AnnotationNotFoundError("Fix revision not found")
            return {
                "review_status": row["status"],
                "review_revision": int(row["state_revision"]),
                "fix_revision_ref": row["revision_ref"],
                "content_sha256": row["content_sha256"],
                "private_artifact_path": row["private_artifact_path"],
                "artifact_sha256": row["artifact_sha256"],
                "artifact_manifest_ref": row["artifact_manifest_ref"],
                "private_compatibility_path": row[
                    "private_compatibility_path"
                ],
                "state": json.loads(row["state_json"]),
            }

    def latest_failed_publication_input(self, review_ref: str) -> dict[str, Any]:
        with self._connect() as connection:
            review_id = self._review_id(connection, review_ref)
            row = connection.execute(
                """
                SELECT p.status, f.revision_ref, f.state_json, f.content_sha256,
                       f.private_artifact_path, f.artifact_sha256,
                       f.artifact_manifest_ref,
                       t.private_compatibility_path,
                       r.status AS review_status,
                       r.state_revision AS review_revision
                FROM compatibility_publications p
                JOIN fix_revisions f ON f.id = p.fix_revision_id
                JOIN trajectory_review_tasks r ON r.id = p.review_id
                JOIN trajectory_revisions t
                  ON t.id = f.base_trajectory_revision_id
                WHERE p.review_id = ?
                ORDER BY p.attempt DESC
                LIMIT 1
                """,
                (review_id,),
            ).fetchone()
            if row is None or row["status"] != "failed":
                raise AnnotationConflictError(
                    "publication_retry_unavailable",
                    "There is no failed compatibility publication to retry.",
                    current=self._review_projection(connection, review_id),
                )
            return {
                "review_status": row["review_status"],
                "review_revision": int(row["review_revision"]),
                "fix_revision_ref": row["revision_ref"],
                "content_sha256": row["content_sha256"],
                "private_artifact_path": row["private_artifact_path"],
                "artifact_sha256": row["artifact_sha256"],
                "artifact_manifest_ref": row["artifact_manifest_ref"],
                "private_compatibility_path": row[
                    "private_compatibility_path"
                ],
                "state": json.loads(row["state_json"]),
            }

    def compatibility_publication_inputs(
        self,
        run_id: int,
    ) -> dict[str, Any]:
        """Return the immutable approved FixRevision bound to a claimed run."""

        with self._connect() as connection:
            run = self._running_run(connection, run_id)
            if run["kind"] != "compatibility_publish":
                raise RuntimeError(
                    "compatibility publication input belongs to another run kind"
                )
            row = connection.execute(
                """
                SELECT p.publication_ref, p.attempt,
                       p.status AS publication_status,
                       r.review_ref, r.status AS review_status,
                       r.approved_fix_revision_id,
                       f.id AS fix_revision_id,
                       f.revision_ref AS fix_revision_ref,
                       f.content_sha256 AS fix_content_sha256,
                       f.private_artifact_path AS candidate_segment_root,
                       f.artifact_sha256 AS candidate_tree_sha256,
                       f.state_json AS fix_state_json,
                       t.private_compatibility_path AS target_segment_root
                FROM runtime_run_publication_links l
                JOIN compatibility_publications p
                  ON p.id = l.publication_id
                JOIN trajectory_review_tasks r ON r.id = l.review_id
                JOIN fix_revisions f ON f.id = p.fix_revision_id
                JOIN trajectory_revisions t
                  ON t.id = f.base_trajectory_revision_id
                WHERE l.run_id = ?
                """,
                (run_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError(
                    "compatibility publication lacks its durable binding"
                )
            if (
                row["publication_status"] != "running"
                or row["review_status"] != "approved"
                or int(row["approved_fix_revision_id"])
                != int(row["fix_revision_id"])
            ):
                raise RuntimeError(
                    "compatibility publication no longer owns the approved revision"
                )
            return {
                "publication_ref": str(row["publication_ref"]),
                "publication_attempt": int(row["attempt"]),
                "review_ref": str(row["review_ref"]),
                "fix_revision_ref": str(row["fix_revision_ref"]),
                "fix_content_sha256": str(row["fix_content_sha256"]),
                "candidate_segment_root": str(
                    row["candidate_segment_root"],
                ),
                "candidate_tree_sha256": str(
                    row["candidate_tree_sha256"],
                ),
                "target_segment_root": str(row["target_segment_root"]),
                "state": json.loads(row["fix_state_json"]),
            }

    def complete_compatibility_publication(
        self,
        *,
        run_id: int,
        content_sha256: str,
        private_artifact_path: str,
        manifest: dict[str, Any],
    ) -> dict[str, Any]:
        """Commit publication evidence after the writer has atomically published."""

        with self._write() as connection:
            run = self._running_run(connection, run_id)
            if run["kind"] != "compatibility_publish":
                raise RuntimeError(
                    "compatibility publication completion belongs to another run kind"
                )
            row = connection.execute(
                """
                SELECT p.id AS publication_id, p.status AS publication_status,
                       p.review_id, r.review_ref,
                       r.status AS review_status,
                       r.approved_fix_revision_id,
                       f.id AS fix_revision_id,
                       f.content_sha256 AS expected_content_sha256,
                       t.private_compatibility_path AS target_segment_root
                FROM runtime_run_publication_links l
                JOIN compatibility_publications p
                  ON p.id = l.publication_id
                JOIN trajectory_review_tasks r ON r.id = l.review_id
                JOIN fix_revisions f ON f.id = p.fix_revision_id
                JOIN trajectory_revisions t
                  ON t.id = f.base_trajectory_revision_id
                WHERE l.run_id = ?
                """,
                (run_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError(
                    "compatibility publication completion lacks its binding"
                )
            if (
                row["publication_status"] != "running"
                or row["review_status"] != "approved"
                or int(row["approved_fix_revision_id"])
                != int(row["fix_revision_id"])
            ):
                raise RuntimeError(
                    "compatibility publication completion lost its approval"
                )
            target = Path(private_artifact_path)
            target_root = Path(str(row["target_segment_root"]))
            if (
                not _valid_sha256(content_sha256)
                or content_sha256 != row["expected_content_sha256"]
                or not target.is_absolute()
                or target.parent != target_root
            ):
                raise RuntimeError(
                    "compatibility publication artifact identity is invalid"
                )
            self._require_runtime_step_ledger(
                connection,
                run_id,
                manifest.get("command_steps"),
            )
            manifest_ref = self._insert_manifest(
                connection,
                run,
                "compatibility_publish",
                manifest,
            )
            timestamp = _now()
            connection.execute(
                """
                UPDATE compatibility_publications
                SET status = 'succeeded', content_sha256 = ?,
                    private_artifact_path = ?, artifact_manifest_ref = ?
                WHERE id = ?
                """,
                (
                    content_sha256,
                    private_artifact_path,
                    manifest_ref,
                    row["publication_id"],
                ),
            )
            connection.execute(
                """
                UPDATE trajectory_review_tasks
                SET state_revision = state_revision + 1, updated_at = ?
                WHERE id = ?
                """,
                (timestamp, row["review_id"]),
            )
            handoff_payload = {
                "review_ref": str(row["review_ref"]),
                "decision": "approved",
            }
            connection.execute(
                """
                INSERT INTO workflow_handoffs (
                    handoff_ref, job_id, review_id, kind, payload_json,
                    content_sha256, created_at
                ) VALUES (?, ?, ?, 'review_completed', ?, ?, ?)
                """,
                (
                    _new_ref("handoff"),
                    run["job_id"],
                    row["review_id"],
                    _canonical_json(handoff_payload),
                    _payload_hash(handoff_payload),
                    timestamp,
                ),
            )
            self._finish_run(connection, run_id, "succeeded")
            return self._review_projection(
                connection,
                int(row["review_id"]),
            )

    def create_fix_session(
        self,
        *,
        review_ref: str,
        expected_review_revision: int,
        calibration: dict[str, Any],
        snapshot_ref: str,
        snapshot_dir: Path,
        snapshot_files: list[dict[str, Any]],
        difference_reason: str | None,
        initial_state: dict[str, Any],
        initial_state_sha256: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        payload = {
            "review_ref": review_ref,
            "expected_review_revision": expected_review_revision,
            "calibration_profile_ref": calibration["profile_ref"],
            "calibration_content_sha256": calibration["content_sha256"],
            "difference_reason": difference_reason,
        }

        def create(connection: sqlite3.Connection) -> dict[str, Any]:
            review_id = self._review_id(connection, review_ref)
            review = self._review_row(connection, review_id)
            self._require_review_revision(
                connection,
                review,
                expected_review_revision,
            )
            if review["status"] != "pending" or review["active_fix_draft_id"] is not None:
                self._invalid_review_action(connection, review_id)
            processing = connection.execute(
                """
                SELECT c.profile_ref
                FROM trajectory_review_tasks r
                JOIN trajectory_revisions t ON t.id = r.trajectory_revision_id
                JOIN annotation_jobs j ON j.id = t.job_id
                JOIN calibration_snapshots c ON c.id = j.calibration_snapshot_id
                WHERE r.id = ?
                """,
                (review_id,),
            ).fetchone()
            differs = str(processing["profile_ref"]) != calibration["profile_ref"]
            if differs and not difference_reason:
                raise AnnotationValidationError(
                    "fix_calibration_reason_required",
                    "A reason is required when Fix calibration differs from processing.",
                )
            state_json = _canonical_json(initial_state)
            actual_sha = hashlib.sha256(state_json.encode("utf-8")).hexdigest()
            if actual_sha != initial_state_sha256:
                raise AnnotationValidationError(
                    "fix_runtime_hash_mismatch",
                    "The Fix runtime result does not match its declared hash.",
                )
            timestamp = _now()
            # Snapshot identity is allocated before filesystem capture so its
            # owned directory and durable ledger cannot diverge.
            snapshot_cursor = connection.execute(
                """
                INSERT INTO fix_calibration_snapshots (
                    snapshot_ref, review_id, profile_ref, label,
                    content_sha256, private_snapshot_dir, files_json,
                    differs_from_processing, difference_reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_ref,
                    review_id,
                    calibration["profile_ref"],
                    calibration["label"],
                    calibration["content_sha256"],
                    str(snapshot_dir),
                    _canonical_json(snapshot_files),
                    int(differs),
                    difference_reason,
                    timestamp,
                ),
            )
            trajectory_id = int(review["trajectory_revision_id"])
            draft_cursor = connection.execute(
                """
                INSERT INTO fix_drafts (
                    draft_ref, review_id, calibration_snapshot_id,
                    base_trajectory_revision_id, draft_revision,
                    original_state_json, state_json, content_sha256,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
                """,
                (
                    _new_ref("fix_draft"),
                    review_id,
                    int(snapshot_cursor.lastrowid),
                    trajectory_id,
                    state_json,
                    state_json,
                    actual_sha,
                    timestamp,
                    timestamp,
                ),
            )
            connection.execute(
                """
                UPDATE trajectory_review_tasks
                SET status = 'in_progress', state_revision = state_revision + 1,
                    active_fix_draft_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (int(draft_cursor.lastrowid), timestamp, review_id),
            )
            return self._review_projection(connection, review_id)

        return self.mutate(
            idempotency_key=idempotency_key,
            operation="create_fix_session",
            request_payload=payload,
            callback=create,
        )

    def apply_fix_command_result(
        self,
        *,
        review_ref: str,
        expected_review_revision: int,
        expected_draft_revision: int,
        command: dict[str, Any],
        result_state: dict[str, Any],
        result_sha256: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        payload = {
            "review_ref": review_ref,
            "expected_review_revision": expected_review_revision,
            "expected_draft_revision": expected_draft_revision,
            "command": command,
        }

        def apply(connection: sqlite3.Connection) -> dict[str, Any]:
            review_id = self._review_id(connection, review_ref)
            review = self._review_row(connection, review_id)
            self._require_review_revision(
                connection,
                review,
                expected_review_revision,
            )
            if review["status"] != "in_progress" or review["active_fix_draft_id"] is None:
                self._invalid_review_action(connection, review_id)
            active = connection.execute(
                """
                SELECT 1
                FROM runtime_run_review_links l
                JOIN runtime_runs r ON r.id = l.run_id
                WHERE l.review_id = ?
                  AND r.status IN ('queued', 'running')
                LIMIT 1
                """,
                (review_id,),
            ).fetchone()
            if active is not None:
                raise AnnotationConflictError(
                    "fix_runtime_already_active",
                    "The Fix draft is frozen while a revision is generated.",
                    current=self._review_projection(connection, review_id),
                )
            draft = connection.execute(
                "SELECT * FROM fix_drafts WHERE id = ?",
                (review["active_fix_draft_id"],),
            ).fetchone()
            if int(draft["draft_revision"]) != expected_draft_revision:
                raise AnnotationConflictError(
                    "fix_draft_revision_conflict",
                    "The Fix draft changed; refresh before retrying.",
                    current=self._review_projection(connection, review_id),
                )
            state_json = _canonical_json(result_state)
            actual_sha = hashlib.sha256(state_json.encode("utf-8")).hexdigest()
            if actual_sha != result_sha256:
                raise AnnotationValidationError(
                    "fix_runtime_hash_mismatch",
                    "The Fix runtime result does not match its declared hash.",
                )
            next_revision = expected_draft_revision + 1
            timestamp = _now()
            connection.execute(
                """
                UPDATE fix_drafts
                SET draft_revision = ?, state_json = ?, content_sha256 = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    next_revision,
                    state_json,
                    actual_sha,
                    timestamp,
                    draft["id"],
                ),
            )
            connection.execute(
                """
                INSERT INTO fix_command_actions (
                    action_ref, review_id, draft_revision, command_json,
                    result_sha256, actor_kind, deployment_instance, created_at
                ) VALUES (?, ?, ?, ?, ?, 'manual_web', ?, ?)
                """,
                (
                    _new_ref("fix_action"),
                    review_id,
                    next_revision,
                    _canonical_json(command),
                    actual_sha,
                    self.deployment_instance,
                    timestamp,
                ),
            )
            connection.execute(
                """
                UPDATE trajectory_review_tasks
                SET state_revision = state_revision + 1, updated_at = ?
                WHERE id = ?
                """,
                (timestamp, review_id),
            )
            return self._review_projection(connection, review_id)

        return self.mutate(
            idempotency_key=idempotency_key,
            operation="apply_fix_command",
            request_payload=payload,
            callback=apply,
        )

    def create_fix_revision(
        self,
        *,
        review_ref: str,
        expected_review_revision: int,
        expected_draft_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        payload = {
            "review_ref": review_ref,
            "expected_review_revision": expected_review_revision,
            "expected_draft_revision": expected_draft_revision,
        }

        def create(connection: sqlite3.Connection) -> dict[str, Any]:
            review_id = self._review_id(connection, review_ref)
            review = self._review_row(connection, review_id)
            self._require_review_revision(
                connection,
                review,
                expected_review_revision,
            )
            if review["status"] != "in_progress" or review["active_fix_draft_id"] is None:
                self._invalid_review_action(connection, review_id)
            draft = connection.execute(
                "SELECT * FROM fix_drafts WHERE id = ?",
                (review["active_fix_draft_id"],),
            ).fetchone()
            if int(draft["draft_revision"]) != expected_draft_revision:
                raise AnnotationConflictError(
                    "fix_draft_revision_conflict",
                    "The Fix draft changed; refresh before retrying.",
                    current=self._review_projection(connection, review_id),
                )
            active = connection.execute(
                """
                SELECT r.run_ref
                FROM runtime_runs r
                JOIN runtime_run_review_links l ON l.run_id = r.id
                WHERE l.review_id = ? AND r.kind = 'fix'
                  AND r.status IN ('queued', 'running')
                ORDER BY r.id DESC LIMIT 1
                """,
                (review_id,),
            ).fetchone()
            if active is not None:
                raise AnnotationConflictError(
                    "fix_runtime_already_active",
                    "A Fix revision is already being generated.",
                    current=self._review_projection(connection, review_id),
                )
            next_row = connection.execute(
                """
                SELECT COALESCE(MAX(revision_number), 0) + 1 AS next_revision
                FROM fix_revisions WHERE review_id = ?
                """,
                (review_id,),
            ).fetchone()
            revision_ref = _new_ref("fix_revision")
            timestamp = _now()
            owner = connection.execute(
                """
                SELECT t.job_id
                FROM trajectory_review_tasks r
                JOIN trajectory_revisions t ON t.id = r.trajectory_revision_id
                WHERE r.id = ?
                """,
                (review_id,),
            ).fetchone()
            run_ref = self._enqueue_run(
                connection,
                job_id=int(owner["job_id"]),
                kind="fix",
            )
            run = connection.execute(
                "SELECT id FROM runtime_runs WHERE run_ref = ?",
                (run_ref,),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO runtime_run_review_links (
                    run_id, review_id, fix_draft_id, source_draft_revision,
                    planned_revision_ref, planned_revision_number, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(run["id"]),
                    review_id,
                    int(draft["id"]),
                    int(draft["draft_revision"]),
                    revision_ref,
                    int(next_row["next_revision"]),
                    timestamp,
                ),
            )
            connection.execute(
                """
                UPDATE trajectory_review_tasks
                SET state_revision = state_revision + 1,
                    fix_failure_code = NULL,
                    fix_failure_message = NULL,
                    fix_failure_ref = NULL,
                    fix_failure_retryable = 0,
                    updated_at = ?
                WHERE id = ?
                """,
                (timestamp, review_id),
            )
            result = self._review_projection(connection, review_id)
            return result

        return self.mutate(
            idempotency_key=idempotency_key,
            operation="create_fix_revision",
            request_payload=payload,
            callback=create,
        )

    def fix_run_inputs(self, run_id: int) -> dict[str, Any]:
        """Return immutable, Store-bound inputs for a claimed Fix run."""

        with self._connect() as connection:
            run = self._running_run(connection, run_id)
            if run["kind"] != "fix":
                raise RuntimeError("Fix input belongs to another run kind")
            row = connection.execute(
                """
                SELECT l.planned_revision_ref, l.planned_revision_number,
                       l.source_draft_revision, l.fix_draft_id,
                       r.review_ref, r.status AS review_status,
                       r.state_revision AS review_revision,
                       d.draft_revision, d.state_json AS draft_state_json,
                       d.content_sha256 AS draft_sha256,
                       t.revision_ref AS trajectory_revision_ref,
                       t.private_artifact_path AS base_artifact_path,
                       t.private_compatibility_path,
                       t.artifact_sha256 AS base_artifact_sha256,
                       t.private_state_json AS trajectory_state_json,
                       t.artifact_manifest_ref,
                       c.snapshot_ref AS calibration_snapshot_ref,
                       c.private_snapshot_dir,
                       c.files_json AS calibration_files_json,
                       c.content_sha256 AS calibration_snapshot_sha256,
                       j.job_ref, j.dataset_date,
                       s.segment_ref, s.source_clip, s.private_segment_key
                FROM runtime_run_review_links l
                JOIN trajectory_review_tasks r ON r.id = l.review_id
                JOIN fix_drafts d ON d.id = l.fix_draft_id
                JOIN trajectory_revisions t
                  ON t.id = r.trajectory_revision_id
                JOIN fix_calibration_snapshots c
                  ON c.id = d.calibration_snapshot_id
                JOIN annotation_jobs j ON j.id = t.job_id
                JOIN annotation_segments s ON s.id = t.segment_id
                WHERE l.run_id = ?
                """,
                (run_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError("Fix run lacks its immutable review binding")
            if (
                row["review_status"] != "in_progress"
                or int(row["draft_revision"])
                != int(row["source_draft_revision"])
            ):
                raise RuntimeError("Fix run no longer owns its frozen draft")
            draft_state = json.loads(row["draft_state_json"])
            trajectory_state = json.loads(row["trajectory_state_json"])
            if (
                not isinstance(draft_state, dict)
                or _payload_hash(draft_state) != row["draft_sha256"]
                or not isinstance(trajectory_state, dict)
            ):
                raise RuntimeError("Fix run state changed after it was bound")
            commands = draft_state.get("commands")
            target_bindings = trajectory_state.get("target_bindings")
            if not isinstance(commands, list) or not isinstance(
                target_bindings,
                dict,
            ):
                raise RuntimeError("Fix run state lacks a command or target ledger")
            manifest = connection.execute(
                """
                SELECT manifest_json, content_sha256
                FROM artifact_manifests
                WHERE manifest_ref = ? AND stage = 'postprocessing'
                """,
                (row["artifact_manifest_ref"],),
            ).fetchone()
            if manifest is None:
                raise RuntimeError("Fix run lacks a postprocessing attestation")
            manifest_json = json.loads(manifest["manifest_json"])
            if (
                not isinstance(manifest_json, dict)
                or _payload_hash(manifest_json) != manifest["content_sha256"]
            ):
                raise RuntimeError("Fix base attestation changed")
            runtime_manifest_sha256 = manifest_json.get(
                "runtime_manifest_sha256",
            )
            if (
                not isinstance(runtime_manifest_sha256, str)
                or len(runtime_manifest_sha256) != 64
            ):
                raise RuntimeError("Fix base attestation lacks a Runtime hash")
            return {
                "run_ref": str(run["run_ref"]),
                "attempt": int(run["attempt"]),
                "job_ref": str(row["job_ref"]),
                "dataset_date": str(row["dataset_date"]),
                "review_ref": str(row["review_ref"]),
                "review_revision": int(row["review_revision"]),
                "planned_revision_ref": str(
                    row["planned_revision_ref"],
                ),
                "planned_revision_number": int(
                    row["planned_revision_number"],
                ),
                "source_draft_revision": int(
                    row["source_draft_revision"],
                ),
                "draft_state": draft_state,
                "draft_sha256": str(row["draft_sha256"]),
                "commands": commands,
                "trajectory_revision_ref": str(
                    row["trajectory_revision_ref"],
                ),
                "base_artifact_path": str(row["base_artifact_path"]),
                "base_artifact_sha256": str(
                    row["base_artifact_sha256"],
                ),
                "private_compatibility_path": str(
                    row["private_compatibility_path"],
                ),
                "target_bindings": target_bindings,
                "calibration_snapshot_ref": str(
                    row["calibration_snapshot_ref"],
                ),
                "calibration_snapshot_dir": str(
                    row["private_snapshot_dir"],
                ),
                "calibration_snapshot_files": json.loads(
                    row["calibration_files_json"],
                ),
                "calibration_snapshot_sha256": str(
                    row["calibration_snapshot_sha256"],
                ),
                "runtime_manifest_sha256": runtime_manifest_sha256,
                "segment_ref": str(row["segment_ref"]),
                "source_clip": str(row["source_clip"]),
                "private_segment_key": str(row["private_segment_key"]),
            }

    def complete_fix_run(
        self,
        *,
        run_id: int,
        candidate_segment_root: str,
        candidate_tree_sha256: str,
        fix_trajectory_sha256: str,
        manifest: dict[str, Any],
    ) -> dict[str, Any]:
        """Atomically freeze a generated FixRevision and close its run."""

        with self._write() as connection:
            run = self._running_run(connection, run_id)
            if run["kind"] != "fix":
                raise RuntimeError("Fix completion belongs to another run kind")
            link = connection.execute(
                """
                SELECT l.*, r.review_ref, r.status AS review_status,
                       r.state_revision AS review_revision,
                       d.draft_revision, d.state_json, d.content_sha256,
                       d.calibration_snapshot_id, d.base_trajectory_revision_id
                FROM runtime_run_review_links l
                JOIN trajectory_review_tasks r ON r.id = l.review_id
                JOIN fix_drafts d ON d.id = l.fix_draft_id
                WHERE l.run_id = ?
                """,
                (run_id,),
            ).fetchone()
            if link is None:
                raise RuntimeError("Fix completion lacks its review binding")
            if (
                link["review_status"] != "in_progress"
                or int(link["draft_revision"])
                != int(link["source_draft_revision"])
            ):
                raise RuntimeError("Fix completion no longer owns its draft")
            root = Path(candidate_segment_root)
            if (
                not root.is_absolute()
                or not _valid_sha256(candidate_tree_sha256)
                or not _valid_sha256(fix_trajectory_sha256)
            ):
                raise RuntimeError("Fix completion artifact identity is invalid")
            self._require_runtime_step_ledger(
                connection,
                run_id,
                manifest.get("command_steps"),
            )
            manifest_ref = self._insert_manifest(
                connection,
                run,
                "fix",
                manifest,
            )
            timestamp = _now()
            connection.execute(
                """
                INSERT INTO fix_revisions (
                    revision_ref, review_id, revision_number,
                    calibration_snapshot_id, base_trajectory_revision_id,
                    source_draft_revision, state_json, content_sha256,
                    private_artifact_path, artifact_sha256,
                    artifact_manifest_ref, runtime_run_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    link["planned_revision_ref"],
                    link["review_id"],
                    link["planned_revision_number"],
                    link["calibration_snapshot_id"],
                    link["base_trajectory_revision_id"],
                    link["source_draft_revision"],
                    link["state_json"],
                    fix_trajectory_sha256,
                    candidate_segment_root,
                    candidate_tree_sha256,
                    manifest_ref,
                    run_id,
                    timestamp,
                ),
            )
            connection.execute(
                """
                UPDATE trajectory_review_tasks
                SET state_revision = state_revision + 1,
                    fix_failure_code = NULL,
                    fix_failure_message = NULL,
                    fix_failure_ref = NULL,
                    fix_failure_retryable = 0,
                    updated_at = ?
                WHERE id = ?
                """,
                (timestamp, link["review_id"]),
            )
            handoff_payload = {
                "review_ref": str(link["review_ref"]),
                "fix_revision_ref": str(
                    link["planned_revision_ref"],
                ),
            }
            connection.execute(
                """
                INSERT INTO workflow_handoffs (
                    handoff_ref, job_id, review_id, kind, payload_json,
                    content_sha256, created_at
                ) VALUES (?, ?, ?, 'fix_revision_submitted', ?, ?, ?)
                """,
                (
                    _new_ref("handoff"),
                    run["job_id"],
                    link["review_id"],
                    _canonical_json(handoff_payload),
                    _payload_hash(handoff_payload),
                    timestamp,
                ),
            )
            self._finish_run(connection, run_id, "succeeded")
            result = self._review_projection(
                connection,
                int(link["review_id"]),
            )
            result["submitted_fix_revision_ref"] = str(
                link["planned_revision_ref"],
            )
            return result

    def decide_review(
        self,
        *,
        operation: str,
        review_ref: str,
        expected_review_revision: int,
        reason: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        if operation not in {"return", "discard"}:
            raise RuntimeError("unsupported review decision")
        payload = {
            "review_ref": review_ref,
            "expected_review_revision": expected_review_revision,
            "reason": reason,
        }

        def decide(connection: sqlite3.Connection) -> dict[str, Any]:
            review_id = self._review_id(connection, review_ref)
            review = self._review_row(connection, review_id)
            self._require_review_revision(
                connection,
                review,
                expected_review_revision,
            )
            active_fix_run = connection.execute(
                """
                SELECT 1
                FROM runtime_run_review_links l
                JOIN runtime_runs r ON r.id = l.run_id
                WHERE l.review_id = ?
                  AND r.status IN ('queued', 'running')
                LIMIT 1
                """,
                (review_id,),
            ).fetchone()
            if active_fix_run is not None:
                raise AnnotationConflictError(
                    "fix_runtime_already_active",
                    "The review is frozen while a Fix revision is generated.",
                    current=self._review_projection(connection, review_id),
                )
            if operation == "return":
                if review["status"] != "in_progress":
                    self._invalid_review_action(connection, review_id)
                target_status = "returned"
                decision = "returned"
            else:
                if review["status"] not in {"pending", "in_progress", "returned"}:
                    self._invalid_review_action(connection, review_id)
                target_status = "discarded"
                decision = "discarded"
            timestamp = _now()
            connection.execute(
                """
                INSERT INTO review_decisions (
                    decision_ref, review_id, decision, reason, actor_kind,
                    deployment_instance, created_at
                ) VALUES (?, ?, ?, ?, 'manual_web', ?, ?)
                """,
                (
                    _new_ref("review_decision"),
                    review_id,
                    decision,
                    reason,
                    self.deployment_instance,
                    timestamp,
                ),
            )
            connection.execute(
                """
                UPDATE trajectory_review_tasks
                SET status = ?, state_revision = state_revision + 1,
                    updated_at = ?
                WHERE id = ?
                """,
                (target_status, timestamp, review_id),
            )
            owner = connection.execute(
                """
                SELECT t.job_id
                FROM trajectory_review_tasks r
                JOIN trajectory_revisions t ON t.id = r.trajectory_revision_id
                WHERE r.id = ?
                """,
                (review_id,),
            ).fetchone()
            handoff_payload = {
                "review_ref": review_ref,
                "decision": decision,
            }
            connection.execute(
                """
                INSERT INTO workflow_handoffs (
                    handoff_ref, job_id, review_id, kind, payload_json,
                    content_sha256, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _new_ref("handoff"),
                    owner["job_id"],
                    review_id,
                    (
                        "review_returned"
                        if decision == "returned"
                        else "review_completed"
                    ),
                    _canonical_json(handoff_payload),
                    _payload_hash(handoff_payload),
                    timestamp,
                ),
            )
            return self._review_projection(connection, review_id)

        return self.mutate(
            idempotency_key=idempotency_key,
            operation=f"{operation}_review",
            request_payload=payload,
            callback=decide,
        )

    def resume_returned_review(
        self,
        *,
        review_ref: str,
        expected_review_revision: int,
        calibration_profile_ref: str,
        calibration_content_sha256: str,
        difference_reason: str | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        payload = {
            "review_ref": review_ref,
            "expected_review_revision": expected_review_revision,
            "calibration_profile_ref": calibration_profile_ref,
            "calibration_content_sha256": calibration_content_sha256,
            "difference_reason": difference_reason,
        }

        def resume(connection: sqlite3.Connection) -> dict[str, Any]:
            review_id = self._review_id(connection, review_ref)
            review = self._review_row(connection, review_id)
            self._require_review_revision(
                connection,
                review,
                expected_review_revision,
            )
            if review["status"] != "returned" or review["active_fix_draft_id"] is None:
                self._invalid_review_action(connection, review_id)
            connection.execute(
                """
                UPDATE trajectory_review_tasks
                SET status = 'in_progress',
                    state_revision = state_revision + 1,
                    updated_at = ?
                WHERE id = ?
                """,
                (_now(), review_id),
            )
            return self._review_projection(connection, review_id)

        return self.mutate(
            idempotency_key=idempotency_key,
            operation="create_fix_session",
            request_payload=payload,
            callback=resume,
        )

    def approve_fix_revision(
        self,
        *,
        review_ref: str,
        expected_review_revision: int,
        fix_revision_ref: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        payload = {
            "review_ref": review_ref,
            "expected_review_revision": expected_review_revision,
            "fix_revision_ref": fix_revision_ref,
        }

        def approve(connection: sqlite3.Connection) -> dict[str, Any]:
            review_id = self._review_id(connection, review_ref)
            review = self._review_row(connection, review_id)
            self._require_review_revision(
                connection,
                review,
                expected_review_revision,
            )
            active_fix_run = connection.execute(
                """
                SELECT 1
                FROM runtime_run_review_links l
                JOIN runtime_runs r ON r.id = l.run_id
                WHERE l.review_id = ?
                  AND r.status IN ('queued', 'running')
                LIMIT 1
                """,
                (review_id,),
            ).fetchone()
            if active_fix_run is not None:
                raise AnnotationConflictError(
                    "fix_runtime_already_active",
                    "The review is frozen while a Fix revision is generated.",
                    current=self._review_projection(connection, review_id),
                )
            if review["status"] not in {"in_progress", "returned"}:
                self._invalid_review_action(connection, review_id)
            fix_revision = connection.execute(
                """
                SELECT f.*, t.job_id
                FROM fix_revisions f
                JOIN trajectory_revisions t
                  ON t.id = f.base_trajectory_revision_id
                WHERE f.review_id = ? AND f.revision_ref = ?
                """,
                (review_id, fix_revision_ref),
            ).fetchone()
            if fix_revision is None:
                raise AnnotationNotFoundError("Fix revision not found")
            existing_publication = connection.execute(
                """
                SELECT 1 FROM compatibility_publications
                WHERE review_id = ? AND status IN ('queued', 'running')
                LIMIT 1
                """,
                (review_id,),
            ).fetchone()
            if existing_publication is not None:
                raise AnnotationConflictError(
                    "compatibility_publication_active",
                    "The approved Fix revision is already queued for publication.",
                    current=self._review_projection(connection, review_id),
                )
            timestamp = _now()
            connection.execute(
                """
                INSERT INTO review_decisions (
                    decision_ref, review_id, decision, fix_revision_id,
                    actor_kind, deployment_instance, created_at
                ) VALUES (?, ?, 'approved', ?, 'manual_web', ?, ?)
                """,
                (
                    _new_ref("review_decision"),
                    review_id,
                    fix_revision["id"],
                    self.deployment_instance,
                    timestamp,
                ),
            )
            connection.execute(
                """
                UPDATE trajectory_review_tasks
                SET status = 'approved', approved_fix_revision_id = ?,
                    state_revision = state_revision + 1, updated_at = ?
                WHERE id = ?
                """,
                (
                    fix_revision["id"],
                    timestamp,
                    review_id,
                ),
            )
            self._enqueue_compatibility_publication(
                connection,
                job_id=int(fix_revision["job_id"]),
                review_id=review_id,
                fix_revision_id=int(fix_revision["id"]),
                created_at=timestamp,
            )
            return self._review_projection(connection, review_id)

        return self.mutate(
            idempotency_key=idempotency_key,
            operation="approve_review",
            request_payload=payload,
            callback=approve,
        )

    def retry_compatibility_publication(
        self,
        *,
        review_ref: str,
        expected_review_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        payload = {
            "review_ref": review_ref,
            "expected_review_revision": expected_review_revision,
        }

        def retry(connection: sqlite3.Connection) -> dict[str, Any]:
            review_id = self._review_id(connection, review_ref)
            review = self._review_row(connection, review_id)
            self._require_review_revision(
                connection,
                review,
                expected_review_revision,
            )
            if (
                review["status"] != "approved"
                or review["approved_fix_revision_id"] is None
            ):
                self._invalid_review_action(connection, review_id)
            latest = connection.execute(
                """
                SELECT p.*, t.job_id
                FROM compatibility_publications p
                JOIN fix_revisions f ON f.id = p.fix_revision_id
                JOIN trajectory_revisions t
                  ON t.id = f.base_trajectory_revision_id
                WHERE p.review_id = ?
                ORDER BY p.attempt DESC
                LIMIT 1
                """,
                (review_id,),
            ).fetchone()
            if (
                latest is None
                or latest["status"] != "failed"
                or int(latest["fix_revision_id"])
                != int(review["approved_fix_revision_id"])
            ):
                raise AnnotationConflictError(
                    "publication_retry_unavailable",
                    "There is no failed compatibility publication to retry.",
                    current=self._review_projection(connection, review_id),
                )
            timestamp = _now()
            self._enqueue_compatibility_publication(
                connection,
                job_id=int(latest["job_id"]),
                review_id=review_id,
                fix_revision_id=int(latest["fix_revision_id"]),
                created_at=timestamp,
            )
            connection.execute(
                """
                UPDATE trajectory_review_tasks
                SET state_revision = state_revision + 1, updated_at = ?
                WHERE id = ?
                """,
                (timestamp, review_id),
            )
            return self._review_projection(connection, review_id)

        return self.mutate(
            idempotency_key=idempotency_key,
            operation="retry_publication",
            request_payload=payload,
            callback=retry,
        )

    @staticmethod
    def _resolve_exact_scope_job(
        connection: sqlite3.Connection,
        *,
        dataset_date: str,
        source_clips: list[str],
    ) -> tuple[int, list[str]] | None:
        placeholders = ",".join("?" for _ in source_clips)
        rows = connection.execute(
            f"""
            SELECT l.source_clip, l.job_id
            FROM annotation_source_leases l
            WHERE l.dataset_date = ?
              AND l.source_clip IN ({placeholders})
            ORDER BY l.source_clip
            """,
            (dataset_date, *source_clips),
        ).fetchall()
        if not rows:
            return None
        job_ids = {int(row["job_id"]) for row in rows}
        if len(job_ids) != 1:
            raise AnnotationConflictError(
                "annotation_scope_split",
                "The selected clips belong to different annotation jobs.",
            )
        job_id = job_ids.pop()
        authoritative_scope = [
            str(row["source_clip"])
            for row in connection.execute(
                """
                SELECT source_clip
                FROM annotation_job_source_clips
                WHERE job_id = ?
                ORDER BY ordinal
                """,
                (job_id,),
            ).fetchall()
        ]
        requested_scope = [str(value) for value in source_clips]
        leased_scope = [str(row["source_clip"]) for row in rows]
        if (
            len(requested_scope) != len(set(requested_scope))
            or len(leased_scope) != len(requested_scope)
            or set(leased_scope) != set(requested_scope)
            or len(authoritative_scope) != len(requested_scope)
            or set(authoritative_scope) != set(requested_scope)
        ):
            raise AnnotationConflictError(
                "annotation_scope_mismatch",
                "The selected clips do not exactly match the annotation job scope.",
            )
        job = connection.execute(
            "SELECT dataset_date FROM annotation_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        if job is None or str(job["dataset_date"]) != dataset_date:
            raise RuntimeError(
                "annotation source leases do not match their owning job"
            )
        return job_id, authoritative_scope

    def get_processing_facts(
        self,
        *,
        dataset_date: str,
        source_clips: list[str],
    ) -> dict[str, Any]:
        if not source_clips:
            raise AnnotationValidationError(
                "empty_annotation_scope",
                "At least one source clip is required.",
            )
        with self._connect() as connection:
            resolved = self._resolve_exact_scope_job(
                connection,
                dataset_date=dataset_date,
                source_clips=source_clips,
            )
            if resolved is None:
                return {
                    "dataset_date": dataset_date,
                    "source_clips": list(source_clips),
                    "exists": False,
                }
            job_id, authoritative_scope = resolved
            projection = self._job_projection(
                connection,
                job_id,
                include_segments=False,
            )
            review_counts = {
                status: 0
                for status in (
                    "pending",
                    "in_progress",
                    "returned",
                    "approved",
                    "discarded",
                )
            }
            for row in connection.execute(
                """
                SELECT r.status, COUNT(*) AS count
                FROM trajectory_review_tasks r
                JOIN trajectory_revisions t ON t.id = r.trajectory_revision_id
                WHERE t.job_id = ?
                GROUP BY r.status
                """,
                (job_id,),
            ).fetchall():
                review_counts[str(row["status"])] = int(row["count"])
            return {
                "dataset_date": dataset_date,
                "source_clips": authoritative_scope,
                "exists": True,
                "job_status": projection["status"],
                "segment_counts": projection["counts"],
                "ready_for_postprocessing": projection["status"] == "tracked",
                "review_counts": review_counts,
            }

    def resolve_scope_binding(
        self,
        *,
        dataset_date: str,
        source_clips: list[str],
    ) -> dict[str, Any]:
        """Resolve private refs for an in-process gateway, never for LLM output."""

        if not source_clips:
            raise AnnotationValidationError(
                "empty_annotation_scope",
                "At least one source clip is required.",
            )
        with self._connect() as connection:
            resolved = self._resolve_exact_scope_job(
                connection,
                dataset_date=dataset_date,
                source_clips=source_clips,
            )
            if resolved is None:
                raise AnnotationNotFoundError("annotation scope not found")
            job_id, _authoritative_scope = resolved
            job = self._job_row(connection, job_id)
            reviews = [
                str(row["review_ref"])
                for row in connection.execute(
                    """
                    SELECT r.review_ref
                    FROM trajectory_review_tasks r
                    JOIN trajectory_revisions t
                        ON t.id = r.trajectory_revision_id
                    JOIN annotation_segments s ON s.id = t.segment_id
                    WHERE t.job_id = ?
                    ORDER BY s.ordinal
                    """,
                    (job["id"],),
                ).fetchall()
            ]
            return {
                "job_ref": str(job["job_ref"]),
                "job_status": str(job["status"]),
                "job_revision": int(job["state_revision"]),
                "review_refs": reviews,
            }

    def resolve_navigation_task_binding(
        self,
        *,
        navigation_task_ref: str,
        link_kind: str,
    ) -> dict[str, Any]:
        """Resolve the private Annotation scope frozen for one Navigation task."""

        if link_kind not in {"processing", "trajectory_fix"}:
            raise AnnotationValidationError(
                "invalid_task_link_kind",
                "The annotation task link kind is unsupported.",
            )
        if (
            not isinstance(navigation_task_ref, str)
            or not navigation_task_ref
            or len(navigation_task_ref) > 200
            or "\n" in navigation_task_ref
            or "\r" in navigation_task_ref
        ):
            raise AnnotationValidationError(
                "invalid_navigation_task_ref",
                "The navigation task reference is invalid.",
            )
        with self._connect() as connection:
            links = connection.execute(
                """
                SELECT l.job_id, l.review_id
                FROM annotation_task_links l
                WHERE l.navigation_task_ref = ? AND l.link_kind = ?
                ORDER BY l.id
                """,
                (navigation_task_ref, link_kind),
            ).fetchall()
            if not links:
                raise AnnotationNotFoundError(
                    "annotation navigation task binding not found"
                )
            job_ids = {int(row["job_id"]) for row in links}
            if len(job_ids) != 1:
                raise RuntimeError(
                    "one Navigation task is bound to multiple Annotation jobs"
                )
            job_id = job_ids.pop()
            job = self._job_row(connection, job_id)
            source_clips = [
                str(row["source_clip"])
                for row in connection.execute(
                    """
                    SELECT source_clip
                    FROM annotation_job_source_clips
                    WHERE job_id = ?
                    ORDER BY ordinal
                    """,
                    (job_id,),
                ).fetchall()
            ]
            review_refs = [
                str(row["review_ref"])
                for row in connection.execute(
                    """
                    SELECT r.review_ref
                    FROM trajectory_review_tasks r
                    JOIN trajectory_revisions t
                      ON t.id = r.trajectory_revision_id
                    JOIN annotation_segments s ON s.id = t.segment_id
                    WHERE t.job_id = ?
                    ORDER BY s.ordinal
                    """,
                    (job_id,),
                ).fetchall()
            ]
            return {
                "job_ref": str(job["job_ref"]),
                "job_status": str(job["status"]),
                "job_revision": int(job["state_revision"]),
                "dataset_date": str(job["dataset_date"]),
                "source_clips": source_clips,
                "review_refs": review_refs,
            }

    def resolve_navigation_review_outcome(
        self,
        *,
        navigation_task_ref: str,
    ) -> dict[str, Any]:
        """Return a ref-free aggregate for one linked Navigation Fix task."""

        if (
            not isinstance(navigation_task_ref, str)
            or not navigation_task_ref
            or len(navigation_task_ref) > 200
            or "\n" in navigation_task_ref
            or "\r" in navigation_task_ref
        ):
            raise AnnotationValidationError(
                "invalid_navigation_task_ref",
                "The navigation task reference is invalid.",
            )
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT r.status,
                       (
                           SELECT p.status
                           FROM compatibility_publications p
                           WHERE p.review_id = r.id
                             AND p.fix_revision_id
                                 = r.approved_fix_revision_id
                           ORDER BY p.attempt DESC
                           LIMIT 1
                       ) AS approved_publication_status
                FROM annotation_task_links l
                JOIN trajectory_review_tasks r ON r.id = l.review_id
                WHERE l.navigation_task_ref = ?
                  AND l.link_kind = 'trajectory_fix'
                ORDER BY l.id
                """,
                (navigation_task_ref,),
            ).fetchall()
        if not rows:
            raise AnnotationNotFoundError(
                "annotation trajectory review binding not found"
            )
        counts = {
            "pending": 0,
            "in_progress": 0,
            "returned": 0,
            "approved": 0,
            "discarded": 0,
        }
        for row in rows:
            status = str(row["status"])
            if status not in counts:
                raise RuntimeError("unknown trajectory review status")
            counts[status] += 1
        published_approved = sum(
            1
            for row in rows
            if row["status"] == "approved"
            and row["approved_publication_status"] == "succeeded"
        )
        all_terminal = published_approved + counts["discarded"] == len(rows)
        overall_status = (
            "completed"
            if all_terminal
            else "returned"
            if counts["returned"]
            else "in_progress"
            if counts["in_progress"] or counts["approved"]
            else "pending"
        )
        return {
            "status": overall_status,
            "review_count": len(rows),
            "counts": counts,
            "all_terminal": all_terminal,
        }

    def create_workflow_handoff(
        self,
        *,
        job_ref: str,
        review_ref: str | None,
        kind: str,
        payload: dict[str, Any],
        idempotency_key: str,
        actor_kind: str = "system_worker",
    ) -> dict[str, Any]:
        allowed_kinds = {
            "initial_annotation_ready",
            "initial_annotation_submitted",
            "tracking_completed",
            "postprocessing_completed",
            "postprocessing_failed",
            "fix_ready",
            "fix_revision_submitted",
            "review_returned",
            "review_completed",
        }
        if kind not in allowed_kinds:
            raise AnnotationValidationError(
                "invalid_handoff_kind",
                "The workflow handoff kind is unsupported.",
            )
        _require_safe_handoff_payload(payload)
        request_payload = {
            "job_ref": job_ref,
            "review_ref": review_ref,
            "kind": kind,
            "payload": payload,
        }

        def create(connection: sqlite3.Connection) -> dict[str, Any]:
            job_id = self._job_id(connection, job_ref)
            review_id = (
                self._review_id(connection, review_ref)
                if review_ref is not None
                else None
            )
            if review_id is not None:
                owner = connection.execute(
                    """
                    SELECT t.job_id
                    FROM trajectory_review_tasks r
                    JOIN trajectory_revisions t
                        ON t.id = r.trajectory_revision_id
                    WHERE r.id = ?
                    """,
                    (review_id,),
                ).fetchone()
                if int(owner["job_id"]) != job_id:
                    raise AnnotationValidationError(
                        "handoff_scope_mismatch",
                        "The workflow handoff review is outside the annotation job.",
                    )
            timestamp = _now()
            handoff_ref = _new_ref("handoff")
            connection.execute(
                """
                INSERT INTO workflow_handoffs (
                    handoff_ref, job_id, review_id, kind, payload_json,
                    content_sha256, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    handoff_ref,
                    job_id,
                    review_id,
                    kind,
                    _canonical_json(payload),
                    _payload_hash(payload),
                    timestamp,
                ),
            )
            return {
                "handoff_ref": handoff_ref,
                "kind": kind,
                "created_at": timestamp,
            }

        return self.mutate(
            idempotency_key=idempotency_key,
            operation="create_workflow_handoff",
            request_payload=request_payload,
            callback=create,
            actor_kind=actor_kind,
        )

    def claim_workflow_handoff_delivery(
        self,
        *,
        worker_id: str,
        lease_seconds: int = 120,
    ) -> dict[str, Any] | None:
        """Claim one system-owned Annotation→Navigation handoff."""

        if not worker_id or len(worker_id) > 200:
            raise ValueError("workflow handoff worker_id is invalid")
        timestamp = _now()
        expires_at = (
            datetime.now(UTC) + timedelta(seconds=lease_seconds)
        ).isoformat(timespec="milliseconds")
        with self._write() as connection:
            row = connection.execute(
                """
                SELECT h.*, j.job_ref, j.status AS job_status,
                       j.state_revision AS job_revision,
                       l.navigation_task_ref
                FROM workflow_handoffs h
                JOIN annotation_jobs j ON j.id = h.job_id
                LEFT JOIN workflow_handoff_processing_links processing_link
                  ON processing_link.handoff_id = h.id
                JOIN annotation_task_links l
                  ON (
                      (
                          h.kind IN (
                              'initial_annotation_submitted',
                              'tracking_completed',
                              'postprocessing_completed',
                              'postprocessing_failed'
                          )
                          AND l.id = processing_link.link_id
                      )
                      OR
                      (
                          h.kind IN (
                              'fix_revision_submitted',
                              'review_returned',
                              'review_completed'
                          )
                          AND l.link_kind = 'trajectory_fix'
                          AND l.review_id = h.review_id
                      )
                 )
                LEFT JOIN workflow_handoff_deliveries d
                  ON d.handoff_id = h.id
                WHERE h.kind IN (
                    'initial_annotation_submitted',
                    'tracking_completed',
                    'postprocessing_completed',
                    'postprocessing_failed',
                    'fix_revision_submitted',
                    'review_returned',
                    'review_completed'
                )
                  AND (
                    d.handoff_id IS NULL
                    OR d.status = 'retry'
                    OR (
                        d.status = 'running'
                        AND d.lease_expires_at IS NOT NULL
                        AND d.lease_expires_at <= ?
                    )
                  )
                ORDER BY h.id
                LIMIT 1
                """,
                (timestamp,),
            ).fetchone()
            if row is None:
                return None
            existing = connection.execute(
                """
                SELECT attempts FROM workflow_handoff_deliveries
                WHERE handoff_id = ?
                """,
                (row["id"],),
            ).fetchone()
            attempts = int(existing["attempts"]) + 1 if existing is not None else 1
            connection.execute(
                """
                INSERT INTO workflow_handoff_deliveries (
                    handoff_id, status, worker_id, attempts,
                    lease_expires_at, last_error, updated_at
                ) VALUES (?, 'running', ?, ?, ?, NULL, ?)
                ON CONFLICT(handoff_id) DO UPDATE SET
                    status = 'running',
                    worker_id = excluded.worker_id,
                    attempts = excluded.attempts,
                    lease_expires_at = excluded.lease_expires_at,
                    last_error = NULL,
                    updated_at = excluded.updated_at
                """,
                (
                    row["id"],
                    worker_id,
                    attempts,
                    expires_at,
                    timestamp,
                ),
            )
            return {
                "handoff_id": int(row["id"]),
                "handoff_ref": str(row["handoff_ref"]),
                "kind": str(row["kind"]),
                "job_ref": str(row["job_ref"]),
                "job_status": str(row["job_status"]),
                "job_revision": int(row["job_revision"]),
                "navigation_task_ref": str(row["navigation_task_ref"]),
                "payload": json.loads(row["payload_json"]),
                "attempt": attempts,
            }

    def complete_workflow_handoff_delivery(
        self,
        *,
        handoff_id: int,
        worker_id: str,
        success: bool,
        error: str | None = None,
    ) -> None:
        """Settle a claimed handoff; failed delivery is retried after restart."""

        with self._write() as connection:
            row = connection.execute(
                """
                SELECT status, worker_id
                FROM workflow_handoff_deliveries
                WHERE handoff_id = ?
                """,
                (handoff_id,),
            ).fetchone()
            if row is None:
                raise KeyError(handoff_id)
            if row["status"] == "delivered" and success:
                return
            if row["status"] != "running" or row["worker_id"] != worker_id:
                raise AnnotationConflictError(
                    "workflow_handoff_lease_lost",
                    "The workflow handoff delivery lease changed.",
                )
            connection.execute(
                """
                UPDATE workflow_handoff_deliveries
                SET status = ?, lease_expires_at = NULL,
                    last_error = ?, updated_at = ?
                WHERE handoff_id = ? AND worker_id = ?
                """,
                (
                    "delivered" if success else "retry",
                    None if success else str(error or "delivery_failed")[:200],
                    _now(),
                    handoff_id,
                    worker_id,
                ),
            )

    @staticmethod
    def _assign_processing_authority(
        connection: sqlite3.Connection,
        *,
        job_id: int,
        link_id: int,
    ) -> None:
        link = connection.execute(
            """
            SELECT job_id, link_kind
            FROM annotation_task_links
            WHERE id = ?
            """,
            (link_id,),
        ).fetchone()
        if (
            link is None
            or int(link["job_id"]) != job_id
            or str(link["link_kind"]) != "processing"
        ):
            raise RuntimeError("processing authority link is invalid")
        current = connection.execute(
            """
            SELECT link_id
            FROM annotation_processing_authorities
            WHERE job_id = ?
            """,
            (job_id,),
        ).fetchone()
        if current is None:
            connection.execute(
                """
                INSERT INTO annotation_processing_authorities (
                    job_id, link_id, revision, updated_at
                ) VALUES (?, ?, 0, ?)
                """,
                (job_id, link_id, _now()),
            )
        elif int(current["link_id"]) != link_id:
            active_run = connection.execute(
                """
                SELECT 1
                FROM runtime_runs
                WHERE job_id = ?
                  AND status IN ('queued', 'running')
                LIMIT 1
                """,
                (job_id,),
            ).fetchone()
            if active_run is not None:
                raise AnnotationConflictError(
                    "annotation_processing_active_attempt_conflict",
                    "This annotation workflow already has active processing.",
                )
            connection.execute(
                """
                UPDATE annotation_processing_authorities
                SET link_id = ?, revision = revision + 1, updated_at = ?
                WHERE job_id = ?
                """,
                (link_id, _now(), job_id),
            )
        connection.execute(
            """
            INSERT OR IGNORE INTO workflow_handoff_processing_links (
                handoff_id, link_id, created_at
            )
            SELECT h.id, ?, ?
            FROM workflow_handoffs h
            LEFT JOIN workflow_handoff_processing_links existing
              ON existing.handoff_id = h.id
            WHERE h.job_id = ?
              AND h.kind IN (
                  'initial_annotation_submitted',
                  'tracking_completed',
                  'postprocessing_completed',
                  'postprocessing_failed'
              )
              AND existing.handoff_id IS NULL
            """,
            (link_id, _now(), job_id),
        )

    def link_navigation_task(
        self,
        *,
        job_ref: str,
        review_ref: str | None,
        navigation_task_ref: str,
        parent_navigation_task_ref: str | None,
        link_kind: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        if link_kind not in {"processing", "trajectory_fix"}:
            raise AnnotationValidationError(
                "invalid_task_link_kind",
                "The annotation task link kind is unsupported.",
            )
        for value in (navigation_task_ref, parent_navigation_task_ref):
            if value is not None and (
                not value or len(value) > 200 or "\n" in value or "\r" in value
            ):
                raise AnnotationValidationError(
                    "invalid_navigation_task_ref",
                    "The navigation task reference is invalid.",
                )
        payload = {
            "job_ref": job_ref,
            "review_ref": review_ref,
            "navigation_task_ref": navigation_task_ref,
            "parent_navigation_task_ref": parent_navigation_task_ref,
            "link_kind": link_kind,
        }

        def create(connection: sqlite3.Connection) -> dict[str, Any]:
            job_id = self._job_id(connection, job_ref)
            review_id = (
                self._review_id(connection, review_ref)
                if review_ref is not None
                else None
            )
            if link_kind == "trajectory_fix" and review_id is None:
                raise AnnotationValidationError(
                    "review_link_required",
                    "A trajectory Fix task must link a review.",
                )
            if link_kind == "trajectory_fix" and parent_navigation_task_ref is None:
                raise AnnotationValidationError(
                    "parent_task_link_required",
                    "A trajectory Fix task must link its completed parent task.",
                )
            if link_kind == "processing" and review_id is not None:
                raise AnnotationValidationError(
                    "unexpected_review_link",
                    "A processing task cannot link an individual review.",
                )
            if review_id is not None:
                owner = connection.execute(
                    """
                    SELECT t.job_id
                    FROM trajectory_review_tasks r
                    JOIN trajectory_revisions t
                        ON t.id = r.trajectory_revision_id
                    WHERE r.id = ?
                    """,
                    (review_id,),
                ).fetchone()
                if int(owner["job_id"]) != job_id:
                    raise AnnotationValidationError(
                        "task_link_scope_mismatch",
                        "The review is outside the annotation task scope.",
                    )
            if link_kind == "processing":
                navigation_owner = connection.execute(
                    """
                    SELECT id, job_id
                    FROM annotation_task_links
                    WHERE navigation_task_ref = ?
                      AND link_kind = 'processing'
                    """,
                    (navigation_task_ref,),
                ).fetchone()
                if (
                    navigation_owner is not None
                    and int(navigation_owner["job_id"]) != job_id
                ):
                    raise AnnotationConflictError(
                        "annotation_processing_owner_conflict",
                        "This Navigation task already owns a different annotation job.",
                    )
                if navigation_owner is not None:
                    self._assign_processing_authority(
                        connection,
                        job_id=job_id,
                        link_id=int(navigation_owner["id"]),
                    )
                    return {"linked": True, "link_kind": link_kind}
            link_ref = _new_ref("annotation_task_link")
            try:
                inserted = connection.execute(
                    """
                    INSERT INTO annotation_task_links (
                        link_ref, job_id, review_id, navigation_task_ref,
                        parent_navigation_task_ref, link_kind, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        link_ref,
                        job_id,
                        review_id,
                        navigation_task_ref,
                        parent_navigation_task_ref,
                        link_kind,
                        _now(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                if link_kind == "processing":
                    owner_after_conflict = connection.execute(
                        """
                        SELECT id, job_id
                        FROM annotation_task_links
                        WHERE navigation_task_ref = ?
                          AND link_kind = 'processing'
                        """,
                        (navigation_task_ref,),
                    ).fetchone()
                    if owner_after_conflict is not None:
                        if int(owner_after_conflict["job_id"]) == job_id:
                            self._assign_processing_authority(
                                connection,
                                job_id=job_id,
                                link_id=int(owner_after_conflict["id"]),
                            )
                            return {"linked": True, "link_kind": link_kind}
                        raise AnnotationConflictError(
                            "annotation_processing_owner_conflict",
                            "This annotation job already has a different processing owner.",
                        ) from exc
                raise
            if link_kind == "processing":
                self._assign_processing_authority(
                    connection,
                    job_id=job_id,
                    link_id=int(inserted.lastrowid),
                )
            return {"linked": True, "link_kind": link_kind}

        return self.mutate(
            idempotency_key=idempotency_key,
            operation="link_navigation_task",
            request_payload=payload,
            callback=create,
            actor_kind="datapilot",
        )

    def save_draft(
        self,
        *,
        job_ref: str,
        segment_ref: str,
        expected_segment_revision: int,
        expected_draft_revision: int | None,
        targets: list[dict[str, Any]],
        idempotency_key: str,
    ) -> dict[str, Any]:
        payload = {
            "job_ref": job_ref,
            "segment_ref": segment_ref,
            "expected_segment_revision": expected_segment_revision,
            "expected_draft_revision": expected_draft_revision,
            "targets": targets,
        }

        def save(connection: sqlite3.Connection) -> dict[str, Any]:
            job_id, job = self._mutable_waiting_job(connection, job_ref)
            segment = self._segment_row(connection, job_id, segment_ref)
            self._require_segment_revision(segment, expected_segment_revision, connection)
            if segment["status"] not in {
                "pending_initial_annotation",
                "draft",
            }:
                raise AnnotationConflictError(
                    "segment_not_editable",
                    "This segment cannot be edited in its current state.",
                    current=self._segment_projection(connection, segment, include_draft=True),
                )
            actual_draft_revision = (
                int(segment["draft_revision"]) if int(segment["draft_revision"]) else None
            )
            if actual_draft_revision != expected_draft_revision:
                raise AnnotationConflictError(
                    "draft_revision_conflict",
                    "The annotation draft changed; refresh before saving.",
                    current=self._segment_projection(connection, segment, include_draft=True),
                )
            next_draft = (expected_draft_revision or 0) + 1
            targets_json = _canonical_json(targets)
            content_sha = hashlib.sha256(targets_json.encode("utf-8")).hexdigest()
            timestamp = _now()
            connection.execute(
                """
                INSERT INTO initial_annotation_drafts (
                    segment_id, draft_revision, targets_json, content_sha256, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(segment_id) DO UPDATE SET
                    draft_revision = excluded.draft_revision,
                    targets_json = excluded.targets_json,
                    content_sha256 = excluded.content_sha256,
                    updated_at = excluded.updated_at
                """,
                (segment["id"], next_draft, targets_json, content_sha, timestamp),
            )
            connection.execute(
                """
                UPDATE annotation_segments
                SET status = 'draft', state_revision = state_revision + 1,
                    draft_revision = ?, submitted_revision = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (next_draft, timestamp, segment["id"]),
            )
            self._touch_job(connection, int(job["id"]))
            self._record_action(connection, int(segment["id"]), "draft_saved", {})
            updated = self._segment_row(connection, job_id, segment_ref)
            return self._segment_projection(connection, updated, include_draft=True)

        return self.mutate(
            idempotency_key=idempotency_key,
            operation="save_draft",
            request_payload=payload,
            callback=save,
        )

    def submit_segment(
        self,
        *,
        job_ref: str,
        segment_ref: str,
        expected_segment_revision: int,
        expected_draft_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        payload = {
            "job_ref": job_ref,
            "segment_ref": segment_ref,
            "expected_segment_revision": expected_segment_revision,
            "expected_draft_revision": expected_draft_revision,
        }

        def submit(connection: sqlite3.Connection) -> dict[str, Any]:
            job_id, job = self._mutable_waiting_job(connection, job_ref)
            segment = self._segment_row(connection, job_id, segment_ref)
            self._require_segment_revision(segment, expected_segment_revision, connection)
            if int(segment["draft_revision"]) != expected_draft_revision:
                raise AnnotationConflictError(
                    "draft_revision_conflict",
                    "The annotation draft changed; refresh before submitting.",
                    current=self._segment_projection(connection, segment, include_draft=True),
                )
            draft = connection.execute(
                """
                SELECT targets_json, content_sha256
                FROM initial_annotation_drafts WHERE segment_id = ?
                """,
                (segment["id"],),
            ).fetchone()
            if draft is None:
                raise AnnotationValidationError(
                    "annotation_incomplete",
                    "Save a complete initial annotation before submitting.",
                )
            targets = json.loads(draft["targets_json"])
            self._validate_submission(segment, targets)
            revision_number_row = connection.execute(
                """
                SELECT COALESCE(MAX(revision_number), 0) + 1 AS next_revision
                FROM initial_annotation_revisions WHERE segment_id = ?
                """,
                (segment["id"],),
            ).fetchone()
            revision_number = int(revision_number_row["next_revision"])
            timestamp = _now()
            connection.execute(
                """
                INSERT INTO initial_annotation_revisions (
                    revision_ref, segment_id, revision_number, targets_json,
                    content_sha256, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    _new_ref("annotation_revision"),
                    segment["id"],
                    revision_number,
                    draft["targets_json"],
                    draft["content_sha256"],
                    timestamp,
                ),
            )
            connection.execute(
                """
                UPDATE annotation_segments
                SET status = 'submitted', state_revision = state_revision + 1,
                    submitted_revision = ?, updated_at = ?
                WHERE id = ?
                """,
                (revision_number, timestamp, segment["id"]),
            )
            self._touch_job(connection, int(job["id"]))
            self._record_action(
                connection,
                int(segment["id"]),
                "submitted",
                {"revision": revision_number},
            )
            resolution = connection.execute(
                """
                SELECT
                    SUM(CASE WHEN status = 'submitted' THEN 1 ELSE 0 END)
                        AS submitted_count,
                    SUM(CASE WHEN status = 'skipped' THEN 1 ELSE 0 END)
                        AS skipped_count,
                    SUM(CASE
                        WHEN status NOT IN ('submitted', 'skipped') THEN 1
                        ELSE 0
                    END) AS unresolved_count
                FROM annotation_segments WHERE job_id = ?
                """,
                (job_id,),
            ).fetchone()
            if (
                int(resolution["unresolved_count"] or 0) == 0
                and int(resolution["submitted_count"] or 0) > 0
            ):
                already_emitted = connection.execute(
                    """
                    SELECT 1 FROM workflow_handoffs
                    WHERE job_id = ? AND kind = 'initial_annotation_submitted'
                    LIMIT 1
                    """,
                    (job_id,),
                ).fetchone()
                if already_emitted is None:
                    handoff_payload = {
                        "job_ref": job_ref,
                        "submitted_count": int(
                            resolution["submitted_count"] or 0
                        ),
                        "skipped_count": int(
                            resolution["skipped_count"] or 0
                        ),
                    }
                    connection.execute(
                        """
                        INSERT INTO workflow_handoffs (
                            handoff_ref, job_id, kind, payload_json,
                            content_sha256, created_at
                        ) VALUES (?, ?, 'initial_annotation_submitted', ?, ?, ?)
                        """,
                        (
                            _new_ref("handoff"),
                            job_id,
                            _canonical_json(handoff_payload),
                            _payload_hash(handoff_payload),
                            timestamp,
                        ),
                    )
            return self._segment_projection(
                connection,
                self._segment_row(connection, job_id, segment_ref),
                include_draft=True,
            )

        return self.mutate(
            idempotency_key=idempotency_key,
            operation="submit_segment",
            request_payload=payload,
            callback=submit,
        )

    def segment_action(
        self,
        *,
        operation: str,
        job_ref: str,
        segment_ref: str,
        expected_segment_revision: int,
        idempotency_key: str,
        reason_code: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            "job_ref": job_ref,
            "segment_ref": segment_ref,
            "expected_segment_revision": expected_segment_revision,
            "reason_code": reason_code,
            "note": note,
        }

        def apply(connection: sqlite3.Connection) -> dict[str, Any]:
            job_id, job = self._mutable_waiting_job(connection, job_ref)
            segment = self._segment_row(connection, job_id, segment_ref)
            self._require_segment_revision(segment, expected_segment_revision, connection)
            current_status = str(segment["status"])
            if operation == "reopen":
                if current_status != "submitted":
                    self._invalid_segment_action(connection, segment)
                next_status = "draft"
                submitted_revision: int | None = None
                skip_reason = None
                skip_note = None
                skip_restore_status = None
                skip_restore_revision = None
            elif operation == "skip":
                if current_status not in {
                    "pending_initial_annotation",
                    "draft",
                    "submitted",
                }:
                    self._invalid_segment_action(connection, segment)
                next_status = "skipped"
                submitted_revision = None
                skip_reason = reason_code
                skip_note = note
                skip_restore_status = current_status
                skip_restore_revision = (
                    int(segment["submitted_revision"])
                    if current_status == "submitted"
                    and segment["submitted_revision"] is not None
                    else None
                )
            elif operation == "unskip":
                if current_status != "skipped":
                    self._invalid_segment_action(connection, segment)
                next_status = segment["skip_restore_status"]
                if next_status not in {
                    "pending_initial_annotation",
                    "draft",
                    "submitted",
                }:
                    raise RuntimeError("skipped segment lacks a restore state")
                submitted_revision = (
                    int(segment["skip_restore_submitted_revision"])
                    if next_status == "submitted"
                    and segment["skip_restore_submitted_revision"] is not None
                    else None
                )
                if next_status == "submitted" and submitted_revision is None:
                    raise RuntimeError("submitted restore state lacks a revision")
                skip_reason = None
                skip_note = None
                skip_restore_status = None
                skip_restore_revision = None
            else:
                raise RuntimeError(f"unsupported segment action: {operation}")
            timestamp = _now()
            connection.execute(
                """
                UPDATE annotation_segments
                SET status = ?, state_revision = state_revision + 1,
                    submitted_revision = ?, skip_reason_code = ?, skip_note = ?,
                    skip_restore_status = ?,
                    skip_restore_submitted_revision = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    next_status,
                    submitted_revision,
                    skip_reason,
                    skip_note,
                    skip_restore_status,
                    skip_restore_revision,
                    timestamp,
                    segment["id"],
                ),
            )
            self._touch_job(connection, int(job["id"]))
            self._record_action(
                connection,
                int(segment["id"]),
                operation,
                {"reason_code": reason_code, "note": note},
            )
            return self._segment_projection(
                connection,
                self._segment_row(connection, job_id, segment_ref),
                include_draft=True,
            )

        return self.mutate(
            idempotency_key=idempotency_key,
            operation=operation,
            request_payload=payload,
            callback=apply,
        )

    def start_tracking(
        self,
        *,
        job_ref: str,
        expected_job_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        payload = {"job_ref": job_ref, "expected_job_revision": expected_job_revision}

        def start(connection: sqlite3.Connection) -> dict[str, Any]:
            job_id = self._job_id(connection, job_ref)
            job = self._job_row(connection, job_id)
            self._require_job_revision(job, expected_job_revision, connection)
            if job["status"] != "waiting_initial_annotation":
                self._invalid_job_action(connection, job_id)
            statuses = [
                str(row["status"])
                for row in connection.execute(
                    "SELECT status FROM annotation_segments WHERE job_id = ?",
                    (job_id,),
                ).fetchall()
            ]
            if not statuses or any(status not in {"submitted", "skipped"} for status in statuses):
                raise AnnotationConflictError(
                    "segments_not_resolved",
                    "Every segment must be submitted or skipped before Tracking starts.",
                    current=self._job_projection(connection, job_id),
                )
            if all(status == "skipped" for status in statuses):
                raise AnnotationConflictError(
                    "no_processable_segments",
                    "Use the no-processable-targets action when every segment is skipped.",
                    current=self._job_projection(connection, job_id),
                )
            timestamp = _now()
            connection.execute(
                """
                UPDATE annotation_jobs
                SET status = 'tracking', state_revision = state_revision + 1,
                    updated_at = ?
                WHERE id = ?
                """,
                (timestamp, job_id),
            )
            connection.execute(
                """
                UPDATE annotation_segments
                SET status = 'tracking', state_revision = state_revision + 1,
                    updated_at = ?
                WHERE job_id = ? AND status = 'submitted'
                """,
                (timestamp, job_id),
            )
            self._enqueue_run(connection, job_id=job_id, kind="tracking")
            return self._job_projection(connection, job_id)

        return self.mutate(
            idempotency_key=idempotency_key,
            operation="start_tracking",
            request_payload=payload,
            callback=start,
        )

    def complete_no_processable_targets(
        self,
        *,
        job_ref: str,
        expected_job_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        payload = {"job_ref": job_ref, "expected_job_revision": expected_job_revision}

        def complete(connection: sqlite3.Connection) -> dict[str, Any]:
            job_id = self._job_id(connection, job_ref)
            job = self._job_row(connection, job_id)
            self._require_job_revision(job, expected_job_revision, connection)
            if job["status"] != "waiting_initial_annotation":
                self._invalid_job_action(connection, job_id)
            statuses = [
                str(row["status"])
                for row in connection.execute(
                    "SELECT status FROM annotation_segments WHERE job_id = ?",
                    (job_id,),
                ).fetchall()
            ]
            if not statuses or any(status != "skipped" for status in statuses):
                raise AnnotationConflictError(
                    "processable_segments_remain",
                    "Every segment must be skipped before completing this way.",
                    current=self._job_projection(connection, job_id),
                )
            self._cancel_job(
                connection,
                job_id,
                completion_outcome="no_processable_targets",
                request_runtime_cancel=False,
            )
            return self._job_projection(connection, job_id)

        return self.mutate(
            idempotency_key=idempotency_key,
            operation="complete_no_processable_targets",
            request_payload=payload,
            callback=complete,
        )

    def cancel_job(
        self,
        *,
        job_ref: str,
        expected_job_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        payload = {"job_ref": job_ref, "expected_job_revision": expected_job_revision}

        def cancel(connection: sqlite3.Connection) -> dict[str, Any]:
            job_id = self._job_id(connection, job_ref)
            job = self._job_row(connection, job_id)
            self._require_job_revision(job, expected_job_revision, connection)
            if job["status"] in {"tracked", "annotated", "cancelled"}:
                self._invalid_job_action(connection, job_id)
            if (
                job["status"] == "failed"
                and job["failure_code"] == "recovery_required"
            ):
                raise AnnotationConflictError(
                    "recovery_confirmation_required",
                    "The failed Runtime scope remains quarantined until an "
                    "operator confirms that the old process group is absent.",
                    current=self._job_projection(connection, job_id),
                )
            self._cancel_job(
                connection,
                job_id,
                completion_outcome="cancelled_by_user",
                request_runtime_cancel=True,
            )
            return self._job_projection(connection, job_id)

        return self.mutate(
            idempotency_key=idempotency_key,
            operation="cancel_job",
            request_payload=payload,
            callback=cancel,
        )

    def retry_job(
        self,
        *,
        job_ref: str,
        expected_job_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        payload = {"job_ref": job_ref, "expected_job_revision": expected_job_revision}

        def retry(connection: sqlite3.Connection) -> dict[str, Any]:
            job_id = self._job_id(connection, job_ref)
            job = self._job_row(connection, job_id)
            self._require_job_revision(job, expected_job_revision, connection)
            if job["status"] != "failed" or not bool(job["failure_retryable"]):
                self._invalid_job_action(connection, job_id)
            last_run = connection.execute(
                """
                SELECT kind FROM runtime_runs
                WHERE job_id = ?
                ORDER BY id DESC LIMIT 1
                """,
                (job_id,),
            ).fetchone()
            kind = str(last_run["kind"]) if last_run is not None else "prepare"
            next_status = {
                "tracking": "tracking",
                "postprocessing": "postprocessing",
            }.get(kind, "preparing")
            timestamp = _now()
            connection.execute(
                """
                UPDATE annotation_jobs
                SET status = ?, state_revision = state_revision + 1,
                    failure_code = NULL, failure_message = NULL,
                    failure_ref = NULL, private_failure_detail = NULL,
                    failure_retryable = 0, cancel_requested = 0,
                    cancel_requested_at = NULL, updated_at = ?
                WHERE id = ?
                """,
                (next_status, timestamp, job_id),
            )
            if kind == "tracking":
                connection.execute(
                    """
                    UPDATE annotation_segments
                    SET status = 'tracking', state_revision = state_revision + 1,
                        updated_at = ?
                    WHERE job_id = ? AND status IN ('submitted', 'tracking')
                    """,
                    (timestamp, job_id),
                )
            elif kind == "postprocessing":
                connection.execute(
                    """
                    UPDATE annotation_segments
                    SET status = 'postprocessing',
                        state_revision = state_revision + 1,
                        updated_at = ?
                    WHERE job_id = ?
                      AND status IN (
                          'tracked', 'postprocessing',
                          'postprocessing_failed'
                      )
                    """,
                    (timestamp, job_id),
                )
            self._enqueue_run(connection, job_id=job_id, kind=kind)
            return self._job_projection(connection, job_id)

        return self.mutate(
            idempotency_key=idempotency_key,
            operation="retry_job",
            request_payload=payload,
            callback=retry,
        )

    def operator_clear_global_writer_quarantine(
        self,
        *,
        confirmation: str,
        operator_reference: str,
        idempotency_key: str,
        writer_lock_path: Path,
    ) -> dict[str, Any]:
        """Audit and clear the cross-domain writer quarantine.

        This is deliberately separate from resolving an Annotation Job. The
        operator confirmation covers every Navigation and Annotation writer,
        so a Job-specific action cannot accidentally clear a marker created by
        an unrelated Navigation process.
        """

        required_confirmation = (
            "all_navigation_annotation_writer_process_groups_absent"
        )
        if confirmation != required_confirmation:
            raise AnnotationValidationError(
                "invalid_global_recovery_confirmation",
                "Global recovery requires confirmation that all writer process "
                "groups are absent.",
            )
        normalized_reference = operator_reference.strip()
        if not normalized_reference or len(normalized_reference) > 200:
            raise AnnotationValidationError(
                "invalid_operator_reference",
                "An operator or ticket reference is required for recovery.",
            )
        if not idempotency_key or len(idempotency_key) > 200:
            raise AnnotationValidationError(
                "invalid_idempotency_key",
                "Idempotency-Key must contain between 1 and 200 characters.",
            )
        request_sha = _payload_hash(
            {
                "action": "clear_global_quarantine",
                "confirmation": confirmation,
                "operator_reference": normalized_reference,
            }
        )
        observed_state = navigation_writer_marker_state(writer_lock_path)
        action_ref: str
        expected_marker_state_sha256: str
        expected_marker_entry_sha256s: tuple[str, ...]
        completed_response: dict[str, Any] | None = None
        with self._write() as connection:
            existing = connection.execute(
                """
                SELECT a.action_ref, a.request_sha256,
                       a.expected_marker_state_sha256,
                       a.expected_marker_entries_json, c.response_json
                FROM writer_quarantine_actions a
                LEFT JOIN writer_quarantine_action_completions c
                  ON c.action_ref = a.action_ref
                WHERE a.idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if existing["request_sha256"] != request_sha:
                    raise AnnotationConflictError(
                        "idempotency_key_reused",
                        "Idempotency-Key was already used for another recovery request.",
                    )
                action_ref = str(existing["action_ref"])
                expected_marker_state_sha256 = str(
                    existing["expected_marker_state_sha256"],
                )
                raw_entries = json.loads(existing["expected_marker_entries_json"])
                if (
                    not isinstance(raw_entries, list)
                    or any(
                        not isinstance(entry, str)
                        or len(entry) != 64
                        or any(
                            character not in "0123456789abcdef"
                            for character in entry
                        )
                        for entry in raw_entries
                    )
                    or len(set(raw_entries)) != len(raw_entries)
                ):
                    raise RuntimeError(
                        "writer quarantine action marker binding is invalid",
                    )
                expected_marker_entry_sha256s = tuple(raw_entries)
                if existing["response_json"] is not None:
                    completed_response = json.loads(existing["response_json"])
            else:
                action_ref = _new_ref("writer_quarantine_action")
                expected_marker_state_sha256 = observed_state.sha256
                expected_marker_entry_sha256s = (
                    observed_state.marker_entry_sha256s
                )
                timestamp = _now()
                connection.execute(
                    """
                    INSERT INTO writer_quarantine_actions (
                        idempotency_key, action_ref, action, confirmation,
                        operator_reference, deployment_instance, request_sha256,
                        expected_marker_state_sha256,
                        expected_marker_entries_json, created_at
                    ) VALUES (?, ?, 'clear_global_quarantine', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        idempotency_key,
                        action_ref,
                        confirmation,
                        normalized_reference,
                        self.deployment_instance,
                        request_sha,
                        expected_marker_state_sha256,
                        _canonical_json(list(expected_marker_entry_sha256s)),
                        timestamp,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO writer_quarantine_action_recoveries (
                        action_ref, failure_ref
                    )
                    SELECT ?, failure_ref
                    FROM annotation_jobs
                    WHERE status = 'failed'
                      AND failure_code = 'recovery_required'
                      AND failure_ref IS NOT NULL
                    """,
                    (action_ref,),
                )
                connection.execute(
                    """
                    INSERT INTO writer_quarantine_action_recoveries (
                        action_ref, failure_ref
                    )
                    SELECT ?, failure_ref
                    FROM compatibility_publications
                    WHERE status = 'failed'
                      AND failure_code = 'recovery_required'
                      AND failure_ref IS NOT NULL
                    """,
                    (action_ref,),
                )

        clearance_expected_sha256 = expected_marker_state_sha256
        if completed_response is not None:
            current_entries = set(observed_state.marker_entry_sha256s)
            expected_entries = set(expected_marker_entry_sha256s)
            if not current_entries.issubset(expected_entries):
                # At least one marker belongs to a newer writer incident.
                # The old action must not delete either old or new state.
                raise AnnotationConflictError(
                    "global_writer_quarantine_active",
                    "A newer global writer quarantine requires another safety "
                    "confirmation.",
                )
            # Completion committed before a partial marker cleanup. The exact
            # remaining subset (including an empty subset) is still owned by
            # this action. Always reacquire flock and recheck that exact
            # observation before returning or removing markers.
            clearance_expected_sha256 = observed_state.sha256

        # Keep flock and the exact marker set until the completion row commits.
        # A DB failure therefore leaves every marker intact. A crash after the
        # commit but before unlink is repaired by replaying this same action.
        with navigation_writer_quarantine_clearance(
            writer_lock_path,
            expected_marker_state_sha256=clearance_expected_sha256,
            all_writer_process_groups_absent=True,
        ) as marker_state:
            if completed_response is not None:
                response = completed_response
            else:
                response = {
                    "action_ref": action_ref,
                    "status": "global_quarantine_clear_confirmed",
                    "marker_was_present": (
                        marker_state.active_present
                        or marker_state.quarantine_present
                    ),
                }
                with self._write() as connection:
                    completed = connection.execute(
                        """
                        SELECT response_json
                        FROM writer_quarantine_action_completions
                        WHERE action_ref = ?
                        """,
                        (action_ref,),
                    ).fetchone()
                    if completed is not None:
                        response = json.loads(completed["response_json"])
                    else:
                        connection.execute(
                            """
                            INSERT INTO writer_quarantine_action_completions (
                                action_ref, marker_was_present,
                                response_json, completed_at
                            ) VALUES (?, ?, ?, ?)
                            """,
                            (
                                action_ref,
                                int(response["marker_was_present"]),
                                _canonical_json(response),
                                _now(),
                            ),
                        )
        return response

    def operator_confirm_recovery(
        self,
        *,
        job_ref: str,
        expected_job_revision: int,
        confirmation: str,
        operator_reference: str,
        idempotency_key: str,
        global_quarantine_action_ref: str,
        writer_lock_path: Path,
        disposition: str = "retry",
    ) -> dict[str, Any]:
        """Resolve recovery_required through an explicit operations boundary.

        This method is intentionally not exposed by the trusted-intranet Web
        router. The caller must first verify that the old process group is gone
        and provide an auditable external operator/ticket reference. The
        operator may then retry the quarantined job or safely abandon it and
        release its source scope.
        """

        if confirmation != "old_process_group_absent":
            raise AnnotationValidationError(
                "invalid_recovery_confirmation",
                "Recovery requires confirmation that the old process group is absent.",
            )
        if disposition not in {"retry", "abandon"}:
            raise AnnotationValidationError(
                "invalid_recovery_disposition",
                "Recovery disposition must be retry or abandon.",
            )
        normalized_reference = operator_reference.strip()
        if not normalized_reference or len(normalized_reference) > 200:
            raise AnnotationValidationError(
                "invalid_operator_reference",
                "An operator or ticket reference is required for recovery.",
            )
        if not idempotency_key or len(idempotency_key) > 200:
            raise AnnotationValidationError(
                "invalid_idempotency_key",
                "Idempotency-Key must contain between 1 and 200 characters.",
            )
        request_sha = _payload_hash(
            {
                "action": (
                    "confirm_recovery"
                    if disposition == "retry"
                    else "abandon_recovery"
                ),
                "job_ref": job_ref,
                "expected_job_revision": expected_job_revision,
                "confirmation": confirmation,
                "operator_reference": normalized_reference,
                "global_quarantine_action_ref": global_quarantine_action_ref,
            }
        )
        observed_state = navigation_writer_marker_state(writer_lock_path)
        # Bind the exact marker observation to the writer flock. The body
        # rejects a non-empty observation before any recovery mutation, so an
        # exception preserves every captured marker. An empty observation
        # remains protected until the nested DB transaction has committed.
        with (
            navigation_writer_quarantine_clearance(
                writer_lock_path,
                expected_marker_state_sha256=observed_state.sha256,
                all_writer_process_groups_absent=True,
            ) as marker_state,
            self._write() as connection,
        ):
            existing = connection.execute(
                """
                SELECT response_json, request_sha256
                FROM annotation_operator_actions
                WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if existing["request_sha256"] != request_sha:
                    raise AnnotationConflictError(
                        "idempotency_key_reused",
                        "Idempotency-Key was already used for another recovery request.",
                    )
                if marker_state.active_present or marker_state.quarantine_present:
                    raise AnnotationConflictError(
                        "global_writer_quarantine_active",
                        "A newer global writer quarantine requires another safety "
                        "confirmation.",
                    )
                return json.loads(existing["response_json"])
            job_id = self._job_id(connection, job_ref)
            job = self._job_row(connection, job_id)
            self._require_job_revision(job, expected_job_revision, connection)
            if (
                job["status"] != "failed"
                or job["failure_code"] != "recovery_required"
            ):
                self._invalid_job_action(connection, job_id)
            global_confirmation = connection.execute(
                """
                SELECT c.completed_at
                FROM writer_quarantine_actions a
                JOIN writer_quarantine_action_completions c
                  ON c.action_ref = a.action_ref
                JOIN writer_quarantine_action_recoveries r
                  ON r.action_ref = a.action_ref
                WHERE a.action_ref = ?
                  AND a.action = 'clear_global_quarantine'
                  AND a.confirmation =
                      'all_navigation_annotation_writer_process_groups_absent'
                  AND r.failure_ref = ?
                """,
                (global_quarantine_action_ref, job["failure_ref"]),
            ).fetchone()
            if global_confirmation is None:
                raise AnnotationValidationError(
                    "global_quarantine_confirmation_required",
                    "Recovery requires a recorded global writer safety confirmation.",
                )
            if marker_state.active_present or marker_state.quarantine_present:
                raise AnnotationConflictError(
                    "global_writer_quarantine_active",
                    "A newer global writer quarantine requires another safety "
                    "confirmation.",
                )
            active = connection.execute(
                """
                SELECT 1 FROM runtime_runs
                WHERE job_id = ? AND status = 'running'
                """,
                (job_id,),
            ).fetchone()
            if active is not None:
                raise AnnotationConflictError(
                    "runtime_still_active",
                    "Recovery cannot start while the old Runtime run is active.",
                    current=self._job_projection(connection, job_id),
                )
            if disposition == "abandon":
                self._cancel_job(
                    connection,
                    job_id,
                    completion_outcome="abandoned_after_recovery_confirmation",
                    request_runtime_cancel=False,
                )
                response = self._job_projection(connection, job_id)
                timestamp = _now()
                action = "abandon_recovery"
                connection.execute(
                    """
                    INSERT INTO annotation_operator_actions (
                        idempotency_key, action_ref, job_id, action, confirmation,
                        operator_reference, deployment_instance, request_sha256,
                        response_json, created_at, global_quarantine_action_ref
                    ) VALUES (?, ?, ?, ?, 'old_process_group_absent',
                              ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        idempotency_key,
                        _new_ref("operator_action"),
                        job_id,
                        action,
                        normalized_reference,
                        self.deployment_instance,
                        request_sha,
                        _canonical_json(response),
                        timestamp,
                        global_quarantine_action_ref,
                    ),
                )
                return response
            last_run = connection.execute(
                """
                SELECT kind FROM runtime_runs
                WHERE job_id = ? ORDER BY id DESC LIMIT 1
                """,
                (job_id,),
            ).fetchone()
            kind = str(last_run["kind"]) if last_run is not None else "prepare"
            next_status = {
                "tracking": "tracking",
                "postprocessing": "postprocessing",
            }.get(kind, "preparing")
            timestamp = _now()
            connection.execute(
                """
                UPDATE annotation_jobs
                SET status = ?, state_revision = state_revision + 1,
                    failure_code = NULL, failure_message = NULL,
                    failure_ref = NULL, private_failure_detail = NULL,
                    failure_retryable = 0, cancel_requested = 0,
                    cancel_requested_at = NULL, updated_at = ?
                WHERE id = ?
                """,
                (next_status, timestamp, job_id),
            )
            if kind == "tracking":
                connection.execute(
                    """
                    UPDATE annotation_segments
                    SET status = 'tracking', state_revision = state_revision + 1,
                        updated_at = ?
                    WHERE job_id = ? AND status IN ('submitted', 'tracking')
                    """,
                    (timestamp, job_id),
                )
            elif kind == "postprocessing":
                connection.execute(
                    """
                    UPDATE annotation_segments
                    SET status = 'postprocessing',
                        state_revision = state_revision + 1,
                        updated_at = ?
                    WHERE job_id = ?
                      AND status IN (
                          'tracked', 'postprocessing',
                          'postprocessing_failed'
                      )
                    """,
                    (timestamp, job_id),
                )
            self._enqueue_run(connection, job_id=job_id, kind=kind)
            response = self._job_projection(connection, job_id)
            action = "confirm_recovery"
            connection.execute(
                """
                INSERT INTO annotation_operator_actions (
                    idempotency_key, action_ref, job_id, action, confirmation,
                    operator_reference, deployment_instance, request_sha256,
                    response_json, created_at, global_quarantine_action_ref
                ) VALUES (?, ?, ?, ?, 'old_process_group_absent',
                          ?, ?, ?, ?, ?, ?)
                """,
                (
                    idempotency_key,
                    _new_ref("operator_action"),
                    job_id,
                    action,
                    normalized_reference,
                    self.deployment_instance,
                    request_sha,
                    _canonical_json(response),
                    timestamp,
                    global_quarantine_action_ref,
                ),
            )
            return response

    def claim_next_run(
        self,
        *,
        worker_id: str,
        owner_epoch: str | None = None,
        writer_lock_path: Path | None = None,
        lease_seconds: int = 120,
    ) -> dict[str, Any] | None:
        with self._write() as connection:
            timestamp = _now()
            if self._recover_interrupted_runs_conn(
                connection,
                current_owner_epoch=owner_epoch,
                writer_lock_path=writer_lock_path,
                timestamp=timestamp,
            ):
                return None
            if self._has_recovery_quarantine_conn(connection):
                return None
            coordination_path = self._optional_writer_lock_path(writer_lock_path)
            if (
                coordination_path is not None
                and navigation_writer_quarantine_present(coordination_path)
            ):
                return None
            connection.execute(
                "DELETE FROM runtime_leases WHERE expires_at <= ?",
                (timestamp,),
            )
            row = connection.execute(
                """
                SELECT r.*, j.job_ref, j.dataset_date, j.status AS job_status,
                       j.staging_root, c.private_snapshot_dir,
                       c.files_json AS calibration_files_json,
                       c.content_sha256 AS calibration_content_sha256
                FROM runtime_runs r
                JOIN annotation_jobs j ON j.id = r.job_id
                JOIN calibration_snapshots c ON c.id = j.calibration_snapshot_id
                WHERE r.status = 'queued'
                  AND (
                    (r.kind = 'prepare' AND j.status = 'preparing')
                    OR (r.kind = 'tracking' AND j.status = 'tracking')
                    OR (
                        r.kind = 'postprocessing'
                        AND j.status = 'postprocessing'
                    )
                    OR (
                        r.kind = 'fix'
                        AND j.status = 'annotated'
                        AND EXISTS (
                            SELECT 1
                            FROM runtime_run_review_links l
                            JOIN trajectory_review_tasks tr
                              ON tr.id = l.review_id
                            WHERE l.run_id = r.id
                              AND tr.status = 'in_progress'
                        )
                    )
                    OR (
                        r.kind = 'compatibility_publish'
                        AND j.status = 'annotated'
                        AND EXISTS (
                            SELECT 1
                            FROM runtime_run_publication_links l
                            JOIN compatibility_publications p
                              ON p.id = l.publication_id
                            JOIN trajectory_review_tasks tr
                              ON tr.id = l.review_id
                            WHERE l.run_id = r.id
                              AND p.status = 'queued'
                              AND tr.status = 'approved'
                              AND tr.approved_fix_revision_id
                                  = p.fix_revision_id
                        )
                    )
                  )
                ORDER BY r.created_at, r.id
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            lease_key = "annotation_writer"
            existing = connection.execute(
                "SELECT lease_key FROM runtime_leases WHERE lease_key = ?",
                (lease_key,),
            ).fetchone()
            if existing is not None:
                return None
            expires = (
                datetime.now(UTC) + timedelta(seconds=lease_seconds)
            ).isoformat(timespec="milliseconds")
            connection.execute(
                """
                INSERT INTO runtime_leases (
                    lease_key, run_id, worker_id, expires_at, acquired_at,
                    owner_epoch
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    lease_key,
                    row["id"],
                    worker_id,
                    expires,
                    timestamp,
                    owner_epoch,
                ),
            )
            connection.execute(
                """
                UPDATE runtime_runs
                SET status = 'running', worker_id = ?, started_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (worker_id, timestamp, timestamp, row["id"]),
            )
            if row["kind"] == "compatibility_publish":
                publication = connection.execute(
                    """
                    SELECT publication_id, review_id
                    FROM runtime_run_publication_links
                    WHERE run_id = ?
                    """,
                    (row["id"],),
                ).fetchone()
                if publication is None:
                    raise RuntimeError(
                        "compatibility publication run lacks its binding"
                    )
                connection.execute(
                    """
                    UPDATE compatibility_publications
                    SET status = 'running'
                    WHERE id = ?
                    """,
                    (publication["publication_id"],),
                )
                connection.execute(
                    """
                    UPDATE trajectory_review_tasks
                    SET state_revision = state_revision + 1, updated_at = ?
                    WHERE id = ?
                    """,
                    (timestamp, publication["review_id"]),
                )
            clips = [
                str(item["source_clip"])
                for item in connection.execute(
                    """
                    SELECT source_clip FROM annotation_job_source_clips
                    WHERE job_id = ? ORDER BY ordinal
                    """,
                    (row["job_id"],),
                ).fetchall()
            ]
            return {
                "run_id": int(row["id"]),
                "run_ref": row["run_ref"],
                "kind": row["kind"],
                "job_id": int(row["job_id"]),
                "job_ref": row["job_ref"],
                "dataset_date": row["dataset_date"],
                "source_clips": clips,
                "staging_root": row["staging_root"],
                "calibration_snapshot_dir": row["private_snapshot_dir"],
                "calibration_snapshot_files": json.loads(
                    row["calibration_files_json"]
                ),
                "calibration_snapshot_sha256": row[
                    "calibration_content_sha256"
                ],
                "attempt": int(row["attempt"]),
                "active_reserved_bytes": self._active_reserved_bytes_conn(
                    connection,
                    excluding_job_id=int(row["job_id"]),
                ),
            }

    def _recover_interrupted_runs_conn(
        self,
        connection: sqlite3.Connection,
        *,
        current_owner_epoch: str | None,
        writer_lock_path: Path | None,
        timestamp: str | None = None,
    ) -> int:
        checked_at = timestamp or _now()
        rows = connection.execute(
            """
            SELECT r.id
            FROM runtime_runs r
            LEFT JOIN runtime_leases l ON l.run_id = r.id
            WHERE r.status = 'running'
              AND (
                    l.run_id IS NULL
                    OR l.expires_at <= ?
                    OR (
                        ? IS NOT NULL
                        AND l.owner_epoch IS NOT NULL
                        AND l.owner_epoch <> ?
                    )
                  )
            """,
            (checked_at, current_owner_epoch, current_owner_epoch),
        ).fetchall()
        if rows:
            coordination_path = self._required_writer_lock_path(writer_lock_path)
        recoveries = [
            (row, _new_ref("annotation_error"))
            for row in rows
        ]
        for _row, failure_ref in recoveries:
            ensure_navigation_writer_quarantine(
                coordination_path,
                recovery_ref=failure_ref,
            )
        for row, failure_ref in recoveries:
            self._fail_run_conn(
                connection,
                int(row["id"]),
                code="recovery_required",
                message="Runtime recovery requires an operator safety check.",
                retryable=False,
                private_detail=(
                    "The Web process restarted while a runtime run was marked running. "
                    "No success was inferred from filesystem state."
                ),
                failure_ref=failure_ref,
            )
        return len(rows)

    @staticmethod
    def _optional_writer_lock_path(
        writer_lock_path: Path | None,
    ) -> Path | None:
        if writer_lock_path is not None:
            return Path(writer_lock_path)
        if os.getenv("VLA_NAVIGATION_WRITER_LOCK_PATH"):
            return configured_writer_lock_path()
        return None

    @classmethod
    def _required_writer_lock_path(
        cls,
        writer_lock_path: Path | None,
    ) -> Path:
        resolved = cls._optional_writer_lock_path(writer_lock_path)
        if resolved is None:
            return configured_writer_lock_path()
        return resolved

    @staticmethod
    def _has_recovery_quarantine_conn(
        connection: sqlite3.Connection,
    ) -> bool:
        row = connection.execute(
            """
            SELECT 1
            FROM annotation_jobs
            WHERE status = 'failed' AND failure_code = 'recovery_required'
            LIMIT 1
            """
        ).fetchone()
        return row is not None

    def renew_run_lease(
        self,
        *,
        run_id: int,
        worker_id: str,
        lease_seconds: int = 120,
    ) -> bool:
        with self._write() as connection:
            expires = (
                datetime.now(UTC) + timedelta(seconds=lease_seconds)
            ).isoformat(timespec="milliseconds")
            cursor = connection.execute(
                """
                UPDATE runtime_leases
                SET expires_at = ?
                WHERE run_id = ? AND worker_id = ?
                """,
                (expires, run_id, worker_id),
            )
            return cursor.rowcount == 1

    def start_runtime_step(
        self,
        *,
        run_id: int,
        safe_step_code: str,
    ) -> None:
        if safe_step_code not in _SAFE_RUNTIME_STEP_CODES:
            raise RuntimeError("runtime step code is not public-safe")
        with self._write() as connection:
            self._running_run(connection, run_id)
            existing = connection.execute(
                """
                SELECT 1 FROM runtime_run_steps
                WHERE run_id = ? AND safe_step_code = ?
                """,
                (run_id, safe_step_code),
            ).fetchone()
            if existing is not None:
                raise RuntimeError("runtime semantic step was already recorded")
            ordinal_row = connection.execute(
                """
                SELECT COALESCE(MAX(ordinal), 0) + 1 AS ordinal
                FROM runtime_run_steps WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
            timestamp = _now()
            connection.execute(
                """
                INSERT INTO runtime_run_steps (
                    run_id, ordinal, safe_step_code, status,
                    artifact_sha256, return_code, diagnostic_ref,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 'started', NULL, NULL, NULL, ?, ?)
                """,
                (
                    run_id,
                    int(ordinal_row["ordinal"]),
                    safe_step_code,
                    timestamp,
                    timestamp,
                ),
            )

    def begin_postprocessing_publication(self, *, run_id: int) -> bool:
        """Atomically choose publication or a previously requested cancel.

        Once this durable fence is recorded, cancellation must no longer turn
        the Job into ``cancelled``: compatibility output may be committed at
        any point after the transaction returns.
        """

        with self._write() as connection:
            run = self._running_run(connection, run_id)
            if run["kind"] != "postprocessing":
                raise RuntimeError(
                    "publication fence belongs to a different run kind"
                )
            job = self._job_row(connection, int(run["job_id"]))
            if job["status"] != "postprocessing":
                raise RuntimeError(
                    "publication fence requires an active postprocessing job"
                )
            if bool(job["cancel_requested"]):
                return False
            existing = connection.execute(
                """
                SELECT 1 FROM runtime_run_steps
                WHERE run_id = ? AND safe_step_code = 'compatibility_publish'
                """,
                (run_id,),
            ).fetchone()
            if existing is not None:
                raise RuntimeError(
                    "postprocessing publication fence was already recorded"
                )
            ordinal_row = connection.execute(
                """
                SELECT COALESCE(MAX(ordinal), 0) + 1 AS ordinal
                FROM runtime_run_steps WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
            timestamp = _now()
            connection.execute(
                """
                INSERT INTO runtime_run_steps (
                    run_id, ordinal, safe_step_code, status,
                    artifact_sha256, return_code, diagnostic_ref,
                    created_at, updated_at
                ) VALUES (
                    ?, ?, 'compatibility_publish', 'started',
                    NULL, NULL, NULL, ?, ?
                )
                """,
                (
                    run_id,
                    int(ordinal_row["ordinal"]),
                    timestamp,
                    timestamp,
                ),
            )
            return True

    def finish_runtime_step(
        self,
        *,
        run_id: int,
        safe_step_code: str,
        status: str,
        return_code: int | None = None,
        diagnostic_kind: str | None = None,
    ) -> str | None:
        if safe_step_code not in _SAFE_RUNTIME_STEP_CODES:
            raise RuntimeError("runtime step code is not public-safe")
        if status not in {"succeeded", "failed"}:
            raise RuntimeError("runtime step terminal status is invalid")
        if status == "succeeded" and return_code != 0:
            raise RuntimeError("successful runtime step must record return code zero")
        if return_code is not None and isinstance(return_code, bool):
            raise RuntimeError("runtime step return code is invalid")
        if status == "succeeded" and diagnostic_kind is not None:
            raise RuntimeError("successful runtime step cannot have a diagnostic kind")
        if status == "failed":
            if diagnostic_kind not in _SAFE_RUNTIME_DIAGNOSTIC_KINDS:
                raise RuntimeError("failed runtime step requires a safe diagnostic kind")
            if diagnostic_kind == "nonzero_exit" and (
                return_code is None or return_code == 0
            ):
                raise RuntimeError(
                    "nonzero runtime failure requires its actual return code",
                )
        with self._write() as connection:
            self._running_run(connection, run_id)
            row = connection.execute(
                """
                SELECT id, status FROM runtime_run_steps
                WHERE run_id = ? AND safe_step_code = ?
                """,
                (run_id, safe_step_code),
            ).fetchone()
            if row is None or row["status"] != "started":
                raise RuntimeError("runtime semantic step is not active")
            diagnostic_ref = (
                _new_ref(f"runtime_step_{diagnostic_kind}")
                if status == "failed"
                else None
            )
            timestamp = _now()
            connection.execute(
                """
                UPDATE runtime_run_steps
                SET status = ?, return_code = ?, diagnostic_ref = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    return_code,
                    diagnostic_ref,
                    timestamp,
                    row["id"],
                ),
            )
            return diagnostic_ref

    def fail_active_runtime_step(
        self,
        *,
        run_id: int,
        return_code: int | None = None,
        diagnostic_kind: str = "error",
    ) -> str | None:
        if diagnostic_kind not in _SAFE_RUNTIME_DIAGNOSTIC_KINDS:
            raise RuntimeError("runtime step diagnostic kind is invalid")
        if return_code is not None and isinstance(return_code, bool):
            raise RuntimeError("runtime step return code is invalid")
        if diagnostic_kind == "nonzero_exit" and (
            return_code is None or return_code == 0
        ):
            raise RuntimeError(
                "nonzero runtime failure requires its actual return code",
            )
        with self._write() as connection:
            run = connection.execute(
                "SELECT status FROM runtime_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if run is None:
                raise RuntimeError("runtime run not found")
            row = connection.execute(
                """
                SELECT id FROM runtime_run_steps
                WHERE run_id = ? AND status = 'started'
                ORDER BY ordinal DESC LIMIT 1
                """,
                (run_id,),
            ).fetchone()
            if row is None:
                return None
            diagnostic_ref = _new_ref(f"runtime_step_{diagnostic_kind}")
            connection.execute(
                """
                UPDATE runtime_run_steps
                SET status = 'failed', return_code = ?,
                    diagnostic_ref = ?, updated_at = ?
                WHERE id = ?
                """,
                (return_code, diagnostic_ref, _now(), row["id"]),
            )
            return diagnostic_ref

    def complete_prepare(
        self,
        *,
        run_id: int,
        staging_root: str,
        segments: list[dict[str, Any]],
        manifest: dict[str, Any],
    ) -> None:
        with self._write() as connection:
            run = self._running_run(connection, run_id)
            job = self._job_row(connection, int(run["job_id"]))
            if job["status"] == "cancelled" or bool(
                job["cancel_requested"],
            ):
                self._finish_run(connection, run_id, "cancelled")
                self._finalize_cancelled_job(
                    connection,
                    int(run["job_id"]),
                    completion_outcome="cancelled_by_user",
                )
                return
            if job["status"] != "preparing":
                raise RuntimeError("prepare run no longer owns a preparing job")
            self._require_runtime_step_ledger(
                connection,
                run_id,
                manifest.get("command_steps"),
            )
            if not segments:
                self._fail_run_conn(
                    connection,
                    run_id,
                    code="no_segments_prepared",
                    message="No processable segments were prepared.",
                    retryable=False,
                )
                return
            source_clips = {
                str(row["source_clip"])
                for row in connection.execute(
                    """
                    SELECT source_clip FROM annotation_job_source_clips
                    WHERE job_id = ?
                    """,
                    (run["job_id"],),
                ).fetchall()
            }
            prepared_clips = [str(segment["source_clip"]) for segment in segments]
            if (
                any(clip not in source_clips for clip in prepared_clips)
                or len({str(segment["private_segment_key"]) for segment in segments})
                != len(segments)
            ):
                self._fail_run_conn(
                    connection,
                    run_id,
                    code="invalid_preparation_result",
                    message="The runtime returned an invalid prepared segment set.",
                    retryable=False,
                    private_detail=(
                        "Prepared segments contained an unselected source clip "
                        "or duplicate private segment key."
                    ),
                )
                return
            timestamp = _now()
            for ordinal, segment in enumerate(segments, start=1):
                connection.execute(
                    """
                    INSERT INTO annotation_segments (
                        segment_ref, job_id, ordinal, source_clip, status,
                        private_segment_key, private_segment_root,
                        private_first_frame_path, first_frame_width,
                        first_frame_height, first_frame_sha256, first_frame_etag,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'pending_initial_annotation',
                              ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        _new_ref("segment"),
                        run["job_id"],
                        ordinal,
                        segment["source_clip"],
                        segment["private_segment_key"],
                        segment["segment_root"],
                        segment["first_frame_path"],
                        segment["width"],
                        segment["height"],
                        segment["sha256"],
                        segment["etag"],
                        timestamp,
                        timestamp,
                    ),
                )
            connection.execute(
                """
                UPDATE annotation_jobs
                SET status = 'waiting_initial_annotation',
                    state_revision = state_revision + 1,
                    staging_root = ?, updated_at = ?
                WHERE id = ?
                """,
                (staging_root, timestamp, run["job_id"]),
            )
            handoff_payload = {
                "job_ref": str(job["job_ref"]),
                "segment_count": len(segments),
            }
            connection.execute(
                """
                INSERT INTO workflow_handoffs (
                    handoff_ref, job_id, kind, payload_json,
                    content_sha256, created_at
                ) VALUES (?, ?, 'initial_annotation_ready', ?, ?, ?)
                """,
                (
                    _new_ref("handoff"),
                    run["job_id"],
                    _canonical_json(handoff_payload),
                    _payload_hash(handoff_payload),
                    timestamp,
                ),
            )
            self._insert_manifest(connection, run, "prepare", manifest)
            self._finish_run(connection, run_id, "succeeded")

    def tracking_inputs(self, job_id: int) -> dict[str, Any]:
        with self._connect() as connection:
            job = self._job_row(connection, job_id)
            prepare_manifest_row = connection.execute(
                """
                SELECT a.manifest_json, a.content_sha256, r.id AS run_id
                FROM artifact_manifests a
                JOIN runtime_runs r ON r.id = a.run_id
                WHERE a.job_id = ? AND a.stage = 'prepare'
                  AND r.status = 'succeeded'
                ORDER BY a.id DESC LIMIT 1
                """,
                (job_id,),
            ).fetchone()
            if prepare_manifest_row is None:
                raise RuntimeError("tracking job lacks a committed prepare manifest")
            prepare_manifest = json.loads(
                prepare_manifest_row["manifest_json"],
            )
            runtime_manifest_sha256 = prepare_manifest.get(
                "runtime_manifest_sha256",
            )
            prepared_artifact_tree_sha256 = prepare_manifest.get(
                "prepared_artifact_tree_sha256",
            )
            for label, value in (
                ("runtime manifest", runtime_manifest_sha256),
                ("prepared artifact tree", prepared_artifact_tree_sha256),
            ):
                if (
                    not isinstance(value, str)
                    or len(value) != 64
                    or any(
                        character not in "0123456789abcdef"
                        for character in value
                    )
                ):
                    raise RuntimeError(
                        f"prepare manifest lacks a valid {label} hash",
                    )
            segments: list[dict[str, Any]] = []
            rows = connection.execute(
                """
                SELECT * FROM annotation_segments
                WHERE job_id = ? AND status = 'tracking'
                ORDER BY ordinal
                """,
                (job_id,),
            ).fetchall()
            for segment in rows:
                revision = connection.execute(
                    """
                    SELECT targets_json, content_sha256
                    FROM initial_annotation_revisions
                    WHERE segment_id = ? AND revision_number = ?
                    """,
                    (segment["id"], segment["submitted_revision"]),
                ).fetchone()
                if revision is None:
                    raise RuntimeError("tracking segment has no submitted revision")
                segments.append(
                    {
                        "segment_id": int(segment["id"]),
                        "segment_ref": segment["segment_ref"],
                        "segment_root": segment["private_segment_root"],
                        "revision_number": int(segment["submitted_revision"]),
                        "revision_sha256": revision["content_sha256"],
                        "targets": json.loads(revision["targets_json"]),
                    }
                )
                for target in segments[-1]["targets"]:
                    checkpoint = connection.execute(
                        """
                        SELECT checkpoint_ref, identity, private_output_dir,
                               private_points_path, artifact_sha256
                        FROM tracking_checkpoints
                        WHERE job_id = ? AND segment_id = ? AND target_ref = ?
                          AND revision_sha256 = ?
                        """,
                        (
                            job_id,
                            segment["id"],
                            target["target_ref"],
                            revision["content_sha256"],
                        ),
                    ).fetchone()
                    if checkpoint is not None:
                        target["checkpoint"] = {
                            "checkpoint_ref": checkpoint["checkpoint_ref"],
                            "identity": checkpoint["identity"],
                            "output_dir": checkpoint["private_output_dir"],
                            "points_path": checkpoint["private_points_path"],
                            "artifact_sha256": checkpoint["artifact_sha256"],
                        }
            return {
                "job_ref": job["job_ref"],
                "staging_root": job["staging_root"],
                "expected_runtime_manifest_sha256": (
                    runtime_manifest_sha256
                ),
                "expected_prepared_artifact_tree_sha256": (
                    prepared_artifact_tree_sha256
                ),
                "segments": segments,
            }

    def record_tracking_checkpoint(
        self,
        *,
        run_id: int,
        segment_ref: str,
        target_ref: str,
        revision_sha256: str,
        identity: str,
        output_dir: str,
        points_path: str,
        artifact_sha256: str,
    ) -> dict[str, Any]:
        with self._write() as connection:
            run = self._running_run(connection, run_id)
            if run["kind"] != "tracking":
                raise RuntimeError("checkpoint belongs to a non-tracking run")
            job = self._job_row(connection, int(run["job_id"]))
            if job["status"] != "tracking":
                raise RuntimeError("checkpoint job is not tracking")
            if bool(job["cancel_requested"]):
                raise RuntimeError(
                    "tracking cancellation was requested before checkpoint commit",
                )
            segment = self._segment_row(
                connection,
                int(run["job_id"]),
                segment_ref,
            )
            revision = connection.execute(
                """
                SELECT content_sha256, targets_json FROM initial_annotation_revisions
                WHERE segment_id = ? AND revision_number = ?
                """,
                (segment["id"], segment["submitted_revision"]),
            ).fetchone()
            if revision is None or revision["content_sha256"] != revision_sha256:
                raise RuntimeError("checkpoint revision does not match submitted annotation")
            revision_target_refs = {
                str(item.get("target_ref"))
                for item in json.loads(revision["targets_json"])
                if isinstance(item, dict)
            }
            if target_ref not in revision_target_refs:
                raise RuntimeError("checkpoint target is not in the submitted annotation")
            existing = connection.execute(
                """
                SELECT checkpoint_ref, identity, private_output_dir,
                       private_points_path, artifact_sha256
                FROM tracking_checkpoints
                WHERE job_id = ? AND segment_id = ? AND target_ref = ?
                  AND revision_sha256 = ?
                """,
                (
                    run["job_id"],
                    segment["id"],
                    target_ref,
                    revision_sha256,
                ),
            ).fetchone()
            safe = {
                "segment_ref": segment_ref,
                "target_ref": target_ref,
                "identity": identity,
                "artifact_sha256": artifact_sha256,
            }
            if existing is not None:
                if (
                    existing["identity"] != identity
                    or existing["private_output_dir"] != output_dir
                    or existing["private_points_path"] != points_path
                    or existing["artifact_sha256"] != artifact_sha256
                ):
                    raise RuntimeError("committed tracking checkpoint is immutable")
                return safe
            timestamp = _now()
            connection.execute(
                """
                INSERT INTO tracking_checkpoints (
                    checkpoint_ref, job_id, run_id, segment_id, target_ref,
                    revision_sha256, identity, private_output_dir,
                    private_points_path, artifact_sha256, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _new_ref("tracking_checkpoint"),
                    run["job_id"],
                    run_id,
                    segment["id"],
                    target_ref,
                    revision_sha256,
                    identity,
                    output_dir,
                    points_path,
                    artifact_sha256,
                    timestamp,
                ),
            )
            ordinal_row = connection.execute(
                """
                SELECT COALESCE(MAX(ordinal), 0) + 1 AS ordinal
                FROM runtime_run_steps WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO runtime_run_steps (
                    run_id, ordinal, safe_step_code, status,
                    artifact_sha256, created_at, updated_at
                ) VALUES (?, ?, 'tracking_target_completed', 'succeeded', ?, ?, ?)
                """,
                (
                    run_id,
                    int(ordinal_row["ordinal"]),
                    artifact_sha256,
                    timestamp,
                    timestamp,
                ),
            )
            return safe

    def complete_tracking(
        self,
        *,
        run_id: int,
        manifest: dict[str, Any],
    ) -> None:
        with self._write() as connection:
            run = self._running_run(connection, run_id)
            job = self._job_row(connection, int(run["job_id"]))
            if job["status"] == "cancelled" or bool(
                job["cancel_requested"],
            ):
                self._finish_run(connection, run_id, "cancelled")
                self._finalize_cancelled_job(
                    connection,
                    int(run["job_id"]),
                    completion_outcome="cancelled_by_user",
                )
                return
            if job["status"] != "tracking":
                raise RuntimeError("tracking run no longer owns a tracking job")
            self._require_runtime_step_ledger(
                connection,
                run_id,
                manifest.get("command_steps"),
            )
            expected_targets = 0
            for segment in connection.execute(
                """
                SELECT id, submitted_revision FROM annotation_segments
                WHERE job_id = ? AND status = 'tracking'
                """,
                (run["job_id"],),
            ).fetchall():
                revision = connection.execute(
                    """
                    SELECT targets_json, content_sha256
                    FROM initial_annotation_revisions
                    WHERE segment_id = ? AND revision_number = ?
                    """,
                    (segment["id"], segment["submitted_revision"]),
                ).fetchone()
                if revision is None:
                    raise RuntimeError("tracking segment revision is missing")
                targets = json.loads(revision["targets_json"])
                expected_targets += len(targets)
                checkpoint_count = connection.execute(
                    """
                    SELECT COUNT(*) AS count FROM tracking_checkpoints
                    WHERE job_id = ? AND segment_id = ? AND revision_sha256 = ?
                    """,
                    (run["job_id"], segment["id"], revision["content_sha256"]),
                ).fetchone()
                if int(checkpoint_count["count"]) != len(targets):
                    raise RuntimeError("tracking checkpoints are incomplete")
            if expected_targets == 0:
                raise RuntimeError("tracking has no submitted targets")
            timestamp = _now()
            connection.execute(
                """
                UPDATE annotation_segments
                SET status = 'tracked', state_revision = state_revision + 1,
                    updated_at = ?
                WHERE job_id = ? AND status = 'tracking'
                """,
                (timestamp, run["job_id"]),
            )
            connection.execute(
                """
                UPDATE annotation_jobs
                SET status = 'tracked', state_revision = state_revision + 1,
                    reserved_bytes = 0, updated_at = ?
                WHERE id = ?
                """,
                (timestamp, run["job_id"]),
            )
            handoff_payload = {
                "job_ref": str(job["job_ref"]),
                "tracked_count": int(
                    connection.execute(
                        """
                        SELECT COUNT(*) AS count
                        FROM annotation_segments
                        WHERE job_id = ? AND status = 'tracked'
                        """,
                        (run["job_id"],),
                    ).fetchone()["count"]
                ),
            }
            connection.execute(
                """
                INSERT INTO workflow_handoffs (
                    handoff_ref, job_id, kind, payload_json,
                    content_sha256, created_at
                ) VALUES (?, ?, 'tracking_completed', ?, ?, ?)
                """,
                (
                    _new_ref("handoff"),
                    run["job_id"],
                    _canonical_json(handoff_payload),
                    _payload_hash(handoff_payload),
                    timestamp,
                ),
            )
            self._insert_manifest(connection, run, "tracking", manifest)
            self._finish_run(connection, run_id, "succeeded")

    def fail_run(
        self,
        *,
        run_id: int,
        code: str,
        message: str,
        retryable: bool,
        private_detail: str | None = None,
    ) -> None:
        with self._write() as connection:
            self._fail_run_conn(
                connection,
                run_id,
                code=code,
                message=message,
                retryable=retryable,
                private_detail=private_detail,
            )

    def complete_cancelled_run(self, *, run_id: int) -> None:
        with self._write() as connection:
            run = connection.execute(
                "SELECT * FROM runtime_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if run is None:
                raise RuntimeError("runtime run not found")
            if run["status"] == "cancelled":
                return
            if run["status"] != "running":
                raise RuntimeError("runtime run is not running")
            job = self._job_row(connection, int(run["job_id"]))
            if not bool(job["cancel_requested"]):
                raise RuntimeError(
                    "runtime cancellation lacks a durable cancel request",
                )
            self._finish_run(connection, run_id, "cancelled")
            self._finalize_cancelled_job(
                connection,
                int(run["job_id"]),
                completion_outcome="cancelled_by_user",
            )

    def first_frame_private(self, job_ref: str, segment_ref: str) -> dict[str, Any]:
        with self._connect() as connection:
            job_id = self._job_id(connection, job_ref)
            segment = self._segment_row(connection, job_id, segment_ref)
            job = self._job_row(connection, job_id)
            return {
                "path": segment["private_first_frame_path"],
                "staging_root": job["staging_root"],
                "sha256": segment["first_frame_sha256"],
                "etag": segment["first_frame_etag"],
                "width": int(segment["first_frame_width"]),
                "height": int(segment["first_frame_height"]),
            }

    @staticmethod
    def _committed_golden_manifest(
        connection: sqlite3.Connection,
        *,
        run_id: int,
        stage: str,
    ) -> tuple[sqlite3.Row, dict[str, Any]]:
        rows = connection.execute(
            """
            SELECT manifest_ref, manifest_json, content_sha256
            FROM artifact_manifests
            WHERE run_id = ? AND stage = ?
            ORDER BY id
            """,
            (run_id, stage),
        ).fetchall()
        if len(rows) != 1:
            raise RuntimeError(
                f"committed {stage} run requires exactly one artifact manifest"
            )
        row = rows[0]
        encoded = row["manifest_json"]
        if (
            not isinstance(encoded, str)
            or hashlib.sha256(encoded.encode("utf-8")).hexdigest()
            != row["content_sha256"]
        ):
            raise RuntimeError(
                f"committed {stage} manifest content hash changed"
            )
        try:
            manifest = json.loads(encoded)
        except (TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"committed {stage} manifest cannot be decoded"
            ) from exc
        if (
            not isinstance(manifest, dict)
            or _canonical_json(manifest) != encoded
        ):
            raise RuntimeError(
                f"committed {stage} manifest is not canonical"
            )
        return row, manifest

    @staticmethod
    def _golden_private_directory(
        value: Any,
        *,
        label: str,
    ) -> Path:
        if not isinstance(value, str) or not value:
            raise RuntimeError(f"{label} is missing")
        path = Path(value)
        if not path.is_absolute():
            raise RuntimeError(f"{label} is not absolute")
        try:
            metadata = path.lstat()
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise RuntimeError(f"{label} is unavailable") from exc
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or resolved != path
        ):
            raise RuntimeError(f"{label} is unsafe")
        return resolved

    @staticmethod
    def _golden_private_file(
        value: Any,
        *,
        label: str,
    ) -> Path:
        if not isinstance(value, str) or not value:
            raise RuntimeError(f"{label} is missing")
        path = Path(value)
        if not path.is_absolute():
            raise RuntimeError(f"{label} is not absolute")
        try:
            metadata = path.lstat()
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise RuntimeError(f"{label} is unavailable") from exc
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or resolved != path
        ):
            raise RuntimeError(f"{label} is unsafe")
        return resolved

    def _postprocessing_golden_ledger(
        self,
        connection: sqlite3.Connection,
        run: sqlite3.Row,
    ) -> dict[str, Any]:
        from vla_data_juicer_agents.annotation.runtime import (
            RuntimeExecutionError,
            _tree_sha256,
            regular_artifact_sha256,
        )

        job_id = int(run["job_id"])
        job = self._job_row(connection, job_id)
        if (
            run["kind"] != "postprocessing"
            or run["status"] != "succeeded"
            or job["status"] != "annotated"
        ):
            raise AnnotationConflictError(
                "runtime_run_not_committed",
                "Runtime attestation requires a succeeded postprocessing run.",
                current=self._job_projection(connection, job_id),
            )
        manifest_row, manifest = self._committed_golden_manifest(
            connection,
            run_id=int(run["id"]),
            stage="postprocessing",
        )
        spec = connection.execute(
            """
            SELECT content_sha256 FROM postprocessing_specs
            WHERE job_id = ?
            """,
            (job_id,),
        ).fetchone()
        calibration = connection.execute(
            """
            SELECT content_sha256 FROM calibration_snapshots
            WHERE id = ?
            """,
            (job["calibration_snapshot_id"],),
        ).fetchone()
        if spec is None or calibration is None:
            raise RuntimeError(
                "postprocessing run lacks its committed spec or calibration"
            )
        if manifest.get("postprocessing_spec_sha256") != spec["content_sha256"]:
            raise RuntimeError(
                "postprocessing manifest differs from its committed spec"
            )
        runtime_manifest_sha256 = manifest.get("runtime_manifest_sha256")
        if (
            not isinstance(runtime_manifest_sha256, str)
            or len(runtime_manifest_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in runtime_manifest_sha256
            )
        ):
            raise RuntimeError(
                "postprocessing manifest lacks a valid Runtime manifest hash"
            )
        command_steps = self._require_runtime_step_ledger(
            connection,
            int(run["id"]),
            manifest.get("command_steps"),
        )
        rows = connection.execute(
            """
            SELECT t.content_sha256 AS trajectory_state_sha256,
                   t.private_state_json, t.private_artifact_path,
                   t.private_compatibility_path, t.artifact_sha256,
                   t.artifact_manifest_ref,
                   s.segment_ref, s.source_clip, s.private_segment_key,
                   s.submitted_revision, s.status AS segment_status,
                   a.content_sha256 AS annotation_revision_sha256
            FROM trajectory_revisions t
            JOIN annotation_segments s ON s.id = t.segment_id
            JOIN initial_annotation_revisions a
              ON a.segment_id = s.id
             AND a.revision_number = s.submitted_revision
            WHERE t.job_id = ? AND t.artifact_manifest_ref = ?
            ORDER BY s.ordinal
            """,
            (job_id, manifest_row["manifest_ref"]),
        ).fetchall()
        if not rows:
            raise RuntimeError(
                "postprocessing manifest lacks committed trajectory revisions"
            )
        revision_set: list[dict[str, str]] = []
        segments: list[dict[str, Any]] = []
        candidate_roots: set[Path] = set()
        for row in rows:
            if row["segment_status"] != "annotated":
                raise RuntimeError(
                    "postprocessing trajectory is not an annotated segment"
                )
            try:
                state = json.loads(row["private_state_json"])
            except (TypeError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    "postprocessing trajectory state cannot be decoded"
                ) from exc
            if (
                not isinstance(state, dict)
                or _payload_hash(state) != row["trajectory_state_sha256"]
            ):
                raise RuntimeError(
                    "postprocessing trajectory state hash changed"
                )
            segment_root = self._golden_private_directory(
                row["private_artifact_path"],
                label="committed postprocessing segment",
            )
            segment_key = str(row["private_segment_key"])
            source_clip = str(row["source_clip"])
            if (
                segment_root.name != segment_key
                or segment_root.parent.name != source_clip
                or segment_root.parent.parent.name != str(job["dataset_date"])
            ):
                raise RuntimeError(
                    "postprocessing trajectory path differs from Store identity"
                )
            compatibility_root = self._golden_private_directory(
                row["private_compatibility_path"],
                label="committed postprocessing publication",
            )
            if (
                compatibility_root.name != segment_key
                or compatibility_root.parent.name != source_clip
                or compatibility_root.parent.parent.name
                != str(job["dataset_date"])
            ):
                raise RuntimeError(
                    "postprocessing publication path differs from Store identity"
                )
            try:
                artifact_sha256 = _tree_sha256(
                    segment_root,
                    unsafe_code="golden_candidate_changed",
                )
            except RuntimeExecutionError as exc:
                raise RuntimeError(
                    "postprocessing candidate failed safety verification"
                ) from exc
            if artifact_sha256 != row["artifact_sha256"]:
                raise RuntimeError(
                    "postprocessing candidate changed after commit"
                )
            trajectory_paths = sorted(
                path
                for path in segment_root.glob("*_trajectory.json")
                if path.is_file()
                and not path.is_symlink()
                and not path.name.endswith("_trajectory_fix_five.json")
            )
            if len(trajectory_paths) != 1:
                raise RuntimeError(
                    "postprocessing candidate lacks one authoritative trajectory"
                )
            try:
                trajectory_sha256 = regular_artifact_sha256(
                    trajectory_paths[0],
                )
            except RuntimeExecutionError as exc:
                raise RuntimeError(
                    "postprocessing trajectory failed safety verification"
                ) from exc
            published_trajectory = compatibility_root / trajectory_paths[0].name
            try:
                published_trajectory_sha256 = regular_artifact_sha256(
                    published_trajectory,
                )
            except RuntimeExecutionError as exc:
                raise RuntimeError(
                    "postprocessing publication failed safety verification"
                ) from exc
            if published_trajectory_sha256 != trajectory_sha256:
                raise RuntimeError(
                    "postprocessing publication differs from committed trajectory"
                )
            revision_set.append(
                {
                    "segment_ref": str(row["segment_ref"]),
                    "annotation_revision_sha256": str(
                        row["annotation_revision_sha256"]
                    ),
                    "trajectory_sha256": trajectory_sha256,
                    "artifact_sha256": artifact_sha256,
                }
            )
            candidate_roots.add(segment_root.parent.parent)
            segments.append(
                {
                    "source_clip": source_clip,
                    "internal_segment": segment_key,
                    "private_artifact_root": segment_root,
                }
            )
        if manifest.get("revision_set") != revision_set:
            raise RuntimeError(
                "postprocessing manifest and trajectory revisions differ"
            )
        if len(candidate_roots) != 1:
            raise RuntimeError(
                "postprocessing candidates do not share one committed root"
            )
        candidate_root = next(iter(candidate_roots))
        try:
            candidate_tree_sha256 = _tree_sha256(
                candidate_root,
                unsafe_code="golden_candidate_changed",
            )
        except RuntimeExecutionError as exc:
            raise RuntimeError(
                "postprocessing candidate root failed safety verification"
            ) from exc
        if manifest.get("candidate_tree_sha256") != candidate_tree_sha256:
            raise RuntimeError(
                "postprocessing candidate root changed after commit"
            )
        publication = manifest.get("publication")
        published_clips = (
            publication.get("source_clips")
            if isinstance(publication, dict)
            else None
        )
        expected_clips = list(
            dict.fromkeys(segment["source_clip"] for segment in segments)
        )
        if (
            published_clips != expected_clips
            or not isinstance(publication.get("journal_sha256"), str)
            or len(publication["journal_sha256"]) != 64
        ):
            raise RuntimeError(
                "postprocessing publication ledger is incomplete"
            )
        return {
            "job": job,
            "source_clips": [
                str(row["source_clip"])
                for row in connection.execute(
                    """
                    SELECT source_clip FROM annotation_job_source_clips
                    WHERE job_id = ? ORDER BY ordinal
                    """,
                    (job_id,),
                ).fetchall()
            ],
            "segments": segments,
            "attestation": {
                "source": "runtime_run",
                "run_ref": str(run["run_ref"]),
                "committed": True,
                "runtime_manifest_sha256": runtime_manifest_sha256,
                "calibration_snapshot_sha256": str(
                    calibration["content_sha256"]
                ),
                "annotation_revision_set_sha256": _payload_hash(
                    revision_set
                ),
                "command_steps": command_steps,
            },
        }

    def _fix_golden_ledger(
        self,
        connection: sqlite3.Connection,
        run: sqlite3.Row,
    ) -> dict[str, Any]:
        from vla_data_juicer_agents.annotation.runtime import (
            RuntimeExecutionError,
            _tree_sha256,
            regular_artifact_sha256,
        )

        job_id = int(run["job_id"])
        job = self._job_row(connection, job_id)
        if run["kind"] != "fix" or run["status"] != "succeeded":
            raise AnnotationConflictError(
                "runtime_run_not_committed",
                "Runtime attestation requires a succeeded Fix run.",
                current=self._job_projection(connection, job_id),
            )
        manifest_row, manifest = self._committed_golden_manifest(
            connection,
            run_id=int(run["id"]),
            stage="fix",
        )
        rows = connection.execute(
            """
            SELECT l.review_id, l.fix_draft_id, l.source_draft_revision,
                   l.planned_revision_ref, l.planned_revision_number,
                   r.review_ref, r.status AS review_status,
                   r.trajectory_revision_id, r.approved_fix_revision_id,
                   d.draft_revision, d.content_sha256 AS draft_sha256,
                   d.base_trajectory_revision_id AS draft_base_revision_id,
                   d.calibration_snapshot_id AS draft_calibration_id,
                   f.id AS fix_revision_id, f.revision_ref AS fix_revision_ref,
                   f.revision_number AS fix_revision_number,
                   f.source_draft_revision AS fix_source_draft_revision,
                   f.state_json AS fix_state_json,
                   f.content_sha256 AS fix_trajectory_sha256,
                   f.private_artifact_path AS fix_private_artifact_path,
                   f.artifact_sha256 AS fix_artifact_sha256,
                   f.artifact_manifest_ref, f.runtime_run_id,
                   f.base_trajectory_revision_id,
                   f.calibration_snapshot_id,
                   c.content_sha256 AS fix_calibration_sha256,
                   t.revision_ref AS trajectory_revision_ref,
                   t.content_sha256 AS trajectory_content_sha256,
                   t.artifact_sha256 AS trajectory_artifact_sha256,
                   s.segment_ref, s.source_clip, s.private_segment_key,
                   p.publication_ref,
                   p.status AS publication_status,
                   p.content_sha256 AS publication_sha256,
                   p.private_artifact_path AS publication_path
            FROM runtime_run_review_links l
            JOIN trajectory_review_tasks r ON r.id = l.review_id
            JOIN trajectory_revisions t ON t.id = r.trajectory_revision_id
            JOIN annotation_segments s ON s.id = t.segment_id
            JOIN fix_drafts d ON d.id = l.fix_draft_id
            JOIN fix_revisions f
              ON f.runtime_run_id = l.run_id
             AND f.review_id = r.id
             AND f.artifact_manifest_ref = ?
            JOIN fix_calibration_snapshots c
              ON c.id = f.calibration_snapshot_id
            JOIN compatibility_publications p
              ON p.review_id = r.id AND p.fix_revision_id = f.id
             AND p.status = 'succeeded'
            WHERE l.run_id = ? AND t.job_id = ?
            """,
            (manifest_row["manifest_ref"], int(run["id"]), job_id),
        ).fetchall()
        if len(rows) != 1:
            raise RuntimeError(
                "Fix run does not resolve one approved committed publication"
            )
        row = rows[0]
        if (
            row["review_status"] != "approved"
            or row["publication_status"] != "succeeded"
            or row["approved_fix_revision_id"] != row["fix_revision_id"]
            or row["runtime_run_id"] != int(run["id"])
            or row["trajectory_revision_id"]
            != row["base_trajectory_revision_id"]
            or row["trajectory_revision_id"] != row["draft_base_revision_id"]
            or row["calibration_snapshot_id"] != row["draft_calibration_id"]
            or row["planned_revision_ref"] != row["fix_revision_ref"]
            or row["planned_revision_number"] != row["fix_revision_number"]
            or row["source_draft_revision"]
            != row["fix_source_draft_revision"]
            or row["draft_revision"] < row["source_draft_revision"]
        ):
            raise RuntimeError(
                "Fix revision lineage differs from its committed run"
            )
        try:
            fix_state = json.loads(row["fix_state_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Fix revision state cannot be decoded") from exc
        if (
            not isinstance(fix_state, dict)
            or _payload_hash(fix_state) != manifest.get("draft_sha256")
            or (
                row["draft_revision"] == row["source_draft_revision"]
                and row["draft_sha256"] != manifest.get("draft_sha256")
            )
        ):
            raise RuntimeError("Fix revision state hash changed")
        candidate_root = self._golden_private_directory(
            row["fix_private_artifact_path"],
            label="committed Fix candidate",
        )
        try:
            candidate_tree_sha256 = _tree_sha256(
                candidate_root,
                unsafe_code="golden_candidate_changed",
            )
        except RuntimeExecutionError as exc:
            raise RuntimeError(
                "Fix candidate failed safety verification"
            ) from exc
        if (
            candidate_tree_sha256 != row["fix_artifact_sha256"]
            or manifest.get("candidate_tree_sha256")
            != candidate_tree_sha256
        ):
            raise RuntimeError("Fix candidate changed after commit")
        candidate_paths = sorted(
            path
            for path in candidate_root.glob("*_trajectory_fix_five.json")
            if path.is_file() and not path.is_symlink()
        )
        if len(candidate_paths) != 1:
            raise RuntimeError(
                "Fix candidate lacks one authoritative compatibility trajectory"
            )
        try:
            fix_trajectory_sha256 = regular_artifact_sha256(
                candidate_paths[0],
            )
        except RuntimeExecutionError as exc:
            raise RuntimeError(
                "Fix candidate trajectory failed safety verification"
            ) from exc
        published_path = self._golden_private_file(
            row["publication_path"],
            label="committed Fix publication",
        )
        if (
            not published_path.name.endswith("_trajectory_fix_five.json")
            or published_path.name != candidate_paths[0].name
            or published_path.parent.name != row["private_segment_key"]
            or published_path.parent.parent.name != row["source_clip"]
            or published_path.parent.parent.parent.name
            != str(job["dataset_date"])
        ):
            raise RuntimeError(
                "Fix publication path differs from Store identity"
            )
        try:
            publication_sha256 = regular_artifact_sha256(published_path)
        except RuntimeExecutionError as exc:
            raise RuntimeError(
                "Fix publication failed safety verification"
            ) from exc
        if (
            publication_sha256 != row["publication_sha256"]
            or publication_sha256 != fix_trajectory_sha256
            or fix_trajectory_sha256 != row["fix_trajectory_sha256"]
            or manifest.get("fix_trajectory_sha256")
            != fix_trajectory_sha256
        ):
            raise RuntimeError(
                "Fix manifest, revision, and publication hashes differ"
            )
        runtime_manifest_sha256 = manifest.get("runtime_manifest_sha256")
        if (
            not isinstance(runtime_manifest_sha256, str)
            or len(runtime_manifest_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in runtime_manifest_sha256
            )
            or manifest.get("calibration_snapshot_sha256")
            != row["fix_calibration_sha256"]
            or manifest.get("trajectory_revision_ref")
            != row["trajectory_revision_ref"]
            or manifest.get("base_tree_sha256")
            != row["trajectory_artifact_sha256"]
        ):
            raise RuntimeError(
                "Fix Runtime or calibration attestation is invalid"
            )
        commands = fix_state.get("commands")
        if (
            not isinstance(commands, list)
            or manifest.get("command_log_sha256") != _payload_hash(commands)
            or not _valid_sha256(manifest.get("adapter_sha256"))
        ):
            raise RuntimeError("Fix command or adapter attestation is invalid")
        command_steps = self._require_runtime_step_ledger(
            connection,
            int(run["id"]),
            manifest.get("command_steps"),
        )
        manifest_revision_set = [
            {
                "review_ref": str(row["review_ref"]),
                "segment_ref": str(row["segment_ref"]),
                "planned_revision_ref": str(row["fix_revision_ref"]),
                "source_draft_revision": int(
                    row["source_draft_revision"]
                ),
            }
        ]
        if manifest.get("revision_set") != manifest_revision_set:
            raise RuntimeError("Fix manifest and revision lineage differ")
        revision_set = [
            {
                "segment_ref": str(row["segment_ref"]),
                "trajectory_revision_ref": str(
                    row["trajectory_revision_ref"]
                ),
                "trajectory_content_sha256": str(
                    row["trajectory_content_sha256"]
                ),
                "fix_revision_ref": str(row["fix_revision_ref"]),
                "fix_content_sha256": fix_trajectory_sha256,
                "publication_sha256": publication_sha256,
            }
        ]
        return {
            "job": job,
            "source_clips": [
                str(item["source_clip"])
                for item in connection.execute(
                    """
                    SELECT source_clip FROM annotation_job_source_clips
                    WHERE job_id = ? ORDER BY ordinal
                    """,
                    (job_id,),
                ).fetchall()
            ],
            "segments": [
                {
                    "source_clip": str(row["source_clip"]),
                    "internal_segment": str(row["private_segment_key"]),
                    "private_artifact_root": published_path.parent,
                }
            ],
            "attestation": {
                "source": "runtime_run",
                "run_ref": str(run["run_ref"]),
                "committed": True,
                "runtime_manifest_sha256": runtime_manifest_sha256,
                "calibration_snapshot_sha256": str(
                    row["fix_calibration_sha256"]
                ),
                "annotation_revision_set_sha256": _payload_hash(
                    revision_set
                ),
                "command_steps": command_steps,
            },
        }

    def runtime_run_attestation(self, run_ref: str) -> dict[str, Any]:
        """Return the safe Golden attestation derived only from committed rows."""

        from vla_data_juicer_agents.annotation.legacy_yaml import (
            LegacyYamlAdapter,
        )

        with self._connect() as connection:
            run = connection.execute(
                """
                SELECT * FROM runtime_runs WHERE run_ref = ?
                """,
                (run_ref,),
            ).fetchone()
            if run is None:
                raise AnnotationNotFoundError("annotation runtime run not found")
            if run["kind"] == "postprocessing":
                return self._postprocessing_golden_ledger(
                    connection,
                    run,
                )["attestation"]
            if run["kind"] == "fix":
                return self._fix_golden_ledger(
                    connection,
                    run,
                )["attestation"]

        with self._connect() as connection:
            tracking_run = connection.execute(
                """
                SELECT * FROM runtime_runs WHERE run_ref = ?
                """,
                (run_ref,),
            ).fetchone()
            if tracking_run is None:
                raise AnnotationNotFoundError("annotation runtime run not found")
            job_id = int(tracking_run["job_id"])
            job = self._job_row(connection, job_id)
            if (
                tracking_run["kind"] != "tracking"
                or tracking_run["status"] != "succeeded"
                or job["status"] != "tracked"
            ):
                raise AnnotationConflictError(
                    "runtime_run_not_committed",
                    "Runtime attestation requires a succeeded Tracking run.",
                    current=self._job_projection(connection, job_id),
                )
            prepare_manifest_row = connection.execute(
                """
                SELECT a.manifest_json, a.content_sha256, r.id AS run_id
                FROM artifact_manifests a
                JOIN runtime_runs r ON r.id = a.run_id
                WHERE a.job_id = ? AND a.stage = 'prepare'
                  AND r.status = 'succeeded'
                ORDER BY a.id DESC LIMIT 1
                """,
                (job_id,),
            ).fetchone()
            tracking_manifest_row = connection.execute(
                """
                SELECT manifest_json, content_sha256 FROM artifact_manifests
                WHERE run_id = ? AND stage = 'tracking'
                ORDER BY id DESC LIMIT 1
                """,
                (tracking_run["id"],),
            ).fetchone()
            calibration = connection.execute(
                """
                SELECT content_sha256 FROM calibration_snapshots
                WHERE id = ?
                """,
                (job["calibration_snapshot_id"],),
            ).fetchone()
            if (
                prepare_manifest_row is None
                or tracking_manifest_row is None
                or calibration is None
            ):
                raise RuntimeError("tracked job has an incomplete committed ledger")
            for manifest_row in (
                prepare_manifest_row,
                tracking_manifest_row,
            ):
                encoded_manifest = manifest_row["manifest_json"]
                if (
                    not isinstance(encoded_manifest, str)
                    or hashlib.sha256(
                        encoded_manifest.encode("utf-8"),
                    ).hexdigest()
                    != manifest_row["content_sha256"]
                ):
                    raise RuntimeError(
                        "committed Runtime manifest content hash changed",
                    )
            prepare_manifest = json.loads(prepare_manifest_row["manifest_json"])
            tracking_manifest = json.loads(tracking_manifest_row["manifest_json"])
            runtime_manifest_sha256 = prepare_manifest.get(
                "runtime_manifest_sha256"
            )
            if not isinstance(runtime_manifest_sha256, str):
                raise RuntimeError("prepare manifest lacks runtime manifest hash")
            tracking_runtime_manifest_sha256 = tracking_manifest.get(
                "runtime_manifest_sha256",
            )
            if (
                not isinstance(tracking_runtime_manifest_sha256, str)
                or tracking_runtime_manifest_sha256
                != runtime_manifest_sha256
            ):
                raise RuntimeError(
                    "prepare and Tracking Runtime manifest hashes differ",
                )
            prepared_artifact_tree_sha256 = prepare_manifest.get(
                "prepared_artifact_tree_sha256",
            )
            tracking_prepared_artifact_tree_sha256 = tracking_manifest.get(
                "prepared_artifact_tree_sha256",
            )
            if (
                not isinstance(prepared_artifact_tree_sha256, str)
                or tracking_prepared_artifact_tree_sha256
                != prepared_artifact_tree_sha256
            ):
                raise RuntimeError(
                    "prepare and Tracking artifact hashes differ",
                )
            revision_set: list[dict[str, Any]] = []
            checkpoint_entries: list[
                tuple[tuple[str, str], dict[str, str]]
            ] = []
            yaml_adapter = LegacyYamlAdapter()
            for segment in connection.execute(
                """
                SELECT id, segment_ref, submitted_revision,
                       private_segment_root
                FROM annotation_segments
                WHERE job_id = ? AND status = 'tracked'
                ORDER BY ordinal
                """,
                (job_id,),
            ).fetchall():
                revision = connection.execute(
                    """
                    SELECT content_sha256, targets_json
                    FROM initial_annotation_revisions
                    WHERE segment_id = ? AND revision_number = ?
                    """,
                    (segment["id"], segment["submitted_revision"]),
                ).fetchone()
                if revision is None:
                    raise RuntimeError("tracked segment revision is missing")
                submitted_actions = connection.execute(
                    """
                    SELECT safe_payload_json
                    FROM annotation_segment_actions
                    WHERE segment_id = ? AND action = 'submitted'
                    ORDER BY id
                    """,
                    (segment["id"],),
                ).fetchall()
                submitted_revision = int(segment["submitted_revision"])
                if not any(
                    json.loads(row["safe_payload_json"]).get("revision")
                    == submitted_revision
                    for row in submitted_actions
                ):
                    raise RuntimeError(
                        "tracked segment lacks its committed submit action"
                    )
                try:
                    targets = json.loads(revision["targets_json"])
                    if _payload_hash(targets) != revision["content_sha256"]:
                        raise RuntimeError(
                            "tracked annotation revision content hash changed",
                        )
                    rendered = yaml_adapter.render(
                        Path(segment["private_segment_root"]),
                        [
                            {
                                "target_ref": target["target_ref"],
                                "bbox": target["bbox"],
                                "point": target["point"],
                                "upper_color": target["colors"]["upper"],
                                "lower_color": target["colors"]["lower"],
                                "shoes_color": target["colors"]["shoes"],
                            }
                            for target in targets
                        ],
                    )
                except Exception as exc:
                    raise RuntimeError(
                        "tracked segment revision cannot be re-rendered",
                    ) from exc
                for rendered_target in rendered:
                    checkpoints = connection.execute(
                        """
                        SELECT c.target_ref, c.identity, c.artifact_sha256
                        FROM tracking_checkpoints c
                        JOIN runtime_runs r ON r.id = c.run_id
                        WHERE c.job_id = ? AND c.segment_id = ?
                          AND c.target_ref = ? AND c.revision_sha256 = ?
                          AND r.job_id = c.job_id AND r.kind = 'tracking'
                          AND EXISTS (
                              SELECT 1 FROM runtime_run_steps step
                              WHERE step.run_id = c.run_id
                                AND step.safe_step_code =
                                    'tracking_target_completed'
                                AND step.status = 'succeeded'
                                AND step.artifact_sha256 = c.artifact_sha256
                          )
                        """,
                        (
                            job_id,
                            segment["id"],
                            rendered_target.target_ref,
                            revision["content_sha256"],
                        ),
                    ).fetchall()
                    if len(checkpoints) != 1:
                        raise RuntimeError(
                            "tracked target lacks one authoritative checkpoint",
                        )
                    checkpoint = checkpoints[0]
                    expected_identity = Path(
                        rendered_target.filename,
                    ).stem
                    if checkpoint["identity"] != expected_identity:
                        raise RuntimeError(
                            "tracked checkpoint identity differs from revision",
                        )
                    checkpoint_entries.append(
                        (
                            (
                                str(segment["private_segment_root"]),
                                rendered_target.filename,
                            ),
                            {
                                "segment_ref": str(segment["segment_ref"]),
                                "target_ref": str(
                                    rendered_target.target_ref,
                                ),
                                "identity": str(checkpoint["identity"]),
                                "artifact_sha256": str(
                                    checkpoint["artifact_sha256"],
                                ),
                            },
                        )
                    )
                revision_set.append(
                    {
                        "segment_ref": segment["segment_ref"],
                        "revision": submitted_revision,
                        "sha256": revision["content_sha256"],
                    }
                )
            completed_target_step_count = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM runtime_run_steps step
                JOIN runtime_runs r ON r.id = step.run_id
                WHERE r.job_id = ? AND r.kind = 'tracking'
                  AND step.safe_step_code = 'tracking_target_completed'
                  AND step.status = 'succeeded'
                """,
                (job_id,),
            ).fetchone()
            if int(completed_target_step_count["count"]) != len(
                checkpoint_entries,
            ):
                raise RuntimeError(
                    "Tracking checkpoint completion ledger is inconsistent",
                )
            if tracking_manifest.get("revision_set") != revision_set:
                raise RuntimeError(
                    "Tracking manifest and committed annotation revisions differ",
                )
            checkpoint_ledger = [
                checkpoint
                for _sort_key, checkpoint in sorted(
                    checkpoint_entries,
                    key=lambda item: item[0],
                )
            ]
            declared_checkpoints = tracking_manifest.get("checkpoints")
            if declared_checkpoints != checkpoint_ledger:
                raise RuntimeError(
                    "Tracking manifest and committed checkpoints differ",
                )
            command_steps = self._require_runtime_step_ledger(
                connection,
                int(prepare_manifest_row["run_id"]),
                prepare_manifest.get("command_steps"),
            )
            command_steps.extend(
                self._require_runtime_step_ledger(
                    connection,
                    int(tracking_run["id"]),
                    tracking_manifest.get("command_steps"),
                )
            )
            if (
                not command_steps
                or len(command_steps) != len(set(command_steps))
                or any(not isinstance(step, str) for step in command_steps)
            ):
                raise RuntimeError("runtime command ledger is invalid")
            return {
                "source": "runtime_run",
                "run_ref": tracking_run["run_ref"],
                "committed": True,
                "runtime_manifest_sha256": runtime_manifest_sha256,
                "calibration_snapshot_sha256": calibration["content_sha256"],
                "annotation_revision_set_sha256": _payload_hash(revision_set),
                "command_steps": command_steps,
            }

    def _m2_golden_candidate_binding(
        self,
        *,
        run_ref: str,
        dataset_date: str,
        source_clip: str,
        internal_segment: str | None,
        scope_kind: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            run = connection.execute(
                """
                SELECT * FROM runtime_runs WHERE run_ref = ?
                """,
                (run_ref,),
            ).fetchone()
            if run is None:
                raise AnnotationNotFoundError("annotation runtime run not found")
            if scope_kind == "postprocessing_segment":
                ledger = self._postprocessing_golden_ledger(
                    connection,
                    run,
                )
            elif scope_kind == "fix_segment":
                ledger = self._fix_golden_ledger(connection, run)
            else:
                raise AnnotationConflictError(
                    "golden_candidate_scope_mismatch",
                    "Golden case artifact scope is not supported.",
                )
            job = ledger["job"]
            source_clips = ledger["source_clips"]
            if (
                str(job["dataset_date"]) != dataset_date
                or source_clip not in source_clips
                or internal_segment is None
            ):
                raise AnnotationConflictError(
                    "golden_candidate_scope_mismatch",
                    "Golden case date, clip, or segment is not owned by this run.",
                )
            clip_segments = [
                segment
                for segment in ledger["segments"]
                if segment["source_clip"] == source_clip
            ]
            selected = [
                segment
                for segment in clip_segments
                if segment["internal_segment"] == internal_segment
            ]
            if len(selected) != 1 or not clip_segments:
                raise AnnotationConflictError(
                    "golden_candidate_scope_mismatch",
                    "Golden case segment is not owned by this run and clip.",
                )
            staging_roots = {
                segment["private_artifact_root"].parent
                for segment in clip_segments
            }
            if len(staging_roots) != 1:
                raise RuntimeError(
                    "committed M2 candidate segments do not share one clip root"
                )
            staging_root = next(iter(staging_roots))
            segments = [
                {
                    "source_clip": str(segment["source_clip"]),
                    "internal_segment": str(segment["internal_segment"]),
                    "artifact_scope": str(segment["internal_segment"]),
                }
                for segment in clip_segments
            ]
            return {
                "source": "annotation_store",
                "run_ref": run_ref,
                "dataset_date": dataset_date,
                "source_clips": source_clips,
                "source_clip": source_clip,
                "scope_kind": scope_kind,
                "internal_segment": internal_segment,
                "staging_root": str(staging_root),
                "artifact_scope": internal_segment,
                "segments": segments,
                "attestation": ledger["attestation"],
            }

    def golden_candidate_binding(
        self,
        *,
        run_ref: str,
        dataset_date: str,
        source_clip: str,
        internal_segment: str | None,
        scope_kind: str = "segment",
    ) -> dict[str, Any]:
        """Bind one Golden case to the private tree owned by a committed run.

        Paths and internal segment names in this projection are process-local
        inputs to the comparator.  Public reports consume only ``attestation``.
        """

        if scope_kind in {"postprocessing_segment", "fix_segment"}:
            return self._m2_golden_candidate_binding(
                run_ref=run_ref,
                dataset_date=dataset_date,
                source_clip=source_clip,
                internal_segment=internal_segment,
                scope_kind=scope_kind,
            )

        from vla_data_juicer_agents.annotation.legacy_yaml import (
            LegacyYamlAdapter,
        )
        from vla_data_juicer_agents.annotation.runtime import (
            RuntimeExecutionError,
            TrackingTarget,
            prepared_staging_artifact_sha256,
            regular_artifact_sha256,
            tracking_checkpoint_artifact_sha256,
        )

        attestation = self.runtime_run_attestation(run_ref)
        with self._connect() as connection:
            run = connection.execute(
                """
                SELECT id, job_id, kind, status
                FROM runtime_runs
                WHERE run_ref = ?
                """,
                (run_ref,),
            ).fetchone()
            if run is None:
                raise AnnotationNotFoundError("annotation runtime run not found")
            job = self._job_row(connection, int(run["job_id"]))
            if (
                run["kind"] != "tracking"
                or run["status"] != "succeeded"
                or job["status"] != "tracked"
            ):
                raise AnnotationConflictError(
                    "runtime_run_not_committed",
                    "Golden comparison requires a succeeded Tracking run.",
                )
            clips = [
                str(row["source_clip"])
                for row in connection.execute(
                    """
                    SELECT source_clip
                    FROM annotation_job_source_clips
                    WHERE job_id = ?
                    ORDER BY ordinal
                    """,
                    (run["job_id"],),
                ).fetchall()
            ]
            if (
                str(job["dataset_date"]) != dataset_date
                or source_clip not in clips
            ):
                raise AnnotationConflictError(
                    "golden_candidate_scope_mismatch",
                    "Golden case date or source clip is not owned by this run.",
                )
            rows = connection.execute(
                """
                SELECT id, segment_ref, source_clip, submitted_revision,
                       private_segment_key, private_segment_root
                FROM annotation_segments
                WHERE job_id = ? AND status = 'tracked'
                ORDER BY ordinal
                """,
                (run["job_id"],),
            ).fetchall()
            if scope_kind not in {
                "segment",
                "prepare_maps",
                "prepare_metadata",
            }:
                raise AnnotationConflictError(
                    "golden_candidate_scope_mismatch",
                    "Golden case artifact scope is not supported.",
                )
            if scope_kind == "segment":
                selected = [
                    row
                    for row in rows
                    if str(row["private_segment_key"]) == internal_segment
                ]
                if (
                    internal_segment is None
                    or len(selected) != 1
                    or str(selected[0]["source_clip"]) != source_clip
                ):
                    raise AnnotationConflictError(
                        "golden_candidate_scope_mismatch",
                        "Golden case segment is not owned by this run and clip.",
                    )
            elif internal_segment is not None:
                raise AnnotationConflictError(
                    "golden_candidate_scope_mismatch",
                    "Golden prepare-global scope cannot select a segment.",
                )
            raw_staging_root = job["staging_root"]
            if not isinstance(raw_staging_root, str) or not raw_staging_root:
                raise RuntimeError(
                    "committed Tracking job lacks a private staging root",
                )
            staging_path = Path(raw_staging_root)
            if not staging_path.is_absolute():
                raise RuntimeError(
                    "committed Tracking staging root is invalid",
                )
            try:
                staging_metadata = staging_path.lstat()
                staging_root = staging_path.resolve(strict=True)
            except OSError as exc:
                raise RuntimeError(
                    "committed Tracking staging root is unavailable",
                ) from exc
            if (
                stat.S_ISLNK(staging_metadata.st_mode)
                or not stat.S_ISDIR(staging_metadata.st_mode)
            ):
                raise RuntimeError(
                    "committed Tracking staging root is unsafe",
                )

            prepare_manifest_row = connection.execute(
                """
                SELECT a.manifest_json
                FROM artifact_manifests a
                JOIN runtime_runs r ON r.id = a.run_id
                WHERE a.job_id = ? AND a.stage = 'prepare'
                  AND r.status = 'succeeded'
                ORDER BY a.id DESC LIMIT 1
                """,
                (run["job_id"],),
            ).fetchone()
            if prepare_manifest_row is None:
                raise RuntimeError(
                    "committed Tracking job lacks its prepare manifest",
                )
            prepare_manifest = json.loads(
                prepare_manifest_row["manifest_json"],
            )
            expected_prepared_sha256 = prepare_manifest.get(
                "prepared_artifact_tree_sha256",
            )
            if (
                not isinstance(expected_prepared_sha256, str)
                or len(expected_prepared_sha256) != 64
            ):
                raise RuntimeError(
                    "committed prepare manifest lacks its artifact hash",
                )

            segments: list[dict[str, str]] = []
            runtime_targets: list[TrackingTarget] = []
            yaml_adapter = LegacyYamlAdapter()
            for row in rows:
                segment_key = str(row["private_segment_key"])
                segment_clip = str(row["source_clip"])
                raw_segment_root = row["private_segment_root"]
                if (
                    not isinstance(raw_segment_root, str)
                    or not raw_segment_root
                    or not Path(raw_segment_root).is_absolute()
                ):
                    raise RuntimeError(
                        "committed Tracking segment mapping is invalid",
                    )
                segment_path = Path(raw_segment_root)
                try:
                    lexical_relative = segment_path.relative_to(staging_path)
                    if (
                        not lexical_relative.parts
                        or any(
                            component in {".", ".."}
                            for component in lexical_relative.parts
                        )
                    ):
                        raise ValueError
                    current = staging_path
                    for component in lexical_relative.parts:
                        current = current / component
                        metadata = current.lstat()
                        if (
                            stat.S_ISLNK(metadata.st_mode)
                            or not stat.S_ISDIR(metadata.st_mode)
                        ):
                            raise RuntimeError(
                                "committed Tracking segment mapping is unsafe",
                            )
                    segment_root = segment_path.resolve(strict=True)
                    relative = segment_root.relative_to(staging_root)
                except (OSError, ValueError) as exc:
                    raise RuntimeError(
                        "committed Tracking segment escapes its staging root",
                    ) from exc
                if (
                    not relative.parts
                    or relative.name != segment_key
                ):
                    raise RuntimeError(
                        "committed Tracking segment mapping is inconsistent",
                    )
                revision = connection.execute(
                    """
                    SELECT content_sha256, targets_json
                    FROM initial_annotation_revisions
                    WHERE segment_id = ? AND revision_number = ?
                    """,
                    (row["id"], row["submitted_revision"]),
                ).fetchone()
                if revision is None:
                    raise RuntimeError(
                        "committed Tracking segment revision is missing",
                    )
                try:
                    targets = json.loads(revision["targets_json"])
                    if _payload_hash(targets) != revision["content_sha256"]:
                        raise RuntimeError(
                            "committed annotation revision content hash changed",
                        )
                    target_payloads = [
                        {
                            "target_ref": target["target_ref"],
                            "bbox": target["bbox"],
                            "point": target["point"],
                            "upper_color": target["colors"]["upper"],
                            "lower_color": target["colors"]["lower"],
                            "shoes_color": target["colors"]["shoes"],
                        }
                        for target in targets
                    ]
                    rendered = yaml_adapter.render(
                        segment_root,
                        target_payloads,
                    )
                except Exception as exc:
                    raise RuntimeError(
                        "committed annotation revision cannot be re-rendered",
                    ) from exc
                checkpoints = connection.execute(
                    """
                    SELECT target_ref, revision_sha256, identity,
                           private_output_dir, private_points_path,
                           artifact_sha256
                    FROM tracking_checkpoints c
                    JOIN runtime_runs r ON r.id = c.run_id
                    WHERE c.job_id = ? AND c.segment_id = ?
                      AND c.revision_sha256 = ?
                      AND r.job_id = c.job_id AND r.kind = 'tracking'
                    ORDER BY c.id
                    """,
                    (
                        run["job_id"],
                        row["id"],
                        revision["content_sha256"],
                    ),
                ).fetchall()
                if len(checkpoints) != len(rendered):
                    raise RuntimeError(
                        "committed Tracking checkpoint set is incomplete",
                    )
                checkpoints_by_target = {
                    str(checkpoint["target_ref"]): checkpoint
                    for checkpoint in checkpoints
                }
                if len(checkpoints_by_target) != len(checkpoints):
                    raise RuntimeError(
                        "committed Tracking checkpoint targets are duplicated",
                    )
                try:
                    for rendered_target in rendered:
                        identity = Path(rendered_target.filename).stem
                        yaml_path = segment_root / rendered_target.filename
                        if (
                            regular_artifact_sha256(yaml_path)
                            != rendered_target.sha256
                        ):
                            raise RuntimeError(
                                "committed Tracking YAML changed after completion",
                            )
                        runtime_target = TrackingTarget(
                            segment_root=segment_root,
                            yaml_path=yaml_path,
                            identity=identity,
                            expected_yaml_sha256=rendered_target.sha256,
                        )
                        runtime_targets.append(runtime_target)
                        checkpoint = checkpoints_by_target.get(
                            rendered_target.target_ref,
                        )
                        if checkpoint is None:
                            raise RuntimeError(
                                "committed Tracking target lacks a checkpoint",
                            )
                        expected_output = (
                            segment_root / f"tracking_img_{identity}"
                        )
                        expected_points = (
                            segment_root / f"img_{identity}.txt"
                        )
                        if (
                            checkpoint["revision_sha256"]
                            != revision["content_sha256"]
                            or checkpoint["identity"] != identity
                            or checkpoint["private_output_dir"]
                            != str(expected_output)
                            or checkpoint["private_points_path"]
                            != str(expected_points)
                        ):
                            raise RuntimeError(
                                "committed Tracking checkpoint mapping changed",
                            )
                        if (
                            tracking_checkpoint_artifact_sha256(
                                expected_output,
                                expected_points,
                            )
                            != checkpoint["artifact_sha256"]
                        ):
                            raise RuntimeError(
                                "committed Tracking checkpoint artifacts changed",
                            )
                except RuntimeExecutionError as exc:
                    raise RuntimeError(
                        "committed Tracking artifacts failed safety verification",
                    ) from exc
                segments.append(
                    {
                        "source_clip": segment_clip,
                        "internal_segment": segment_key,
                        "artifact_scope": relative.as_posix(),
                    },
                )
            try:
                current_prepared_sha256 = prepared_staging_artifact_sha256(
                    staging_root,
                    tuple(runtime_targets),
                )
            except RuntimeExecutionError as exc:
                raise RuntimeError(
                    "committed prepare artifacts failed safety verification",
                ) from exc
            if current_prepared_sha256 != expected_prepared_sha256:
                raise RuntimeError(
                    "committed prepare artifacts changed after completion",
                )
            if scope_kind == "segment":
                selected_scope = next(
                    segment["artifact_scope"]
                    for segment in segments
                    if segment["internal_segment"] == internal_segment
                )
            else:
                selected_scope = {
                    "prepare_maps": "maps",
                    "prepare_metadata": "v1.0-trainval",
                }[scope_kind]
                expected_top_level = {
                    ".runtime",
                    "maps",
                    "samples",
                    "v1.0-trainval",
                }
                try:
                    top_level = {
                        child.name: child.lstat()
                        for child in staging_root.iterdir()
                    }
                except OSError as exc:
                    raise RuntimeError(
                        "committed prepare-global artifacts are unavailable",
                    ) from exc
                if set(top_level) != expected_top_level or any(
                    stat.S_ISLNK(metadata.st_mode)
                    or not stat.S_ISDIR(metadata.st_mode)
                    for metadata in top_level.values()
                ):
                    raise RuntimeError(
                        "committed prepare-global artifact roots changed",
                    )
                expected_segment_scopes = {
                    f"samples/{dataset_date}/{segment['internal_segment']}"
                    for segment in segments
                }
                actual_segment_scopes = {
                    segment["artifact_scope"]
                    for segment in segments
                }
                if actual_segment_scopes != expected_segment_scopes:
                    raise RuntimeError(
                        "committed prepare-global segment layout is inconsistent",
                    )
                samples_date = staging_root / "samples" / dataset_date
                try:
                    sample_dates = {
                        child.name: child.lstat()
                        for child in (staging_root / "samples").iterdir()
                    }
                    sample_segments = {
                        child.name: child.lstat()
                        for child in samples_date.iterdir()
                    }
                except OSError as exc:
                    raise RuntimeError(
                        "committed prepare-global sample layout is unavailable",
                    ) from exc
                if (
                    set(sample_dates) != {dataset_date}
                    or any(
                        stat.S_ISLNK(metadata.st_mode)
                        or not stat.S_ISDIR(metadata.st_mode)
                        for metadata in sample_dates.values()
                    )
                    or set(sample_segments)
                    != {
                        segment["internal_segment"]
                        for segment in segments
                    }
                    or any(
                        stat.S_ISLNK(metadata.st_mode)
                        or not stat.S_ISDIR(metadata.st_mode)
                        for metadata in sample_segments.values()
                    )
                ):
                    raise RuntimeError(
                        "committed prepare-global sample roots changed",
                    )
            return {
                "source": "annotation_store",
                "run_ref": run_ref,
                "dataset_date": dataset_date,
                "source_clips": clips,
                "source_clip": source_clip,
                "scope_kind": scope_kind,
                "internal_segment": internal_segment,
                "staging_root": str(staging_root),
                "artifact_scope": selected_scope,
                "segments": segments,
                "attestation": attestation,
            }

    def _review_projection(
        self,
        connection: sqlite3.Connection,
        review_id: int,
    ) -> dict[str, Any]:
        row = connection.execute(
            """
            SELECT r.*, t.revision_ref AS trajectory_revision_ref,
                   t.content_sha256 AS trajectory_content_sha256,
                   j.job_ref, j.dataset_date,
                   s.segment_ref, s.ordinal AS segment_ordinal, s.source_clip,
                   c.profile_ref AS processing_profile_ref,
                   c.label AS processing_profile_label,
                   c.content_sha256 AS processing_calibration_sha256
            FROM trajectory_review_tasks r
            JOIN trajectory_revisions t ON t.id = r.trajectory_revision_id
            JOIN annotation_jobs j ON j.id = t.job_id
            JOIN annotation_segments s ON s.id = t.segment_id
            JOIN calibration_snapshots c ON c.id = j.calibration_snapshot_id
            WHERE r.id = ?
            """,
            (review_id,),
        ).fetchone()
        if row is None:
            raise AnnotationNotFoundError("trajectory review not found")
        draft = connection.execute(
            """
            SELECT d.draft_ref, d.draft_revision, d.content_sha256,
                   c.profile_ref, c.label, c.content_sha256 AS calibration_sha256,
                   c.differs_from_processing, c.difference_reason
            FROM fix_drafts d
            JOIN fix_calibration_snapshots c ON c.id = d.calibration_snapshot_id
            WHERE d.id = ?
            """,
            (row["active_fix_draft_id"],),
        ).fetchone() if row["active_fix_draft_id"] is not None else None
        revisions = [
            {
                "revision_ref": revision["revision_ref"],
                "revision_number": int(revision["revision_number"]),
                "source_draft_revision": int(revision["source_draft_revision"]),
                "content_sha256": revision["content_sha256"],
                "created_at": revision["created_at"],
            }
            for revision in connection.execute(
                """
                SELECT revision_ref, revision_number, source_draft_revision,
                       content_sha256, created_at
                FROM fix_revisions
                WHERE review_id = ?
                ORDER BY revision_number
                """,
                (review_id,),
            ).fetchall()
        ]
        publication = connection.execute(
            """
            SELECT p.publication_ref, p.attempt, p.status, p.content_sha256,
                   p.failure_code, p.failure_ref, p.created_at,
                   f.revision_ref AS fix_revision_ref
            FROM compatibility_publications p
            JOIN fix_revisions f ON f.id = p.fix_revision_id
            WHERE p.review_id = ?
            ORDER BY p.attempt DESC
            LIMIT 1
            """,
            (review_id,),
        ).fetchone()
        fix_run = connection.execute(
            """
            SELECT rr.run_ref, rr.status, rr.failure_code,
                   rr.failure_ref, rr.created_at, rr.updated_at
            FROM runtime_run_review_links l
            JOIN runtime_runs rr ON rr.id = l.run_id
            WHERE l.review_id = ?
            ORDER BY rr.id DESC LIMIT 1
            """,
            (review_id,),
        ).fetchone()
        result: dict[str, Any] = {
            "review_ref": row["review_ref"],
            "status": row["status"],
            "state_revision": int(row["state_revision"]),
            "job_ref": row["job_ref"],
            "dataset_date": row["dataset_date"],
            "source_clip": row["source_clip"],
            "segment_ref": row["segment_ref"],
            "segment_ordinal": int(row["segment_ordinal"]),
            "trajectory_revision": {
                "revision_ref": row["trajectory_revision_ref"],
                "content_sha256": row["trajectory_content_sha256"],
            },
            "processing_calibration": {
                "profile_ref": row["processing_profile_ref"],
                "label": row["processing_profile_label"],
                "content_sha256": row["processing_calibration_sha256"],
            },
            "fix_draft": (
                {
                    "revision": int(draft["draft_revision"]),
                    "content_sha256": draft["content_sha256"],
                    "calibration": {
                        "profile_ref": draft["profile_ref"],
                        "label": draft["label"],
                        "content_sha256": draft["calibration_sha256"],
                        "differs_from_processing": bool(
                            draft["differs_from_processing"]
                        ),
                        "difference_reason": draft["difference_reason"],
                    },
                }
                if draft is not None
                else None
            ),
            "fix_revisions": revisions,
            "active_fix_run": (
                {
                    "status": fix_run["status"],
                    "failure": (
                        {
                            "code": fix_run["failure_code"],
                            "error_ref": fix_run["failure_ref"],
                        }
                        if fix_run["failure_code"]
                        else None
                    ),
                    "created_at": fix_run["created_at"],
                    "updated_at": fix_run["updated_at"],
                }
                if fix_run is not None
                and fix_run["status"] in {"queued", "running", "failed"}
                else None
            ),
            "fix_failure": (
                {
                    "code": row["fix_failure_code"],
                    "message": row["fix_failure_message"],
                    "error_ref": row["fix_failure_ref"],
                    "retryable": bool(row["fix_failure_retryable"]),
                }
                if row["fix_failure_code"]
                else None
            ),
            "latest_publication": (
                {
                    "fix_revision_ref": publication["fix_revision_ref"],
                    "attempt": int(publication["attempt"]),
                    "status": {
                        "queued": "publishing",
                        "running": "publishing",
                        "succeeded": "published",
                        "failed": "failed",
                    }[str(publication["status"])],
                    "content_sha256": publication["content_sha256"],
                    "failure": (
                        {
                            "code": publication["failure_code"],
                            "error_ref": publication["failure_ref"],
                        }
                        if publication["failure_code"]
                        else None
                    ),
                    "created_at": publication["created_at"],
                }
                if publication is not None
                else None
            ),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        return result

    def _review_id(
        self,
        connection: sqlite3.Connection,
        review_ref: str,
    ) -> int:
        row = connection.execute(
            "SELECT id FROM trajectory_review_tasks WHERE review_ref = ?",
            (review_ref,),
        ).fetchone()
        if row is None:
            raise AnnotationNotFoundError("trajectory review not found")
        return int(row["id"])

    def _review_row(
        self,
        connection: sqlite3.Connection,
        review_id: int,
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM trajectory_review_tasks WHERE id = ?",
            (review_id,),
        ).fetchone()
        if row is None:
            raise AnnotationNotFoundError("trajectory review not found")
        return row

    def _require_review_revision(
        self,
        connection: sqlite3.Connection,
        review: sqlite3.Row,
        expected: int,
    ) -> None:
        if int(review["state_revision"]) != expected:
            raise AnnotationConflictError(
                "review_revision_conflict",
                "The trajectory review changed; refresh before retrying.",
                current=self._review_projection(connection, int(review["id"])),
            )

    def _invalid_review_action(
        self,
        connection: sqlite3.Connection,
        review_id: int,
    ) -> None:
        raise AnnotationConflictError(
            "invalid_review_state",
            "The requested action is unavailable in the current review state.",
            current=self._review_projection(connection, review_id),
        )

    def _job_projection(
        self,
        connection: sqlite3.Connection,
        job_id: int,
        *,
        include_segments: bool = True,
    ) -> dict[str, Any]:
        job = self._job_row(connection, job_id)
        clips = [
            str(row["source_clip"])
            for row in connection.execute(
                """
                SELECT source_clip FROM annotation_job_source_clips
                WHERE job_id = ? ORDER BY ordinal
                """,
                (job_id,),
            ).fetchall()
        ]
        segment_rows = connection.execute(
            "SELECT * FROM annotation_segments WHERE job_id = ? ORDER BY ordinal",
            (job_id,),
        ).fetchall()
        segments = [
            self._segment_projection(
                connection,
                row,
                include_draft=False,
                job_status=str(job["status"]),
            )
            for row in segment_rows
        ]
        all_statuses = (
            "pending_initial_annotation",
            "draft",
            "submitted",
            "skipped",
            "tracking",
            "tracked",
            "postprocessing",
            "annotated",
            "postprocessing_failed",
        )
        counts = {"total": len(segments), **{status: 0 for status in all_statuses}}
        for segment in segments:
            counts[segment["status"]] += 1
        calibration = connection.execute(
            """
            SELECT profile_ref, label, content_sha256
            FROM calibration_snapshots WHERE id = ?
            """,
            (job["calibration_snapshot_id"],),
        ).fetchone()
        resolved = counts["total"] > 0 and (
            counts["submitted"] + counts["skipped"] == counts["total"]
        )
        ready_for_tracking = (
            job["status"] == "waiting_initial_annotation"
            and resolved
            and counts["submitted"] > 0
        )
        ready_for_no_targets = (
            job["status"] == "waiting_initial_annotation"
            and counts["total"] > 0
            and counts["skipped"] == counts["total"]
        )
        failure = None
        if job["failure_code"]:
            failure = {
                "code": job["failure_code"],
                "message": job["failure_message"],
                "retryable": bool(job["failure_retryable"]),
                "error_ref": job["failure_ref"],
            }
        result = {
            "job_ref": job["job_ref"],
            "dataset_date": job["dataset_date"],
            "source_clips": clips,
            "status": job["status"],
            "completion_outcome": job["completion_outcome"],
            "cancel_requested": bool(job["cancel_requested"]),
            "state_revision": int(job["state_revision"]),
            "calibration": {
                "profile_ref": calibration["profile_ref"],
                "label": calibration["label"],
                "content_sha256": calibration["content_sha256"],
            },
            "counts": counts,
            "ready_for_tracking": ready_for_tracking,
            "ready_for_no_processable_targets": ready_for_no_targets,
            "failure": failure,
            "created_at": job["created_at"],
            "updated_at": job["updated_at"],
        }
        if include_segments:
            result["segments"] = segments
        return result

    def _segment_projection(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        include_draft: bool,
        job_status: str | None = None,
    ) -> dict[str, Any]:
        projected_status = str(row["status"])
        if projected_status == "tracking":
            resolved_job_status = job_status
            if resolved_job_status is None:
                resolved_job_status = str(
                    self._job_row(connection, int(row["job_id"]))["status"],
                )
            # Keep the raw `tracking` row as an audit fact about where runtime
            # cancellation occurred.  Publicly, however, a terminal cancelled
            # Job must not look like it still has active segment work.
            if (
                resolved_job_status == "cancelled"
                and row["submitted_revision"] is not None
            ):
                projected_status = "submitted"
        frame = {
            "url": (
                f"/api/annotation/jobs/"
                f"{self._job_ref_for_id(connection, int(row['job_id']))}/segments/"
                f"{row['segment_ref']}/first-frame"
            ),
            "width": int(row["first_frame_width"]),
            "height": int(row["first_frame_height"]),
            "sha256": row["first_frame_sha256"],
            "etag": row["first_frame_etag"],
        }
        result: dict[str, Any] = {
            "segment_ref": row["segment_ref"],
            "ordinal": int(row["ordinal"]),
            "source_clip": row["source_clip"],
            "status": projected_status,
            "state_revision": int(row["state_revision"]),
            "draft_revision": (
                int(row["draft_revision"]) if int(row["draft_revision"]) else None
            ),
            "submitted_revision": (
                int(row["submitted_revision"])
                if row["submitted_revision"] is not None
                else None
            ),
            "first_frame": frame,
        }
        if include_draft:
            draft = connection.execute(
                """
                SELECT draft_revision, targets_json
                FROM initial_annotation_drafts WHERE segment_id = ?
                """,
                (row["id"],),
            ).fetchone()
            result["draft"] = (
                {
                    "revision": int(draft["draft_revision"]),
                    "targets": json.loads(draft["targets_json"]),
                }
                if draft is not None
                else None
            )
            result["skip_reason"] = (
                {
                    "reason_code": row["skip_reason_code"],
                    "note": row["skip_note"],
                }
                if row["skip_reason_code"]
                else None
            )
        return result

    def _validate_submission(
        self,
        segment: sqlite3.Row,
        targets: list[dict[str, Any]],
    ) -> None:
        if not targets:
            raise AnnotationValidationError(
                "annotation_incomplete",
                "At least one target is required.",
            )
        width = int(segment["first_frame_width"])
        height = int(segment["first_frame_height"])
        for raw in targets:
            target = DraftAnnotationTarget.model_validate(raw)
            if target.bbox is None or target.point is None:
                raise AnnotationValidationError(
                    "annotation_incomplete",
                    "Every target requires a bounding box and foreground point.",
                )
            if any(value is None for value in target.colors.values()):
                raise AnnotationValidationError(
                    "annotation_incomplete",
                    "Every target requires upper, lower, and shoes colors.",
                )
            x, y, box_width, box_height = target.bbox
            point_x, point_y = target.point
            if (
                box_width < 0
                or box_height < 0
                or x < 0
                or y < 0
                or x + box_width > width
                or y + box_height > height
                or point_x < 0
                or point_y < 0
                or point_x >= width
                or point_y >= height
            ):
                raise AnnotationValidationError(
                    "annotation_coordinates_out_of_bounds",
                    "Annotation coordinates must remain inside the first frame.",
                )

    def _mutable_waiting_job(
        self,
        connection: sqlite3.Connection,
        job_ref: str,
    ) -> tuple[int, sqlite3.Row]:
        job_id = self._job_id(connection, job_ref)
        job = self._job_row(connection, job_id)
        if job["status"] != "waiting_initial_annotation":
            self._invalid_job_action(connection, job_id)
        return job_id, job

    def _job_id(self, connection: sqlite3.Connection, job_ref: str) -> int:
        row = connection.execute(
            "SELECT id FROM annotation_jobs WHERE job_ref = ?",
            (job_ref,),
        ).fetchone()
        if row is None:
            raise AnnotationNotFoundError("annotation job not found")
        return int(row["id"])

    def _job_ref_for_id(self, connection: sqlite3.Connection, job_id: int) -> str:
        return str(self._job_row(connection, job_id)["job_ref"])

    def _job_row(self, connection: sqlite3.Connection, job_id: int) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM annotation_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            raise AnnotationNotFoundError("annotation job not found")
        return row

    def _segment_row(
        self,
        connection: sqlite3.Connection,
        job_id: int,
        segment_ref: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT * FROM annotation_segments
            WHERE job_id = ? AND segment_ref = ?
            """,
            (job_id, segment_ref),
        ).fetchone()
        if row is None:
            raise AnnotationNotFoundError("annotation segment not found")
        return row

    def _require_job_revision(
        self,
        job: sqlite3.Row,
        expected: int,
        connection: sqlite3.Connection,
    ) -> None:
        if int(job["state_revision"]) != expected:
            raise AnnotationConflictError(
                "job_revision_conflict",
                "The annotation job changed; refresh before retrying.",
                current=self._job_projection(connection, int(job["id"])),
            )

    def _require_segment_revision(
        self,
        segment: sqlite3.Row,
        expected: int,
        connection: sqlite3.Connection,
    ) -> None:
        if int(segment["state_revision"]) != expected:
            raise AnnotationConflictError(
                "segment_revision_conflict",
                "The annotation segment changed; refresh before retrying.",
                current=self._segment_projection(connection, segment, include_draft=True),
            )

    def _invalid_job_action(
        self,
        connection: sqlite3.Connection,
        job_id: int,
    ) -> None:
        raise AnnotationConflictError(
            "invalid_job_state",
            "The requested action is unavailable in the current job state.",
            current=self._job_projection(connection, job_id),
        )

    def _invalid_segment_action(
        self,
        connection: sqlite3.Connection,
        segment: sqlite3.Row,
    ) -> None:
        raise AnnotationConflictError(
            "invalid_segment_state",
            "The requested action is unavailable in the current segment state.",
            current=self._segment_projection(connection, segment, include_draft=True),
        )

    def _touch_job(self, connection: sqlite3.Connection, job_id: int) -> None:
        connection.execute(
            """
            UPDATE annotation_jobs
            SET state_revision = state_revision + 1, updated_at = ?
            WHERE id = ?
            """,
            (_now(), job_id),
        )

    def _record_action(
        self,
        connection: sqlite3.Connection,
        segment_id: int,
        action: str,
        payload: dict[str, Any],
    ) -> None:
        connection.execute(
            """
            INSERT INTO annotation_segment_actions (
                action_ref, segment_id, action, safe_payload_json,
                actor_kind, deployment_instance, created_at
            ) VALUES (?, ?, ?, ?, 'manual_web', ?, ?)
            """,
            (
                _new_ref("segment_action"),
                segment_id,
                action,
                _canonical_json(payload),
                self.deployment_instance,
                _now(),
            ),
        )

    def _enqueue_run(
        self,
        connection: sqlite3.Connection,
        *,
        job_id: int,
        kind: str,
    ) -> str:
        row = connection.execute(
            "SELECT COALESCE(MAX(attempt), 0) + 1 AS attempt FROM runtime_runs "
            "WHERE job_id = ? AND kind = ?",
            (job_id, kind),
        ).fetchone()
        run_ref = _new_ref("run")
        timestamp = _now()
        connection.execute(
            """
            INSERT INTO runtime_runs (
                run_ref, job_id, kind, status, attempt, created_at, updated_at
            ) VALUES (?, ?, ?, 'queued', ?, ?, ?)
            """,
            (run_ref, job_id, kind, int(row["attempt"]), timestamp, timestamp),
        )
        return run_ref

    def _enqueue_compatibility_publication(
        self,
        connection: sqlite3.Connection,
        *,
        job_id: int,
        review_id: int,
        fix_revision_id: int,
        created_at: str,
    ) -> None:
        attempt_row = connection.execute(
            """
            SELECT COALESCE(MAX(attempt), 0) + 1 AS next_attempt
            FROM compatibility_publications
            WHERE review_id = ?
            """,
            (review_id,),
        ).fetchone()
        run_ref = self._enqueue_run(
            connection,
            job_id=job_id,
            kind="compatibility_publish",
        )
        run = connection.execute(
            "SELECT id FROM runtime_runs WHERE run_ref = ?",
            (run_ref,),
        ).fetchone()
        publication_ref = _new_ref("publication")
        connection.execute(
            """
            INSERT INTO compatibility_publications (
                publication_ref, review_id, fix_revision_id, attempt,
                status, created_at
            ) VALUES (?, ?, ?, ?, 'queued', ?)
            """,
            (
                publication_ref,
                review_id,
                fix_revision_id,
                int(attempt_row["next_attempt"]),
                created_at,
            ),
        )
        publication = connection.execute(
            """
            SELECT id FROM compatibility_publications
            WHERE publication_ref = ?
            """,
            (publication_ref,),
        ).fetchone()
        connection.execute(
            """
            INSERT INTO runtime_run_publication_links (
                run_id, publication_id, review_id, created_at
            ) VALUES (?, ?, ?, ?)
            """,
            (
                int(run["id"]),
                int(publication["id"]),
                review_id,
                created_at,
            ),
        )

    def _cancel_job(
        self,
        connection: sqlite3.Connection,
        job_id: int,
        *,
        completion_outcome: str,
        request_runtime_cancel: bool,
    ) -> None:
        timestamp = _now()
        active = connection.execute(
            """
            SELECT id, kind FROM runtime_runs
            WHERE job_id = ? AND status = 'running'
            LIMIT 1
            """,
            (job_id,),
        ).fetchone()
        connection.execute(
            """
            UPDATE runtime_runs
            SET status = 'cancelled', finished_at = ?, updated_at = ?
            WHERE job_id = ? AND status = 'queued'
            """,
            (timestamp, timestamp, job_id),
        )
        if active is not None:
            if not request_runtime_cancel:
                raise RuntimeError(
                    "cannot release a source scope while its Runtime is active",
                )
            if active["kind"] == "postprocessing":
                publication_fence = connection.execute(
                    """
                    SELECT 1 FROM runtime_run_steps
                    WHERE run_id = ?
                      AND safe_step_code = 'compatibility_publish'
                    LIMIT 1
                    """,
                    (active["id"],),
                ).fetchone()
                if publication_fence is not None:
                    raise AnnotationConflictError(
                        "postprocessing_publication_in_progress",
                        "Postprocessing publication has started and can no "
                        "longer be cancelled.",
                        current=self._job_projection(connection, job_id),
                    )
            connection.execute(
                """
                UPDATE annotation_jobs
                SET state_revision = state_revision + 1,
                    cancel_requested = 1,
                    cancel_requested_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (timestamp, timestamp, job_id),
            )
            return
        self._finalize_cancelled_job(
            connection,
            job_id,
            completion_outcome=completion_outcome,
        )

    def _finalize_cancelled_job(
        self,
        connection: sqlite3.Connection,
        job_id: int,
        *,
        completion_outcome: str,
    ) -> None:
        timestamp = _now()
        connection.execute(
            """
            UPDATE annotation_jobs
            SET status = 'cancelled', completion_outcome = ?,
                state_revision = state_revision + 1,
                cancel_requested = 0,
                cancel_requested_at = NULL,
                updated_at = ?
            WHERE id = ?
            """,
            (
                completion_outcome,
                timestamp,
                job_id,
            ),
        )
        connection.execute(
            "DELETE FROM annotation_source_leases WHERE job_id = ?",
            (job_id,),
        )

    def _running_run(self, connection: sqlite3.Connection, run_id: int) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM runtime_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        if row is None or row["status"] != "running":
            raise RuntimeError("runtime run is not running")
        return row

    def _finish_run(
        self,
        connection: sqlite3.Connection,
        run_id: int,
        status: str,
    ) -> None:
        timestamp = _now()
        connection.execute(
            """
            UPDATE runtime_runs
            SET status = ?, finished_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (status, timestamp, timestamp, run_id),
        )
        connection.execute("DELETE FROM runtime_leases WHERE run_id = ?", (run_id,))

    def _fail_run_conn(
        self,
        connection: sqlite3.Connection,
        run_id: int,
        *,
        code: str,
        message: str,
        retryable: bool,
        private_detail: str | None = None,
        failure_ref: str | None = None,
    ) -> None:
        run = connection.execute(
            "SELECT * FROM runtime_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        if run is None:
            raise RuntimeError("runtime run not found")
        job = self._job_row(connection, int(run["job_id"]))
        if (
            code != "recovery_required"
            and run["status"] == "running"
            and bool(job["cancel_requested"])
        ):
            timestamp = _now()
            connection.execute(
                """
                UPDATE runtime_run_steps
                SET status = 'failed', diagnostic_ref = ?,
                    updated_at = ?
                WHERE run_id = ? AND status = 'started'
                """,
                (
                    _new_ref("runtime_step_cancelled"),
                    timestamp,
                    run_id,
                ),
            )
            self._finish_run(connection, run_id, "cancelled")
            self._finalize_cancelled_job(
                connection,
                int(run["job_id"]),
                completion_outcome="cancelled_by_user",
            )
            return
        if (
            run["status"] == "failed"
            and run["failure_code"] == "recovery_required"
        ):
            # Recovery already made the authoritative fail-closed decision.
            # A late exception from the old worker/thread must not downgrade
            # or relabel that terminal safety state.
            return
        timestamp = _now()
        resolved_failure_ref = failure_ref or _new_ref("annotation_error")
        connection.execute(
            """
            UPDATE runtime_runs
            SET status = 'failed', failure_code = ?, failure_message = ?,
                failure_ref = ?, private_failure_detail = ?,
                finished_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                code,
                message,
                resolved_failure_ref,
                private_detail,
                timestamp,
                timestamp,
                run_id,
            ),
        )
        if run["kind"] == "compatibility_publish":
            link = connection.execute(
                """
                SELECT l.review_id, p.id AS publication_id, p.status
                FROM runtime_run_publication_links l
                JOIN compatibility_publications p
                  ON p.id = l.publication_id
                WHERE l.run_id = ?
                """,
                (run_id,),
            ).fetchone()
            if link is None:
                raise RuntimeError(
                    "failed compatibility publication lacks its binding"
                )
            if link["status"] != "running":
                raise RuntimeError(
                    "failed compatibility publication is not running"
                )
            connection.execute(
                """
                UPDATE compatibility_publications
                SET status = 'failed', failure_code = ?, failure_ref = ?
                WHERE id = ?
                """,
                (
                    code,
                    resolved_failure_ref,
                    link["publication_id"],
                ),
            )
            connection.execute(
                """
                UPDATE trajectory_review_tasks
                SET state_revision = state_revision + 1, updated_at = ?
                WHERE id = ? AND status = 'approved'
                """,
                (timestamp, link["review_id"]),
            )
            connection.execute(
                "DELETE FROM runtime_leases WHERE run_id = ?",
                (run_id,),
            )
            return
        if run["kind"] == "fix":
            link = connection.execute(
                """
                SELECT review_id FROM runtime_run_review_links
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
            if link is None:
                raise RuntimeError("failed Fix run lacks its review binding")
            connection.execute(
                """
                UPDATE trajectory_review_tasks
                SET state_revision = state_revision + 1,
                    fix_failure_code = ?,
                    fix_failure_message = ?,
                    fix_failure_ref = ?,
                    fix_failure_retryable = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    code,
                    message,
                    resolved_failure_ref,
                    int(retryable),
                    timestamp,
                    link["review_id"],
                ),
            )
            connection.execute(
                "DELETE FROM runtime_leases WHERE run_id = ?",
                (run_id,),
            )
            return
        if job["status"] != "cancelled":
            connection.execute(
                """
                UPDATE annotation_jobs
                SET status = 'failed', state_revision = state_revision + 1,
                    failure_code = ?, failure_message = ?, failure_ref = ?,
                    private_failure_detail = ?,
                    failure_retryable = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    code,
                    message,
                    resolved_failure_ref,
                    private_detail,
                    int(retryable),
                    timestamp,
                    run["job_id"],
                ),
            )
            if run["kind"] == "postprocessing":
                connection.execute(
                    """
                    UPDATE annotation_segments
                    SET status = 'postprocessing_failed',
                        state_revision = state_revision + 1,
                        updated_at = ?
                    WHERE job_id = ? AND status = 'postprocessing'
                    """,
                    (timestamp, run["job_id"]),
                )
                if run["status"] == "running":
                    handoff_payload = {
                        "job_ref": str(job["job_ref"]),
                        "failure_code": code,
                        "error_ref": resolved_failure_ref,
                        "retryable": bool(retryable),
                    }
                    _require_safe_handoff_payload(handoff_payload)
                    connection.execute(
                        """
                        INSERT INTO workflow_handoffs (
                            handoff_ref, job_id, kind, payload_json,
                            content_sha256, created_at
                        ) VALUES (?, ?, 'postprocessing_failed', ?, ?, ?)
                        """,
                        (
                            _new_ref("handoff"),
                            run["job_id"],
                            _canonical_json(handoff_payload),
                            _payload_hash(handoff_payload),
                            timestamp,
                        ),
                    )
        connection.execute("DELETE FROM runtime_leases WHERE run_id = ?", (run_id,))

    def _insert_manifest(
        self,
        connection: sqlite3.Connection,
        run: sqlite3.Row,
        stage: str,
        manifest: dict[str, Any],
    ) -> str:
        encoded = _canonical_json(manifest)
        manifest_ref = _new_ref("artifact_manifest")
        connection.execute(
            """
            INSERT INTO artifact_manifests (
                manifest_ref, job_id, run_id, stage, content_sha256,
                manifest_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                manifest_ref,
                run["job_id"],
                run["id"],
                stage,
                hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
                encoded,
                _now(),
            ),
        )
        return manifest_ref

    def _require_runtime_step_ledger(
        self,
        connection: sqlite3.Connection,
        run_id: int,
        declared_steps: Any,
    ) -> list[str]:
        rows = connection.execute(
            """
            SELECT safe_step_code, status, return_code, diagnostic_ref
            FROM runtime_run_steps
            WHERE run_id = ?
            ORDER BY ordinal
            """,
            (run_id,),
        ).fetchall()
        if not rows:
            raise RuntimeError("runtime step ledger is empty")
        if any(row["status"] != "succeeded" for row in rows):
            raise RuntimeError("runtime step ledger is not fully committed")
        if any(row["diagnostic_ref"] is not None for row in rows):
            raise RuntimeError("successful runtime step has a diagnostic reference")
        semantic_steps = [
            str(row["safe_step_code"])
            for row in rows
            if row["safe_step_code"] != "tracking_target_completed"
        ]
        if any(step not in _SAFE_RUNTIME_STEP_CODES for step in semantic_steps):
            raise RuntimeError("runtime step ledger contains an unsafe step")
        semantic_rows = [
            row
            for row in rows
            if row["safe_step_code"] != "tracking_target_completed"
        ]
        if any(
            row["return_code"] is None or int(row["return_code"]) != 0
            for row in semantic_rows
        ):
            raise RuntimeError("runtime step ledger lacks a successful return code")
        if (
            not isinstance(declared_steps, list)
            and not isinstance(declared_steps, tuple)
        ):
            raise RuntimeError("runtime manifest lacks declared command steps")
        if semantic_steps != list(declared_steps):
            raise RuntimeError(
                "runtime manifest and committed step ledger differ",
            )
        return semantic_steps

    @staticmethod
    def _active_reserved_bytes_conn(
        connection: sqlite3.Connection,
        *,
        excluding_job_id: int | None,
    ) -> int:
        if excluding_job_id is None:
            row = connection.execute(
                """
                SELECT COALESCE(SUM(reserved_bytes), 0) AS total
                FROM annotation_jobs j
                WHERE j.status <> 'cancelled'
                   OR EXISTS (
                       SELECT 1 FROM runtime_runs r
                       WHERE r.job_id = j.id AND r.status = 'running'
                   )
                """
            ).fetchone()
        else:
            row = connection.execute(
                """
                SELECT COALESCE(SUM(reserved_bytes), 0) AS total
                FROM annotation_jobs j
                WHERE (
                    j.status <> 'cancelled'
                    OR EXISTS (
                        SELECT 1 FROM runtime_runs r
                        WHERE r.job_id = j.id AND r.status = 'running'
                    )
                )
                  AND j.id <> ?
                """,
                (excluding_job_id,),
            ).fetchone()
        return int(row["total"])

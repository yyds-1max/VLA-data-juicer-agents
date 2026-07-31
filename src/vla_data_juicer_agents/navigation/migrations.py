"""Stopped-service, fail-closed Navigation state migration.

The M1 database predates a migration ledger.  This module accepts only the
exact frozen M1 schema generation, creates an immutable backup, and upgrades
the two phase-constrained ledger tables without rewriting their payloads.
"""

from __future__ import annotations

from contextlib import nullcontext
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
from typing import Any
from uuid import uuid4

from vla_data_juicer_agents.annotation.maintenance import (
    acquire_annotation_maintenance,
    annotation_maintenance_lock_path,
)
from vla_data_juicer_agents.navigation.schema import (
    LATEST_NAVIGATION_SCHEMA_VERSION,
    NAVIGATION_INDEX_SQL,
    NAVIGATION_STATE_SCHEMA_GENERATION,
    NAVIGATION_TABLE_SQL,
    NAVIGATION_TRIGGER_SQL,
    PREVIOUS_NAVIGATION_STATE_SCHEMA_GENERATION,
    UnsupportedNavigationSchemaVersion,
    _current_migration_ledger_violation,
    _m1_schema_contract_violation,
    _schema_contract_violation,
)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_existing_database(database: Path) -> Path:
    if not database.is_absolute():
        database = (Path.cwd() / database).absolute()
    try:
        metadata = database.lstat()
        resolved = database.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError("navigation database is unavailable") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or resolved != database
    ):
        raise RuntimeError("navigation database is unsafe")
    return resolved


def _safe_backup_destination(database: Path, backup_root: Path | None) -> Path:
    destination = backup_root or (
        database.parent
        / (
            "navigation-migration-backup-"
            + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            + "-"
            + uuid4().hex
        )
    )
    if not destination.is_absolute():
        destination = (Path.cwd() / destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise RuntimeError("navigation migration backup destination already exists")
    try:
        parent_metadata = destination.parent.lstat()
        parent_resolved = destination.parent.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError(
            "navigation migration backup parent is unavailable"
        ) from exc
    if (
        stat.S_ISLNK(parent_metadata.st_mode)
        or not stat.S_ISDIR(parent_metadata.st_mode)
        or parent_metadata.st_uid != os.geteuid()
        or parent_metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        or parent_resolved != destination.parent
    ):
        raise RuntimeError("navigation migration backup parent is unsafe")
    destination.mkdir(mode=0o700)
    return destination


def _backup_database(database: Path, destination: Path) -> str:
    copied: list[dict[str, Any]] = []
    for source in (
        database,
        Path(f"{database}-wal"),
        Path(f"{database}-shm"),
    ):
        try:
            metadata = source.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError("navigation migration backup source is unsafe")
        target = destination / source.name
        shutil.copyfile(source, target, follow_symlinks=False)
        target.chmod(0o600)
        copied.append(
            {
                "name": source.name,
                "size": target.stat().st_size,
                "sha256": _sha256_file(target),
            }
        )
    manifest = {
        "database_name": database.name,
        "source_generation": PREVIOUS_NAVIGATION_STATE_SCHEMA_GENERATION,
        "target_generation": NAVIGATION_STATE_SCHEMA_GENERATION,
        "target_schema_version": LATEST_NAVIGATION_SCHEMA_VERSION,
        "files": copied,
    }
    manifest_path = destination / "backup-manifest.json"
    manifest_path.write_text(_canonical_json(manifest), encoding="utf-8")
    manifest_path.chmod(0o600)
    return _sha256_file(manifest_path)


def _row_count(connection: sqlite3.Connection, table: str) -> int:
    return int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])


def _apply_m1_to_m2(connection: sqlite3.Connection, *, applied_at: str) -> None:
    plan_count = _row_count(connection, "navigation_plans")
    attempt_count = _row_count(
        connection, "navigation_plan_submission_attempts"
    )
    task_count = _row_count(connection, "navigation_tasks")

    for trigger_name in NAVIGATION_TRIGGER_SQL:
        connection.execute(f'DROP TRIGGER IF EXISTS "{trigger_name}"')
    connection.execute("PRAGMA legacy_alter_table = ON")
    connection.execute(
        "ALTER TABLE navigation_plans RENAME TO navigation_plans_m1_old"
    )
    connection.execute(NAVIGATION_TABLE_SQL["navigation_plans"])
    connection.execute(
        """
        INSERT INTO navigation_plans
        SELECT * FROM navigation_plans_m1_old
        """
    )
    connection.execute(
        """
        ALTER TABLE navigation_plan_submission_attempts
        RENAME TO navigation_plan_submission_attempts_m1_old
        """
    )
    connection.execute(
        NAVIGATION_TABLE_SQL["navigation_plan_submission_attempts"]
    )
    connection.execute(
        """
        INSERT INTO navigation_plan_submission_attempts
        SELECT * FROM navigation_plan_submission_attempts_m1_old
        """
    )
    connection.execute("DROP TABLE navigation_plans_m1_old")
    connection.execute(
        "DROP TABLE navigation_plan_submission_attempts_m1_old"
    )
    connection.execute("PRAGMA legacy_alter_table = OFF")

    for table in (
        "navigation_task_outcomes",
        "navigation_task_lineage",
        "navigation_schema_migrations",
        "navigation_migration_safety",
    ):
        connection.execute(NAVIGATION_TABLE_SQL[table])
    connection.execute(
        """
        INSERT INTO navigation_task_outcomes (
            task_id, requested_outcome, completion_outcome, revision,
            metadata_json, created_at, updated_at
        )
        SELECT task_id, 'auto', NULL, 1, '{}', created_at, updated_at
        FROM navigation_tasks
        """
    )

    for name, statement in NAVIGATION_INDEX_SQL.items():
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?",
            (name,),
        ).fetchone() is None:
            connection.execute(statement)
    for statement in NAVIGATION_TRIGGER_SQL.values():
        connection.execute(statement)
    connection.execute(
        """
        UPDATE navigation_state_schema
        SET generation = ?
        WHERE singleton = 1
        """,
        (NAVIGATION_STATE_SCHEMA_GENERATION,),
    )
    connection.execute(
        """
        INSERT INTO navigation_schema_migrations (
            version, name, source_generation, target_generation, applied_at
        ) VALUES (1, 'navigation_m2_v1', ?, ?, ?)
        """,
        (
            PREVIOUS_NAVIGATION_STATE_SCHEMA_GENERATION,
            NAVIGATION_STATE_SCHEMA_GENERATION,
            applied_at,
        ),
    )
    connection.execute(
        """
        INSERT INTO navigation_migration_safety (
            singleton, schema_version, status, verified_at
        ) VALUES (1, ?, 'pending_integrity_check', NULL)
        """,
        (LATEST_NAVIGATION_SCHEMA_VERSION,),
    )
    if _row_count(connection, "navigation_plans") != plan_count:
        raise RuntimeError("navigation plan ledger row count changed")
    if (
        _row_count(connection, "navigation_plan_submission_attempts")
        != attempt_count
    ):
        raise RuntimeError("navigation plan attempt ledger row count changed")
    if _row_count(connection, "navigation_task_outcomes") != task_count:
        raise RuntimeError("navigation task outcome backfill is incomplete")


def _verify_and_mark(connection: sqlite3.Connection) -> None:
    contract_violation = _schema_contract_violation(connection)
    foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
    integrity = connection.execute("PRAGMA integrity_check").fetchall()
    integrity_errors = [
        str(row[0]) for row in integrity if not row or str(row[0]).lower() != "ok"
    ]
    if contract_violation or foreign_key_errors or integrity_errors:
        raise RuntimeError("navigation database failed post-migration integrity checks")
    connection.execute("BEGIN IMMEDIATE")
    try:
        cursor = connection.execute(
            """
            UPDATE navigation_migration_safety
            SET status = 'verified', verified_at = CURRENT_TIMESTAMP
            WHERE singleton = 1
              AND schema_version = ?
              AND status = 'pending_integrity_check'
            """,
            (LATEST_NAVIGATION_SCHEMA_VERSION,),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("navigation migration safety marker changed")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    violation, newer = _current_migration_ledger_violation(connection)
    if violation is not None:
        if newer:
            raise UnsupportedNavigationSchemaVersion(violation)
        raise RuntimeError(violation)


def migrate_navigation_store_offline(
    db_path: Path | str,
    *,
    backup_root: Path | str | None = None,
    maintenance_lease: Any | None = None,
) -> dict[str, Any]:
    """Back up and migrate the exact M1 Navigation store under lifecycle lock."""

    database = _safe_existing_database(Path(db_path))
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
            raise RuntimeError("navigation migration maintenance lease is invalid")
        maintenance_context = nullcontext(maintenance_lease)

    with maintenance_context:
        connection = sqlite3.connect(
            f"{database.as_uri()}?mode=rw",
            uri=True,
            timeout=10,
        )
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            marker = connection.execute(
                """
                SELECT generation FROM navigation_state_schema
                WHERE singleton = 1
                """
            ).fetchone()
            generation = marker["generation"] if marker is not None else None
            if generation == NAVIGATION_STATE_SCHEMA_GENERATION:
                raise RuntimeError("navigation database schema is already current")
            if generation != PREVIOUS_NAVIGATION_STATE_SCHEMA_GENERATION:
                raise UnsupportedNavigationSchemaVersion(
                    f"unsupported navigation state generation {generation!r}"
                )
            source_violation = _m1_schema_contract_violation(connection)
            if source_violation is not None:
                raise RuntimeError(
                    f"navigation M1 source contract is invalid: {source_violation}"
                )

            destination = _safe_backup_destination(
                database,
                Path(backup_root) if backup_root is not None else None,
            )
            manifest_sha256 = _backup_database(database, destination)
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("BEGIN IMMEDIATE")
            try:
                _apply_m1_to_m2(
                    connection,
                    applied_at=datetime.now(UTC).isoformat(),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            connection.execute("PRAGMA foreign_keys = ON")
            _verify_and_mark(connection)
        finally:
            connection.close()

    return {
        "status": "migrated",
        "from_generation": PREVIOUS_NAVIGATION_STATE_SCHEMA_GENERATION,
        "to_generation": NAVIGATION_STATE_SCHEMA_GENERATION,
        "to_version": LATEST_NAVIGATION_SCHEMA_VERSION,
        "backup_root": str(destination),
        "backup_manifest_sha256": manifest_sha256,
    }

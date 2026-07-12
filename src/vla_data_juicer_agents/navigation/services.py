from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
import sqlite3

from vla_data_juicer_agents.navigation.config import NavigationSettings
from vla_data_juicer_agents.navigation.evidence_store import FileNavigationEvidenceStore
from vla_data_juicer_agents.navigation.observation_models import (
    EvidenceDescriptor,
    NavigationObservationRevision,
)
from vla_data_juicer_agents.navigation.observation_store import (
    SqliteNavigationObservationStore,
)
from vla_data_juicer_agents.navigation.plan_store import SqliteNavigationPlanRepository
from vla_data_juicer_agents.navigation.task_store import SqliteNavigationTaskStore


@dataclass(frozen=True)
class NavigationServices:
    settings: NavigationSettings
    task_store: SqliteNavigationTaskStore
    observation_store: SqliteNavigationObservationStore
    evidence_store: FileNavigationEvidenceStore
    plan_store: SqliteNavigationPlanRepository


class LegacyObservationMigrationError(RuntimeError):
    """Raised when deployed observation state cannot be safely imported."""


_LEGACY_OBSERVATION_MIGRATION = "legacy_observations_to_unified_v1"


def _legacy_migration_complete(target: Path) -> bool:
    if not target.is_file():
        return False
    connection = sqlite3.connect(f"file:{target}?mode=ro", uri=True, timeout=5.0)
    try:
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='navigation_service_migrations'"
        ).fetchone() is None:
            return False
        return connection.execute(
            "SELECT 1 FROM navigation_service_migrations WHERE name=?",
            (_LEGACY_OBSERVATION_MIGRATION,),
        ).fetchone() is not None
    finally:
        connection.close()


def _migrate_legacy_observations(
    legacy_db_path: Path,
    target_db_path: Path,
) -> None:
    """Copy deployed observation rows once without overwriting unified state."""
    if _legacy_migration_complete(target_db_path):
        return
    if not legacy_db_path.is_file() or legacy_db_path == target_db_path:
        return
    connection = sqlite3.connect(target_db_path, timeout=30)
    connection.row_factory = sqlite3.Row
    attached = False
    try:
        connection.execute("ATTACH DATABASE ? AS legacy_observations", (str(legacy_db_path),))
        attached = True
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS navigation_service_migrations (
                name TEXT PRIMARY KEY,
                completed_at TEXT NOT NULL
            )
            """
        )
        marker = connection.execute(
            "SELECT 1 FROM navigation_service_migrations WHERE name = ?",
            (_LEGACY_OBSERVATION_MIGRATION,),
        ).fetchone()
        if marker is not None:
            connection.commit()
            return
        legacy_tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM legacy_observations.sqlite_master WHERE type = 'table'"
            )
        }
        required = {"navigation_observation_revisions", "navigation_evidence"}
        if not required.issubset(legacy_tables):
            raise LegacyObservationMigrationError(
                "legacy observation database has a partial or unsupported schema"
            )
        revision_rows = connection.execute(
            "SELECT * FROM legacy_observations.navigation_observation_revisions"
        ).fetchall()
        revisions: dict[tuple[str, int], NavigationObservationRevision] = {}
        for row in revision_rows:
            if connection.execute("SELECT json_valid(?)", (row["revision_json"],)).fetchone()[0] != 1:
                raise LegacyObservationMigrationError(
                    "legacy observation revision contains invalid JSON"
                )
            revision = NavigationObservationRevision.model_validate_json(
                row["revision_json"]
            )
            if (
                revision.task_id != row["task_id"]
                or revision.revision != row["revision"]
                or revision.phase.value != row["phase"]
                or revision.created_at != row["created_at"]
            ):
                raise LegacyObservationMigrationError(
                    "legacy observation revision columns do not match its payload"
                )
            revisions[(revision.task_id, revision.revision)] = revision
            target_row = connection.execute(
                """
                SELECT task_id, revision, phase, revision_json, created_at
                FROM navigation_observation_revisions
                WHERE task_id = ? AND revision = ?
                """,
                (revision.task_id, revision.revision),
            ).fetchone()
            if target_row is not None and dict(target_row) != {
                "task_id": row["task_id"],
                "revision": row["revision"],
                "phase": row["phase"],
                "revision_json": row["revision_json"],
                "created_at": row["created_at"],
            }:
                raise LegacyObservationMigrationError(
                    "legacy observation revision conflicts with unified state"
                )
        evidence_rows = connection.execute(
            "SELECT * FROM legacy_observations.navigation_evidence"
        ).fetchall()
        evidence_by_ref = {}
        for row in evidence_rows:
            descriptor = EvidenceDescriptor.model_validate(dict(row))
            if descriptor.ref in evidence_by_ref:
                raise LegacyObservationMigrationError("legacy evidence metadata contains duplicate ref")
            evidence_by_ref[descriptor.ref] = descriptor
            revision_key = (descriptor.task_id, descriptor.observation_revision)
            if revision_key not in revisions:
                raise LegacyObservationMigrationError(
                    "legacy evidence metadata references an unknown revision"
                )
            indexed_task, indexed_revision, _ = FileNavigationEvidenceStore._decode_ref(
                descriptor.ref
            )
            if (
                indexed_task != descriptor.task_id
                or indexed_revision != descriptor.observation_revision
            ):
                raise LegacyObservationMigrationError(
                    "legacy evidence ref ownership does not match its metadata"
                )
            target_evidence = connection.execute(
                "SELECT * FROM navigation_evidence WHERE ref = ?",
                (descriptor.ref,),
            ).fetchone()
            if (
                target_evidence is not None
                and EvidenceDescriptor.model_validate(dict(target_evidence)) != descriptor
            ):
                raise LegacyObservationMigrationError(
                    "legacy evidence metadata conflicts with unified state"
                )
        for key, revision in revisions.items():
            if len(revision.evidence_refs) != len(set(revision.evidence_refs)):
                raise LegacyObservationMigrationError("legacy revision contains duplicate evidence refs")
            for ref in revision.evidence_refs:
                descriptor = evidence_by_ref.get(ref)
                if descriptor is None or (descriptor.task_id, descriptor.observation_revision) != key:
                    raise LegacyObservationMigrationError("legacy observation evidence refs do not match metadata")
        for ref, descriptor in evidence_by_ref.items():
            if ref not in revisions[(descriptor.task_id, descriptor.observation_revision)].evidence_refs:
                raise LegacyObservationMigrationError("legacy evidence metadata is not referenced by revision")
        connection.execute(
            """
            INSERT OR IGNORE INTO navigation_observation_revisions (
                task_id, revision, phase, revision_json, created_at
            )
            SELECT task_id, revision, phase, revision_json, created_at
            FROM legacy_observations.navigation_observation_revisions
            """
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO navigation_evidence (
                ref, task_id, observation_revision, kind, summary,
                byte_size, source_tool, created_at
            )
            SELECT evidence.ref, evidence.task_id, evidence.observation_revision,
                   evidence.kind, evidence.summary,
                   evidence.byte_size, evidence.source_tool, evidence.created_at
            FROM legacy_observations.navigation_evidence AS evidence
            JOIN legacy_observations.navigation_observation_revisions AS legacy_revision
              ON legacy_revision.task_id = evidence.task_id
             AND legacy_revision.revision = evidence.observation_revision
            JOIN navigation_observation_revisions AS target_revision
              ON target_revision.task_id = legacy_revision.task_id
             AND target_revision.revision = legacy_revision.revision
             AND target_revision.revision_json = legacy_revision.revision_json
            """
        )
        connection.execute(
            "INSERT INTO navigation_service_migrations (name, completed_at) VALUES (?, ?)",
            (_LEGACY_OBSERVATION_MIGRATION, datetime.now(UTC).isoformat()),
        )
        connection.commit()
    except Exception as error:
        connection.rollback()
        if isinstance(error, LegacyObservationMigrationError):
            raise
        raise LegacyObservationMigrationError(
            "legacy observation migration failed validation or SQLite import"
        ) from error
    finally:
        if attached:
            connection.execute("DETACH DATABASE legacy_observations")
        connection.close()


def build_navigation_services(
    workspace_root: Path,
    settings: NavigationSettings | None = None,
) -> NavigationServices:
    """Build one coherent durable service bundle for a navigation workspace."""
    resolved_settings = settings or NavigationSettings()
    db_path = workspace_root / "navigation-tasks.sqlite"
    task_store = SqliteNavigationTaskStore(db_path)
    observation_store = SqliteNavigationObservationStore(db_path)
    _migrate_legacy_observations(workspace_root / "navigation-observations.sqlite", db_path)
    return NavigationServices(
        settings=resolved_settings,
        task_store=task_store,
        observation_store=observation_store,
        evidence_store=FileNavigationEvidenceStore(workspace_root / "navigation-evidence"),
        plan_store=SqliteNavigationPlanRepository(db_path),
    )

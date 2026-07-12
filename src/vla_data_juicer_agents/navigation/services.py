from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sqlite3

from vla_data_juicer_agents.navigation.config import NavigationSettings
from vla_data_juicer_agents.navigation.evidence_store import FileNavigationEvidenceStore
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


def _migrate_legacy_observations(
    legacy_db_path: Path,
    target_db_path: Path,
) -> None:
    """Copy deployed observation rows once without overwriting unified state."""
    if not legacy_db_path.is_file() or legacy_db_path == target_db_path:
        return
    connection = sqlite3.connect(target_db_path, timeout=30)
    attached = False
    try:
        connection.execute("ATTACH DATABASE ? AS legacy_observations", (str(legacy_db_path),))
        attached = True
        legacy_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM legacy_observations.sqlite_master WHERE type = 'table'"
            )
        }
        required = {"navigation_observation_revisions", "navigation_evidence"}
        if not required.issubset(legacy_tables):
            return
        connection.execute("BEGIN IMMEDIATE")
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
        connection.commit()
    except Exception:
        connection.rollback()
        raise
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
    _migrate_legacy_observations(
        workspace_root / "navigation-observations.sqlite",
        db_path,
    )
    return NavigationServices(
        settings=resolved_settings,
        task_store=task_store,
        observation_store=observation_store,
        evidence_store=FileNavigationEvidenceStore(workspace_root / "navigation-evidence"),
        plan_store=SqliteNavigationPlanRepository(db_path),
    )

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from vla_data_juicer_agents.navigation.annotation_gateway import (
    NavigationAnnotationGateway,
)
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
    annotation_gateway: NavigationAnnotationGateway | None = None


def build_navigation_services(
    workspace_root: Path,
    settings: NavigationSettings | None = None,
    *,
    annotation_gateway: NavigationAnnotationGateway | None = None,
) -> NavigationServices:
    """Build one coherent durable service bundle for a navigation workspace."""
    resolved_settings = settings or NavigationSettings()
    db_path = workspace_root / "navigation-tasks.sqlite"
    task_store = SqliteNavigationTaskStore(db_path)
    observation_store = SqliteNavigationObservationStore(db_path)
    return NavigationServices(
        settings=resolved_settings,
        task_store=task_store,
        observation_store=observation_store,
        evidence_store=FileNavigationEvidenceStore(workspace_root / "navigation-evidence"),
        plan_store=SqliteNavigationPlanRepository(db_path),
        annotation_gateway=annotation_gateway,
    )

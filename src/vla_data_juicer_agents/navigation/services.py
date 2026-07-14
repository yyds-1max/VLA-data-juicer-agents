from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

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

    def delete_control_state_for_web_session(self, web_session_id: str) -> list[str]:
        evidence_root = self.evidence_store.root.absolute()
        if evidence_root.name != "navigation-evidence" or evidence_root.is_symlink():
            raise ValueError("navigation evidence root is unsafe for deletion")
        task_ids = [
            task.task_id for task in self.task_store.find_by_web_session(web_session_id)
        ]
        for task_id in task_ids:
            task_path = evidence_root / task_id
            if task_path.parent != evidence_root:
                raise ValueError("task_id resolves outside navigation evidence root")
            if task_path.exists() and not (task_path.is_dir() or task_path.is_symlink()):
                raise ValueError("navigation evidence task path is not a directory")
        for task_id in task_ids:
            task_path = evidence_root / task_id
            if task_path.is_symlink():
                task_path.unlink()
            elif task_path.exists():
                shutil.rmtree(task_path)
        return self.task_store.delete_control_state_for_web_session(web_session_id)


def build_navigation_services(
    workspace_root: Path,
    settings: NavigationSettings | None = None,
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
    )

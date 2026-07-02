from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Protocol

from vla_data_juicer_agents.navigation.plan_draft import WorkflowPlanDraftState


def _validate_draft_state(payload: object) -> WorkflowPlanDraftState:
    return WorkflowPlanDraftState.model_validate(payload, extra="forbid")


class NavigationPlanDraftStore(Protocol):
    def load(self, session_id: str) -> WorkflowPlanDraftState | None: ...

    def save(self, session_id: str, state: WorkflowPlanDraftState) -> None: ...

    def clear(self, session_id: str) -> None: ...


class InMemoryNavigationPlanDraftStore:
    def __init__(self) -> None:
        self._states: dict[str, WorkflowPlanDraftState] = {}

    def load(self, session_id: str) -> WorkflowPlanDraftState | None:
        state = self._states.get(session_id)
        if state is None:
            return None
        return _validate_draft_state(state.model_dump(mode="json"))

    def save(self, session_id: str, state: WorkflowPlanDraftState) -> None:
        self._states[session_id] = _validate_draft_state(state.model_dump(mode="json"))

    def clear(self, session_id: str) -> None:
        self._states.pop(session_id, None)


class JsonNavigationPlanDraftStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def load(self, session_id: str) -> WorkflowPlanDraftState | None:
        path = self._path(session_id)
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        return _validate_draft_state(payload)

    def save(self, session_id: str, state: WorkflowPlanDraftState) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(session_id)
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(
            json.dumps(state.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp_path.replace(path)

    def clear(self, session_id: str) -> None:
        self._path(session_id).unlink(missing_ok=True)

    def _path(self, session_id: str) -> Path:
        digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
        return self.root / f"{digest}.json"

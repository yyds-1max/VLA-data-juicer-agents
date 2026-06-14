from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class WorkflowRunStore:
    def __init__(self, root: Path):
        self.root = root

    def create_run(self, date: str) -> Path:
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        run_dir = self.root / date / run_id
        (run_dir / "steps").mkdir(parents=True, exist_ok=False)
        return run_dir

    def write_json(self, run_dir: Path, relative_name: str, payload: dict[str, Any]) -> Path:
        path = run_dir / relative_name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
        return path

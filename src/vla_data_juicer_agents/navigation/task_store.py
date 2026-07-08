from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from vla_data_juicer_agents.navigation.task_state import (
    TASK_SCHEMA_VERSION,
    NavigationArtifactSnapshot,
    NavigationTask,
    NavigationTaskDrift,
    NavigationTaskPhase,
    NavigationTaskStatus,
    NavigationTaskStep,
    utc_now,
)


def _json_dump(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def _json_load(value: str | None) -> Any:
    if value is None:
        return None
    return json.loads(value)


class NavigationTaskStore(Protocol):
    def create_or_update_task(
        self,
        *,
        date: str,
        segments: list[str] | None,
        scene_mode: str | None,
        web_session_id: str | None = None,
        agentscope_session_id: str | None = None,
    ) -> NavigationTask: ...

    def get_task(self, task_id: str) -> NavigationTask | None: ...

    def find_latest_by_date(self, date: str, segments: list[str] | None = None) -> NavigationTask | None: ...

    def list_resumable(self, date: str | None = None) -> list[NavigationTask]: ...

    def update_task(self, task_id: str, **changes: Any) -> NavigationTask: ...

    def record_step(
        self,
        *,
        task_id: str,
        phase: NavigationTaskPhase,
        step_id: str,
        tool_name: str,
        status: NavigationTaskStatus,
        arguments: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        produced_paths: list[str] | None = None,
        started_at: str | None = None,
        finished_at: str | None = None,
    ) -> NavigationTaskStep: ...


class SqliteNavigationTaskStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS navigation_tasks (
                    task_id TEXT PRIMARY KEY,
                    date TEXT NOT NULL,
                    segments_json TEXT,
                    scene_mode TEXT,
                    phase TEXT NOT NULL,
                    status TEXT NOT NULL,
                    waiting_reason TEXT,
                    next_required_input TEXT,
                    created_by_web_session_id TEXT,
                    latest_web_session_id TEXT,
                    agentscope_session_id TEXT,
                    latest_run_id TEXT,
                    last_completed_step TEXT,
                    data_profile_json TEXT,
                    artifact_snapshot_json TEXT,
                    drift_json TEXT,
                    schema_version INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_navigation_tasks_date_updated
                ON navigation_tasks (date, updated_at)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS navigation_task_steps (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    step_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    arguments_json TEXT,
                    result_json TEXT,
                    produced_paths_json TEXT,
                    started_at TEXT,
                    finished_at TEXT,
                    FOREIGN KEY (task_id) REFERENCES navigation_tasks(task_id)
                )
                """
            )

    def create_or_update_task(
        self,
        *,
        date: str,
        segments: list[str] | None,
        scene_mode: str | None,
        web_session_id: str | None = None,
        agentscope_session_id: str | None = None,
    ) -> NavigationTask:
        existing = self.find_latest_by_date(date, segments)
        timestamp = utc_now()
        if existing is None:
            task = NavigationTask(
                task_id=f"nav_{uuid4().hex}",
                date=date,
                segments=segments,
                scene_mode=scene_mode if scene_mode in {"in", "out"} else None,
                created_by_web_session_id=web_session_id,
                latest_web_session_id=web_session_id,
                agentscope_session_id=agentscope_session_id,
                created_at=timestamp,
                updated_at=timestamp,
            )
            with self._connect() as connection:
                self._insert_task(connection, task)
            return task
        changes: dict[str, Any] = {"latest_web_session_id": web_session_id}
        if scene_mode in {"in", "out"}:
            changes["scene_mode"] = scene_mode
        if agentscope_session_id is not None:
            changes["agentscope_session_id"] = agentscope_session_id
        return self.update_task(existing.task_id, **changes)

    def get_task(self, task_id: str) -> NavigationTask | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM navigation_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        return self._task_from_row(row) if row is not None else None

    def find_latest_by_date(self, date: str, segments: list[str] | None = None) -> NavigationTask | None:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM navigation_tasks
                WHERE date = ?
                ORDER BY updated_at DESC, rowid DESC
                """,
                (date,),
            ).fetchall()
        normalized = segments or None
        for row in rows:
            task = self._task_from_row(row)
            if task.segments == normalized:
                return task
        return None

    def list_resumable(self, date: str | None = None) -> list[NavigationTask]:
        params: tuple[Any, ...]
        date_filter = ""
        if date is None:
            params = ()
        else:
            date_filter = "AND date = ?"
            params = (date,)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM navigation_tasks
                WHERE status IN (?, ?, ?, ?)
                {date_filter}
                ORDER BY updated_at DESC, rowid DESC
                """,
                (
                    NavigationTaskStatus.WAITING_USER.value,
                    NavigationTaskStatus.NEEDS_RECONCILE.value,
                    NavigationTaskStatus.NEEDS_RERUN.value,
                    NavigationTaskStatus.FAILED.value,
                    *params,
                ),
            ).fetchall()
        return [self._task_from_row(row) for row in rows]

    def update_task(self, task_id: str, **changes: Any) -> NavigationTask:
        current = self.get_task(task_id)
        if current is None:
            raise KeyError(task_id)
        payload = current.model_dump(mode="json")
        payload.update({key: value for key, value in changes.items() if value is not None})
        payload["updated_at"] = utc_now()
        task = NavigationTask.model_validate(payload)
        with self._connect() as connection:
            self._replace_task(connection, task)
        return task

    def record_step(
        self,
        *,
        task_id: str,
        phase: NavigationTaskPhase,
        step_id: str,
        tool_name: str,
        status: NavigationTaskStatus,
        arguments: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        produced_paths: list[str] | None = None,
        started_at: str | None = None,
        finished_at: str | None = None,
    ) -> NavigationTaskStep:
        step = NavigationTaskStep(
            id=f"nav_step_{uuid4().hex}",
            task_id=task_id,
            phase=phase,
            step_id=step_id,
            tool_name=tool_name,
            status=status,
            arguments=arguments,
            result=result,
            produced_paths=produced_paths or [],
            started_at=started_at,
            finished_at=finished_at,
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO navigation_task_steps (
                    id, task_id, phase, step_id, tool_name, status,
                    arguments_json, result_json, produced_paths_json,
                    started_at, finished_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    step.id,
                    step.task_id,
                    step.phase.value,
                    step.step_id,
                    step.tool_name,
                    step.status.value,
                    _json_dump(step.arguments),
                    _json_dump(step.result),
                    _json_dump(step.produced_paths),
                    step.started_at,
                    step.finished_at,
                ),
            )
        return step

    def list_steps(self, task_id: str) -> list[NavigationTaskStep]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM navigation_task_steps WHERE task_id = ? ORDER BY rowid ASC",
                (task_id,),
            ).fetchall()
        return [self._step_from_row(row) for row in rows]

    def _insert_task(self, connection: sqlite3.Connection, task: NavigationTask) -> None:
        connection.execute(
            """
            INSERT INTO navigation_tasks (
                task_id, date, segments_json, scene_mode, phase, status,
                waiting_reason, next_required_input, created_by_web_session_id,
                latest_web_session_id, agentscope_session_id, latest_run_id,
                last_completed_step, data_profile_json, artifact_snapshot_json,
                drift_json, schema_version, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            self._task_values(task),
        )

    def _replace_task(self, connection: sqlite3.Connection, task: NavigationTask) -> None:
        connection.execute("DELETE FROM navigation_tasks WHERE task_id = ?", (task.task_id,))
        self._insert_task(connection, task)

    def _task_values(self, task: NavigationTask) -> tuple[Any, ...]:
        return (
            task.task_id,
            task.date,
            _json_dump(task.segments),
            task.scene_mode,
            task.phase.value,
            task.status.value,
            task.waiting_reason,
            task.next_required_input,
            task.created_by_web_session_id,
            task.latest_web_session_id,
            task.agentscope_session_id,
            task.latest_run_id,
            task.last_completed_step,
            _json_dump(task.data_profile),
            _json_dump(task.artifact_snapshot.model_dump(mode="json") if task.artifact_snapshot else None),
            _json_dump(task.drift.model_dump(mode="json") if task.drift else None),
            TASK_SCHEMA_VERSION,
            task.created_at,
            task.updated_at,
        )

    def _task_from_row(self, row: sqlite3.Row) -> NavigationTask:
        snapshot = _json_load(row["artifact_snapshot_json"])
        drift = _json_load(row["drift_json"])
        return NavigationTask(
            task_id=row["task_id"],
            date=row["date"],
            segments=_json_load(row["segments_json"]),
            scene_mode=row["scene_mode"],
            phase=row["phase"],
            status=row["status"],
            waiting_reason=row["waiting_reason"],
            next_required_input=row["next_required_input"],
            created_by_web_session_id=row["created_by_web_session_id"],
            latest_web_session_id=row["latest_web_session_id"],
            agentscope_session_id=row["agentscope_session_id"],
            latest_run_id=row["latest_run_id"],
            last_completed_step=row["last_completed_step"],
            data_profile=_json_load(row["data_profile_json"]),
            artifact_snapshot=NavigationArtifactSnapshot.model_validate(snapshot) if snapshot else None,
            drift=NavigationTaskDrift.model_validate(drift) if drift else None,
            schema_version=row["schema_version"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _step_from_row(self, row: sqlite3.Row) -> NavigationTaskStep:
        return NavigationTaskStep(
            id=row["id"],
            task_id=row["task_id"],
            phase=row["phase"],
            step_id=row["step_id"],
            tool_name=row["tool_name"],
            status=row["status"],
            arguments=_json_load(row["arguments_json"]),
            result=_json_load(row["result_json"]),
            produced_paths=_json_load(row["produced_paths_json"]) or [],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
        )

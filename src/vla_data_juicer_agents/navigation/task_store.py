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


def normalize_segments(value: list[str] | str | None) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        decoded = _decode_json_string_list(stripped)
        if decoded is not None:
            return normalize_segments(decoded)
        return [stripped]

    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str):
            item = str(item)
        stripped = item.strip()
        if not stripped:
            continue
        decoded = _decode_json_string_list(stripped)
        if decoded is not None:
            nested = normalize_segments(decoded)
            if nested:
                normalized.extend(nested)
            continue
        normalized.append(stripped)
    return normalized or None


def _decode_json_string_list(value: str) -> list[str] | None:
    if not value.startswith("["):
        return None
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return None
    if isinstance(decoded, list) and all(isinstance(item, str) for item in decoded):
        return decoded
    return None


def _segments_key(segments: list[str] | None) -> str:
    segments = normalize_segments(segments)
    if segments is None:
        return "__all__"
    return json.dumps(sorted(segments), ensure_ascii=False, separators=(",", ":"))


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
                    segments_key TEXT,
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
            self._migrate_segments_key(connection)
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_navigation_tasks_date_updated
                ON navigation_tasks (date, updated_at)
                """
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_navigation_tasks_active_date_segments_key
                ON navigation_tasks (date, segments_key)
                WHERE status != 'superseded'
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

    def _migrate_segments_key(self, connection: sqlite3.Connection) -> None:
        connection.execute("DROP INDEX IF EXISTS idx_navigation_tasks_active_date_segments_key")
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(navigation_tasks)").fetchall()
        }
        if "segments_key" not in columns:
            connection.execute("ALTER TABLE navigation_tasks ADD COLUMN segments_key TEXT")
        rows = connection.execute(
            "SELECT rowid, segments_json FROM navigation_tasks"
        ).fetchall()
        for row in rows:
            segments = normalize_segments(_json_load(row["segments_json"]))
            connection.execute(
                """
                UPDATE navigation_tasks
                SET segments_json = ?, segments_key = ?
                WHERE rowid = ?
                """,
                (_json_dump(segments), _segments_key(segments), row["rowid"]),
            )
        duplicate_rows = connection.execute(
            """
            SELECT rowid FROM (
                SELECT
                    rowid,
                    ROW_NUMBER() OVER (
                        PARTITION BY date, segments_key
                        ORDER BY updated_at DESC, rowid DESC
                    ) AS duplicate_rank
                FROM navigation_tasks
                WHERE status != ?
            )
            WHERE duplicate_rank > 1
            """,
            (NavigationTaskStatus.SUPERSEDED.value,),
        ).fetchall()
        for row in duplicate_rows:
            connection.execute(
                """
                UPDATE navigation_tasks
                SET status = ?, updated_at = ?
                WHERE rowid = ?
                """,
                (NavigationTaskStatus.SUPERSEDED.value, utc_now(), row["rowid"]),
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
        segments = normalize_segments(segments)
        timestamp = utc_now()
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
        key = _segments_key(segments)
        with self._connect() as connection:
            self._upsert_task(connection, task)
            row = connection.execute(
                """
                SELECT * FROM navigation_tasks
                WHERE date = ? AND segments_key = ? AND status != ?
                ORDER BY updated_at DESC, rowid DESC
                LIMIT 1
                """,
                (date, key, NavigationTaskStatus.SUPERSEDED.value),
            ).fetchone()
        if row is None:
            raise RuntimeError("navigation task upsert did not return a task row")
        return self._task_from_row(row)

    def get_task(self, task_id: str) -> NavigationTask | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM navigation_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        return self._task_from_row(row) if row is not None else None

    def find_latest_by_date(self, date: str, segments: list[str] | None = None) -> NavigationTask | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM navigation_tasks
                WHERE date = ? AND segments_key = ? AND status != ?
                ORDER BY updated_at DESC, rowid DESC
                LIMIT 1
                """,
                (date, _segments_key(segments), NavigationTaskStatus.SUPERSEDED.value),
            ).fetchone()
        return self._task_from_row(row) if row is not None else None

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
        payload.update(changes)
        if "segments" in payload:
            payload["segments"] = normalize_segments(payload["segments"])
        payload["updated_at"] = utc_now()
        task = NavigationTask.model_validate(payload)
        with self._connect() as connection:
            self._update_task(connection, task)
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
                task_id, date, segments_json, segments_key, scene_mode, phase, status,
                waiting_reason, next_required_input, created_by_web_session_id,
                latest_web_session_id, agentscope_session_id, latest_run_id,
                last_completed_step, data_profile_json, artifact_snapshot_json,
                drift_json, schema_version, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            self._task_values(task),
        )

    def _upsert_task(
        self,
        connection: sqlite3.Connection,
        task: NavigationTask,
    ) -> None:
        values = self._task_values(task)
        connection.execute(
            """
            INSERT INTO navigation_tasks (
                task_id, date, segments_json, segments_key, scene_mode, phase, status,
                waiting_reason, next_required_input, created_by_web_session_id,
                latest_web_session_id, agentscope_session_id, latest_run_id,
                last_completed_step, data_profile_json, artifact_snapshot_json,
                drift_json, schema_version, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date, segments_key) WHERE status != 'superseded'
            DO UPDATE SET
                scene_mode = COALESCE(excluded.scene_mode, navigation_tasks.scene_mode),
                latest_web_session_id = COALESCE(excluded.latest_web_session_id, navigation_tasks.latest_web_session_id),
                agentscope_session_id = COALESCE(excluded.agentscope_session_id, navigation_tasks.agentscope_session_id),
                updated_at = excluded.updated_at
            """,
            values,
        )

    def _update_task(self, connection: sqlite3.Connection, task: NavigationTask) -> None:
        values = self._task_values(task)
        connection.execute(
            """
            UPDATE navigation_tasks SET
                date = ?,
                segments_json = ?,
                segments_key = ?,
                scene_mode = ?,
                phase = ?,
                status = ?,
                waiting_reason = ?,
                next_required_input = ?,
                created_by_web_session_id = ?,
                latest_web_session_id = ?,
                agentscope_session_id = ?,
                latest_run_id = ?,
                last_completed_step = ?,
                data_profile_json = ?,
                artifact_snapshot_json = ?,
                drift_json = ?,
                schema_version = ?,
                created_at = ?,
                updated_at = ?
            WHERE task_id = ?
            """,
            values[1:] + (values[0],),
        )

    def _task_values(self, task: NavigationTask) -> tuple[Any, ...]:
        return (
            task.task_id,
            task.date,
            _json_dump(task.segments),
            _segments_key(task.segments),
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
            segments=normalize_segments(_json_load(row["segments_json"])),
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

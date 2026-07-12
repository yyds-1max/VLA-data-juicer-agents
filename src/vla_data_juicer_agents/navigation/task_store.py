from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from vla_data_juicer_agents.navigation.aggregate_revision import (
    ensure_navigation_aggregate_revision_triggers,
)

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


def ensure_navigation_task_step_ledger_columns(connection: sqlite3.Connection) -> None:
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='navigation_task_steps'"
    ).fetchone() is None:
        return
    columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(navigation_task_steps)").fetchall()
    }
    migrations = {
        "plan_id": "ALTER TABLE navigation_task_steps ADD COLUMN plan_id TEXT",
        "plan_revision": (
            "ALTER TABLE navigation_task_steps ADD COLUMN plan_revision INTEGER"
        ),
        "sequence": "ALTER TABLE navigation_task_steps ADD COLUMN sequence INTEGER",
        "result_summary_json": (
            "ALTER TABLE navigation_task_steps ADD COLUMN result_summary_json TEXT"
        ),
        "result_ref": "ALTER TABLE navigation_task_steps ADD COLUMN result_ref TEXT",
        "retry_count": (
            "ALTER TABLE navigation_task_steps "
            "ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0"
        ),
    }
    for name, statement in migrations.items():
        if name not in columns:
            connection.execute(statement)
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_navigation_task_steps_plan_sequence
        ON navigation_task_steps (plan_id, sequence)
        WHERE plan_id IS NOT NULL
        """
    )
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_navigation_task_steps_plan_step_id
        ON navigation_task_steps (plan_id, step_id)
        WHERE plan_id IS NOT NULL
        """
    )


class NavigationTaskOwnershipError(PermissionError):
    def __init__(
        self,
        task_id: str,
        *,
        expected_web_session_id: str,
        requested_web_session_id: str | None,
    ) -> None:
        self.task_id = task_id
        self.expected_web_session_id = expected_web_session_id
        self.requested_web_session_id = requested_web_session_id
        super().__init__("navigation task belongs to another Web session")


class NavigationTaskStateRevisionError(RuntimeError):
    pass


def authorize_navigation_task_write(
    connection: sqlite3.Connection,
    task_id: str,
    *,
    expected_web_session_id: str | None,
    expected_agentscope_session_id: str | None,
) -> None:
    """Reject writes to an owned task unless the exact durable session is supplied."""
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='navigation_tasks'"
    ).fetchone() is None:
        return
    row = connection.execute(
        """SELECT created_by_web_session_id, latest_web_session_id,
                  agentscope_session_id
           FROM navigation_tasks WHERE task_id = ?""",
        (task_id,),
    ).fetchone()
    if row is None:
        return
    durable = (
        row["created_by_web_session_id"],
        row["latest_web_session_id"],
        row["agentscope_session_id"],
    )
    if durable == (None, None, None):
        if expected_web_session_id is None and expected_agentscope_session_id is None:
            return
    elif durable == (
        expected_web_session_id,
        expected_web_session_id,
        expected_agentscope_session_id,
    ):
        return
    raise PermissionError("navigation task session mismatch")


class NavigationTaskStore(Protocol):
    def create_or_update_task(
        self,
        *,
        date: str,
        segments: list[str] | None,
        scene_mode: str | None,
        dry_run: bool | None = None,
        web_session_id: str | None = None,
        agentscope_session_id: str | None = None,
    ) -> NavigationTask: ...

    def get_task(self, task_id: str) -> NavigationTask | None: ...

    def find_latest_by_date(self, date: str, segments: list[str] | None = None) -> NavigationTask | None: ...

    def find_latest_by_agentscope_session(self, session_id: str) -> NavigationTask | None: ...

    def list_resumable(self, date: str | None = None) -> list[NavigationTask]: ...

    def update_task(self, task_id: str, **changes: Any) -> NavigationTask: ...

    def restore_task_exact(self, task: NavigationTask) -> NavigationTask: ...

    def delete_task(self, task_id: str) -> None: ...

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
    def __init__(self, db_path: str | Path, *, initialize: bool = True) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        if initialize:
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
                    dry_run INTEGER NOT NULL DEFAULT 0,
                    guidance_revision INTEGER NOT NULL DEFAULT 0,
                    state_revision INTEGER NOT NULL DEFAULT 0,
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
            self._migrate_task_entry_fields(connection)
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
                    plan_id TEXT,
                    plan_revision INTEGER,
                    sequence INTEGER,
                    result_summary_json TEXT,
                    result_ref TEXT,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY (task_id) REFERENCES navigation_tasks(task_id)
                )
                """
            )
            ensure_navigation_task_step_ledger_columns(connection)
            ensure_navigation_aggregate_revision_triggers(connection)

    def _migrate_task_entry_fields(self, connection: sqlite3.Connection) -> None:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(navigation_tasks)").fetchall()
        }
        if "dry_run" not in columns:
            connection.execute(
                "ALTER TABLE navigation_tasks ADD COLUMN dry_run INTEGER NOT NULL DEFAULT 0"
            )
        if "guidance_revision" not in columns:
            connection.execute(
                "ALTER TABLE navigation_tasks "
                "ADD COLUMN guidance_revision INTEGER NOT NULL DEFAULT 0"
            )
        if "state_revision" not in columns:
            connection.execute(
                "ALTER TABLE navigation_tasks ADD COLUMN state_revision INTEGER NOT NULL DEFAULT 0"
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
            "SELECT rowid, segments_json, segments_key FROM navigation_tasks"
        ).fetchall()
        for row in rows:
            segments = normalize_segments(_json_load(row["segments_json"]))
            canonical_json = _json_dump(segments)
            canonical_key = _segments_key(segments)
            if row["segments_json"] == canonical_json and row["segments_key"] == canonical_key:
                continue
            connection.execute(
                """
                UPDATE navigation_tasks
                SET segments_json = ?, segments_key = ?, state_revision = state_revision + 1
                WHERE rowid = ?
                """,
                (canonical_json, canonical_key, row["rowid"]),
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
                SET status = ?, updated_at = ?, state_revision = state_revision + 1
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
        dry_run: bool | None = None,
        web_session_id: str | None = None,
        agentscope_session_id: str | None = None,
    ) -> NavigationTask:
        segments = normalize_segments(segments)
        timestamp = utc_now()
        key = _segments_key(segments)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
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
                task = NavigationTask(
                    task_id=f"nav_{uuid4().hex}",
                    date=date,
                    segments=segments,
                    scene_mode=scene_mode if scene_mode in {"in", "out"} else None,
                    dry_run=bool(dry_run),
                    state_revision=1,
                    created_by_web_session_id=web_session_id,
                    latest_web_session_id=web_session_id,
                    agentscope_session_id=agentscope_session_id,
                    created_at=timestamp,
                    updated_at=timestamp,
                )
                self._insert_task(connection, task)
            else:
                existing = self._task_from_row(row)
                creator = existing.created_by_web_session_id
                if creator is not None and creator != web_session_id:
                    raise NavigationTaskOwnershipError(
                        existing.task_id,
                        expected_web_session_id=creator,
                        requested_web_session_id=web_session_id,
                    )
                payload = existing.model_dump(mode="json")
                payload.update(
                    {
                        "scene_mode": (
                            scene_mode
                            if scene_mode in {"in", "out"}
                            else existing.scene_mode
                        ),
                        "dry_run": existing.dry_run if dry_run is None else bool(dry_run),
                        "created_by_web_session_id": creator or web_session_id,
                        "latest_web_session_id": (
                            web_session_id
                            if web_session_id is not None
                            else existing.latest_web_session_id
                        ),
                        "agentscope_session_id": (
                            agentscope_session_id
                            if agentscope_session_id is not None
                            else existing.agentscope_session_id
                        ),
                        "updated_at": timestamp,
                        "state_revision": existing.state_revision + 1,
                    }
                )
                task = NavigationTask.model_validate(payload)
                self._update_task(connection, task)
            connection.commit()
            return task
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

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

    def find_latest_by_agentscope_session(self, session_id: str) -> NavigationTask | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM navigation_tasks
                WHERE agentscope_session_id = ? AND status != ?
                ORDER BY updated_at DESC, rowid DESC
                LIMIT 1
                """,
                (session_id, NavigationTaskStatus.SUPERSEDED.value),
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
                WHERE status IN (?, ?, ?, ?, ?)
                {date_filter}
                ORDER BY updated_at DESC, rowid DESC
                """,
                (
                    NavigationTaskStatus.WAITING_USER.value,
                    NavigationTaskStatus.NEEDS_RECONCILE.value,
                    NavigationTaskStatus.NEEDS_RERUN.value,
                    NavigationTaskStatus.NEEDS_REPLAN.value,
                    NavigationTaskStatus.FAILED.value,
                    *params,
                ),
            ).fetchall()
        return [self._task_from_row(row) for row in rows]

    def update_task(self, task_id: str, **changes: Any) -> NavigationTask:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM navigation_tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise KeyError(task_id)
            authorize_navigation_task_write(
                connection,
                task_id,
                expected_web_session_id=None,
                expected_agentscope_session_id=None,
            )
            current = self._task_from_row(row)
            task = self._merged_task(current, changes)
            self._update_task(connection, task)
            connection.commit()
            return task
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def update_task_for_session(
        self,
        task_id: str,
        *,
        web_session_id: str | None,
        agentscope_session_id: str | None,
        expected_state_revision: int | None = None,
        **changes: Any,
    ) -> NavigationTask:
        """Atomically authorize the current session pair and update one task."""
        identity_fields = {
            "created_by_web_session_id",
            "latest_web_session_id",
            "agentscope_session_id",
        }
        if identity_fields.intersection(changes):
            raise ValueError("session identity fields cannot be changed by owned update")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM navigation_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                raise KeyError(task_id)
            current = self._task_from_row(row)
            try:
                authorize_navigation_task_write(
                    connection,
                    task_id,
                    expected_web_session_id=web_session_id,
                    expected_agentscope_session_id=agentscope_session_id,
                )
            except PermissionError:
                raise NavigationTaskOwnershipError(
                    task_id,
                    expected_web_session_id=current.created_by_web_session_id or "",
                    requested_web_session_id=web_session_id,
                )
            if (
                expected_state_revision is not None
                and current.state_revision != expected_state_revision
            ):
                raise NavigationTaskStateRevisionError(
                    "navigation task state revision changed"
                )
            task = self._merged_task(current, changes)
            self._update_task(connection, task)
            connection.commit()
            return task
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _merged_task(current: NavigationTask, changes: dict[str, Any]) -> NavigationTask:
        payload = current.model_dump(mode="json")
        payload.update(changes)
        if "segments" in payload:
            payload["segments"] = normalize_segments(payload["segments"])
        payload["created_at"] = current.created_at
        payload["updated_at"] = utc_now()
        payload["state_revision"] = current.state_revision + 1
        return NavigationTask.model_validate(payload)

    def restore_task_exact(self, task: NavigationTask) -> NavigationTask:
        current = self.get_task(task.task_id)
        if current is None:
            raise KeyError(task.task_id)
        task = task.model_copy(update={"state_revision": current.state_revision + 1})
        with self._connect() as connection:
            cursor = self._update_task(connection, task)
            if cursor.rowcount == 0:
                raise KeyError(task.task_id)
            row = connection.execute(
                "SELECT * FROM navigation_tasks WHERE task_id = ?",
                (task.task_id,),
            ).fetchone()
        if row is None:
            raise KeyError(task.task_id)
        return self._task_from_row(row)

    def restore_task_exact_if_current(
        self, task: NavigationTask, *, expected_state_revision: int,
        expected_web_session_id: str | None, expected_agentscope_session_id: str | None,
    ) -> bool:
        task = task.model_copy(update={"state_revision": expected_state_revision + 1})
        values = self._task_values(task)
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE navigation_tasks SET date=?, segments_json=?, segments_key=?,
                scene_mode=?, dry_run=?, guidance_revision=?, state_revision=?, phase=?, status=?,
                waiting_reason=?, next_required_input=?, created_by_web_session_id=?,
                latest_web_session_id=?, agentscope_session_id=?, latest_run_id=?,
                last_completed_step=?, data_profile_json=?, artifact_snapshot_json=?,
                drift_json=?, schema_version=?, created_at=?, updated_at=?
                WHERE task_id=? AND state_revision=? AND latest_web_session_id IS ?
                  AND agentscope_session_id IS ?""",
                values[1:] + (values[0], expected_state_revision,
                              expected_web_session_id, expected_agentscope_session_id),
            )
        return cursor.rowcount == 1

    def delete_task_if_current(
        self, task_id: str, *, expected_state_revision: int,
        expected_web_session_id: str | None, expected_agentscope_session_id: str | None,
    ) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """DELETE FROM navigation_tasks WHERE task_id=? AND state_revision=?
                AND latest_web_session_id IS ? AND agentscope_session_id IS ?""",
                (task_id, expected_state_revision, expected_web_session_id,
                 expected_agentscope_session_id),
            )
        return cursor.rowcount == 1

    def delete_task(self, task_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM navigation_task_steps WHERE task_id = ?",
                (task_id,),
            )
            cursor = connection.execute(
                "DELETE FROM navigation_tasks WHERE task_id = ?",
                (task_id,),
            )
            if cursor.rowcount == 0:
                raise KeyError(task_id)

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

    def record_step_for_session(
        self,
        *,
        web_session_id: str | None,
        agentscope_session_id: str,
        **step_fields: Any,
    ) -> NavigationTaskStep:
        """Atomically authorize the current session pair and append one legacy step."""
        step = NavigationTaskStep(
            id=f"nav_step_{uuid4().hex}",
            produced_paths=step_fields.pop("produced_paths", None) or [],
            **step_fields,
        )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM navigation_tasks WHERE task_id = ?",
                (step.task_id,),
            ).fetchone()
            if row is None:
                raise KeyError(step.task_id)
            current = self._task_from_row(row)
            if (
                current.created_by_web_session_id != web_session_id
                or current.latest_web_session_id != web_session_id
                or current.agentscope_session_id != agentscope_session_id
            ):
                raise NavigationTaskOwnershipError(
                    step.task_id,
                    expected_web_session_id=current.created_by_web_session_id or "",
                    requested_web_session_id=web_session_id,
                )
            connection.execute(
                """
                INSERT INTO navigation_task_steps (
                    id, task_id, phase, step_id, tool_name, status,
                    arguments_json, result_json, produced_paths_json,
                    started_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            connection.commit()
            return step
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

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
                task_id, date, segments_json, segments_key, scene_mode,
                dry_run, guidance_revision, state_revision, phase, status,
                waiting_reason, next_required_input, created_by_web_session_id,
                latest_web_session_id, agentscope_session_id, latest_run_id,
                last_completed_step, data_profile_json, artifact_snapshot_json,
                drift_json, schema_version, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            self._task_values(task),
        )

    def _update_task(
        self,
        connection: sqlite3.Connection,
        task: NavigationTask,
    ) -> sqlite3.Cursor:
        values = self._task_values(task)
        return connection.execute(
            """
            UPDATE navigation_tasks SET
                date = ?,
                segments_json = ?,
                segments_key = ?,
                scene_mode = ?,
                dry_run = ?,
                guidance_revision = ?,
                state_revision = ?,
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
            int(task.dry_run),
            task.guidance_revision,
            task.state_revision,
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
            task.schema_version,
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
            dry_run=bool(row["dry_run"]),
            guidance_revision=row["guidance_revision"],
            state_revision=row["state_revision"],
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

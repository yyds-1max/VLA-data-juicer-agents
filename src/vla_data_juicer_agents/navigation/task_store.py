from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any
from uuid import uuid4

from vla_data_juicer_agents.navigation.aggregate_revision import (
    ensure_navigation_aggregate_revision_triggers,
)
from vla_data_juicer_agents.navigation.catalog import (
    list_navigation_tool_capabilities,
)

from vla_data_juicer_agents.navigation.task_state import (
    TASK_SCHEMA_VERSION,
    NavigationArtifactSnapshot,
    NavigationRunningWriter,
    NavigationTask,
    NavigationTaskDrift,
    NavigationTaskStatus,
    TaskAttemptCreation,
    utc_now,
)


TRANSITIONAL_ENTRY_REQUEST = "Navigation task created by the transitional entry path."
NAVIGATION_STATE_SCHEMA_GENERATION = "navigation-attempts-transitional-v1"
_RESET_MESSAGE_MAX_CHARS = 1000
_SUPPORTED_TABLE_SQL = {
    "navigation_state_schema": """CREATE TABLE navigation_state_schema (
           singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
           generation TEXT NOT NULL
       )""",
    "navigation_tasks": """CREATE TABLE navigation_tasks (
           task_id TEXT PRIMARY KEY,
           request TEXT NOT NULL,
           target TEXT NOT NULL,
           date TEXT NOT NULL,
           segments_json TEXT,
           segments_key TEXT NOT NULL,
           scene_mode TEXT,
           dry_run INTEGER NOT NULL DEFAULT 0,
           guidance_revision INTEGER NOT NULL DEFAULT 0,
           state_revision INTEGER NOT NULL DEFAULT 0,
           phase TEXT NOT NULL,
           status TEXT NOT NULL,
           accepted_plan_phase TEXT,
           waiting_reason TEXT,
           next_required_input TEXT,
           created_by_web_session_id TEXT,
           latest_web_session_id TEXT,
           agentscope_session_id TEXT,
           latest_run_id TEXT,
           last_completed_step TEXT,
           artifact_snapshot_json TEXT,
           drift_json TEXT,
           schema_version INTEGER NOT NULL,
           created_at TEXT NOT NULL,
           updated_at TEXT NOT NULL
       )""",
    "navigation_task_steps": """CREATE TABLE navigation_task_steps (
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
       )""",
}
_SUPPORTED_INDEX_SQL = {
    "idx_navigation_tasks_date_updated": """CREATE INDEX
        idx_navigation_tasks_date_updated
        ON navigation_tasks (date, updated_at)""",
    "idx_navigation_tasks_target_history": """CREATE INDEX
        idx_navigation_tasks_target_history
        ON navigation_tasks (date, segments_key, created_at)""",
    "idx_navigation_tasks_session": """CREATE INDEX idx_navigation_tasks_session
        ON navigation_tasks (
            created_by_web_session_id, agentscope_session_id, created_at
        )""",
    "idx_navigation_tasks_attempt_replay": """CREATE UNIQUE INDEX
        idx_navigation_tasks_attempt_replay
        ON navigation_tasks (
            created_by_web_session_id, agentscope_session_id,
            date, segments_key, target
        )""",
    "idx_navigation_task_steps_plan_sequence": """CREATE UNIQUE INDEX
        idx_navigation_task_steps_plan_sequence
        ON navigation_task_steps (plan_id, sequence)
        WHERE plan_id IS NOT NULL""",
    "idx_navigation_task_steps_plan_step_id": """CREATE UNIQUE INDEX
        idx_navigation_task_steps_plan_step_id
        ON navigation_task_steps (plan_id, step_id)
        WHERE plan_id IS NOT NULL""",
}


@dataclass(frozen=True)
class _IndexContract:
    name: str
    unique: bool
    origin: str
    partial: bool
    columns: tuple[tuple[int, int, str | None, int, str | None, int], ...]
    create_sql: str | None


@dataclass(frozen=True)
class _TableContract:
    columns: tuple[tuple[str, str, int, str | None, int, int], ...]
    foreign_keys: tuple[tuple[int, int, str, str, str, str, str, str], ...]
    indexes: tuple[_IndexContract, ...]
    create_sql: str


def _normalize_create_sql(sql: str) -> str:
    return " ".join(sql.lower().split())


def _create_supported_core_schema(connection: sqlite3.Connection) -> None:
    for statement in _SUPPORTED_TABLE_SQL.values():
        connection.execute(statement)
    for statement in _SUPPORTED_INDEX_SQL.values():
        connection.execute(statement)


def _read_table_contract(
    connection: sqlite3.Connection,
    table: str,
) -> _TableContract:
    columns = tuple(
        (
            row["name"],
            row["type"],
            int(row["notnull"]),
            row["dflt_value"],
            int(row["pk"]),
            int(row["hidden"]),
        )
        for row in connection.execute(f'PRAGMA table_xinfo("{table}")').fetchall()
    )
    foreign_keys = tuple(
        (
            int(row["id"]),
            int(row["seq"]),
            row["table"],
            row["from"],
            row["to"],
            row["on_update"],
            row["on_delete"],
            row["match"],
        )
        for row in connection.execute(
            f'PRAGMA foreign_key_list("{table}")'
        ).fetchall()
    )
    indexes: list[_IndexContract] = []
    for row in connection.execute(f'PRAGMA index_list("{table}")').fetchall():
        name = row["name"]
        sql_row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
            (name,),
        ).fetchone()
        create_sql = None
        if sql_row is not None and sql_row["sql"] is not None:
            create_sql = _normalize_create_sql(sql_row["sql"])
        ordered_columns = tuple(
            (
                int(column["seqno"]),
                int(column["cid"]),
                column["name"],
                int(column["desc"]),
                column["coll"],
                int(column["key"]),
            )
            for column in connection.execute(
                f'PRAGMA index_xinfo("{name}")'
            ).fetchall()
        )
        indexes.append(
            _IndexContract(
                name=name,
                unique=bool(row["unique"]),
                origin=row["origin"],
                partial=bool(row["partial"]),
                columns=ordered_columns,
                create_sql=create_sql,
            )
        )
    indexes.sort(key=lambda index: index.name)
    table_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return _TableContract(
        columns=columns,
        foreign_keys=foreign_keys,
        indexes=tuple(indexes),
        create_sql=_normalize_create_sql(table_row["sql"]),
    )


@lru_cache(maxsize=1)
def _supported_schema_contract() -> dict[str, _TableContract]:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        _create_supported_core_schema(connection)
        return {
            table: _read_table_contract(connection, table)
            for table in _SUPPORTED_TABLE_SQL
        }
    finally:
        connection.close()


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


def navigation_targets_overlap(
    *,
    left_date: str,
    left_segments: list[str] | None,
    right_date: str,
    right_segments: list[str] | None,
) -> bool:
    if left_date != right_date:
        return False
    normalized_left = normalize_segments(left_segments)
    normalized_right = normalize_segments(right_segments)
    if normalized_left is None or normalized_right is None:
        return True
    return bool(set(normalized_left).intersection(normalized_right))


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


class NavigationStateResetRequired(RuntimeError):
    def __init__(self, db_path: str | Path, reason: str) -> None:
        self.db_path = Path(db_path)
        path_text = str(self.db_path)
        if len(path_text) > 500:
            path_text = f"...{path_text[-497:]}"
        bounded_reason = reason[:240]
        message = (
            f"Navigation state reset required for database '{path_text}': "
            f"{bounded_reason}. Stop the service, back up this database, move or "
            "remove it, then restart to create a fresh navigation-state database."
        )
        super().__init__(message[:_RESET_MESSAGE_MAX_CHARS])


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
        """SELECT created_by_web_session_id, agentscope_session_id
           FROM navigation_tasks WHERE task_id = ?""",
        (task_id,),
    ).fetchone()
    if row is None:
        return
    durable = (row["created_by_web_session_id"], row["agentscope_session_id"])
    if durable == (None, None):
        if expected_web_session_id is None and expected_agentscope_session_id is None:
            return
    elif durable == (expected_web_session_id, expected_agentscope_session_id):
        return
    raise PermissionError("navigation task session mismatch")


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

    def _read_only_connect(self) -> sqlite3.Connection:
        uri = f"{self.db_path.resolve().as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @staticmethod
    def _navigation_table_names(connection: sqlite3.Connection) -> set[str]:
        return {
            row["name"]
            for row in connection.execute(
                """SELECT name FROM sqlite_master
                   WHERE type = 'table' AND name GLOB 'navigation_*'"""
            ).fetchall()
        }

    def _schema_contract_violation(
        self,
        connection: sqlite3.Connection,
    ) -> str | None:
        tables = self._navigation_table_names(connection)
        required_tables = set(_SUPPORTED_TABLE_SQL)
        missing_tables = required_tables - tables
        if missing_tables:
            return f"missing required tables {sorted(missing_tables)}"
        expected_contract = _supported_schema_contract()
        for table in _SUPPORTED_TABLE_SQL:
            if _read_table_contract(connection, table) != expected_contract[table]:
                return f"{table} does not match the generation contract"
        marker_rows = connection.execute(
            "SELECT singleton, generation FROM navigation_state_schema"
        ).fetchall()
        if len(marker_rows) != 1 or marker_rows[0]["singleton"] != 1:
            return "navigation state schema marker is missing or ambiguous"
        if marker_rows[0]["generation"] != NAVIGATION_STATE_SCHEMA_GENERATION:
            return (
                "unsupported navigation state generation "
                f"{marker_rows[0]['generation']!r}"
            )
        return None

    def _init_schema(self) -> None:
        if self.db_path.exists() and self.db_path.stat().st_size:
            try:
                with self._read_only_connect() as connection:
                    if self._navigation_table_names(connection):
                        violation = self._schema_contract_violation(connection)
                        if violation is not None:
                            raise NavigationStateResetRequired(self.db_path, violation)
                        return
            except NavigationStateResetRequired:
                raise
            except sqlite3.DatabaseError as error:
                raise NavigationStateResetRequired(
                    self.db_path,
                    f"database cannot be inspected ({error.__class__.__name__}: {error})",
                ) from error
        self._create_supported_schema()

    def _create_supported_schema(self) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if self._navigation_table_names(connection):
                violation = self._schema_contract_violation(connection)
                if violation is not None:
                    raise NavigationStateResetRequired(self.db_path, violation)
                connection.commit()
                return
            _create_supported_core_schema(connection)
            connection.execute(
                """INSERT INTO navigation_state_schema (singleton, generation)
                   VALUES (1, ?)""",
                (NAVIGATION_STATE_SCHEMA_GENERATION,),
            )
            ensure_navigation_aggregate_revision_triggers(connection)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def create_task_attempt(
        self,
        *,
        request: str,
        target: str,
        date: str,
        segments: list[str] | None,
        scene_mode: str | None,
        dry_run: bool,
        web_session_id: str,
        agentscope_session_id: str,
    ) -> TaskAttemptCreation:
        segments = normalize_segments(segments)
        key = _segments_key(segments)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT * FROM navigation_tasks
                   WHERE created_by_web_session_id = ?
                     AND agentscope_session_id = ?
                     AND date = ? AND segments_key = ? AND target = ?
                   LIMIT 1""",
                (web_session_id, agentscope_session_id, date, key, target),
            ).fetchone()
            if row is not None:
                connection.commit()
                return TaskAttemptCreation(
                    task=self._task_from_row(row), created=False
                )

            timestamp = utc_now()
            task = NavigationTask(
                task_id=f"nav_{uuid4().hex}",
                request=request,
                target=target,
                date=date,
                segments=segments,
                scene_mode=scene_mode if scene_mode in {"in", "out"} else None,
                dry_run=bool(dry_run),
                state_revision=1,
                status=NavigationTaskStatus.ACTIVE,
                created_by_web_session_id=web_session_id,
                latest_web_session_id=web_session_id,
                agentscope_session_id=agentscope_session_id,
                created_at=timestamp,
                updated_at=timestamp,
            )
            self._insert_task(connection, task)
            connection.commit()
            return TaskAttemptCreation(task=task, created=True)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def find_by_session(
        self,
        *,
        web_session_id: str,
        agentscope_session_id: str,
    ) -> NavigationTask | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT * FROM navigation_tasks
                   WHERE created_by_web_session_id = ?
                     AND agentscope_session_id = ?
                   ORDER BY created_at DESC, rowid DESC
                   LIMIT 1""",
                (web_session_id, agentscope_session_id),
            ).fetchone()
        return self._task_from_row(row) if row is not None else None

    def find_running_target_writer(
        self,
        *,
        date: str,
        segments: list[str] | None,
    ) -> NavigationRunningWriter | None:
        locking_actions = [
            capability.tool_name
            for capability in list_navigation_tool_capabilities()
            if capability.locks_navigation_target
        ]
        if not locking_actions:
            return None
        placeholders = ", ".join("?" for _ in locking_actions)
        with self._connect() as connection:
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='navigation_plans'"
            ).fetchone() is None:
                return None
            rows = connection.execute(
                f"""SELECT tasks.task_id, tasks.date, tasks.segments_json,
                           steps.plan_id, steps.step_id, steps.tool_name AS action
                    FROM navigation_task_steps AS steps
                    JOIN navigation_tasks AS tasks
                      ON tasks.task_id = steps.task_id
                    JOIN navigation_plans AS plans
                      ON plans.plan_id = steps.plan_id
                    JOIN json_each(plans.plan_json, '$.steps') AS plan_step
                      ON json_extract(plan_step.value, '$.step_id') = steps.step_id
                     AND json_extract(plan_step.value, '$.action') = steps.tool_name
                    WHERE steps.status = 'running'
                      AND plans.status = 'active'
                      AND tasks.dry_run = 0
                      AND tasks.date = ?
                      AND steps.tool_name IN ({placeholders})
                    ORDER BY steps.started_at DESC,
                             tasks.updated_at DESC,
                             steps.rowid DESC""",
                (date, *locking_actions),
            ).fetchall()
        requested_segments = normalize_segments(segments)
        for row in rows:
            writer_segments = normalize_segments(_json_load(row["segments_json"]))
            if not navigation_targets_overlap(
                left_date=date,
                left_segments=requested_segments,
                right_date=row["date"],
                right_segments=writer_segments,
            ):
                continue
            return NavigationRunningWriter(
                task_id=row["task_id"],
                plan_id=row["plan_id"],
                step_id=row["step_id"],
                action=row["action"],
                date=row["date"],
                segments=writer_segments,
            )
        return None

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
                    request=TRANSITIONAL_ENTRY_REQUEST,
                    target=date,
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
                last_completed_step=?, artifact_snapshot_json=?,
                drift_json=?, schema_version=?, created_at=?, updated_at=?,
                request=?, target=?, accepted_plan_phase=?
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

    def _insert_task(self, connection: sqlite3.Connection, task: NavigationTask) -> None:
        connection.execute(
            """
            INSERT INTO navigation_tasks (
                task_id, date, segments_json, segments_key, scene_mode,
                dry_run, guidance_revision, state_revision, phase, status,
                waiting_reason, next_required_input, created_by_web_session_id,
                latest_web_session_id, agentscope_session_id, latest_run_id,
                last_completed_step, artifact_snapshot_json,
                drift_json, schema_version, created_at, updated_at,
                request, target, accepted_plan_phase
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                artifact_snapshot_json = ?,
                drift_json = ?,
                schema_version = ?,
                created_at = ?,
                updated_at = ?,
                request = ?,
                target = ?,
                accepted_plan_phase = ?
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
            _json_dump(task.artifact_snapshot.model_dump(mode="json") if task.artifact_snapshot else None),
            _json_dump(task.drift.model_dump(mode="json") if task.drift else None),
            task.schema_version,
            task.created_at,
            task.updated_at,
            task.request,
            task.target,
            (
                task.accepted_plan_phase.value
                if task.accepted_plan_phase is not None
                else None
            ),
        )

    def _task_from_row(self, row: sqlite3.Row) -> NavigationTask:
        snapshot = _json_load(row["artifact_snapshot_json"])
        drift = _json_load(row["drift_json"])
        return NavigationTask(
            task_id=row["task_id"],
            request=row["request"],
            target=row["target"],
            date=row["date"],
            segments=normalize_segments(_json_load(row["segments_json"])),
            scene_mode=row["scene_mode"],
            dry_run=bool(row["dry_run"]),
            guidance_revision=row["guidance_revision"],
            state_revision=row["state_revision"],
            phase=row["phase"],
            status=row["status"],
            accepted_plan_phase=row["accepted_plan_phase"],
            waiting_reason=row["waiting_reason"],
            next_required_input=row["next_required_input"],
            created_by_web_session_id=row["created_by_web_session_id"],
            latest_web_session_id=row["latest_web_session_id"],
            agentscope_session_id=row["agentscope_session_id"],
            latest_run_id=row["latest_run_id"],
            last_completed_step=row["last_completed_step"],
            artifact_snapshot=NavigationArtifactSnapshot.model_validate(snapshot) if snapshot else None,
            drift=NavigationTaskDrift.model_validate(drift) if drift else None,
            schema_version=row["schema_version"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

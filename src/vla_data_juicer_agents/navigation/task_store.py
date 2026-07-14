from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any
from uuid import uuid4

from vla_data_juicer_agents.navigation.catalog import (
    list_navigation_tool_capabilities,
)
from vla_data_juicer_agents.navigation.schema import (
    NAVIGATION_STATE_SCHEMA_GENERATION,
    NavigationStateResetRequired,
    initialize_navigation_schema,
)

from vla_data_juicer_agents.navigation.task_state import (
    TASK_SCHEMA_VERSION,
    NavigationRunningWriter,
    NavigationTask,
    NavigationTaskStatus,
    TaskAttemptCreation,
    utc_now,
)


_SAFE_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_CONTROL_CHILD_TABLES = (
    "navigation_step_result_outbox",
    "navigation_human_decision_handoffs",
    "navigation_evidence",
    "navigation_plan_submission_attempts",
    "navigation_task_steps",
    "navigation_plans",
    "navigation_observation_revisions",
)


def validate_navigation_task_id(task_id: str) -> str:
    if not isinstance(task_id, str) or _SAFE_TASK_ID.fullmatch(task_id) is None:
        raise ValueError("task_id contains unsupported path characters")
    return task_id


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


class NavigationTaskNotCurrentError(PermissionError):
    pass


def authorize_navigation_task_write(
    connection: sqlite3.Connection,
    task_id: str,
    *,
    expected_web_session_id: str | None,
    expected_agentscope_session_id: str | None,
) -> None:
    """Fence writes to the newest task for one exact durable session pair."""
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
    expected = (expected_web_session_id, expected_agentscope_session_id)
    if durable != expected:
        raise PermissionError("navigation task session mismatch")
    current = connection.execute(
        """SELECT task_id
           FROM navigation_tasks
           WHERE created_by_web_session_id IS ?
             AND agentscope_session_id IS ?
           ORDER BY created_at DESC, rowid DESC
           LIMIT 1""",
        expected,
    ).fetchone()
    if current is None or current["task_id"] != task_id:
        raise NavigationTaskNotCurrentError("navigation task session mismatch")


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
        initialize_navigation_schema(self.db_path)

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
        if not isinstance(web_session_id, str) or not web_session_id.strip():
            raise ValueError("web_session_id must be a non-empty string")
        if not isinstance(agentscope_session_id, str) or not agentscope_session_id.strip():
            raise ValueError("agentscope_session_id must be a non-empty string")
        web_session_id = web_session_id.strip()
        agentscope_session_id = agentscope_session_id.strip()
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

    def find_by_web_session(self, web_session_id: str) -> list[NavigationTask]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM navigation_tasks
                   WHERE created_by_web_session_id = ?
                   ORDER BY created_at, rowid""",
                (web_session_id,),
            ).fetchall()
        return [self._task_from_row(row) for row in rows]

    def delete_control_state_for_web_session(self, web_session_id: str) -> list[str]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """SELECT task_id FROM navigation_tasks
                   WHERE created_by_web_session_id = ?
                   ORDER BY created_at, rowid""",
                (web_session_id,),
            ).fetchall()
            task_ids = [str(row["task_id"]) for row in rows]
            for task_id in task_ids:
                validate_navigation_task_id(task_id)
            for task_id in task_ids:
                for table in _CONTROL_CHILD_TABLES:
                    connection.execute(
                        f'DELETE FROM "{table}" WHERE task_id = ?',
                        (task_id,),
                    )
                connection.execute(
                    "DELETE FROM navigation_tasks WHERE task_id = ?",
                    (task_id,),
                )
            connection.commit()
            return task_ids
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

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

    def get_task(self, task_id: str) -> NavigationTask | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM navigation_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        return self._task_from_row(row) if row is not None else None

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

    def delete_task_if_current(
        self, task_id: str, *, expected_state_revision: int,
        expected_web_session_id: str | None, expected_agentscope_session_id: str | None,
    ) -> bool:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                authorize_navigation_task_write(
                    connection,
                    task_id,
                    expected_web_session_id=expected_web_session_id,
                    expected_agentscope_session_id=expected_agentscope_session_id,
                )
            except PermissionError:
                connection.rollback()
                return False
            cursor = connection.execute(
                """DELETE FROM navigation_tasks WHERE task_id=? AND state_revision=?
                AND created_by_web_session_id IS ? AND agentscope_session_id IS ?""",
                (task_id, expected_state_revision, expected_web_session_id,
                 expected_agentscope_session_id),
            )
            connection.commit()
            return cursor.rowcount == 1
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _insert_task(self, connection: sqlite3.Connection, task: NavigationTask) -> None:
        connection.execute(
            """
            INSERT INTO navigation_tasks (
                task_id, date, segments_json, segments_key, scene_mode,
                dry_run, guidance_revision, state_revision, status,
                created_by_web_session_id, agentscope_session_id,
                schema_version, created_at, updated_at,
                request, target, accepted_plan_phase
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                status = ?,
                created_by_web_session_id = ?,
                agentscope_session_id = ?,
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
            task.status.value,
            task.created_by_web_session_id,
            task.agentscope_session_id,
            task.schema_version,
            task.created_at,
            task.updated_at,
            task.request,
            task.target,
            (
                task.accepted_plan_phase
            ),
        )

    def _task_from_row(self, row: sqlite3.Row) -> NavigationTask:
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
            status=row["status"],
            accepted_plan_phase=row["accepted_plan_phase"],
            created_by_web_session_id=row["created_by_web_session_id"],
            agentscope_session_id=row["agentscope_session_id"],
            schema_version=row["schema_version"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

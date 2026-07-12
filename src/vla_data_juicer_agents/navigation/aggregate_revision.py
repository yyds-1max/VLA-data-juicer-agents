from __future__ import annotations

import sqlite3


_CHILD_TASK_ID_COLUMNS = {
    "navigation_observation_revisions": "task_id",
    "navigation_evidence": "task_id",
    "navigation_plan_submission_attempts": "task_id",
    "navigation_plans": "task_id",
    "navigation_task_steps": "task_id",
    "navigation_step_result_outbox": "task_id",
    "navigation_human_decision_handoffs": "task_id",
}


def ensure_navigation_aggregate_revision_triggers(
    connection: sqlite3.Connection,
) -> None:
    """Install transaction-local aggregate revision triggers for existing tables.

    Stores remain usable with their historical standalone databases: triggers are
    only created when both ``navigation_tasks`` and a child table exist. Repeated
    and concurrent schema initialization is safe through SQLite's schema lock and
    ``IF NOT EXISTS``.
    """
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    if "navigation_tasks" not in tables:
        return
    for table, task_id_column in _CHILD_TASK_ID_COLUMNS.items():
        if table not in tables:
            continue
        for operation, row_alias in (("INSERT", "NEW"), ("UPDATE", "NEW"), ("DELETE", "OLD")):
            trigger = f"trg_{table}_aggregate_revision_after_{operation.lower()}"
            connection.execute(
                f"""
                CREATE TRIGGER IF NOT EXISTS {trigger}
                AFTER {operation} ON {table}
                BEGIN
                    UPDATE navigation_tasks
                    SET state_revision = state_revision + 1
                    WHERE task_id = {row_alias}.{task_id_column};
                END
                """
            )

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


NAVIGATION_STATE_SCHEMA_GENERATION = "navigation-attempts-final-v2"
_RESET_MESSAGE_MAX_CHARS = 1000


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


NAVIGATION_TABLE_SQL = {
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
        status TEXT NOT NULL,
        accepted_plan_phase TEXT,
        created_by_web_session_id TEXT NOT NULL,
        agentscope_session_id TEXT NOT NULL,
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
    "navigation_observation_revisions": """CREATE TABLE navigation_observation_revisions (
        task_id TEXT NOT NULL,
        revision INTEGER NOT NULL,
        revision_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (task_id, revision)
    )""",
    "navigation_evidence": """CREATE TABLE navigation_evidence (
        ref TEXT PRIMARY KEY,
        task_id TEXT NOT NULL,
        observation_revision INTEGER NOT NULL,
        kind TEXT NOT NULL,
        summary TEXT NOT NULL,
        byte_size INTEGER NOT NULL,
        source_tool TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY (task_id, observation_revision)
            REFERENCES navigation_observation_revisions(task_id, revision)
    )""",
    "navigation_plans": """CREATE TABLE navigation_plans (
        plan_id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL,
        phase TEXT NOT NULL CHECK (phase IN ('extract_sync', 'finish_processing')),
        plan_revision INTEGER NOT NULL,
        contract_version TEXT NOT NULL,
        observation_revision INTEGER NOT NULL,
        plan_json TEXT NOT NULL,
        validation_summary_json TEXT NOT NULL,
        status TEXT NOT NULL CHECK (
            status IN ('active', 'superseded', 'completed', 'invalidated')
        ),
        invalidation_reason TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (task_id, phase, plan_revision),
        FOREIGN KEY (task_id) REFERENCES navigation_tasks(task_id)
            ON DELETE CASCADE
    )""",
    "navigation_step_result_outbox": """CREATE TABLE navigation_step_result_outbox (
        plan_id TEXT NOT NULL,
        step_id TEXT NOT NULL,
        task_id TEXT NOT NULL,
        plan_revision INTEGER NOT NULL,
        target_status TEXT NOT NULL CHECK (target_status IN ('completed', 'failed')),
        expected_statuses_json TEXT NOT NULL,
        full_result_json TEXT NOT NULL,
        result_summary_json TEXT NOT NULL,
        result_ref TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (plan_id, step_id),
        FOREIGN KEY (plan_id) REFERENCES navigation_plans(plan_id)
            ON DELETE CASCADE,
        FOREIGN KEY (task_id) REFERENCES navigation_tasks(task_id)
            ON DELETE CASCADE
    )""",
    "navigation_human_decision_handoffs": """CREATE TABLE navigation_human_decision_handoffs (
        plan_id TEXT NOT NULL,
        step_id TEXT NOT NULL,
        task_id TEXT NOT NULL,
        decision_key TEXT NOT NULL,
        decision_json TEXT NOT NULL,
        status TEXT NOT NULL CHECK (
            status IN ('pending', 'recovery_required', 'quarantined')
        ),
        delivery_status TEXT NOT NULL DEFAULT 'pending' CHECK (
            delivery_status IN (
                'pending', 'delivering', 'delivered',
                'recovery_required', 'quarantined'
            )
        ),
        delivery_owner TEXT,
        delivery_token TEXT,
        leased_at TEXT,
        expires_at TEXT,
        recovery_reason_code TEXT,
        recovery_reason TEXT,
        recovered_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (plan_id, step_id),
        FOREIGN KEY (plan_id) REFERENCES navigation_plans(plan_id)
            ON DELETE CASCADE,
        FOREIGN KEY (task_id) REFERENCES navigation_tasks(task_id)
            ON DELETE CASCADE
    )""",
    "navigation_plan_submission_attempts": """CREATE TABLE navigation_plan_submission_attempts (
        attempt_id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL,
        phase TEXT NOT NULL CHECK (phase IN ('extract_sync', 'finish_processing')),
        planning_context_revision TEXT NOT NULL,
        candidate_json TEXT NOT NULL,
        validation_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY (task_id) REFERENCES navigation_tasks(task_id)
            ON DELETE CASCADE
    )""",
}


NAVIGATION_INDEX_SQL = {
    "idx_navigation_tasks_date_updated": """CREATE INDEX
        idx_navigation_tasks_date_updated ON navigation_tasks (date, updated_at)""",
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
    "idx_navigation_evidence_task_revision_kind": """CREATE INDEX
        idx_navigation_evidence_task_revision_kind
        ON navigation_evidence (task_id, observation_revision, kind)""",
    "idx_navigation_plans_active_task_phase": """CREATE UNIQUE INDEX
        idx_navigation_plans_active_task_phase
        ON navigation_plans (task_id, phase) WHERE status = 'active'""",
    "idx_navigation_plan_attempts_task_phase_created": """CREATE INDEX
        idx_navigation_plan_attempts_task_phase_created
        ON navigation_plan_submission_attempts (task_id, phase, created_at)""",
}


_CHILD_TASK_ID_COLUMNS = {
    "navigation_observation_revisions": "task_id",
    "navigation_evidence": "task_id",
    "navigation_plan_submission_attempts": "task_id",
    "navigation_plans": "task_id",
    "navigation_task_steps": "task_id",
    "navigation_step_result_outbox": "task_id",
    "navigation_human_decision_handoffs": "task_id",
}
NAVIGATION_TRIGGER_SQL = {
    f"trg_{table}_aggregate_revision_after_{operation.lower()}": f"""CREATE TRIGGER
        trg_{table}_aggregate_revision_after_{operation.lower()}
        AFTER {operation} ON {table}
        BEGIN
            UPDATE navigation_tasks
            SET state_revision = state_revision + 1
            WHERE task_id = {row_alias}.{task_id_column};
        END"""
    for table, task_id_column in _CHILD_TASK_ID_COLUMNS.items()
    for operation, row_alias in (("INSERT", "NEW"), ("UPDATE", "NEW"), ("DELETE", "OLD"))
}
NAVIGATION_AGGREGATE_REVISION_TRIGGER_NAMES = frozenset(NAVIGATION_TRIGGER_SQL)


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


def _normalize_sql(sql: str) -> str:
    return " ".join(sql.lower().split())


def _create_schema_objects(connection: sqlite3.Connection) -> None:
    for statement in NAVIGATION_TABLE_SQL.values():
        connection.execute(statement)
    for statement in NAVIGATION_INDEX_SQL.values():
        connection.execute(statement)
    for statement in NAVIGATION_TRIGGER_SQL.values():
        connection.execute(statement)


def _read_table_contract(connection: sqlite3.Connection, table: str) -> _TableContract:
    columns = tuple(
        (
            row["name"], row["type"], int(row["notnull"]), row["dflt_value"],
            int(row["pk"]), int(row["hidden"]),
        )
        for row in connection.execute(f'PRAGMA table_xinfo("{table}")').fetchall()
    )
    foreign_keys = tuple(
        (
            int(row["id"]), int(row["seq"]), row["table"], row["from"], row["to"],
            row["on_update"], row["on_delete"], row["match"],
        )
        for row in connection.execute(f'PRAGMA foreign_key_list("{table}")').fetchall()
    )
    indexes: list[_IndexContract] = []
    for row in connection.execute(f'PRAGMA index_list("{table}")').fetchall():
        name = row["name"]
        sql_row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?", (name,)
        ).fetchone()
        indexes.append(
            _IndexContract(
                name=name,
                unique=bool(row["unique"]),
                origin=row["origin"],
                partial=bool(row["partial"]),
                columns=tuple(
                    (
                        int(column["seqno"]), int(column["cid"]), column["name"],
                        int(column["desc"]), column["coll"], int(column["key"]),
                    )
                    for column in connection.execute(
                        f'PRAGMA index_xinfo("{name}")'
                    ).fetchall()
                ),
                create_sql=(
                    _normalize_sql(sql_row["sql"])
                    if sql_row is not None and sql_row["sql"] is not None
                    else None
                ),
            )
        )
    table_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return _TableContract(
        columns=columns,
        foreign_keys=foreign_keys,
        indexes=tuple(sorted(indexes, key=lambda index: index.name)),
        create_sql=_normalize_sql(table_row["sql"]),
    )


@lru_cache(maxsize=1)
def _supported_contract() -> tuple[dict[str, _TableContract], dict[str, str]]:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        _create_schema_objects(connection)
        tables = {
            name: _read_table_contract(connection, name)
            for name in NAVIGATION_TABLE_SQL
        }
        triggers = {
            row["name"]: _normalize_sql(row["sql"])
            for row in connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'trigger'"
            ).fetchall()
        }
        return tables, triggers
    finally:
        connection.close()


def _navigation_table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name GLOB 'navigation_*'"
        ).fetchall()
    }


def _schema_contract_violation(connection: sqlite3.Connection) -> str | None:
    tables = _navigation_table_names(connection)
    expected_tables, expected_triggers = _supported_contract()
    if tables != set(expected_tables):
        return (
            "navigation tables do not match the generation contract "
            f"(missing={sorted(set(expected_tables) - tables)}, "
            f"unexpected={sorted(tables - set(expected_tables))})"
        )
    for table, expected in expected_tables.items():
        if _read_table_contract(connection, table) != expected:
            return f"{table} does not match the generation contract"
    triggers = {
        row["name"]: _normalize_sql(row["sql"])
        for row in connection.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'trigger' AND name GLOB 'trg_navigation_*'"
        ).fetchall()
    }
    if triggers != expected_triggers:
        return "navigation aggregate triggers do not match the generation contract"
    marker_rows = connection.execute(
        "SELECT singleton, generation FROM navigation_state_schema"
    ).fetchall()
    if len(marker_rows) != 1 or marker_rows[0]["singleton"] != 1:
        return "navigation state schema marker is missing or ambiguous"
    if marker_rows[0]["generation"] != NAVIGATION_STATE_SCHEMA_GENERATION:
        return f"unsupported navigation state generation {marker_rows[0]['generation']!r}"
    return None


def _connect(db_path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    if read_only:
        connection = sqlite3.connect(
            f"{db_path.resolve().as_uri()}?mode=ro", uri=True, timeout=30
        )
    else:
        connection = sqlite3.connect(db_path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize_navigation_schema(db_path: str | Path) -> None:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size:
        try:
            with _connect(path, read_only=True) as connection:
                if _navigation_table_names(connection):
                    violation = _schema_contract_violation(connection)
                    if violation is not None:
                        raise NavigationStateResetRequired(path, violation)
                    return
        except NavigationStateResetRequired:
            raise
        except sqlite3.DatabaseError as error:
            raise NavigationStateResetRequired(
                path,
                f"database cannot be inspected ({error.__class__.__name__}: {error})",
            ) from error

    connection = _connect(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        if _navigation_table_names(connection):
            violation = _schema_contract_violation(connection)
            if violation is not None:
                raise NavigationStateResetRequired(path, violation)
            connection.commit()
            return
        _create_schema_objects(connection)
        connection.execute(
            "INSERT INTO navigation_state_schema (singleton, generation) VALUES (1, ?)",
            (NAVIGATION_STATE_SCHEMA_GENERATION,),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

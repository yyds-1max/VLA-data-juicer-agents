from __future__ import annotations

import sqlite3
from collections.abc import Callable


LATEST_SCHEMA_VERSION = 1


class UnsupportedSchemaVersionError(RuntimeError):
    """Raised before startup mutates a database created by a newer binary."""


def prepare_migration_ledger(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )
    versions = [
        int(row[0])
        for row in connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
    ]
    if versions and versions[-1] > LATEST_SCHEMA_VERSION:
        raise UnsupportedSchemaVersionError(
            "sessions database schema version "
            f"{versions[-1]} is newer than supported version {LATEST_SCHEMA_VERSION}"
        )
    expected = list(range(1, (versions[-1] if versions else 0) + 1))
    if versions != expected:
        raise RuntimeError(f"sessions database has a non-contiguous migration ledger: {versions}")


def apply_pending_migrations(connection: sqlite3.Connection, *, applied_at: str) -> None:
    applied = {
        int(row[0])
        for row in connection.execute("SELECT version FROM schema_migrations").fetchall()
    }
    for version, name, migration in _MIGRATIONS:
        if version in applied:
            continue
        migration(connection)
        connection.execute(
            "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
            (version, name, applied_at),
        )


def _migration_001_single_agent_contract(connection: sqlite3.Connection) -> None:
    session_columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(sessions)").fetchall()
    }
    add_contract_version = (
        "ALTER TABLE sessions ADD COLUMN contract_version INTEGER NOT NULL DEFAULT 0;"
        if "contract_version" not in session_columns
        else ""
    )

    # Keep the legacy-table alteration, all V1 sidecars, and the ledger insert
    # in the same transaction.  ``executescript`` commits any transaction that
    # predates the script, so BEGIN must live inside this script as well.
    connection.executescript(
        """
        BEGIN IMMEDIATE;
        """
        + add_contract_version
        + """
        CREATE TABLE IF NOT EXISTS conversation_task_bindings (
            task_id TEXT PRIMARY KEY,
            web_session_id TEXT NOT NULL,
            task_ref TEXT NOT NULL UNIQUE,
            navigation_session_id TEXT NOT NULL,
            domain TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            slot_state TEXT NOT NULL DEFAULT 'open'
                CHECK (slot_state IN ('open', 'closed')),
            state_revision INTEGER NOT NULL DEFAULT 0,
            scope_json TEXT NOT NULL DEFAULT '{}',
            latest_public_update TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            terminal_at TEXT,
            FOREIGN KEY (web_session_id) REFERENCES sessions(id)
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_task_bindings_one_open_slot
        ON conversation_task_bindings (web_session_id)
        WHERE slot_state = 'open';

        CREATE TRIGGER IF NOT EXISTS trg_sessions_contract_version_immutable
        BEFORE UPDATE OF contract_version ON sessions
        WHEN OLD.contract_version <> NEW.contract_version
        BEGIN
            SELECT RAISE(ABORT, 'sessions.contract_version is immutable');
        END;

        CREATE INDEX IF NOT EXISTS idx_task_bindings_session_updated
        ON conversation_task_bindings (web_session_id, updated_at DESC);

        CREATE TABLE IF NOT EXISTS conversation_agent_sessions (
            id TEXT PRIMARY KEY,
            web_session_id TEXT NOT NULL,
            agent_role TEXT NOT NULL CHECK (agent_role IN ('router', 'navigation')),
            agent_id TEXT NOT NULL,
            agentscope_session_id TEXT NOT NULL UNIQUE,
            task_id TEXT,
            event_cursor TEXT,
            active_turn_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (web_session_id) REFERENCES sessions(id),
            FOREIGN KEY (task_id) REFERENCES conversation_task_bindings(task_id),
            FOREIGN KEY (active_turn_id) REFERENCES web_turns(id),
            CHECK (
                (agent_role = 'router' AND task_id IS NULL)
                OR (agent_role = 'navigation' AND task_id IS NOT NULL)
            )
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_sessions_one_router
        ON conversation_agent_sessions (web_session_id)
        WHERE agent_role = 'router';

        CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_sessions_one_navigation_per_task
        ON conversation_agent_sessions (task_id)
        WHERE agent_role = 'navigation';

        CREATE TABLE IF NOT EXISTS conversation_task_focus (
            web_session_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            generation INTEGER NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (web_session_id) REFERENCES sessions(id),
            FOREIGN KEY (task_id) REFERENCES conversation_task_bindings(task_id)
        );

        CREATE TABLE IF NOT EXISTS turn_runs (
            run_id TEXT PRIMARY KEY,
            turn_id TEXT NOT NULL,
            task_id TEXT,
            producer TEXT NOT NULL,
            parent_run_id TEXT,
            agentscope_session_id TEXT,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            finished_at TEXT,
            FOREIGN KEY (turn_id) REFERENCES web_turns(id),
            FOREIGN KEY (task_id) REFERENCES conversation_task_bindings(task_id),
            FOREIGN KEY (parent_run_id) REFERENCES turn_runs(run_id)
        );

        CREATE INDEX IF NOT EXISTS idx_turn_runs_turn
        ON turn_runs (turn_id, created_at);

        CREATE TABLE IF NOT EXISTS turn_response_authority (
            turn_id TEXT PRIMARY KEY,
            producer TEXT NOT NULL,
            generation INTEGER NOT NULL,
            lease_state TEXT NOT NULL CHECK (lease_state IN ('open', 'closed')),
            final_message_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (turn_id) REFERENCES web_turns(id),
            FOREIGN KEY (final_message_id) REFERENCES messages(id)
        );

        CREATE TABLE IF NOT EXISTS interactions (
            interaction_id TEXT PRIMARY KEY,
            web_session_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            task_ref TEXT NOT NULL,
            origin_turn_id TEXT,
            kind TEXT NOT NULL,
            blocking INTEGER NOT NULL CHECK (blocking IN (0, 1)),
            risk TEXT NOT NULL,
            title TEXT NOT NULL,
            summary TEXT,
            options_json TEXT NOT NULL,
            private_payload_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL,
            revision INTEGER NOT NULL,
            expected_task_revision INTEGER NOT NULL,
            expires_at TEXT,
            response_json TEXT,
            idempotency_key TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            resolved_at TEXT,
            FOREIGN KEY (web_session_id) REFERENCES sessions(id),
            FOREIGN KEY (task_id) REFERENCES conversation_task_bindings(task_id),
            FOREIGN KEY (origin_turn_id) REFERENCES web_turns(id)
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_interactions_consumption_key
        ON interactions (idempotency_key)
        WHERE idempotency_key IS NOT NULL;

        CREATE INDEX IF NOT EXISTS idx_interactions_session_open
        ON interactions (web_session_id, status, created_at);

        CREATE TABLE IF NOT EXISTS runtime_outbox (
            outbox_id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            aggregate_type TEXT NOT NULL,
            aggregate_id TEXT NOT NULL,
            web_session_id TEXT,
            task_id TEXT,
            turn_id TEXT,
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            available_at TEXT NOT NULL,
            claimed_by TEXT,
            lease_expires_at TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT,
            FOREIGN KEY (web_session_id) REFERENCES sessions(id),
            FOREIGN KEY (task_id) REFERENCES conversation_task_bindings(task_id),
            FOREIGN KEY (turn_id) REFERENCES web_turns(id)
        );

        CREATE INDEX IF NOT EXISTS idx_runtime_outbox_ready
        ON runtime_outbox (status, available_at, created_at);

        CREATE TABLE IF NOT EXISTS runtime_resource_leases (
            lease_id TEXT PRIMARY KEY,
            resource_key TEXT NOT NULL UNIQUE,
            owner_id TEXT NOT NULL,
            task_id TEXT,
            run_id TEXT,
            kind TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (task_id) REFERENCES conversation_task_bindings(task_id)
        );
        """
    )


_MIGRATIONS: tuple[tuple[int, str, Callable[[sqlite3.Connection], None]], ...] = (
    (1, "single_agent_contract_v1", _migration_001_single_agent_contract),
)

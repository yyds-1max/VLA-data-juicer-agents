from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from collections.abc import Callable
from typing import Any, Literal
from uuid import uuid4

from vla_data_juicer_agents.web.schemas import (
    ChatMessageRecord,
    MessageRole,
    PublicEventRecord,
    PublicToolRun,
    SessionDetail,
    SessionRecord,
)


WEB_SCHEMA_GENERATION = "agentscope-native-events-v1"
WEB_CONTROL_TABLES = (
    "human_decision_consumptions",
    "public_tool_runs",
    "public_events",
    "agentscope_sessions",
    "messages",
    "sessions",
    "web_schema",
)
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


@dataclass(frozen=True)
class AgentScopeSessionMapping:
    web_session_id: str
    agent_id: str
    agentscope_session_id: str
    event_cursor: str | None = None


class WebSessionStore:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30.0)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.row_factory = sqlite3.Row
        return connection

    def _init_schema(self) -> None:
        connection = self._connect()
        try:
            # A legacy non-whitelisted table can still reference a Web control table.
            # Resetting foreign-key enforcement for this connection lets us replace only
            # the explicit development-control whitelist without touching that neighbor.
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("BEGIN IMMEDIATE")
            if self._schema_generation(connection) != WEB_SCHEMA_GENERATION:
                for table in WEB_CONTROL_TABLES:
                    connection.execute(f'DROP TABLE IF EXISTS "{table}"')
            self._create_schema(connection)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _schema_generation(connection: sqlite3.Connection) -> str | None:
        try:
            row = connection.execute(
                "SELECT generation FROM web_schema WHERE singleton = 1"
            ).fetchone()
        except sqlite3.Error:
            return None
        return str(row[0]) if row is not None else None

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (session_id) REFERENCES sessions(id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS public_events (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                dedupe_key TEXT NOT NULL,
                event_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (session_id, sequence),
                UNIQUE (session_id, dedupe_key),
                FOREIGN KEY (session_id) REFERENCES sessions(id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS public_tool_runs (
                session_id TEXT NOT NULL,
                tool_call_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN ('running', 'success', 'failure', 'stopped')
                ),
                summary TEXT NOT NULL DEFAULT '',
                error_type TEXT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                PRIMARY KEY (session_id, tool_call_id),
                FOREIGN KEY (session_id) REFERENCES sessions(id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS agentscope_sessions (
                web_session_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                agentscope_session_id TEXT NOT NULL,
                event_cursor TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (web_session_id, agent_id),
                FOREIGN KEY (web_session_id) REFERENCES sessions(id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS human_decision_consumptions (
                agentscope_session_id TEXT NOT NULL,
                reply_id TEXT NOT NULL,
                tool_call_id TEXT NOT NULL,
                action TEXT NOT NULL,
                request_id TEXT,
                consumed_at TEXT NOT NULL,
                PRIMARY KEY (agentscope_session_id, reply_id, tool_call_id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS web_schema (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                generation TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO web_schema (singleton, generation)
            VALUES (1, ?)
            ON CONFLICT(singleton) DO UPDATE SET generation = excluded.generation
            """,
            (WEB_SCHEMA_GENERATION,),
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_public_events_session_sequence
            ON public_events (session_id, sequence)
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_agentscope_sessions_agentscope_id
            ON agentscope_sessions (agentscope_session_id)
            """
        )

    def create_session(self, title: str) -> SessionRecord:
        timestamp = _now()
        record = SessionRecord(
            id=f"session_{uuid4().hex}",
            title=title,
            created_at=timestamp,
            updated_at=timestamp,
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO sessions (id, title, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (record.id, record.title, record.created_at, record.updated_at),
            )
        return record

    def list_sessions(self, limit: int = 20) -> list[SessionRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, title, created_at, updated_at
                FROM sessions
                ORDER BY updated_at DESC, rowid DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._session_from_row(row) for row in rows]

    def get_session(self, session_id: str) -> SessionDetail | None:
        with self._connect() as connection:
            session_row = connection.execute(
                """
                SELECT id, title, created_at, updated_at
                FROM sessions
                WHERE id = ?
                """,
                (session_id,),
            ).fetchone()
            if session_row is None:
                return None
            message_rows = connection.execute(
                """
                SELECT id, session_id, role, content, created_at
                FROM messages
                WHERE session_id = ?
                ORDER BY created_at ASC, rowid ASC
                """,
                (session_id,),
            ).fetchall()
            event_rows = connection.execute(
                """
                SELECT id, session_id, sequence, dedupe_key, event_json, created_at
                FROM public_events
                WHERE session_id = ?
                ORDER BY sequence ASC
                """,
                (session_id,),
            ).fetchall()
            tool_rows = connection.execute(
                """
                SELECT session_id, tool_call_id, tool_name, status, summary,
                       error_type, started_at, finished_at
                FROM public_tool_runs
                WHERE session_id = ?
                ORDER BY started_at ASC, rowid ASC
                """,
                (session_id,),
            ).fetchall()

        session = self._session_from_row(session_row)
        events = [self._public_event_from_row(row) for row in event_rows]
        return SessionDetail(
            **session.model_dump(),
            messages=[self._message_from_row(row) for row in message_rows],
            events=events,
            tool_runs=[self._tool_run_from_row(row) for row in tool_rows],
            last_sequence=events[-1].sequence if events else 0,
        )

    def append_message(self, session_id: str, *, role: MessageRole, content: str) -> ChatMessageRecord:
        timestamp = _now()
        record = ChatMessageRecord(
            id=f"message_{uuid4().hex}",
            session_id=session_id,
            role=role,
            content=content,
            created_at=timestamp,
        )
        with self._connect() as connection:
            self._require_session(connection, session_id)
            connection.execute(
                """
                INSERT INTO messages (id, session_id, role, content, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (record.id, record.session_id, record.role, record.content, record.created_at),
            )
            self._touch_session(connection, session_id, timestamp)
        return record

    def append_public_event(
        self,
        session_id: str,
        dedupe_key: str,
        event: dict[str, Any],
    ) -> PublicEventRecord:
        if _SHA256_HEX.fullmatch(dedupe_key) is None:
            raise ValueError("dedupe_key must be a lowercase SHA-256 hexadecimal digest")
        event_json = json.dumps(event, ensure_ascii=False)
        timestamp = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_session(connection, session_id)
            duplicate = connection.execute(
                """
                SELECT id, session_id, sequence, dedupe_key, event_json, created_at
                FROM public_events
                WHERE session_id = ? AND dedupe_key = ?
                """,
                (session_id, dedupe_key),
            ).fetchone()
            if duplicate is not None:
                return self._public_event_from_row(duplicate)
            sequence = int(
                connection.execute(
                    """
                    SELECT COALESCE(MAX(sequence), 0) + 1
                    FROM public_events
                    WHERE session_id = ?
                    """,
                    (session_id,),
                ).fetchone()[0]
            )
            record = PublicEventRecord(
                id=f"event_{uuid4().hex}",
                session_id=session_id,
                sequence=sequence,
                dedupe_key=dedupe_key,
                event=event,
                created_at=timestamp,
            )
            connection.execute(
                """
                INSERT INTO public_events (
                    id, session_id, sequence, dedupe_key, event_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    record.id,
                    record.session_id,
                    record.sequence,
                    record.dedupe_key,
                    event_json,
                    record.created_at,
                ),
            )
            self._touch_session(connection, session_id, timestamp)
        return record

    def list_public_events(
        self,
        session_id: str,
        after_sequence: int = 0,
    ) -> list[PublicEventRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, session_id, sequence, dedupe_key, event_json, created_at
                FROM public_events
                WHERE session_id = ? AND sequence > ?
                ORDER BY sequence ASC
                """,
                (session_id, after_sequence),
            ).fetchall()
        return [self._public_event_from_row(row) for row in rows]

    def start_tool_run(
        self,
        session_id: str,
        tool_call_id: str,
        tool_name: str,
        started_at: str,
    ) -> PublicToolRun:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_session(connection, session_id)
            connection.execute(
                """
                INSERT INTO public_tool_runs (
                    session_id, tool_call_id, tool_name, status, summary,
                    error_type, started_at, finished_at
                )
                VALUES (?, ?, ?, 'running', '', NULL, ?, NULL)
                ON CONFLICT(session_id, tool_call_id) DO NOTHING
                """,
                (session_id, tool_call_id, tool_name, started_at),
            )
            row = self._select_tool_run(connection, session_id, tool_call_id)
        assert row is not None
        return self._tool_run_from_row(row)

    def finish_tool_run(
        self,
        session_id: str,
        tool_call_id: str,
        *,
        status: Literal["success", "failure"],
        summary: str = "",
        error_type: str | None = None,
    ) -> PublicToolRun | None:
        if status not in {"success", "failure"}:
            raise ValueError("finish_tool_run status must be success or failure")
        finished_at = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE public_tool_runs
                SET status = ?, summary = ?, error_type = ?, finished_at = ?
                WHERE session_id = ? AND tool_call_id = ? AND status = 'running'
                """,
                (status, summary, error_type, finished_at, session_id, tool_call_id),
            )
            if cursor.rowcount != 1:
                return None
            row = self._select_tool_run(connection, session_id, tool_call_id)
        assert row is not None
        return self._tool_run_from_row(row)

    def stop_open_tool_runs(self, session_id: str) -> list[PublicToolRun]:
        finished_at = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT session_id, tool_call_id, tool_name, status, summary,
                       error_type, started_at, finished_at
                FROM public_tool_runs
                WHERE session_id = ? AND status = 'running'
                ORDER BY started_at ASC, rowid ASC
                """,
                (session_id,),
            ).fetchall()
            connection.execute(
                """
                UPDATE public_tool_runs
                SET status = 'stopped', finished_at = ?
                WHERE session_id = ? AND status = 'running'
                """,
                (finished_at, session_id),
            )
        return [
            self._tool_run_from_row(row).model_copy(
                update={"status": "stopped", "finished_at": finished_at}
            )
            for row in rows
        ]

    def stop_open_tool_runs_with_terminal_events(
        self,
        session_id: str,
        terminal_event_factory: Callable[
            [PublicToolRun],
            tuple[str, dict[str, Any]],
        ],
    ) -> tuple[list[PublicToolRun], list[PublicEventRecord]]:
        """Stop every running tool and append its terminal event atomically."""
        finished_at = _now()
        stopped: list[PublicToolRun] = []
        records: list[PublicEventRecord] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_session(connection, session_id)
            rows = connection.execute(
                """
                SELECT session_id, tool_call_id, tool_name, status, summary,
                       error_type, started_at, finished_at
                FROM public_tool_runs
                WHERE session_id = ? AND status = 'running'
                ORDER BY started_at ASC, rowid ASC
                """,
                (session_id,),
            ).fetchall()
            stopped = [
                self._tool_run_from_row(row).model_copy(
                    update={"status": "stopped", "finished_at": finished_at}
                )
                for row in rows
            ]
            if not stopped:
                return [], []
            connection.execute(
                """
                UPDATE public_tool_runs
                SET status = 'stopped', finished_at = ?
                WHERE session_id = ? AND status = 'running'
                """,
                (finished_at, session_id),
            )
            sequence = int(
                connection.execute(
                    """
                    SELECT COALESCE(MAX(sequence), 0) + 1
                    FROM public_events
                    WHERE session_id = ?
                    """,
                    (session_id,),
                ).fetchone()[0]
            )
            for tool_run in stopped:
                dedupe_key, event = terminal_event_factory(tool_run)
                if _SHA256_HEX.fullmatch(dedupe_key) is None:
                    raise ValueError(
                        "dedupe_key must be a lowercase SHA-256 hexadecimal digest"
                    )
                record = PublicEventRecord(
                    id=f"event_{uuid4().hex}",
                    session_id=session_id,
                    sequence=sequence,
                    dedupe_key=dedupe_key,
                    event=event,
                    created_at=finished_at,
                )
                connection.execute(
                    """
                    INSERT INTO public_events (
                        id, session_id, sequence, dedupe_key, event_json, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.id,
                        record.session_id,
                        record.sequence,
                        record.dedupe_key,
                        json.dumps(record.event, ensure_ascii=False),
                        record.created_at,
                    ),
                )
                records.append(record)
                sequence += 1
            self._touch_session(connection, session_id, finished_at)
        return stopped, records

    def mark_human_decision_consumed(
        self,
        *,
        agentscope_session_id: str,
        reply_id: str,
        tool_call_id: str,
        action: str,
        request_id: str | None = None,
    ) -> None:
        timestamp = _now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO human_decision_consumptions (
                    agentscope_session_id, reply_id, tool_call_id, action,
                    request_id, consumed_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(agentscope_session_id, reply_id, tool_call_id)
                DO UPDATE SET
                    action = excluded.action,
                    request_id = excluded.request_id,
                    consumed_at = excluded.consumed_at
                """,
                (
                    agentscope_session_id,
                    reply_id,
                    tool_call_id,
                    action,
                    request_id,
                    timestamp,
                ),
            )

    def is_human_decision_consumed(
        self,
        *,
        agentscope_session_id: str,
        reply_id: str,
        tool_call_id: str,
    ) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1
                FROM human_decision_consumptions
                WHERE agentscope_session_id = ?
                  AND reply_id = ?
                  AND tool_call_id = ?
                """,
                (agentscope_session_id, reply_id, tool_call_id),
            ).fetchone()
        return row is not None

    def delete_session(self, session_id: str) -> None:
        with self._connect() as connection:
            mappings = connection.execute(
                "SELECT agentscope_session_id FROM agentscope_sessions WHERE web_session_id = ?",
                (session_id,),
            ).fetchall()
            for mapping in mappings:
                connection.execute(
                    "DELETE FROM human_decision_consumptions WHERE agentscope_session_id = ?",
                    (mapping["agentscope_session_id"],),
                )
            connection.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            connection.execute("DELETE FROM public_events WHERE session_id = ?", (session_id,))
            connection.execute("DELETE FROM public_tool_runs WHERE session_id = ?", (session_id,))
            connection.execute(
                "DELETE FROM agentscope_sessions WHERE web_session_id = ?", (session_id,)
            )
            cursor = connection.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            if cursor.rowcount == 0:
                raise KeyError(session_id)

    def save_agentscope_session_mapping(
        self,
        web_session_id: str,
        *,
        agent_id: str,
        agentscope_session_id: str,
    ) -> None:
        timestamp = _now()
        with self._connect() as connection:
            self._require_session(connection, web_session_id)
            connection.execute(
                "UPDATE agentscope_sessions SET active = 0 WHERE web_session_id = ?",
                (web_session_id,),
            )
            connection.execute(
                """
                INSERT INTO agentscope_sessions (
                    web_session_id, agent_id, agentscope_session_id,
                    event_cursor, active, updated_at
                )
                VALUES (?, ?, ?, NULL, 1, ?)
                ON CONFLICT(web_session_id, agent_id) DO UPDATE SET
                    agentscope_session_id = excluded.agentscope_session_id,
                    event_cursor = CASE
                        WHEN agentscope_sessions.agentscope_session_id = excluded.agentscope_session_id
                        THEN agentscope_sessions.event_cursor
                        ELSE NULL
                    END,
                    active = 1,
                    updated_at = excluded.updated_at
                """,
                (web_session_id, agent_id, agentscope_session_id, timestamp),
            )

    def restore_agentscope_session_mapping(
        self,
        web_session_id: str,
        *,
        agent_id: str | None,
        agentscope_session_id: str | None,
    ) -> None:
        timestamp = _now()
        with self._connect() as connection:
            connection.execute(
                "UPDATE agentscope_sessions SET active = 0 WHERE web_session_id = ?",
                (web_session_id,),
            )
            if agent_id is None or agentscope_session_id is None:
                return
            cursor = connection.execute(
                """
                UPDATE agentscope_sessions
                SET active = 1, updated_at = ?
                WHERE web_session_id = ? AND agent_id = ? AND agentscope_session_id = ?
                """,
                (timestamp, web_session_id, agent_id, agentscope_session_id),
            )
            if cursor.rowcount == 0:
                raise KeyError((web_session_id, agent_id, agentscope_session_id))

    def get_agentscope_session_mapping(
        self,
        web_session_id: str,
    ) -> AgentScopeSessionMapping | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT web_session_id, agent_id, agentscope_session_id, event_cursor
                FROM agentscope_sessions
                WHERE web_session_id = ? AND active = 1
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (web_session_id,),
            ).fetchone()
        return self._agentscope_mapping_from_row(row) if row is not None else None

    def get_agentscope_session_mapping_for_agent(
        self,
        web_session_id: str,
        agent_id: str,
    ) -> AgentScopeSessionMapping | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT web_session_id, agent_id, agentscope_session_id, event_cursor
                FROM agentscope_sessions
                WHERE web_session_id = ? AND agent_id = ?
                """,
                (web_session_id, agent_id),
            ).fetchone()
        return self._agentscope_mapping_from_row(row) if row is not None else None

    def list_agentscope_session_mappings(
        self,
        web_session_id: str,
    ) -> list[AgentScopeSessionMapping]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT web_session_id, agent_id, agentscope_session_id, event_cursor
                FROM agentscope_sessions
                WHERE web_session_id = ?
                ORDER BY agent_id
                """,
                (web_session_id,),
            ).fetchall()
        return [self._agentscope_mapping_from_row(row) for row in rows]

    def get_agentscope_session_mapping_by_agentscope_session(
        self,
        agentscope_session_id: str,
    ) -> AgentScopeSessionMapping | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT web_session_id, agent_id, agentscope_session_id, event_cursor
                FROM agentscope_sessions
                WHERE agentscope_session_id = ?
                """,
                (agentscope_session_id,),
            ).fetchone()
        return self._agentscope_mapping_from_row(row) if row is not None else None

    def save_agentscope_event_cursor(self, agentscope_session_id: str, cursor: str) -> None:
        timestamp = _now()
        with self._connect() as connection:
            result = connection.execute(
                """
                UPDATE agentscope_sessions
                SET event_cursor = ?, updated_at = ?
                WHERE agentscope_session_id = ?
                """,
                (cursor, timestamp, agentscope_session_id),
            )
            if result.rowcount == 0:
                raise KeyError(agentscope_session_id)

    @staticmethod
    def _require_session(connection: sqlite3.Connection, session_id: str) -> None:
        exists = connection.execute(
            "SELECT 1 FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if exists is None:
            raise KeyError(session_id)

    @staticmethod
    def _touch_session(
        connection: sqlite3.Connection,
        session_id: str,
        timestamp: str,
    ) -> None:
        connection.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?",
            (timestamp, session_id),
        )

    @staticmethod
    def _select_tool_run(
        connection: sqlite3.Connection,
        session_id: str,
        tool_call_id: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            """
            SELECT session_id, tool_call_id, tool_name, status, summary,
                   error_type, started_at, finished_at
            FROM public_tool_runs
            WHERE session_id = ? AND tool_call_id = ?
            """,
            (session_id, tool_call_id),
        ).fetchone()

    @staticmethod
    def _session_from_row(row: sqlite3.Row) -> SessionRecord:
        return SessionRecord(
            id=row["id"],
            title=row["title"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _message_from_row(row: sqlite3.Row) -> ChatMessageRecord:
        return ChatMessageRecord(
            id=row["id"],
            session_id=row["session_id"],
            role=row["role"],
            content=row["content"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _public_event_from_row(row: sqlite3.Row) -> PublicEventRecord:
        try:
            event = json.loads(row["event_json"])
        except (TypeError, json.JSONDecodeError):
            event = {}
        return PublicEventRecord(
            id=row["id"],
            session_id=row["session_id"],
            sequence=int(row["sequence"]),
            dedupe_key=row["dedupe_key"],
            event=event if isinstance(event, dict) else {},
            created_at=row["created_at"],
        )

    @staticmethod
    def _tool_run_from_row(row: sqlite3.Row) -> PublicToolRun:
        return PublicToolRun(
            session_id=row["session_id"],
            tool_call_id=row["tool_call_id"],
            tool_name=row["tool_name"],
            status=row["status"],
            summary=row["summary"],
            error_type=row["error_type"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
        )

    @staticmethod
    def _agentscope_mapping_from_row(row: sqlite3.Row) -> AgentScopeSessionMapping:
        return AgentScopeSessionMapping(
            web_session_id=row["web_session_id"],
            agent_id=row["agent_id"],
            agentscope_session_id=row["agentscope_session_id"],
            event_cursor=row["event_cursor"],
        )

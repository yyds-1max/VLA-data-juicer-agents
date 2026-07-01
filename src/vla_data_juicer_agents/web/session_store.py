from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from vla_data_juicer_agents.web.schemas import (
    ChatMessageRecord,
    MessageRole,
    SessionDetail,
    SessionRecord,
)


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
        connection = sqlite3.connect(self.db_path)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.row_factory = sqlite3.Row
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL,
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
            self._migrate_agentscope_sessions_schema(connection)
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_agentscope_sessions_agentscope_id
                ON agentscope_sessions (agentscope_session_id)
                """
            )

    def _migrate_agentscope_sessions_schema(self, connection: sqlite3.Connection) -> None:
        columns = connection.execute("PRAGMA table_info(agentscope_sessions)").fetchall()
        if not columns:
            return
        primary_key_columns = [row["name"] for row in columns if row["pk"]]
        if primary_key_columns != ["web_session_id"]:
            return
        connection.execute("ALTER TABLE agentscope_sessions RENAME TO agentscope_sessions_legacy")
        connection.execute(
            """
            CREATE TABLE agentscope_sessions (
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
            INSERT INTO agentscope_sessions (
                web_session_id,
                agent_id,
                agentscope_session_id,
                event_cursor,
                active,
                updated_at
            )
            SELECT web_session_id, agent_id, agentscope_session_id, event_cursor, 1, updated_at
            FROM agentscope_sessions_legacy
            """
        )
        connection.execute("DROP TABLE agentscope_sessions_legacy")

    def create_session(self, title: str) -> SessionRecord:
        timestamp = _now()
        record = SessionRecord(
            id=f"session_{uuid4().hex}",
            title=title,
            status="active",
            created_at=timestamp,
            updated_at=timestamp,
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO sessions (id, title, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (record.id, record.title, record.status, record.created_at, record.updated_at),
            )
        return record

    def list_sessions(self, limit: int = 20) -> list[SessionRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, title, status, created_at, updated_at
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
                SELECT id, title, status, created_at, updated_at
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

        session = self._session_from_row(session_row)
        return SessionDetail(
            **session.model_dump(),
            messages=[self._message_from_row(row) for row in message_rows],
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
            exists = connection.execute(
                """
                SELECT 1
                FROM sessions
                WHERE id = ?
                """,
                (session_id,),
            ).fetchone()
            if exists is None:
                raise KeyError(session_id)
            connection.execute(
                """
                INSERT INTO messages (id, session_id, role, content, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (record.id, record.session_id, record.role, record.content, record.created_at),
            )
            connection.execute(
                """
                UPDATE sessions
                SET updated_at = ?
                WHERE id = ?
                """,
                (timestamp, session_id),
            )
        return record

    def mark_historical(self, session_id: str) -> None:
        timestamp = _now()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE sessions
                SET status = ?, updated_at = ?
                WHERE id = ?
                """,
                ("historical", timestamp, session_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(session_id)

    def delete_session(self, session_id: str) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            connection.execute("DELETE FROM agentscope_sessions WHERE web_session_id = ?", (session_id,))
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
            exists = connection.execute(
                """
                SELECT 1
                FROM sessions
                WHERE id = ?
                """,
                (web_session_id,),
            ).fetchone()
            if exists is None:
                raise KeyError(web_session_id)
            connection.execute(
                """
                UPDATE agentscope_sessions
                SET active = 0
                WHERE web_session_id = ?
                """,
                (web_session_id,),
            )
            connection.execute(
                """
                INSERT INTO agentscope_sessions (
                    web_session_id,
                    agent_id,
                    agentscope_session_id,
                    event_cursor,
                    active,
                    updated_at
                )
                VALUES (?, ?, ?, NULL, 1, ?)
                ON CONFLICT(web_session_id, agent_id) DO UPDATE SET
                    agent_id = excluded.agent_id,
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

    def get_agentscope_session_mapping(
        self,
        web_session_id: str,
    ) -> AgentScopeSessionMapping | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT web_session_id, agent_id, agentscope_session_id, event_cursor
                FROM agentscope_sessions
                WHERE web_session_id = ?
                ORDER BY active DESC, updated_at DESC
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
    def _session_from_row(row: sqlite3.Row) -> SessionRecord:
        return SessionRecord(
            id=row["id"],
            title=row["title"],
            status=row["status"],
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
    def _agentscope_mapping_from_row(row: sqlite3.Row) -> AgentScopeSessionMapping:
        return AgentScopeSessionMapping(
            web_session_id=row["web_session_id"],
            agent_id=row["agent_id"],
            agentscope_session_id=row["agentscope_session_id"],
            event_cursor=row["event_cursor"],
        )

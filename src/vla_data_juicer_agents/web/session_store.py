from __future__ import annotations

import sqlite3
import json
from datetime import UTC, datetime
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from vla_data_juicer_agents.web.schemas import (
    ChatMessageRecord,
    MessageRole,
    SessionDetail,
    SessionRecord,
    TimelineEventRecord,
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
                CREATE TABLE IF NOT EXISTS timeline_events (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    type TEXT NOT NULL,
                    source TEXT,
                    run_id TEXT,
                    parent_run_id TEXT,
                    timestamp TEXT,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (session_id) REFERENCES sessions(id)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_timeline_events_session_seq
                ON timeline_events (session_id, seq)
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
            event_rows = connection.execute(
                """
                SELECT id, session_id, seq, type, source, run_id, parent_run_id,
                       timestamp, payload_json, created_at
                FROM timeline_events
                WHERE session_id = ?
                ORDER BY seq ASC, rowid ASC
                """,
                (session_id,),
            ).fetchall()

        session = self._session_from_row(session_row)
        return SessionDetail(
            **session.model_dump(),
            messages=[self._message_from_row(row) for row in message_rows],
            events=[self._timeline_event_from_row(row) for row in event_rows],
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

    def append_timeline_event(self, session_id: str, event: dict) -> TimelineEventRecord:
        timestamp = _now()
        payload = event.get("payload")
        safe_payload = payload if isinstance(payload, dict) else {}
        record_id = f"event_{uuid4().hex}"
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
            seq = int(
                connection.execute(
                    """
                    SELECT COALESCE(MAX(seq), 0) + 1
                    FROM timeline_events
                    WHERE session_id = ?
                    """,
                    (session_id,),
                ).fetchone()[0]
            )
            connection.execute(
                """
                INSERT INTO timeline_events (
                    id,
                    session_id,
                    seq,
                    type,
                    source,
                    run_id,
                    parent_run_id,
                    timestamp,
                    payload_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record_id,
                    session_id,
                    seq,
                    str(event.get("type", "")),
                    _optional_text(event.get("source")),
                    _optional_text(event.get("run_id")),
                    _optional_text(event.get("parent_run_id")),
                    _optional_text(event.get("timestamp")),
                    json.dumps(safe_payload, ensure_ascii=False),
                    timestamp,
                ),
            )
            connection.execute(
                """
                UPDATE sessions
                SET updated_at = ?
                WHERE id = ?
                """,
                (timestamp, session_id),
            )

        return TimelineEventRecord(
            id=record_id,
            session_id=session_id,
            seq=seq,
            type=str(event.get("type", "")),
            source=_optional_text(event.get("source")),
            run_id=_optional_text(event.get("run_id")),
            parent_run_id=_optional_text(event.get("parent_run_id")),
            timestamp=_optional_text(event.get("timestamp")),
            payload=safe_payload,
            created_at=timestamp,
        )

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
            connection.execute("DELETE FROM timeline_events WHERE session_id = ?", (session_id,))
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
    def _timeline_event_from_row(row: sqlite3.Row) -> TimelineEventRecord:
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError):
            payload = {}
        return TimelineEventRecord(
            id=row["id"],
            session_id=row["session_id"],
            seq=int(row["seq"]),
            type=row["type"],
            source=row["source"],
            run_id=row["run_id"],
            parent_run_id=row["parent_run_id"],
            timestamp=row["timestamp"],
            payload=payload if isinstance(payload, dict) else {},
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


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None

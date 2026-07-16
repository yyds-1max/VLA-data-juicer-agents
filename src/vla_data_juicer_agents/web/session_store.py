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


@dataclass(frozen=True)
class UnresolvedBackgroundTool:
    web_session_id: str
    agentscope_session_id: str
    tool: str
    call_id: str


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
                    origin_key TEXT,
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
            self._migrate_timeline_events_schema(connection)
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_timeline_events_session_seq
                ON timeline_events (session_id, seq)
                """
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_timeline_events_origin_key
                ON timeline_events (origin_key)
                WHERE origin_key IS NOT NULL
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
            self._migrate_agentscope_sessions_schema(connection)
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_agentscope_sessions_agentscope_id
                ON agentscope_sessions (agentscope_session_id)
                """
            )

    @staticmethod
    def _migrate_timeline_events_schema(connection: sqlite3.Connection) -> None:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(timeline_events)").fetchall()
        }
        if columns and "origin_key" not in columns:
            connection.execute("ALTER TABLE timeline_events ADD COLUMN origin_key TEXT")

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
            duplicate = self._duplicate_human_decision_event(
                connection,
                session_id=session_id,
                event=event,
                payload=safe_payload,
            )
            if duplicate is not None:
                return duplicate
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

    def append_projected_event_batch(
        self,
        *,
        web_session_id: str,
        agentscope_session_id: str,
        entry_id: str,
        events: list[dict],
    ) -> list[TimelineEventRecord]:
        """Persist one AgentScope event projection and cursor atomically.

        One AgentScope event can project to several public events.  The
        origin key makes every projected item replay-safe while the cursor is
        advanced only after the full batch and any final message are durable.
        """
        timestamp = _now()
        inserted: list[TimelineEventRecord] = []
        with self._connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM sessions WHERE id = ?",
                (web_session_id,),
            ).fetchone()
            if exists is None:
                raise KeyError(web_session_id)
            mapping = connection.execute(
                """
                SELECT 1
                FROM agentscope_sessions
                WHERE web_session_id = ? AND agentscope_session_id = ?
                """,
                (web_session_id, agentscope_session_id),
            ).fetchone()
            if mapping is None:
                raise KeyError((web_session_id, agentscope_session_id))

            next_seq = int(
                connection.execute(
                    """
                    SELECT COALESCE(MAX(seq), 0) + 1
                    FROM timeline_events
                    WHERE session_id = ?
                    """,
                    (web_session_id,),
                ).fetchone()[0]
            )
            for projection_index, event in enumerate(events):
                origin_key = (
                    f"agentscope:{agentscope_session_id}:{entry_id}:{projection_index}"
                )
                duplicate_origin = connection.execute(
                    "SELECT 1 FROM timeline_events WHERE origin_key = ?",
                    (origin_key,),
                ).fetchone()
                if duplicate_origin is not None:
                    continue
                payload = event.get("payload")
                safe_payload = payload if isinstance(payload, dict) else {}
                if self._duplicate_terminal_tool_event(
                    connection,
                    session_id=web_session_id,
                    run_id=_optional_text(event.get("run_id")),
                    event_type=str(event.get("type", "")),
                    payload=safe_payload,
                ):
                    continue
                duplicate = self._duplicate_human_decision_event(
                    connection,
                    session_id=web_session_id,
                    event=event,
                    payload=safe_payload,
                )
                if duplicate is not None:
                    continue

                record_id = f"event_{uuid4().hex}"
                connection.execute(
                    """
                    INSERT INTO timeline_events (
                        id, session_id, seq, origin_key, type, source, run_id,
                        parent_run_id, timestamp, payload_json, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record_id,
                        web_session_id,
                        next_seq,
                        origin_key,
                        str(event.get("type", "")),
                        _optional_text(event.get("source")),
                        _optional_text(event.get("run_id")),
                        _optional_text(event.get("parent_run_id")),
                        _optional_text(event.get("timestamp")),
                        json.dumps(safe_payload, ensure_ascii=False),
                        timestamp,
                    ),
                )
                record = TimelineEventRecord(
                    id=record_id,
                    session_id=web_session_id,
                    seq=next_seq,
                    type=str(event.get("type", "")),
                    source=_optional_text(event.get("source")),
                    run_id=_optional_text(event.get("run_id")),
                    parent_run_id=_optional_text(event.get("parent_run_id")),
                    timestamp=_optional_text(event.get("timestamp")),
                    payload=safe_payload,
                    created_at=timestamp,
                )
                inserted.append(record)
                next_seq += 1

                final_text = _final_event_text(event)
                if final_text is not None:
                    connection.execute(
                        """
                        INSERT INTO messages (id, session_id, role, content, created_at)
                        VALUES (?, ?, 'assistant', ?, ?)
                        """,
                        (f"message_{uuid4().hex}", web_session_id, final_text, timestamp),
                    )

            connection.execute(
                """
                UPDATE agentscope_sessions
                SET event_cursor = ?, updated_at = ?
                WHERE web_session_id = ? AND agentscope_session_id = ?
                """,
                (entry_id, timestamp, web_session_id, agentscope_session_id),
            )
            connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (timestamp, web_session_id),
            )
        return inserted

    @staticmethod
    def _duplicate_terminal_tool_event(
        connection: sqlite3.Connection,
        *,
        session_id: str,
        run_id: str | None,
        event_type: str,
        payload: dict,
    ) -> bool:
        if event_type != "tool_end" or run_id is None:
            return False
        call_id = _optional_text(payload.get("call_id"))
        if call_id is None:
            return False
        rows = connection.execute(
            """
            SELECT payload_json
            FROM timeline_events
            WHERE session_id = ? AND run_id = ? AND type = 'tool_end'
            """,
            (session_id, run_id),
        ).fetchall()
        for row in rows:
            try:
                existing = json.loads(row["payload_json"])
            except (TypeError, ValueError):
                continue
            if isinstance(existing, dict) and existing.get("call_id") == call_id:
                return True
        return False

    def list_unresolved_background_tools(self) -> list[UnresolvedBackgroundTool]:
        """Return background calls that have no durable public terminal event."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT session_id, run_id, type, payload_json
                FROM timeline_events
                WHERE type IN ('tool_background', 'tool_end')
                ORDER BY session_id ASC, seq ASC
                """
            ).fetchall()
        unresolved: dict[tuple[str, str, str], UnresolvedBackgroundTool] = {}
        for row in rows:
            run_id = _optional_text(row["run_id"])
            if run_id is None:
                continue
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            call_id = _optional_text(payload.get("call_id"))
            tool = _optional_text(payload.get("tool"))
            if call_id is None:
                continue
            key = (row["session_id"], run_id, call_id)
            if row["type"] == "tool_end":
                unresolved.pop(key, None)
            elif tool is not None:
                unresolved[key] = UnresolvedBackgroundTool(
                    web_session_id=row["session_id"],
                    agentscope_session_id=run_id,
                    tool=tool,
                    call_id=call_id,
                )
        return list(unresolved.values())

    def append_background_tool_reconciliation(
        self,
        background: UnresolvedBackgroundTool,
        *,
        status: str,
    ) -> TimelineEventRecord | None:
        """Idempotently close one orphaned background call without moving a cursor."""
        if status not in {"completed", "failed", "interrupted"}:
            raise ValueError(f"unsupported terminal tool status: {status}")
        timestamp = _now()
        origin_key = (
            "background-reconcile:"
            f"{background.agentscope_session_id}:{background.call_id}"
        )
        payload = {
            "tool": background.tool,
            "call_id": background.call_id,
            "status": status,
        }
        with self._connect() as connection:
            duplicate_origin = connection.execute(
                "SELECT 1 FROM timeline_events WHERE origin_key = ?",
                (origin_key,),
            ).fetchone()
            if duplicate_origin is not None:
                return None
            rows = connection.execute(
                """
                SELECT payload_json
                FROM timeline_events
                WHERE session_id = ? AND run_id = ? AND type = 'tool_end'
                """,
                (background.web_session_id, background.agentscope_session_id),
            ).fetchall()
            for row in rows:
                try:
                    existing_payload = json.loads(row["payload_json"])
                except (TypeError, ValueError):
                    continue
                if (
                    isinstance(existing_payload, dict)
                    and existing_payload.get("call_id") == background.call_id
                ):
                    return None
            next_seq = int(
                connection.execute(
                    """
                    SELECT COALESCE(MAX(seq), 0) + 1
                    FROM timeline_events
                    WHERE session_id = ?
                    """,
                    (background.web_session_id,),
                ).fetchone()[0]
            )
            record_id = f"event_{uuid4().hex}"
            connection.execute(
                """
                INSERT INTO timeline_events (
                    id, session_id, seq, origin_key, type, source, run_id,
                    parent_run_id, timestamp, payload_json, created_at
                ) VALUES (?, ?, ?, ?, 'tool_end', 'agentscope', ?, NULL, ?, ?, ?)
                """,
                (
                    record_id,
                    background.web_session_id,
                    next_seq,
                    origin_key,
                    background.agentscope_session_id,
                    timestamp,
                    json.dumps(payload, ensure_ascii=False),
                    timestamp,
                ),
            )
            connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (timestamp, background.web_session_id),
            )
        return TimelineEventRecord(
            id=record_id,
            session_id=background.web_session_id,
            seq=next_seq,
            type="tool_end",
            source="agentscope",
            run_id=background.agentscope_session_id,
            parent_run_id=None,
            timestamp=timestamp,
            payload=payload,
            created_at=timestamp,
        )

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
                    agentscope_session_id,
                    reply_id,
                    tool_call_id,
                    action,
                    request_id,
                    consumed_at
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
                WHERE web_session_id = ?
                  AND agent_id = ?
                  AND agentscope_session_id = ?
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
                ORDER BY active DESC, updated_at DESC
                LIMIT 1
                """,
                (web_session_id,),
            ).fetchone()
        return self._agentscope_mapping_from_row(row) if row is not None else None

    def list_agentscope_session_mappings(self) -> list[AgentScopeSessionMapping]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT web_session_id, agent_id, agentscope_session_id, event_cursor
                FROM agentscope_sessions
                ORDER BY web_session_id ASC, rowid ASC
                """
            ).fetchall()
        return [self._agentscope_mapping_from_row(row) for row in rows]

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

    def _duplicate_human_decision_event(
        self,
        connection: sqlite3.Connection,
        *,
        session_id: str,
        event: dict,
        payload: dict,
    ) -> TimelineEventRecord | None:
        if event.get("type") != "human_decision_required":
            return None
        reply_id = payload.get("reply_id")
        tool_call_id = payload.get("tool_call_id")
        if not isinstance(reply_id, str) or not isinstance(tool_call_id, str):
            return None
        rows = connection.execute(
            """
            SELECT id, session_id, seq, type, source, run_id, parent_run_id,
                   timestamp, payload_json, created_at
            FROM timeline_events
            WHERE session_id = ?
              AND type = ?
              AND run_id IS ?
            ORDER BY seq ASC
            """,
            (
                session_id,
                "human_decision_required",
                _optional_text(event.get("run_id")),
            ),
        ).fetchall()
        for row in rows:
            existing = self._timeline_event_from_row(row)
            if (
                existing.payload.get("reply_id") == reply_id
                and existing.payload.get("tool_call_id") == tool_call_id
            ):
                if (
                    payload.get("recovery_required") is True
                    and existing.payload.get("recovery_required") is not True
                ):
                    connection.execute(
                        "UPDATE timeline_events SET payload_json = ? WHERE id = ?",
                        (json.dumps(payload, ensure_ascii=False), existing.id),
                    )
                    return existing.model_copy(update={"payload": payload})
                return existing
        return None

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


def _final_event_text(event: dict) -> str | None:
    if event.get("type") != "final":
        return None
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return None
    text = payload.get("text")
    return text if isinstance(text, str) and text else None

from __future__ import annotations

import sqlite3
import json
from datetime import UTC, datetime
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from vla_data_juicer_agents.adapters.agentscope.events import sanitize_public_reply
from vla_data_juicer_agents.web.schemas import (
    ChatMessageRecord,
    MessageRole,
    SessionDetail,
    SessionRecord,
    TimelineEventRecord,
    TurnRecord,
)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


_SAFE_PUBLIC_REPLY_FAILURE = "本轮处理已结束，但未能生成可安全展示的回复。请重试。"


@dataclass(frozen=True)
class AgentScopeSessionMapping:
    web_session_id: str
    agent_id: str
    agentscope_session_id: str
    event_cursor: str | None = None
    active_turn_id: str | None = None


@dataclass(frozen=True)
class UnresolvedBackgroundTool:
    web_session_id: str
    agentscope_session_id: str
    tool: str
    call_id: str
    turn_id: str | None = None


@dataclass(frozen=True)
class TurnSubmission:
    turn: TurnRecord
    message: ChatMessageRecord
    events: tuple[TimelineEventRecord, ...]


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
                    turn_id TEXT,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (session_id) REFERENCES sessions(id),
                    FOREIGN KEY (turn_id) REFERENCES web_turns(id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS timeline_events (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    turn_id TEXT,
                    seq INTEGER NOT NULL,
                    origin_key TEXT,
                    type TEXT NOT NULL,
                    source TEXT,
                    run_id TEXT,
                    parent_run_id TEXT,
                    timestamp TEXT,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (session_id) REFERENCES sessions(id),
                    FOREIGN KEY (turn_id) REFERENCES web_turns(id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS web_turns (
                    id TEXT PRIMARY KEY,
                    web_session_id TEXT NOT NULL,
                    origin TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    final_message_id TEXT,
                    FOREIGN KEY (web_session_id) REFERENCES sessions(id),
                    FOREIGN KEY (final_message_id) REFERENCES messages(id)
                )
                """
            )
            self._migrate_turn_columns(connection)
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
                CREATE UNIQUE INDEX IF NOT EXISTS idx_web_turns_one_active
                ON web_turns (web_session_id)
                WHERE status IN ('running', 'waiting')
                """
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_timeline_events_turn_final
                ON timeline_events (turn_id)
                WHERE turn_id IS NOT NULL AND type = 'final'
                """
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_turn_assistant
                ON messages (turn_id)
                WHERE turn_id IS NOT NULL AND role = 'assistant'
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
                    active_turn_id TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (web_session_id, agent_id),
                    FOREIGN KEY (web_session_id) REFERENCES sessions(id),
                    FOREIGN KEY (active_turn_id) REFERENCES web_turns(id)
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
            self._migrate_agentscope_turn_column(connection)
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS agentscope_turn_replies (
                    id TEXT PRIMARY KEY,
                    turn_id TEXT NOT NULL,
                    agentscope_session_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    reply_id TEXT,
                    source TEXT NOT NULL,
                    status TEXT NOT NULL,
                    summary_text TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (turn_id) REFERENCES web_turns(id)
                )
                """
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_turn_replies_reply
                ON agentscope_turn_replies (agentscope_session_id, reply_id)
                WHERE reply_id IS NOT NULL
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS agentscope_turn_tools (
                    turn_id TEXT NOT NULL,
                    agentscope_session_id TEXT NOT NULL,
                    call_id TEXT NOT NULL,
                    tool TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (turn_id, agentscope_session_id, call_id),
                    FOREIGN KEY (turn_id) REFERENCES web_turns(id)
                )
                """
            )
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

    @staticmethod
    def _migrate_turn_columns(connection: sqlite3.Connection) -> None:
        message_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(messages)").fetchall()
        }
        if message_columns and "turn_id" not in message_columns:
            connection.execute("ALTER TABLE messages ADD COLUMN turn_id TEXT")
        event_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(timeline_events)").fetchall()
        }
        if event_columns and "turn_id" not in event_columns:
            connection.execute("ALTER TABLE timeline_events ADD COLUMN turn_id TEXT")

    @staticmethod
    def _migrate_agentscope_turn_column(connection: sqlite3.Connection) -> None:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(agentscope_sessions)").fetchall()
        }
        if columns and "active_turn_id" not in columns:
            connection.execute("ALTER TABLE agentscope_sessions ADD COLUMN active_turn_id TEXT")

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
                active_turn_id TEXT,
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
                active_turn_id,
                updated_at
            )
            SELECT web_session_id, agent_id, agentscope_session_id, event_cursor, 1, NULL, updated_at
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

    def begin_user_turn(self, session_id: str, message: str) -> TurnSubmission:
        """Create the authoritative user turn, message and public start event atomically."""
        timestamp = _now()
        turn = TurnRecord(
            id=f"turn_{uuid4().hex}",
            web_session_id=session_id,
            origin="user",
            status="running",
            started_at=timestamp,
        )
        message_record = ChatMessageRecord(
            id=f"message_{uuid4().hex}",
            session_id=session_id,
            turn_id=turn.id,
            role="user",
            content=message,
            created_at=timestamp,
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone() is None:
                raise KeyError(session_id)
            if connection.execute(
                """
                SELECT 1 FROM web_turns
                WHERE web_session_id = ? AND status IN ('running', 'waiting')
                """,
                (session_id,),
            ).fetchone() is not None:
                raise RuntimeError("the session already has an active turn")
            connection.execute(
                """
                INSERT INTO web_turns (
                    id, web_session_id, origin, status, started_at, finished_at, final_message_id
                ) VALUES (?, ?, 'user', 'running', ?, NULL, NULL)
                """,
                (turn.id, session_id, timestamp),
            )
            connection.execute(
                """
                INSERT INTO messages (id, session_id, turn_id, role, content, created_at)
                VALUES (?, ?, ?, 'user', ?, ?)
                """,
                (message_record.id, session_id, turn.id, message, timestamp),
            )
            event = self._insert_timeline_event(
                connection,
                session_id=session_id,
                turn_id=turn.id,
                event={
                    "type": "turn_start",
                    "timestamp": timestamp,
                    "payload": {"status": "running", "started_at": timestamp},
                },
                created_at=timestamp,
            )
            connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (timestamp, session_id),
            )
        return TurnSubmission(turn=turn, message=message_record, events=(event,))

    def abort_initial_turn(self, turn_id: str) -> None:
        """Compensate a rejected initial spawn without leaving visible transcript data."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT web_session_id, final_message_id FROM web_turns WHERE id = ?",
                (turn_id,),
            ).fetchone()
            if row is None:
                return
            connection.execute(
                "UPDATE agentscope_sessions SET active_turn_id = NULL WHERE active_turn_id = ?",
                (turn_id,),
            )
            connection.execute("DELETE FROM agentscope_turn_tools WHERE turn_id = ?", (turn_id,))
            connection.execute("DELETE FROM agentscope_turn_replies WHERE turn_id = ?", (turn_id,))
            connection.execute("DELETE FROM timeline_events WHERE turn_id = ?", (turn_id,))
            connection.execute("DELETE FROM messages WHERE turn_id = ?", (turn_id,))
            connection.execute("DELETE FROM web_turns WHERE id = ?", (turn_id,))

    def get_active_turn(self, web_session_id: str) -> TurnRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, web_session_id, origin, status, started_at, finished_at,
                       final_message_id
                FROM web_turns
                WHERE web_session_id = ? AND status IN ('running', 'waiting')
                ORDER BY started_at DESC LIMIT 1
                """,
                (web_session_id,),
            ).fetchone()
        return self._turn_from_row(row) if row is not None else None

    def reconcile_stale_reply_leases(self) -> int:
        """Release pre-restart pending/running leases so a recovered run can take over."""
        timestamp = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE agentscope_turn_replies
                SET status = 'interrupted', updated_at = ?
                WHERE status IN ('pending', 'running')
                  AND turn_id IN (
                      SELECT id FROM web_turns WHERE status IN ('running', 'waiting')
                  )
                """,
                (timestamp,),
            )
        return max(cursor.rowcount, 0)

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
                SELECT id, session_id, turn_id, role, content, created_at
                FROM messages
                WHERE session_id = ?
                ORDER BY created_at ASC, rowid ASC
                """,
                (session_id,),
            ).fetchall()
            event_rows = connection.execute(
                """
                SELECT id, session_id, turn_id, seq, type, source, run_id, parent_run_id,
                       timestamp, payload_json, created_at
                FROM timeline_events
                WHERE session_id = ?
                ORDER BY seq ASC, rowid ASC
                """,
                (session_id,),
            ).fetchall()
            turn_rows = connection.execute(
                """
                SELECT id, web_session_id, origin, status, started_at, finished_at,
                       final_message_id
                FROM web_turns
                WHERE web_session_id = ?
                ORDER BY started_at ASC, rowid ASC
                """,
                (session_id,),
            ).fetchall()

        session = self._session_from_row(session_row)
        return SessionDetail(
            **session.model_dump(),
            messages=[self._message_from_row(row) for row in message_rows],
            events=[self._timeline_event_from_row(row) for row in event_rows],
            turns=[self._turn_from_row(row) for row in turn_rows],
        )

    def append_message(
        self,
        session_id: str,
        *,
        role: MessageRole,
        content: str,
        turn_id: str | None = None,
    ) -> ChatMessageRecord:
        timestamp = _now()
        record = ChatMessageRecord(
            id=f"message_{uuid4().hex}",
            session_id=session_id,
            role=role,
            content=content,
            created_at=timestamp,
            turn_id=turn_id,
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
                INSERT INTO messages (id, session_id, turn_id, role, content, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    record.id,
                    record.session_id,
                    record.turn_id,
                    record.role,
                    record.content,
                    record.created_at,
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
        return record

    def append_timeline_event(self, session_id: str, event: dict) -> TimelineEventRecord:
        timestamp = _now()
        payload = event.get("payload")
        safe_payload = payload if isinstance(payload, dict) else {}
        record_id = f"event_{uuid4().hex}"
        turn_id = _optional_text(event.get("turn_id"))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
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
                    turn_id,
                    seq,
                    type,
                    source,
                    run_id,
                    parent_run_id,
                    timestamp,
                    payload_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record_id,
                    session_id,
                    turn_id,
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
            turn_id=turn_id,
        )

    def _insert_timeline_event(
        self,
        connection: sqlite3.Connection,
        *,
        session_id: str,
        turn_id: str | None,
        event: dict,
        created_at: str,
        origin_key: str | None = None,
    ) -> TimelineEventRecord:
        payload = event.get("payload")
        safe_payload = payload if isinstance(payload, dict) else {}
        seq = int(
            connection.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM timeline_events WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0]
        )
        record = TimelineEventRecord(
            id=f"event_{uuid4().hex}",
            session_id=session_id,
            turn_id=turn_id,
            seq=seq,
            type=str(event.get("type", "")),
            source=_optional_text(event.get("source")),
            run_id=_optional_text(event.get("run_id")),
            parent_run_id=_optional_text(event.get("parent_run_id")),
            timestamp=_optional_text(event.get("timestamp")),
            payload=safe_payload,
            created_at=created_at,
        )
        connection.execute(
            """
            INSERT INTO timeline_events (
                id, session_id, turn_id, seq, origin_key, type, source, run_id,
                parent_run_id, timestamp, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.id,
                session_id,
                turn_id,
                seq,
                origin_key,
                record.type,
                record.source,
                record.run_id,
                record.parent_run_id,
                record.timestamp,
                json.dumps(safe_payload, ensure_ascii=False),
                created_at,
            ),
        )
        return record

    def append_projected_event_batch(
        self,
        *,
        web_session_id: str,
        agentscope_session_id: str,
        entry_id: str,
        events: list[dict],
        raw_event_type: str | None = None,
        reply_id: str | None = None,
    ) -> list[TimelineEventRecord]:
        """Persist projection, turn state, final message and cursor atomically."""
        timestamp = _now()
        inserted: list[TimelineEventRecord] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            exists = connection.execute(
                "SELECT 1 FROM sessions WHERE id = ?",
                (web_session_id,),
            ).fetchone()
            if exists is None:
                raise KeyError(web_session_id)
            mapping = connection.execute(
                """
                SELECT agent_id, active_turn_id
                FROM agentscope_sessions
                WHERE web_session_id = ? AND agentscope_session_id = ?
                """,
                (web_session_id, agentscope_session_id),
            ).fetchone()
            if mapping is None:
                raise KeyError((web_session_id, agentscope_session_id))

            turn_id = _optional_text(mapping["active_turn_id"])
            known_reply = (
                connection.execute(
                    """
                    SELECT replies.turn_id, turns.status
                    FROM agentscope_turn_replies AS replies
                    JOIN web_turns AS turns ON turns.id = replies.turn_id
                    WHERE replies.agentscope_session_id = ? AND replies.reply_id = ?
                    ORDER BY replies.updated_at DESC, replies.rowid DESC LIMIT 1
                    """,
                    (agentscope_session_id, reply_id),
                ).fetchone()
                if reply_id is not None
                else None
            )
            if known_reply is not None and known_reply["status"] in {
                "completed",
                "failed",
                "interrupted",
            }:
                connection.execute(
                    """
                    UPDATE agentscope_sessions
                    SET event_cursor = ?, updated_at = ?
                    WHERE web_session_id = ? AND agentscope_session_id = ?
                    """,
                    (entry_id, timestamp, web_session_id, agentscope_session_id),
                )
                return []
            if turn_id is None and known_reply is not None:
                turn_id = _optional_text(known_reply["turn_id"])
            if turn_id is None:
                active_turn = connection.execute(
                    """
                    SELECT id FROM web_turns
                    WHERE web_session_id = ? AND status IN ('running', 'waiting')
                    ORDER BY started_at DESC LIMIT 1
                    """,
                    (web_session_id,),
                ).fetchone()
                turn_id = _optional_text(active_turn["id"]) if active_turn is not None else None
            if turn_id is None and raw_event_type == "REPLY_START":
                turn_id = f"turn_{uuid4().hex}"
                connection.execute(
                    """
                    INSERT INTO web_turns (
                        id, web_session_id, origin, status, started_at, finished_at,
                        final_message_id
                    ) VALUES (?, ?, 'system', 'running', ?, NULL, NULL)
                    """,
                    (turn_id, web_session_id, timestamp),
                )
                turn_start_origin = (
                    f"agentscope:{agentscope_session_id}:{entry_id}:turn-start"
                )
                if connection.execute(
                    "SELECT 1 FROM timeline_events WHERE origin_key = ?",
                    (turn_start_origin,),
                ).fetchone() is None:
                    inserted.append(
                        self._insert_timeline_event(
                            connection,
                            session_id=web_session_id,
                            turn_id=turn_id,
                            event={
                                "type": "turn_start",
                                "source": "agentscope",
                                "run_id": agentscope_session_id,
                                "timestamp": timestamp,
                                "payload": {"status": "running", "started_at": timestamp},
                            },
                            created_at=timestamp,
                            origin_key=turn_start_origin,
                        )
                    )
            if turn_id is not None:
                connection.execute(
                    """
                    UPDATE agentscope_sessions
                    SET active_turn_id = ?, updated_at = ?
                    WHERE web_session_id = ? AND agentscope_session_id = ?
                    """,
                    (turn_id, timestamp, web_session_id, agentscope_session_id),
                )

            if turn_id is not None and raw_event_type == "REPLY_START":
                pending = connection.execute(
                    """
                    SELECT id FROM agentscope_turn_replies
                    WHERE turn_id = ? AND agentscope_session_id = ? AND status = 'pending'
                    ORDER BY updated_at DESC, rowid DESC LIMIT 1
                    """,
                    (turn_id, agentscope_session_id),
                ).fetchone()
                if pending is not None:
                    connection.execute(
                        """
                        UPDATE agentscope_turn_replies
                        SET reply_id = COALESCE(?, reply_id), status = 'running', updated_at = ?
                        WHERE id = ?
                        """,
                        (reply_id, timestamp, pending["id"]),
                    )
                    connection.execute(
                        """
                        UPDATE agentscope_turn_replies
                        SET status = 'interrupted', updated_at = ?
                        WHERE turn_id = ? AND agentscope_session_id = ?
                          AND status = 'pending' AND id <> ?
                        """,
                        (timestamp, turn_id, agentscope_session_id, pending["id"]),
                    )
                elif reply_id is not None:
                    connection.execute(
                        """
                        INSERT INTO agentscope_turn_replies (
                            id, turn_id, agentscope_session_id, agent_id, reply_id,
                            source, status, summary_text, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, 'event', 'running', NULL, ?, ?)
                        ON CONFLICT(agentscope_session_id, reply_id)
                        WHERE reply_id IS NOT NULL DO UPDATE SET
                            status = 'running', updated_at = excluded.updated_at
                        """,
                        (
                            f"reply_run_{uuid4().hex}",
                            turn_id,
                            agentscope_session_id,
                            mapping["agent_id"],
                            reply_id,
                            timestamp,
                            timestamp,
                        ),
                    )

            reply_summary: str | None = None
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
                safe_payload = dict(payload) if isinstance(payload, dict) else {}
                event_type = str(event.get("type", ""))
                event_reply_id = _optional_text(safe_payload.get("reply_id")) or reply_id
                if event_type in {"answer_delta", "answer_reset"} and event_reply_id:
                    safe_payload["reply_id"] = event_reply_id
                if event_type == "answer_delta":
                    raw_delta = safe_payload.get("delta")
                    if not isinstance(raw_delta, str) or not raw_delta:
                        continue
                    if raw_delta.strip():
                        safe_delta = sanitize_public_reply(raw_delta)
                        if not safe_delta:
                            continue
                        leading = raw_delta[: len(raw_delta) - len(raw_delta.lstrip())]
                        trailing = raw_delta[len(raw_delta.rstrip()) :]
                        safe_payload["delta"] = f"{leading}{safe_delta}{trailing}"
                if turn_id is not None and event_type in {"reply_summary", "final"}:
                    reply_summary = sanitize_public_reply(safe_payload.get("text")) or None
                    continue
                if self._duplicate_terminal_tool_event(
                    connection,
                    session_id=web_session_id,
                    run_id=_optional_text(event.get("run_id")),
                    event_type=event_type,
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

                public_event = {**event, "payload": safe_payload}
                public_event["turn_id"] = turn_id
                record = self._insert_timeline_event(
                    connection,
                    session_id=web_session_id,
                    turn_id=turn_id,
                    event=public_event,
                    created_at=timestamp,
                    origin_key=origin_key,
                )
                inserted.append(record)
                if turn_id is None:
                    final_text = _final_event_text(event)
                    if final_text is not None:
                        connection.execute(
                            """
                            INSERT INTO messages (id, session_id, turn_id, role, content, created_at)
                            VALUES (?, ?, NULL, 'assistant', ?, ?)
                            """,
                            (f"message_{uuid4().hex}", web_session_id, final_text, timestamp),
                        )

                if turn_id is not None and event_type in {
                    "tool_start",
                    "tool_background",
                    "tool_end",
                }:
                    call_id = _optional_text(safe_payload.get("call_id"))
                    tool = _optional_text(safe_payload.get("tool")) or "unknown_tool"
                    if call_id is not None:
                        status = (
                            "running"
                            if event_type == "tool_start"
                            else "background"
                            if event_type == "tool_background"
                            else _terminal_tool_status(safe_payload.get("status"))
                        )
                        connection.execute(
                            """
                            INSERT INTO agentscope_turn_tools (
                                turn_id, agentscope_session_id, call_id, tool, status,
                                started_at, finished_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(turn_id, agentscope_session_id, call_id) DO UPDATE SET
                                tool = excluded.tool,
                                status = CASE
                                    WHEN agentscope_turn_tools.status IN (
                                        'completed', 'failed', 'interrupted'
                                    ) THEN agentscope_turn_tools.status
                                    ELSE excluded.status
                                END,
                                finished_at = COALESCE(
                                    agentscope_turn_tools.finished_at,
                                    excluded.finished_at
                                ),
                                updated_at = excluded.updated_at
                            """,
                            (
                                turn_id,
                                agentscope_session_id,
                                call_id,
                                tool,
                                status,
                                timestamp,
                                timestamp if status in {"completed", "failed", "interrupted"} else None,
                                timestamp,
                            ),
                        )

                if turn_id is not None and event_type == "human_decision_required":
                    connection.execute(
                        "UPDATE web_turns SET status = 'waiting' WHERE id = ?",
                        (turn_id,),
                    )
                    connection.execute(
                        """
                        UPDATE agentscope_turn_replies
                        SET status = 'waiting', updated_at = ?
                        WHERE turn_id = ? AND agentscope_session_id = ?
                          AND status = 'running'
                        """,
                        (timestamp, turn_id, agentscope_session_id),
                    )
                    state_origin = f"agentscope:{agentscope_session_id}:{entry_id}:waiting"
                    if connection.execute(
                        "SELECT 1 FROM timeline_events WHERE origin_key = ?",
                        (state_origin,),
                    ).fetchone() is None:
                        inserted.append(
                            self._insert_timeline_event(
                                connection,
                                session_id=web_session_id,
                                turn_id=turn_id,
                                event={
                                    "type": "turn_state",
                                    "source": "agentscope",
                                    "run_id": agentscope_session_id,
                                    "timestamp": timestamp,
                                    "payload": {"status": "waiting"},
                                },
                                created_at=timestamp,
                                origin_key=state_origin,
                            )
                        )

            if turn_id is not None and raw_event_type == "REPLY_END":
                if reply_id is not None:
                    connection.execute(
                        """
                        UPDATE agentscope_turn_replies
                        SET status = 'ended', summary_text = COALESCE(?, summary_text),
                            updated_at = ?
                        WHERE turn_id = ? AND agentscope_session_id = ? AND reply_id = ?
                        """,
                        (reply_summary, timestamp, turn_id, agentscope_session_id, reply_id),
                    )
                else:
                    running = connection.execute(
                        """
                        SELECT id FROM agentscope_turn_replies
                        WHERE turn_id = ? AND agentscope_session_id = ?
                          AND status IN ('running', 'waiting')
                        ORDER BY updated_at DESC, rowid DESC LIMIT 1
                        """,
                        (turn_id, agentscope_session_id),
                    ).fetchone()
                    if running is not None:
                        connection.execute(
                            """
                            UPDATE agentscope_turn_replies
                            SET status = 'ended', summary_text = COALESCE(?, summary_text),
                                updated_at = ? WHERE id = ?
                            """,
                            (reply_summary, timestamp, running["id"]),
                        )

                blockers = int(
                    connection.execute(
                        """
                        SELECT
                            (SELECT COUNT(*) FROM agentscope_turn_replies
                             WHERE turn_id = ? AND status IN ('pending', 'running', 'waiting'))
                            +
                            (SELECT COUNT(*) FROM agentscope_turn_tools
                             WHERE turn_id = ? AND status IN ('running', 'background'))
                        """,
                        (turn_id, turn_id),
                    ).fetchone()[0]
                )
                turn_status_row = connection.execute(
                    "SELECT status FROM web_turns WHERE id = ?",
                    (turn_id,),
                ).fetchone()
                waiting = turn_status_row is not None and turn_status_row["status"] == "waiting"
                summary_origin = f"agentscope:{agentscope_session_id}:{entry_id}:summary"
                streamed_reply = (
                    connection.execute(
                        """
                        SELECT 1 FROM timeline_events
                        WHERE session_id = ? AND turn_id = ? AND type = 'answer_delta'
                          AND json_extract(payload_json, '$.reply_id') = ?
                        LIMIT 1
                        """,
                        (web_session_id, turn_id, reply_id),
                    ).fetchone()
                    if reply_id
                    else None
                )
                safe_reply_failed = not reply_summary and blockers == 0 and not waiting
                if safe_reply_failed:
                    reply_summary = _SAFE_PUBLIC_REPLY_FAILURE
                    if reply_id is not None:
                        connection.execute(
                            """
                            UPDATE agentscope_turn_replies
                            SET summary_text = ?, updated_at = ?
                            WHERE turn_id = ? AND agentscope_session_id = ? AND reply_id = ?
                            """,
                            (
                                reply_summary,
                                timestamp,
                                turn_id,
                                agentscope_session_id,
                                reply_id,
                            ),
                        )
                if reply_summary and connection.execute(
                    "SELECT 1 FROM timeline_events WHERE origin_key = ?",
                    (summary_origin,),
                ).fetchone() is None:
                    if blockers > 0 or waiting:
                        if streamed_reply is not None:
                            reset_origin = (
                                f"agentscope:{agentscope_session_id}:{entry_id}:answer-reset"
                            )
                            inserted.append(
                                self._insert_timeline_event(
                                    connection,
                                    session_id=web_session_id,
                                    turn_id=turn_id,
                                    event={
                                        "type": "answer_reset",
                                        "source": "agentscope",
                                        "run_id": agentscope_session_id,
                                        "timestamp": timestamp,
                                        "payload": {"reply_id": reply_id},
                                    },
                                    created_at=timestamp,
                                    origin_key=reset_origin,
                                )
                            )
                        inserted.append(
                            self._insert_timeline_event(
                                connection,
                                session_id=web_session_id,
                                turn_id=turn_id,
                                event={
                                    "type": "progress_update",
                                    "source": "agentscope",
                                    "run_id": agentscope_session_id,
                                    "timestamp": timestamp,
                                    "payload": {
                                        "text": reply_summary,
                                        **({"reply_id": reply_id} if reply_id else {}),
                                    },
                                },
                                created_at=timestamp,
                                origin_key=summary_origin,
                            )
                        )
                    else:
                        terminal_status = "failed" if safe_reply_failed else "completed"
                        if streamed_reply is not None and safe_reply_failed:
                            reset_origin = (
                                f"agentscope:{agentscope_session_id}:{entry_id}:answer-reset"
                            )
                            inserted.append(
                                self._insert_timeline_event(
                                    connection,
                                    session_id=web_session_id,
                                    turn_id=turn_id,
                                    event={
                                        "type": "answer_reset",
                                        "source": "agentscope",
                                        "run_id": agentscope_session_id,
                                        "timestamp": timestamp,
                                        "payload": {"reply_id": reply_id},
                                    },
                                    created_at=timestamp,
                                    origin_key=reset_origin,
                                )
                            )
                        final_message_id = f"message_{uuid4().hex}"
                        connection.execute(
                            """
                            INSERT INTO messages (
                                id, session_id, turn_id, role, content, created_at
                            ) VALUES (?, ?, ?, 'assistant', ?, ?)
                            """,
                            (
                                final_message_id,
                                web_session_id,
                                turn_id,
                                reply_summary,
                                timestamp,
                            ),
                        )
                        inserted.append(
                            self._insert_timeline_event(
                                connection,
                                session_id=web_session_id,
                                turn_id=turn_id,
                                event={
                                    "type": "final",
                                    "source": "agentscope",
                                    "run_id": agentscope_session_id,
                                    "timestamp": timestamp,
                                    "payload": {
                                        "text": reply_summary,
                                        "message_id": final_message_id,
                                        **({"reply_id": reply_id} if reply_id else {}),
                                    },
                                },
                                created_at=timestamp,
                                origin_key=summary_origin,
                            )
                        )
                        connection.execute(
                            """
                            UPDATE web_turns
                            SET status = ?, finished_at = ?, final_message_id = ?
                            WHERE id = ?
                            """,
                            (terminal_status, timestamp, final_message_id, turn_id),
                        )
                        connection.execute(
                            "UPDATE agentscope_sessions SET active_turn_id = NULL WHERE active_turn_id = ?",
                            (turn_id,),
                        )
                        inserted.append(
                            self._insert_timeline_event(
                                connection,
                                session_id=web_session_id,
                                turn_id=turn_id,
                                event={
                                    "type": "turn_state",
                                    "source": "agentscope",
                                    "run_id": agentscope_session_id,
                                    "timestamp": timestamp,
                                    "payload": {
                                        "status": terminal_status,
                                        "finished_at": timestamp,
                                    },
                                },
                                created_at=timestamp,
                                origin_key=(
                                    f"agentscope:{agentscope_session_id}:{entry_id}:"
                                    f"{terminal_status}"
                                ),
                            )
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
                SELECT session_id, turn_id, run_id, type, payload_json
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
                    turn_id=_optional_text(row["turn_id"]),
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
            connection.execute("BEGIN IMMEDIATE")
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
                    id, session_id, turn_id, seq, origin_key, type, source, run_id,
                    parent_run_id, timestamp, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, 'tool_end', 'agentscope', ?, NULL, ?, ?, ?)
                """,
                (
                    record_id,
                    background.web_session_id,
                    background.turn_id,
                    next_seq,
                    origin_key,
                    background.agentscope_session_id,
                    timestamp,
                    json.dumps(payload, ensure_ascii=False),
                    timestamp,
                ),
            )
            if background.turn_id is not None:
                connection.execute(
                    """
                    UPDATE agentscope_turn_tools
                    SET status = ?, finished_at = ?, updated_at = ?
                    WHERE turn_id = ? AND agentscope_session_id = ? AND call_id = ?
                    """,
                    (
                        status,
                        timestamp,
                        timestamp,
                        background.turn_id,
                        background.agentscope_session_id,
                        background.call_id,
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
            turn_id=background.turn_id,
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
            connection.execute("DELETE FROM agentscope_sessions WHERE web_session_id = ?", (session_id,))
            connection.execute(
                "DELETE FROM agentscope_turn_tools WHERE turn_id IN (SELECT id FROM web_turns WHERE web_session_id = ?)",
                (session_id,),
            )
            connection.execute(
                "DELETE FROM agentscope_turn_replies WHERE turn_id IN (SELECT id FROM web_turns WHERE web_session_id = ?)",
                (session_id,),
            )
            connection.execute("UPDATE web_turns SET final_message_id = NULL WHERE web_session_id = ?", (session_id,))
            connection.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            connection.execute("DELETE FROM timeline_events WHERE session_id = ?", (session_id,))
            connection.execute("DELETE FROM web_turns WHERE web_session_id = ?", (session_id,))
            cursor = connection.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            if cursor.rowcount == 0:
                raise KeyError(session_id)

    def save_agentscope_session_mapping(
        self,
        web_session_id: str,
        *,
        agent_id: str,
        agentscope_session_id: str,
        active_turn_id: str | None = None,
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
                    active_turn_id,
                    updated_at
                )
                VALUES (?, ?, ?, NULL, 1, ?, ?)
                ON CONFLICT(web_session_id, agent_id) DO UPDATE SET
                    agent_id = excluded.agent_id,
                    agentscope_session_id = excluded.agentscope_session_id,
                    event_cursor = CASE
                        WHEN agentscope_sessions.agentscope_session_id = excluded.agentscope_session_id
                        THEN agentscope_sessions.event_cursor
                        ELSE NULL
                    END,
                    active = 1,
                    active_turn_id = COALESCE(excluded.active_turn_id, agentscope_sessions.active_turn_id),
                    updated_at = excluded.updated_at
                """,
                (
                    web_session_id,
                    agent_id,
                    agentscope_session_id,
                    active_turn_id,
                    timestamp,
                ),
            )

    def bind_agentscope_session_to_turn(
        self,
        *,
        web_session_id: str,
        agentscope_session_id: str,
        turn_id: str,
    ) -> None:
        timestamp = _now()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE agentscope_sessions
                SET active_turn_id = ?, updated_at = ?
                WHERE web_session_id = ? AND agentscope_session_id = ?
                """,
                (turn_id, timestamp, web_session_id, agentscope_session_id),
            )
            if cursor.rowcount == 0:
                raise KeyError((web_session_id, agentscope_session_id))

    def register_pending_reply(
        self,
        *,
        turn_id: str,
        agentscope_session_id: str,
        agent_id: str,
        source: str,
    ) -> str:
        timestamp = _now()
        lease_id = f"reply_run_{uuid4().hex}"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT id FROM agentscope_turn_replies
                WHERE turn_id = ? AND agentscope_session_id = ? AND status = 'pending'
                ORDER BY updated_at DESC, rowid DESC LIMIT 1
                """,
                (turn_id, agentscope_session_id),
            ).fetchone()
            if existing is not None:
                connection.execute(
                    """
                    UPDATE agentscope_turn_replies
                    SET agent_id = ?, source = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (agent_id, source, timestamp, existing["id"]),
                )
                return str(existing["id"])
            connection.execute(
                """
                INSERT INTO agentscope_turn_replies (
                    id, turn_id, agentscope_session_id, agent_id, reply_id, source,
                    status, summary_text, created_at, updated_at
                ) VALUES (?, ?, ?, ?, NULL, ?, 'pending', NULL, ?, ?)
                """,
                (
                    lease_id,
                    turn_id,
                    agentscope_session_id,
                    agent_id,
                    source,
                    timestamp,
                    timestamp,
                ),
            )
        return lease_id

    def discard_pending_reply(self, lease_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM agentscope_turn_replies WHERE id = ? AND status = 'pending'",
                (lease_id,),
            )

    def fail_reply_lease(self, lease_id: str) -> list[TimelineEventRecord]:
        """Settle an asynchronously failed run and fail its turn when nothing can continue it."""
        timestamp = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            reply = connection.execute(
                """
                SELECT replies.turn_id, turns.web_session_id, turns.status
                FROM agentscope_turn_replies AS replies
                JOIN web_turns AS turns ON turns.id = replies.turn_id
                WHERE replies.id = ?
                """,
                (lease_id,),
            ).fetchone()
            if reply is None:
                return []
            connection.execute(
                """
                UPDATE agentscope_turn_replies
                SET status = 'failed', updated_at = ?
                WHERE id = ? AND status IN ('pending', 'running', 'waiting')
                """,
                (timestamp, lease_id),
            )
            if reply["status"] in {"completed", "failed", "interrupted"}:
                return []
            blockers = int(
                connection.execute(
                    """
                    SELECT
                        (SELECT COUNT(*) FROM agentscope_turn_replies
                         WHERE turn_id = ? AND status IN ('pending', 'running', 'waiting'))
                        +
                        (SELECT COUNT(*) FROM agentscope_turn_tools
                         WHERE turn_id = ? AND status IN ('running', 'background'))
                    """,
                    (reply["turn_id"], reply["turn_id"]),
                ).fetchone()[0]
            )
            if blockers > 0:
                return []
            connection.execute(
                """
                UPDATE web_turns
                SET status = 'failed', finished_at = ?
                WHERE id = ? AND status IN ('running', 'waiting')
                """,
                (timestamp, reply["turn_id"]),
            )
            connection.execute(
                "UPDATE agentscope_sessions SET active_turn_id = NULL WHERE active_turn_id = ?",
                (reply["turn_id"],),
            )
            record = self._insert_timeline_event(
                connection,
                session_id=reply["web_session_id"],
                turn_id=reply["turn_id"],
                event={
                    "type": "turn_state",
                    "source": "agentscope",
                    "timestamp": timestamp,
                    "payload": {"status": "failed", "finished_at": timestamp},
                },
                created_at=timestamp,
                origin_key=f"reply-failed:{lease_id}",
            )
            connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (timestamp, reply["web_session_id"]),
            )
        return [record]

    def resume_active_turn(self, web_session_id: str) -> list[TimelineEventRecord]:
        timestamp = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT id FROM web_turns
                WHERE web_session_id = ? AND status = 'waiting'
                ORDER BY started_at DESC LIMIT 1
                """,
                (web_session_id,),
            ).fetchone()
            if row is None:
                return []
            turn_id = row["id"]
            connection.execute(
                "UPDATE web_turns SET status = 'running' WHERE id = ?",
                (turn_id,),
            )
            connection.execute(
                """
                UPDATE agentscope_turn_replies
                SET status = 'running', updated_at = ?
                WHERE turn_id = ? AND status = 'waiting'
                """,
                (timestamp, turn_id),
            )
            record = self._insert_timeline_event(
                connection,
                session_id=web_session_id,
                turn_id=turn_id,
                event={
                    "type": "turn_state",
                    "timestamp": timestamp,
                    "payload": {"status": "running"},
                },
                created_at=timestamp,
                origin_key=f"turn-resume:{turn_id}:{timestamp}",
            )
        return [record]

    def restore_waiting_turn(self, web_session_id: str) -> None:
        timestamp = _now()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id FROM web_turns
                WHERE web_session_id = ? AND status = 'running'
                ORDER BY started_at DESC LIMIT 1
                """,
                (web_session_id,),
            ).fetchone()
            if row is None:
                return
            connection.execute(
                "UPDATE web_turns SET status = 'waiting' WHERE id = ?",
                (row["id"],),
            )
            connection.execute(
                """
                UPDATE agentscope_turn_replies
                SET status = 'waiting', updated_at = ?
                WHERE turn_id = ? AND status = 'running'
                """,
                (timestamp, row["id"]),
            )

    def interrupt_active_turn(self, web_session_id: str) -> list[TimelineEventRecord]:
        timestamp = _now()
        records: list[TimelineEventRecord] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT id FROM web_turns
                WHERE web_session_id = ? AND status IN ('running', 'waiting')
                ORDER BY started_at DESC LIMIT 1
                """,
                (web_session_id,),
            ).fetchone()
            if row is None:
                return []
            turn_id = row["id"]
            tools = connection.execute(
                """
                SELECT agentscope_session_id, call_id, tool
                FROM agentscope_turn_tools
                WHERE turn_id = ? AND status IN ('running', 'background')
                ORDER BY started_at ASC, rowid ASC
                """,
                (turn_id,),
            ).fetchall()
            for tool in tools:
                connection.execute(
                    """
                    UPDATE agentscope_turn_tools
                    SET status = 'interrupted', finished_at = ?, updated_at = ?
                    WHERE turn_id = ? AND agentscope_session_id = ? AND call_id = ?
                    """,
                    (
                        timestamp,
                        timestamp,
                        turn_id,
                        tool["agentscope_session_id"],
                        tool["call_id"],
                    ),
                )
                records.append(
                    self._insert_timeline_event(
                        connection,
                        session_id=web_session_id,
                        turn_id=turn_id,
                        event={
                            "type": "tool_end",
                            "source": "agentscope",
                            "run_id": tool["agentscope_session_id"],
                            "timestamp": timestamp,
                            "payload": {
                                "tool": tool["tool"],
                                "call_id": tool["call_id"],
                                "status": "interrupted",
                            },
                        },
                        created_at=timestamp,
                        origin_key=f"turn-interrupt:{turn_id}:tool:{tool['agentscope_session_id']}:{tool['call_id']}",
                    )
                )
            connection.execute(
                """
                UPDATE agentscope_turn_replies
                SET status = 'interrupted', updated_at = ?
                WHERE turn_id = ? AND status IN ('pending', 'running', 'waiting')
                """,
                (timestamp, turn_id),
            )
            connection.execute(
                """
                UPDATE web_turns
                SET status = 'interrupted', finished_at = ?
                WHERE id = ?
                """,
                (timestamp, turn_id),
            )
            connection.execute(
                "UPDATE agentscope_sessions SET active_turn_id = NULL WHERE active_turn_id = ?",
                (turn_id,),
            )
            records.append(
                self._insert_timeline_event(
                    connection,
                    session_id=web_session_id,
                    turn_id=turn_id,
                    event={
                        "type": "turn_state",
                        "timestamp": timestamp,
                        "payload": {"status": "interrupted", "finished_at": timestamp},
                    },
                    created_at=timestamp,
                    origin_key=f"turn-interrupt:{turn_id}:state",
                )
            )
        return records

    def complete_turn(self, turn_id: str, text: str) -> list[TimelineEventRecord]:
        timestamp = _now()
        safe_text = sanitize_public_reply(text)
        terminal_status = "completed" if safe_text else "failed"
        if not safe_text:
            safe_text = _SAFE_PUBLIC_REPLY_FAILURE
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            turn = connection.execute(
                "SELECT web_session_id, status FROM web_turns WHERE id = ?",
                (turn_id,),
            ).fetchone()
            if turn is None:
                raise KeyError(turn_id)
            if turn["status"] in {"completed", "failed", "interrupted"}:
                return []
            message_id = f"message_{uuid4().hex}"
            connection.execute(
                """
                INSERT INTO messages (id, session_id, turn_id, role, content, created_at)
                VALUES (?, ?, ?, 'assistant', ?, ?)
                """,
                (message_id, turn["web_session_id"], turn_id, safe_text, timestamp),
            )
            final = self._insert_timeline_event(
                connection,
                session_id=turn["web_session_id"],
                turn_id=turn_id,
                event={
                    "type": "final",
                    "timestamp": timestamp,
                    "payload": {"text": safe_text, "message_id": message_id},
                },
                created_at=timestamp,
                origin_key=f"controller:{turn_id}:final",
            )
            connection.execute(
                """
                UPDATE web_turns
                SET status = ?, finished_at = ?, final_message_id = ?
                WHERE id = ?
                """,
                (terminal_status, timestamp, message_id, turn_id),
            )
            connection.execute(
                "UPDATE agentscope_sessions SET active_turn_id = NULL WHERE active_turn_id = ?",
                (turn_id,),
            )
            connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (timestamp, turn["web_session_id"]),
            )
            state = self._insert_timeline_event(
                connection,
                session_id=turn["web_session_id"],
                turn_id=turn_id,
                event={
                    "type": "turn_state",
                    "timestamp": timestamp,
                    "payload": {"status": terminal_status, "finished_at": timestamp},
                },
                created_at=timestamp,
                origin_key=f"controller:{turn_id}:{terminal_status}",
            )
        return [final, state]

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
                SELECT web_session_id, agent_id, agentscope_session_id, event_cursor, active_turn_id
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
                SELECT web_session_id, agent_id, agentscope_session_id, event_cursor, active_turn_id
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
                SELECT web_session_id, agent_id, agentscope_session_id, event_cursor, active_turn_id
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
                SELECT web_session_id, agent_id, agentscope_session_id, event_cursor, active_turn_id
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
            turn_id=row["turn_id"] if "turn_id" in row.keys() else None,
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
            turn_id=row["turn_id"] if "turn_id" in row.keys() else None,
        )

    @staticmethod
    def _turn_from_row(row: sqlite3.Row) -> TurnRecord:
        return TurnRecord(
            id=row["id"],
            web_session_id=row["web_session_id"],
            origin=row["origin"],
            status=row["status"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            final_message_id=row["final_message_id"],
        )

    @staticmethod
    def _agentscope_mapping_from_row(row: sqlite3.Row) -> AgentScopeSessionMapping:
        return AgentScopeSessionMapping(
            web_session_id=row["web_session_id"],
            agent_id=row["agent_id"],
            agentscope_session_id=row["agentscope_session_id"],
            event_cursor=row["event_cursor"],
            active_turn_id=row["active_turn_id"],
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


def _terminal_tool_status(value: object) -> str:
    status = _optional_text(value)
    return status if status in {"completed", "failed", "interrupted"} else "failed"

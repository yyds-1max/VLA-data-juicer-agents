from __future__ import annotations

import base64
import binascii
import json
import hashlib
import re
import sqlite3
import time
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
    "session_run_admissions",
    "session_turn_admissions",
    "session_deletions",
    "session_stop_requests",
    "tool_execution_provenance",
    "session_execution_boundaries",
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


@dataclass(frozen=True)
class StopRequest:
    session_id: str
    generation: int
    request_id: str
    status: Literal["pending", "complete"]


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
            CREATE TABLE IF NOT EXISTS session_execution_boundaries (
                session_id TEXT PRIMARY KEY,
                generation INTEGER NOT NULL DEFAULT 0,
                stopped_generation INTEGER,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (session_id) REFERENCES sessions(id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS session_stop_requests (
                session_id TEXT NOT NULL,
                generation INTEGER NOT NULL,
                request_id TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL CHECK (status IN ('pending', 'complete')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (session_id, generation),
                FOREIGN KEY (session_id) REFERENCES sessions(id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS session_deletions (
                session_id TEXT PRIMARY KEY,
                started_at TEXT NOT NULL,
                FOREIGN KEY (session_id) REFERENCES sessions(id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS session_run_admissions (
                session_id TEXT NOT NULL,
                ticket_id TEXT NOT NULL UNIQUE,
                runtime_id TEXT NOT NULL,
                started_at TEXT NOT NULL,
                expires_at REAL NOT NULL,
                PRIMARY KEY (session_id, ticket_id),
                FOREIGN KEY (session_id) REFERENCES sessions(id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS session_turn_admissions (
                session_id TEXT NOT NULL,
                message_id TEXT NOT NULL UNIQUE,
                content TEXT NOT NULL,
                runtime_id TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending', 'admitted', 'terminal')),
                terminal_status TEXT NULL,
                turn_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at REAL NOT NULL,
                PRIMARY KEY (session_id, message_id),
                FOREIGN KEY (session_id) REFERENCES sessions(id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS tool_execution_provenance (
                session_id TEXT NOT NULL,
                tool_call_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                generation INTEGER NOT NULL,
                delivery_suppressed INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (session_id, tool_call_id),
                FOREIGN KEY (session_id) REFERENCES sessions(id)
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

    def create_session(
        self,
        title: str,
        *,
        creation_id: str | None = None,
    ) -> SessionRecord:
        timestamp = _now()
        session_id = (
            "session_"
            + hashlib.sha256(creation_id.encode("utf-8")).hexdigest()[:32]
            if creation_id is not None
            else f"session_{uuid4().hex}"
        )
        record = SessionRecord(
            id=session_id,
            title=title,
            created_at=timestamp,
            updated_at=timestamp,
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT id, title, created_at, updated_at
                FROM sessions WHERE id = ?
                """,
                (session_id,),
            ).fetchone()
            if existing is not None:
                persisted = self._session_from_row(existing)
                if persisted.title != title:
                    raise sqlite3.IntegrityError(
                        "creation_id was submitted with a different first message"
                    )
                return persisted
            connection.execute(
                """
                INSERT INTO sessions (id, title, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (record.id, record.title, record.created_at, record.updated_at),
            )
        return record

    def list_sessions(self, limit: int = 20) -> list[SessionRecord]:
        sessions, _next_cursor = self.list_sessions_page(limit=limit)
        return sessions

    def list_sessions_page(
        self,
        *,
        limit: int = 20,
        cursor: str | None = None,
    ) -> tuple[list[SessionRecord], str | None]:
        if limit < 1 or limit > 100:
            raise ValueError("session page limit must be between 1 and 100")
        boundary = self._decode_session_cursor(cursor) if cursor is not None else None
        with self._connect() as connection:
            if boundary is None:
                rows = connection.execute(
                    """
                    SELECT id, title, created_at, updated_at, rowid
                    FROM sessions
                    ORDER BY updated_at DESC, rowid DESC
                    LIMIT ?
                    """,
                    (limit + 1,),
                ).fetchall()
            else:
                updated_at, row_id = boundary
                rows = connection.execute(
                    """
                    SELECT id, title, created_at, updated_at, rowid
                    FROM sessions
                    WHERE updated_at < ? OR (updated_at = ? AND rowid < ?)
                    ORDER BY updated_at DESC, rowid DESC
                    LIMIT ?
                    """,
                    (updated_at, updated_at, row_id, limit + 1),
                ).fetchall()
        visible = rows[:limit]
        next_cursor = (
            self._encode_session_cursor(str(visible[-1][3]), int(visible[-1][4]))
            if len(rows) > limit and visible
            else None
        )
        return [self._session_from_row(row) for row in visible], next_cursor

    @staticmethod
    def _encode_session_cursor(updated_at: str, row_id: int) -> str:
        payload = json.dumps([updated_at, row_id], separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(payload).decode().rstrip("=")

    @staticmethod
    def _decode_session_cursor(cursor: str) -> tuple[str, int]:
        try:
            padding = "=" * (-len(cursor) % 4)
            value = json.loads(base64.urlsafe_b64decode(cursor + padding))
            if (
                not isinstance(value, list)
                or len(value) != 2
                or not isinstance(value[0], str)
                or not isinstance(value[1], int)
                or isinstance(value[1], bool)
                or value[1] < 1
                or value[1] > 2**63 - 1
            ):
                raise ValueError
            datetime.fromisoformat(value[0])
            return value[0], value[1]
        except (ValueError, TypeError, json.JSONDecodeError, binascii.Error) as exc:
            raise ValueError("invalid session cursor") from exc

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

    def append_message(
        self,
        session_id: str,
        *,
        role: MessageRole,
        content: str,
        message_id: str | None = None,
    ) -> ChatMessageRecord:
        timestamp = _now()
        record = ChatMessageRecord(
            id=message_id or f"message_{uuid4().hex}",
            session_id=session_id,
            role=role,
            content=content,
            created_at=timestamp,
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_session(connection, session_id)
            deleting = connection.execute(
                "SELECT 1 FROM session_deletions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if deleting is not None:
                raise RuntimeError("session deletion is pending")
            connection.execute(
                """
                INSERT INTO messages (id, session_id, role, content, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (record.id, record.session_id, record.role, record.content, record.created_at),
            )
            self._touch_session(connection, session_id, timestamp)
        return record

    def claim_user_message(
        self,
        session_id: str,
        message_id: str,
        content: str,
        *,
        runtime_id: str,
        turn_id: str,
        ttl_seconds: float,
        now: float | None = None,
    ) -> Literal["claimed", "pending", "admitted", "orphaned", "busy"]:
        if ttl_seconds <= 0:
            raise ValueError("turn admission lease ttl_seconds must be positive")
        timestamp = _now()
        now_seconds = time.time() if now is None else now
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_session(connection, session_id)
            deleting = connection.execute(
                "SELECT 1 FROM session_deletions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if deleting is not None:
                raise RuntimeError("session deletion is pending")
            existing = connection.execute(
                """
                SELECT session_id, content, status, expires_at
                FROM session_turn_admissions
                WHERE message_id = ?
                """,
                (message_id,),
            ).fetchone()
            if existing is not None:
                if existing[0] != session_id or existing[1] != content:
                    raise sqlite3.IntegrityError(
                        "message_id was submitted with different content"
                    )
                if existing[2] == "terminal":
                    return "admitted"
                if existing[2] == "admitted":
                    return (
                        "orphaned"
                        if float(existing[3]) <= now_seconds
                        else "admitted"
                    )
                if float(existing[3]) > now_seconds:
                    return "pending"
                connection.execute(
                    "DELETE FROM session_turn_admissions WHERE message_id = ?",
                    (message_id,),
                )
            connection.execute(
                """
                DELETE FROM session_turn_admissions
                WHERE status = 'pending' AND expires_at <= ?
                """,
                (now_seconds,),
            )
            busy = connection.execute(
                """
                SELECT 1 FROM session_turn_admissions
                WHERE session_id = ? AND status IN ('pending', 'admitted')
                UNION ALL
                SELECT 1 FROM public_tool_runs
                WHERE session_id = ? AND status = 'running'
                LIMIT 1
                """,
                (session_id, session_id),
            ).fetchone()
            if busy is not None:
                return "busy"
            connection.execute(
                """
                INSERT INTO session_turn_admissions (
                    session_id, message_id, content, runtime_id, status,
                    turn_id, created_at, expires_at
                ) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)
                """,
                (
                    session_id,
                    message_id,
                    content,
                    runtime_id,
                    turn_id,
                    timestamp,
                    now_seconds + ttl_seconds,
                ),
            )
        return "claimed"

    def user_message_turn_id(self, session_id: str, message_id: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT turn_id FROM session_turn_admissions
                WHERE session_id = ? AND message_id = ?
                """,
                (session_id, message_id),
            ).fetchone()
        return str(row[0]) if row is not None else None

    def user_message_turn_status(self, session_id: str, message_id: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT status FROM session_turn_admissions
                WHERE session_id = ? AND message_id = ?
                """,
                (session_id, message_id),
            ).fetchone()
        return str(row[0]) if row is not None else None

    def admitted_user_message_turns(
        self,
        session_id: str,
    ) -> list[tuple[str, str, str]]:
        """Return exact turns whose execution fence is still authoritative."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT message_id, turn_id, runtime_id
                FROM session_turn_admissions
                WHERE session_id = ? AND status = 'admitted'
                ORDER BY created_at, message_id
                """,
                (session_id,),
            ).fetchall()
        return [
            (str(message_id), str(turn_id), str(runtime_id))
            for message_id, turn_id, runtime_id in rows
        ]

    def renew_user_message(
        self,
        session_id: str,
        message_id: str,
        *,
        runtime_id: str,
        ttl_seconds: float,
        now: float | None = None,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("turn admission lease ttl_seconds must be positive")
        now_seconds = time.time() if now is None else now
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE session_turn_admissions
                SET expires_at = ?
                WHERE session_id = ? AND message_id = ? AND runtime_id = ?
                  AND status IN ('pending', 'admitted')
                  AND expires_at > ?
                """,
                (
                    now_seconds + ttl_seconds,
                    session_id,
                    message_id,
                    runtime_id,
                    now_seconds,
                ),
            )
        if cursor.rowcount == 0:
            raise RuntimeError("user message admission lease was lost")

    def commit_user_message(
        self,
        session_id: str,
        message_id: str,
        content: str,
        *,
        runtime_id: str,
        ttl_seconds: float,
    ) -> ChatMessageRecord:
        if ttl_seconds <= 0:
            raise ValueError("turn admission lease ttl_seconds must be positive")
        timestamp = _now()
        record = ChatMessageRecord(
            id=message_id,
            session_id=session_id,
            role="user",
            content=content,
            created_at=timestamp,
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            claim = connection.execute(
                """
                SELECT 1 FROM session_turn_admissions
                WHERE session_id = ? AND message_id = ? AND content = ?
                  AND runtime_id = ? AND status = 'pending' AND expires_at > ?
                """,
                (session_id, message_id, content, runtime_id, time.time()),
            ).fetchone()
            if claim is None:
                raise RuntimeError("user message admission is missing")
            connection.execute(
                """
                INSERT INTO messages (id, session_id, role, content, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (record.id, record.session_id, record.role, record.content, record.created_at),
            )
            connection.execute(
                """
                UPDATE session_turn_admissions
                SET status = 'admitted', expires_at = ?
                WHERE session_id = ? AND message_id = ?
                """,
                (time.time() + ttl_seconds, session_id, message_id),
            )
            self._touch_session(connection, session_id, timestamp)
        return record

    def release_user_message(
        self,
        session_id: str,
        message_id: str,
        content: str,
        *,
        runtime_id: str,
    ) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                DELETE FROM session_turn_admissions
                WHERE session_id = ? AND message_id = ? AND content = ?
                  AND runtime_id = ? AND status = 'pending'
                """,
                (session_id, message_id, content, runtime_id),
            )
        return cursor.rowcount > 0

    def finish_user_message_turn_with_event(
        self,
        session_id: str,
        message_id: str,
        *,
        turn_id: str,
        terminal_status: Literal["success", "failure", "stopped"],
        expired_before: float | None = None,
    ) -> PublicEventRecord:
        event = {
            "id": f"event_{uuid4().hex}",
            "created_at": _now(),
            "type": "custom",
            "name": "datapilot_run_terminal",
            "value": {
                "turn_id": turn_id,
                "message_id": message_id,
                "status": terminal_status,
            },
        }
        event_json = json.dumps(event, ensure_ascii=False)
        dedupe_key = hashlib.sha256(
            f"run-terminal:{session_id}:{turn_id}".encode("utf-8")
        ).hexdigest()
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
            expiry_clause = " AND expires_at <= ?" if expired_before is not None else ""
            parameters: tuple[Any, ...] = (
                (terminal_status, session_id, message_id, expired_before)
                if expired_before is not None
                else (terminal_status, session_id, message_id)
            )
            updated = connection.execute(
                f"""
                UPDATE session_turn_admissions
                SET status = 'terminal', terminal_status = ?
                WHERE session_id = ? AND message_id = ?
                  AND status = 'admitted'
                  {expiry_clause}
                """,
                parameters,
            )
            if updated.rowcount == 0:
                raise RuntimeError("admitted user turn is unavailable")
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
                ) VALUES (?, ?, ?, ?, ?, ?)
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

    def recover_expired_user_message_turns(
        self,
        session_id: str,
        *,
        now: float | None = None,
    ) -> list[PublicEventRecord]:
        """Do not infer execution quiescence from a missed heartbeat.

        The row remains admitted (and therefore keeps the per-session fence)
        until the owning cancellation lease reaches real agent/tool/worker
        quiescence.  A user-initiated Stop performs the distributed owner/ACK
        barrier when automatic ownership can no longer be proven.
        """
        del now
        del session_id
        return []

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
            connection.execute(
                """
                INSERT INTO tool_execution_provenance (
                    session_id, tool_call_id, tool_name, generation,
                    delivery_suppressed
                )
                VALUES (
                    ?, ?, ?,
                    COALESCE((
                        SELECT generation FROM session_execution_boundaries
                        WHERE session_id = ?
                    ), 0),
                    0
                )
                ON CONFLICT(session_id, tool_call_id) DO NOTHING
                """,
                (session_id, tool_call_id, tool_name, session_id),
            )
            row = self._select_tool_run(connection, session_id, tool_call_id)
        assert row is not None
        return self._tool_run_from_row(row)

    def tool_run_status(self, session_id: str, tool_call_id: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT status FROM public_tool_runs
                WHERE session_id = ? AND tool_call_id = ?
                """,
                (session_id, tool_call_id),
            ).fetchone()
        return str(row[0]) if row is not None else None

    def tool_delivery_is_suppressed(
        self,
        session_id: str,
        tool_name: str,
        tool_call_id: str,
    ) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT delivery_suppressed
                FROM tool_execution_provenance
                WHERE session_id = ? AND tool_call_id = ? AND tool_name = ?
                """,
                (session_id, tool_call_id, tool_name),
            ).fetchone()
        return bool(row is not None and row[0])

    def tool_delivery_sublabel_is_suppressed(
        self,
        session_id: str,
        sublabel: str,
    ) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT delivery_suppressed
                FROM tool_execution_provenance
                WHERE session_id = ?
                  AND tool_name || ' · ' || tool_call_id = ?
                """,
                (session_id, sublabel),
            ).fetchone()
        return bool(row is not None and row[0])

    def execution_boundary_snapshot(self, session_id: str) -> tuple[int, int | None]:
        with self._connect() as connection:
            self._require_session(connection, session_id)
            deleting = connection.execute(
                "SELECT 1 FROM session_deletions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if deleting is not None:
                raise RuntimeError("session deletion is pending")
            row = connection.execute(
                """
                SELECT generation, stopped_generation
                FROM session_execution_boundaries
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
        if row is None:
            return 0, None
        return int(row[0]), None if row[1] is None else int(row[1])

    def begin_execution_generation(
        self,
        session_id: str,
        *,
        expected_boundary: tuple[int, int | None] | None = None,
    ) -> int:
        timestamp = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_session(connection, session_id)
            deleting = connection.execute(
                "SELECT 1 FROM session_deletions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if deleting is not None:
                raise RuntimeError("session deletion is pending")
            boundary = connection.execute(
                """
                SELECT generation, stopped_generation
                FROM session_execution_boundaries
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
            actual_boundary = (
                (0, None)
                if boundary is None
                else (
                    int(boundary[0]),
                    None if boundary[1] is None else int(boundary[1]),
                )
            )
            if expected_boundary is not None and actual_boundary != expected_boundary:
                raise RuntimeError("execution admission was invalidated by stop")
            pending = connection.execute(
                """
                SELECT 1 FROM session_stop_requests
                WHERE session_id = ? AND status = 'pending'
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            if pending is not None:
                raise RuntimeError("stop request is pending")
            connection.execute(
                """
                INSERT INTO session_execution_boundaries (
                    session_id, generation, stopped_generation, updated_at
                ) VALUES (?, 1, NULL, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    generation = generation + 1,
                    stopped_generation = NULL,
                    updated_at = excluded.updated_at
                """,
                (session_id, timestamp),
            )
            row = connection.execute(
                "SELECT generation FROM session_execution_boundaries WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        assert row is not None
        return int(row[0])

    def begin_session_deletion(self, session_id: str) -> None:
        timestamp = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_session(connection, session_id)
            connection.execute(
                """
                INSERT INTO session_deletions (session_id, started_at)
                VALUES (?, ?)
                ON CONFLICT(session_id) DO NOTHING
                """,
                (session_id, timestamp),
            )

    def session_run_admission_is_pending(self, session_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM session_run_admissions WHERE session_id = ? LIMIT 1",
                (session_id,),
            ).fetchone()
        return row is not None

    def claim_session_run_admission(
        self,
        session_id: str,
        *,
        runtime_id: str = "local-runtime",
        ttl_seconds: float = 30.0,
        now: datetime | float | None = None,
    ) -> tuple[str, tuple[int, int | None]]:
        if ttl_seconds <= 0:
            raise ValueError("admission lease ttl_seconds must be positive")
        ticket_id = f"admission_{uuid4().hex}"
        timestamp = _now()
        now_seconds = self._lease_time_seconds(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_session(connection, session_id)
            deleting = connection.execute(
                "SELECT 1 FROM session_deletions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if deleting is not None:
                raise RuntimeError("session deletion is pending")
            boundary = connection.execute(
                """
                SELECT generation, stopped_generation
                FROM session_execution_boundaries
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
            baseline = (
                (0, None)
                if boundary is None
                else (
                    int(boundary[0]),
                    None if boundary[1] is None else int(boundary[1]),
                )
            )
            connection.execute(
                """
                INSERT INTO session_run_admissions (
                    session_id, ticket_id, runtime_id, started_at, expires_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    ticket_id,
                    runtime_id,
                    timestamp,
                    now_seconds + ttl_seconds,
                ),
            )
        return ticket_id, baseline

    def renew_session_run_admission(
        self,
        session_id: str,
        ticket_id: str,
        *,
        ttl_seconds: float,
        runtime_id: str | None = None,
        now: datetime | float | None = None,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("admission lease ttl_seconds must be positive")
        now_seconds = self._lease_time_seconds(now)
        with self._connect() as connection:
            if runtime_id is None:
                cursor = connection.execute(
                    """
                    UPDATE session_run_admissions
                    SET expires_at = ?
                    WHERE session_id = ? AND ticket_id = ?
                    """,
                    (now_seconds + ttl_seconds, session_id, ticket_id),
                )
            else:
                cursor = connection.execute(
                    """
                    UPDATE session_run_admissions
                    SET expires_at = ?
                    WHERE session_id = ? AND ticket_id = ? AND runtime_id = ?
                    """,
                    (
                        now_seconds + ttl_seconds,
                        session_id,
                        ticket_id,
                        runtime_id,
                    ),
                )
            if cursor.rowcount != 1:
                raise RuntimeError("session run admission lease was lost")

    def reap_expired_session_run_admissions(
        self,
        session_id: str,
        *,
        now: datetime | float | None = None,
    ) -> int:
        now_seconds = self._lease_time_seconds(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                DELETE FROM session_run_admissions
                WHERE session_id = ? AND expires_at <= ?
                """,
                (session_id, now_seconds),
            )
        return cursor.rowcount

    @staticmethod
    def _lease_time_seconds(value: datetime | float | None) -> float:
        if value is None:
            return time.time()
        if isinstance(value, datetime):
            return value.timestamp()
        return float(value)

    def release_session_run_admission(
        self,
        session_id: str,
        ticket_id: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                DELETE FROM session_run_admissions
                WHERE session_id = ? AND ticket_id = ?
                """,
                (session_id, ticket_id),
            )

    def session_deletion_is_pending(self, session_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM session_deletions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return row is not None

    def current_execution_generation(self, session_id: str) -> int:
        with self._connect() as connection:
            self._require_session(connection, session_id)
            row = connection.execute(
                """
                SELECT generation FROM session_execution_boundaries
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
        return int(row[0]) if row is not None else 0

    def begin_or_resume_stop_request(self, session_id: str) -> StopRequest:
        timestamp = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_session(connection, session_id)
            connection.execute(
                """
                INSERT INTO session_execution_boundaries (
                    session_id, generation, stopped_generation, updated_at
                ) VALUES (?, 0, NULL, ?)
                ON CONFLICT(session_id) DO NOTHING
                """,
                (session_id, timestamp),
            )
            row = connection.execute(
                """
                SELECT generation FROM session_execution_boundaries
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
            generation = int(row[0]) if row is not None else 0
            existing = connection.execute(
                """
                SELECT request_id, status FROM session_stop_requests
                WHERE session_id = ? AND generation = ?
                """,
                (session_id, generation),
            ).fetchone()
            if existing is None:
                request_id = f"stop_{uuid4().hex}"
                connection.execute(
                    """
                    INSERT INTO session_stop_requests (
                        session_id, generation, request_id, status,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, 'pending', ?, ?)
                    """,
                    (session_id, generation, request_id, timestamp, timestamp),
                )
                status = "pending"
            else:
                request_id = str(existing[0])
                status = str(existing[1])
        return StopRequest(
            session_id=session_id,
            generation=generation,
            request_id=request_id,
            status=status,
        )

    def stop_request_is_pending(self, session_id: str, generation: int) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT status FROM session_stop_requests
                WHERE session_id = ? AND generation = ?
                """,
                (session_id, generation),
            ).fetchone()
        return bool(row is not None and row[0] == "pending")

    def execution_generation_is_stopped(self, session_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT generation, stopped_generation
                FROM session_execution_boundaries
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
        return bool(row is not None and row[1] is not None and row[0] == row[1])

    def execution_generation_is_fenced(self, session_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT boundary.generation, boundary.stopped_generation,
                       stop.status
                FROM session_execution_boundaries AS boundary
                LEFT JOIN session_stop_requests AS stop
                  ON stop.session_id = boundary.session_id
                 AND stop.generation = boundary.generation
                WHERE boundary.session_id = ?
                """,
                (session_id,),
            ).fetchone()
        return bool(
            row is not None
            and (
                (row[1] is not None and row[0] == row[1])
                or row[2] == "pending"
            )
        )

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
            if self._pending_stop_for_current_generation(connection, session_id):
                return None
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

    def finish_tool_run_with_terminal_event(
        self,
        session_id: str,
        tool_call_id: str,
        *,
        status: Literal["success", "failure"],
        terminal_event_factory: Callable[
            [PublicToolRun],
            tuple[str, dict[str, Any]],
        ],
        summary: str = "",
        error_type: str | None = None,
    ) -> tuple[PublicToolRun, PublicEventRecord] | None:
        """Finish one running tool and append its terminal event atomically."""
        if status not in {"success", "failure"}:
            raise ValueError(
                "finish_tool_run_with_terminal_event status must be success or failure"
            )
        finished_at = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_session(connection, session_id)
            if self._pending_stop_for_current_generation(connection, session_id):
                return None
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
            tool_run = self._tool_run_from_row(row)
            dedupe_key, event = terminal_event_factory(tool_run)
            if _SHA256_HEX.fullmatch(dedupe_key) is None:
                raise ValueError(
                    "dedupe_key must be a lowercase SHA-256 hexadecimal digest"
                )

            duplicate = connection.execute(
                """
                SELECT id, session_id, sequence, dedupe_key, event_json, created_at
                FROM public_events
                WHERE session_id = ? AND dedupe_key = ?
                """,
                (session_id, dedupe_key),
            ).fetchone()
            if duplicate is not None:
                record = self._public_event_from_row(duplicate)
            else:
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
            self._touch_session(connection, session_id, finished_at)
        return tool_run, record

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
        *,
        stop_request_id: str | None = None,
    ) -> tuple[list[PublicToolRun], list[PublicEventRecord]]:
        """Stop every running tool and append its terminal event atomically."""
        finished_at = _now()
        stopped: list[PublicToolRun] = []
        records: list[PublicEventRecord] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_session(connection, session_id)
            generation_row = connection.execute(
                """
                SELECT generation FROM session_execution_boundaries
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
            generation = int(generation_row[0]) if generation_row is not None else 0
            if stop_request_id is not None:
                request_row = connection.execute(
                    """
                    SELECT generation, status FROM session_stop_requests
                    WHERE session_id = ? AND request_id = ?
                    """,
                    (session_id, stop_request_id),
                ).fetchone()
                if request_row is None:
                    raise RuntimeError("stop request does not exist")
                if int(request_row[0]) != generation:
                    raise RuntimeError("stop request generation changed")
                if request_row[1] == "complete":
                    return [], []
            connection.execute(
                """
                INSERT INTO tool_execution_provenance (
                    session_id, tool_call_id, tool_name, generation,
                    delivery_suppressed
                )
                SELECT session_id, tool_call_id, tool_name, ?, 0
                FROM public_tool_runs
                WHERE session_id = ?
                ON CONFLICT(session_id, tool_call_id) DO NOTHING
                """,
                (generation, session_id),
            )
            connection.execute(
                """
                UPDATE tool_execution_provenance
                SET delivery_suppressed = 1
                WHERE session_id = ?
                """,
                (session_id,),
            )
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
            connection.execute(
                """
                UPDATE public_tool_runs
                SET status = 'stopped', finished_at = ?
                WHERE session_id = ? AND status = 'running'
                """,
                (finished_at, session_id),
            )
            connection.execute(
                """
                INSERT INTO session_execution_boundaries (
                    session_id, generation, stopped_generation, updated_at
                ) VALUES (?, 0, 0, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    stopped_generation = generation,
                    updated_at = excluded.updated_at
                """,
                (session_id, finished_at),
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
            if stop_request_id is not None:
                connection.execute(
                    """
                    UPDATE session_stop_requests
                    SET status = 'complete', updated_at = ?
                    WHERE session_id = ? AND request_id = ? AND status = 'pending'
                    """,
                    (finished_at, session_id, stop_request_id),
                )
            self._touch_session(connection, session_id, finished_at)
        return stopped, records

    def complete_stop_request_with_terminal_events(
        self,
        session_id: str,
        request_id: str,
        terminal_event_factory: Callable[
            [PublicToolRun],
            tuple[str, dict[str, Any]],
        ],
    ) -> tuple[list[PublicToolRun], list[PublicEventRecord]]:
        return self.stop_open_tool_runs_with_terminal_events(
            session_id,
            terminal_event_factory,
            stop_request_id=request_id,
        )

    @staticmethod
    def _pending_stop_for_current_generation(
        connection: sqlite3.Connection,
        session_id: str,
    ) -> bool:
        row = connection.execute(
            """
            SELECT 1
            FROM session_stop_requests AS stop
            JOIN session_execution_boundaries AS boundary
              ON boundary.session_id = stop.session_id
             AND boundary.generation = stop.generation
            WHERE stop.session_id = ? AND stop.status = 'pending'
            LIMIT 1
            """,
            (session_id,),
        ).fetchone()
        return row is not None

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
                "DELETE FROM tool_execution_provenance WHERE session_id = ?",
                (session_id,),
            )
            connection.execute(
                "DELETE FROM session_stop_requests WHERE session_id = ?",
                (session_id,),
            )
            connection.execute(
                "DELETE FROM session_execution_boundaries WHERE session_id = ?",
                (session_id,),
            )
            connection.execute(
                "DELETE FROM session_deletions WHERE session_id = ?",
                (session_id,),
            )
            connection.execute(
                "DELETE FROM session_run_admissions WHERE session_id = ?",
                (session_id,),
            )
            connection.execute(
                "DELETE FROM session_turn_admissions WHERE session_id = ?",
                (session_id,),
            )
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
        admission_ticket: str | None = None,
        runtime_id: str | None = None,
    ) -> None:
        timestamp = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_session(connection, web_session_id)
            deleting = connection.execute(
                "SELECT 1 FROM session_deletions WHERE session_id = ?",
                (web_session_id,),
            ).fetchone()
            if deleting is not None:
                admitted = None
                if admission_ticket is not None and runtime_id is not None:
                    admitted = connection.execute(
                        """
                        SELECT 1
                        FROM session_run_admissions
                        WHERE session_id = ?
                          AND ticket_id = ?
                          AND runtime_id = ?
                          AND expires_at > ?
                        """,
                        (
                            web_session_id,
                            admission_ticket,
                            runtime_id,
                            time.time(),
                        ),
                    ).fetchone()
                if admitted is None:
                    raise RuntimeError("session deletion is pending")
            connection.execute(
                "UPDATE agentscope_sessions SET active = 0 WHERE web_session_id = ?",
                (web_session_id,),
            )
            connection.execute(
                """
                INSERT INTO agentscope_sessions (
                    web_session_id, agent_id, agentscope_session_id,
                    active, updated_at
                )
                VALUES (?, ?, ?, 1, ?)
                ON CONFLICT(web_session_id, agent_id) DO UPDATE SET
                    agentscope_session_id = excluded.agentscope_session_id,
                    active = 1,
                    updated_at = excluded.updated_at
                """,
                (web_session_id, agent_id, agentscope_session_id, timestamp),
            )

    def list_all_agentscope_session_mappings(
        self,
    ) -> list[AgentScopeSessionMapping]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT web_session_id, agent_id, agentscope_session_id
                FROM agentscope_sessions
                ORDER BY web_session_id, updated_at, rowid
                """
            ).fetchall()
        return [
            AgentScopeSessionMapping(
                web_session_id=str(row["web_session_id"]),
                agent_id=str(row["agent_id"]),
                agentscope_session_id=str(row["agentscope_session_id"]),
            )
            for row in rows
        ]

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
                SELECT web_session_id, agent_id, agentscope_session_id
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
                SELECT web_session_id, agent_id, agentscope_session_id
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
                SELECT web_session_id, agent_id, agentscope_session_id
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
                SELECT web_session_id, agent_id, agentscope_session_id
                FROM agentscope_sessions
                WHERE agentscope_session_id = ?
                """,
                (agentscope_session_id,),
            ).fetchone()
        return self._agentscope_mapping_from_row(row) if row is not None else None

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
        )

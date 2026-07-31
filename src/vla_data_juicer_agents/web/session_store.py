from __future__ import annotations

import json
import logging
import os
import sqlite3
import stat
from datetime import UTC, datetime, timedelta
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from vla_data_juicer_agents.adapters.agentscope.events import sanitize_public_reply
from vla_data_juicer_agents.web.contract_models import (
    AuthorizedFinalCommit,
    ContractConflictError,
    ConversationAgentSession,
    InteractionConsumption,
    InteractionRecord,
    ResourceLease,
    ResponseAuthority,
    RuntimeOutboxItem,
    TaskBinding,
    TaskBindingCreation,
    TaskFocus,
    TurnRun,
)
from vla_data_juicer_agents.web.migrations import (
    apply_pending_migrations,
    prepare_migration_ledger,
)
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
_BACKGROUND_TURN_REPLY = "任务已转入后台继续处理，完成后会自动通知你。"
_OPEN_TASK_STATUS_REPLY = "任务状态仍为处理中，后续状态会继续更新。"
_INTERACTION_CONTINUATION_REPLY = (
    "已收到你的选择，我会按确认结果继续处理。"
    "下一次需要你操作时，DataPilot 会在这里提醒你。"
)
_NAVIGATION_WORKFLOW_TURN_PREFIX = "navigation_workflow:"
_NAVIGATION_WORKFLOW_TURN_FALLBACKS = {
    "initial_annotation_tracking_completed": (
        "Tracking 已完成。我正在继续检查当前数据并执行后处理，"
        "后续状态会自动更新。"
    ),
}
_NAVIGATION_DATASET_SELECTION_CONTEXT = "navigation_dataset_selection_v1"
_MAX_REQUEST_CONTEXT_BYTES = 3_000
_logger = logging.getLogger(__name__)


def _private_session_sqlite_path(path: Path) -> Path:
    candidate = path if path.is_absolute() else Path.cwd() / path
    parent = candidate.parent
    try:
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        metadata = parent.lstat()
        canonical_parent = parent.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError("session database parent is unavailable") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or canonical_parent != parent
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise RuntimeError("session database parent is unsafe")
    return canonical_parent / candidate.name


def _secure_session_sqlite_file(path: Path, *, create: bool) -> None:
    try:
        existing = path.lstat()
    except FileNotFoundError:
        if not create:
            return
    except OSError as exc:
        raise RuntimeError("session database storage is unavailable") from exc
    else:
        if stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode):
            raise RuntimeError(
                "session database storage must be a regular file",
            )

    flags = os.O_RDWR
    if create:
        flags |= os.O_CREAT
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags, 0o600)
        opened = os.fstat(descriptor)
        current = path.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.geteuid()
            or current.st_dev != opened.st_dev
            or current.st_ino != opened.st_ino
        ):
            raise RuntimeError("session database storage is unsafe")
        os.fchmod(descriptor, 0o600)
    except RuntimeError:
        raise
    except OSError as exc:
        raise RuntimeError("session database storage is unsafe") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _secure_session_sqlite_storage(path: Path) -> None:
    _secure_session_sqlite_file(path, create=True)
    for suffix in ("-wal", "-shm"):
        _secure_session_sqlite_file(Path(f"{path}{suffix}"), create=False)


def _open_task_turn_reply(status: str, *, background_tools: int = 0) -> str:
    return {
        "waiting_user": "继续处理前需要你的补充或确认。",
        "pausing": "正在安全停止当前处理，任务状态会继续更新。",
        "paused": "当前处理已停止，任务状态已保留。",
        "cancelling": "正在安全取消任务，任务状态会继续更新。",
        "needs_replan": "当前方案需要调整，任务状态已保留。",
    }.get(
        status,
        _BACKGROUND_TURN_REPLY if background_tools > 0 else _OPEN_TASK_STATUS_REPLY,
    )


def _navigation_workflow_reason(invocation_id: object) -> str | None:
    if not isinstance(invocation_id, str) or not invocation_id.startswith(
        _NAVIGATION_WORKFLOW_TURN_PREFIX
    ):
        return None
    remainder = invocation_id[len(_NAVIGATION_WORKFLOW_TURN_PREFIX) :]
    reason, separator, _idempotency_key = remainder.partition(":")
    if not separator or reason not in _NAVIGATION_WORKFLOW_TURN_FALLBACKS:
        return None
    return reason


def _normalize_turn_request_context(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("request_context must be an object")
    if value.get("kind") != _NAVIGATION_DATASET_SELECTION_CONTEXT:
        raise ValueError("unsupported request_context kind")
    date = value.get("dataset_date")
    if not isinstance(date, str) or len(date) != 8 or not date.isdigit():
        raise ValueError("request_context dataset_date must be YYYYMMDD")
    selection = value.get("selection")
    if not isinstance(selection, dict):
        raise ValueError("request_context selection must be an object")
    selection_kind = selection.get("kind")
    if selection_kind == "all_clips":
        normalized_selection: dict[str, Any] = {"kind": "all_clips"}
    elif selection_kind == "selected_clips":
        clips = selection.get("clips")
        if not isinstance(clips, list) or not clips or len(clips) > 200:
            raise ValueError("selected_clips requires between 1 and 200 clips")
        normalized_clips: list[str] = []
        for clip in clips:
            if not isinstance(clip, str):
                raise ValueError("request_context clips must be strings")
            normalized = clip.strip()
            if (
                not normalized
                or normalized in {".", ".."}
                or "/" in normalized
                or "\\" in normalized
                or "\r" in normalized
                or "\n" in normalized
                or len(normalized) > 200
            ):
                raise ValueError("request_context clips must be safe path components")
            normalized_clips.append(normalized)
        if len(set(normalized_clips)) != len(normalized_clips):
            raise ValueError("request_context clips must be unique")
        normalized_selection = {
            "kind": "selected_clips",
            "clips": normalized_clips,
        }
    else:
        raise ValueError("unsupported request_context selection kind")
    normalized_context = {
        "kind": _NAVIGATION_DATASET_SELECTION_CONTEXT,
        "dataset_date": date,
        "selection": normalized_selection,
    }
    encoded = json.dumps(
        normalized_context,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(encoded.encode("utf-8")) > _MAX_REQUEST_CONTEXT_BYTES:
        raise ValueError("request_context is too large")
    return normalized_context


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
    created: bool = True


class UnsupportedLegacySessionError(RuntimeError):
    """Raised when a pre-contract-v1 session database requires a development reset."""


class WebSessionStore:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = _private_session_sqlite_path(Path(db_path))
        _secure_session_sqlite_storage(self.db_path)
        try:
            self._init_schema()
        finally:
            _secure_session_sqlite_storage(self.db_path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.row_factory = sqlite3.Row
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            prepare_migration_ledger(connection)
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
                    invocation_id TEXT,
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
                CREATE UNIQUE INDEX IF NOT EXISTS idx_web_turns_session_invocation
                ON web_turns (web_session_id, invocation_id)
                WHERE invocation_id IS NOT NULL
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
            apply_pending_migrations(connection, applied_at=_now())
            legacy_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM sessions WHERE contract_version <> 1"
                ).fetchone()[0]
            )
            if legacy_count:
                raise UnsupportedLegacySessionError(
                    "sessions database contains contract v0 sessions; back up and reset "
                    "the development sessions database before starting this v1-only service"
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
        turn_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(web_turns)").fetchall()
        }
        if turn_columns and "invocation_id" not in turn_columns:
            connection.execute("ALTER TABLE web_turns ADD COLUMN invocation_id TEXT")

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

    def create_session(self, title: str, *, contract_version: int = 1) -> SessionRecord:
        if contract_version != 1:
            raise ValueError("contract_version must be 1")
        timestamp = _now()
        record = SessionRecord(
            id=f"session_{uuid4().hex}",
            title=title,
            status="active",
            created_at=timestamp,
            updated_at=timestamp,
            contract_version=contract_version,
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO sessions (
                    id, title, status, created_at, updated_at, contract_version
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    record.id,
                    record.title,
                    record.status,
                    record.created_at,
                    record.updated_at,
                    record.contract_version,
                ),
            )
        return record

    def list_sessions(self, limit: int = 20) -> list[SessionRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, title, status, created_at, updated_at, contract_version
                FROM sessions
                ORDER BY updated_at DESC, rowid DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._session_from_row(row) for row in rows]

    def get_session_contract_version(self, session_id: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT contract_version FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            raise KeyError(session_id)
        return int(row["contract_version"])

    def begin_user_turn(
        self,
        session_id: str,
        message: str,
        *,
        invocation_id: str | None = None,
        request_context: dict[str, Any] | None = None,
    ) -> TurnSubmission:
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
        supplied_context = (
            _normalize_turn_request_context(request_context)
            if request_context is not None
            else None
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            session_row = connection.execute(
                "SELECT contract_version FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if session_row is None:
                raise KeyError(session_id)
            if invocation_id is not None:
                existing_turn_row = connection.execute(
                    """
                    SELECT id, web_session_id, origin, status, started_at, finished_at,
                           final_message_id
                    FROM web_turns
                    WHERE web_session_id = ? AND invocation_id = ?
                    """,
                    (session_id, invocation_id),
                ).fetchone()
                if existing_turn_row is not None:
                    existing_message_row = connection.execute(
                        """
                        SELECT id, session_id, turn_id, role, content, created_at
                        FROM messages
                        WHERE turn_id = ? AND role = 'user'
                        ORDER BY created_at ASC, rowid ASC
                        LIMIT 1
                        """,
                        (existing_turn_row["id"],),
                    ).fetchone()
                    if existing_message_row is None:
                        raise RuntimeError("idempotent turn is missing its user message")
                    if supplied_context is not None:
                        existing_context_row = connection.execute(
                            "SELECT context_json FROM turn_request_contexts WHERE turn_id = ?",
                            (existing_turn_row["id"],),
                        ).fetchone()
                        existing_context = (
                            json.loads(existing_context_row["context_json"])
                            if existing_context_row is not None
                            else None
                        )
                        if existing_context != supplied_context:
                            raise RuntimeError(
                                "idempotent turn request_context does not match"
                            )
                    return TurnSubmission(
                        turn=self._turn_from_row(existing_turn_row),
                        message=self._message_from_row(existing_message_row),
                        events=(),
                        created=False,
                    )
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
                    id, web_session_id, invocation_id, origin, status, started_at,
                    finished_at, final_message_id
                ) VALUES (?, ?, ?, 'user', 'running', ?, NULL, NULL)
                """,
                (turn.id, session_id, invocation_id, timestamp),
            )
            connection.execute(
                """
                INSERT INTO messages (id, session_id, turn_id, role, content, created_at)
                VALUES (?, ?, ?, 'user', ?, ?)
                """,
                (message_record.id, session_id, turn.id, message, timestamp),
            )
            pending_context_row = connection.execute(
                """
                SELECT context_json FROM pending_session_request_contexts
                WHERE web_session_id = ?
                """,
                (session_id,),
            ).fetchone()
            pending_context = (
                json.loads(pending_context_row["context_json"])
                if pending_context_row is not None
                else None
            )
            if (
                supplied_context is not None
                and pending_context is not None
                and supplied_context != pending_context
            ):
                raise RuntimeError("pending request_context does not match the turn")
            resolved_context = supplied_context or pending_context
            if resolved_context is not None:
                connection.execute(
                    """
                    INSERT INTO turn_request_contexts (turn_id, context_json, created_at)
                    VALUES (?, ?, ?)
                    """,
                    (
                        turn.id,
                        json.dumps(resolved_context, ensure_ascii=False, sort_keys=True),
                        timestamp,
                    ),
                )
                connection.execute(
                    "DELETE FROM pending_session_request_contexts WHERE web_session_id = ?",
                    (session_id,),
                )
            if int(session_row["contract_version"]) == 1:
                self._insert_response_authority(
                    connection,
                    turn_id=turn.id,
                    producer="router",
                    timestamp=timestamp,
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

    def save_pending_request_context(
        self,
        web_session_id: str,
        context: dict[str, Any],
    ) -> None:
        """Persist trusted shortcut scope privately until the first user Turn."""
        normalized = _normalize_turn_request_context(context)
        encoded = json.dumps(normalized, ensure_ascii=False, sort_keys=True)
        timestamp = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            session = connection.execute(
                "SELECT contract_version FROM sessions WHERE id = ?",
                (web_session_id,),
            ).fetchone()
            if session is None:
                raise KeyError(web_session_id)
            if int(session["contract_version"]) != 1:
                raise RuntimeError("request_context requires contract v1")
            existing = connection.execute(
                """
                SELECT context_json FROM pending_session_request_contexts
                WHERE web_session_id = ?
                """,
                (web_session_id,),
            ).fetchone()
            if existing is not None:
                if json.loads(existing["context_json"]) != normalized:
                    raise RuntimeError("pending request_context cannot be replaced")
                return
            first_turn = connection.execute(
                "SELECT 1 FROM web_turns WHERE web_session_id = ? LIMIT 1",
                (web_session_id,),
            ).fetchone()
            if first_turn is not None:
                raise RuntimeError("pending request_context must be saved before the first turn")
            connection.execute(
                """
                INSERT INTO pending_session_request_contexts (
                    web_session_id, context_json, created_at
                ) VALUES (?, ?, ?)
                """,
                (web_session_id, encoded, timestamp),
            )

    def get_turn_user_message(self, turn_id: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT content FROM messages
                WHERE turn_id = ? AND role = 'user'
                ORDER BY created_at ASC, rowid ASC LIMIT 1
                """,
                (turn_id,),
            ).fetchone()
        return str(row["content"]) if row is not None else None

    def get_turn_request_context(self, turn_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT context_json FROM turn_request_contexts WHERE turn_id = ?",
                (turn_id,),
            ).fetchone()
        if row is None:
            return None
        value = json.loads(row["context_json"])
        return dict(value) if isinstance(value, dict) else None

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
            connection.execute(
                "UPDATE conversation_agent_sessions SET active_turn_id = NULL WHERE active_turn_id = ?",
                (turn_id,),
            )
            connection.execute("DELETE FROM turn_runs WHERE turn_id = ?", (turn_id,))
            connection.execute(
                "DELETE FROM turn_response_authority WHERE turn_id = ?", (turn_id,)
            )
            connection.execute("DELETE FROM agentscope_turn_tools WHERE turn_id = ?", (turn_id,))
            connection.execute("DELETE FROM agentscope_turn_replies WHERE turn_id = ?", (turn_id,))
            connection.execute("DELETE FROM turn_request_contexts WHERE turn_id = ?", (turn_id,))
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

    def get_turn(self, turn_id: str) -> TurnRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, web_session_id, origin, status, started_at, finished_at,
                       final_message_id
                FROM web_turns WHERE id = ?
                """,
                (turn_id,),
            ).fetchone()
        return self._turn_from_row(row) if row is not None else None

    def get_navigation_workflow_turn_reason(self, turn_id: str) -> str | None:
        """Return the private semantic boundary attached to a System Turn."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT invocation_id FROM web_turns WHERE id = ?",
                (turn_id,),
            ).fetchone()
        return (
            _navigation_workflow_reason(row["invocation_id"])
            if row is not None
            else None
        )

    def begin_navigation_workflow_turn(
        self,
        *,
        task_id: str,
        reason: str,
        idempotency_key: str,
    ) -> tuple[TimelineEventRecord, ...]:
        """Open one idempotent System Turn for a semantic workflow boundary.

        The turn is created only when no user or interaction turn currently
        owns response authority. A deterministic milestone remains available
        when another turn is active.
        """

        if reason not in _NAVIGATION_WORKFLOW_TURN_FALLBACKS:
            raise ValueError("unsupported navigation workflow turn reason")
        if not idempotency_key.strip():
            raise ValueError("workflow turn idempotency key must not be empty")
        timestamp = _now()
        invocation_id = (
            f"{_NAVIGATION_WORKFLOW_TURN_PREFIX}{reason}:{idempotency_key}"
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            binding = connection.execute(
                """
                SELECT web_session_id, navigation_session_id, slot_state
                FROM conversation_task_bindings
                WHERE task_id = ?
                """,
                (task_id,),
            ).fetchone()
            if binding is None:
                raise KeyError(task_id)
            if binding["slot_state"] != "open":
                return ()
            existing = connection.execute(
                """
                SELECT id FROM web_turns
                WHERE web_session_id = ? AND invocation_id = ?
                """,
                (binding["web_session_id"], invocation_id),
            ).fetchone()
            if existing is not None:
                return ()
            if connection.execute(
                """
                SELECT 1 FROM web_turns
                WHERE web_session_id = ? AND status IN ('running', 'waiting')
                """,
                (binding["web_session_id"],),
            ).fetchone() is not None:
                return ()
            navigation = connection.execute(
                """
                SELECT agent_role FROM conversation_agent_sessions
                WHERE web_session_id = ? AND task_id = ?
                  AND agentscope_session_id = ?
                """,
                (
                    binding["web_session_id"],
                    task_id,
                    binding["navigation_session_id"],
                ),
            ).fetchone()
            if navigation is None or navigation["agent_role"] != "navigation":
                raise RuntimeError("navigation workflow session is unavailable")
            turn_id = f"turn_{uuid4().hex}"
            connection.execute(
                """
                INSERT INTO web_turns (
                    id, web_session_id, invocation_id, origin, status, started_at,
                    finished_at, final_message_id
                ) VALUES (?, ?, ?, 'system', 'running', ?, NULL, NULL)
                """,
                (
                    turn_id,
                    binding["web_session_id"],
                    invocation_id,
                    timestamp,
                ),
            )
            self._insert_response_authority(
                connection,
                turn_id=turn_id,
                producer="navigation",
                timestamp=timestamp,
            )
            connection.execute(
                """
                UPDATE conversation_agent_sessions
                SET active_turn_id = ?, updated_at = ?
                WHERE web_session_id = ? AND task_id = ?
                  AND agentscope_session_id = ?
                """,
                (
                    turn_id,
                    timestamp,
                    binding["web_session_id"],
                    task_id,
                    binding["navigation_session_id"],
                ),
            )
            event = self._insert_timeline_event(
                connection,
                session_id=str(binding["web_session_id"]),
                turn_id=turn_id,
                event={
                    "type": "turn_start",
                    "timestamp": timestamp,
                    "payload": {"status": "running", "started_at": timestamp},
                },
                created_at=timestamp,
                origin_key=f"{invocation_id}:turn-start",
            )
            connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (timestamp, binding["web_session_id"]),
            )
        return (event,)

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

    def reconcile_terminal_turn_residues(self) -> int:
        """Close internal leases left open for an already-terminal Web Turn."""

        timestamp = _now()
        repaired = 0
        terminal_turns = (
            "SELECT id FROM web_turns "
            "WHERE status IN ('completed', 'failed', 'interrupted')"
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                f"""
                UPDATE agentscope_turn_replies
                SET status = 'interrupted', updated_at = ?
                WHERE status IN ('pending', 'running', 'waiting')
                  AND turn_id IN ({terminal_turns})
                """,
                (timestamp,),
            )
            repaired += max(cursor.rowcount, 0)
            cursor = connection.execute(
                f"""
                UPDATE turn_runs
                SET status = 'interrupted', updated_at = ?, finished_at = ?
                WHERE status = 'running' AND turn_id IN ({terminal_turns})
                """,
                (timestamp, timestamp),
            )
            repaired += max(cursor.rowcount, 0)
            cursor = connection.execute(
                f"""
                UPDATE turn_response_authority
                SET lease_state = 'closed', updated_at = ?
                WHERE lease_state = 'open' AND final_message_id IS NULL
                  AND turn_id IN ({terminal_turns})
                """,
                (timestamp,),
            )
            repaired += max(cursor.rowcount, 0)
            cursor = connection.execute(
                f"""
                UPDATE conversation_agent_sessions
                SET active_turn_id = NULL, updated_at = ?
                WHERE active_turn_id IN ({terminal_turns})
                """,
                (timestamp,),
            )
            repaired += max(cursor.rowcount, 0)
            cursor = connection.execute(
                f"""
                UPDATE agentscope_sessions
                SET active_turn_id = NULL, updated_at = ?
                WHERE active_turn_id IN ({terminal_turns})
                """,
                (timestamp,),
            )
            repaired += max(cursor.rowcount, 0)
        return repaired

    def get_session(self, session_id: str) -> SessionDetail | None:
        with self._connect() as connection:
            session_row = connection.execute(
                """
                SELECT id, title, status, created_at, updated_at, contract_version
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
            snapshot_seq=int(event_rows[-1]["seq"]) if event_rows else 0,
        )

    def list_timeline_events_after(
        self,
        session_id: str,
        *,
        after_seq: int,
    ) -> list[TimelineEventRecord]:
        if after_seq < 0:
            raise ValueError("after_seq must be non-negative")
        with self._connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if exists is None:
                raise KeyError(session_id)
            rows = connection.execute(
                """
                SELECT id, session_id, turn_id, seq, type, source, run_id,
                       parent_run_id, timestamp, payload_json, created_at
                FROM timeline_events
                WHERE session_id = ? AND seq > ?
                ORDER BY seq ASC, rowid ASC
                """,
                (session_id, after_seq),
            ).fetchall()
        return [self._timeline_event_from_row(row) for row in rows]

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

    def append_timeline_event(
        self,
        session_id: str,
        event: dict,
        *,
        origin_key: str | None = None,
    ) -> TimelineEventRecord:
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
            if origin_key is not None:
                existing = connection.execute(
                    """
                    SELECT id, session_id, turn_id, seq, type, source, run_id,
                           parent_run_id, timestamp, payload_json, created_at
                    FROM timeline_events
                    WHERE origin_key = ?
                    """,
                    (origin_key,),
                ).fetchone()
                if existing is not None:
                    if existing["session_id"] != session_id:
                        raise RuntimeError(
                            "timeline event origin belongs to another session"
                        )
                    return self._timeline_event_from_row(existing)
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
                    origin_key,
                    type,
                    source,
                    run_id,
                    parent_run_id,
                    timestamp,
                    payload_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record_id,
                    session_id,
                    turn_id,
                    seq,
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

    def _finalize_v1_interaction_turn(
        self,
        connection: sqlite3.Connection,
        *,
        web_session_id: str,
        turn_id: str,
        agentscope_session_id: str,
        producer: str,
        reply_id: str | None,
        payload: dict,
        timestamp: str,
        origin_key: str,
    ) -> list[TimelineEventRecord]:
        """End the prompt Turn while the specialist remains resumable."""
        turn = connection.execute(
            "SELECT status FROM web_turns WHERE id = ? AND web_session_id = ?",
            (turn_id, web_session_id),
        ).fetchone()
        if turn is None or turn["status"] not in {"running", "waiting"}:
            return []
        authority = connection.execute(
            "SELECT * FROM turn_response_authority WHERE turn_id = ?",
            (turn_id,),
        ).fetchone()
        if (
            authority is None
            or authority["producer"] != producer
            or authority["lease_state"] != "open"
            or authority["final_message_id"] is not None
        ):
            raise ContractConflictError(
                "response_authority_mismatch",
                "interaction prompt producer no longer owns this turn",
            )
        safe_text = sanitize_public_reply(
            payload.get("summary") or payload.get("title") or "继续前需要你的选择。"
        ) or "继续前需要你的选择。"
        message_id = f"message_{uuid4().hex}"
        connection.execute(
            """
            INSERT INTO messages (id, session_id, turn_id, role, content, created_at)
            VALUES (?, ?, ?, 'assistant', ?, ?)
            """,
            (message_id, web_session_id, turn_id, safe_text, timestamp),
        )
        final = self._insert_timeline_event(
            connection,
            session_id=web_session_id,
            turn_id=turn_id,
            event={
                "type": "final",
                "timestamp": timestamp,
                "payload": {"text": safe_text, "message_id": message_id},
            },
            created_at=timestamp,
            origin_key=f"{origin_key}:interaction-final",
        )
        connection.execute(
            """
            UPDATE web_turns
            SET status = 'completed', finished_at = ?, final_message_id = ?
            WHERE id = ? AND status IN ('running', 'waiting')
            """,
            (timestamp, message_id, turn_id),
        )
        connection.execute(
            """
            UPDATE turn_response_authority
            SET lease_state = 'closed', final_message_id = ?, updated_at = ?
            WHERE turn_id = ? AND producer = ? AND generation = ?
              AND lease_state = 'open' AND final_message_id IS NULL
            """,
            (
                message_id,
                timestamp,
                turn_id,
                producer,
                int(authority["generation"]),
            ),
        )
        if reply_id is not None:
            connection.execute(
                """
                UPDATE agentscope_turn_replies
                SET status = 'waiting', updated_at = ?
                WHERE turn_id = ? AND agentscope_session_id = ? AND reply_id = ?
                  AND status IN ('pending', 'running', 'waiting')
                """,
                (timestamp, turn_id, agentscope_session_id, reply_id),
            )
        connection.execute(
            """
            UPDATE conversation_agent_sessions
            SET active_turn_id = NULL, updated_at = ?
            WHERE web_session_id = ? AND agentscope_session_id = ?
              AND active_turn_id = ?
            """,
            (timestamp, web_session_id, agentscope_session_id, turn_id),
        )
        state = self._insert_timeline_event(
            connection,
            session_id=web_session_id,
            turn_id=turn_id,
            event={
                "type": "turn_state",
                "timestamp": timestamp,
                "payload": {"status": "completed", "finished_at": timestamp},
            },
            created_at=timestamp,
            origin_key=f"{origin_key}:interaction-completed",
        )
        return [final, state]

    def append_projected_event_batch(
        self,
        *,
        web_session_id: str,
        agentscope_session_id: str,
        entry_id: str,
        events: list[dict],
        private_events: list[dict] | tuple[dict, ...] | None = None,
        raw_event_type: str | None = None,
        reply_id: str | None = None,
        allow_system_turn_defer: bool = True,
    ) -> list[TimelineEventRecord]:
        """Persist private lifecycle, public projection and cursor atomically."""
        private_events = list(private_events or ())
        timestamp = _now()
        inserted: list[TimelineEventRecord] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            session_row = connection.execute(
                "SELECT contract_version FROM sessions WHERE id = ?",
                (web_session_id,),
            ).fetchone()
            if session_row is None:
                raise KeyError(web_session_id)
            mapping = connection.execute(
                """
                SELECT agent_id, active_turn_id, NULL AS agent_role, NULL AS task_id
                FROM agentscope_sessions
                WHERE web_session_id = ? AND agentscope_session_id = ?
                """,
                (web_session_id, agentscope_session_id),
            ).fetchone()
            v1_mapping = False
            if mapping is None and int(session_row["contract_version"]) == 1:
                mapping = connection.execute(
                    """
                    SELECT agent_id, active_turn_id, agent_role, task_id
                    FROM conversation_agent_sessions
                    WHERE web_session_id = ? AND agentscope_session_id = ?
                    """,
                    (web_session_id, agentscope_session_id),
                ).fetchone()
                v1_mapping = mapping is not None
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
                    f"""
                    UPDATE {'conversation_agent_sessions' if v1_mapping else 'agentscope_sessions'}
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
                if active_turn is not None and v1_mapping:
                    active_authority = connection.execute(
                        """
                        SELECT producer, lease_state FROM turn_response_authority
                        WHERE turn_id = ?
                        """,
                        (active_turn["id"],),
                    ).fetchone()
                    if (
                        active_authority is not None
                        and active_authority["lease_state"] == "open"
                        and active_authority["producer"] != mapping["agent_role"]
                    ):
                        if not allow_system_turn_defer:
                            raise ContractConflictError(
                                "system_turn_deferred",
                                "background update is waiting for the active user turn",
                            )
                        outbox_id = f"outbox_{uuid4().hex}"
                        self._insert_outbox(
                            connection,
                            outbox_id=outbox_id,
                            kind="system_turn",
                            aggregate_type="agentscope_event",
                            aggregate_id=f"{agentscope_session_id}:{entry_id}",
                            web_session_id=web_session_id,
                            task_id=_optional_text(mapping["task_id"]),
                            turn_id=None,
                            payload={
                                "agentscope_session_id": agentscope_session_id,
                                "entry_id": entry_id,
                                "events": events,
                                "private_events": private_events,
                                "raw_event_type": raw_event_type,
                                "reply_id": reply_id,
                            },
                            idempotency_key=(
                                f"system_turn:{agentscope_session_id}:{entry_id}"
                            ),
                            available_at=timestamp,
                            timestamp=timestamp,
                        )
                        connection.execute(
                            """
                            UPDATE conversation_agent_sessions
                            SET event_cursor = ?, updated_at = ?
                            WHERE web_session_id = ? AND agentscope_session_id = ?
                            """,
                            (
                                entry_id,
                                timestamp,
                                web_session_id,
                                agentscope_session_id,
                            ),
                        )
                        return []
                turn_id = _optional_text(active_turn["id"]) if active_turn is not None else None
            if v1_mapping and turn_id is not None:
                authority_row = connection.execute(
                    """
                    SELECT producer, lease_state, final_message_id
                    FROM turn_response_authority WHERE turn_id = ?
                    """,
                    (turn_id,),
                ).fetchone()
                if (
                    authority_row is None
                    or authority_row["producer"] != mapping["agent_role"]
                    or authority_row["lease_state"] != "open"
                    or authority_row["final_message_id"] is not None
                ):
                    # Re-check authority inside the same write transaction that
                    # would publish the events. Runtime's earlier check is only
                    # an optimization and cannot close this race by itself.
                    # Do not feed stale producer tools into the operative Run
                    # ledger: blockers are turn-wide, so a late tool_start could
                    # otherwise prevent the current producer from finalizing.
                    connection.execute(
                        """
                        UPDATE conversation_agent_sessions
                        SET event_cursor = ?, updated_at = ?
                        WHERE web_session_id = ? AND agentscope_session_id = ?
                        """,
                        (
                            entry_id,
                            timestamp,
                            web_session_id,
                            agentscope_session_id,
                        ),
                    )
                    return []
            v1_system_attention = v1_mapping and any(
                str(event.get("type") or "") in {"final", "interaction_required"}
                or (
                    str(event.get("type") or "") == "task_state_updated"
                    and isinstance(event.get("payload"), dict)
                    and event["payload"].get("status")
                    in {"completed", "failed", "needs_replan", "waiting_user"}
                )
                for event in events
            )
            if turn_id is None and (
                (raw_event_type == "REPLY_START" and not v1_mapping)
                or v1_system_attention
            ):
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
                if v1_mapping:
                    self._insert_response_authority(
                        connection,
                        turn_id=turn_id,
                        producer=str(mapping["agent_role"]),
                        timestamp=timestamp,
                    )
                    if reply_id is not None:
                        connection.execute(
                            """
                            INSERT INTO agentscope_turn_replies (
                                id, turn_id, agentscope_session_id, agent_id,
                                reply_id, source, status, summary_text,
                                created_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, 'event', 'waiting', NULL, ?, ?)
                            ON CONFLICT(agentscope_session_id, reply_id)
                            WHERE reply_id IS NOT NULL DO UPDATE SET
                                turn_id = excluded.turn_id,
                                status = 'waiting',
                                updated_at = excluded.updated_at
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
                                **({} if v1_mapping else {
                                    "source": "agentscope",
                                    "run_id": agentscope_session_id,
                                }),
                                "timestamp": timestamp,
                                "payload": {"status": "running", "started_at": timestamp},
                            },
                            created_at=timestamp,
                            origin_key=turn_start_origin,
                        )
                    )
            if turn_id is not None:
                connection.execute(
                    f"""
                    UPDATE {'conversation_agent_sessions' if v1_mapping else 'agentscope_sessions'}
                    SET active_turn_id = ?, updated_at = ?
                    WHERE web_session_id = ? AND agentscope_session_id = ?
                    """,
                    (turn_id, timestamp, web_session_id, agentscope_session_id),
                )
                self._record_private_tool_events(
                    connection,
                    turn_id=turn_id,
                    agentscope_session_id=agentscope_session_id,
                    events=private_events,
                    timestamp=timestamp,
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
            background_update_only = False
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
                if event_type == "task_state_updated" and safe_payload.get("status") == "active":
                    background_update_only = True
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
                if v1_mapping and turn_id is not None and event_type == "interaction_required":
                    inserted.extend(
                        self._finalize_v1_interaction_turn(
                            connection,
                            web_session_id=web_session_id,
                            turn_id=turn_id,
                            agentscope_session_id=agentscope_session_id,
                            producer=str(mapping["agent_role"]),
                            reply_id=reply_id,
                            payload=safe_payload,
                            timestamp=timestamp,
                            origin_key=origin_key,
                        )
                    )
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

                if v1_mapping:
                    # Contract v1 transfers response authority between producers.
                    # A parent Router reply/tool can legitimately remain open in
                    # the private ledger after Navigation owns the Turn, because
                    # its late terminal events are rejected by the authority gate.
                    # Only the authoritative producer's own work may block its
                    # final; otherwise an AwaitUser final is downgraded to a
                    # progress update and the Turn can never close.
                    private_tool_counts = connection.execute(
                        """
                        SELECT
                            SUM(CASE WHEN status IN ('running', 'background')
                                     THEN 1 ELSE 0 END),
                            SUM(CASE WHEN status = 'background' THEN 1 ELSE 0 END)
                        FROM agentscope_turn_tools
                        WHERE turn_id = ? AND agentscope_session_id = ?
                        """,
                        (turn_id, agentscope_session_id),
                    ).fetchone()
                    pending_reply_count = int(
                        connection.execute(
                            """
                            SELECT COUNT(*) FROM agentscope_turn_replies
                            WHERE turn_id = ? AND agentscope_session_id = ?
                              AND status IN ('pending', 'running', 'waiting')
                            """,
                            (turn_id, agentscope_session_id),
                        ).fetchone()[0]
                    )
                else:
                    private_tool_counts = connection.execute(
                        """
                        SELECT
                            SUM(CASE WHEN status IN ('running', 'background')
                                     THEN 1 ELSE 0 END),
                            SUM(CASE WHEN status = 'background' THEN 1 ELSE 0 END)
                        FROM agentscope_turn_tools WHERE turn_id = ?
                        """,
                        (turn_id,),
                    ).fetchone()
                    pending_reply_count = int(
                        connection.execute(
                            """
                            SELECT COUNT(*) FROM agentscope_turn_replies
                            WHERE turn_id = ?
                              AND status IN ('pending', 'running', 'waiting')
                            """,
                            (turn_id,),
                        ).fetchone()[0]
                    )
                unresolved_tools = int(private_tool_counts[0] or 0)
                background_tools = int(private_tool_counts[1] or 0)
                blockers = pending_reply_count + unresolved_tools
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
                turn_origin_row = connection.execute(
                    "SELECT origin, invocation_id FROM web_turns WHERE id = ?",
                    (turn_id,),
                ).fetchone()
                workflow_reason = (
                    _navigation_workflow_reason(turn_origin_row["invocation_id"])
                    if turn_origin_row is not None
                    else None
                )
                binding_status_row = (
                    connection.execute(
                        """
                        SELECT status, slot_state FROM conversation_task_bindings
                        WHERE task_id = ?
                        """,
                        (mapping["task_id"],),
                    ).fetchone()
                    if v1_mapping and mapping["task_id"] is not None
                    else connection.execute(
                        """
                        SELECT status, slot_state FROM conversation_task_bindings
                        WHERE web_session_id = ? AND slot_state = 'open'
                        ORDER BY updated_at DESC, rowid DESC LIMIT 1
                        """,
                        (web_session_id,),
                    ).fetchone()
                    if v1_mapping
                    else None
                )
                open_task_status = (
                    str(binding_status_row["status"])
                    if binding_status_row is not None
                    and binding_status_row["slot_state"] == "open"
                    else None
                )
                if (
                    v1_mapping
                    and turn_origin_row is not None
                    and turn_origin_row["origin"] in {"user", "interaction"}
                    and reply_summary is None
                    and open_task_status is not None
                    and (background_tools > 0 or blockers == 0)
                ):
                    turn_reply = (
                        _INTERACTION_CONTINUATION_REPLY
                        if turn_origin_row["origin"] == "interaction"
                        and open_task_status in {"active", "waiting_user"}
                        else _open_task_turn_reply(
                            open_task_status,
                            background_tools=background_tools,
                        )
                    )
                    if open_task_status == "active" and background_tools == 0:
                        _logger.warning(
                            "Navigation User Turn ended with an open active task but no "
                            "durable background-tool ledger: web_session_id=%s turn_id=%s "
                            "agentscope_session_id=%s",
                            web_session_id,
                            turn_id,
                            agentscope_session_id,
                        )
                    inserted.extend(
                        self._finalize_v1_background_turn(
                            connection,
                            web_session_id=web_session_id,
                            turn_id=turn_id,
                            agentscope_session_id=agentscope_session_id,
                            producer=str(mapping["agent_role"]),
                            entry_id=entry_id,
                            timestamp=timestamp,
                            text=turn_reply,
                        )
                    )
                    connection.execute(
                        """
                        UPDATE conversation_agent_sessions
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
                if (
                    v1_mapping
                    and turn_origin_row is not None
                    and turn_origin_row["origin"] == "system"
                    and workflow_reason is not None
                    and reply_summary is None
                    and blockers == 0
                    and not waiting
                ):
                    reply_summary = _NAVIGATION_WORKFLOW_TURN_FALLBACKS[
                        workflow_reason
                    ]
                if (
                    v1_mapping
                    and turn_origin_row is not None
                    and turn_origin_row["origin"] == "system"
                    and (background_update_only or open_task_status is not None)
                    and reply_summary is None
                    and blockers == 0
                ):
                    connection.execute(
                        """
                        UPDATE web_turns SET status = 'completed', finished_at = ?
                        WHERE id = ?
                        """,
                        (timestamp, turn_id),
                    )
                    connection.execute(
                        """
                        UPDATE turn_response_authority
                        SET lease_state = 'closed', updated_at = ?
                        WHERE turn_id = ? AND lease_state = 'open'
                        """,
                        (timestamp, turn_id),
                    )
                    connection.execute(
                        """
                        UPDATE conversation_agent_sessions
                        SET active_turn_id = NULL, event_cursor = ?, updated_at = ?
                        WHERE web_session_id = ? AND agentscope_session_id = ?
                        """,
                        (entry_id, timestamp, web_session_id, agentscope_session_id),
                    )
                    inserted.append(
                        self._insert_timeline_event(
                            connection,
                            session_id=web_session_id,
                            turn_id=turn_id,
                            event={
                                "type": "turn_state",
                                "timestamp": timestamp,
                                "payload": {
                                    "status": "completed",
                                    "finished_at": timestamp,
                                },
                            },
                            created_at=timestamp,
                            origin_key=(
                                f"agentscope:{agentscope_session_id}:{entry_id}:"
                                "background-update-completed"
                            ),
                        )
                    )
                    connection.execute(
                        "UPDATE sessions SET updated_at = ? WHERE id = ?",
                        (timestamp, web_session_id),
                    )
                    return inserted
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
                        authority_generation: int | None = None
                        if v1_mapping:
                            producer = str(mapping["agent_role"])
                            authority_row = connection.execute(
                                """
                                SELECT * FROM turn_response_authority WHERE turn_id = ?
                                """,
                                (turn_id,),
                            ).fetchone()
                            if (
                                authority_row is None
                                or authority_row["producer"] != producer
                                or authority_row["lease_state"] != "open"
                                or authority_row["final_message_id"] is not None
                            ):
                                connection.execute(
                                    """
                                    UPDATE conversation_agent_sessions
                                    SET event_cursor = ?, updated_at = ?
                                    WHERE web_session_id = ? AND agentscope_session_id = ?
                                    """,
                                    (
                                        entry_id,
                                        timestamp,
                                        web_session_id,
                                        agentscope_session_id,
                                    ),
                                )
                                return inserted
                            authority_generation = int(authority_row["generation"])
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
                                    **({} if v1_mapping else {
                                        "source": "agentscope",
                                        "run_id": agentscope_session_id,
                                    }),
                                    "timestamp": timestamp,
                                    "payload": {
                                        "text": reply_summary,
                                        "message_id": final_message_id,
                                        **(
                                            {"reply_id": reply_id}
                                            if reply_id and not v1_mapping
                                            else {}
                                        ),
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
                        if v1_mapping:
                            connection.execute(
                                """
                                UPDATE turn_response_authority
                                SET lease_state = 'closed', final_message_id = ?, updated_at = ?
                                WHERE turn_id = ? AND producer = ? AND generation = ?
                                  AND lease_state = 'open' AND final_message_id IS NULL
                                """,
                                (
                                    final_message_id,
                                    timestamp,
                                    turn_id,
                                    mapping["agent_role"],
                                    authority_generation,
                                ),
                            )
                        connection.execute(
                            f"UPDATE {'conversation_agent_sessions' if v1_mapping else 'agentscope_sessions'} "
                            "SET active_turn_id = NULL WHERE active_turn_id = ?",
                            (turn_id,),
                        )
                        inserted.append(
                            self._insert_timeline_event(
                                connection,
                                session_id=web_session_id,
                                turn_id=turn_id,
                                event={
                                    "type": "turn_state",
                                    **({} if v1_mapping else {
                                        "source": "agentscope",
                                        "run_id": agentscope_session_id,
                                    }),
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
                f"""
                UPDATE {'conversation_agent_sessions' if v1_mapping else 'agentscope_sessions'}
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
    def _record_private_tool_events(
        connection: sqlite3.Connection,
        *,
        turn_id: str,
        agentscope_session_id: str,
        events: list[dict],
        timestamp: str,
    ) -> None:
        """Update the private tool ledger before any public projection is used."""
        for event in events:
            event_type = str(event.get("type") or "")
            if event_type not in {"tool_start", "tool_background", "tool_end"}:
                continue
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            call_id = _optional_text(payload.get("call_id"))
            if call_id is None:
                continue
            tool = _optional_text(payload.get("tool")) or "unknown_tool"
            status = (
                "running"
                if event_type == "tool_start"
                else "background"
                if event_type == "tool_background"
                else _terminal_tool_status(payload.get("status"))
            )
            finished_at = (
                timestamp if status in {"completed", "failed", "interrupted"} else None
            )
            connection.execute(
                """
                INSERT INTO agentscope_turn_tools (
                    turn_id, agentscope_session_id, call_id, tool, status,
                    started_at, finished_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(turn_id, agentscope_session_id, call_id) DO UPDATE SET
                    tool = CASE
                        WHEN excluded.tool = 'unknown_tool'
                        THEN agentscope_turn_tools.tool
                        ELSE excluded.tool
                    END,
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
                    finished_at,
                    timestamp,
                ),
            )

    def _finalize_v1_background_turn(
        self,
        connection: sqlite3.Connection,
        *,
        web_session_id: str,
        turn_id: str,
        agentscope_session_id: str,
        producer: str,
        entry_id: str,
        timestamp: str,
        text: str = _BACKGROUND_TURN_REPLY,
    ) -> list[TimelineEventRecord]:
        authority = connection.execute(
            "SELECT * FROM turn_response_authority WHERE turn_id = ?",
            (turn_id,),
        ).fetchone()
        if (
            authority is None
            or authority["producer"] != producer
            or authority["lease_state"] != "open"
            or authority["final_message_id"] is not None
        ):
            return []
        final_message_id = f"message_{uuid4().hex}"
        connection.execute(
            """
            INSERT INTO messages (id, session_id, turn_id, role, content, created_at)
            VALUES (?, ?, ?, 'assistant', ?, ?)
            """,
            (
                final_message_id,
                web_session_id,
                turn_id,
                text,
                timestamp,
            ),
        )
        connection.execute(
            """
            UPDATE web_turns
            SET status = 'completed', finished_at = ?, final_message_id = ?
            WHERE id = ? AND status IN ('running', 'waiting')
            """,
            (timestamp, final_message_id, turn_id),
        )
        connection.execute(
            """
            UPDATE turn_response_authority
            SET lease_state = 'closed', final_message_id = ?, updated_at = ?
            WHERE turn_id = ? AND producer = ? AND generation = ?
              AND lease_state = 'open' AND final_message_id IS NULL
            """,
            (
                final_message_id,
                timestamp,
                turn_id,
                producer,
                int(authority["generation"]),
            ),
        )
        connection.execute(
            """
            UPDATE conversation_agent_sessions SET active_turn_id = NULL
            WHERE active_turn_id = ? AND agentscope_session_id = ?
            """,
            (turn_id, agentscope_session_id),
        )
        final = self._insert_timeline_event(
            connection,
            session_id=web_session_id,
            turn_id=turn_id,
            event={
                "type": "final",
                "timestamp": timestamp,
                "payload": {"text": text, "message_id": final_message_id},
            },
            created_at=timestamp,
            origin_key=f"agentscope:{agentscope_session_id}:{entry_id}:background-final",
        )
        state = self._insert_timeline_event(
            connection,
            session_id=web_session_id,
            turn_id=turn_id,
            event={
                "type": "turn_state",
                "timestamp": timestamp,
                "payload": {"status": "completed", "finished_at": timestamp},
            },
            created_at=timestamp,
            origin_key=f"agentscope:{agentscope_session_id}:{entry_id}:background-completed",
        )
        return [final, state]

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
            private_rows = connection.execute(
                """
                SELECT turns.web_session_id, tools.turn_id,
                       tools.agentscope_session_id, tools.tool, tools.call_id
                FROM agentscope_turn_tools AS tools
                JOIN web_turns AS turns ON turns.id = tools.turn_id
                WHERE tools.status = 'background'
                ORDER BY tools.started_at ASC, tools.rowid ASC
                """
            ).fetchall()
            rows = connection.execute(
                """
                SELECT session_id, turn_id, run_id, type, payload_json
                FROM timeline_events
                WHERE type IN ('tool_background', 'tool_end')
                ORDER BY session_id ASC, seq ASC
                """
            ).fetchall()
        unresolved: dict[tuple[str, str, str], UnresolvedBackgroundTool] = {
            (
                row["web_session_id"],
                row["agentscope_session_id"],
                row["call_id"],
            ): UnresolvedBackgroundTool(
                web_session_id=row["web_session_id"],
                agentscope_session_id=row["agentscope_session_id"],
                tool=row["tool"],
                call_id=row["call_id"],
                turn_id=row["turn_id"],
            )
            for row in private_rows
        }
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
            session = connection.execute(
                "SELECT contract_version FROM sessions WHERE id = ?",
                (background.web_session_id,),
            ).fetchone()
            if session is None:
                raise KeyError(background.web_session_id)
            if int(session["contract_version"]) == 1:
                if background.turn_id is not None:
                    connection.execute(
                        """
                        UPDATE agentscope_turn_tools
                        SET status = ?, finished_at = ?, updated_at = ?
                        WHERE turn_id = ? AND agentscope_session_id = ? AND call_id = ?
                          AND status IN ('running', 'background')
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
                return None
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
            connection.execute(
                "DELETE FROM pending_session_request_contexts WHERE web_session_id = ?",
                (session_id,),
            )
            connection.execute(
                "DELETE FROM interactions WHERE web_session_id = ?", (session_id,)
            )
            connection.execute(
                "DELETE FROM runtime_outbox WHERE web_session_id = ?", (session_id,)
            )
            connection.execute(
                """
                DELETE FROM runtime_resource_leases
                WHERE task_id IN (
                    SELECT task_id FROM conversation_task_bindings WHERE web_session_id = ?
                )
                """,
                (session_id,),
            )
            connection.execute(
                "DELETE FROM conversation_task_focus WHERE web_session_id = ?", (session_id,)
            )
            connection.execute(
                """
                DELETE FROM turn_runs WHERE turn_id IN (
                    SELECT id FROM web_turns WHERE web_session_id = ?
                )
                """,
                (session_id,),
            )
            connection.execute(
                """
                DELETE FROM turn_response_authority WHERE turn_id IN (
                    SELECT id FROM web_turns WHERE web_session_id = ?
                )
                """,
                (session_id,),
            )
            connection.execute(
                "DELETE FROM conversation_agent_sessions WHERE web_session_id = ?",
                (session_id,),
            )
            connection.execute(
                "DELETE FROM conversation_task_bindings WHERE web_session_id = ?",
                (session_id,),
            )
            connection.execute("DELETE FROM agentscope_sessions WHERE web_session_id = ?", (session_id,))
            connection.execute(
                "DELETE FROM agentscope_turn_tools WHERE turn_id IN (SELECT id FROM web_turns WHERE web_session_id = ?)",
                (session_id,),
            )
            connection.execute(
                "DELETE FROM agentscope_turn_replies WHERE turn_id IN (SELECT id FROM web_turns WHERE web_session_id = ?)",
                (session_id,),
            )
            connection.execute(
                "DELETE FROM turn_request_contexts WHERE turn_id IN (SELECT id FROM web_turns WHERE web_session_id = ?)",
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
                SELECT replies.turn_id, turns.web_session_id, turns.status,
                       sessions.contract_version
                FROM agentscope_turn_replies AS replies
                JOIN web_turns AS turns ON turns.id = replies.turn_id
                JOIN sessions ON sessions.id = turns.web_session_id
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
            if int(reply["contract_version"]) == 1:
                open_task = connection.execute(
                    """
                    SELECT status FROM conversation_task_bindings
                    WHERE web_session_id = ? AND slot_state = 'open'
                    ORDER BY updated_at DESC, rowid DESC LIMIT 1
                    """,
                    (reply["web_session_id"],),
                ).fetchone()
                failure_text = (
                    _open_task_turn_reply(str(open_task["status"]))
                    if open_task is not None
                    else _SAFE_PUBLIC_REPLY_FAILURE
                )
                authority_row = connection.execute(
                    "SELECT * FROM turn_response_authority WHERE turn_id = ?",
                    (reply["turn_id"],),
                ).fetchone()
                if authority_row is not None and authority_row["lease_state"] != "open":
                    return []
                generation = (
                    int(authority_row["generation"])
                    if authority_row is not None
                    and authority_row["producer"] == "system_controller"
                    else int(authority_row["generation"]) + 1
                    if authority_row is not None
                    else 0
                )
                if authority_row is None:
                    connection.execute(
                        """
                        INSERT INTO turn_response_authority (
                            turn_id, producer, generation, lease_state,
                            final_message_id, created_at, updated_at
                        ) VALUES (?, 'system_controller', ?, 'open', NULL, ?, ?)
                        """,
                        (reply["turn_id"], generation, timestamp, timestamp),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE turn_response_authority
                        SET producer = 'system_controller', generation = ?, updated_at = ?
                        WHERE turn_id = ? AND lease_state = 'open'
                          AND final_message_id IS NULL
                        """,
                        (generation, timestamp, reply["turn_id"]),
                    )
                message_id = f"message_{uuid4().hex}"
                connection.execute(
                    """
                    INSERT INTO messages (
                        id, session_id, turn_id, role, content, created_at
                    ) VALUES (?, ?, ?, 'assistant', ?, ?)
                    """,
                    (
                        message_id,
                        reply["web_session_id"],
                        reply["turn_id"],
                        failure_text,
                        timestamp,
                    ),
                )
                final = self._insert_timeline_event(
                    connection,
                    session_id=reply["web_session_id"],
                    turn_id=reply["turn_id"],
                    event={
                        "type": "final",
                        "timestamp": timestamp,
                        "payload": {
                            "text": failure_text,
                            "message_id": message_id,
                        },
                    },
                    created_at=timestamp,
                    origin_key=f"reply-failed:{lease_id}:final",
                )
                connection.execute(
                    """
                    UPDATE web_turns
                    SET status = 'failed', finished_at = ?, final_message_id = ?
                    WHERE id = ? AND status IN ('running', 'waiting')
                    """,
                    (timestamp, message_id, reply["turn_id"]),
                )
                connection.execute(
                    """
                    UPDATE turn_response_authority
                    SET lease_state = 'closed', final_message_id = ?, updated_at = ?
                    WHERE turn_id = ? AND producer = 'system_controller'
                      AND generation = ? AND lease_state = 'open'
                      AND final_message_id IS NULL
                    """,
                    (message_id, timestamp, reply["turn_id"], generation),
                )
                connection.execute(
                    "UPDATE conversation_agent_sessions "
                    "SET active_turn_id = NULL, updated_at = ? WHERE active_turn_id = ?",
                    (timestamp, reply["turn_id"]),
                )
                connection.execute(
                    "UPDATE agentscope_sessions "
                    "SET active_turn_id = NULL, updated_at = ? WHERE active_turn_id = ?",
                    (timestamp, reply["turn_id"]),
                )
                state = self._insert_timeline_event(
                    connection,
                    session_id=reply["web_session_id"],
                    turn_id=reply["turn_id"],
                    event={
                        "type": "turn_state",
                        "timestamp": timestamp,
                        "payload": {"status": "failed", "finished_at": timestamp},
                    },
                    created_at=timestamp,
                    origin_key=f"reply-failed:{lease_id}:state",
                )
                connection.execute(
                    "UPDATE sessions SET updated_at = ? WHERE id = ?",
                    (timestamp, reply["web_session_id"]),
                )
                return [final, state]
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
                UPDATE turn_runs
                SET status = 'interrupted', updated_at = ?, finished_at = ?
                WHERE turn_id = ? AND status = 'running'
                """,
                (timestamp, timestamp, turn_id),
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
                """
                UPDATE turn_response_authority
                SET lease_state = 'closed', updated_at = ?
                WHERE turn_id = ? AND lease_state = 'open'
                  AND final_message_id IS NULL
                """,
                (timestamp, turn_id),
            )
            connection.execute(
                """
                UPDATE conversation_agent_sessions
                SET active_turn_id = NULL, updated_at = ?
                WHERE active_turn_id = ?
                """,
                (timestamp, turn_id),
            )
            connection.execute(
                """
                UPDATE agentscope_sessions
                SET active_turn_id = NULL, updated_at = ?
                WHERE active_turn_id = ?
                """,
                (timestamp, turn_id),
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
            connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (timestamp, web_session_id),
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

    # Contract v1 storage primitives. Legacy AgentScope mappings above remain unchanged.

    def save_conversation_agent_session(
        self,
        web_session_id: str,
        *,
        agent_role: str,
        agent_id: str,
        agentscope_session_id: str,
        task_id: str | None = None,
    ) -> ConversationAgentSession:
        if agent_role not in {"router", "navigation"}:
            raise ValueError("agent_role must be router or navigation")
        if (agent_role == "router") != (task_id is None):
            raise ValueError("router sessions cannot have task_id; navigation sessions require it")
        timestamp = _now()
        record_id = f"conversation_agent_session_{uuid4().hex}"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_contract_v1(connection, web_session_id)
            existing = connection.execute(
                """
                SELECT * FROM conversation_agent_sessions
                WHERE web_session_id = ? AND agent_role = ?
                  AND ((task_id IS NULL AND ? IS NULL) OR task_id = ?)
                """,
                (web_session_id, agent_role, task_id, task_id),
            ).fetchone()
            if existing is not None:
                if (
                    existing["agent_id"] != agent_id
                    or existing["agentscope_session_id"] != agentscope_session_id
                ):
                    raise ContractConflictError(
                        "agent_session_conflict",
                        "the conversation agent session is already bound",
                        current=self._conversation_agent_session_from_row(existing),
                    )
                return self._conversation_agent_session_from_row(existing)
            connection.execute(
                """
                INSERT INTO conversation_agent_sessions (
                    id, web_session_id, agent_role, agent_id, agentscope_session_id,
                    task_id, event_cursor, active_turn_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)
                """,
                (
                    record_id,
                    web_session_id,
                    agent_role,
                    agent_id,
                    agentscope_session_id,
                    task_id,
                    timestamp,
                    timestamp,
                ),
            )
            row = connection.execute(
                "SELECT * FROM conversation_agent_sessions WHERE id = ?", (record_id,)
            ).fetchone()
        assert row is not None
        return self._conversation_agent_session_from_row(row)

    def get_conversation_agent_session(
        self,
        web_session_id: str,
        *,
        agent_role: str,
        task_id: str | None = None,
    ) -> ConversationAgentSession | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM conversation_agent_sessions
                WHERE web_session_id = ? AND agent_role = ?
                  AND ((task_id IS NULL AND ? IS NULL) OR task_id = ?)
                """,
                (web_session_id, agent_role, task_id, task_id),
            ).fetchone()
        return self._conversation_agent_session_from_row(row) if row is not None else None

    def get_conversation_agent_session_by_agentscope_session(
        self, agentscope_session_id: str
    ) -> ConversationAgentSession | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM conversation_agent_sessions
                WHERE agentscope_session_id = ?
                """,
                (agentscope_session_id,),
            ).fetchone()
        return self._conversation_agent_session_from_row(row) if row is not None else None

    def get_agentscope_reply_turn_id(
        self,
        agentscope_session_id: str,
        reply_id: str,
    ) -> str | None:
        """Return the durable owner Turn for a reply before runtime side effects."""
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT turn_id FROM agentscope_turn_replies
                WHERE agentscope_session_id = ? AND reply_id = ?
                ORDER BY updated_at DESC, rowid DESC LIMIT 1
                """,
                (agentscope_session_id, reply_id),
            ).fetchone()
        return _optional_text(row["turn_id"]) if row is not None else None

    def list_conversation_agent_sessions(
        self, web_session_id: str | None = None
    ) -> list[ConversationAgentSession]:
        with self._connect() as connection:
            if web_session_id is None:
                rows = connection.execute(
                    "SELECT * FROM conversation_agent_sessions ORDER BY created_at, rowid"
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM conversation_agent_sessions
                    WHERE web_session_id = ? ORDER BY created_at, rowid
                    """,
                    (web_session_id,),
                ).fetchall()
        return [self._conversation_agent_session_from_row(row) for row in rows]

    def save_conversation_event_cursor(
        self, agentscope_session_id: str, cursor: str
    ) -> None:
        timestamp = _now()
        with self._connect() as connection:
            result = connection.execute(
                """
                UPDATE conversation_agent_sessions
                SET event_cursor = ?, updated_at = ? WHERE agentscope_session_id = ?
                """,
                (cursor, timestamp, agentscope_session_id),
            )
            if result.rowcount == 0:
                raise KeyError(agentscope_session_id)

    def bind_conversation_agent_session_to_turn(
        self, agentscope_session_id: str, turn_id: str | None
    ) -> ConversationAgentSession:
        timestamp = _now()
        with self._connect() as connection:
            result = connection.execute(
                """
                UPDATE conversation_agent_sessions
                SET active_turn_id = ?, updated_at = ? WHERE agentscope_session_id = ?
                """,
                (turn_id, timestamp, agentscope_session_id),
            )
            if result.rowcount == 0:
                raise KeyError(agentscope_session_id)
            row = connection.execute(
                """
                SELECT * FROM conversation_agent_sessions
                WHERE agentscope_session_id = ?
                """,
                (agentscope_session_id,),
            ).fetchone()
        assert row is not None
        return self._conversation_agent_session_from_row(row)

    def create_task_binding(
        self,
        web_session_id: str,
        *,
        task_id: str,
        task_ref: str,
        navigation_session_id: str,
        domain: str = "navigation",
        navigation_agent_id: str = "navigation-data-agent",
        scope: dict | None = None,
        outbox_payload: dict | None = None,
        outbox_idempotency_key: str | None = None,
        outbox_kind: str = "navigation_start",
    ) -> TaskBindingCreation:
        if not outbox_kind.strip():
            raise ValueError("outbox_kind must not be empty")
        timestamp = _now()
        agent_session_id = f"conversation_agent_session_{uuid4().hex}"
        outbox_id = f"outbox_{uuid4().hex}"
        outbox_key = outbox_idempotency_key or f"navigation_start:{task_id}"
        scope_json = json.dumps(scope or {}, ensure_ascii=False, sort_keys=True)
        payload = dict(outbox_payload or {})
        payload.setdefault("task_ref", task_ref)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_contract_v1(connection, web_session_id)
            existing = connection.execute(
                "SELECT * FROM conversation_task_bindings WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if existing is not None:
                binding = self._task_binding_from_row(existing)
                navigation = connection.execute(
                    """
                    SELECT * FROM conversation_agent_sessions
                    WHERE task_id = ? AND agent_role = 'navigation'
                    """,
                    (task_id,),
                ).fetchone()
                focus = connection.execute(
                    "SELECT * FROM conversation_task_focus WHERE web_session_id = ?",
                    (web_session_id,),
                ).fetchone()
                outbox = connection.execute(
                    "SELECT * FROM runtime_outbox WHERE idempotency_key = ?",
                    (outbox_key,),
                ).fetchone()
                if (
                    binding.web_session_id != web_session_id
                    or binding.task_ref != task_ref
                    or binding.domain != domain
                    or navigation is None
                    or navigation["agentscope_session_id"] != navigation_session_id
                    or focus is None
                    or outbox is None
                ):
                    raise ContractConflictError(
                        "task_binding_conflict",
                        "task_id is already bound with different contract data",
                        current=binding,
                    )
                return TaskBindingCreation(
                    binding=binding,
                    focus=self._task_focus_from_row(focus),
                    navigation_session=self._conversation_agent_session_from_row(navigation),
                    outbox=self._outbox_from_row(outbox),
                    created=False,
                )
            try:
                connection.execute(
                    """
                    INSERT INTO conversation_task_bindings (
                        task_id, web_session_id, task_ref, navigation_session_id,
                        domain, status, slot_state, state_revision, scope_json,
                        latest_public_update, created_at, updated_at, terminal_at
                    ) VALUES (?, ?, ?, ?, ?, 'active', 'open', 0, ?, NULL, ?, ?, NULL)
                    """,
                    (
                        task_id,
                        web_session_id,
                        task_ref,
                        navigation_session_id,
                        domain,
                        scope_json,
                        timestamp,
                        timestamp,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO conversation_agent_sessions (
                        id, web_session_id, agent_role, agent_id, agentscope_session_id,
                        task_id, event_cursor, active_turn_id, created_at, updated_at
                    ) VALUES (?, ?, 'navigation', ?, ?, ?, NULL, NULL, ?, ?)
                    """,
                    (
                        agent_session_id,
                        web_session_id,
                        navigation_agent_id,
                        navigation_session_id,
                        task_id,
                        timestamp,
                        timestamp,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO conversation_task_focus (
                        web_session_id, task_id, generation, updated_at
                    ) VALUES (?, ?, 1, ?)
                    ON CONFLICT(web_session_id) DO UPDATE SET
                        task_id = excluded.task_id,
                        generation = conversation_task_focus.generation + 1,
                        updated_at = excluded.updated_at
                    """,
                    (web_session_id, task_id, timestamp),
                )
                self._insert_outbox(
                    connection,
                    outbox_id=outbox_id,
                    kind=outbox_kind,
                    aggregate_type="task",
                    aggregate_id=task_id,
                    web_session_id=web_session_id,
                    task_id=task_id,
                    turn_id=(
                        str(payload["origin_turn_id"])
                        if isinstance(payload.get("origin_turn_id"), str)
                        else None
                    ),
                    payload=payload,
                    idempotency_key=outbox_key,
                    available_at=timestamp,
                    timestamp=timestamp,
                )
            except sqlite3.IntegrityError as exc:
                open_task = connection.execute(
                    """
                    SELECT * FROM conversation_task_bindings
                    WHERE web_session_id = ? AND slot_state = 'open'
                    """,
                    (web_session_id,),
                ).fetchone()
                if open_task is not None:
                    raise ContractConflictError(
                        "open_task_slot_occupied",
                        "the session already has an open navigation task",
                        current=self._task_binding_from_row(open_task),
                    ) from exc
                raise ContractConflictError(
                    "task_binding_conflict", str(exc)
                ) from exc
            binding_row = connection.execute(
                "SELECT * FROM conversation_task_bindings WHERE task_id = ?", (task_id,)
            ).fetchone()
            focus_row = connection.execute(
                "SELECT * FROM conversation_task_focus WHERE web_session_id = ?",
                (web_session_id,),
            ).fetchone()
            navigation_row = connection.execute(
                "SELECT * FROM conversation_agent_sessions WHERE id = ?", (agent_session_id,)
            ).fetchone()
            outbox_row = connection.execute(
                "SELECT * FROM runtime_outbox WHERE outbox_id = ?", (outbox_id,)
            ).fetchone()
        assert all(row is not None for row in (binding_row, focus_row, navigation_row, outbox_row))
        return TaskBindingCreation(
            binding=self._task_binding_from_row(binding_row),
            focus=self._task_focus_from_row(focus_row),
            navigation_session=self._conversation_agent_session_from_row(navigation_row),
            outbox=self._outbox_from_row(outbox_row),
            created=True,
        )

    def get_task_binding_by_ref(
        self, web_session_id: str, task_ref: str
    ) -> TaskBinding | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM conversation_task_bindings
                WHERE web_session_id = ? AND task_ref = ?
                """,
                (web_session_id, task_ref),
            ).fetchone()
        return self._task_binding_from_row(row) if row is not None else None

    def get_task_binding(self, task_id: str) -> TaskBinding | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM conversation_task_bindings WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        return self._task_binding_from_row(row) if row is not None else None

    def get_focused_task_binding(self, web_session_id: str) -> TaskBinding | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT bindings.*
                FROM conversation_task_focus AS focus
                JOIN conversation_task_bindings AS bindings ON bindings.task_id = focus.task_id
                WHERE focus.web_session_id = ?
                """,
                (web_session_id,),
            ).fetchone()
        return self._task_binding_from_row(row) if row is not None else None

    def get_task_focus(self, web_session_id: str) -> TaskFocus | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM conversation_task_focus WHERE web_session_id = ?",
                (web_session_id,),
            ).fetchone()
        return self._task_focus_from_row(row) if row is not None else None

    def focus_task(self, web_session_id: str, *, task_id: str) -> TaskFocus:
        timestamp = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            binding = connection.execute(
                """
                SELECT 1 FROM conversation_task_bindings
                WHERE web_session_id = ? AND task_id = ?
                """,
                (web_session_id, task_id),
            ).fetchone()
            if binding is None:
                raise KeyError((web_session_id, task_id))
            connection.execute(
                """
                INSERT INTO conversation_task_focus (
                    web_session_id, task_id, generation, updated_at
                ) VALUES (?, ?, 1, ?)
                ON CONFLICT(web_session_id) DO UPDATE SET
                    task_id = excluded.task_id,
                    generation = conversation_task_focus.generation + 1,
                    updated_at = excluded.updated_at
                """,
                (web_session_id, task_id, timestamp),
            )
            row = connection.execute(
                "SELECT * FROM conversation_task_focus WHERE web_session_id = ?",
                (web_session_id,),
            ).fetchone()
        assert row is not None
        return self._task_focus_from_row(row)

    def list_task_bindings(self, web_session_id: str) -> list[TaskBinding]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM conversation_task_bindings
                WHERE web_session_id = ? ORDER BY created_at DESC, rowid DESC
                """,
                (web_session_id,),
            ).fetchall()
        return [self._task_binding_from_row(row) for row in rows]

    def update_task_binding(
        self,
        task_id: str,
        *,
        expected_revision: int,
        status: str,
        slot_state: str | None = None,
        scope: dict | None = None,
        latest_public_update: str | None = None,
    ) -> TaskBinding:
        if slot_state is not None and slot_state not in {"open", "closed"}:
            raise ValueError("slot_state must be open or closed")
        timestamp = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current_row = connection.execute(
                "SELECT * FROM conversation_task_bindings WHERE task_id = ?", (task_id,)
            ).fetchone()
            if current_row is None:
                raise KeyError(task_id)
            current = self._task_binding_from_row(current_row)
            if current.state_revision != expected_revision:
                raise ContractConflictError(
                    "task_revision_mismatch",
                    "task revision does not match",
                    current=current,
                )
            next_slot = slot_state or current.slot_state
            next_scope = current.scope if scope is None else scope
            terminal_at = timestamp if next_slot == "closed" else None
            connection.execute(
                """
                UPDATE conversation_task_bindings
                SET status = ?, slot_state = ?, state_revision = state_revision + 1,
                    scope_json = ?, latest_public_update = ?, updated_at = ?, terminal_at = ?
                WHERE task_id = ? AND state_revision = ?
                """,
                (
                    status,
                    next_slot,
                    json.dumps(next_scope, ensure_ascii=False, sort_keys=True),
                    latest_public_update,
                    timestamp,
                    terminal_at,
                    task_id,
                    expected_revision,
                ),
            )
            row = connection.execute(
                "SELECT * FROM conversation_task_bindings WHERE task_id = ?", (task_id,)
            ).fetchone()
        assert row is not None
        return self._task_binding_from_row(row)

    def mark_task_binding_terminal(
        self,
        task_id: str,
        *,
        expected_revision: int,
        status: str,
        latest_public_update: str | None = None,
    ) -> TaskBinding:
        return self.update_task_binding(
            task_id,
            expected_revision=expected_revision,
            status=status,
            slot_state="closed",
            latest_public_update=latest_public_update,
        )

    def create_turn_run(
        self,
        *,
        run_id: str,
        turn_id: str,
        producer: str,
        status: str = "running",
        task_id: str | None = None,
        parent_run_id: str | None = None,
        agentscope_session_id: str | None = None,
    ) -> TurnRun:
        timestamp = _now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO turn_runs (
                    run_id, turn_id, task_id, producer, parent_run_id,
                    agentscope_session_id, status, created_at, updated_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    run_id,
                    turn_id,
                    task_id,
                    producer,
                    parent_run_id,
                    agentscope_session_id,
                    status,
                    timestamp,
                    timestamp,
                ),
            )
            row = connection.execute(
                "SELECT * FROM turn_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        assert row is not None
        return self._turn_run_from_row(row)

    def update_turn_run(self, run_id: str, *, status: str) -> TurnRun:
        timestamp = _now()
        finished_at = timestamp if status in {"completed", "failed", "interrupted"} else None
        with self._connect() as connection:
            result = connection.execute(
                """
                UPDATE turn_runs SET status = ?, updated_at = ?, finished_at = ?
                WHERE run_id = ?
                """,
                (status, timestamp, finished_at, run_id),
            )
            if result.rowcount == 0:
                raise KeyError(run_id)
            row = connection.execute(
                "SELECT * FROM turn_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        assert row is not None
        return self._turn_run_from_row(row)

    def get_latest_turn_run(
        self,
        turn_id: str,
        *,
        producer: str | None = None,
    ) -> TurnRun | None:
        with self._connect() as connection:
            if producer is None:
                row = connection.execute(
                    """
                    SELECT * FROM turn_runs WHERE turn_id = ?
                    ORDER BY created_at DESC, rowid DESC LIMIT 1
                    """,
                    (turn_id,),
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT * FROM turn_runs WHERE turn_id = ? AND producer = ?
                    ORDER BY created_at DESC, rowid DESC LIMIT 1
                    """,
                    (turn_id, producer),
                ).fetchone()
        return self._turn_run_from_row(row) if row is not None else None

    def initialize_response_authority(
        self, turn_id: str, *, producer: str = "router"
    ) -> ResponseAuthority:
        timestamp = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            turn = connection.execute(
                """
                SELECT sessions.contract_version
                FROM web_turns JOIN sessions ON sessions.id = web_turns.web_session_id
                WHERE web_turns.id = ?
                """,
                (turn_id,),
            ).fetchone()
            if turn is None:
                raise KeyError(turn_id)
            if int(turn["contract_version"]) != 1:
                raise ContractConflictError(
                    "contract_version_mismatch", "response authority requires contract v1"
                )
            existing = connection.execute(
                "SELECT * FROM turn_response_authority WHERE turn_id = ?", (turn_id,)
            ).fetchone()
            if existing is not None:
                record = self._response_authority_from_row(existing)
                if record.producer != producer:
                    raise ContractConflictError(
                        "authority_already_initialized",
                        "response authority is already initialized",
                        current=record,
                    )
                return record
            self._insert_response_authority(
                connection, turn_id=turn_id, producer=producer, timestamp=timestamp
            )
            row = connection.execute(
                "SELECT * FROM turn_response_authority WHERE turn_id = ?", (turn_id,)
            ).fetchone()
        assert row is not None
        return self._response_authority_from_row(row)

    def get_response_authority(self, turn_id: str) -> ResponseAuthority | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM turn_response_authority WHERE turn_id = ?", (turn_id,)
            ).fetchone()
        return self._response_authority_from_row(row) if row is not None else None

    def handover_response_authority(
        self,
        turn_id: str,
        *,
        expected_producer: str,
        expected_generation: int,
        new_producer: str = "navigation",
    ) -> ResponseAuthority:
        return self._transfer_response_authority(
            turn_id,
            expected_producer=expected_producer,
            expected_generation=expected_generation,
            new_producer=new_producer,
        )

    def handover_response_authority_with_outbox(
        self,
        turn_id: str,
        *,
        expected_producer: str,
        expected_generation: int,
        new_producer: str,
        kind: str,
        aggregate_type: str,
        aggregate_id: str,
        payload: dict,
        idempotency_key: str,
        web_session_id: str,
        task_id: str | None,
    ) -> tuple[ResponseAuthority, RuntimeOutboxItem]:
        """Atomically transfer reply ownership and record the accepted wakeup."""
        timestamp = _now()
        outbox_id = f"outbox_{uuid4().hex}"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE turn_response_authority
                SET producer = ?, generation = generation + 1, updated_at = ?
                WHERE turn_id = ? AND producer = ? AND generation = ?
                  AND lease_state = 'open' AND final_message_id IS NULL
                """,
                (
                    new_producer,
                    timestamp,
                    turn_id,
                    expected_producer,
                    expected_generation,
                ),
            )
            authority_row = connection.execute(
                "SELECT * FROM turn_response_authority WHERE turn_id = ?",
                (turn_id,),
            ).fetchone()
            if authority_row is None:
                raise KeyError(turn_id)
            if cursor.rowcount == 0:
                raise ContractConflictError(
                    "response_authority_mismatch",
                    "response authority is stale or already closed",
                    current=self._response_authority_from_row(authority_row),
                )
            self._insert_outbox(
                connection,
                outbox_id=outbox_id,
                kind=kind,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                web_session_id=web_session_id,
                task_id=task_id,
                turn_id=turn_id,
                payload=payload,
                idempotency_key=idempotency_key,
                available_at=timestamp,
                timestamp=timestamp,
            )
            outbox_row = connection.execute(
                "SELECT * FROM runtime_outbox WHERE outbox_id = ?",
                (outbox_id,),
            ).fetchone()
        assert outbox_row is not None
        return (
            self._response_authority_from_row(authority_row),
            self._outbox_from_row(outbox_row),
        )

    def takeover_response_authority(
        self,
        turn_id: str,
        *,
        expected_producer: str,
        expected_generation: int,
    ) -> ResponseAuthority:
        return self._transfer_response_authority(
            turn_id,
            expected_producer=expected_producer,
            expected_generation=expected_generation,
            new_producer="system_controller",
        )

    def _transfer_response_authority(
        self,
        turn_id: str,
        *,
        expected_producer: str,
        expected_generation: int,
        new_producer: str,
    ) -> ResponseAuthority:
        timestamp = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE turn_response_authority
                SET producer = ?, generation = generation + 1, updated_at = ?
                WHERE turn_id = ? AND producer = ? AND generation = ?
                  AND lease_state = 'open' AND final_message_id IS NULL
                """,
                (
                    new_producer,
                    timestamp,
                    turn_id,
                    expected_producer,
                    expected_generation,
                ),
            )
            row = connection.execute(
                "SELECT * FROM turn_response_authority WHERE turn_id = ?", (turn_id,)
            ).fetchone()
            if row is None:
                raise KeyError(turn_id)
            if cursor.rowcount == 0:
                raise ContractConflictError(
                    "response_authority_mismatch",
                    "response authority is stale or already closed",
                    current=self._response_authority_from_row(row),
                )
        return self._response_authority_from_row(row)

    def commit_authorized_final(
        self,
        turn_id: str,
        *,
        producer: str,
        response_generation: int,
        text: str,
        terminal_status: str = "completed",
    ) -> AuthorizedFinalCommit:
        if terminal_status not in {"completed", "failed"}:
            raise ValueError("terminal_status must be completed or failed")
        timestamp = _now()
        safe_text = sanitize_public_reply(text)
        safe_reply_failed = not safe_text
        if safe_reply_failed:
            terminal_status = "failed"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            authority_row = connection.execute(
                "SELECT * FROM turn_response_authority WHERE turn_id = ?", (turn_id,)
            ).fetchone()
            if authority_row is None:
                raise KeyError(turn_id)
            authority = self._response_authority_from_row(authority_row)
            if (
                authority.producer != producer
                or authority.generation != response_generation
                or authority.lease_state != "open"
                or authority.final_message_id is not None
            ):
                raise ContractConflictError(
                    "response_authority_mismatch",
                    "final reply producer is stale or the turn is already closed",
                    current=authority,
                )
            turn = connection.execute(
                "SELECT web_session_id, status FROM web_turns WHERE id = ?", (turn_id,)
            ).fetchone()
            if turn is None:
                raise KeyError(turn_id)
            if safe_reply_failed:
                open_task = connection.execute(
                    """
                    SELECT status FROM conversation_task_bindings
                    WHERE web_session_id = ? AND slot_state = 'open'
                    ORDER BY updated_at DESC, rowid DESC LIMIT 1
                    """,
                    (turn["web_session_id"],),
                ).fetchone()
                safe_text = (
                    _open_task_turn_reply(str(open_task["status"]))
                    if open_task is not None
                    else _SAFE_PUBLIC_REPLY_FAILURE
                )
            if turn["status"] not in {"running", "waiting"}:
                raise ContractConflictError(
                    "turn_not_open", "the turn cannot accept a final reply"
                )
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
                origin_key=f"contract-v1:{turn_id}:{response_generation}:final",
            )
            connection.execute(
                """
                UPDATE web_turns
                SET status = ?, finished_at = ?, final_message_id = ? WHERE id = ?
                """,
                (terminal_status, timestamp, message_id, turn_id),
            )
            connection.execute(
                """
                UPDATE turn_response_authority
                SET lease_state = 'closed', final_message_id = ?, updated_at = ?
                WHERE turn_id = ? AND producer = ? AND generation = ?
                  AND lease_state = 'open' AND final_message_id IS NULL
                """,
                (message_id, timestamp, turn_id, producer, response_generation),
            )
            connection.execute(
                "UPDATE conversation_agent_sessions "
                "SET active_turn_id = NULL, updated_at = ? WHERE active_turn_id = ?",
                (timestamp, turn_id),
            )
            connection.execute(
                "UPDATE agentscope_sessions "
                "SET active_turn_id = NULL, updated_at = ? WHERE active_turn_id = ?",
                (timestamp, turn_id),
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
                origin_key=f"contract-v1:{turn_id}:{response_generation}:{terminal_status}",
            )
            connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (timestamp, turn["web_session_id"]),
            )
        return AuthorizedFinalCommit(
            message=ChatMessageRecord(
                id=message_id,
                session_id=turn["web_session_id"],
                turn_id=turn_id,
                role="assistant",
                content=safe_text,
                created_at=timestamp,
            ),
            events=(final, state),
            terminal_status=terminal_status,
        )

    def create_interaction(
        self,
        web_session_id: str,
        *,
        task_ref: str,
        kind: str,
        blocking: bool,
        risk: str,
        title: str,
        options: list[dict],
        expected_task_revision: int,
        private_payload: dict | None = None,
        summary: str | None = None,
        origin_turn_id: str | None = None,
        expires_at: str | None = None,
        interaction_id: str | None = None,
    ) -> InteractionRecord:
        if not title.strip():
            raise ValueError("interaction title must not be empty")
        option_ids = [item.get("option_id") or item.get("id") for item in options]
        if not options or any(not isinstance(value, str) or not value for value in option_ids):
            raise ValueError("every interaction option requires a non-empty string id")
        if len(set(option_ids)) != len(option_ids):
            raise ValueError("interaction option ids must be unique")
        normalized_options = [
            {
                **{key: value for key, value in item.items() if key != "id"},
                "option_id": option_id,
            }
            for item, option_id in zip(options, option_ids, strict=True)
        ]
        timestamp = _now()
        interaction_id = interaction_id or f"interaction_{uuid4().hex}"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            binding_row = connection.execute(
                """
                SELECT * FROM conversation_task_bindings
                WHERE web_session_id = ? AND task_ref = ?
                """,
                (web_session_id, task_ref),
            ).fetchone()
            if binding_row is None:
                raise KeyError((web_session_id, task_ref))
            binding = self._task_binding_from_row(binding_row)
            existing = connection.execute(
                "SELECT * FROM interactions WHERE interaction_id = ?", (interaction_id,)
            ).fetchone()
            if existing is not None:
                return self._interaction_from_row(existing)
            connection.execute(
                """
                INSERT INTO interactions (
                    interaction_id, web_session_id, task_id, task_ref, origin_turn_id,
                    kind, blocking, risk, title, summary, options_json, status,
                    private_payload_json, revision, expected_task_revision, expires_at, response_json,
                    idempotency_key, created_at, updated_at, resolved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, 1, ?, ?, NULL,
                          NULL, ?, ?, NULL)
                """,
                (
                    interaction_id,
                    web_session_id,
                    binding.task_id,
                    task_ref,
                    origin_turn_id,
                    kind,
                    int(blocking),
                    risk,
                    title.strip(),
                    summary,
                    json.dumps(normalized_options, ensure_ascii=False, sort_keys=True),
                    json.dumps(private_payload or {}, ensure_ascii=False, sort_keys=True),
                    expected_task_revision,
                    expires_at,
                    timestamp,
                    timestamp,
                ),
            )
            row = connection.execute(
                "SELECT * FROM interactions WHERE interaction_id = ?", (interaction_id,)
            ).fetchone()
        assert row is not None
        return self._interaction_from_row(row)

    def get_interaction(self, interaction_id: str) -> InteractionRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM interactions WHERE interaction_id = ?", (interaction_id,)
            ).fetchone()
        return self._interaction_from_row(row) if row is not None else None

    def get_open_interaction(self, web_session_id: str) -> InteractionRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM interactions
                WHERE web_session_id = ? AND status = 'open'
                ORDER BY created_at DESC, rowid DESC LIMIT 1
                """,
                (web_session_id,),
            ).fetchone()
        return self._interaction_from_row(row) if row is not None else None

    def consume_interaction(
        self,
        interaction_id: str,
        *,
        interaction_revision: int,
        expected_task_revision: int,
        idempotency_key: str,
        option_id: str | None = None,
        option_ids: list[str] | None = None,
        navigation_db_path: str | Path | None = None,
    ) -> InteractionConsumption:
        if not idempotency_key:
            raise ValueError("idempotency_key must not be empty")
        selected = list(option_ids or ([] if option_id is None else [option_id]))
        if not selected or len(set(selected)) != len(selected):
            raise ValueError("one or more unique option ids are required")
        timestamp = _now()
        with self._connect() as connection:
            if navigation_db_path is not None:
                connection.execute(
                    "ATTACH DATABASE ? AS navigation_contract",
                    (str(navigation_db_path),),
                )
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM interactions WHERE interaction_id = ?", (interaction_id,)
            ).fetchone()
            if row is None:
                raise KeyError(interaction_id)
            current = self._interaction_from_row(row)
            if current.idempotency_key == idempotency_key and current.status == "resolved":
                return InteractionConsumption(interaction=current, created=False)
            duplicate_key = connection.execute(
                """
                SELECT * FROM interactions
                WHERE idempotency_key = ? AND interaction_id <> ?
                """,
                (idempotency_key, interaction_id),
            ).fetchone()
            if duplicate_key is not None:
                raise ContractConflictError(
                    "interaction_idempotency_conflict",
                    "idempotency key was used for another interaction",
                    current=self._interaction_from_row(duplicate_key),
                )
            if current.status != "open":
                raise ContractConflictError(
                    "interaction_not_open", "interaction is no longer open", current=current
                )
            if current.expires_at is not None and current.expires_at <= timestamp:
                connection.execute(
                    """
                    UPDATE interactions SET status = 'expired', revision = revision + 1,
                        updated_at = ? WHERE interaction_id = ?
                    """,
                    (timestamp, interaction_id),
                )
                expired_row = connection.execute(
                    "SELECT * FROM interactions WHERE interaction_id = ?", (interaction_id,)
                ).fetchone()
                connection.commit()
                raise ContractConflictError(
                    "interaction_expired",
                    "interaction has expired",
                    current=self._interaction_from_row(expired_row),
                )
            if (
                current.revision != interaction_revision
                or current.expected_task_revision != expected_task_revision
            ):
                raise ContractConflictError(
                    "interaction_revision_mismatch",
                    "interaction or task revision does not match",
                    current=current,
                )
            if navigation_db_path is not None:
                task_revision = connection.execute(
                    """
                    SELECT state_revision, created_by_web_session_id
                    FROM navigation_contract.navigation_tasks
                    WHERE task_id = ?
                    """,
                    (current.task_id,),
                ).fetchone()
                if (
                    task_revision is None
                    or task_revision["created_by_web_session_id"] != current.web_session_id
                    or int(task_revision["state_revision"]) != expected_task_revision
                ):
                    raise ContractConflictError(
                        "task_revision_mismatch",
                        "navigation task changed before the interaction was consumed",
                        current=current,
                    )
            allowed = {
                str(option.get("option_id") or option.get("id"))
                for option in current.options
            }
            if any(value not in allowed for value in selected):
                raise ContractConflictError(
                    "invalid_interaction_option",
                    "one or more selected options are not available",
                    current=current,
                )
            if current.kind != "multi_select" and len(selected) != 1:
                raise ContractConflictError(
                    "invalid_interaction_selection_count",
                    "this interaction accepts exactly one option",
                    current=current,
                )
            response = {"option_ids": selected}
            connection.execute(
                """
                UPDATE interactions
                SET status = 'resolved', revision = revision + 1, response_json = ?,
                    idempotency_key = ?, updated_at = ?, resolved_at = ?
                WHERE interaction_id = ? AND status = 'open' AND revision = ?
                """,
                (
                    json.dumps(response, ensure_ascii=False, sort_keys=True),
                    idempotency_key,
                    timestamp,
                    timestamp,
                    interaction_id,
                    interaction_revision,
                ),
            )
            connection.execute(
                """
                UPDATE conversation_task_focus
                SET task_id = ?, generation = generation + 1, updated_at = ?
                WHERE web_session_id = ?
                """,
                (current.task_id, timestamp, current.web_session_id),
            )
            resolved_row = connection.execute(
                "SELECT * FROM interactions WHERE interaction_id = ?", (interaction_id,)
            ).fetchone()
        assert resolved_row is not None
        return InteractionConsumption(
            interaction=self._interaction_from_row(resolved_row), created=True
        )

    def create_interaction_turn(
        self,
        interaction_id: str,
        *,
        content: str,
        invocation_id: str | None = None,
    ) -> TurnSubmission:
        timestamp = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            interaction_row = connection.execute(
                "SELECT * FROM interactions WHERE interaction_id = ?", (interaction_id,)
            ).fetchone()
            if interaction_row is None:
                raise KeyError(interaction_id)
            interaction = self._interaction_from_row(interaction_row)
            if interaction.status != "resolved":
                raise ContractConflictError(
                    "interaction_not_resolved",
                    "only a resolved interaction can create a turn",
                    current=interaction,
                )
            turn_invocation = invocation_id or f"interaction:{interaction_id}:{interaction.revision}"
            existing_turn = connection.execute(
                """
                SELECT id, web_session_id, origin, status, started_at, finished_at,
                       final_message_id FROM web_turns
                WHERE web_session_id = ? AND invocation_id = ?
                """,
                (interaction.web_session_id, turn_invocation),
            ).fetchone()
            if existing_turn is not None:
                message_row = connection.execute(
                    """
                    SELECT id, session_id, turn_id, role, content, created_at
                    FROM messages WHERE turn_id = ? AND role = 'user' LIMIT 1
                    """,
                    (existing_turn["id"],),
                ).fetchone()
                assert message_row is not None
                return TurnSubmission(
                    turn=self._turn_from_row(existing_turn),
                    message=self._message_from_row(message_row),
                    events=(),
                    created=False,
                )
            if connection.execute(
                """
                SELECT 1 FROM web_turns
                WHERE web_session_id = ? AND status IN ('running', 'waiting')
                """,
                (interaction.web_session_id,),
            ).fetchone() is not None:
                raise ContractConflictError(
                    "active_turn_exists", "the session already has an active turn"
                )
            turn = TurnRecord(
                id=f"turn_{uuid4().hex}",
                web_session_id=interaction.web_session_id,
                origin="interaction",
                status="running",
                started_at=timestamp,
            )
            message = ChatMessageRecord(
                id=f"message_{uuid4().hex}",
                session_id=interaction.web_session_id,
                turn_id=turn.id,
                role="user",
                content=content,
                created_at=timestamp,
            )
            connection.execute(
                """
                INSERT INTO web_turns (
                    id, web_session_id, invocation_id, origin, status, started_at,
                    finished_at, final_message_id
                ) VALUES (?, ?, ?, 'interaction', 'running', ?, NULL, NULL)
                """,
                (turn.id, turn.web_session_id, turn_invocation, timestamp),
            )
            connection.execute(
                """
                INSERT INTO messages (id, session_id, turn_id, role, content, created_at)
                VALUES (?, ?, ?, 'user', ?, ?)
                """,
                (message.id, message.session_id, turn.id, content, timestamp),
            )
            self._insert_response_authority(
                connection, turn_id=turn.id, producer="navigation", timestamp=timestamp
            )
            self._insert_outbox(
                connection,
                outbox_id=f"outbox_{uuid4().hex}",
                kind="navigation_resume",
                aggregate_type="interaction",
                aggregate_id=interaction_id,
                web_session_id=interaction.web_session_id,
                task_id=interaction.task_id,
                turn_id=turn.id,
                payload={
                    "interaction_id": interaction_id,
                    "option_ids": list((interaction.response or {}).get("option_ids") or []),
                },
                idempotency_key=f"navigation_resume:{interaction_id}:{interaction.revision}",
                available_at=timestamp,
                timestamp=timestamp,
            )
            event = self._insert_timeline_event(
                connection,
                session_id=turn.web_session_id,
                turn_id=turn.id,
                event={
                    "type": "turn_start",
                    "timestamp": timestamp,
                    "payload": {"status": "running", "started_at": timestamp},
                },
                created_at=timestamp,
                origin_key=f"interaction:{interaction_id}:turn-start",
            )
            connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (timestamp, turn.web_session_id),
            )
        return TurnSubmission(turn=turn, message=message, events=(event,), created=True)

    def enqueue_outbox(
        self,
        *,
        kind: str,
        aggregate_type: str,
        aggregate_id: str,
        payload: dict,
        idempotency_key: str,
        web_session_id: str | None = None,
        task_id: str | None = None,
        turn_id: str | None = None,
        available_at: str | None = None,
    ) -> RuntimeOutboxItem:
        if not idempotency_key:
            raise ValueError("idempotency_key must not be empty")
        timestamp = _now()
        available_at = available_at or timestamp
        outbox_id = f"outbox_{uuid4().hex}"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM runtime_outbox WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                return self._outbox_from_row(existing)
            self._insert_outbox(
                connection,
                outbox_id=outbox_id,
                kind=kind,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                web_session_id=web_session_id,
                task_id=task_id,
                turn_id=turn_id,
                payload=payload,
                idempotency_key=idempotency_key,
                available_at=available_at,
                timestamp=timestamp,
            )
            row = connection.execute(
                "SELECT * FROM runtime_outbox WHERE outbox_id = ?", (outbox_id,)
            ).fetchone()
        assert row is not None
        return self._outbox_from_row(row)

    def record_outbox_receipt(
        self,
        *,
        kind: str,
        aggregate_type: str,
        aggregate_id: str,
        payload: dict,
        idempotency_key: str,
        web_session_id: str | None = None,
        task_id: str | None = None,
    ) -> RuntimeOutboxItem:
        """Atomically record one completed, idempotent transport receipt."""

        if not kind.strip() or not idempotency_key.strip():
            raise ValueError("receipt kind and idempotency key must not be empty")
        timestamp = _now()
        outbox_id = f"outbox_{uuid4().hex}"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM runtime_outbox WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                receipt = self._outbox_from_row(existing)
                if (
                    receipt.kind != kind
                    or receipt.aggregate_type != aggregate_type
                    or receipt.aggregate_id != aggregate_id
                    or receipt.web_session_id != web_session_id
                    or receipt.task_id != task_id
                    or receipt.turn_id is not None
                    or receipt.status != "completed"
                ):
                    raise ContractConflictError(
                        "outbox_receipt_conflict",
                        "idempotency key is already used by another outbox record",
                        current=receipt,
                    )
                return receipt
            self._insert_outbox(
                connection,
                outbox_id=outbox_id,
                kind=kind,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                web_session_id=web_session_id,
                task_id=task_id,
                turn_id=None,
                payload=payload,
                idempotency_key=idempotency_key,
                available_at=timestamp,
                timestamp=timestamp,
            )
            connection.execute(
                """
                UPDATE runtime_outbox
                SET status = 'completed', completed_at = ?, updated_at = ?
                WHERE outbox_id = ?
                """,
                (timestamp, timestamp, outbox_id),
            )
            row = connection.execute(
                "SELECT * FROM runtime_outbox WHERE outbox_id = ?",
                (outbox_id,),
            ).fetchone()
        assert row is not None
        return self._outbox_from_row(row)

    def claim_outbox(
        self,
        *,
        worker_id: str,
        kinds: list[str] | None = None,
        limit: int = 1,
        lease_seconds: int = 60,
    ) -> list[RuntimeOutboxItem]:
        if limit < 1:
            return []
        timestamp = _now()
        lease_expires_at = (datetime.now(UTC) + timedelta(seconds=lease_seconds)).isoformat(
            timespec="milliseconds"
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            parameters: list[object] = [timestamp, timestamp]
            kind_clause = ""
            if kinds:
                placeholders = ", ".join("?" for _ in kinds)
                kind_clause = f" AND kind IN ({placeholders})"
                parameters.extend(kinds)
            parameters.append(limit)
            rows = connection.execute(
                f"""
                SELECT outbox_id FROM runtime_outbox
                WHERE available_at <= ?
                  AND (status = 'pending' OR (status = 'claimed' AND lease_expires_at <= ?))
                  {kind_clause}
                ORDER BY available_at ASC, created_at ASC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
            ids = [row["outbox_id"] for row in rows]
            for outbox_id in ids:
                connection.execute(
                    """
                    UPDATE runtime_outbox
                    SET status = 'claimed', claimed_by = ?, lease_expires_at = ?,
                        attempts = attempts + 1, updated_at = ?
                    WHERE outbox_id = ?
                    """,
                    (worker_id, lease_expires_at, timestamp, outbox_id),
                )
            claimed = [
                connection.execute(
                    "SELECT * FROM runtime_outbox WHERE outbox_id = ?", (outbox_id,)
                ).fetchone()
                for outbox_id in ids
            ]
        return [self._outbox_from_row(row) for row in claimed if row is not None]

    def claim_outbox_item(
        self,
        outbox_id: str,
        *,
        worker_id: str,
        lease_seconds: int = 60,
    ) -> RuntimeOutboxItem:
        timestamp = _now()
        lease_expires_at = (datetime.now(UTC) + timedelta(seconds=lease_seconds)).isoformat(
            timespec="milliseconds"
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT * FROM runtime_outbox WHERE outbox_id = ?", (outbox_id,)
            ).fetchone()
            if current is None:
                raise KeyError(outbox_id)
            if current["status"] == "completed":
                return self._outbox_from_row(current)
            if current["status"] == "claimed" and current["lease_expires_at"] > timestamp:
                raise ContractConflictError(
                    "outbox_claim_mismatch",
                    "outbox item is already claimed",
                    current=self._outbox_from_row(current),
                )
            connection.execute(
                """
                UPDATE runtime_outbox
                SET status = 'claimed', claimed_by = ?, lease_expires_at = ?,
                    attempts = attempts + 1, updated_at = ?
                WHERE outbox_id = ?
                """,
                (worker_id, lease_expires_at, timestamp, outbox_id),
            )
            row = connection.execute(
                "SELECT * FROM runtime_outbox WHERE outbox_id = ?", (outbox_id,)
            ).fetchone()
        assert row is not None
        return self._outbox_from_row(row)

    def complete_outbox(
        self,
        outbox_id: str,
        *,
        worker_id: str,
        success: bool = True,
        error: str | None = None,
        retry_at: str | None = None,
    ) -> RuntimeOutboxItem:
        timestamp = _now()
        if success:
            status = "completed"
            completed_at = timestamp
            claimed_by: str | None = None
            lease_expires_at: str | None = None
        elif retry_at is not None:
            status = "pending"
            completed_at = None
            claimed_by = None
            lease_expires_at = None
        else:
            status = "failed"
            completed_at = timestamp
            claimed_by = None
            lease_expires_at = None
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT * FROM runtime_outbox WHERE outbox_id = ?", (outbox_id,)
            ).fetchone()
            if current is None:
                raise KeyError(outbox_id)
            if current["status"] == "completed":
                return self._outbox_from_row(current)
            if current["status"] != "claimed" or current["claimed_by"] != worker_id:
                raise ContractConflictError(
                    "outbox_claim_mismatch",
                    "outbox item is not claimed by this worker",
                    current=self._outbox_from_row(current),
                )
            connection.execute(
                """
                UPDATE runtime_outbox
                SET status = ?, available_at = COALESCE(?, available_at), claimed_by = ?,
                    lease_expires_at = ?, last_error = ?, updated_at = ?, completed_at = ?
                WHERE outbox_id = ?
                """,
                (
                    status,
                    retry_at,
                    claimed_by,
                    lease_expires_at,
                    error,
                    timestamp,
                    completed_at,
                    outbox_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM runtime_outbox WHERE outbox_id = ?", (outbox_id,)
            ).fetchone()
        assert row is not None
        return self._outbox_from_row(row)

    def get_outbox(self, outbox_id: str) -> RuntimeOutboxItem | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM runtime_outbox WHERE outbox_id = ?", (outbox_id,)
            ).fetchone()
        return self._outbox_from_row(row) if row is not None else None

    def get_outbox_by_idempotency_key(self, idempotency_key: str) -> RuntimeOutboxItem | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM runtime_outbox WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        return self._outbox_from_row(row) if row is not None else None

    def list_explicit_linked_fix_recoveries(
        self,
        *,
        limit: int = 20,
    ) -> list[RuntimeOutboxItem]:
        """List automatic linked-Fix creations whose turnless wake is unfinished."""

        if limit < 1:
            return []
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT creation.*
                FROM runtime_outbox AS creation
                JOIN conversation_task_bindings AS bindings
                  ON bindings.task_id = creation.task_id
                WHERE creation.idempotency_key
                          GLOB 'navigation_auto_linked_fix:*'
                  AND creation.kind IN (
                      'navigation_start',
                      'navigation_explicit_linked_fix_create'
                  )
                  AND bindings.slot_state = 'open'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM runtime_outbox AS wake
                      WHERE wake.kind =
                                'navigation_explicit_linked_fix_wake'
                        AND wake.task_id = creation.task_id
                        AND wake.status = 'completed'
                  )
                ORDER BY creation.created_at, creation.outbox_id
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._outbox_from_row(row) for row in rows]

    def transfer_waiting_reply_to_turn(
        self,
        *,
        agentscope_session_id: str,
        reply_id: str,
        turn_id: str,
    ) -> None:
        """Move the paused specialist reply lease onto an Interaction Turn."""
        timestamp = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            target = connection.execute(
                """
                SELECT turns.status, turns.origin, authority.producer, authority.lease_state
                FROM web_turns AS turns
                JOIN turn_response_authority AS authority ON authority.turn_id = turns.id
                WHERE turns.id = ?
                """,
                (turn_id,),
            ).fetchone()
            if (
                target is None
                or target["origin"] != "interaction"
                or target["status"] not in {"running", "waiting"}
                or target["producer"] != "navigation"
                or target["lease_state"] != "open"
            ):
                raise ContractConflictError(
                    "interaction_turn_not_open",
                    "interaction turn cannot receive the specialist reply",
                )
            cursor = connection.execute(
                """
                UPDATE agentscope_turn_replies
                SET turn_id = ?, status = 'pending', summary_text = NULL, updated_at = ?
                WHERE agentscope_session_id = ? AND reply_id = ?
                  AND status IN ('waiting', 'ended', 'failed', 'pending', 'running')
                """,
                (turn_id, timestamp, agentscope_session_id, reply_id),
            )
            if cursor.rowcount != 1:
                raise ContractConflictError(
                    "interaction_reply_unavailable",
                    "the specialist reply can no longer be resumed",
                )

    def acquire_resource_lease(
        self,
        resource_key: str,
        *,
        owner_id: str,
        kind: str,
        lease_seconds: int,
        task_id: str | None = None,
        run_id: str | None = None,
    ) -> ResourceLease:
        timestamp = _now()
        expires_at = (datetime.now(UTC) + timedelta(seconds=lease_seconds)).isoformat(
            timespec="milliseconds"
        )
        lease_id = f"resource_lease_{uuid4().hex}"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM runtime_resource_leases WHERE resource_key = ?",
                (resource_key,),
            ).fetchone()
            if existing is not None and existing["expires_at"] <= timestamp:
                connection.execute(
                    "DELETE FROM runtime_resource_leases WHERE lease_id = ?",
                    (existing["lease_id"],),
                )
                existing = None
            if existing is not None:
                lease = self._resource_lease_from_row(existing)
                if lease.owner_id == owner_id:
                    connection.execute(
                        """
                        UPDATE runtime_resource_leases
                        SET expires_at = ?, updated_at = ? WHERE lease_id = ?
                        """,
                        (expires_at, timestamp, lease.lease_id),
                    )
                    renewed = connection.execute(
                        "SELECT * FROM runtime_resource_leases WHERE lease_id = ?",
                        (lease.lease_id,),
                    ).fetchone()
                    return self._resource_lease_from_row(renewed)
                raise ContractConflictError(
                    "resource_lease_unavailable",
                    "resource is leased by another owner",
                    current=lease,
                )
            connection.execute(
                """
                INSERT INTO runtime_resource_leases (
                    lease_id, resource_key, owner_id, task_id, run_id, kind,
                    expires_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    lease_id,
                    resource_key,
                    owner_id,
                    task_id,
                    run_id,
                    kind,
                    expires_at,
                    timestamp,
                    timestamp,
                ),
            )
            row = connection.execute(
                "SELECT * FROM runtime_resource_leases WHERE lease_id = ?", (lease_id,)
            ).fetchone()
        assert row is not None
        return self._resource_lease_from_row(row)

    def release_resource_lease(self, lease_id: str, *, owner_id: str) -> bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT * FROM runtime_resource_leases WHERE lease_id = ?", (lease_id,)
            ).fetchone()
            if current is None:
                return False
            if current["owner_id"] != owner_id:
                raise ContractConflictError(
                    "resource_lease_owner_mismatch",
                    "resource lease is owned by another worker",
                    current=self._resource_lease_from_row(current),
                )
            connection.execute(
                "DELETE FROM runtime_resource_leases WHERE lease_id = ?", (lease_id,)
            )
        return True

    @staticmethod
    def _insert_response_authority(
        connection: sqlite3.Connection,
        *,
        turn_id: str,
        producer: str,
        timestamp: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO turn_response_authority (
                turn_id, producer, generation, lease_state, final_message_id,
                created_at, updated_at
            ) VALUES (?, ?, 1, 'open', NULL, ?, ?)
            """,
            (turn_id, producer, timestamp, timestamp),
        )

    @staticmethod
    def _insert_outbox(
        connection: sqlite3.Connection,
        *,
        outbox_id: str,
        kind: str,
        aggregate_type: str,
        aggregate_id: str,
        web_session_id: str | None,
        task_id: str | None,
        turn_id: str | None,
        payload: dict,
        idempotency_key: str,
        available_at: str,
        timestamp: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO runtime_outbox (
                outbox_id, kind, aggregate_type, aggregate_id, web_session_id,
                task_id, turn_id, payload_json, status, idempotency_key,
                available_at, claimed_by, lease_expires_at, attempts, last_error,
                created_at, updated_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, NULL, NULL, 0,
                      NULL, ?, ?, NULL)
            """,
            (
                outbox_id,
                kind,
                aggregate_type,
                aggregate_id,
                web_session_id,
                task_id,
                turn_id,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                idempotency_key,
                available_at,
                timestamp,
                timestamp,
            ),
        )

    @staticmethod
    def _require_contract_v1(
        connection: sqlite3.Connection, web_session_id: str
    ) -> None:
        row = connection.execute(
            "SELECT contract_version FROM sessions WHERE id = ?", (web_session_id,)
        ).fetchone()
        if row is None:
            raise KeyError(web_session_id)
        if int(row["contract_version"]) != 1:
            raise ContractConflictError(
                "contract_version_mismatch", "operation requires contract v1"
            )

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
            contract_version=int(row["contract_version"]),
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

    @staticmethod
    def _conversation_agent_session_from_row(
        row: sqlite3.Row,
    ) -> ConversationAgentSession:
        return ConversationAgentSession(
            id=row["id"],
            web_session_id=row["web_session_id"],
            agent_role=row["agent_role"],
            agent_id=row["agent_id"],
            agentscope_session_id=row["agentscope_session_id"],
            task_id=row["task_id"],
            event_cursor=row["event_cursor"],
            active_turn_id=row["active_turn_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _task_binding_from_row(row: sqlite3.Row) -> TaskBinding:
        try:
            scope = json.loads(row["scope_json"])
        except (TypeError, json.JSONDecodeError):
            scope = {}
        return TaskBinding(
            task_id=row["task_id"],
            web_session_id=row["web_session_id"],
            task_ref=row["task_ref"],
            navigation_session_id=row["navigation_session_id"],
            domain=row["domain"],
            status=row["status"],
            slot_state=row["slot_state"],
            state_revision=int(row["state_revision"]),
            scope=scope if isinstance(scope, dict) else {},
            latest_public_update=row["latest_public_update"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            terminal_at=row["terminal_at"],
        )

    @staticmethod
    def _task_focus_from_row(row: sqlite3.Row) -> TaskFocus:
        return TaskFocus(
            web_session_id=row["web_session_id"],
            task_id=row["task_id"],
            generation=int(row["generation"]),
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _turn_run_from_row(row: sqlite3.Row) -> TurnRun:
        return TurnRun(
            run_id=row["run_id"],
            turn_id=row["turn_id"],
            task_id=row["task_id"],
            producer=row["producer"],
            parent_run_id=row["parent_run_id"],
            agentscope_session_id=row["agentscope_session_id"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            finished_at=row["finished_at"],
        )

    @staticmethod
    def _response_authority_from_row(row: sqlite3.Row) -> ResponseAuthority:
        return ResponseAuthority(
            turn_id=row["turn_id"],
            producer=row["producer"],
            generation=int(row["generation"]),
            lease_state=row["lease_state"],
            final_message_id=row["final_message_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _interaction_from_row(row: sqlite3.Row) -> InteractionRecord:
        try:
            options = json.loads(row["options_json"])
        except (TypeError, json.JSONDecodeError):
            options = []
        try:
            response = json.loads(row["response_json"]) if row["response_json"] else None
        except (TypeError, json.JSONDecodeError):
            response = None
        try:
            private_payload = json.loads(row["private_payload_json"])
        except (TypeError, json.JSONDecodeError):
            private_payload = {}
        return InteractionRecord(
            interaction_id=row["interaction_id"],
            web_session_id=row["web_session_id"],
            task_id=row["task_id"],
            task_ref=row["task_ref"],
            origin_turn_id=row["origin_turn_id"],
            kind=row["kind"],
            blocking=bool(row["blocking"]),
            risk=row["risk"],
            title=row["title"],
            summary=row["summary"],
            options=tuple(options if isinstance(options, list) else []),
            private_payload=private_payload if isinstance(private_payload, dict) else {},
            status=row["status"],
            revision=int(row["revision"]),
            expected_task_revision=int(row["expected_task_revision"]),
            expires_at=row["expires_at"],
            response=response if isinstance(response, dict) else None,
            idempotency_key=row["idempotency_key"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            resolved_at=row["resolved_at"],
        )

    @staticmethod
    def _outbox_from_row(row: sqlite3.Row) -> RuntimeOutboxItem:
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError):
            payload = {}
        return RuntimeOutboxItem(
            outbox_id=row["outbox_id"],
            kind=row["kind"],
            aggregate_type=row["aggregate_type"],
            aggregate_id=row["aggregate_id"],
            web_session_id=row["web_session_id"],
            task_id=row["task_id"],
            turn_id=row["turn_id"],
            payload=payload if isinstance(payload, dict) else {},
            status=row["status"],
            idempotency_key=row["idempotency_key"],
            available_at=row["available_at"],
            claimed_by=row["claimed_by"],
            lease_expires_at=row["lease_expires_at"],
            attempts=int(row["attempts"]),
            last_error=row["last_error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            completed_at=row["completed_at"],
        )

    @staticmethod
    def _resource_lease_from_row(row: sqlite3.Row) -> ResourceLease:
        return ResourceLease(
            lease_id=row["lease_id"],
            resource_key=row["resource_key"],
            owner_id=row["owner_id"],
            task_id=row["task_id"],
            run_id=row["run_id"],
            kind=row["kind"],
            expires_at=row["expires_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
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

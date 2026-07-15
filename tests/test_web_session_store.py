from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
import base64
import json
import sqlite3
from threading import Event

import pytest

from vla_data_juicer_agents.web import schemas as web_schemas
from vla_data_juicer_agents.web.schemas import (
    CreateTurnRequest,
    SessionRecord,
    generate_session_title,
)
from vla_data_juicer_agents.web.session_store import WebSessionStore


SCHEMA_GENERATION = "agentscope-native-events-v1"


def _digest(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def _session_cursor(row_id: object) -> str:
    payload = json.dumps(
        ["2026-06-26T10:00:00+08:00", row_id],
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _create_legacy_web_schema(db_path: Path) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE sessions (
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
            INSERT INTO sessions (id, title, status, created_at, updated_at)
            VALUES ('old-session', 'old', 'historical', 'before', 'before')
            """
        )
        connection.execute("CREATE TABLE sync_data (name TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("INSERT INTO sync_data VALUES ('clip', 'preserve-me')")


def test_generate_session_title_uses_first_30_chars():
    title = generate_session_title("处理 20270605 的室外导航数据，并进行 dry-run 验证")

    assert title == "处理 20270605 的室外导航数据，并进行 dry-ru"


def test_generate_session_title_bounds_long_ascii_token():
    title = generate_session_title("a" * 5000)

    assert len(title) == 30
    assert title == "a" * 30


def test_turn_request_rejects_empty_message():
    with pytest.raises(ValueError, match="message must not be empty"):
        CreateTurnRequest(message="   ")


def test_fresh_store_creates_public_event_schema(tmp_path: Path):
    db_path = tmp_path / "sessions.sqlite"

    WebSessionStore(db_path)

    with sqlite3.connect(db_path) as connection:
        generation = connection.execute(
            "SELECT generation FROM web_schema WHERE singleton = 1"
        ).fetchone()
        session_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(sessions)").fetchall()
        }
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }

    assert generation == (SCHEMA_GENERATION,)
    assert "status" not in session_columns
    assert {
        "public_events",
        "public_tool_runs",
        "session_execution_boundaries",
        "tool_execution_provenance",
    } <= tables


def test_schema_generation_check_holds_write_lock_against_concurrent_initializer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    db_path = tmp_path / "sessions.sqlite"
    original = WebSessionStore._schema_generation
    contender_was_locked: list[bool] = []

    def probe_generation(connection: sqlite3.Connection) -> str | None:
        contender = sqlite3.connect(db_path, timeout=0)
        try:
            try:
                contender.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                assert "locked" in str(exc)
                contender_was_locked.append(True)
            else:
                contender_was_locked.append(False)
                contender.rollback()
        finally:
            contender.close()
        return original(connection)

    monkeypatch.setattr(WebSessionStore, "_schema_generation", staticmethod(probe_generation))

    WebSessionStore(db_path)

    assert contender_was_locked == [True]


def test_old_web_schema_is_reset_without_touching_artifacts_or_other_tables(tmp_path: Path):
    db_path = tmp_path / "sessions.sqlite"
    _create_legacy_web_schema(db_path)
    artifact = tmp_path / "VLADatasets" / "clip" / "sync_data" / "frame.jpg"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"frame")

    store = WebSessionStore(db_path)

    with sqlite3.connect(db_path) as connection:
        preserved = connection.execute("SELECT name, value FROM sync_data").fetchall()
        generation = connection.execute(
            "SELECT generation FROM web_schema WHERE singleton = 1"
        ).fetchone()
    assert store.list_sessions() == []
    assert artifact.read_bytes() == b"frame"
    assert preserved == [("clip", "preserve-me")]
    assert generation == (SCHEMA_GENERATION,)


def test_session_record_has_no_legacy_status():
    record = SessionRecord(
        id="session_1",
        title="处理 20270605 的室外导航数据",
        created_at="2026-06-26T10:00:00+08:00",
        updated_at="2026-06-26T10:01:00+08:00",
    )

    assert record.model_dump() == {
        "id": "session_1",
        "title": "处理 20270605 的室外导航数据",
        "created_at": "2026-06-26T10:00:00+08:00",
        "updated_at": "2026-06-26T10:01:00+08:00",
    }


def test_store_creates_session_and_lists_recent(tmp_path: Path):
    store = WebSessionStore(tmp_path / "sessions.sqlite")

    session = store.create_session(title="处理 20270605 的室外导航数据")

    assert store.list_sessions() == [session]


def test_store_session_cursor_paginates_stably_without_duplicates(tmp_path: Path):
    store = WebSessionStore(tmp_path / "session-pagination.sqlite")
    created = [store.create_session(title=f"session {index:02d}") for index in range(27)]

    seen = []
    cursor = None
    while True:
        page, cursor = store.list_sessions_page(limit=7, cursor=cursor)
        seen.extend(page)
        if cursor is None:
            break

    assert [session.id for session in seen] == [session.id for session in reversed(created)]
    assert len({session.id for session in seen}) == 27
    with pytest.raises(ValueError, match="session cursor"):
        store.list_sessions_page(limit=7, cursor="not-a-valid-cursor")


@pytest.mark.parametrize("limit", [0, 101])
def test_store_session_page_rejects_limit_outside_public_bounds(
    tmp_path: Path,
    limit: int,
) -> None:
    store = WebSessionStore(tmp_path / "session-pagination-limit.sqlite")

    with pytest.raises(ValueError, match="session page limit"):
        store.list_sessions_page(limit=limit)


@pytest.mark.parametrize(
    "row_id",
    [False, True, 0, -1, 2**63],
    ids=["false", "true", "zero", "negative", "above-sqlite-int64"],
)
def test_store_session_cursor_rejects_invalid_row_id(
    tmp_path: Path,
    row_id: object,
) -> None:
    store = WebSessionStore(tmp_path / "session-pagination-cursor.sqlite")

    with pytest.raises(ValueError, match="invalid session cursor"):
        store.list_sessions_page(limit=7, cursor=_session_cursor(row_id))


def test_store_persists_transcript(tmp_path: Path):
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    session = store.create_session(title="处理 20270605 的室外导航数据")

    user = store.append_message(session.id, role="user", content="处理 20270605")
    assistant = store.append_message(session.id, role="assistant", content="好的，我开始处理。")
    detail = store.get_session(session.id)

    assert detail is not None
    assert [message.id for message in detail.messages] == [user.id, assistant.id]
    assert [message.content for message in detail.messages] == ["处理 20270605", "好的，我开始处理。"]
    assert detail.events == []
    assert detail.tool_runs == []
    assert detail.last_sequence == 0


def test_public_event_dedupe_is_idempotent_and_sequence_is_monotonic(tmp_path: Path):
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    session = store.create_session(title="public events")
    reply_start = {"type": "reply_start", "payload": {"reply_id": "reply-1"}}
    first_key = _digest("as:s1:r1:0")

    first = store.append_public_event(session.id, first_key, reply_start)
    duplicate = store.append_public_event(session.id, first_key, reply_start)
    second = store.append_public_event(
        session.id,
        _digest("as:s1:r1:1"),
        {"type": "text_delta", "payload": {"delta": "hello"}},
    )

    assert first.id == duplicate.id
    assert first.sequence == duplicate.sequence == 1
    assert second.sequence == 2
    assert store.list_public_events(session.id) == [first, second]
    assert store.list_public_events(session.id, after_sequence=1) == [second]
    assert "as:s1" not in first.dedupe_key


def test_public_event_model_rejects_non_sha256_dedupe_key():
    with pytest.raises(ValueError, match="dedupe_key"):
        web_schemas.PublicEventRecord(
            id="event-1",
            session_id="session-1",
            sequence=1,
            dedupe_key="as:s1:r1:0",
            event={"type": "reply_start"},
            created_at="2026-06-26T10:00:00.000+00:00",
        )


def test_public_event_sequence_allocation_is_atomic(tmp_path: Path):
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    session = store.create_session(title="concurrent events")

    def append(index: int):
        return store.append_public_event(
            session.id,
            _digest(f"event-{index}"),
            {"type": "text_delta", "payload": {"index": index}},
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        records = list(executor.map(append, range(16)))

    assert sorted(record.sequence for record in records) == list(range(1, 17))
    assert [record.sequence for record in store.list_public_events(session.id)] == list(range(1, 17))


def test_finish_tool_run_is_first_terminal_wins(tmp_path: Path):
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    session = store.create_session(title="tool ledger")
    started_at = "2026-06-26T10:00:00.000+00:00"

    running = store.start_tool_run(session.id, "call-1", "extract", started_at)
    failed = store.finish_tool_run(
        session.id,
        "call-1",
        status="failure",
        summary="boom",
        error_type="RuntimeError",
    )
    late = store.finish_tool_run(
        session.id,
        "call-1",
        status="success",
        summary="late",
    )

    assert running.status == "running"
    assert failed is not None
    assert failed.status == "failure"
    assert failed.summary == "boom"
    assert failed.error_type == "RuntimeError"
    assert failed.finished_at is not None
    assert late is None
    assert store.get_session(session.id).tool_runs == [failed]


def test_finish_tool_run_with_terminal_event_rolls_back_on_event_encoding_failure(
    tmp_path: Path,
):
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    session = store.create_session(title="atomic tool terminal")
    started_at = "2026-06-26T10:00:00.000+00:00"
    store.start_tool_run(session.id, "call-atomic", "extract", started_at)

    def invalid_terminal_event(tool_run):
        assert tool_run.status == "success"
        return (
            _digest("tool-terminal:call-atomic:success"),
            {"type": "CUSTOM", "value": {"not_json": object()}},
        )

    with pytest.raises(TypeError, match="JSON serializable"):
        store.finish_tool_run_with_terminal_event(
            session.id,
            "call-atomic",
            status="success",
            summary="done",
            terminal_event_factory=invalid_terminal_event,
        )

    detail = store.get_session(session.id)
    assert detail is not None
    assert [(row.tool_call_id, row.status) for row in detail.tool_runs] == [
        ("call-atomic", "running")
    ]
    assert detail.events == []


def test_finish_tool_run_with_terminal_event_has_one_concurrent_winner(
    tmp_path: Path,
):
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    session = store.create_session(title="concurrent tool terminal")
    started_at = "2026-06-26T10:00:00.000+00:00"
    store.start_tool_run(session.id, "call-race", "extract", started_at)

    def finish(status: str):
        return store.finish_tool_run_with_terminal_event(
            session.id,
            "call-race",
            status=status,
            summary=f"{status} outcome",
            error_type="RuntimeError" if status == "failure" else None,
            terminal_event_factory=lambda tool_run: (
                _digest(f"tool-terminal:call-race:{status}"),
                {
                    "type": "CUSTOM",
                    "name": "datapilot_tool_terminal",
                    "value": {
                        "tool_call_id": tool_run.tool_call_id,
                        "status": tool_run.status,
                        "summary": tool_run.summary,
                        "error_type": tool_run.error_type,
                    },
                },
            ),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(finish, ("success", "failure")))

    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    winning_run, winning_event = winners[0]
    detail = store.get_session(session.id)
    assert detail is not None
    assert detail.tool_runs == [winning_run]
    assert detail.events == [winning_event]
    assert winning_event.event["value"]["status"] == winning_run.status
    assert winning_event.event["value"]["summary"] == winning_run.summary
    assert store.finish_tool_run_with_terminal_event(
        session.id,
        "call-race",
        status="success" if winning_run.status == "failure" else "failure",
        summary="late outcome",
        terminal_event_factory=lambda _tool_run: pytest.fail(
            "late outcome must not build another terminal event"
        ),
    ) is None
    assert store.get_session(session.id) == detail


def test_stop_open_tool_runs_only_stops_running_rows(tmp_path: Path):
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    session = store.create_session(title="stop tools")
    started_at = "2026-06-26T10:00:00.000+00:00"
    store.start_tool_run(session.id, "call-1", "extract", started_at)
    store.start_tool_run(session.id, "call-2", "sync", started_at)
    success = store.finish_tool_run(session.id, "call-2", status="success", summary="done")

    stopped = store.stop_open_tool_runs(session.id)

    assert success is not None
    assert [record.tool_call_id for record in stopped] == ["call-1"]
    assert stopped[0].status == "stopped"
    assert stopped[0].finished_at is not None
    assert store.stop_open_tool_runs(session.id) == []
    assert [record.status for record in store.get_session(session.id).tool_runs] == [
        "stopped",
        "success",
    ]


def test_execution_stop_boundary_survives_restart_and_new_generation_clears_it(
    tmp_path: Path,
):
    db_path = tmp_path / "sessions.sqlite"
    store = WebSessionStore(db_path)
    session = store.create_session(title="generation boundary")

    assert store.begin_execution_generation(session.id) == 1
    assert store.execution_generation_is_stopped(session.id) is False
    stopped, events = store.stop_open_tool_runs_with_terminal_events(
        session.id,
        lambda _row: pytest.fail("no running rows"),
    )
    assert stopped == []
    assert events == []
    assert WebSessionStore(db_path).execution_generation_is_stopped(session.id) is True

    assert store.begin_execution_generation(session.id) == 2
    assert WebSessionStore(db_path).execution_generation_is_stopped(session.id) is False

    store.delete_session(session.id)
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM session_execution_boundaries"
        ).fetchone() == (0,)


def test_pending_stop_is_durable_retryable_and_blocks_generation_and_tool_outcome(
    tmp_path: Path,
) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    session = store.create_session("durable pending stop")
    generation = store.begin_execution_generation(session.id)
    store.start_tool_run(session.id, "call-1", "extract", "2026-07-15T00:00:00Z")

    first = store.begin_or_resume_stop_request(session.id)
    restarted = WebSessionStore(store.db_path)
    retry = restarted.begin_or_resume_stop_request(session.id)

    assert first == retry
    assert first.generation == generation
    assert restarted.stop_request_is_pending(session.id, generation) is True
    with pytest.raises(RuntimeError, match="stop request is pending"):
        restarted.begin_execution_generation(session.id)
    assert restarted.finish_tool_run(
        session.id,
        "call-1",
        status="success",
    ) is None
    detail = restarted.get_session(session.id)
    assert detail is not None
    assert detail.tool_runs[0].status == "running"


def test_run_admission_ticket_and_delete_fence_are_mutually_exclusive(
    tmp_path: Path,
) -> None:
    first = WebSessionStore(tmp_path / "admission-delete.sqlite")
    session = first.create_session("admission/delete exclusion")
    second = WebSessionStore(first.db_path)

    ticket, baseline = first.claim_session_run_admission(session.id)
    assert baseline == (0, None)
    second.begin_session_deletion(session.id)
    assert second.session_run_admission_is_pending(session.id) is True
    with pytest.raises(RuntimeError, match="session deletion is pending"):
        second.claim_session_run_admission(session.id)

    first.release_session_run_admission(session.id, ticket)
    assert second.session_run_admission_is_pending(session.id) is False
    with pytest.raises(RuntimeError, match="session deletion is pending"):
        first.claim_session_run_admission(session.id)


def test_restart_delete_reaps_only_expired_run_admission_lease(
    tmp_path: Path,
) -> None:
    first = WebSessionStore(tmp_path / "expired-admission.sqlite")
    session = first.create_session("expired admission")
    ticket, _baseline = first.claim_session_run_admission(
        session.id,
        runtime_id="crashed-runtime",
        ttl_seconds=5.0,
        now=100.0,
    )
    restarted = WebSessionStore(first.db_path)
    restarted.begin_session_deletion(session.id)

    assert restarted.reap_expired_session_run_admissions(
        session.id,
        now=104.9,
    ) == 0
    assert restarted.session_run_admission_is_pending(session.id) is True
    assert restarted.reap_expired_session_run_admissions(
        session.id,
        now=105.0,
    ) == 1
    assert restarted.session_run_admission_is_pending(session.id) is False
    assert ticket.startswith("admission_")


def test_live_run_admission_renewal_prevents_delete_reap(
    tmp_path: Path,
) -> None:
    submitter = WebSessionStore(tmp_path / "live-admission.sqlite")
    session = submitter.create_session("live admission")
    ticket, _baseline = submitter.claim_session_run_admission(
        session.id,
        runtime_id="live-runtime",
        ttl_seconds=5.0,
        now=100.0,
    )
    deleter = WebSessionStore(submitter.db_path)
    deleter.begin_session_deletion(session.id)

    submitter.renew_session_run_admission(
        session.id,
        ticket,
        runtime_id="live-runtime",
        ttl_seconds=5.0,
        now=104.0,
    )
    assert deleter.reap_expired_session_run_admissions(
        session.id,
        now=106.0,
    ) == 0
    assert deleter.session_run_admission_is_pending(session.id) is True

    submitter.release_session_run_admission(session.id, ticket)
    assert deleter.session_run_admission_is_pending(session.id) is False


def test_restarted_store_reaps_expired_admission_ticket_before_deletion(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "expired-admission.sqlite"
    crashed = WebSessionStore(db_path)
    session = crashed.create_session("expired admission")
    claimed_at = datetime(2026, 7, 15, tzinfo=UTC)

    crashed.claim_session_run_admission(
        session.id,
        runtime_id="runtime-that-crashed",
        ttl_seconds=0.05,
        now=claimed_at,
    )

    restarted = WebSessionStore(db_path)
    restarted.reap_expired_session_run_admissions(
        session.id,
        now=claimed_at + timedelta(milliseconds=51),
    )
    restarted.begin_session_deletion(session.id)

    assert restarted.session_run_admission_is_pending(session.id) is False
    assert restarted.session_deletion_is_pending(session.id) is True


def test_renewed_admission_ticket_is_not_reaped_at_its_original_expiry(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "renewed-admission.sqlite"
    owner = WebSessionStore(db_path)
    session = owner.create_session("renewed admission")
    claimed_at = datetime(2026, 7, 15, tzinfo=UTC)
    ticket, _baseline = owner.claim_session_run_admission(
        session.id,
        runtime_id="live-runtime",
        ttl_seconds=0.05,
        now=claimed_at,
    )
    owner.renew_session_run_admission(
        session.id,
        ticket,
        ttl_seconds=0.05,
        now=claimed_at + timedelta(milliseconds=40),
    )

    deleter = WebSessionStore(db_path)
    deleter.reap_expired_session_run_admissions(
        session.id,
        now=claimed_at + timedelta(milliseconds=60),
    )

    assert deleter.session_run_admission_is_pending(session.id) is True
    owner.release_session_run_admission(session.id, ticket)
    assert deleter.session_run_admission_is_pending(session.id) is False


def test_complete_stop_request_atomically_stops_tools_and_is_idempotent(tmp_path: Path) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    session = store.create_session("complete durable stop")
    store.begin_execution_generation(session.id)
    store.start_tool_run(session.id, "call-1", "extract", "2026-07-15T00:00:00Z")
    request = store.begin_or_resume_stop_request(session.id)

    stopped, records = store.complete_stop_request_with_terminal_events(
        session.id,
        request.request_id,
        lambda row: (
            _digest(f"stop:{row.tool_call_id}"),
            {
                "name": "datapilot_tool_terminal",
                "value": {"tool_call_id": row.tool_call_id, "status": "stopped"},
            },
        ),
    )
    repeated = store.complete_stop_request_with_terminal_events(
        session.id,
        request.request_id,
        lambda _row: pytest.fail("completed stop must not emit again"),
    )

    assert [row.status for row in stopped] == ["stopped"]
    assert len(records) == 1
    assert repeated == ([], [])
    assert store.execution_generation_is_stopped(session.id) is True
    assert store.stop_request_is_pending(session.id, request.generation) is False

def test_existing_v1_schema_adds_stop_boundary_table_without_resetting_sessions(
    tmp_path: Path,
):
    db_path = tmp_path / "sessions.sqlite"
    store = WebSessionStore(db_path)
    session = store.create_session(title="preserved v1 session")
    with sqlite3.connect(db_path) as connection:
        connection.execute("DROP TABLE session_execution_boundaries")
        connection.execute("DROP TABLE tool_execution_provenance")
        assert connection.execute(
            "SELECT generation FROM web_schema WHERE singleton = 1"
        ).fetchone() == (SCHEMA_GENERATION,)

    reopened = WebSessionStore(db_path)

    assert reopened.list_sessions() == [session]
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'session_execution_boundaries'"
        ).fetchone() == ("session_execution_boundaries",)
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'tool_execution_provenance'"
        ).fetchone() == ("tool_execution_provenance",)


def test_stopped_generation_keeps_tool_delivery_tombstone_after_new_generation_and_delete(
    tmp_path: Path,
):
    db_path = tmp_path / "sessions.sqlite"
    store = WebSessionStore(db_path)
    session = store.create_session(title="delivery tombstone")
    store.begin_execution_generation(session.id)
    store.start_tool_run(
        session.id,
        "call-completed-before-stop",
        "extract",
        "2026-07-15T00:00:00.000+00:00",
    )
    store.finish_tool_run(
        session.id,
        "call-completed-before-stop",
        status="success",
        summary="done",
    )
    assert store.tool_delivery_sublabel_is_suppressed(
        session.id,
        "extract · call-completed-before-stop",
    ) is False

    stopped, records = store.stop_open_tool_runs_with_terminal_events(
        session.id,
        lambda _row: pytest.fail("terminal already won before stop"),
    )
    assert stopped == []
    assert records == []
    assert WebSessionStore(db_path).tool_delivery_sublabel_is_suppressed(
        session.id,
        "extract · call-completed-before-stop",
    ) is True

    store.begin_execution_generation(session.id)
    assert store.tool_delivery_sublabel_is_suppressed(
        session.id,
        "extract · call-completed-before-stop",
    ) is True

    store.delete_session(session.id)
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM tool_execution_provenance"
        ).fetchone() == (0,)


def test_stop_suppresses_an_older_generation_tool_that_is_still_running(
    tmp_path: Path,
):
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    session = store.create_session(title="older running delivery")
    assert store.begin_execution_generation(session.id) == 1
    store.start_tool_run(
        session.id,
        "call-generation-one",
        "extract",
        "2026-07-15T00:00:00.000+00:00",
    )
    assert store.begin_execution_generation(session.id) == 2

    stopped, _records = store.stop_open_tool_runs_with_terminal_events(
        session.id,
        lambda row: (
            _digest(f"stop:{row.tool_call_id}"),
            {
                "type": "CUSTOM",
                "name": "datapilot_tool_terminal",
                "value": {
                    "tool_call_id": row.tool_call_id,
                    "status": "stopped",
                },
            },
        ),
    )

    assert [row.tool_call_id for row in stopped] == ["call-generation-one"]
    assert store.tool_delivery_sublabel_is_suppressed(
        session.id,
        "extract · call-generation-one",
    ) is True


def test_store_rejects_message_and_event_for_missing_session(tmp_path: Path):
    store = WebSessionStore(tmp_path / "sessions.sqlite")

    with pytest.raises(KeyError):
        store.append_message("missing", role="user", content="hello")
    with pytest.raises(KeyError):
        store.append_public_event("missing", _digest("missing"), {"type": "reply_start"})

    assert store.get_session("missing") is None


def test_store_orders_messages_deterministically_when_timestamps_collide(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        "vla_data_juicer_agents.web.session_store._now",
        lambda: "2026-06-26T10:00:00.000+00:00",
    )
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    session = store.create_session(title="同一时间戳")

    user = store.append_message(session.id, role="user", content="first")
    assistant = store.append_message(session.id, role="assistant", content="second")
    detail = store.get_session(session.id)

    assert detail is not None
    assert [message.id for message in detail.messages] == [user.id, assistant.id]


def test_store_lists_recent_deterministically_when_timestamps_collide(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        "vla_data_juicer_agents.web.session_store._now",
        lambda: "2026-06-26T10:00:00.000+00:00",
    )
    store = WebSessionStore(tmp_path / "sessions.sqlite")

    first = store.create_session(title="第一个任务")
    second = store.create_session(title="第二个任务")

    assert [session.id for session in store.list_sessions()] == [second.id, first.id]


def test_store_keeps_agentscope_mappings_and_human_decisions_internal(tmp_path: Path):
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    session = store.create_session(title="internal state")
    store.save_agentscope_session_mapping(
        session.id,
        agent_id="navigation-data-agent",
        agentscope_session_id="internal-as-session",
    )
    store.mark_human_decision_consumed(
        agentscope_session_id="internal-as-session",
        reply_id="reply-1",
        tool_call_id="call-1",
        action="confirm",
    )

    mapping = store.get_agentscope_session_mapping(session.id)
    assert mapping is not None
    assert mapping.agentscope_session_id == "internal-as-session"
    assert store.is_human_decision_consumed(
        agentscope_session_id="internal-as-session",
        reply_id="reply-1",
        tool_call_id="call-1",
    )
    detail = store.get_session(session.id)
    assert detail is not None
    assert "internal-as-session" not in detail.model_dump_json()

    store.delete_session(session.id)

    assert not store.is_human_decision_consumed(
        agentscope_session_id="internal-as-session",
        reply_id="reply-1",
        tool_call_id="call-1",
    )


def test_store_rejects_agentscope_mapping_save_after_deletion_fence(
    tmp_path: Path,
) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    session = store.create_session(title="deleting session")
    store.begin_session_deletion(session.id)

    with pytest.raises(RuntimeError, match="session deletion is pending"):
        store.save_agentscope_session_mapping(
            session.id,
            agent_id="late-navigation-agent",
            agentscope_session_id="late-navigation-session",
        )

    assert store.list_agentscope_session_mappings(session.id) == []


def test_concurrent_mapping_save_observes_uncommitted_deletion_winner(
    tmp_path: Path,
) -> None:
    deleter = WebSessionStore(tmp_path / "mapping-delete-race.sqlite")
    session = deleter.create_session(title="mapping/delete race")
    submitter = WebSessionStore(deleter.db_path)
    save_started = Event()

    def save_mapping() -> None:
        save_started.set()
        submitter.save_agentscope_session_mapping(
            session.id,
            agent_id="late-navigation-agent",
            agentscope_session_id="late-navigation-session",
        )

    with sqlite3.connect(deleter.db_path, timeout=30.0) as deletion_connection:
        deletion_connection.execute("BEGIN IMMEDIATE")
        deletion_connection.execute(
            "INSERT INTO session_deletions (session_id, started_at) VALUES (?, ?)",
            (session.id, "2026-07-15T00:00:00.000+00:00"),
        )
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(save_mapping)
            assert save_started.wait(timeout=1.0)
            deletion_connection.commit()
            with pytest.raises(RuntimeError, match="session deletion is pending"):
                future.result(timeout=1.0)

    assert submitter.list_agentscope_session_mappings(session.id) == []


def test_deleting_session_allows_only_matching_live_admission_mapping(
    tmp_path: Path,
) -> None:
    store = WebSessionStore(tmp_path / "ticket-aware-mapping.sqlite")

    live = store.create_session(title="live admission")
    live_ticket, _ = store.claim_session_run_admission(
        live.id,
        runtime_id="owner-runtime",
        ttl_seconds=30.0,
    )
    store.begin_session_deletion(live.id)
    store.save_agentscope_session_mapping(
        live.id,
        agent_id="main-router-agent",
        agentscope_session_id="live-router-session",
        admission_ticket=live_ticket,
        runtime_id="owner-runtime",
    )
    assert [
        mapping.agentscope_session_id
        for mapping in store.list_agentscope_session_mappings(live.id)
    ] == ["live-router-session"]

    stolen = store.create_session(title="stolen admission")
    stolen_ticket, _ = store.claim_session_run_admission(
        stolen.id,
        runtime_id="owner-runtime",
        ttl_seconds=30.0,
    )
    store.begin_session_deletion(stolen.id)
    with pytest.raises(RuntimeError, match="session deletion is pending"):
        store.save_agentscope_session_mapping(
            stolen.id,
            agent_id="main-router-agent",
            agentscope_session_id="stolen-router-session",
            admission_ticket=stolen_ticket,
            runtime_id="intruder-runtime",
        )

    expired = store.create_session(title="expired admission")
    expired_ticket, _ = store.claim_session_run_admission(
        expired.id,
        runtime_id="owner-runtime",
        ttl_seconds=1.0,
        now=100.0,
    )
    store.begin_session_deletion(expired.id)
    with pytest.raises(RuntimeError, match="session deletion is pending"):
        store.save_agentscope_session_mapping(
            expired.id,
            agent_id="main-router-agent",
            agentscope_session_id="expired-router-session",
            admission_ticket=expired_ticket,
            runtime_id="owner-runtime",
        )

    assert store.list_agentscope_session_mappings(stolen.id) == []
    assert store.list_agentscope_session_mappings(expired.id) == []


def test_crashed_turn_reservation_expires_for_same_id_retry_and_delete_cleanup(
    tmp_path: Path,
) -> None:
    store = WebSessionStore(tmp_path / "turn-reservation-crash.sqlite")
    session = store.create_session(title="turn reservation crash")
    store.claim_user_message(
        session.id,
        "local-crash-retry",
        "retry me",
        runtime_id="crashed-runtime",
        turn_id="turn-crash-retry",
        ttl_seconds=5.0,
        now=100.0,
    )

    assert store.claim_user_message(
        session.id,
        "local-crash-retry",
        "retry me",
        runtime_id="retry-runtime",
        turn_id="turn-crash-retry",
        ttl_seconds=5.0,
        now=104.9,
    ) == "pending"

    store.claim_user_message(
        session.id,
        "local-crash-retry",
        "retry me",
        runtime_id="retry-runtime",
        turn_id="turn-crash-retry",
        ttl_seconds=5.0,
        now=105.0,
    )
    with sqlite3.connect(store.db_path) as connection:
        owner = connection.execute(
            "SELECT runtime_id FROM session_turn_admissions WHERE message_id = ?",
            ("local-crash-retry",),
        ).fetchone()
    assert owner == ("retry-runtime",)

    store.delete_session(session.id)
    with sqlite3.connect(store.db_path) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM session_turn_admissions",
        ).fetchone()
    assert count == (0,)


def test_user_turn_terminal_event_insert_failure_rolls_back_status(
    tmp_path: Path,
) -> None:
    store = WebSessionStore(tmp_path / "turn-terminal-rollback.sqlite")
    session = store.create_session(title="terminal rollback")
    store.claim_user_message(
        session.id,
        "local-terminal-rollback",
        "run",
        runtime_id="runtime-1",
        turn_id="turn_terminal_rollback",
        ttl_seconds=30.0,
    )
    store.commit_user_message(
        session.id,
        "local-terminal-rollback",
        "run",
        runtime_id="runtime-1",
        ttl_seconds=30.0,
    )
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER fail_turn_terminal_insert
            BEFORE INSERT ON public_events
            BEGIN
                SELECT RAISE(ABORT, 'injected terminal insert failure');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="injected terminal insert failure"):
        store.finish_user_message_turn_with_event(
            session.id,
            "local-terminal-rollback",
            turn_id="turn_terminal_rollback",
            terminal_status="success",
        )

    assert store.user_message_turn_status(
        session.id,
        "local-terminal-rollback",
    ) == "admitted"
    assert store.list_public_events(session.id) == []


def test_expired_admitted_turn_stays_fenced_without_owner_quiescence(
    tmp_path: Path,
) -> None:
    store = WebSessionStore(tmp_path / "turn-expiry-fail-closed.sqlite")
    session = store.create_session(title="expired owner may still be live")
    message_id = "local-expired-live-owner"
    store.claim_user_message(
        session.id,
        message_id,
        "run",
        runtime_id="runtime-1",
        turn_id="turn_expired_live_owner",
        ttl_seconds=30.0,
    )
    store.commit_user_message(
        session.id,
        message_id,
        "run",
        runtime_id="runtime-1",
        ttl_seconds=30.0,
    )
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            "UPDATE session_turn_admissions SET expires_at = 100 WHERE message_id = ?",
            (message_id,),
        )

    assert store.recover_expired_user_message_turns(session.id, now=101.0) == []
    assert store.user_message_turn_status(session.id, message_id) == "admitted"
    assert store.list_public_events(session.id) == []
    assert store.claim_user_message(
        session.id,
        "local-successor",
        "must not overlap",
        runtime_id="runtime-2",
        turn_id="turn_successor",
        ttl_seconds=30.0,
    ) == "busy"


def test_session_turn_fence_rejects_distinct_id_until_terminal(
    tmp_path: Path,
) -> None:
    store = WebSessionStore(tmp_path / "session-turn-fence.sqlite")
    session = store.create_session(title="session turn fence")
    assert store.claim_user_message(
        session.id,
        "local-first-turn",
        "first",
        runtime_id="runtime-1",
        turn_id="turn_first",
        ttl_seconds=30.0,
    ) == "claimed"
    assert store.claim_user_message(
        session.id,
        "local-second-turn",
        "second",
        runtime_id="runtime-2",
        turn_id="turn_second",
        ttl_seconds=30.0,
    ) == "busy"
    store.commit_user_message(
        session.id,
        "local-first-turn",
        "first",
        runtime_id="runtime-1",
        ttl_seconds=30.0,
    )
    assert store.claim_user_message(
        session.id,
        "local-second-turn",
        "second",
        runtime_id="runtime-2",
        turn_id="turn_second",
        ttl_seconds=30.0,
    ) == "busy"
    store.finish_user_message_turn_with_event(
        session.id,
        "local-first-turn",
        turn_id="turn_first",
        terminal_status="success",
    )
    assert store.claim_user_message(
        session.id,
        "local-second-turn",
        "second",
        runtime_id="runtime-2",
        turn_id="turn_second",
        ttl_seconds=30.0,
    ) == "claimed"


def test_running_public_tool_keeps_session_turn_fenced_after_foreground_terminal(
    tmp_path: Path,
) -> None:
    store = WebSessionStore(tmp_path / "running-tool-turn-fence.sqlite")
    session = store.create_session(title="running tool turn fence")
    store.start_tool_run(
        session.id,
        "call-running",
        "extract_and_sync",
        "2026-07-15T00:00:00.000+00:00",
    )

    assert store.claim_user_message(
        session.id,
        "local-after-running-tool",
        "must wait",
        runtime_id="runtime-2",
        turn_id="turn_after_running_tool",
        ttl_seconds=30.0,
    ) == "busy"

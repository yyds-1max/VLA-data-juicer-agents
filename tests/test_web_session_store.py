import os
import sqlite3
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from vla_data_juicer_agents.web.schemas import (
    CreateTurnRequest,
    SessionRecord,
    generate_session_title,
)
from vla_data_juicer_agents.web.session_store import WebSessionStore


def test_generate_session_title_uses_first_30_chars():
    title = generate_session_title("处理 20270605 的室外导航数据，并进行 dry-run 验证")

    assert title == "处理 20270605 的室外导航数据，并进行 dry-ru"


def test_generate_session_title_bounds_long_ascii_token():
    title = generate_session_title("a" * 5000)

    assert len(title) == 30
    assert title == "a" * 30


def test_turn_request_rejects_empty_message():
    try:
        CreateTurnRequest(message="   ")
    except ValueError as exc:
        assert "message must not be empty" in str(exc)
    else:
        raise AssertionError("expected validation error")


def test_turn_request_rejects_empty_invocation_id():
    with pytest.raises(ValueError, match="invocation_id must not be empty"):
        CreateTurnRequest(message="处理数据", invocation_id="   ")


def test_session_record_serializes_status():
    record = SessionRecord(
        id="session_1",
        title="处理 20270605 的室外导航数据",
        status="active",
        created_at="2026-06-26T10:00:00+08:00",
        updated_at="2026-06-26T10:01:00+08:00",
    )

    assert record.model_dump()["status"] == "active"


def test_store_creates_session_and_lists_recent(tmp_path: Path):
    store = WebSessionStore(tmp_path / "sessions.sqlite")

    session = store.create_session(title="处理 20270605 的室外导航数据")
    recent = store.list_sessions()

    assert session.status == "active"
    assert recent == [session]


def test_store_secures_new_and_existing_sqlite_files(
    tmp_path: Path,
) -> None:
    database = tmp_path / "sessions.sqlite"
    previous_umask = os.umask(0o022)
    try:
        WebSessionStore(database)
    finally:
        os.umask(previous_umask)
    assert stat.S_IMODE(database.stat().st_mode) == 0o600

    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute(
            "INSERT INTO sessions (id, title, status, created_at, updated_at, contract_version) "
            "VALUES ('mode-check', 'mode-check', 'active', 'now', 'now', 1)",
        )
        connection.commit()
        sidecars = (Path(f"{database}-wal"), Path(f"{database}-shm"))
        assert all(path.exists() for path in sidecars)
        for path in (database, *sidecars):
            path.chmod(0o666)

        WebSessionStore(database)

        for path in (database, *sidecars):
            assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_store_rejects_shared_writable_database_parent(
    tmp_path: Path,
) -> None:
    shared_parent = tmp_path / "shared-session-state"
    shared_parent.mkdir()
    shared_parent.chmod(0o777)

    with pytest.raises(RuntimeError, match="parent is unsafe"):
        WebSessionStore(shared_parent / "sessions.sqlite")

    assert not (shared_parent / "sessions.sqlite").exists()


def test_store_rejects_symlink_database_parent(
    tmp_path: Path,
) -> None:
    private_parent = tmp_path / "private-session-state"
    private_parent.mkdir(mode=0o700)
    linked_parent = tmp_path / "linked-session-state"
    linked_parent.symlink_to(private_parent, target_is_directory=True)

    with pytest.raises(RuntimeError, match="parent is unsafe"):
        WebSessionStore(linked_parent / "sessions.sqlite")

    assert not (private_parent / "sessions.sqlite").exists()


@pytest.mark.parametrize("suffix", ["", "-wal", "-shm"])
def test_store_rejects_sqlite_symlinks(
    tmp_path: Path,
    suffix: str,
) -> None:
    database = tmp_path / "sessions.sqlite"
    if suffix:
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE preexisting (value TEXT)")
    target = tmp_path / f"symlink-target{suffix or '-main'}"
    target.write_bytes(b"private")
    Path(f"{database}{suffix}").symlink_to(target)

    with pytest.raises(RuntimeError, match="regular file"):
        WebSessionStore(database)


@pytest.mark.parametrize("suffix", ["", "-wal", "-shm"])
def test_store_rejects_sqlite_special_files(
    tmp_path: Path,
    suffix: str,
) -> None:
    database = tmp_path / "sessions.sqlite"
    if suffix:
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE preexisting (value TEXT)")
    os.mkfifo(Path(f"{database}{suffix}"))

    with pytest.raises(RuntimeError, match="regular file"):
        WebSessionStore(database)


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


def test_store_persists_timeline_events(tmp_path: Path):
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    session = store.create_session(title="处理 20270605 的室外导航数据")

    first = store.append_timeline_event(
        session.id,
        {
            "type": "assistant_delta",
            "source": "navigation-data-agent",
            "run_id": "as-session",
            "parent_run_id": None,
            "timestamp": "2026-06-26T10:00:00.000+00:00",
            "payload": {"delta": "开始检查"},
        },
    )
    second = store.append_timeline_event(
        session.id,
        {
            "type": "tool_end",
            "source": "navigation-data-agent",
            "run_id": "as-session",
            "parent_run_id": None,
            "timestamp": "2026-06-26T10:00:01.000+00:00",
            "payload": {"tool": "prepare_raw_data", "call_id": "call-1", "status": "completed"},
        },
    )

    detail = store.get_session(session.id)

    assert detail is not None
    assert [event.id for event in detail.events] == [first.id, second.id]
    assert [event.seq for event in detail.events] == [1, 2]
    assert detail.events[0].payload == {"delta": "开始检查"}
    assert detail.events[1].type == "tool_end"


def test_store_projects_navigation_dataset_events_with_a_durable_cursor(
    tmp_path: Path,
):
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    session = store.create_session(title="处理 20270605")

    store.append_timeline_event(
        session.id,
        {
            "type": "task_state_updated",
            "timestamp": "2026-08-10T10:00:00+00:00",
            "payload": {
                "task_ref": "DP-TEST",
                "dataset_date": "20270605",
                "status": "active",
                "phase": "拆解与同步",
                "state_revision": 4,
            },
        },
    )
    store.append_timeline_event(
        session.id,
        {
            "type": "tool_end",
            "timestamp": "2026-08-10T10:00:01+00:00",
            "payload": {"status": "completed"},
        },
    )

    assert store.navigation_dataset_event_cursor() == 1
    events = store.list_navigation_dataset_events_after(after_seq=0)
    assert events == [
        {
            "seq": 1,
            "event_ref": events[0]["event_ref"],
            "event_kind": "navigation.task.changed",
            "dataset_date": "20270605",
            "state_revision": 4,
            "occurred_at": "2026-08-10T10:00:00+00:00",
            "task_ref": "DP-TEST",
            "status": "active",
            "phase": "拆解与同步",
        }
    ]
    assert store.list_navigation_dataset_events_after(after_seq=1) == []


def test_projected_progress_stream_is_durable_and_idempotent(tmp_path: Path):
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    session = store.create_session(title="streamed progress")
    submission = store.begin_user_turn(session.id, "处理数据")
    turn_id = submission.turn.id
    store.save_agentscope_session_mapping(
        session.id,
        agent_id="navigation-data-agent",
        agentscope_session_id="as-navigation",
        active_turn_id=turn_id,
    )
    store.register_pending_reply(
        turn_id=turn_id,
        agentscope_session_id="as-navigation",
        agent_id="navigation-data-agent",
        source="user",
    )
    store.append_projected_event_batch(
        web_session_id=session.id,
        agentscope_session_id="as-navigation",
        entry_id="1-0",
        raw_event_type="REPLY_START",
        reply_id="reply-1",
        events=[],
    )
    projected = [
        {
            "type": "progress_start",
            "source": "agentscope",
            "run_id": "as-navigation",
            "payload": {
                "progress_id": "progress-1",
                "reply_id": "reply-1",
            },
        },
        {
            "type": "progress_delta",
            "source": "agentscope",
            "run_id": "as-navigation",
            "payload": {
                "progress_id": "progress-1",
                "delta": "已确认原始数据，",
                "reply_id": "reply-1",
            },
        },
        {
            "type": "progress_end",
            "source": "agentscope",
            "run_id": "as-navigation",
            "payload": {
                "progress_id": "progress-1",
                "reply_id": "reply-1",
            },
        },
    ]

    first = store.append_projected_event_batch(
        web_session_id=session.id,
        agentscope_session_id="as-navigation",
        entry_id="2-0",
        raw_event_type="TEXT_BLOCK_DELTA",
        reply_id="reply-1",
        events=projected,
    )
    duplicate = store.append_projected_event_batch(
        web_session_id=session.id,
        agentscope_session_id="as-navigation",
        entry_id="2-0",
        raw_event_type="TEXT_BLOCK_DELTA",
        reply_id="reply-1",
        events=projected,
    )

    assert [record.type for record in first] == [
        "progress_start",
        "progress_delta",
        "progress_end",
    ]
    assert all(record.turn_id == turn_id for record in first)
    assert all(record.payload["reply_id"] == "reply-1" for record in first)
    assert duplicate == []
    detail = store.get_session(session.id)
    assert detail is not None
    assert [event.type for event in detail.events[-3:]] == [
        "progress_start",
        "progress_delta",
        "progress_end",
    ]


def test_projected_answer_stream_is_persisted_and_reconciled_by_reply_id(tmp_path: Path):
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    session = store.create_session(title="streamed answer")
    submission = store.begin_user_turn(session.id, "请汇总结果")
    turn_id = submission.turn.id
    store.save_agentscope_session_mapping(
        session.id,
        agent_id="main-router-agent",
        agentscope_session_id="as-router",
        active_turn_id=turn_id,
    )
    store.register_pending_reply(
        turn_id=turn_id,
        agentscope_session_id="as-router",
        agent_id="main-router-agent",
        source="user",
    )
    store.append_projected_event_batch(
        web_session_id=session.id,
        agentscope_session_id="as-router",
        entry_id="1-0",
        raw_event_type="REPLY_START",
        reply_id="reply-1",
        events=[],
    )
    delta = store.append_projected_event_batch(
        web_session_id=session.id,
        agentscope_session_id="as-router",
        entry_id="2-0",
        raw_event_type="TEXT_BLOCK_DELTA",
        reply_id="reply-1",
        events=[
            {
                "type": "answer_delta",
                "source": "agentscope",
                "run_id": "as-router",
                "payload": {"delta": "结果已准备好。", "reply_id": "reply-1"},
            }
        ],
    )
    final = store.append_projected_event_batch(
        web_session_id=session.id,
        agentscope_session_id="as-router",
        entry_id="3-0",
        raw_event_type="REPLY_END",
        reply_id="reply-1",
        events=[
            {
                "type": "reply_summary",
                "source": "agentscope",
                "run_id": "as-router",
                "payload": {"text": "结果已准备好。", "reply_id": "reply-1"},
            }
        ],
    )

    assert delta[0].payload == {"delta": "结果已准备好。", "reply_id": "reply-1"}
    final_event = next(event for event in final if event.type == "final")
    detail = store.get_session(session.id)
    assert detail is not None
    assert final_event.payload["reply_id"] == "reply-1"
    assert final_event.payload["message_id"] == detail.turns[0].final_message_id
    assert [message.content for message in detail.messages if message.role == "assistant"] == [
        "结果已准备好。"
    ]
    assert store.append_projected_event_batch(
        web_session_id=session.id,
        agentscope_session_id="as-router",
        entry_id="3-0",
        raw_event_type="REPLY_END",
        reply_id="reply-1",
        events=[],
    ) == []


def test_intermediate_stream_is_reset_when_reply_remains_blocked(tmp_path: Path):
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    session = store.create_session(title="background stream")
    submission = store.begin_user_turn(session.id, "处理数据")
    turn_id = submission.turn.id
    store.save_agentscope_session_mapping(
        session.id,
        agent_id="navigation-data-agent",
        agentscope_session_id="as-navigation",
        active_turn_id=turn_id,
    )
    store.register_pending_reply(
        turn_id=turn_id,
        agentscope_session_id="as-navigation",
        agent_id="navigation-data-agent",
        source="user",
    )
    store.append_projected_event_batch(
        web_session_id=session.id,
        agentscope_session_id="as-navigation",
        entry_id="1-0",
        raw_event_type="REPLY_START",
        reply_id="reply-1",
        events=[],
    )
    store.append_projected_event_batch(
        web_session_id=session.id,
        agentscope_session_id="as-navigation",
        entry_id="2-0",
        raw_event_type="TOOL_RESULT_END",
        reply_id="reply-1",
        events=[
            {
                "type": "tool_background",
                "source": "agentscope",
                "run_id": "as-navigation",
                "payload": {"tool": "extract_tool", "call_id": "call-1"},
            },
            {
                "type": "answer_delta",
                "source": "agentscope",
                "run_id": "as-navigation",
                "payload": {"delta": "暂时汇总。", "reply_id": "reply-1"},
            },
        ],
    )
    records = store.append_projected_event_batch(
        web_session_id=session.id,
        agentscope_session_id="as-navigation",
        entry_id="3-0",
        raw_event_type="REPLY_END",
        reply_id="reply-1",
        events=[
            {
                "type": "reply_summary",
                "source": "agentscope",
                "run_id": "as-navigation",
                "payload": {"text": "后台处理仍在继续。"},
            }
        ],
    )

    assert [record.type for record in records] == ["answer_reset", "progress_update"]
    assert records[0].payload == {"reply_id": "reply-1"}
    assert records[1].payload == {"text": "后台处理仍在继续。", "reply_id": "reply-1"}


def test_empty_public_reply_fails_and_closes_turn_instead_of_leaving_it_running(tmp_path: Path):
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    session = store.create_session(title="empty reply")
    submission = store.begin_user_turn(session.id, "请处理")
    turn_id = submission.turn.id
    store.save_agentscope_session_mapping(
        session.id,
        agent_id="main-router-agent",
        agentscope_session_id="as-router",
        active_turn_id=turn_id,
    )
    store.register_pending_reply(
        turn_id=turn_id,
        agentscope_session_id="as-router",
        agent_id="main-router-agent",
        source="user",
    )
    store.append_projected_event_batch(
        web_session_id=session.id,
        agentscope_session_id="as-router",
        entry_id="1-0",
        raw_event_type="REPLY_START",
        reply_id="reply-empty",
        events=[],
    )

    records = store.append_projected_event_batch(
        web_session_id=session.id,
        agentscope_session_id="as-router",
        entry_id="2-0",
        raw_event_type="REPLY_END",
        reply_id="reply-empty",
        events=[],
    )

    detail = store.get_session(session.id)
    assert detail is not None
    assert [record.type for record in records] == ["final", "turn_state"]
    assert records[0].payload == {
        "text": "本轮处理已结束，但未能生成可安全展示的回复。请重试。",
        "message_id": detail.turns[0].final_message_id,
        "reply_id": "reply-empty",
    }
    assert detail.turns[0].status == "failed"
    assert detail.messages[-1].content == records[0].payload["text"]


def test_empty_summary_resets_existing_stream_before_safe_failure_final(tmp_path: Path):
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    session = store.create_session(title="unsafe reply")
    submission = store.begin_user_turn(session.id, "请处理")
    turn_id = submission.turn.id
    store.save_agentscope_session_mapping(
        session.id,
        agent_id="main-router-agent",
        agentscope_session_id="as-router",
        active_turn_id=turn_id,
    )
    store.register_pending_reply(
        turn_id=turn_id,
        agentscope_session_id="as-router",
        agent_id="main-router-agent",
        source="user",
    )
    store.append_projected_event_batch(
        web_session_id=session.id,
        agentscope_session_id="as-router",
        entry_id="1-0",
        raw_event_type="REPLY_START",
        reply_id="reply-unsafe",
        events=[],
    )
    store.append_projected_event_batch(
        web_session_id=session.id,
        agentscope_session_id="as-router",
        entry_id="2-0",
        raw_event_type="TEXT_BLOCK_DELTA",
        reply_id="reply-unsafe",
        events=[
            {
                "type": "answer_delta",
                "source": "agentscope",
                "run_id": "as-router",
                "payload": {"delta": "临时安全前缀。", "reply_id": "reply-unsafe"},
            }
        ],
    )

    records = store.append_projected_event_batch(
        web_session_id=session.id,
        agentscope_session_id="as-router",
        entry_id="3-0",
        raw_event_type="REPLY_END",
        reply_id="reply-unsafe",
        events=[],
    )

    assert [record.type for record in records] == ["answer_reset", "final", "turn_state"]
    assert records[-1].payload["status"] == "failed"


def test_controller_complete_turn_uses_sanitized_text_and_message_identity(tmp_path: Path):
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    session = store.create_session(title="controller final")
    submission = store.begin_user_turn(session.id, "请处理")

    records = store.complete_turn(
        submission.turn.id,
        "处理完成。\n- task_id: secret\n- 已生成结果。",
    )

    detail = store.get_session(session.id)
    assert detail is not None
    final = records[0]
    assert final.type == "final"
    assert final.payload == {
        "text": "处理完成。\n- 已生成结果。",
        "message_id": detail.turns[0].final_message_id,
    }
    assert detail.messages[-1].content == "处理完成。\n- 已生成结果。"


def test_store_commits_projected_batch_final_and_cursor_atomically_and_idempotently(
    tmp_path: Path,
):
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    session = store.create_session(title="background wakeup")
    store.save_agentscope_session_mapping(
        session.id,
        agent_id="navigation-data-agent",
        agentscope_session_id="as-navigation",
    )
    events = [
        {
            "type": "activity_delta",
            "source": "agentscope",
            "run_id": "as-navigation",
            "payload": {"activity_id": "activity-1", "status": "completed"},
        },
        {
            "type": "final",
            "source": "agentscope",
            "run_id": "as-navigation",
            "payload": {"text": "后台任务已经完成。"},
        },
    ]

    first = store.append_projected_event_batch(
        web_session_id=session.id,
        agentscope_session_id="as-navigation",
        entry_id="9-0",
        events=events,
    )
    second = store.append_projected_event_batch(
        web_session_id=session.id,
        agentscope_session_id="as-navigation",
        entry_id="9-0",
        events=events,
    )

    detail = store.get_session(session.id)
    mapping = store.get_agentscope_session_mapping(session.id)
    assert [event.type for event in first] == ["activity_delta", "final"]
    assert second == []
    assert detail is not None
    assert [event.type for event in detail.events] == ["activity_delta", "final"]
    assert [(message.role, message.content) for message in detail.messages] == [
        ("assistant", "后台任务已经完成。")
    ]
    assert mapping is not None
    assert mapping.event_cursor == "9-0"


def test_store_keeps_background_reply_as_progress_and_wakeup_as_unique_final(
    tmp_path: Path,
):
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    session = store.create_session(title="background wakeup")
    submission = store.begin_user_turn(session.id, "处理导航数据")
    turn_id = submission.turn.id
    store.save_agentscope_session_mapping(
        session.id,
        agent_id="navigation-data-agent",
        agentscope_session_id="as-navigation",
        active_turn_id=turn_id,
    )
    store.register_pending_reply(
        turn_id=turn_id,
        agentscope_session_id="as-navigation",
        agent_id="navigation-data-agent",
        source="user",
    )
    store.append_projected_event_batch(
        web_session_id=session.id,
        agentscope_session_id="as-navigation",
        entry_id="1-0",
        raw_event_type="REPLY_START",
        reply_id="reply-1",
        events=[],
    )
    store.append_projected_event_batch(
        web_session_id=session.id,
        agentscope_session_id="as-navigation",
        entry_id="2-0",
        raw_event_type="TOOL_CALL_START",
        reply_id="reply-1",
        events=[
            {
                "type": "tool_start",
                "source": "agentscope",
                "run_id": "as-navigation",
                "payload": {"tool": "extract_tool", "call_id": "call-1"},
            }
        ],
    )
    store.append_projected_event_batch(
        web_session_id=session.id,
        agentscope_session_id="as-navigation",
        entry_id="3-0",
        raw_event_type="TOOL_RESULT_END",
        reply_id="reply-1",
        events=[
            {
                "type": "tool_background",
                "source": "agentscope",
                "run_id": "as-navigation",
                "payload": {
                    "tool": "extract_tool",
                    "call_id": "call-1",
                    "status": "background",
                },
            }
        ],
    )
    first_reply = store.append_projected_event_batch(
        web_session_id=session.id,
        agentscope_session_id="as-navigation",
        entry_id="4-0",
        raw_event_type="REPLY_END",
        reply_id="reply-1",
        events=[
            {
                "type": "reply_summary",
                "source": "agentscope",
                "run_id": "as-navigation",
                "payload": {"text": "提取操作仍在运行，完成后会继续检查结果。"},
            }
        ],
    )

    detail = store.get_session(session.id)
    assert detail is not None
    assert [event.type for event in first_reply] == ["progress_update"]
    assert [message.role for message in detail.messages] == ["user"]
    assert detail.turns[0].status == "running"

    store.register_pending_reply(
        turn_id=turn_id,
        agentscope_session_id="as-navigation",
        agent_id="navigation-data-agent",
        source="wakeup",
    )
    store.append_projected_event_batch(
        web_session_id=session.id,
        agentscope_session_id="as-navigation",
        entry_id="5-0",
        raw_event_type="REPLY_START",
        reply_id="reply-2",
        events=[],
    )
    store.append_projected_event_batch(
        web_session_id=session.id,
        agentscope_session_id="as-navigation",
        entry_id="6-0",
        raw_event_type="HINT_BLOCK",
        reply_id="reply-2",
        events=[
            {
                "type": "tool_end",
                "source": "agentscope",
                "run_id": "as-navigation",
                "payload": {
                    "tool": "extract_tool",
                    "call_id": "call-1",
                    "status": "completed",
                },
            }
        ],
    )
    final_reply = store.append_projected_event_batch(
        web_session_id=session.id,
        agentscope_session_id="as-navigation",
        entry_id="7-0",
        raw_event_type="REPLY_END",
        reply_id="reply-2",
        events=[
            {
                "type": "reply_summary",
                "source": "agentscope",
                "run_id": "as-navigation",
                "payload": {"text": "导航数据已提取并完成产物检查。"},
            }
        ],
    )
    store.append_projected_event_batch(
        web_session_id=session.id,
        agentscope_session_id="as-navigation",
        entry_id="7-0",
        raw_event_type="REPLY_END",
        reply_id="reply-2",
        events=[
            {
                "type": "reply_summary",
                "source": "agentscope",
                "run_id": "as-navigation",
                "payload": {"text": "导航数据已提取并完成产物检查。"},
            }
        ],
    )

    detail = store.get_session(session.id)
    assert detail is not None
    assert [event.type for event in final_reply] == ["final", "turn_state"]
    assert [(message.role, message.turn_id) for message in detail.messages] == [
        ("user", turn_id),
        ("assistant", turn_id),
    ]
    assert [event.type for event in detail.events].count("final") == 1
    assert detail.turns[0].status == "completed"
    assert detail.turns[0].final_message_id == detail.messages[-1].id


def test_router_handoff_reply_stays_progress_until_navigation_reply_finishes(
    tmp_path: Path,
):
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    session = store.create_session(title="router handoff")
    submission = store.begin_user_turn(session.id, "处理导航数据")
    turn_id = submission.turn.id
    for agent_id, agentscope_session_id in (
        ("main-router-agent", "as-router"),
        ("navigation-data-agent", "as-navigation"),
    ):
        store.save_agentscope_session_mapping(
            session.id,
            agent_id=agent_id,
            agentscope_session_id=agentscope_session_id,
            active_turn_id=turn_id,
        )

    store.register_pending_reply(
        turn_id=turn_id,
        agentscope_session_id="as-router",
        agent_id="main-router-agent",
        source="user",
    )
    store.append_projected_event_batch(
        web_session_id=session.id,
        agentscope_session_id="as-router",
        entry_id="1-0",
        raw_event_type="REPLY_START",
        reply_id="router-reply",
        events=[],
    )
    # The handoff registers the child before the router can close its own reply.
    store.register_pending_reply(
        turn_id=turn_id,
        agentscope_session_id="as-navigation",
        agent_id="navigation-data-agent",
        source="handoff",
    )
    router_end = store.append_projected_event_batch(
        web_session_id=session.id,
        agentscope_session_id="as-router",
        entry_id="2-0",
        raw_event_type="REPLY_END",
        reply_id="router-reply",
        events=[
            {
                "type": "reply_summary",
                "source": "agentscope",
                "run_id": "as-router",
                "payload": {"text": "已确认这是导航数据任务，正在进入数据检查。"},
            }
        ],
    )

    assert [event.type for event in router_end] == ["progress_update"]
    assert store.get_active_turn(session.id).id == turn_id

    store.append_projected_event_batch(
        web_session_id=session.id,
        agentscope_session_id="as-navigation",
        entry_id="3-0",
        raw_event_type="REPLY_START",
        reply_id="navigation-reply",
        events=[],
    )
    navigation_end = store.append_projected_event_batch(
        web_session_id=session.id,
        agentscope_session_id="as-navigation",
        entry_id="4-0",
        raw_event_type="REPLY_END",
        reply_id="navigation-reply",
        events=[
            {
                "type": "reply_summary",
                "source": "agentscope",
                "run_id": "as-navigation",
                "payload": {"text": "导航数据检查完成，可以继续执行。"},
            }
        ],
    )

    assert [event.type for event in navigation_end] == ["final", "turn_state"]
    detail = store.get_session(session.id)
    assert detail is not None
    assert [message.role for message in detail.messages] == ["user", "assistant"]
    assert detail.messages[-1].content == "导航数据检查完成，可以继续执行。"
    assert detail.turns[0].status == "completed"


def test_v1_navigation_await_user_final_ignores_parent_router_blockers(
    tmp_path: Path,
) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    session = store.create_session(title="navigation awaits user")
    turn = store.begin_user_turn(session.id, "检查已有产物后继续").turn
    router_session_id = "as-router-await-user"
    navigation_session_id = "as-navigation-await-user"

    store.save_conversation_agent_session(
        session.id,
        agent_role="router",
        agent_id="main-router-agent",
        agentscope_session_id=router_session_id,
    )
    store.bind_conversation_agent_session_to_turn(router_session_id, turn.id)
    binding = store.create_task_binding(
        session.id,
        task_id="task-await-user-final",
        task_ref="DP-AWAIT-FINAL",
        navigation_session_id=navigation_session_id,
    ).binding
    store.bind_conversation_agent_session_to_turn(navigation_session_id, turn.id)

    store.register_pending_reply(
        turn_id=turn.id,
        agentscope_session_id=router_session_id,
        agent_id="main-router-agent",
        source="user",
    )
    store.append_projected_event_batch(
        web_session_id=session.id,
        agentscope_session_id=router_session_id,
        entry_id="router-start",
        raw_event_type="REPLY_START",
        reply_id="router-reply",
        events=[],
    )
    # Router's delegation call starts before authority is handed to Navigation.
    # Its late ToolResult/ReplyEnd is intentionally rejected after handover, so
    # these private parent rows can remain running and must not block the owner.
    store.append_projected_event_batch(
        web_session_id=session.id,
        agentscope_session_id=router_session_id,
        entry_id="router-tool-start",
        raw_event_type="TOOL_CALL_START",
        reply_id="router-reply",
        events=[],
        private_events=[
            {
                "type": "tool_start",
                "payload": {
                    "tool": "start_navigation_data_task",
                    "call_id": "router-handoff-call",
                },
            }
        ],
    )
    authority = store.get_response_authority(turn.id)
    assert authority is not None
    store.handover_response_authority(
        turn.id,
        expected_producer="router",
        expected_generation=authority.generation,
        new_producer="navigation",
    )

    store.register_pending_reply(
        turn_id=turn.id,
        agentscope_session_id=navigation_session_id,
        agent_id="navigation-data-agent",
        source="handoff",
    )
    store.append_projected_event_batch(
        web_session_id=session.id,
        agentscope_session_id=navigation_session_id,
        entry_id="navigation-start",
        raw_event_type="REPLY_START",
        reply_id="navigation-reply",
        events=[],
    )
    store.append_projected_event_batch(
        web_session_id=session.id,
        agentscope_session_id=navigation_session_id,
        entry_id="navigation-delta",
        raw_event_type="TEXT_BLOCK_DELTA",
        reply_id="navigation-reply",
        events=[
            {
                "type": "answer_delta",
                "payload": {
                    "delta": "已完成产物检查，",
                    "reply_id": "navigation-reply",
                },
            }
        ],
    )
    binding = store.update_task_binding(
        binding.task_id,
        expected_revision=binding.state_revision,
        status="waiting_user",
        latest_public_update="等待你补充场景模式。",
    )

    prompt = "继续处理前，请告诉我这是室内还是室外数据。"
    records = store.append_projected_event_batch(
        web_session_id=session.id,
        agentscope_session_id=navigation_session_id,
        entry_id="navigation-await-user",
        raw_event_type="REPLY_END",
        reply_id="navigation-reply",
        events=[
            {
                "type": "task_state_updated",
                "payload": {
                    "task_ref": binding.task_ref,
                    "status": "waiting_user",
                },
            },
            {
                "type": "final",
                "payload": {
                    "text": prompt,
                    "task_ref": binding.task_ref,
                    "task_status": "waiting_user",
                },
            },
        ],
    )

    detail = store.get_session(session.id)
    final_authority = store.get_response_authority(turn.id)
    assert detail is not None
    assert [record.type for record in records] == [
        "task_state_updated",
        "final",
        "turn_state",
    ]
    assert all(record.type != "answer_reset" for record in records)
    assert all(record.type != "progress_update" for record in records)
    assert detail.messages[-1].role == "assistant"
    assert detail.messages[-1].content == prompt
    assert detail.turns[0].status == "completed"
    assert detail.turns[0].final_message_id == detail.messages[-1].id
    assert final_authority is not None
    assert final_authority.lease_state == "closed"
    assert final_authority.final_message_id == detail.messages[-1].id
    assert store.get_active_turn(session.id) is None
    assert all(
        mapping.active_turn_id is None
        for mapping in store.list_conversation_agent_sessions(session.id)
    )


def test_store_rejects_second_active_turn_and_can_abort_initial_submission(tmp_path: Path):
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    session = store.create_session(title="turn lifecycle")
    submission = store.begin_user_turn(session.id, "first")

    with pytest.raises(RuntimeError, match="active turn"):
        store.begin_user_turn(session.id, "second")

    store.abort_initial_turn(submission.turn.id)
    detail = store.get_session(session.id)
    assert detail is not None
    assert detail.turns == []
    assert detail.messages == []
    assert detail.events == []


def test_store_replays_same_invocation_before_active_turn_conflict(tmp_path: Path):
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    session = store.create_session(title="idempotent turn")

    first = store.begin_user_turn(
        session.id,
        "开始处理",
        invocation_id="navigation-request-1",
    )
    replay = store.begin_user_turn(
        session.id,
        "开始处理",
        invocation_id="navigation-request-1",
    )

    assert first.created is True
    assert replay.created is False
    assert replay.turn.id == first.turn.id
    assert replay.message.id == first.message.id
    assert replay.events == ()
    detail = store.get_session(session.id)
    assert detail is not None
    assert len(detail.turns) == 1
    assert len(detail.messages) == 1
    assert len(detail.events) == 1


def test_store_serializes_concurrent_replays_of_same_invocation(tmp_path: Path):
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    session = store.create_session(title="concurrent idempotent turn")

    def submit():
        return store.begin_user_turn(
            session.id,
            "开始处理",
            invocation_id="navigation-request-1",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        submissions = list(executor.map(lambda _index: submit(), range(2)))

    assert len({submission.turn.id for submission in submissions}) == 1
    assert sorted(submission.created for submission in submissions) == [False, True]
    detail = store.get_session(session.id)
    assert detail is not None
    assert len(detail.turns) == 1
    assert len(detail.messages) == 1


def test_store_migrates_invocation_id_for_existing_database(tmp_path: Path):
    db_path = tmp_path / "legacy.sqlite"
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
            CREATE TABLE web_turns (
                id TEXT PRIMARY KEY,
                web_session_id TEXT NOT NULL,
                origin TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                final_message_id TEXT
            )
            """
        )

    store = WebSessionStore(db_path)
    session = store.create_session(title="migrated")
    first = store.begin_user_turn(session.id, "开始处理", invocation_id="request-1")
    replay = store.begin_user_turn(session.id, "开始处理", invocation_id="request-1")

    with sqlite3.connect(db_path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(web_turns)")}
        indexes = {row[1] for row in connection.execute("PRAGMA index_list(web_turns)")}
    assert "invocation_id" in columns
    assert "idx_web_turns_session_invocation" in indexes
    assert replay.turn.id == first.turn.id


def test_system_reply_start_creates_turn_and_reply_without_active_user_turn(tmp_path: Path):
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    session = store.create_session(title="scheduled task")
    store.save_agentscope_session_mapping(
        session.id,
        agent_id="navigation-data-agent",
        agentscope_session_id="as-scheduled",
    )

    records = store.append_projected_event_batch(
        web_session_id=session.id,
        agentscope_session_id="as-scheduled",
        entry_id="1-0",
        raw_event_type="REPLY_START",
        reply_id="scheduled-reply",
        events=[],
    )

    detail = store.get_session(session.id)
    assert detail is not None
    assert [record.type for record in records] == ["turn_start"]
    assert len(detail.turns) == 1
    assert detail.turns[0].origin == "system"
    assert detail.turns[0].status == "running"


def test_recovery_reuses_pending_reply_lease_instead_of_stranding_turn(tmp_path: Path):
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    session = store.create_session(title="recovered reply")
    submission = store.begin_user_turn(session.id, "继续任务")
    turn_id = submission.turn.id
    store.save_agentscope_session_mapping(
        session.id,
        agent_id="navigation-data-agent",
        agentscope_session_id="as-navigation",
        active_turn_id=turn_id,
    )
    original = store.register_pending_reply(
        turn_id=turn_id,
        agentscope_session_id="as-navigation",
        agent_id="navigation-data-agent",
        source="user",
    )
    recovered = store.register_pending_reply(
        turn_id=turn_id,
        agentscope_session_id="as-navigation",
        agent_id="navigation-data-agent",
        source="wakeup-recovery",
    )

    assert recovered == original
    store.append_projected_event_batch(
        web_session_id=session.id,
        agentscope_session_id="as-navigation",
        entry_id="1-0",
        raw_event_type="REPLY_START",
        reply_id="reply-recovered",
        events=[],
    )
    records = store.append_projected_event_batch(
        web_session_id=session.id,
        agentscope_session_id="as-navigation",
        entry_id="2-0",
        raw_event_type="REPLY_END",
        reply_id="reply-recovered",
        events=[
            {
                "type": "reply_summary",
                "source": "agentscope",
                "run_id": "as-navigation",
                "payload": {"text": "恢复后任务已完成。"},
            }
        ],
    )

    assert [record.type for record in records] == ["final", "turn_state"]
    assert store.get_active_turn(session.id) is None


def test_async_reply_failure_closes_turn_when_no_other_work_can_continue(tmp_path: Path):
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    session = store.create_session(title="failed model run")
    submission = store.begin_user_turn(session.id, "开始处理")
    store.save_agentscope_session_mapping(
        session.id,
        agent_id="main-router-agent",
        agentscope_session_id="as-router",
        active_turn_id=submission.turn.id,
    )
    lease_id = store.register_pending_reply(
        turn_id=submission.turn.id,
        agentscope_session_id="as-router",
        agent_id="main-router-agent",
        source="user",
    )

    records = store.fail_reply_lease(lease_id)

    assert [record.type for record in records] == ["final", "turn_state"]
    assert records[-1].payload["status"] == "failed"
    detail = store.get_session(session.id)
    assert detail is not None
    assert detail.turns[0].status == "failed"
    assert detail.turns[0].final_message_id is not None
    assert store.get_agentscope_session_mapping(session.id).active_turn_id is None


def test_late_events_from_interrupted_reply_do_not_create_a_system_turn(tmp_path: Path):
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    session = store.create_session(title="interrupted reply")
    submission = store.begin_user_turn(session.id, "停止这个任务")
    store.save_agentscope_session_mapping(
        session.id,
        agent_id="navigation-data-agent",
        agentscope_session_id="as-navigation",
        active_turn_id=submission.turn.id,
    )
    store.register_pending_reply(
        turn_id=submission.turn.id,
        agentscope_session_id="as-navigation",
        agent_id="navigation-data-agent",
        source="user",
    )
    store.append_projected_event_batch(
        web_session_id=session.id,
        agentscope_session_id="as-navigation",
        entry_id="1-0",
        raw_event_type="REPLY_START",
        reply_id="reply-interrupted",
        events=[],
    )
    store.interrupt_active_turn(session.id)

    late = store.append_projected_event_batch(
        web_session_id=session.id,
        agentscope_session_id="as-navigation",
        entry_id="2-0",
        raw_event_type="REPLY_END",
        reply_id="reply-interrupted",
        events=[
            {
                "type": "reply_summary",
                "source": "agentscope",
                "run_id": "as-navigation",
                "payload": {"text": "这条迟到的回复不应进入新轮次。"},
            }
        ],
    )

    detail = store.get_session(session.id)
    assert late == []
    assert detail is not None
    assert len(detail.turns) == 1
    assert detail.turns[0].status == "interrupted"
    assert [message.role for message in detail.messages] == ["user"]


def test_contract_v1_interrupt_closes_authority_runs_and_agent_mapping(tmp_path: Path):
    db_path = tmp_path / "sessions.sqlite"
    store = WebSessionStore(db_path)
    session = store.create_session(title="interrupt contract v1", contract_version=1)
    turn = store.begin_user_turn(session.id, "停止当前请求").turn
    mapping = store.save_conversation_agent_session(
        session.id,
        agent_role="router",
        agent_id="main-router-agent",
        agentscope_session_id="as-router-v1",
    )
    store.bind_conversation_agent_session_to_turn(mapping.agentscope_session_id, turn.id)
    store.register_pending_reply(
        turn_id=turn.id,
        agentscope_session_id=mapping.agentscope_session_id,
        agent_id=mapping.agent_id,
        source="user",
    )
    store.create_turn_run(
        run_id="router-run-v1",
        turn_id=turn.id,
        producer="router",
        agentscope_session_id=mapping.agentscope_session_id,
    )

    records = store.interrupt_active_turn(session.id)

    assert [record.type for record in records] == ["turn_state"]
    detail = store.get_session(session.id)
    assert detail is not None
    assert detail.turns[0].status == "interrupted"
    assert store.get_active_turn(session.id) is None
    authority = store.get_response_authority(turn.id)
    assert authority is not None
    assert authority.lease_state == "closed"
    assert authority.final_message_id is None
    interrupted_run = store.get_latest_turn_run(turn.id, producer="router")
    assert interrupted_run is not None
    assert interrupted_run.status == "interrupted"
    updated_mapping = store.get_conversation_agent_session_by_agentscope_session(
        mapping.agentscope_session_id
    )
    assert updated_mapping is not None
    assert updated_mapping.active_turn_id is None
    with sqlite3.connect(db_path) as connection:
        reply_status = connection.execute(
            "SELECT status FROM agentscope_turn_replies WHERE turn_id = ?",
            (turn.id,),
        ).fetchone()
    assert reply_status == ("interrupted",)


def test_reconcile_terminal_turn_residues_repairs_pre_fix_interrupt_state(
    tmp_path: Path,
):
    db_path = tmp_path / "sessions.sqlite"
    store = WebSessionStore(db_path)
    session = store.create_session(title="stale interrupted turn", contract_version=1)
    turn = store.begin_user_turn(session.id, "处理导航数据").turn
    mapping = store.save_conversation_agent_session(
        session.id,
        agent_role="router",
        agent_id="main-router-agent",
        agentscope_session_id="as-router-stale",
    )
    store.bind_conversation_agent_session_to_turn(mapping.agentscope_session_id, turn.id)
    store.register_pending_reply(
        turn_id=turn.id,
        agentscope_session_id=mapping.agentscope_session_id,
        agent_id=mapping.agent_id,
        source="user",
    )
    store.create_turn_run(
        run_id="router-run-stale",
        turn_id=turn.id,
        producer="router",
        agentscope_session_id=mapping.agentscope_session_id,
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE web_turns SET status = 'interrupted', finished_at = 'now' WHERE id = ?",
            (turn.id,),
        )

    repaired = store.reconcile_terminal_turn_residues()

    assert repaired >= 4
    authority = store.get_response_authority(turn.id)
    assert authority is not None
    assert authority.lease_state == "closed"
    repaired_mapping = store.get_conversation_agent_session_by_agentscope_session(
        mapping.agentscope_session_id
    )
    assert repaired_mapping is not None
    assert repaired_mapping.active_turn_id is None
    repaired_run = store.get_latest_turn_run(turn.id, producer="router")
    assert repaired_run is not None
    assert repaired_run.status == "interrupted"
    with sqlite3.connect(db_path) as connection:
        reply_status = connection.execute(
            "SELECT status FROM agentscope_turn_replies WHERE turn_id = ?",
            (turn.id,),
        ).fetchone()
    assert reply_status == ("interrupted",)


def test_store_lists_all_agentscope_mappings_for_bridge_recovery(tmp_path: Path):
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    session = store.create_session(title="mapping recovery")
    store.save_agentscope_session_mapping(
        session.id,
        agent_id="main-router-agent",
        agentscope_session_id="as-main",
    )
    store.save_agentscope_session_mapping(
        session.id,
        agent_id="navigation-data-agent",
        agentscope_session_id="as-navigation",
    )

    mappings = store.list_agentscope_session_mappings()

    assert [mapping.agentscope_session_id for mapping in mappings] == [
        "as-main",
        "as-navigation",
    ]


def test_store_reconciles_orphaned_background_tool_once(tmp_path: Path):
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    session = store.create_session(title="background reconciliation")
    turn = store.begin_user_turn(session.id, "处理导航数据").turn
    binding = store.create_task_binding(
        session.id,
        task_id="task-private-background",
        task_ref="DP-BACKGROUND",
        navigation_session_id="as-navigation",
    ).binding
    store.bind_conversation_agent_session_to_turn(binding.navigation_session_id, turn.id)
    authority = store.get_response_authority(turn.id)
    assert authority is not None
    store.handover_response_authority(
        turn.id,
        expected_producer="router",
        expected_generation=authority.generation,
        new_producer="navigation",
    )
    store.append_projected_event_batch(
        web_session_id=session.id,
        agentscope_session_id="as-navigation",
        entry_id="10-0",
        events=[],
        private_events=[
            {
                "type": "tool_start",
                "payload": {
                    "tool": "extract_and_sync_navigation_data_tool",
                    "call_id": "call-background",
                },
            },
            {
                "type": "tool_background",
                "payload": {
                    "tool": "extract_and_sync_navigation_data_tool",
                    "call_id": "call-background",
                    "status": "background",
                },
            }
        ],
    )

    unresolved = store.list_unresolved_background_tools()
    first = store.append_background_tool_reconciliation(unresolved[0], status="failed")
    second = store.append_background_tool_reconciliation(unresolved[0], status="failed")

    assert first is None
    assert second is None
    assert store.list_unresolved_background_tools() == []
    detail = store.get_session(session.id)
    assert detail is not None
    assert "extract_and_sync_navigation_data_tool" not in detail.model_dump_json()


def test_store_deduplicates_human_decision_required_events(tmp_path: Path):
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    session = store.create_session(title="处理 20270605 的室外导航数据")
    event = {
        "type": "human_decision_required",
        "source": "NavigationDataAgent",
        "run_id": "as-session",
        "parent_run_id": None,
        "timestamp": "2026-06-26T10:00:00.000+00:00",
        "payload": {
            "reply_id": "reply-1",
            "tool_call_id": "tool-call-1",
            "request_id": "confirm_navigation_calibration_params:20270605",
            "summary": "请确认相机参数。",
        },
    }

    first = store.append_timeline_event(session.id, event)
    second = store.append_timeline_event(
        session.id,
        {
            **event,
            "timestamp": "2026-06-26T10:00:01.000+00:00",
        },
    )

    detail = store.get_session(session.id)

    assert detail is not None
    assert first.id == second.id
    assert [item.seq for item in detail.events] == [1]
    assert detail.events[0].payload["tool_call_id"] == "tool-call-1"


def test_store_refreshes_duplicate_human_decision_with_recovery_metadata(tmp_path: Path):
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    session = store.create_session(title="recover handoff")
    base = {
        "type": "human_decision_required",
        "source": "NavigationDataAgent",
        "run_id": "as-session",
        "payload": {
            "reply_id": "reply-1",
            "tool_call_id": "tool-call-1",
            "plan_id": "plan-1",
            "step_id": "confirm",
            "summary": "confirm",
        },
    }
    first = store.append_timeline_event(session.id, base)

    second = store.append_timeline_event(
        session.id,
        {
            **base,
            "payload": {
                **base["payload"],
                "recovery_required": True,
                "submission_disabled": True,
                "recovery_endpoint": (
                    f"/api/sessions/{session.id}/human-decisions/recovery"
                ),
            },
        },
    )

    detail = store.get_session(session.id)
    assert detail is not None
    assert first.id == second.id
    assert len(detail.events) == 1
    assert detail.events[0].payload["recovery_required"] is True
    assert detail.events[0].payload["submission_disabled"] is True


def test_store_marks_human_decision_consumed_idempotently(tmp_path: Path):
    store = WebSessionStore(tmp_path / "sessions.sqlite")

    assert (
        store.is_human_decision_consumed(
            agentscope_session_id="as-session",
            reply_id="reply-1",
            tool_call_id="tool-call-1",
        )
        is False
    )

    store.mark_human_decision_consumed(
        agentscope_session_id="as-session",
        reply_id="reply-1",
        tool_call_id="tool-call-1",
        action="confirm",
        request_id="confirm_navigation_calibration_params:20270605",
    )
    store.mark_human_decision_consumed(
        agentscope_session_id="as-session",
        reply_id="reply-1",
        tool_call_id="tool-call-1",
        action="confirm",
        request_id="confirm_navigation_calibration_params:20270605",
    )

    assert (
        store.is_human_decision_consumed(
            agentscope_session_id="as-session",
            reply_id="reply-1",
            tool_call_id="tool-call-1",
        )
        is True
    )


def test_store_rejects_message_for_missing_session(tmp_path: Path):
    store = WebSessionStore(tmp_path / "sessions.sqlite")

    with pytest.raises(KeyError):
        store.append_message("missing", role="user", content="hello")

    assert store.get_session("missing") is None


def test_store_orders_messages_deterministically_when_timestamps_collide(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr("vla_data_juicer_agents.web.session_store._now", lambda: "2026-06-26T10:00:00.000+00:00")
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
    monkeypatch.setattr("vla_data_juicer_agents.web.session_store._now", lambda: "2026-06-26T10:00:00.000+00:00")
    store = WebSessionStore(tmp_path / "sessions.sqlite")

    first = store.create_session(title="第一个任务")
    second = store.create_session(title="第二个任务")

    assert [session.id for session in store.list_sessions()] == [second.id, first.id]


def test_store_marks_previous_active_historical(tmp_path: Path):
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    first = store.create_session(title="第一个任务")
    second = store.create_session(title="第二个任务")

    store.mark_historical(first.id)

    assert store.get_session(first.id).status == "historical"
    assert store.get_session(second.id).status == "active"


def test_store_deletes_session_and_messages(tmp_path: Path):
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    session = store.create_session(title="处理 20270605 的室外导航数据")
    store.append_message(session.id, role="user", content="处理 20270605")

    store.delete_session(session.id)

    assert store.get_session(session.id) is None
    assert store.list_sessions() == []


def test_store_rejects_delete_for_missing_session(tmp_path: Path):
    store = WebSessionStore(tmp_path / "sessions.sqlite")

    with pytest.raises(KeyError):
        store.delete_session("missing")


def test_store_rejects_mark_historical_for_missing_session(tmp_path: Path):
    store = WebSessionStore(tmp_path / "sessions.sqlite")

    with pytest.raises(KeyError):
        store.mark_historical("missing")

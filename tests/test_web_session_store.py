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

    assert [record.type for record in records] == ["turn_state"]
    assert records[0].payload["status"] == "failed"
    detail = store.get_session(session.id)
    assert detail is not None
    assert detail.turns[0].status == "failed"
    assert detail.turns[0].final_message_id is None
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
    store.save_agentscope_session_mapping(
        session.id,
        agent_id="navigation-data-agent",
        agentscope_session_id="as-navigation",
    )
    store.append_projected_event_batch(
        web_session_id=session.id,
        agentscope_session_id="as-navigation",
        entry_id="10-0",
        events=[
            {
                "type": "tool_background",
                "source": "agentscope",
                "run_id": "as-navigation",
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

    assert first is not None
    assert first.payload == {
        "tool": "extract_and_sync_navigation_data_tool",
        "call_id": "call-background",
        "status": "failed",
    }
    assert second is None
    assert store.list_unresolved_background_tools() == []


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

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

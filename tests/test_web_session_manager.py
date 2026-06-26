from pathlib import Path

import pytest

from vla_data_juicer_agents.web.session_manager import WebSessionManager
from vla_data_juicer_agents.web.session_store import WebSessionStore


class FakeController:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.started = False
        self.submitted = []
        self.interrupts = 0
        self.is_running = False

    def start(self):
        self.started = True

    def submit_turn(self, message):
        self.submitted.append(message)

    def request_interrupt(self):
        self.interrupts += 1
        return True

    def drain_events(self):
        return []


class FailingStartController(FakeController):
    def start(self):
        raise RuntimeError("start failed")


class RejectingSubmitController(FakeController):
    def submit_turn(self, message):
        raise RuntimeError("turn rejected")


def test_create_session_starts_controller(tmp_path: Path):
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    manager = WebSessionManager(
        store=store,
        working_dir=str(tmp_path / ".djx"),
        model="qwen-test",
        controller_factory=FakeController,
    )

    session = manager.create_session("处理 20270605")

    controller = manager.get_controller(session.id)
    assert controller.started is True
    assert controller.kwargs["working_dir"] == str(tmp_path / ".djx" / session.id)
    assert controller.kwargs["model"] == "qwen-test"


def test_submit_turn_appends_user_message_and_calls_controller(tmp_path: Path):
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    manager = WebSessionManager(store=store, working_dir=str(tmp_path), controller_factory=FakeController)
    session = manager.create_session("处理 20270605")

    turn_id = manager.submit_turn(session.id, "开始处理")

    assert turn_id.startswith("turn_")
    assert manager.get_controller(session.id).submitted == ["开始处理"]
    assert store.get_session(session.id).messages[0].role == "user"
    assert store.get_session(session.id).messages[0].content == "开始处理"


def test_interrupt_requests_controller_interrupt(tmp_path: Path):
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    manager = WebSessionManager(store=store, working_dir=str(tmp_path), controller_factory=FakeController)
    session = manager.create_session("处理 20270605")

    assert manager.interrupt(session.id) is True
    assert manager.get_controller(session.id).interrupts == 1


def test_mark_historical_updates_store_and_removes_controller(tmp_path: Path):
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    manager = WebSessionManager(store=store, working_dir=str(tmp_path), controller_factory=FakeController)
    session = manager.create_session("处理 20270605")

    manager.mark_historical(session.id)

    assert store.get_session(session.id).status == "historical"
    with pytest.raises(KeyError):
        manager.get_controller(session.id)


def test_create_session_rolls_back_when_controller_start_fails(tmp_path: Path):
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    manager = WebSessionManager(store=store, working_dir=str(tmp_path), controller_factory=FailingStartController)

    with pytest.raises(RuntimeError, match="start failed"):
        manager.create_session("处理 20270605")

    assert store.list_sessions() == []


def test_submit_turn_rejection_does_not_append_user_message(tmp_path: Path):
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    manager = WebSessionManager(store=store, working_dir=str(tmp_path), controller_factory=RejectingSubmitController)
    session = manager.create_session("处理 20270605")

    with pytest.raises(RuntimeError, match="turn rejected"):
        manager.submit_turn(session.id, "开始处理")

    assert store.get_session(session.id).messages == []


def test_submit_turn_rejects_unknown_session(tmp_path: Path):
    manager = WebSessionManager(
        store=WebSessionStore(tmp_path / "sessions.sqlite"),
        working_dir=str(tmp_path),
        controller_factory=FakeController,
    )

    with pytest.raises(KeyError):
        manager.submit_turn("missing", "hello")

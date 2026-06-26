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
    assert store.get_session(session.id).messages[0].content == "开始处理"


def test_submit_turn_rejects_unknown_session(tmp_path: Path):
    manager = WebSessionManager(
        store=WebSessionStore(tmp_path / "sessions.sqlite"),
        working_dir=str(tmp_path),
        controller_factory=FakeController,
    )

    with pytest.raises(KeyError):
        manager.submit_turn("missing", "hello")

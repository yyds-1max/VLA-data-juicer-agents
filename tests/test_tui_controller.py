import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from vla_data_juicer_agents.tui.controller import SessionController


def wait_until_idle(controller: SessionController, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while controller.is_running and time.monotonic() < deadline:
        time.sleep(0.01)
    assert controller.is_running is False


def run_turn(controller: SessionController, message: str):
    controller.submit_turn(message)
    wait_until_idle(controller)
    return controller.consume_turn_result()


class FakeAgent:
    def __init__(self, event_callback):
        self.event_callback = event_callback
        self.messages = []
        self.interrupt_calls = 0

    def prepare_turn(self):
        pass

    def handle_message(self, message):
        self.messages.append(message)
        self.event_callback(
            {
                "type": "final",
                "source": "main",
                "run_id": f"run_{len(self.messages)}",
                "parent_run_id": None,
                "timestamp": "2026-06-22T00:00:00+00:00",
                "payload": {"text": message, "stop": False},
            }
        )
        return SimpleNamespace(text=message, stop=False, interrupted=False)

    def request_interrupt(self, *, allow_pending=False):
        del allow_pending
        self.interrupt_calls += 1
        return True


def test_controller_start_is_idempotent_and_reuses_one_agent_for_two_turns():
    created = []

    def factory(**kwargs):
        agent = FakeAgent(kwargs["event_callback"])
        created.append((agent, kwargs))
        return agent

    controller = SessionController(
        working_dir="/tmp/session",
        model="qwen-plus",
        agent_factory=factory,
    )

    controller.start()
    controller.start()
    first = run_turn(controller, "first")
    second = run_turn(controller, "second")

    assert len(created) == 1
    agent, kwargs = created[0]
    assert kwargs["use_llm_router"] is True
    assert kwargs["working_dir"] == "/tmp/session"
    assert kwargs["model"] == "qwen-plus"
    assert agent.messages == ["first", "second"]
    assert first.text == "first"
    assert second.text == "second"


def test_controller_keeps_legacy_agent_factory_interrupt_api_compatible():
    class LegacyAgent:
        def __init__(self, event_callback):
            self.event_callback = event_callback
            self.interrupted = False

        def handle_message(self, message):
            return SimpleNamespace(text=message, stop=False, interrupted=self.interrupted)

        def request_interrupt(self):
            self.interrupted = True
            return True

    controller = SessionController(agent_factory=lambda **kwargs: LegacyAgent(kwargs["event_callback"]))
    controller.start()

    assert run_turn(controller, "legacy").text == "legacy"


def test_controller_drains_copied_events_and_consumes_result_once():
    source_event = {
        "type": "reasoning",
        "payload": {"summary": "working", "steps": [{"text": "inspect"}]},
    }

    class EventAgent(FakeAgent):
        def handle_message(self, message):
            self.event_callback(source_event)
            source_event["type"] = "mutated"
            source_event["payload"]["summary"] = "mutated"
            source_event["payload"]["steps"][0]["text"] = "mutated"
            source_event["payload"]["steps"].append({"text": "extra"})
            return SimpleNamespace(text=message, stop=False, interrupted=False)

    controller = SessionController(agent_factory=lambda **kwargs: EventAgent(kwargs["event_callback"]))
    controller.start()
    controller.submit_turn("run")
    wait_until_idle(controller)

    assert controller.drain_events() == [
        {
            "type": "reasoning",
            "payload": {"summary": "working", "steps": [{"text": "inspect"}]},
        }
    ]
    assert controller.drain_events() == []
    assert controller.consume_turn_result().text == "run"
    with pytest.raises(RuntimeError, match="No completed turn result"):
        controller.consume_turn_result()


def test_controller_rejects_a_second_active_turn():
    release = threading.Event()

    class BlockingAgent(FakeAgent):
        def handle_message(self, message):
            release.wait(timeout=2)
            return SimpleNamespace(text=message, stop=False, interrupted=False)

    controller = SessionController(agent_factory=lambda **kwargs: BlockingAgent(kwargs["event_callback"]))
    controller.start()
    controller.submit_turn("first")

    with pytest.raises(RuntimeError, match="already active"):
        controller.submit_turn("second")

    release.set()
    wait_until_idle(controller)
    assert controller.consume_turn_result().text == "first"


def test_controller_rejects_new_turn_until_completed_result_is_consumed():
    controller = SessionController(agent_factory=lambda **kwargs: FakeAgent(kwargs["event_callback"]))
    controller.start()
    controller.submit_turn("first")
    wait_until_idle(controller)

    with pytest.raises(RuntimeError, match="Consume the completed turn result"):
        controller.submit_turn("second")

    assert controller.consume_turn_result().text == "first"
    assert run_turn(controller, "second").text == "second"


def test_request_interrupt_targets_running_turn_only():
    started = threading.Event()
    release = threading.Event()

    class BlockingAgent(FakeAgent):
        def handle_message(self, message):
            started.set()
            release.wait(timeout=2)
            return SimpleNamespace(text=message, stop=False, interrupted=True)

    agent_holder = {}

    def factory(**kwargs):
        agent_holder["agent"] = BlockingAgent(kwargs["event_callback"])
        return agent_holder["agent"]

    controller = SessionController(agent_factory=factory)
    controller.start()
    assert controller.request_interrupt() is False

    controller.submit_turn("run")
    assert started.wait(timeout=1)
    assert controller.request_interrupt() is True
    assert agent_holder["agent"].interrupt_calls == 1

    release.set()
    wait_until_idle(controller)
    assert controller.request_interrupt() is False


def test_request_interrupt_during_agent_startup_is_accepted_and_delivered():
    entered_handle = threading.Event()
    allow_install = threading.Event()
    interrupt_delivered = threading.Event()

    class StartingAgent(FakeAgent):
        def __init__(self, event_callback):
            super().__init__(event_callback)
            self.pending_interrupt = False

        def handle_message(self, message):
            entered_handle.set()
            assert allow_install.wait(timeout=2)
            if self.pending_interrupt:
                interrupt_delivered.set()
            return SimpleNamespace(text=message, stop=False, interrupted=True)

        def request_interrupt(self, *, allow_pending=False):
            assert allow_pending is True
            self.pending_interrupt = True
            return True

    controller = SessionController(agent_factory=lambda **kwargs: StartingAgent(kwargs["event_callback"]))
    controller.start()
    controller.submit_turn("run")
    assert entered_handle.wait(timeout=1)

    assert controller.request_interrupt() is True
    allow_install.set()
    wait_until_idle(controller)

    assert interrupt_delivered.is_set()
    assert controller.consume_turn_result().interrupted is True


def test_worker_exception_emits_one_final_event_and_keeps_controller_reusable():
    class FailingOnceAgent(FakeAgent):
        def handle_message(self, message):
            self.messages.append(message)
            if len(self.messages) == 1:
                raise RuntimeError("model disconnected")
            return super().handle_message(message)

    controller = SessionController(agent_factory=lambda **kwargs: FailingOnceAgent(kwargs["event_callback"]))
    controller.start()

    failed = run_turn(controller, "first")
    events = controller.drain_events()

    assert failed.stop is False
    assert failed.text == "Session turn failed: model disconnected"
    assert [(event["type"], event["source"]) for event in events] == [("final", "main")]
    assert events[0]["payload"] == {"text": failed.text, "stop": False}

    recovered = run_turn(controller, "second")
    assert recovered.text == "second"


def test_worker_cancelled_error_still_produces_one_final_and_consumable_result():
    class CancelledAgent(FakeAgent):
        def handle_message(self, message):
            raise asyncio.CancelledError

    controller = SessionController(agent_factory=lambda **kwargs: CancelledAgent(kwargs["event_callback"]))
    controller.start()
    controller.submit_turn("cancel")
    wait_until_idle(controller)

    result = controller.consume_turn_result()
    events = controller.drain_events()

    assert result.text == "Session turn failed: CancelledError"
    assert result.stop is False
    assert result.interrupted is False
    assert [event["type"] for event in events] == ["final"]
    assert events[0]["payload"] == {"text": result.text, "stop": False}


def test_exception_after_final_keeps_displayed_result_without_duplicate_final():
    class FinalThenFailAgent(FakeAgent):
        def handle_message(self, message):
            self.messages.append(message)
            if len(self.messages) == 1:
                self.event_callback(
                    {
                        "type": "final",
                        "source": "main",
                        "run_id": "run_1",
                        "parent_run_id": None,
                        "timestamp": "2026-06-22T00:00:00+00:00",
                        "payload": {"text": "already displayed", "stop": False},
                    }
                )
                raise RuntimeError("late cleanup failure")
            return super().handle_message(message)

    controller = SessionController(agent_factory=lambda **kwargs: FinalThenFailAgent(kwargs["event_callback"]))
    controller.start()

    result = run_turn(controller, "first")
    events = controller.drain_events()

    assert result.text == "already displayed"
    assert result.stop is False
    assert [event["type"] for event in events] == ["final"]
    assert run_turn(controller, "second").text == "second"

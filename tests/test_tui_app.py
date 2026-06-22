import io
import re
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from rich.console import Console

from vla_data_juicer_agents.tui.app import (
    _ThinkingSpinner,
    _render_timeline_item,
    run_tui_session,
)
from vla_data_juicer_agents.tui.models import TimelineItem


def event(
    event_type: str,
    source: str = "main",
    *,
    run_id: str = "run_1",
    parent_run_id: str | None = None,
    timestamp: datetime | None = None,
    **payload,
):
    return {
        "type": event_type,
        "source": source,
        "run_id": run_id,
        "parent_run_id": parent_run_id,
        "timestamp": (timestamp or datetime.now(timezone.utc)).isoformat(),
        "payload": payload,
    }


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class FakeController:
    instances = []

    def __init__(self, *, working_dir="./.djx", model=None):
        self.working_dir = working_dir
        self.model = model
        self.started = False
        self.messages = []
        self.events_by_message = []
        self.results = []
        self.running_polls = []
        self.interrupt_calls = 0
        FakeController.instances.append(self)

    def start(self):
        self.started = True

    @property
    def is_running(self):
        if self.running_polls:
            return self.running_polls.pop(0)
        return False

    def submit_turn(self, message):
        self.messages.append(message)

    def drain_events(self):
        if self.events_by_message:
            return self.events_by_message.pop(0)
        return []

    def consume_turn_result(self):
        if self.results:
            return self.results.pop(0)
        return SimpleNamespace(text="", stop=False, interrupted=False)

    def request_interrupt(self):
        self.interrupt_calls += 1
        return True


def console_for(stream):
    return Console(file=stream, force_terminal=True, color_system="standard", width=120)


def strip_ansi(text):
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)


def test_thinking_spinner_ticks_every_350ms_and_clear_erases_line():
    stream = io.StringIO()
    clock = FakeClock()
    spinner = _ThinkingSpinner(stream=stream, monotonic=clock.monotonic)

    spinner.tick("thinking")
    spinner.tick("too soon")
    clock.now = 0.35
    spinner.tick("thinking")
    spinner.clear()

    assert stream.getvalue() == "\r| thinking\r/ thinking\r          \r"


def test_timeline_rendering_styles_reasoning_tools_agents_and_final_reply():
    stream = io.StringIO()
    console = console_for(stream)
    started = datetime(2026, 6, 22, 10, 0, tzinfo=timezone.utc)

    items = [
        TimelineItem(kind="reasoning", source_label="Plan", text="Inspect raw clips.", timestamp=started),
        TimelineItem(
            kind="tool",
            source_label="Executor",
            tool="prepare_raw_data_tool",
            status="completed",
            text="prepared 2 clips",
            elapsed_sec=1.25,
            timestamp=started + timedelta(seconds=1),
        ),
        TimelineItem(kind="agent", source_label="Workflow", text="completed", status="completed"),
        TimelineItem(kind="assistant", source_label="Main", text="Done."),
    ]

    for item in items:
        _render_timeline_item(console, item)

    output = stream.getvalue()
    plain = strip_ansi(output)
    assert "[Plan] Inspect raw clips." in plain
    assert "● completed prepare_raw_data_tool 1.2s prepared 2 clips" in plain
    assert "[Workflow] completed" in plain
    assert "Done." in plain
    assert "\x1b[" in output


def test_one_shot_submits_one_turn_renders_final_once_and_does_not_echo_result_text():
    stream = io.StringIO()

    class OneShotController(FakeController):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.events_by_message = [
                [],
                [event("final", text="rendered final", stop=True)],
            ]
            self.running_polls = [True, False]
            self.results = [SimpleNamespace(text="raw duplicate", stop=True, interrupted=False)]

    args = SimpleNamespace(message="run once", working_dir=".djx-test", model="qwen-plus")

    code = run_tui_session(
        args,
        controller_factory=OneShotController,
        read_line=lambda prompt="": (_ for _ in ()).throw(AssertionError("read_line called")),
        console=console_for(stream),
    )

    assert code == 0
    controller = OneShotController.instances[-1]
    assert controller.started is True
    assert controller.messages == ["run once"]
    output = strip_ansi(stream.getvalue())
    assert output.count("rendered final") == 1
    assert "raw duplicate" not in output


def test_one_shot_blank_message_is_rejected_before_controller_construction(capsys):
    FakeController.instances.clear()
    args = SimpleNamespace(message="   ", working_dir=".djx-test", model=None)

    code = run_tui_session(args, controller_factory=FakeController)

    assert code == 2
    assert FakeController.instances == []
    assert "message must not be empty" in capsys.readouterr().err


def test_interactive_eof_returns_zero():
    FakeController.instances.clear()
    args = SimpleNamespace(message=None, working_dir=".djx-test", model=None)

    code = run_tui_session(
        args,
        controller_factory=FakeController,
        read_line=lambda prompt="": (_ for _ in ()).throw(EOFError),
        console=console_for(io.StringIO()),
    )

    assert code == 0
    assert FakeController.instances[-1].started is True


def test_keyboard_interrupt_while_idle_prints_no_running_task_hint():
    FakeController.instances.clear()
    stream = io.StringIO()
    args = SimpleNamespace(message=None, working_dir=".djx-test", model=None)
    inputs = iter([KeyboardInterrupt, EOFError])

    def read_line(prompt=""):
        value = next(inputs)
        raise value

    code = run_tui_session(
        args,
        controller_factory=FakeController,
        read_line=read_line,
        console=console_for(stream),
    )

    assert code == 0
    assert "No running task to interrupt" in strip_ansi(stream.getvalue())
    assert FakeController.instances[-1].interrupt_calls == 0


def test_keyboard_interrupt_while_turn_runs_requests_interrupt_once():
    FakeController.instances.clear()
    stream = io.StringIO()
    started = threading.Event()
    release = threading.Event()

    class BlockingConsole(Console):
        def print(self, *args, **kwargs):
            text = "".join(str(arg) for arg in args)
            if "working" in text:
                started.set()
                release.wait(timeout=1)
                raise KeyboardInterrupt
            return super().print(*args, **kwargs)

    class RunningController(FakeController):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.running = True
            self.results = [SimpleNamespace(text="interrupted", stop=False, interrupted=True)]

        @property
        def is_running(self):
            return self.running

        def drain_events(self):
            return [event("reasoning", "navigation.plan", summary="working")]

        def request_interrupt(self):
            super().request_interrupt()
            self.running = False
            return True

    args = SimpleNamespace(message=None, working_dir=".djx-test", model=None)
    inputs = iter(["do work", EOFError])

    def read_line(prompt=""):
        value = next(inputs)
        if value is EOFError:
            raise EOFError
        return value

    thread = threading.Thread(
        target=lambda: run_tui_session(
            args,
            controller_factory=RunningController,
            read_line=read_line,
            console=BlockingConsole(file=stream, force_terminal=True),
        )
    )
    thread.start()
    assert started.wait(timeout=1)
    controller = FakeController.instances[-1]
    release.set()
    thread.join(timeout=1)

    assert controller.messages == ["do work"]
    assert controller.interrupt_calls == 1
    assert not thread.is_alive()


@pytest.mark.parametrize("exit_word", ["exit", "quit", "q", "退出"])
def test_exit_words_are_submitted_and_only_stop_after_final_stop(exit_word):
    FakeController.instances.clear()
    stream = io.StringIO()

    class ExitController(FakeController):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.events_by_message = [
                [event("final", text="not yet", stop=False)],
                [event("final", run_id="run_2", text="stopped", stop=True)],
            ]
            self.results = [
                SimpleNamespace(text="not yet raw", stop=False, interrupted=False),
                SimpleNamespace(text="stopped raw", stop=True, interrupted=False),
            ]

    args = SimpleNamespace(message=None, working_dir=".djx-test", model=None)
    inputs = iter([exit_word, "continue", AssertionError("should stop before another prompt")])

    def read_line(prompt=""):
        value = next(inputs)
        if isinstance(value, BaseException):
            raise value
        return value

    code = run_tui_session(
        args,
        controller_factory=ExitController,
        read_line=read_line,
        console=console_for(stream),
    )

    controller = FakeController.instances[-1]
    assert code == 0
    assert controller.messages == [exit_word, "continue"]
    assert "stopped" in strip_ansi(stream.getvalue())

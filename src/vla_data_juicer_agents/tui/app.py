from __future__ import annotations

import sys
import time
from argparse import Namespace
from collections.abc import Callable
from typing import Any, TextIO

from rich.console import Console
from rich.text import Text

from vla_data_juicer_agents.tui.controller import SessionController
from vla_data_juicer_agents.tui.event_adapter import apply_event
from vla_data_juicer_agents.tui.models import TimelineItem, TuiState


SOURCE_COLORS = {
    "Main": "magenta",
    "Workflow": "yellow",
    "Plan": "blue",
    "Executor": "bright_red",
}

_EXIT_WORDS = {"exit", "quit", "q", "退出"}


class _ThinkingSpinner:
    _FRAMES = ("|", "/", "-", "\\")

    def __init__(
        self,
        *,
        stream: TextIO | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._stream = stream or sys.stderr
        self._monotonic = monotonic
        self._next_frame = 0
        self._last_tick: float | None = None
        self._last_width = 0

    def tick(self, text: str) -> None:
        now = self._monotonic()
        if self._last_tick is not None and now - self._last_tick < 0.35:
            return
        self._last_tick = now
        frame = self._FRAMES[self._next_frame]
        self._next_frame = (self._next_frame + 1) % len(self._FRAMES)
        rendered = f"{frame} {text}".rstrip()
        self._last_width = max(self._last_width, len(rendered))
        self._stream.write(f"\r{rendered}")
        self._stream.flush()

    def clear(self) -> None:
        if self._last_width:
            self._stream.write("\r" + (" " * self._last_width) + "\r")
            self._stream.flush()
            self._last_width = 0
        self._last_tick = None


def _source_style(label: str) -> str:
    return SOURCE_COLORS.get(label, "white")


def _bounded(text: str, *, limit: int = 160) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"


def _status_bullet(status: str | None) -> tuple[str, str]:
    normalized = (status or "completed").lower()
    if normalized in {"completed", "success", "succeeded", "ok"}:
        return "●", "green"
    if normalized in {"failed", "error", "cancelled", "interrupted"}:
        return "●", "red"
    return "●", "yellow"


def _render_source(text: Text, label: str) -> None:
    text.append("[", style="dim")
    text.append(label, style=_source_style(label))
    text.append("] ", style="dim")


def _render_timeline_item(console: Console, item: TimelineItem) -> None:
    label = item.source_label
    if item.kind == "reasoning":
        line = Text()
        _render_source(line, label)
        line.append(item.text)
        console.print(line)
        return

    if item.kind == "tool":
        status = item.status or "completed"
        bullet, bullet_style = _status_bullet(status)
        line = Text()
        line.append(bullet, style=bullet_style)
        line.append(f" {status}")
        if item.tool:
            line.append(f" {item.tool}", style="bold")
        if item.elapsed_sec is not None:
            line.append(f" {item.elapsed_sec:.1f}s", style="dim")
        summary = _bounded(item.text)
        if summary:
            line.append(f" {summary}")
        console.print(line)
        return

    if item.kind == "agent":
        line = Text()
        _render_source(line, label)
        line.append(item.text or item.status or "completed")
        console.print(line)
        return

    if item.kind == "assistant":
        console.print(Text(item.text, style="cyan"))
        return

    line = Text()
    _render_source(line, label)
    line.append(item.text)
    console.print(line, style="dim")


def _controller_kwargs(args: Namespace) -> dict[str, Any]:
    return {
        "working_dir": getattr(args, "working_dir", "./.djx"),
        "model": getattr(args, "model", None),
    }


def _is_initialization_failure(result: Any) -> bool:
    text = str(getattr(result, "text", ""))
    return text.startswith("Session turn failed:")


def _flush_events(
    *,
    controller: Any,
    state: TuiState,
    console: Console,
    spinner: _ThinkingSpinner,
    rendered_count: int,
) -> int:
    events = controller.drain_events()
    for event in events:
        before = len(state.timeline)
        apply_event(state, event)
        if len(state.timeline) > before:
            spinner.clear()
            for item in state.timeline[max(rendered_count, before) :]:
                _render_timeline_item(console, item)
            rendered_count = len(state.timeline)
    return rendered_count


def _run_turn(
    *,
    controller: Any,
    message: str,
    state: TuiState,
    console: Console,
    spinner: _ThinkingSpinner,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> Any:
    rendered_count = len(state.timeline)
    active_text: str | None = None
    active_since: float | None = None
    interrupt_requested = False
    controller.submit_turn(message)
    try:
        while controller.is_running:
            try:
                rendered_count = _flush_events(
                    controller=controller,
                    state=state,
                    console=console,
                    spinner=spinner,
                    rendered_count=rendered_count,
                )
                text = state.spinner_text()
                if text:
                    now = monotonic()
                    if text != active_text:
                        active_text = text
                        active_since = now
                    elapsed = int(now - (active_since if active_since is not None else now))
                    spinner.tick(f"{text} (+{elapsed}s)")
                else:
                    active_text = None
                    active_since = None
                    spinner.clear()
                sleep(0.03)
            except KeyboardInterrupt:
                if not interrupt_requested and controller.is_running:
                    controller.request_interrupt()
                    interrupt_requested = True
        rendered_count = _flush_events(
            controller=controller,
            state=state,
            console=console,
            spinner=spinner,
            rendered_count=rendered_count,
        )
        result = controller.consume_turn_result()
        spinner.clear()
        return result
    except KeyboardInterrupt:
        spinner.clear()
        console.print("No running task to interrupt.")
        return None


def run_tui_session(
    args: Namespace,
    *,
    controller_factory: Callable[..., Any] = SessionController,
    read_line: Callable[[str], str] = input,
    console: Console | None = None,
) -> int:
    if getattr(args, "message", None) is not None and not str(args.message).strip():
        print("message must not be empty", file=sys.stderr)
        return 2

    console = console or Console()
    spinner = _ThinkingSpinner(stream=console.file)
    state = TuiState()
    try:
        controller = controller_factory(**_controller_kwargs(args))
        controller.start()
    except Exception as exc:
        print(f"Failed to start vla-data-agent session: {exc}", file=sys.stderr)
        return 2

    if getattr(args, "message", None) is not None:
        result = _run_turn(
            controller=controller,
            message=str(args.message).strip(),
            state=state,
            console=console,
            spinner=spinner,
        )
        if result is None:
            return 0
        return 2 if _is_initialization_failure(result) else 0

    console.print("VLA data agent started. Describe your task in natural language. Type `help` or `exit`.")
    while True:
        try:
            message = read_line("you> ")
        except EOFError:
            spinner.clear()
            console.print("Session ended.")
            return 0
        except KeyboardInterrupt:
            spinner.clear()
            console.print("No running task to interrupt.")
            continue

        stripped = str(message).strip()
        if not stripped:
            continue
        result = _run_turn(
            controller=controller,
            message=stripped,
            state=state,
            console=console,
            spinner=spinner,
        )
        if result is None:
            continue
        if bool(getattr(result, "stop", False)) or (
            stripped.lower() in _EXIT_WORDS and state.stop
        ):
            return 0

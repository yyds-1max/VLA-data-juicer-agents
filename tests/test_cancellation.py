import asyncio
import subprocess
import sys
import threading
import time

import pytest

from vla_data_juicer_agents.core.cancellation import (
    CancellationContext,
    TurnCancelled,
    bind_cancellation,
    current_cancellation,
)
from vla_data_juicer_agents.navigation.subprocess_runner import run_command


class InterruptibleAgent:
    def __init__(self) -> None:
        self.interrupt_count = 0
        self.interrupted = asyncio.Event()

    async def interrupt(self) -> None:
        self.interrupt_count += 1
        self.interrupted.set()


def test_cancel_interrupts_each_registered_agent_once() -> None:
    async def exercise() -> None:
        cancellation = CancellationContext()
        agents = [InterruptibleAgent(), InterruptibleAgent()]

        async with cancellation.track_agent(agents[0]):
            async with cancellation.track_agent(agents[1]):
                assert cancellation.cancel() is True
                assert cancellation.cancel() is False
                await asyncio.gather(*(agent.interrupted.wait() for agent in agents))

        assert [agent.interrupt_count for agent in agents] == [1, 1]

    asyncio.run(exercise())


def test_binding_exposes_context_and_cancelled_context_raises() -> None:
    cancellation = CancellationContext()

    assert current_cancellation() is None
    with bind_cancellation(cancellation):
        assert current_cancellation() is cancellation
        assert cancellation.cancelled is False
        cancellation.raise_if_cancelled()
        assert cancellation.cancel() is True
        assert cancellation.cancelled is True
        with pytest.raises(TurnCancelled):
            cancellation.raise_if_cancelled()

    assert current_cancellation() is None


def test_track_agent_rejects_already_cancelled_context() -> None:
    async def exercise() -> None:
        cancellation = CancellationContext()
        cancellation.cancel()

        with pytest.raises(TurnCancelled):
            async with cancellation.track_agent(InterruptibleAgent()):
                pytest.fail("cancelled context yielded")

    asyncio.run(exercise())


def test_run_command_can_be_cancelled_promptly() -> None:
    cancellation = CancellationContext()
    timer = threading.Timer(0.2, cancellation.cancel)
    started = time.monotonic()

    timer.start()
    try:
        with bind_cancellation(cancellation):
            with pytest.raises(TurnCancelled):
                run_command([sys.executable, "-c", "import time; time.sleep(30)"])
    finally:
        timer.cancel()

    assert time.monotonic() - started < 5


def test_run_command_preserves_timeout_error() -> None:
    with pytest.raises(subprocess.TimeoutExpired):
        run_command(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            timeout_seconds=0.1,
        )


def test_timeout_kills_descendants_that_ignore_sigterm() -> None:
    descendant_code = (
        "import signal, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "time.sleep(5)"
    )
    parent_code = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {descendant_code!r}]); "
        "time.sleep(5)"
    )
    started = time.monotonic()

    with pytest.raises(subprocess.TimeoutExpired):
        run_command([sys.executable, "-c", parent_code], timeout_seconds=0.1)

    assert time.monotonic() - started < 3

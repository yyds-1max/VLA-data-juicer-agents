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


def test_cancel_cancels_active_task_for_agent_without_interrupt() -> None:
    async def exercise() -> None:
        cancellation = CancellationContext()
        started = asyncio.Event()

        async def worker() -> str:
            try:
                async with cancellation.track_agent(object()):
                    started.set()
                    await asyncio.Future()
            except asyncio.CancelledError:
                return "cancelled"
            return "completed"

        task = asyncio.create_task(worker())
        await started.wait()

        assert cancellation.cancel() is True
        assert await task == "cancelled"

    asyncio.run(exercise())


def test_cancel_cancels_each_registered_task_once() -> None:
    async def exercise() -> None:
        cancellation = CancellationContext()
        started = [asyncio.Event(), asyncio.Event()]

        async def worker(index: int) -> None:
            async with cancellation.track_agent(object()):
                started[index].set()
                await asyncio.Future()

        tasks = [asyncio.create_task(worker(index)) for index in range(2)]
        await asyncio.gather(*(event.wait() for event in started))

        assert cancellation.cancel() is True
        assert cancellation.cancel() is False
        results = await asyncio.gather(*tasks, return_exceptions=True)

        assert all(isinstance(result, asyncio.CancelledError) for result in results)

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
            async with cancellation.track_agent(object()):
                pytest.fail("cancelled context yielded")

    asyncio.run(exercise())


def test_nested_registration_keeps_agent_tracked_until_outer_exit() -> None:
    async def exercise() -> None:
        cancellation = CancellationContext()
        ready = asyncio.Event()

        async def worker() -> None:
            agent = object()
            async with cancellation.track_agent(agent):
                async with cancellation.track_agent(agent):
                    async with cancellation.track_agent(agent):
                        pass
                    ready.set()
                    await asyncio.Future()

        task = asyncio.create_task(worker())
        await ready.wait()
        assert cancellation.cancel() is True
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())


def test_cancel_handles_loop_close_during_scheduling(monkeypatch) -> None:
    async def exercise() -> None:
        cancellation = CancellationContext()
        loop = asyncio.get_running_loop()

        def fail_scheduling(_callback, *_args):
            raise RuntimeError("event loop is closed")

        monkeypatch.setattr(loop, "call_soon_threadsafe", fail_scheduling)
        async with cancellation.track_agent(object()):
            assert cancellation.cancel() is True

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
        "time.sleep(8)"
    )
    parent_code = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {descendant_code!r}]); "
        "time.sleep(8)"
    )
    started = time.monotonic()

    with pytest.raises(subprocess.TimeoutExpired):
        run_command([sys.executable, "-c", parent_code], timeout_seconds=1.0)

    assert time.monotonic() - started < 4

from __future__ import annotations

import asyncio
from collections import defaultdict
from types import SimpleNamespace

import pytest

from vla_data_juicer_agents.core.cancellation import CancellationContext
from vla_data_juicer_agents.runtime.stop_coordinator import OwnerLease, StopCoordinator
from vla_data_juicer_agents.runtime.agentscope_runtime import AgentScopeRuntime
from vla_data_juicer_agents.web.session_store import WebSessionStore


class SharedBus:
    def __init__(self) -> None:
        self.registries: dict[str, dict[str, str]] = defaultdict(dict)
        self.subscribers: dict[str, list[asyncio.Queue]] = defaultdict(list)
        self.published: list[tuple[str, dict]] = []

    async def publish(self, key: str, payload: dict) -> None:
        self.published.append((key, payload))
        for queue in list(self.subscribers[key]):
            await queue.put(dict(payload))

    async def subscribe(self, key: str, *, on_ready=None):
        queue: asyncio.Queue = asyncio.Queue()
        self.subscribers[key].append(queue)
        if on_ready is not None:
            on_ready()
        try:
            while True:
                yield await queue.get()
        finally:
            self.subscribers[key].remove(queue)

    async def registry_set(
        self,
        namespace: str,
        field: str,
        value: str,
        *,
        ttl_secs: int | None = None,
    ) -> None:
        del ttl_secs
        self.registries[namespace][field] = value

    async def registry_del(self, namespace: str, field: str) -> None:
        self.registries[namespace].pop(field, None)

    async def registry_getall(self, namespace: str) -> dict[str, str]:
        return dict(self.registries[namespace])


@pytest.mark.asyncio
async def test_stop_ack_waits_for_owner_worker_quiescence() -> None:
    bus = SharedBus()
    cancellation = CancellationContext()
    worker_token = cancellation.reserve_worker()
    worker_exited = asyncio.Event()
    lease = OwnerLease("session-1", 3, cancellation, frozenset({"call-1"}))
    owner = StopCoordinator(bus, lambda: [lease], runtime_id="owner", ack_timeout=1)
    requester = StopCoordinator(bus, lambda: [], runtime_id="requester", ack_timeout=1)
    await owner.start()
    await requester.start()
    try:
        await owner.refresh_owners()
        request = asyncio.create_task(
            requester.request_and_wait(
                request_id="stop-1",
                target_generation=3,
                agentscope_session_ids=["session-1"],
            )
        )
        await asyncio.sleep(0.02)

        assert cancellation.cancelled is True
        assert request.done() is False

        worker_exited.set()
        cancellation.finish_worker(worker_token)
        await request
        assert worker_exited.is_set() is True
    finally:
        await requester.stop()
        await owner.stop()


@pytest.mark.asyncio
async def test_stop_ack_waits_for_tracked_owner_task_cleanup() -> None:
    bus = SharedBus()
    cancellation = CancellationContext()
    started = asyncio.Event()
    cleanup_release = asyncio.Event()

    async def agent_run() -> None:
        async with cancellation.track_agent("agent"):
            started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                await cleanup_release.wait()

    agent_task = asyncio.create_task(agent_run())
    await started.wait()
    lease = OwnerLease("session-1", 3, cancellation, frozenset())
    owner = StopCoordinator(bus, lambda: [lease], runtime_id="owner", ack_timeout=1)
    requester = StopCoordinator(bus, lambda: [], runtime_id="requester", ack_timeout=1)
    await owner.start()
    await requester.start()
    try:
        await owner.refresh_owners()
        request = asyncio.create_task(
            requester.request_and_wait(
                request_id="stop-agent",
                target_generation=3,
                agentscope_session_ids=["session-1"],
            )
        )
        await asyncio.sleep(0.02)

        assert cancellation.cancelled is True
        assert request.done() is False
        cleanup_release.set()
        await agent_task
        await request
    finally:
        cleanup_release.set()
        await asyncio.gather(agent_task, return_exceptions=True)
        await requester.stop()
        await owner.stop()


@pytest.mark.asyncio
async def test_stop_timeout_has_no_false_ack_and_same_request_can_retry() -> None:
    bus = SharedBus()
    cancellation = CancellationContext()
    worker_token = cancellation.reserve_worker()
    lease = OwnerLease("session-1", 4, cancellation, frozenset({"call-1"}))
    owner = StopCoordinator(bus, lambda: [lease], runtime_id="owner", ack_timeout=0.03)
    requester = StopCoordinator(bus, lambda: [], runtime_id="requester", ack_timeout=0.03)
    await owner.start()
    await requester.start()
    try:
        await owner.refresh_owners()
        with pytest.raises(TimeoutError, match="owner acknowledgement"):
            await requester.request_and_wait(
                request_id="stable-stop",
                target_generation=4,
                agentscope_session_ids=["session-1"],
            )
        assert await bus.registry_getall(StopCoordinator.ack_namespace("stable-stop")) == {}

        cancellation.finish_worker(worker_token)
        await requester.request_and_wait(
            request_id="stable-stop",
            target_generation=4,
            agentscope_session_ids=["session-1"],
        )
        request_ids = [
            payload["request_id"]
            for key, payload in bus.published
            if key == StopCoordinator.REQUEST_CHANNEL
        ]
        assert request_ids and set(request_ids) == {"stable-stop"}
    finally:
        await requester.stop()
        await owner.stop()


@pytest.mark.asyncio
async def test_stop_waits_for_every_mapping_owner() -> None:
    bus = SharedBus()
    first = CancellationContext()
    second = CancellationContext()
    first_token = first.reserve_worker()
    second_token = second.reserve_worker()
    owner_a = StopCoordinator(
        bus,
        lambda: [OwnerLease("session-1", 5, first, frozenset({"call-1"}))],
        runtime_id="owner-a",
        ack_timeout=1,
    )
    owner_b = StopCoordinator(
        bus,
        lambda: [OwnerLease("session-2", 5, second, frozenset({"call-2"}))],
        runtime_id="owner-b",
        ack_timeout=1,
    )
    requester = StopCoordinator(bus, lambda: [], runtime_id="requester", ack_timeout=1)
    await owner_a.start()
    await owner_b.start()
    await requester.start()
    try:
        await owner_a.refresh_owners()
        await owner_b.refresh_owners()
        request = asyncio.create_task(
            requester.request_and_wait(
                request_id="stop-many",
                target_generation=5,
                agentscope_session_ids=["session-1", "session-2"],
            )
        )
        await asyncio.sleep(0.02)
        first.finish_worker(first_token)
        await asyncio.sleep(0.02)
        assert request.done() is False

        second.finish_worker(second_token)
        await request
    finally:
        await requester.stop()
        await owner_b.stop()
        await owner_a.stop()


@pytest.mark.asyncio
async def test_cross_runtime_timeout_keeps_running_then_retry_commits_after_worker_exit(
    tmp_path,
) -> None:
    class ChatService:
        async def interrupt(self, *_args):
            return None

    def runtime(bus: SharedBus, runtime_id: str) -> AgentScopeRuntime:
        return AgentScopeRuntime(
            config=SimpleNamespace(
                user_id="alice",
                main_router_agent_id="main-router-agent",
                navigation_agent_id="navigation-data-agent",
            ),
            storage=None,
            message_bus=bus,
            workspace_manager=None,
            app=SimpleNamespace(state=SimpleNamespace(chat_service=ChatService())),
            bootstrapped=True,
            runtime_id=runtime_id,
        )

    bus = SharedBus()
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    session = store.create_session("cross runtime")
    internal_session_id = "owner-session"
    store.save_agentscope_session_mapping(
        session.id,
        agent_id="main-router-agent",
        agentscope_session_id=internal_session_id,
    )
    generation = store.begin_execution_generation(session.id)
    store.start_tool_run(
        session.id,
        "call-1",
        "extract",
        "2026-07-15T00:00:00Z",
    )
    requester = runtime(bus, "requester")
    owner = runtime(bus, "owner")
    requester.set_web_session_store(WebSessionStore(store.db_path))
    owner.set_web_session_store(WebSessionStore(store.db_path))
    cancellation = CancellationContext()
    worker_token = cancellation.reserve_worker()
    owner.register_run_cancellation(
        internal_session_id,
        cancellation,
        generation=generation,
    )
    owner.retain_tool_cancellation(internal_session_id, "call-1", cancellation)
    assert requester.stop_coordinator is not None
    assert owner.stop_coordinator is not None
    requester.stop_coordinator.ack_timeout = 0.05
    requester.stop_coordinator.retry_interval = 0.01
    owner.stop_coordinator.ack_timeout = 0.05
    await owner.start_stop_coordinator()
    await requester.start_stop_coordinator()
    try:
        await owner.stop_coordinator.refresh_owners()
        stop_task = asyncio.create_task(
            requester.interrupt_web_session(web_session_id=session.id)
        )
        await asyncio.sleep(0.03)

        assert cancellation.cancelled is True
        assert stop_task.done() is False
        before_exit = store.get_session(session.id)
        assert before_exit is not None
        assert before_exit.tool_runs[0].status == "running"
        assert [
            event
            for event in before_exit.events
            if event.event.get("name") == "datapilot_tool_terminal"
        ] == []

        with pytest.raises(RuntimeError, match="owner acknowledgement"):
            await stop_task
        timed_out = store.get_session(session.id)
        assert timed_out is not None
        assert timed_out.tool_runs[0].status == "running"
        pending = store.begin_or_resume_stop_request(session.id)

        requester.stop_coordinator.ack_timeout = 1
        owner.stop_coordinator.ack_timeout = 1
        cancellation.finish_worker(worker_token)
        owner.release_tool_cancellation(internal_session_id, "call-1", cancellation)
        owner.clear_run_cancellation(internal_session_id, cancellation)
        response = await requester.interrupt_web_session(web_session_id=session.id)
        assert store.begin_or_resume_stop_request(session.id).request_id == pending.request_id
        after_exit = store.get_session(session.id)
        assert response.stopped_tool_call_ids == ["call-1"]
        assert after_exit is not None
        assert after_exit.tool_runs[0].status == "stopped"
        assert len(
            [
                event
                for event in after_exit.events
                if event.event.get("name") == "datapilot_tool_terminal"
            ]
        ) == 1
    finally:
        cancellation.finish_worker(worker_token)
        await requester.stop_stop_coordinator()
        await owner.stop_stop_coordinator()


@pytest.mark.asyncio
async def test_same_runtime_stop_freezes_owner_before_lease_cleanup(tmp_path) -> None:
    class ChatService:
        async def interrupt(self, *_args):
            return None

    bus = SharedBus()
    runtime = AgentScopeRuntime(
        config=SimpleNamespace(
            user_id="alice",
            main_router_agent_id="main-router-agent",
            navigation_agent_id="navigation-data-agent",
        ),
        storage=None,
        message_bus=bus,
        workspace_manager=None,
        app=SimpleNamespace(state=SimpleNamespace(chat_service=ChatService())),
        bootstrapped=True,
        runtime_id="same-runtime",
    )
    store = WebSessionStore(tmp_path / "same-runtime.sqlite")
    session = store.create_session("same runtime")
    internal_session_id = "same-owner-session"
    store.save_agentscope_session_mapping(
        session.id,
        agent_id="main-router-agent",
        agentscope_session_id=internal_session_id,
    )
    generation = store.begin_execution_generation(session.id)
    store.start_tool_run(session.id, "call-1", "extract", "2026-07-15T00:00:00Z")
    runtime.set_web_session_store(store)
    cancellation = CancellationContext()
    worker_token = cancellation.reserve_worker()
    runtime.register_run_cancellation(
        internal_session_id,
        cancellation,
        generation=generation,
    )
    runtime.register_run_cancellation(internal_session_id, cancellation)
    runtime.retain_tool_cancellation(internal_session_id, "call-1", cancellation)
    assert runtime.stop_coordinator is not None
    runtime.stop_coordinator.retry_interval = 0.01
    await runtime.start_stop_coordinator()

    async def cleanup_owner() -> None:
        while not cancellation.cancelled:
            await asyncio.sleep(0)
        cancellation.finish_worker(worker_token)
        runtime.release_tool_cancellation(internal_session_id, "call-1", cancellation)
        runtime.clear_run_cancellation(internal_session_id, cancellation)
        runtime.clear_run_cancellation(internal_session_id, cancellation)

    cleanup = asyncio.create_task(cleanup_owner())
    try:
        response = await runtime.interrupt_web_session(web_session_id=session.id)
        await cleanup
        detail = store.get_session(session.id)
        assert response.stopped_tool_call_ids == ["call-1"]
        assert detail is not None
        assert detail.tool_runs[0].status == "stopped"
    finally:
        cancellation.finish_worker(worker_token)
        cleanup.cancel()
        await asyncio.gather(cleanup, return_exceptions=True)
        await runtime.stop_stop_coordinator()

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from types import SimpleNamespace

import pytest
from agentscope.app.message_bus import MessageBusKeys

from vla_data_juicer_agents.core.cancellation import CancellationContext
from vla_data_juicer_agents.runtime.agentscope_runtime import AgentScopeRuntime
from vla_data_juicer_agents.runtime.datapilot_projection import (
    DataPilotRunBoundaryMiddleware,
)
from vla_data_juicer_agents.runtime.stop_coordinator import OwnerLease, StopCoordinator
from vla_data_juicer_agents.web.agent_session import AgentScopeWebSessionManager
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


def _runtime(
    bus: SharedBus,
    runtime_id: str,
    *,
    session_service=None,
) -> AgentScopeRuntime:
    class ChatService:
        async def interrupt(self, *_args):
            return None

    return AgentScopeRuntime(
        config=SimpleNamespace(
            user_id="alice",
            main_router_agent_id="main-router-agent",
            navigation_agent_id="navigation-data-agent",
        ),
        storage=None,
        message_bus=bus,
        workspace_manager=None,
        app=SimpleNamespace(
            state=SimpleNamespace(
                chat_service=ChatService(),
                session_service=session_service,
            )
        ),
        bootstrapped=True,
        runtime_id=runtime_id,
    )


@pytest.mark.asyncio
async def test_official_background_cancel_precedes_owner_ack_for_pure_async_tool(
    tmp_path,
) -> None:
    class OfficialCancelBus(SharedBus):
        def __init__(self) -> None:
            super().__init__()
            self.background_task: asyncio.Task | None = None
            self.operations: list[str] = []

        async def publish(self, key: str, payload: dict) -> None:
            if key == MessageBusKeys.task_cancel_channel():
                self.operations.append("official-cancel")
                assert payload == {"task_id": "bg-task"}
                assert self.background_task is not None
                self.background_task.cancel()
            await super().publish(key, payload)

        async def registry_set(self, namespace, field, value, *, ttl_secs=None):
            if namespace.startswith(StopCoordinator._ACK_PREFIX):
                self.operations.append("owner-ack")
            await super().registry_set(
                namespace,
                field,
                value,
                ttl_secs=ttl_secs,
            )

    bus = OfficialCancelBus()
    store = WebSessionStore(tmp_path / "official-cancel.sqlite")
    session = store.create_session("pure async background tool")
    internal_session_id = "owner-session"
    store.save_agentscope_session_mapping(
        session.id,
        agent_id="main-router-agent",
        agentscope_session_id=internal_session_id,
    )
    generation = store.begin_execution_generation(session.id)
    store.start_tool_run(session.id, "call-1", "extract", "2026-07-15T00:00:00Z")
    requester = _runtime(bus, "requester")
    owner = _runtime(bus, "owner")
    requester.set_web_session_store(WebSessionStore(store.db_path))
    owner.set_web_session_store(WebSessionStore(store.db_path))
    cancellation = CancellationContext()
    owner.register_run_cancellation(
        internal_session_id,
        cancellation,
        generation=generation,
    )
    owner.retain_tool_cancellation(internal_session_id, "call-1", cancellation)
    owner.clear_run_cancellation(internal_session_id, cancellation)

    async def pure_async_tool() -> None:
        try:
            await asyncio.Future()
        finally:
            owner.release_tool_cancellation(
                internal_session_id,
                "call-1",
                cancellation,
            )

    bus.background_task = asyncio.create_task(pure_async_tool())
    bus.registries[MessageBusKeys.bg_tasks(internal_session_id)]["bg-task"] = "{}"
    assert requester.stop_coordinator is not None
    assert owner.stop_coordinator is not None
    requester.stop_coordinator.ack_timeout = 0.2
    requester.stop_coordinator.retry_interval = 0.01
    owner.stop_coordinator.ack_timeout = 0.2
    await owner.start_stop_coordinator()
    await requester.start_stop_coordinator()
    try:
        await owner.stop_coordinator.refresh_owners()
        response = await requester.interrupt_web_session(web_session_id=session.id)

        assert response.stopped_tool_call_ids == ["call-1"]
        assert bus.background_task.done() is True
        assert bus.operations.index("official-cancel") < bus.operations.index("owner-ack")
    finally:
        bus.background_task.cancel()
        await asyncio.gather(bus.background_task, return_exceptions=True)
        await requester.stop_stop_coordinator()
        await owner.stop_stop_coordinator()


@pytest.mark.asyncio
async def test_delete_waits_for_every_remote_owner_and_timeout_is_retryable(
    tmp_path,
) -> None:
    class SessionService:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, str]] = []

        async def delete_session(self, user_id, agent_id, session_id):
            self.calls.append((user_id, agent_id, session_id))
            return True

    class NavigationControl:
        def __init__(self) -> None:
            self.deleted: list[str] = []

        def delete_control_state_for_web_session(self, web_session_id: str) -> None:
            self.deleted.append(web_session_id)

    bus = SharedBus()
    service = SessionService()
    navigation = NavigationControl()
    store = WebSessionStore(tmp_path / "delete-quiescence.sqlite")
    session = store.create_session("remote owners")
    generation = store.begin_execution_generation(session.id)
    mappings = [("agent-a", "session-a"), ("agent-b", "session-b")]
    for index, (agent_id, internal_session_id) in enumerate(mappings, start=1):
        store.save_agentscope_session_mapping(
            session.id,
            agent_id=agent_id,
            agentscope_session_id=internal_session_id,
        )
        store.start_tool_run(
            session.id,
            f"call-{index}",
            "extract",
            "2026-07-15T00:00:00Z",
        )

    requester = _runtime(bus, "requester", session_service=service)
    owners = [_runtime(bus, f"owner-{index}") for index in range(2)]
    requester.set_web_session_store(WebSessionStore(store.db_path))
    requester._navigation_services = lambda: navigation
    cancellations = [CancellationContext(), CancellationContext()]
    for index, (owner, cancellation, mapping) in enumerate(
        zip(owners, cancellations, mappings, strict=True),
        start=1,
    ):
        owner.set_web_session_store(WebSessionStore(store.db_path))
        owner.register_run_cancellation(mapping[1], cancellation, generation=generation)
        owner.retain_tool_cancellation(mapping[1], f"call-{index}", cancellation)
    assert requester.stop_coordinator is not None
    requester.stop_coordinator.ack_timeout = 0.08
    requester.stop_coordinator.retry_interval = 0.01
    for owner in owners:
        assert owner.stop_coordinator is not None
        owner.stop_coordinator.ack_timeout = 0.08
        await owner.start_stop_coordinator()
    await requester.start_stop_coordinator()
    try:
        for owner in owners:
            await owner.stop_coordinator.refresh_owners()
        delete_task = asyncio.create_task(requester.delete_web_session(session.id))
        await asyncio.sleep(0.02)

        assert all(item.cancelled for item in cancellations)
        owners[0].release_tool_cancellation("session-a", "call-1", cancellations[0])
        owners[0].clear_run_cancellation("session-a", cancellations[0])
        assert delete_task.done() is False
        with pytest.raises(RuntimeError, match="owner acknowledgement"):
            await delete_task
        assert service.calls == []
        assert navigation.deleted == []
        assert store.get_session(session.id) is not None

        owners[1].release_tool_cancellation("session-b", "call-2", cancellations[1])
        owners[1].clear_run_cancellation("session-b", cancellations[1])
        requester.stop_coordinator.ack_timeout = 1
        assert await requester.delete_web_session(session.id) is True
        assert [call[2] for call in service.calls] == ["session-a", "session-b"]
        assert navigation.deleted == [session.id]
    finally:
        for index, (owner, cancellation, mapping) in enumerate(
            zip(owners, cancellations, mappings, strict=True),
            start=1,
        ):
            owner.release_tool_cancellation(mapping[1], f"call-{index}", cancellation)
            owner.clear_run_cancellation(mapping[1], cancellation)
        await requester.stop_stop_coordinator()
        for owner in reversed(owners):
            await owner.stop_stop_coordinator()


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


@pytest.mark.asyncio
async def test_stop_ack_converges_when_refresh_snapshot_contains_old_and_new_owner_fields(
) -> None:
    class InterleavingOwnerBus(SharedBus):
        def __init__(self) -> None:
            super().__init__()
            self.pause_generation_refresh = False
            self.new_owner_is_visible = asyncio.Event()
            self.allow_old_owner_delete = asyncio.Event()

        async def registry_set(
            self,
            namespace: str,
            field: str,
            value: str,
            *,
            ttl_secs: int | None = None,
        ) -> None:
            await super().registry_set(
                namespace,
                field,
                value,
                ttl_secs=ttl_secs,
            )
            if self.pause_generation_refresh and field == "owner:2":
                self.new_owner_is_visible.set()

        async def registry_del(self, namespace: str, field: str) -> None:
            if self.pause_generation_refresh and field == "owner:1":
                await self.allow_old_owner_delete.wait()
            await super().registry_del(namespace, field)

    bus = InterleavingOwnerBus()
    cancellation = CancellationContext()
    leases = [OwnerLease("session-1", 1, cancellation, frozenset())]
    owner = StopCoordinator(
        bus,
        lambda: list(leases),
        runtime_id="owner",
        ack_timeout=0.2,
        retry_interval=0.01,
    )
    requester = StopCoordinator(
        bus,
        lambda: [],
        runtime_id="requester",
        ack_timeout=0.08,
        retry_interval=0.01,
    )
    refresh: asyncio.Task | None = None
    await owner.start()
    await requester.start()
    try:
        bus.pause_generation_refresh = True
        leases[:] = [OwnerLease("session-1", 2, cancellation, frozenset())]
        refresh = asyncio.create_task(owner.refresh_owners())
        await asyncio.wait_for(bus.new_owner_is_visible.wait(), timeout=0.2)

        expected = await requester.snapshot_expected_owners(["session-1"], 2)
        assert sorted(item["generation"] for item in expected.values()) == [1, 2]

        bus.allow_old_owner_delete.set()
        await refresh
        await requester.request_and_wait(
            request_id="stop-generation-refresh",
            target_generation=2,
            agentscope_session_ids=["session-1"],
            expected_owners=expected,
        )

        acknowledgements = await bus.registry_getall(
            StopCoordinator.ack_namespace("stop-generation-refresh")
        )
        assert set(acknowledgements) == set(expected)
        assert all(
            json.loads(raw)["status"] == "applied"
            for raw in acknowledgements.values()
        )
    finally:
        bus.allow_old_owner_delete.set()
        if refresh is not None:
            await asyncio.gather(refresh, return_exceptions=True)
        await requester.stop()
        await owner.stop()


@pytest.mark.asyncio
async def test_stop_ack_converges_when_only_old_owner_generation_was_frozen() -> None:
    bus = SharedBus()
    old_cancellation = CancellationContext()
    new_cancellation = CancellationContext()
    old_lease = OwnerLease("session-1", 1, old_cancellation, frozenset())
    leases = [old_lease]
    owner = StopCoordinator(bus, lambda: list(leases), runtime_id="owner", ack_timeout=0.2)
    requester = StopCoordinator(bus, lambda: [], runtime_id="requester", ack_timeout=0.2)
    await owner.start()
    await requester.start()
    try:
        expected = await requester.snapshot_expected_owners(["session-1"], 2)
        assert [item["generation"] for item in expected.values()] == [1]
        leases[:] = [
            old_lease,
            OwnerLease("session-1", 2, new_cancellation, frozenset()),
        ]
        await owner.refresh_owners()

        await requester.request_and_wait(
            request_id="stop-old-generation",
            target_generation=2,
            agentscope_session_ids=["session-1"],
            expected_owners=expected,
        )
        assert old_cancellation.cancelled is True
        assert new_cancellation.cancelled is True
    finally:
        await requester.stop()
        await owner.stop()


@pytest.mark.asyncio
async def test_owner_write_then_raise_is_best_effort_removed() -> None:
    class WriteThenRaiseBus(SharedBus):
        async def registry_set(self, namespace, field, value, *, ttl_secs=None):
            await super().registry_set(
                namespace,
                field,
                value,
                ttl_secs=ttl_secs,
            )
            raise ConnectionError("reply lost after owner write")

    bus = WriteThenRaiseBus()
    cancellation = CancellationContext()
    owner = StopCoordinator(
        bus,
        lambda: [OwnerLease("session-1", 1, cancellation, frozenset())],
        runtime_id="owner",
    )

    with pytest.raises(ConnectionError, match="reply lost"):
        await owner.refresh_owners()

    assert await bus.registry_getall(owner.owner_namespace("session-1")) == {}


@pytest.mark.asyncio
async def test_delete_barrier_rejects_cross_runtime_submit_before_destructive_delete(
    tmp_path,
) -> None:
    class GatedSessionService:
        def __init__(self) -> None:
            self.delete_entered = asyncio.Event()
            self.allow_delete = asyncio.Event()
            self.deleted: list[str] = []

        async def delete_session(self, _user_id, _agent_id, session_id):
            self.delete_entered.set()
            await self.allow_delete.wait()
            self.deleted.append(session_id)
            return True

    class NavigationControl:
        def delete_control_state_for_web_session(self, _web_session_id: str) -> None:
            return None

    class TaskRegistry:
        def __init__(self) -> None:
            self.spawned: list[asyncio.Task] = []

        def spawn(self, coroutine, *, session_id):
            task = asyncio.create_task(coroutine, name=f"submit-during-delete:{session_id}")
            self.spawned.append(task)
            return task

    class BoundaryChatService:
        def __init__(self) -> None:
            self.runtime: AgentScopeRuntime | None = None

        async def interrupt(self, *_args):
            return None

        async def run(self, *, session_id, input_msg, **_kwargs):
            assert self.runtime is not None
            middleware = DataPilotRunBoundaryMiddleware(session_id, self.runtime)

            async def handler(**_handler_kwargs):
                if False:
                    yield None

            async for _event in middleware.on_reply(
                SimpleNamespace(name="MainRouterAgent"),
                {"inputs": input_msg},
                handler,
            ):
                pass

    bus = SharedBus()
    service = GatedSessionService()
    store = WebSessionStore(tmp_path / "delete-submit-race.sqlite")
    session = store.create_session("delete submit race")
    internal_session_id = f"{session.id}__main-router-agent"
    store.save_agentscope_session_mapping(
        session.id,
        agent_id="main-router-agent",
        agentscope_session_id=internal_session_id,
    )

    deleter = _runtime(bus, "deleter", session_service=service)
    deleter._navigation_services = lambda: NavigationControl()
    submitter = _runtime(bus, "submitter")
    submitter.config.router_model = "router-model"
    submitter.config.navigation_model = "navigation-model"
    registry = TaskRegistry()
    chat_service = BoundaryChatService()
    chat_service.runtime = submitter
    submitter.app.state.chat_service = chat_service
    submitter.app.state.chat_run_registry = registry

    delete_manager = AgentScopeWebSessionManager(
        store=WebSessionStore(store.db_path),
        runtime=deleter,
    )
    submit_manager = AgentScopeWebSessionManager(
        store=WebSessionStore(store.db_path),
        runtime=submitter,
    )
    delete_task: asyncio.Task | None = None
    await deleter.start_stop_coordinator()
    await submitter.start_stop_coordinator()
    try:
        delete_task = asyncio.create_task(delete_manager.delete_session(session.id))
        await asyncio.wait_for(service.delete_entered.wait(), timeout=0.3)
        generation_after_stop = store.current_execution_generation(session.id)
        assert store.execution_generation_is_stopped(session.id) is True
        assert store.session_deletion_is_pending(session.id) is True

        submit_result = (
            await asyncio.gather(
                submit_manager.submit_turn(session.id, "must not be admitted"),
                return_exceptions=True,
            )
        )[0]

        assert isinstance(submit_result, RuntimeError), (
            "concurrent submit unexpectedly succeeded after the delete stop barrier: "
            f"{submit_result!r}"
        )
        assert registry.spawned == []
        assert store.current_execution_generation(session.id) == generation_after_stop
        assert store.execution_generation_is_stopped(session.id) is True
        detail = store.get_session(session.id)
        assert detail is not None
        assert detail.messages == []
    finally:
        service.allow_delete.set()
        if delete_task is not None:
            await asyncio.gather(delete_task, return_exceptions=True)
        await asyncio.gather(*registry.spawned, return_exceptions=True)
        await submitter.stop_stop_coordinator()
        await deleter.stop_stop_coordinator()

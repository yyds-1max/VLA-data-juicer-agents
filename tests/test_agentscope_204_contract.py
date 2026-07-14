from __future__ import annotations

import inspect
from importlib.metadata import version
from types import SimpleNamespace

import agentscope.app
import agentscope.app._lifespan as agentscope_lifespan
import pytest
from agentscope.app.message_bus import MessageBusKeys
from fastapi.testclient import TestClient

from navigation_chat_service_harness import (
    ChatServiceBus,
    ChatServiceStorage,
    ChatServiceWorkspaceManager,
)


class _LifespanResource:
    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback):
        return False


class _Storage(_LifespanResource, ChatServiceStorage):
    pass


class _MessageBus(_LifespanResource, ChatServiceBus):
    pass


class _WorkspaceManager(_LifespanResource, ChatServiceWorkspaceManager):
    pass


class _InertLifecycleManager(_LifespanResource):
    def __init__(self, *_args, **_kwargs) -> None:
        pass


@pytest.fixture
def runtime(monkeypatch):
    for name in (
        "BackgroundTaskManager",
        "ChatRunRegistry",
        "SchedulerManager",
        "WakeupDispatcher",
        "CancelDispatcher",
    ):
        monkeypatch.setattr(agentscope_lifespan, name, _InertLifecycleManager)

    app = agentscope.app.create_app(
        storage=_Storage(),
        message_bus=_MessageBus(),
        workspace_manager=_WorkspaceManager(),
    )
    with TestClient(app):
        yield SimpleNamespace(app=app)


def test_agentscope_204_embedded_contract():
    assert version("agentscope") == "2.0.4"
    assert callable(MessageBusKeys.bg_tasks)
    assert callable(MessageBusKeys.task_cancel_channel)


def test_embedded_services_expose_interrupt_and_delete(runtime):
    assert inspect.iscoroutinefunction(runtime.app.state.chat_service.interrupt)
    assert inspect.iscoroutinefunction(runtime.app.state.session_service.delete_session)

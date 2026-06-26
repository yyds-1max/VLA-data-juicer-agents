# DataPilot Web Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the first front-end/back-end separated DataPilot Web UI that embeds a floating chat window in a DataLoop-style console and connects it to the existing main Agent.

**Architecture:** Keep the Python Agent system as the backend source of truth and add a thin `web` package for sessions, persistence, REST, and WebSocket streaming. Put the React application in a top-level `frontend/` directory so the DataLoop shell, DataPilot window, state store, reducer, and API client remain separate from Python backend code. Reuse `SessionController` and normalized Agent events; do not parse TUI output.

**Tech Stack:** Backend: Python, FastAPI, Uvicorn, SQLite, existing `SessionController`. Frontend: React, TypeScript, Vite, Tailwind CSS, shadcn/ui, Radix UI, Zustand, Vitest, Testing Library, Playwright.

---

## Non-Negotiable Boundaries

- Backend code lives under `src/vla_data_juicer_agents/web/`.
- Frontend code lives under `frontend/`.
- Shared API contracts are described in both backend Pydantic schemas and frontend TypeScript types; no direct frontend import from Python files.
- The existing TUI remains intact. Do not move TUI rendering logic into the Web layer.
- `data_loop_v1.1.html` is a visual reference, not the place to keep production UI code.
- The draft new-session page does not create a backend session until the first message is submitted.
- Clicking `New` does not mark the previous session historical until the first draft message is submitted.
- Closing the floating window or browser tab does not end the session.
- The stop icon interrupts the current turn only; it does not end the session.

## Target File Structure

```text
src/vla_data_juicer_agents/
  web/
    __init__.py
    app.py                 # FastAPI factory and route registration
    cli.py                 # vla-data-agent-web entry point
    schemas.py             # Pydantic request/response/event models
    session_store.py       # SQLite metadata + transcript persistence
    session_manager.py     # Maps session_id to SessionController/runtime state
    event_stream.py        # Async fan-out queue and controller drain loop

tests/
  test_web_session_store.py
  test_web_session_manager.py
  test_web_api.py
  test_web_event_stream.py

frontend/
  package.json
  vite.config.ts
  tsconfig.json
  index.html
  src/
    main.tsx
    app/App.tsx
    app/AppShell.tsx
    api/client.ts
    api/types.ts
    store/datapilotStore.ts
    store/eventReducer.ts
    store/eventReducer.test.ts
    components/datapilot/DataPilotButton.tsx
    components/datapilot/DataPilotWindow.tsx
    components/datapilot/SessionHeader.tsx
    components/datapilot/DraftNewSessionView.tsx
    components/datapilot/Composer.tsx
    components/datapilot/MessageList.tsx
    components/datapilot/AgentRunSummary.tsx
    components/datapilot/SessionHistoryPanel.tsx
    components/ui/...
    styles/globals.css
  tests/
    datapilot.spec.ts
```

## Task 1: Add Backend Web Dependencies And Entry Point

**Files:**
- Modify: `pyproject.toml`
- Create: `src/vla_data_juicer_agents/web/__init__.py`
- Create: `src/vla_data_juicer_agents/web/cli.py`

- [ ] **Step 1: Add the backend Web dependencies**

Modify `pyproject.toml` dependencies:

```toml
dependencies = [
    "agentscope==2.0.1",
    "fastapi>=0.115",
    "pydantic>=2.7",
    "PyYAML>=6.0",
    "rich>=13.7.0",
    "uvicorn[standard]>=0.30",
]
```

Add a script entry:

```toml
[project.scripts]
vla-nav-agent = "vla_data_juicer_agents.cli:main"
vla-data-agent = "vla_data_juicer_agents.session_cli:main"
vla-data-agent-web = "vla_data_juicer_agents.web.cli:main"
```

- [ ] **Step 2: Create the Web package marker**

Create `src/vla_data_juicer_agents/web/__init__.py`:

```python
"""Web API for DataPilot."""
```

- [ ] **Step 3: Add a CLI placeholder that imports the future app factory**

Create `src/vla_data_juicer_agents/web/cli.py`:

```python
from __future__ import annotations

import argparse

import uvicorn


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vla-data-agent-web",
        description="Run the DataPilot Web API and frontend server.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--working-dir", default="./.djx")
    parser.add_argument("--model", default=None)
    parser.add_argument("--reload", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    uvicorn.run(
        "vla_data_juicer_agents.web.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        reload=args.reload,
        app_dir=None,
        log_level="info",
    )
    return 0
```

- [ ] **Step 4: Run the existing backend tests**

Run:

```bash
pytest tests/test_tui_controller.py tests/test_tui_event_adapter.py -q
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml src/vla_data_juicer_agents/web
git commit -m "feat: add DataPilot web entry point"
```

## Task 2: Define Backend API Schemas

**Files:**
- Create: `src/vla_data_juicer_agents/web/schemas.py`
- Test: `tests/test_web_session_store.py`

- [ ] **Step 1: Create schema tests for title generation and response shape**

Create `tests/test_web_session_store.py` with the first schema-focused test:

```python
from vla_data_juicer_agents.web.schemas import (
    CreateTurnRequest,
    SessionRecord,
    generate_session_title,
)


def test_generate_session_title_uses_first_30_chars():
    title = generate_session_title("处理 20270605 的室外导航数据，并进行 dry-run 验证")

    assert title == "处理 20270605 的室外导航数据，并进行 dry-run"


def test_turn_request_rejects_empty_message():
    try:
        CreateTurnRequest(message="   ")
    except ValueError as exc:
        assert "message must not be empty" in str(exc)
    else:
        raise AssertionError("expected validation error")


def test_session_record_serializes_status():
    record = SessionRecord(
        id="session_1",
        title="处理 20270605 的室外导航数据",
        status="active",
        created_at="2026-06-26T10:00:00+08:00",
        updated_at="2026-06-26T10:01:00+08:00",
    )

    assert record.model_dump()["status"] == "active"
```

- [ ] **Step 2: Run the tests and verify they fail**

Run:

```bash
pytest tests/test_web_session_store.py -q
```

Expected: FAIL because `vla_data_juicer_agents.web.schemas` does not exist.

- [ ] **Step 3: Implement schemas**

Create `src/vla_data_juicer_agents/web/schemas.py`:

```python
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


SessionStatus = Literal["draft", "active", "historical"]
MessageRole = Literal["user", "assistant", "system"]


def generate_session_title(message: str, *, limit: int = 30) -> str:
    normalized = " ".join(str(message).split())
    return normalized[:limit] if normalized else "未命名任务"


class SessionRecord(BaseModel):
    id: str
    title: str
    status: SessionStatus
    created_at: str
    updated_at: str


class ChatMessageRecord(BaseModel):
    id: str
    session_id: str
    role: MessageRole
    content: str
    created_at: str


class SessionDetail(SessionRecord):
    messages: list[ChatMessageRecord] = Field(default_factory=list)


class CreateSessionResponse(BaseModel):
    session: SessionRecord


class CreateTurnRequest(BaseModel):
    message: str

    @field_validator("message")
    @classmethod
    def message_must_not_be_empty(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("message must not be empty")
        return stripped


class CreateTurnResponse(BaseModel):
    turn_id: str


class InterruptResponse(BaseModel):
    interrupted: bool


class AgentEvent(BaseModel):
    type: str
    source: str | None = None
    run_id: str | None = None
    parent_run_id: str | None = None
    timestamp: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
```

- [ ] **Step 4: Run schema tests**

Run:

```bash
pytest tests/test_web_session_store.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/vla_data_juicer_agents/web/schemas.py tests/test_web_session_store.py
git commit -m "feat: define DataPilot web schemas"
```

## Task 3: Implement SQLite Session Store

**Files:**
- Modify: `src/vla_data_juicer_agents/web/session_store.py`
- Modify: `tests/test_web_session_store.py`

- [ ] **Step 1: Add failing persistence tests**

Append to `tests/test_web_session_store.py`:

```python
from pathlib import Path

from vla_data_juicer_agents.web.session_store import WebSessionStore


def test_store_creates_session_and_lists_recent(tmp_path: Path):
    store = WebSessionStore(tmp_path / "sessions.sqlite")

    session = store.create_session(title="处理 20270605 的室外导航数据")
    recent = store.list_sessions()

    assert session.status == "active"
    assert recent == [session]


def test_store_persists_transcript(tmp_path: Path):
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    session = store.create_session(title="处理 20270605 的室外导航数据")

    user = store.append_message(session.id, role="user", content="处理 20270605")
    assistant = store.append_message(session.id, role="assistant", content="好的，我开始处理。")
    detail = store.get_session(session.id)

    assert detail is not None
    assert [message.id for message in detail.messages] == [user.id, assistant.id]
    assert [message.content for message in detail.messages] == ["处理 20270605", "好的，我开始处理。"]


def test_store_marks_previous_active_historical(tmp_path: Path):
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    first = store.create_session(title="第一个任务")
    second = store.create_session(title="第二个任务")

    store.mark_historical(first.id)

    assert store.get_session(first.id).status == "historical"
    assert store.get_session(second.id).status == "active"
```

- [ ] **Step 2: Run the tests and verify they fail**

Run:

```bash
pytest tests/test_web_session_store.py -q
```

Expected: FAIL because `session_store.py` does not exist.

- [ ] **Step 3: Implement `WebSessionStore`**

Create `src/vla_data_juicer_agents/web/session_store.py` with these public methods:

```python
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from vla_data_juicer_agents.web.schemas import (
    ChatMessageRecord,
    MessageRole,
    SessionDetail,
    SessionRecord,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class WebSessionStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_schema(self) -> None:
        with self._connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(id),
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )

    def create_session(self, *, title: str) -> SessionRecord:
        now = _now()
        record = SessionRecord(
            id=f"session_{uuid4().hex}",
            title=title,
            status="active",
            created_at=now,
            updated_at=now,
        )
        with self._connect() as db:
            db.execute(
                "INSERT INTO sessions (id, title, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (record.id, record.title, record.status, record.created_at, record.updated_at),
            )
        return record

    def list_sessions(self, *, limit: int = 20) -> list[SessionRecord]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT id, title, status, created_at, updated_at FROM sessions ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [SessionRecord(**dict(row)) for row in rows]

    def get_session(self, session_id: str) -> SessionDetail | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT id, title, status, created_at, updated_at FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                return None
            messages = db.execute(
                "SELECT id, session_id, role, content, created_at FROM messages WHERE session_id = ? ORDER BY created_at ASC",
                (session_id,),
            ).fetchall()
        return SessionDetail(**dict(row), messages=[ChatMessageRecord(**dict(message)) for message in messages])

    def append_message(self, session_id: str, *, role: MessageRole, content: str) -> ChatMessageRecord:
        now = _now()
        record = ChatMessageRecord(
            id=f"message_{uuid4().hex}",
            session_id=session_id,
            role=role,
            content=content,
            created_at=now,
        )
        with self._connect() as db:
            db.execute(
                "INSERT INTO messages (id, session_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
                (record.id, record.session_id, record.role, record.content, record.created_at),
            )
            db.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (now, session_id))
        return record

    def mark_historical(self, session_id: str) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE sessions SET status = ?, updated_at = ? WHERE id = ?",
                ("historical", _now(), session_id),
            )
```

- [ ] **Step 4: Run store tests**

Run:

```bash
pytest tests/test_web_session_store.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/vla_data_juicer_agents/web/session_store.py tests/test_web_session_store.py
git commit -m "feat: persist DataPilot sessions"
```

## Task 4: Implement Session Manager Around `SessionController`

**Files:**
- Create: `src/vla_data_juicer_agents/web/session_manager.py`
- Create: `tests/test_web_session_manager.py`

- [ ] **Step 1: Add tests using a fake controller**

Create `tests/test_web_session_manager.py`:

```python
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
    assert store.get_session(session.id).messages[0].role == "user"
    assert store.get_session(session.id).messages[0].content == "开始处理"


def test_submit_turn_rejects_unknown_session(tmp_path: Path):
    manager = WebSessionManager(
        store=WebSessionStore(tmp_path / "sessions.sqlite"),
        working_dir=str(tmp_path),
        controller_factory=FakeController,
    )

    with pytest.raises(KeyError):
        manager.submit_turn("missing", "hello")
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
pytest tests/test_web_session_manager.py -q
```

Expected: FAIL because `session_manager.py` does not exist.

- [ ] **Step 3: Implement manager**

Create `src/vla_data_juicer_agents/web/session_manager.py`:

```python
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from vla_data_juicer_agents.tui.controller import SessionController
from vla_data_juicer_agents.web.schemas import SessionRecord, generate_session_title
from vla_data_juicer_agents.web.session_store import WebSessionStore


ControllerFactory = Callable[..., Any]


class WebSessionManager:
    def __init__(
        self,
        *,
        store: WebSessionStore,
        working_dir: str = "./.djx",
        model: str | None = None,
        controller_factory: ControllerFactory = SessionController,
    ) -> None:
        self.store = store
        self.working_dir = Path(working_dir)
        self.model = model
        self.controller_factory = controller_factory
        self._controllers: dict[str, Any] = {}

    def create_session(self, first_message: str) -> SessionRecord:
        session = self.store.create_session(title=generate_session_title(first_message))
        controller = self.controller_factory(
            working_dir=str(self.working_dir / session.id),
            model=self.model,
        )
        controller.start()
        self._controllers[session.id] = controller
        return session

    def get_controller(self, session_id: str) -> Any:
        controller = self._controllers.get(session_id)
        if controller is None:
            raise KeyError(session_id)
        return controller

    def submit_turn(self, session_id: str, message: str) -> str:
        controller = self.get_controller(session_id)
        controller.submit_turn(message)
        self.store.append_message(session_id, role="user", content=message)
        return f"turn_{uuid4().hex}"

    def interrupt(self, session_id: str) -> bool:
        return bool(self.get_controller(session_id).request_interrupt())

    def mark_historical(self, session_id: str) -> None:
        self.store.mark_historical(session_id)
        self._controllers.pop(session_id, None)
```

- [ ] **Step 4: Run manager tests**

Run:

```bash
pytest tests/test_web_session_manager.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/vla_data_juicer_agents/web/session_manager.py tests/test_web_session_manager.py
git commit -m "feat: manage DataPilot agent sessions"
```

## Task 5: Add Event Stream Fan-Out

**Files:**
- Create: `src/vla_data_juicer_agents/web/event_stream.py`
- Create: `tests/test_web_event_stream.py`

- [ ] **Step 1: Add event stream tests**

Create `tests/test_web_event_stream.py`:

```python
import asyncio

import pytest

from vla_data_juicer_agents.web.event_stream import SessionEventBus


@pytest.mark.asyncio
async def test_event_bus_delivers_events_to_subscriber():
    bus = SessionEventBus()

    async with bus.subscribe("session_1") as queue:
        await bus.publish("session_1", {"type": "reasoning", "payload": {"summary": "working"}})
        event = await asyncio.wait_for(queue.get(), timeout=1)

    assert event["type"] == "reasoning"


@pytest.mark.asyncio
async def test_event_bus_scopes_by_session():
    bus = SessionEventBus()

    async with bus.subscribe("session_1") as queue:
        await bus.publish("session_2", {"type": "final", "payload": {"text": "wrong"}})

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(queue.get(), timeout=0.05)
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
pytest tests/test_web_event_stream.py -q
```

Expected: FAIL because `event_stream.py` does not exist.

- [ ] **Step 3: Implement the event bus**

Create `src/vla_data_juicer_agents/web/event_stream.py`:

```python
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from typing import Any


class SessionEventBus:
    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[dict[str, Any]]]] = {}

    @asynccontextmanager
    async def subscribe(self, session_id: str) -> AsyncIterator[asyncio.Queue[dict[str, Any]]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._subscribers.setdefault(session_id, set()).add(queue)
        try:
            yield queue
        finally:
            subscribers = self._subscribers.get(session_id)
            if subscribers is not None:
                subscribers.discard(queue)
                if not subscribers:
                    self._subscribers.pop(session_id, None)

    async def publish(self, session_id: str, event: dict[str, Any]) -> None:
        for queue in list(self._subscribers.get(session_id, ())):
            await queue.put(event)
```

- [ ] **Step 4: Run event stream tests**

Run:

```bash
pytest tests/test_web_event_stream.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/vla_data_juicer_agents/web/event_stream.py tests/test_web_event_stream.py
git commit -m "feat: add DataPilot event stream"
```

## Task 6: Implement FastAPI REST And WebSocket API

**Files:**
- Create: `src/vla_data_juicer_agents/web/app.py`
- Modify: `tests/test_web_api.py`

- [ ] **Step 1: Add REST API tests**

Create `tests/test_web_api.py`:

```python
from pathlib import Path

from fastapi.testclient import TestClient

from vla_data_juicer_agents.web.app import create_app


class FakeController:
    def __init__(self, **kwargs):
        self.started = False
        self.submitted = []
        self.is_running = False

    def start(self):
        self.started = True

    def submit_turn(self, message):
        self.submitted.append(message)

    def request_interrupt(self):
        return True

    def drain_events(self):
        return []


def test_create_session_and_submit_turn(tmp_path: Path):
    app = create_app(
        working_dir=str(tmp_path / ".djx"),
        db_path=tmp_path / "sessions.sqlite",
        controller_factory=FakeController,
    )
    client = TestClient(app)

    created = client.post("/api/sessions", json={"message": "处理 20270605 的室外导航数据"})
    session = created.json()["session"]
    turn = client.post(f"/api/sessions/{session['id']}/turns", json={"message": "处理 20270605 的室外导航数据"})

    assert created.status_code == 200
    assert turn.status_code == 200
    assert session["title"] == "处理 20270605 的室外导航数据"


def test_history_returns_title_and_updated_at(tmp_path: Path):
    app = create_app(
        working_dir=str(tmp_path / ".djx"),
        db_path=tmp_path / "sessions.sqlite",
        controller_factory=FakeController,
    )
    client = TestClient(app)

    client.post("/api/sessions", json={"message": "处理 20270605 的室外导航数据"})
    sessions = client.get("/api/sessions").json()["sessions"]

    assert set(sessions[0]) == {"id", "title", "status", "created_at", "updated_at"}
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
pytest tests/test_web_api.py -q
```

Expected: FAIL because `web.app` does not exist.

- [ ] **Step 3: Implement `create_app`**

Create `src/vla_data_juicer_agents/web/app.py`:

```python
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect

from vla_data_juicer_agents.tui.controller import SessionController
from vla_data_juicer_agents.web.event_stream import SessionEventBus
from vla_data_juicer_agents.web.schemas import (
    CreateSessionResponse,
    CreateTurnRequest,
    CreateTurnResponse,
    InterruptResponse,
)
from vla_data_juicer_agents.web.session_manager import ControllerFactory, WebSessionManager
from vla_data_juicer_agents.web.session_store import WebSessionStore


def create_app(
    *,
    working_dir: str = "./.djx",
    model: str | None = None,
    db_path: str | Path | None = None,
    controller_factory: ControllerFactory = SessionController,
) -> FastAPI:
    app = FastAPI(title="DataPilot API")
    database = Path(db_path) if db_path is not None else Path(working_dir) / "sessions.sqlite"
    store = WebSessionStore(database)
    manager = WebSessionManager(
        store=store,
        working_dir=working_dir,
        model=model,
        controller_factory=controller_factory,
    )
    bus = SessionEventBus()

    @app.post("/api/sessions", response_model=CreateSessionResponse)
    def create_session(request: CreateTurnRequest) -> CreateSessionResponse:
        session = manager.create_session(request.message)
        return CreateSessionResponse(session=session)

    @app.get("/api/sessions")
    def list_sessions() -> dict[str, Any]:
        return {"sessions": [session.model_dump() for session in store.list_sessions()]}

    @app.get("/api/sessions/{session_id}")
    def get_session(session_id: str) -> dict[str, Any]:
        session = store.get_session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        return {"session": session.model_dump()}

    @app.post("/api/sessions/{session_id}/turns", response_model=CreateTurnResponse)
    async def create_turn(session_id: str, request: CreateTurnRequest) -> CreateTurnResponse:
        try:
            turn_id = manager.submit_turn(session_id, request.message)
        except KeyError:
            raise HTTPException(status_code=404, detail="active session not found") from None
        asyncio.create_task(_drain_controller_events(session_id, manager, store, bus))
        return CreateTurnResponse(turn_id=turn_id)

    @app.post("/api/sessions/{session_id}/interrupt", response_model=InterruptResponse)
    def interrupt(session_id: str) -> InterruptResponse:
        try:
            interrupted = manager.interrupt(session_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="active session not found") from None
        return InterruptResponse(interrupted=interrupted)

    @app.websocket("/api/sessions/{session_id}/events")
    async def session_events(websocket: WebSocket, session_id: str) -> None:
        await websocket.accept()
        async with bus.subscribe(session_id) as queue:
            try:
                while True:
                    event = await queue.get()
                    await websocket.send_json(event)
            except WebSocketDisconnect:
                return

    return app


async def _drain_controller_events(
    session_id: str,
    manager: WebSessionManager,
    store: WebSessionStore,
    bus: SessionEventBus,
) -> None:
    controller = manager.get_controller(session_id)
    while controller.is_running:
        for event in controller.drain_events():
            await bus.publish(session_id, event)
        await asyncio.sleep(0.03)
    for event in controller.drain_events():
        await bus.publish(session_id, event)
        if event.get("type") == "final":
            text = str(event.get("payload", {}).get("text", "")).strip()
            if text:
                store.append_message(session_id, role="assistant", content=text)
    try:
        result = controller.consume_turn_result()
    except RuntimeError:
        return
    if str(getattr(result, "text", "")).strip():
        # Avoid double append when final event already persisted the assistant text.
        detail = store.get_session(session_id)
        if detail is not None and not any(message.role == "assistant" and message.content == result.text for message in detail.messages):
            store.append_message(session_id, role="assistant", content=result.text)
```

- [ ] **Step 4: Run Web API tests**

Run:

```bash
pytest tests/test_web_api.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/vla_data_juicer_agents/web/app.py tests/test_web_api.py
git commit -m "feat: expose DataPilot web API"
```

## Task 7: Scaffold Frontend Workspace

**Files:**
- Create: `frontend/package.json`
- Create: `frontend/index.html`
- Create: `frontend/vite.config.ts`
- Create: `frontend/tsconfig.json`
- Create: `frontend/src/main.tsx`
- Create: `frontend/src/app/App.tsx`

- [ ] **Step 1: Create frontend package metadata**

Create `frontend/package.json`:

```json
{
  "name": "datapilot-frontend",
  "private": true,
  "version": "0.1.0",
  "type": "module",
  "scripts": {
    "dev": "vite --host 127.0.0.1 --port 5173",
    "build": "tsc -b && vite build",
    "test": "vitest run",
    "test:watch": "vitest",
    "e2e": "playwright test"
  },
  "dependencies": {
    "@radix-ui/react-dialog": "^1.1.6",
    "@radix-ui/react-popover": "^1.1.6",
    "@radix-ui/react-scroll-area": "^1.2.3",
    "@radix-ui/react-tooltip": "^1.1.8",
    "class-variance-authority": "^0.7.1",
    "clsx": "^2.1.1",
    "lucide-react": "^0.468.0",
    "react": "^19.0.0",
    "react-dom": "^19.0.0",
    "tailwind-merge": "^2.5.5",
    "zustand": "^5.0.2"
  },
  "devDependencies": {
    "@playwright/test": "^1.49.0",
    "@testing-library/jest-dom": "^6.6.3",
    "@testing-library/react": "^16.1.0",
    "@types/node": "^22.10.2",
    "@types/react": "^19.0.1",
    "@types/react-dom": "^19.0.2",
    "@vitejs/plugin-react": "^4.3.4",
    "autoprefixer": "^10.4.20",
    "postcss": "^8.4.49",
    "tailwindcss": "^3.4.17",
    "typescript": "^5.7.2",
    "vite": "^6.0.3",
    "vitest": "^2.1.8"
  }
}
```

- [ ] **Step 2: Add Vite config with API proxy**

Create `frontend/vite.config.ts`:

```ts
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8765",
        ws: true,
      },
    },
  },
});
```

- [ ] **Step 3: Add TypeScript and HTML shell**

Create `frontend/tsconfig.json`:

```json
{
  "compilerOptions": {
    "target": "ES2022",
    "useDefineForClassFields": true,
    "lib": ["DOM", "DOM.Iterable", "ES2022"],
    "allowJs": false,
    "skipLibCheck": true,
    "esModuleInterop": true,
    "allowSyntheticDefaultImports": true,
    "strict": true,
    "forceConsistentCasingInFileNames": true,
    "module": "ESNext",
    "moduleResolution": "Node",
    "resolveJsonModule": true,
    "isolatedModules": true,
    "noEmit": true,
    "jsx": "react-jsx"
  },
  "include": ["src"]
}
```

Create `frontend/index.html`:

```html
<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>DataPilot</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.tsx"></script>
  </body>
</html>
```

- [ ] **Step 4: Add minimal React app**

Create `frontend/src/main.tsx`:

```tsx
import React from "react";
import ReactDOM from "react-dom/client";
import { App } from "./app/App";
import "./styles/globals.css";

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
```

Create `frontend/src/app/App.tsx`:

```tsx
export function App() {
  return <div>DataPilot</div>;
}
```

- [ ] **Step 5: Install and verify**

Run:

```bash
cd frontend
npm install
npm run build
```

Expected: dependencies install and production build succeeds.

- [ ] **Step 6: Commit**

```bash
git add frontend
git commit -m "feat: scaffold DataPilot frontend"
```

## Task 8: Add Tailwind, UI Helpers, And DataLoop Shell

**Files:**
- Create: `frontend/tailwind.config.ts`
- Create: `frontend/postcss.config.js`
- Create: `frontend/src/lib/utils.ts`
- Create: `frontend/src/styles/globals.css`
- Create: `frontend/src/app/AppShell.tsx`
- Modify: `frontend/src/app/App.tsx`

- [ ] **Step 1: Add Tailwind config**

Create `frontend/tailwind.config.ts`:

```ts
import type { Config } from "tailwindcss";

export default {
  darkMode: ["class"],
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        console: {
          bg: "#08111f",
          panel: "#0d1829",
          panel2: "#101f35",
          line: "#1e3a5f",
          text: "#eaf2ff",
          muted: "#8ca2c4",
          cyan: "#15d1d8",
        },
      },
    },
  },
  plugins: [],
} satisfies Config;
```

Create `frontend/postcss.config.js`:

```js
export default {
  plugins: {
    tailwindcss: {},
    autoprefixer: {},
  },
};
```

- [ ] **Step 2: Add CSS globals**

Create `frontend/src/styles/globals.css`:

```css
@tailwind base;
@tailwind components;
@tailwind utilities;

:root {
  color-scheme: dark;
  font-family:
    Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI",
    sans-serif;
  background: #08111f;
}

body {
  margin: 0;
  min-width: 320px;
  min-height: 100vh;
  background: #08111f;
  color: #eaf2ff;
}

button,
input,
textarea {
  font: inherit;
}
```

- [ ] **Step 3: Add class helper**

Create `frontend/src/lib/utils.ts`:

```ts
import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}
```

- [ ] **Step 4: Build DataLoop-style shell from the reference HTML**

Create `frontend/src/app/AppShell.tsx`:

```tsx
import { Activity, Database, FlaskConical, Route, Settings } from "lucide-react";
import type { ReactNode } from "react";

export function AppShell({ children }: { children: ReactNode }) {
  return (
    <div className="min-h-screen bg-console-bg text-console-text">
      <aside className="fixed inset-y-0 left-0 hidden w-64 border-r border-console-line bg-console-panel/95 p-5 lg:block">
        <div className="text-xl font-semibold tracking-normal">DataLoop</div>
        <nav className="mt-8 space-y-2 text-sm text-console-muted">
          <NavItem icon={<Activity size={18} />} label="总览" active />
          <NavItem icon={<Database size={18} />} label="数据处理" />
          <NavItem icon={<Route size={18} />} label="导航场景" />
          <NavItem icon={<FlaskConical size={18} />} label="仿真验证" />
          <NavItem icon={<Settings size={18} />} label="系统设置" />
        </nav>
      </aside>
      <main className="min-h-screen p-4 lg:ml-64 lg:p-8">
        <section className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
          {["数据接入", "处理任务", "质量检查", "Agent 状态"].map((title) => (
            <div key={title} className="rounded-lg border border-console-line bg-console-panel2/70 p-5">
              <div className="text-sm text-console-muted">{title}</div>
              <div className="mt-4 h-16 rounded-md border border-console-line/70 bg-console-bg/50" />
            </div>
          ))}
        </section>
        {children}
      </main>
    </div>
  );
}

function NavItem({ icon, label, active = false }: { icon: ReactNode; label: string; active?: boolean }) {
  return (
    <div
      className={
        active
          ? "flex items-center gap-3 rounded-md bg-console-line/45 px-3 py-2 text-console-text"
          : "flex items-center gap-3 rounded-md px-3 py-2"
      }
    >
      {icon}
      <span>{label}</span>
    </div>
  );
}
```

- [ ] **Step 5: Wire shell into app**

Modify `frontend/src/app/App.tsx`:

```tsx
import { AppShell } from "./AppShell";

export function App() {
  return <AppShell>{null}</AppShell>;
}
```

- [ ] **Step 6: Build**

Run:

```bash
cd frontend
npm run build
```

Expected: build succeeds.

- [ ] **Step 7: Commit**

```bash
git add frontend
git commit -m "feat: add DataLoop frontend shell"
```

## Task 9: Add Frontend API Types And Client

**Files:**
- Create: `frontend/src/api/types.ts`
- Create: `frontend/src/api/client.ts`

- [ ] **Step 1: Define TypeScript API contracts**

Create `frontend/src/api/types.ts`:

```ts
export type SessionStatus = "draft" | "active" | "historical";
export type MessageRole = "user" | "assistant" | "system";

export interface SessionRecord {
  id: string;
  title: string;
  status: SessionStatus;
  created_at: string;
  updated_at: string;
}

export interface ChatMessageRecord {
  id: string;
  session_id: string;
  role: MessageRole;
  content: string;
  created_at: string;
}

export interface SessionDetail extends SessionRecord {
  messages: ChatMessageRecord[];
}

export interface AgentEvent {
  type: string;
  source?: string | null;
  run_id?: string | null;
  parent_run_id?: string | null;
  timestamp?: string | null;
  payload: Record<string, unknown>;
}
```

- [ ] **Step 2: Add fetch/WebSocket client**

Create `frontend/src/api/client.ts`:

```ts
import type { AgentEvent, SessionDetail, SessionRecord } from "./types";

async function requestJson<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    headers: { "content-type": "application/json", ...(init?.headers ?? {}) },
    ...init,
  });
  if (!response.ok) {
    throw new Error(await response.text());
  }
  return (await response.json()) as T;
}

export async function createSession(message: string): Promise<SessionRecord> {
  const data = await requestJson<{ session: SessionRecord }>("/api/sessions", {
    method: "POST",
    body: JSON.stringify({ message }),
  });
  return data.session;
}

export async function listSessions(): Promise<SessionRecord[]> {
  const data = await requestJson<{ sessions: SessionRecord[] }>("/api/sessions");
  return data.sessions;
}

export async function getSession(sessionId: string): Promise<SessionDetail> {
  const data = await requestJson<{ session: SessionDetail }>(`/api/sessions/${sessionId}`);
  return data.session;
}

export async function submitTurn(sessionId: string, message: string): Promise<string> {
  const data = await requestJson<{ turn_id: string }>(`/api/sessions/${sessionId}/turns`, {
    method: "POST",
    body: JSON.stringify({ message }),
  });
  return data.turn_id;
}

export async function interruptTurn(sessionId: string): Promise<boolean> {
  const data = await requestJson<{ interrupted: boolean }>(`/api/sessions/${sessionId}/interrupt`, {
    method: "POST",
  });
  return data.interrupted;
}

export function openSessionEvents(sessionId: string, onEvent: (event: AgentEvent) => void): WebSocket {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const socket = new WebSocket(`${protocol}//${window.location.host}/api/sessions/${sessionId}/events`);
  socket.addEventListener("message", (message) => onEvent(JSON.parse(message.data) as AgentEvent));
  return socket;
}
```

- [ ] **Step 3: Build**

Run:

```bash
cd frontend
npm run build
```

Expected: build succeeds.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/api
git commit -m "feat: add DataPilot frontend API client"
```

## Task 10: Build Event Reducer And Zustand Store

**Files:**
- Create: `frontend/src/store/eventReducer.ts`
- Create: `frontend/src/store/eventReducer.test.ts`
- Create: `frontend/src/store/datapilotStore.ts`

- [ ] **Step 1: Add reducer tests**

Create `frontend/src/store/eventReducer.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { applyAgentEvent, createEmptyRunState } from "./eventReducer";

describe("applyAgentEvent", () => {
  it("localizes main thinking text", () => {
    const state = createEmptyRunState();
    applyAgentEvent(state, {
      type: "agent_start",
      source: "main",
      run_id: "main_1",
      parent_run_id: null,
      timestamp: "2026-06-26T10:00:00+08:00",
      payload: {},
    });

    expect(state.activeText).toBe("[Main] 正在思考");
  });

  it("keeps tool end compact without args payload", () => {
    const state = createEmptyRunState();
    applyAgentEvent(state, {
      type: "tool_start",
      source: "navigation.plan",
      run_id: "plan_1",
      parent_run_id: "workflow_1",
      timestamp: "2026-06-26T10:00:00+08:00",
      payload: { call_id: "c1", tool: "classify_navigation_dataset_tool", args: "{\"large\": true}" },
    });
    applyAgentEvent(state, {
      type: "tool_end",
      source: "navigation.plan",
      run_id: "plan_1",
      parent_run_id: "workflow_1",
      timestamp: "2026-06-26T10:00:01+08:00",
      payload: { call_id: "c1", tool: "classify_navigation_dataset_tool", status: "completed" },
    });

    expect(state.timeline.at(-1)?.text).toBe("completed classify_navigation_dataset_tool 1.0s");
  });
});
```

- [ ] **Step 2: Implement reducer**

Create `frontend/src/store/eventReducer.ts`:

```ts
import type { AgentEvent } from "../api/types";

export interface TimelineItem {
  id: string;
  kind: "reasoning" | "tool" | "agent" | "assistant" | "system";
  source: string;
  text: string;
  status?: string;
  runId?: string | null;
  parentRunId?: string | null;
}

interface ActiveTool {
  callId: string;
  runId: string;
  tool: string;
  source: string;
  startedAt: number;
}

export interface RunState {
  timeline: TimelineItem[];
  activeAgents: Record<string, string>;
  activeTools: Record<string, ActiveTool>;
  activeText: string;
  running: boolean;
}

export function createEmptyRunState(): RunState {
  return { timeline: [], activeAgents: {}, activeTools: {}, activeText: "", running: false };
}

export function applyAgentEvent(state: RunState, event: AgentEvent): void {
  const type = event.type;
  const source = event.source ?? "main";
  const runId = event.run_id ?? "";
  const payload = event.payload ?? {};

  if (type === "agent_start" && runId) {
    state.activeAgents[runId] = source;
    state.running = true;
    state.activeText = `[${sourceLabel(source)}] 正在思考`;
    return;
  }

  if (type === "reasoning") {
    const summary = String(payload.summary ?? "").trim();
    if (summary) push(state, event, "reasoning", source, summary);
    return;
  }

  if (type === "tool_start") {
    const callId = String(payload.call_id ?? "").trim();
    const tool = String(payload.tool ?? "unknown_tool").trim();
    if (callId && runId) {
      state.activeTools[`${runId}:${callId}`] = {
        callId,
        runId,
        tool,
        source,
        startedAt: timestampMs(event.timestamp),
      };
      state.activeText = `[${sourceLabel(source)}] 正在运行 ${tool}`;
    }
    return;
  }

  if (type === "tool_end") {
    const callId = String(payload.call_id ?? "").trim();
    const key = `${runId}:${callId}`;
    const active = state.activeTools[key];
    delete state.activeTools[key];
    const tool = String(payload.tool ?? active?.tool ?? "unknown_tool").trim();
    const status = String(payload.status ?? (payload.ok === false ? "failed" : "completed")).trim();
    const elapsed = active ? Math.max((timestampMs(event.timestamp) - active.startedAt) / 1000, 0) : 0;
    push(state, event, "tool", source, `${status} ${tool} ${elapsed.toFixed(1)}s`, status);
    return;
  }

  if (type === "agent_end") {
    delete state.activeAgents[runId];
    state.running = Object.keys(state.activeAgents).length > 0 || Object.keys(state.activeTools).length > 0;
    return;
  }

  if (type === "final") {
    const text = String(payload.text ?? "").trim();
    if (text) push(state, event, "assistant", source, text);
    state.running = false;
    state.activeText = "";
  }
}

function push(
  state: RunState,
  event: AgentEvent,
  kind: TimelineItem["kind"],
  source: string,
  text: string,
  status?: string,
) {
  state.timeline.push({
    id: `${event.run_id ?? "event"}:${state.timeline.length}`,
    kind,
    source,
    text,
    status,
    runId: event.run_id,
    parentRunId: event.parent_run_id,
  });
}

function sourceLabel(source: string): string {
  if (source === "navigation.workflow") return "Workflow";
  if (source === "navigation.workflow.resume") return "Workflow";
  if (source === "navigation.plan") return "Plan";
  if (source === "navigation.executor") return "Executor";
  return "Main";
}

function timestampMs(value: string | null | undefined): number {
  const parsed = Date.parse(value ?? "");
  return Number.isFinite(parsed) ? parsed : Date.now();
}
```

- [ ] **Step 3: Add Zustand store shell**

Create `frontend/src/store/datapilotStore.ts`:

```ts
import { create } from "zustand";
import type { ChatMessageRecord, SessionRecord } from "../api/types";
import { applyAgentEvent, createEmptyRunState, type RunState } from "./eventReducer";

export type SessionMode = "draft_new_session" | "active_session" | "history_session";

interface DataPilotState {
  open: boolean;
  mode: SessionMode;
  currentSessionId: string | null;
  previousActiveSessionId: string | null;
  sessions: SessionRecord[];
  messages: ChatMessageRecord[];
  run: RunState;
  setOpen: (open: boolean) => void;
  enterDraft: () => void;
  setActiveSession: (session: SessionRecord) => void;
  restoreHistory: (session: SessionRecord, messages: ChatMessageRecord[]) => void;
  appendUserMessage: (message: ChatMessageRecord) => void;
  applyEvent: typeof applyAgentEvent;
}

export const useDataPilotStore = create<DataPilotState>((set) => ({
  open: false,
  mode: "draft_new_session",
  currentSessionId: null,
  previousActiveSessionId: null,
  sessions: [],
  messages: [],
  run: createEmptyRunState(),
  setOpen: (open) => set({ open }),
  enterDraft: () =>
    set((state) => ({
      mode: "draft_new_session",
      previousActiveSessionId: state.currentSessionId,
      currentSessionId: null,
      messages: [],
      run: createEmptyRunState(),
    })),
  setActiveSession: (session) =>
    set((state) => ({
      mode: "active_session",
      currentSessionId: session.id,
      previousActiveSessionId: null,
      sessions: [session, ...state.sessions.filter((item) => item.id !== session.id)],
    })),
  restoreHistory: (session, messages) =>
    set({
      mode: "history_session",
      currentSessionId: session.id,
      messages,
      run: createEmptyRunState(),
    }),
  appendUserMessage: (message) => set((state) => ({ messages: [...state.messages, message] })),
  applyEvent: (state, event) => {
    applyAgentEvent(state, event);
  },
}));
```

- [ ] **Step 4: Run reducer tests**

Run:

```bash
cd frontend
npm test
```

Expected: reducer tests pass.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/store
git commit -m "feat: add DataPilot frontend state"
```

## Task 11: Build DataPilot Floating UI

**Files:**
- Create: `frontend/src/components/datapilot/DataPilotButton.tsx`
- Create: `frontend/src/components/datapilot/DataPilotWindow.tsx`
- Create: `frontend/src/components/datapilot/SessionHeader.tsx`
- Create: `frontend/src/components/datapilot/DraftNewSessionView.tsx`
- Create: `frontend/src/components/datapilot/Composer.tsx`
- Modify: `frontend/src/app/App.tsx`

- [ ] **Step 1: Add floating button**

Create `frontend/src/components/datapilot/DataPilotButton.tsx`:

```tsx
import { Bot } from "lucide-react";
import { useDataPilotStore } from "../../store/datapilotStore";

export function DataPilotButton() {
  const open = useDataPilotStore((state) => state.open);
  const setOpen = useDataPilotStore((state) => state.setOpen);

  if (open) return null;

  return (
    <button
      type="button"
      aria-label="Open DataPilot"
      onClick={() => setOpen(true)}
      className="fixed bottom-6 right-6 z-50 grid h-14 w-14 place-items-center rounded-full bg-console-cyan text-console-bg shadow-2xl shadow-cyan-950/50"
    >
      <Bot size={26} />
    </button>
  );
}
```

- [ ] **Step 2: Add composer with stop icon state**

Create `frontend/src/components/datapilot/Composer.tsx`:

```tsx
import { ArrowRight, Plus, Square } from "lucide-react";
import { FormEvent, useState } from "react";

export function Composer({
  placeholder,
  running,
  onSubmit,
  onInterrupt,
}: {
  placeholder: string;
  running: boolean;
  onSubmit: (message: string) => void;
  onInterrupt: () => void;
}) {
  const [value, setValue] = useState("");

  function submit(event: FormEvent) {
    event.preventDefault();
    const message = value.trim();
    if (!message || running) return;
    setValue("");
    onSubmit(message);
  }

  return (
    <form onSubmit={submit} className="flex items-center gap-3 rounded-full border border-blue-400/50 bg-console-bg/80 p-3">
      <Plus className="shrink-0 text-console-text" size={26} />
      <input
        value={value}
        onChange={(event) => setValue(event.target.value)}
        placeholder={placeholder}
        className="min-w-0 flex-1 bg-transparent text-lg text-console-text outline-none placeholder:text-console-muted"
      />
      {running ? (
        <button
          type="button"
          aria-label="Stop current run"
          onClick={onInterrupt}
          className="grid h-12 w-12 place-items-center rounded-full bg-console-cyan text-console-bg"
        >
          <Square size={16} fill="currentColor" />
        </button>
      ) : (
        <button type="submit" aria-label="Send" className="grid h-12 w-12 place-items-center rounded-full bg-console-cyan text-console-bg">
          <ArrowRight size={28} />
        </button>
      )}
    </form>
  );
}
```

- [ ] **Step 3: Add draft new-session view**

Create `frontend/src/components/datapilot/DraftNewSessionView.tsx`:

```tsx
import { Composer } from "./Composer";

export function DraftNewSessionView({ onSubmit }: { onSubmit: (message: string) => void }) {
  return (
    <div className="flex h-full flex-col justify-center px-6 py-10 text-center">
      <h1 className="text-4xl font-semibold tracking-normal text-console-text">开始一个任务</h1>
      <p className="mx-auto mt-4 max-w-md text-base leading-7 text-console-muted">
        描述你的 VLA 数据处理目标，DataPilot 会接入主智能体执行。
      </p>
      <div className="mt-10">
        <Composer placeholder="我们要做什么？" running={false} onSubmit={onSubmit} onInterrupt={() => undefined} />
      </div>
    </div>
  );
}
```

- [ ] **Step 4: Add header and window shell**

Create `frontend/src/components/datapilot/SessionHeader.tsx`:

```tsx
import { History, Plus, X } from "lucide-react";

export function SessionHeader({
  title,
  onHistory,
  onNew,
  onClose,
}: {
  title: string;
  onHistory: () => void;
  onNew: () => void;
  onClose: () => void;
}) {
  return (
    <header className="flex items-center justify-between border-b border-console-line px-4 py-3">
      <div>
        <div className="text-sm font-semibold text-console-text">DataPilot</div>
        <div className="max-w-56 truncate text-xs text-console-muted">{title}</div>
      </div>
      <div className="flex items-center gap-1">
        <button aria-label="History" onClick={onHistory} className="rounded-md p-2 text-console-muted hover:bg-console-line/40">
          <History size={18} />
        </button>
        <button aria-label="New session" onClick={onNew} className="rounded-md p-2 text-console-muted hover:bg-console-line/40">
          <Plus size={18} />
        </button>
        <button aria-label="Close DataPilot" onClick={onClose} className="rounded-md p-2 text-console-muted hover:bg-console-line/40">
          <X size={18} />
        </button>
      </div>
    </header>
  );
}
```

Create `frontend/src/components/datapilot/DataPilotWindow.tsx`:

```tsx
import { useDataPilotStore } from "../../store/datapilotStore";
import { DraftNewSessionView } from "./DraftNewSessionView";
import { SessionHeader } from "./SessionHeader";

export function DataPilotWindow() {
  const open = useDataPilotStore((state) => state.open);
  const mode = useDataPilotStore((state) => state.mode);
  const setOpen = useDataPilotStore((state) => state.setOpen);
  const enterDraft = useDataPilotStore((state) => state.enterDraft);

  if (!open) return null;

  return (
    <section className="fixed bottom-6 right-6 z-50 flex h-[720px] max-h-[calc(100vh-3rem)] w-[560px] max-w-[calc(100vw-3rem)] flex-col overflow-hidden rounded-lg border border-console-line bg-console-panel shadow-2xl shadow-black/50">
      <SessionHeader title={mode === "draft_new_session" ? "新任务" : "当前会话"} onHistory={() => undefined} onNew={enterDraft} onClose={() => setOpen(false)} />
      {mode === "draft_new_session" ? <DraftNewSessionView onSubmit={() => undefined} /> : null}
    </section>
  );
}
```

- [ ] **Step 5: Mount DataPilot into app**

Modify `frontend/src/app/App.tsx`:

```tsx
import { AppShell } from "./AppShell";
import { DataPilotButton } from "../components/datapilot/DataPilotButton";
import { DataPilotWindow } from "../components/datapilot/DataPilotWindow";

export function App() {
  return (
    <AppShell>
      <DataPilotButton />
      <DataPilotWindow />
    </AppShell>
  );
}
```

- [ ] **Step 6: Build**

Run:

```bash
cd frontend
npm run build
```

Expected: build succeeds.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/components/datapilot frontend/src/app/App.tsx
git commit -m "feat: add DataPilot floating window"
```

## Task 12: Wire Draft Submit, Sessions, History, And Interrupt

**Files:**
- Modify: `frontend/src/store/datapilotStore.ts`
- Modify: `frontend/src/components/datapilot/DataPilotWindow.tsx`
- Create: `frontend/src/components/datapilot/SessionHistoryPanel.tsx`
- Create: `frontend/src/components/datapilot/MessageList.tsx`

- [ ] **Step 1: Add history panel**

Create `frontend/src/components/datapilot/SessionHistoryPanel.tsx`:

```tsx
import type { SessionRecord } from "../../api/types";

export function SessionHistoryPanel({
  sessions,
  onSelect,
}: {
  sessions: SessionRecord[];
  onSelect: (session: SessionRecord) => void;
}) {
  return (
    <div className="border-b border-console-line bg-console-bg/70 p-3">
      <div className="space-y-1">
        {sessions.map((session) => (
          <button
            key={session.id}
            type="button"
            onClick={() => onSelect(session)}
            className="block w-full rounded-md px-3 py-2 text-left hover:bg-console-line/40"
          >
            <div className="truncate text-sm text-console-text">{session.title}</div>
            <div className="mt-1 text-xs text-console-muted">{new Date(session.updated_at).toLocaleString()}</div>
          </button>
        ))}
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Add message list**

Create `frontend/src/components/datapilot/MessageList.tsx`:

```tsx
import type { ChatMessageRecord } from "../../api/types";
import type { TimelineItem } from "../../store/eventReducer";
import { AgentRunSummary } from "./AgentRunSummary";

export function MessageList({ messages, timeline }: { messages: ChatMessageRecord[]; timeline: TimelineItem[] }) {
  return (
    <div className="flex-1 overflow-y-auto px-5 py-4">
      <div className="space-y-4">
        {messages.map((message) => (
          <div key={message.id} className={message.role === "user" ? "ml-auto max-w-[80%] rounded-lg bg-console-cyan px-4 py-3 text-console-bg" : "mr-auto max-w-[88%] text-console-text"}>
            {message.content}
          </div>
        ))}
        <AgentRunSummary items={timeline} />
      </div>
    </div>
  );
}
```

- [ ] **Step 3: Add app actions into `DataPilotWindow`**

Modify `frontend/src/components/datapilot/DataPilotWindow.tsx` so draft submit:

```tsx
async function handleDraftSubmit(message: string) {
  const session = await createSession(message);
  setActiveSession(session);
  appendUserMessage({
    id: `local_${Date.now()}`,
    session_id: session.id,
    role: "user",
    content: message,
    created_at: new Date().toISOString(),
  });
  openSessionEvents(session.id, (event) => useDataPilotStore.getState().applyEvent(event));
  await submitTurn(session.id, message);
}
```

Also wire:

```tsx
async function handleHistoryOpen() {
  setSessions(await listSessions());
  setHistoryOpen(true);
}

async function handleSelectHistory(session: SessionRecord) {
  const detail = await getSession(session.id);
  restoreHistory(session, detail.messages);
  setHistoryOpen(false);
}

async function handleInterrupt() {
  const sessionId = useDataPilotStore.getState().currentSessionId;
  if (sessionId) await interruptTurn(sessionId);
}
```

- [ ] **Step 4: Build and run reducer tests**

Run:

```bash
cd frontend
npm test
npm run build
```

Expected: tests and build pass.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/datapilot frontend/src/store/datapilotStore.ts
git commit -m "feat: wire DataPilot session actions"
```

## Task 13: Implement `AgentRunSummary`

**Files:**
- Create: `frontend/src/components/datapilot/AgentRunSummary.tsx`
- Modify: `frontend/src/store/eventReducer.ts`
- Modify: `frontend/src/store/eventReducer.test.ts`

- [ ] **Step 1: Add reducer test for collapsed child-agent summary**

Append to `frontend/src/store/eventReducer.test.ts`:

```ts
it("records child timeline items that AgentRunSummary can fold", () => {
  const state = createEmptyRunState();
  applyAgentEvent(state, {
    type: "reasoning",
    source: "navigation.plan",
    run_id: "plan_1",
    parent_run_id: "workflow_1",
    timestamp: "2026-06-26T10:00:00+08:00",
    payload: { summary: "已读取 2 个文件执行了 1 条命令" },
  });

  expect(state.timeline[0].source).toBe("navigation.plan");
  expect(state.timeline[0].text).toContain("已读取");
});
```

- [ ] **Step 2: Create collapsible run summary**

Create `frontend/src/components/datapilot/AgentRunSummary.tsx`:

```tsx
import { ChevronDown, TerminalSquare } from "lucide-react";
import { useState } from "react";
import type { TimelineItem } from "../../store/eventReducer";

export function AgentRunSummary({ items }: { items: TimelineItem[] }) {
  const childItems = items.filter((item) => item.source !== "main" && item.kind !== "assistant");
  const [expanded, setExpanded] = useState(false);
  if (childItems.length === 0) return null;

  const fileReads = childItems.filter((item) => item.text.includes("读取") || item.text.toLowerCase().includes("read ")).length;
  const commands = childItems.filter((item) => item.text.includes("命令") || item.text.toLowerCase().includes("command")).length;
  const summary = `已读取 ${fileReads} 个文件，执行了 ${commands} 条命令`;

  return (
    <div className="text-console-muted">
      <button type="button" onClick={() => setExpanded((value) => !value)} className="flex items-center gap-2 rounded-md px-2 py-1 hover:bg-console-line/30">
        <TerminalSquare size={16} />
        <span>{summary}</span>
        <ChevronDown size={16} className={expanded ? "rotate-180 transition" : "transition"} />
      </button>
      {expanded ? (
        <div className="mt-3 space-y-2 border-l border-console-line pl-4">
          {childItems.map((item) => (
            <div key={item.id} className="text-sm">
              {item.kind === "tool" ? (
                <span>
                  <span className={item.status === "completed" ? "text-emerald-400" : "text-red-400"}>●</span> {item.text}
                </span>
              ) : (
                <span>[{item.source}] {item.text}</span>
              )}
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}
```

- [ ] **Step 3: Build and test**

Run:

```bash
cd frontend
npm test
npm run build
```

Expected: tests and build pass.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/datapilot/AgentRunSummary.tsx frontend/src/store
git commit -m "feat: render DataPilot agent run summaries"
```

## Task 14: Add Backend Static Serving For Built Frontend

**Files:**
- Modify: `src/vla_data_juicer_agents/web/app.py`
- Modify: `tests/test_web_api.py`

- [ ] **Step 1: Add static fallback test**

Append to `tests/test_web_api.py`:

```python
def test_frontend_index_is_served_when_dist_exists(tmp_path: Path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<div id='root'></div>", encoding="utf-8")
    app = create_app(
        working_dir=str(tmp_path / ".djx"),
        db_path=tmp_path / "sessions.sqlite",
        frontend_dist=dist,
        controller_factory=FakeController,
    )
    client = TestClient(app)

    response = client.get("/")

    assert response.status_code == 200
    assert "root" in response.text
```

- [ ] **Step 2: Modify `create_app` signature**

Update `create_app` to accept:

```python
frontend_dist: str | Path | None = None,
```

At the end of `create_app`, before `return app`, add:

```python
    if frontend_dist is not None and Path(frontend_dist).exists():
        from fastapi.responses import FileResponse
        from fastapi.staticfiles import StaticFiles

        dist_path = Path(frontend_dist)
        assets_path = dist_path / "assets"
        if assets_path.exists():
            app.mount("/assets", StaticFiles(directory=assets_path), name="assets")

        @app.get("/")
        def frontend_index() -> FileResponse:
            return FileResponse(dist_path / "index.html")
```

- [ ] **Step 3: Run Web API tests**

Run:

```bash
pytest tests/test_web_api.py -q
```

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add src/vla_data_juicer_agents/web/app.py tests/test_web_api.py
git commit -m "feat: serve built DataPilot frontend"
```

## Task 15: End-To-End Verification

**Files:**
- Create: `frontend/tests/datapilot.spec.ts`
- Create: `frontend/playwright.config.ts`
- Modify: `README.md`

- [ ] **Step 1: Add Playwright config**

Create `frontend/playwright.config.ts`:

```ts
import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./tests",
  use: {
    baseURL: "http://127.0.0.1:5173",
    ...devices["Desktop Chrome"],
  },
});
```

- [ ] **Step 2: Add UI smoke test**

Create `frontend/tests/datapilot.spec.ts`:

```ts
import { expect, test } from "@playwright/test";

test("opens DataPilot draft task screen", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("Open DataPilot").click();

  await expect(page.getByText("开始一个任务")).toBeVisible();
  await expect(page.getByPlaceholder("我们要做什么？")).toBeVisible();
  await expect(page.getByText("继续任务")).toHaveCount(0);
});
```

- [ ] **Step 3: Add README run instructions**

Append to `README.md`:

```markdown
## DataPilot Web UI

Backend:

```bash
vla-data-agent-web --host 127.0.0.1 --port 8765
```

Frontend development server:

```bash
cd frontend
npm install
npm run dev
```

Open `http://127.0.0.1:5173`, click the bottom-right DataPilot button, and submit the first task from the draft screen.
```
```

- [ ] **Step 4: Run full verification**

Run:

```bash
pytest tests/test_web_session_store.py tests/test_web_session_manager.py tests/test_web_event_stream.py tests/test_web_api.py -q
cd frontend
npm test
npm run build
npm run e2e
```

Expected:

- Backend tests pass.
- Frontend reducer tests pass.
- Frontend build passes.
- Playwright smoke test confirms draft page, placeholder, and no example tags.

- [ ] **Step 5: Commit**

```bash
git add README.md frontend/tests frontend/playwright.config.ts
git commit -m "test: verify DataPilot web UI"
```

## Implementation Notes For The Worker

- Use `git status --short` before each task. Do not overwrite unrelated user changes.
- Keep commits small and aligned with tasks.
- If `npm install` cannot access the registry because of network restrictions, stop and ask for dependency installation approval instead of rewriting the plan around CDN scripts.
- If `FastAPI` dependency installation is blocked, stop and ask for approval. Do not implement a hand-rolled HTTP server.
- When running the local app manually, use two terminals:

```bash
vla-data-agent-web --host 127.0.0.1 --port 8765
cd frontend && npm run dev
```

- First visual QA targets:
  - desktop: `1440x900`
  - laptop: `1280x800`
  - mobile-ish narrow window: `390x844`

## Self-Review

- Spec coverage: This plan covers floating button, draft new-session page, no example tags, DataPilot naming, ChatGPT-style messages, compact tool lines, collapsible child-agent run summaries, session creation timing, history list title/update time only, interrupt behavior, and frontend/backend separation.
- Placeholder scan: No task uses `TBD`, vague future placeholders, or "write tests" without concrete test content.
- Type consistency: Backend uses `session_id` externally in URL paths and `id` in session records. Frontend mirrors `SessionRecord`, `ChatMessageRecord`, and `AgentEvent` fields from backend schemas.

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from vla_data_juicer_agents.capabilities.session.orchestrator import (
    SessionReply,
    VLASessionAgent,
)


AgentFactory = Callable[..., VLASessionAgent]


class SessionController:
    def __init__(
        self,
        *,
        working_dir: str = "./.djx",
        model: str | None = None,
        agent_factory: AgentFactory = VLASessionAgent,
    ) -> None:
        self._working_dir = working_dir
        self._model = model
        self._agent_factory = agent_factory
        self._events: queue.Queue[dict[str, Any]] = queue.Queue()
        self._lock = threading.RLock()
        self._agent: VLASessionAgent | None = None
        self._worker: threading.Thread | None = None
        self._result: SessionReply | None = None
        self._turn_emitted_final = False

    def start(self) -> None:
        with self._lock:
            if self._agent is not None:
                return
            self._agent = self._agent_factory(
                use_llm_router=True,
                working_dir=self._working_dir,
                model=self._model,
                event_callback=self._on_agent_event,
            )

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._worker is not None and self._worker.is_alive()

    def submit_turn(self, message: str) -> None:
        with self._lock:
            if self._agent is None:
                raise RuntimeError("Session controller has not been started.")
            if self._worker is not None:
                if self._worker.is_alive():
                    raise RuntimeError("A session turn is already active.")
                if self._result is not None:
                    raise RuntimeError("Consume the completed turn result before starting another turn.")
            self._turn_emitted_final = False
            self._result = None
            self._worker = threading.Thread(
                target=self._run_turn,
                args=(message,),
                name="vla-tui-turn",
                daemon=True,
            )
            self._worker.start()

    def drain_events(self) -> list[dict[str, Any]]:
        events = []
        while True:
            try:
                events.append(self._events.get_nowait())
            except queue.Empty:
                return events

    def consume_turn_result(self) -> SessionReply:
        with self._lock:
            worker = self._worker
        if worker is None or worker.is_alive():
            raise RuntimeError("No completed turn result is available.")
        worker.join()
        with self._lock:
            result = self._result
            if result is None:
                raise RuntimeError("No completed turn result is available.")
            self._result = None
            self._worker = None
            return result

    def request_interrupt(self) -> bool:
        with self._lock:
            if self._worker is None or not self._worker.is_alive() or self._agent is None:
                return False
            agent = self._agent
        return agent.request_interrupt()

    def _on_agent_event(self, event: dict[str, Any]) -> None:
        copied = dict(event)
        with self._lock:
            if copied.get("type") == "final":
                self._turn_emitted_final = True
        self._events.put(copied)

    def _run_turn(self, message: str) -> None:
        with self._lock:
            agent = self._agent
        if agent is None:
            return
        try:
            result = agent.handle_message(message)
        except Exception as exc:
            text = f"Session turn failed: {exc}"
            result = SessionReply(text=text, stop=False)
            with self._lock:
                emitted_final = self._turn_emitted_final
            if not emitted_final:
                self._on_agent_event(
                    {
                        "type": "final",
                        "source": "main",
                        "run_id": f"turn_{uuid4().hex}",
                        "parent_run_id": None,
                        "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                        "payload": {"text": text, "stop": False},
                    }
                )
        with self._lock:
            self._result = result

"""Transport-neutral events emitted by agent workflows."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4


logger = logging.getLogger(__name__)

Event = dict[str, Any]


class EventSink(Protocol):
    """Destination for normalized agent events."""

    def publish(self, event: Mapping[str, Any]) -> None:
        """Publish an event."""


class CallbackEventSink:
    """Deliver events to an in-process callback."""

    def __init__(self, callback: Callable[[Event], None]) -> None:
        self._callback = callback

    def publish(self, event: Mapping[str, Any]) -> None:
        self._callback(dict(event))


class JsonlEventSink:
    """Append events to a UTF-8 JSON Lines file."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def publish(self, event: Mapping[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as output:
            json.dump(dict(event), output, ensure_ascii=False)
            output.write("\n")


class CompositeEventSink:
    """Publish to every sink, isolating failures between destinations."""

    def __init__(self, *sinks: EventSink) -> None:
        self._sinks = tuple(sinks)

    def publish(self, event: Mapping[str, Any]) -> None:
        for sink in self._sinks:
            try:
                sink.publish(event)
            except Exception:
                logger.exception("Event sink failed: %r", sink)


class EventEmitter:
    """Publish normalized events to one or more sinks."""

    def __init__(self, *sinks: EventSink) -> None:
        self._sinks = tuple(sinks)
        self._composite = CompositeEventSink(*sinks)

    def with_sink(self, sink: EventSink) -> EventEmitter:
        return EventEmitter(*self._sinks, sink)

    def scope(
        self,
        source: str,
        run_id: str | None = None,
        parent_run_id: str | None = None,
    ) -> EventScope:
        return EventScope(
            emitter=self,
            source=source,
            run_id=run_id or str(uuid4()),
            parent_run_id=parent_run_id,
        )

    def publish(self, event: Mapping[str, Any]) -> None:
        self._composite.publish(event)


@dataclass(frozen=True)
class EventScope:
    """Source and run context used to construct normalized events."""

    emitter: EventEmitter
    source: str
    run_id: str
    parent_run_id: str | None = None

    def child(self, source: str, run_id: str | None = None) -> EventScope:
        return self.emitter.scope(
            source=source,
            run_id=run_id,
            parent_run_id=self.run_id,
        )

    def emit(self, event_type: str, payload: Mapping[str, Any]) -> Event:
        event = {
            "type": event_type,
            "source": self.source,
            "run_id": self.run_id,
            "parent_run_id": self.parent_run_id,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "payload": dict(payload),
        }
        self.emitter.publish(event)
        return event

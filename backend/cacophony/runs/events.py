"""Run progress events (design document sections 55 and 86).

Section 55 wants generation to *look* like something: per-entity counters,
throughput, records per second, tokens per second. Section 86 wants the same
facts as structured log lines. These are the same events, so they are emitted
once and consumed twice - by the terminal progress bar, by the WebSocket feed,
and by the store.

The bus is deliberately non-blocking. A slow subscriber - a browser tab on a
bad connection - must never apply backpressure to generation, so a subscriber
that falls behind loses events rather than stalling the run.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import AsyncIterator, Callable

__all__ = ["EventBus", "EventKind", "RunEvent"]


class EventKind(StrEnum):
    RUN_STARTED = "run.started"
    RUN_PROGRESS = "run.progress"
    RUN_PAUSED = "run.paused"
    RUN_RESUMED = "run.resumed"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    RUN_CANCELLED = "run.cancelled"
    JOB_STARTED = "job.started"
    JOB_PROGRESS = "job.progress"
    JOB_CHECKPOINT = "job.checkpoint"
    JOB_COMPLETED = "job.completed"
    JOB_FAILED = "job.failed"
    WARNING = "warning"
    ERROR = "error"


@dataclass(slots=True)
class RunEvent:
    """One thing that happened, in a shape both a UI and a log can use."""

    kind: EventKind
    run_id: str
    message: str = ""
    level: str = "info"
    job_id: int | None = None
    entity: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "run_id": self.run_id,
            "job_id": self.job_id,
            "entity": self.entity,
            "level": self.level,
            "message": self.message,
            "data": self.data,
            "timestamp": self.timestamp,
        }

    def to_store_row(self) -> dict[str, Any]:
        """The shape :class:`cacophony.store.models.RunEvent` expects."""
        from datetime import UTC, datetime

        return {
            "run_id": self.run_id,
            "job_id": self.job_id,
            "level": self.level,
            "event": self.kind.value,
            "entity": self.entity,
            "message": self.message,
            "data": self.data,
            "timestamp": datetime.fromtimestamp(self.timestamp, tz=UTC),
        }


class EventBus:
    """Fan-out of run events to any number of subscribers."""

    def __init__(self, *, history: int = 200, queue_size: int = 256) -> None:
        self._subscribers: list[asyncio.Queue[RunEvent]] = []
        self._sinks: list[Callable[[RunEvent], None]] = []
        self._history: deque[RunEvent] = deque(maxlen=history)
        self._queue_size = queue_size
        #: Events dropped because a subscriber could not keep up.
        self.dropped = 0

    # -- publishing --------------------------------------------------------- #

    def publish(self, event: RunEvent) -> None:
        self._history.append(event)

        for sink in self._sinks:
            sink(event)

        for queue in self._subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # Drop rather than block. A stalled browser tab must not become
                # backpressure on a nine-hour generation run.
                self.dropped += 1

    def emit(self, kind: EventKind, run_id: str, **kwargs: Any) -> RunEvent:
        event = RunEvent(kind=kind, run_id=run_id, **kwargs)
        self.publish(event)
        return event

    # -- subscribing -------------------------------------------------------- #

    def add_sink(self, sink: Callable[[RunEvent], None]) -> Callable[[], None]:
        """Attach a synchronous consumer, such as a logger or the store."""
        self._sinks.append(sink)

        def remove() -> None:
            if sink in self._sinks:
                self._sinks.remove(sink)

        return remove

    async def subscribe(self) -> AsyncIterator[RunEvent]:
        """Yield events until the subscriber stops consuming."""
        queue: asyncio.Queue[RunEvent] = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers.append(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            if queue in self._subscribers:
                self._subscribers.remove(queue)

    def replay(self) -> list[RunEvent]:
        """Recent events, so a late subscriber sees where the run got to."""
        return list(self._history)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

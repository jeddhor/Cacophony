"""The stream service (design document sections 35, 36, 94).

A run has an end and is worth recording; a stream has neither. So live streams
are held here rather than in the run store: they are process state, not
history, and a stream that outlived the server that was producing it would be a
row describing traffic nobody is sending.

    POST   /api/projects/{id}/streams      start
    GET    /api/streams                    list
    GET    /api/streams/{id}               status, rates, destinations
    POST   /api/streams/{id}/retarget      change a rate while it runs
    POST   /api/streams/{id}/pause|resume|stop
    GET    /api/streams/{id}/records       what it just produced
    WS     /api/streams/{id}/feed          the same status, pushed

What the API adds over ``cacophony stream`` is the thing a browser cannot do
for itself: steer. ``retarget`` already existed and was tested, and until now
nothing called it - a rate could be set at launch and never changed. Over HTTP
it becomes what section 94 describes, a workload you turn up while watching
what it does.

Streams are stopped on shutdown rather than abandoned, so a server that goes
away stops sending traffic to somebody's syslog collector.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from ..core.errors import CacophonyError
from ..live.rates import parse_rate
from ..live.sinks import MemorySink, create_sink
from ..live.stream import LiveStream, StreamConfig

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import AsyncIterator, Sequence

    from ..schema.plan import CompiledProject

__all__ = ["StreamService"]

#: How often the WebSocket pushes a status frame. Fast enough to look live,
#: slow enough that a stream at 50,000/s is not also serialising JSON for a
#: browser twenty times a second.
FEED_INTERVAL = 0.5


class StreamRecord:
    """One live stream, and what the API knows about it."""

    def __init__(
        self,
        stream: LiveStream,
        *,
        project_id: int,
        project_name: str,
        destinations: Sequence[str],
    ) -> None:
        self.stream = stream
        self.project_id = project_id
        self.project_name = project_name
        self.destinations = list(destinations)
        self.created_at = time.time()
        self.task: asyncio.Task[Any] | None = None

    @property
    def id(self) -> str:
        return self.stream.id

    @property
    def memory(self) -> MemorySink | None:
        """The sink a browser can read back, if this stream has one."""
        for sink in self.stream.config.sinks:
            if isinstance(sink, MemorySink):
                return sink
        return None

    def describe(self) -> dict[str, Any]:
        stream = self.stream
        return {
            "id": stream.id,
            "project_id": self.project_id,
            "project": self.project_name,
            "state": stream.state,
            "error": stream.error,
            "created_at": self.created_at,
            "destinations": self.destinations,
            "config": stream.config.to_dict(),
            "stats": stream.stats.to_dict(),
            "entities": [entity.to_dict() for entity in stream.streams.values()],
            "sinks": [sink.describe() for sink in stream.config.sinks],
        }


class StreamService:
    """Owns the running streams.

    Deliberately not backed by the store. Restarting the server ends the
    streams, which is correct: nothing was persisted because nothing about a
    stream is a fact about the past.
    """

    def __init__(self) -> None:
        self._streams: dict[str, StreamRecord] = {}

    # -- lifecycle ------------------------------------------------------------ #

    def start(
        self,
        compiled: CompiledProject,
        *,
        project_id: int,
        project_name: str,
        rates: dict[str, Any],
        destinations: Sequence[str | dict[str, Any]] | None = None,
        keep_records: int = MemorySink.DEFAULT_KEEP,
        **options: Any,
    ) -> dict[str, Any]:
        """Build a stream, start it, and return its first status.

        A stream started over the API always keeps a window of what it produced
        (``keep_records``), because the caller is a page rather than a terminal
        and has no other way to see whether the data is what it wanted. Set
        ``keep_records`` to 0 for a pure workload generator.
        """
        sinks = [create_sink(spec) for spec in (destinations or [])]
        described = [_describe_destination(spec) for spec in (destinations or [])]
        if keep_records:
            sinks.append(MemorySink(keep=keep_records))

        if not sinks:
            raise CacophonyError(
                "a stream needs somewhere to go: give at least one destination, or "
                "keep_records greater than zero to sample it in the browser"
            )

        # Rates arrive as people write them - "250/s", "8 per minute" - and go
        # through the same parser the CLI uses, so a rate that works in a
        # terminal works in a request.
        config = StreamConfig(
            rates={name: parse_rate(value) for name, value in rates.items()},
            sinks=sinks,
            **options,
        )
        stream = LiveStream(compiled, config)
        record = StreamRecord(
            stream,
            project_id=project_id,
            project_name=project_name,
            destinations=described,
        )
        self._streams[stream.id] = record
        record.task = asyncio.create_task(
            self._execute(record), name=f"cacophony-stream-{stream.id}"
        )
        return record.describe()

    async def _execute(self, record: StreamRecord) -> Any:
        try:
            return await record.stream.run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A stream that dies has to say so on its own status, or the page
            # watching it shows a rate of zero and no reason.
            record.stream.state = "failed"
            record.stream.error = str(exc)
            return None

    # -- questions ------------------------------------------------------------ #

    def get(self, stream_id: str) -> StreamRecord | None:
        return self._streams.get(stream_id)

    def require(self, stream_id: str) -> StreamRecord:
        record = self._streams.get(stream_id)
        if record is None:
            raise CacophonyError(f"No stream {stream_id}.")
        return record

    def listing(self, *, project_id: int | None = None) -> list[dict[str, Any]]:
        return [
            record.describe()
            for record in self._streams.values()
            if project_id is None or record.project_id == project_id
        ]

    def active(self) -> list[str]:
        return [stream_id for stream_id, record in self._streams.items() if record.stream.running]

    def records(
        self, stream_id: str, *, limit: int = 50, entity: str | None = None
    ) -> list[dict[str, Any]]:
        """What the stream has just produced.

        Empty rather than an error when the stream keeps no window: a workload
        generator pointed at a syslog collector is doing exactly what it was
        asked to, and the page should say "not sampled" rather than "failed".
        """
        sink = self.require(stream_id).memory
        return sink.recent(limit=limit, entity=entity) if sink is not None else []

    # -- control -------------------------------------------------------------- #

    def retarget(self, stream_id: str, entity: str, rate: str | float) -> dict[str, Any]:
        record = self.require(stream_id)
        parsed = record.stream.retarget(entity, rate)
        return {"entity": entity, "rate": parsed.render(), "per_second": parsed.per_second}

    def pause(self, stream_id: str) -> bool:
        record = self.require(stream_id)
        if not record.stream.running:
            return False
        record.stream.pause()
        return True

    def resume(self, stream_id: str) -> bool:
        record = self.require(stream_id)
        if record.stream.state != "paused":
            return False
        record.stream.unpause()
        return True

    async def stop(self, stream_id: str, *, timeout: float = 10.0) -> bool:
        """Ask a stream to finish, and wait for it to close its destinations."""
        record = self.require(stream_id)
        if not record.stream.running:
            return False
        record.stream.stop()
        if record.task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(record.task), timeout=timeout)
            except TimeoutError:
                record.task.cancel()
        return True

    def forget(self, stream_id: str) -> bool:
        """Drop a finished stream from the registry."""
        record = self._streams.get(stream_id)
        if record is None or record.stream.running:
            return False
        del self._streams[stream_id]
        return True

    # -- the feed -------------------------------------------------------------- #

    async def feed(self, stream_id: str) -> AsyncIterator[dict[str, Any]]:
        """Push status until the stream finishes (section 55's shape, for streams).

        Polled rather than event-driven, on purpose. A run emits a handful of
        events a minute and a socket per event is right; a stream produces tens
        of thousands of records a second, and a frame per batch would make the
        dashboard the most expensive thing in the process.
        """
        record = self.get(stream_id)
        if record is None:
            return
        while True:
            yield record.describe()
            if not record.stream.running:
                return
            await asyncio.sleep(FEED_INTERVAL)

    # -- shutdown -------------------------------------------------------------- #

    async def shutdown(self) -> None:
        """Stop every stream, so the server does not outlive its own traffic."""
        for record in self._streams.values():
            record.stream.stop()
        tasks = [record.task for record in self._streams.values() if record.task is not None]
        pending = [task for task in tasks if not task.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    def describe(self) -> dict[str, Any]:
        return {"streams": len(self._streams), "active_streams": self.active()}


def _describe_destination(spec: str | dict[str, Any]) -> str:
    """A destination as the caller wrote it, with any credentials left out."""
    if isinstance(spec, dict):
        kind = str(spec.get("type", "?"))
        target = spec.get("url") or spec.get("path") or spec.get("host") or spec.get("topic")
        return f"{kind}://{target}" if target else kind
    return str(spec)

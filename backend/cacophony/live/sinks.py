"""Stream destinations (design document sections 35, 94).

    Possible destinations: Kafka, HTTP, syslog, database, file stream.

A sink is deliberately smaller than an :class:`~cacophony.core.interfaces.OutputWriter`.
A writer owns a file: it opens it, writes a header, writes a footer, and the
run ends. A stream has no end, so a sink only has to accept a batch and keep
going - and, crucially, to say what it did with it, because a workload
generator that quietly drops the events it could not deliver is measuring
nothing.

Three properties every sink here holds.

**Failure is counted, not fatal.** A syslog server that goes away must not end
a stream that has been running for six hours. Delivery failures are recorded
and reported; whether that matters is the operator's call, and
``--on-error abort`` is available for when it does.

**Backpressure is real.** An HTTP endpoint that takes 400ms per request cannot
absorb 250 events a second, and pretending otherwise fills memory until the
process dies. A sink that cannot keep up says so, and the stream slows to what
the destination can actually take rather than buffering without limit.

**Nothing is buffered forever.** Sinks flush on a batch or on a deadline,
whichever comes first, so a stream at eight events a minute still delivers
them within seconds rather than when a buffer eventually fills.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..core.errors import OutputError
from ..core.record import to_jsonable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from ..core.record import GeneratedRecord

__all__ = [
    "SINK_TYPES",
    "DeliveryStats",
    "FileStreamSink",
    "HttpSink",
    "KafkaSink",
    "MemorySink",
    "StdoutSink",
    "StreamSink",
    "SyslogSink",
    "create_sink",
]

#: Syslog severities and facilities, for the PRI calculation of RFC 5424.
_FACILITY_LOCAL0 = 16
_SEVERITY_INFO = 6


@dataclass(slots=True)
class DeliveryStats:
    """What actually reached the destination."""

    delivered: int = 0
    failed: int = 0
    bytes_sent: int = 0
    batches: int = 0
    last_error: str | None = None
    #: Seconds spent inside the destination, for the backpressure display.
    seconds_blocked: float = 0.0

    @property
    def success_rate(self) -> float:
        total = self.delivered + self.failed
        return self.delivered / total if total else 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "delivered": self.delivered,
            "failed": self.failed,
            "batches": self.batches,
            "bytes_sent": self.bytes_sent,
            "success_rate": round(self.success_rate, 6),
            "seconds_blocked": round(self.seconds_blocked, 3),
            "last_error": self.last_error,
        }


class StreamSink:
    """Somewhere a stream's records go."""

    #: Registry key.
    name = "sink"

    def __init__(self, **options: Any) -> None:
        self.options = options
        self.stats = DeliveryStats()
        #: Records are rendered once, here, so a sink that sends the same batch
        #: to two places does not serialise it twice.
        self.template = str(options.get("format") or "json")

    async def open(self) -> None:
        """Prepare the destination. Called once, before the first batch."""

    async def close(self) -> None:
        """Release the destination. Called once, when the stream stops."""

    async def send(self, records: Sequence[GeneratedRecord]) -> int:
        """Deliver a batch and return how many records got through."""
        raise NotImplementedError

    # -- helpers ------------------------------------------------------------- #

    def render(self, record: GeneratedRecord) -> str:
        return json.dumps(record.to_dict(jsonable=True), ensure_ascii=False, default=str)

    def _note(self, delivered: int, failed: int, size: int, error: str | None = None) -> int:
        self.stats.delivered += delivered
        self.stats.failed += failed
        self.stats.bytes_sent += size
        self.stats.batches += 1
        if error:
            self.stats.last_error = error
        return delivered

    def describe(self) -> dict[str, Any]:
        return {"sink": self.name, **self.stats.to_dict()}


# --------------------------------------------------------------------------- #
# Files and terminals
# --------------------------------------------------------------------------- #


class StdoutSink(StreamSink):
    """One JSON object per line, on standard output.

    The sink that makes a stream composable with everything else::

        cacophony stream project.yaml --to stdout | jq 'select(.risk_score > 90)'
    """

    name = "stdout"

    async def send(self, records: Sequence[GeneratedRecord]) -> int:
        import sys

        payload = "".join(self.render(record) + "\n" for record in records)
        sys.stdout.write(payload)
        sys.stdout.flush()
        return self._note(len(records), 0, len(payload))


class MemorySink(StreamSink):
    """The last ``keep`` records, and nothing older.

    What a browser needs and a terminal does not. The Studio's streaming page
    shows the records going past, and a page cannot tail a file on the server
    or read the process's stdout - so a stream started over the API keeps a
    bounded window of what it produced and the page reads that.

    Bounded by construction, with a ``deque``. A sink that accumulated for the
    benefit of a dashboard would defeat the whole point of a stream, which is
    that it runs for hours in constant memory.
    """

    name = "memory"

    #: Enough to fill a screen several times over, small enough that a stream
    #: at 50,000/s costs the same as one at 5/s.
    DEFAULT_KEEP = 200

    def __init__(self, **options: Any) -> None:
        super().__init__(**options)
        self.keep = max(1, int(options.get("keep", self.DEFAULT_KEEP)))
        #: The entity is kept beside the record rather than mixed into it, so a
        #: schema with a field called ``entity`` cannot shadow the label.
        self.records: deque[dict[str, Any]] = deque(maxlen=self.keep)
        self._sequence = 0

    async def send(self, records: Sequence[GeneratedRecord]) -> int:
        size = 0
        for record in records:
            values = record.to_dict(jsonable=True)
            self._sequence += 1
            self.records.append({"seq": self._sequence, "entity": record.entity, "record": values})
            size += len(str(values))
        return self._note(len(records), 0, size)

    def recent(self, limit: int | None = None, entity: str | None = None) -> list[dict[str, Any]]:
        """The most recent records first, optionally for one entity."""
        rows = [row for row in reversed(self.records) if entity is None or row["entity"] == entity]
        return rows[: limit or self.keep]

    def describe(self) -> dict[str, Any]:
        return {**super().describe(), "keep": self.keep, "held": len(self.records)}


class FileStreamSink(StreamSink):
    """Append to a file, rotating it by size (section 35's "file stream").

    Rotation is what makes this different from the JSON Lines *writer*: a
    stream has no end, so an unrotated file grows until the disk does not.
    """

    name = "file"

    def __init__(self, **options: Any) -> None:
        super().__init__(**options)
        self.path = Path(str(options.get("path") or "stream.jsonl"))
        #: Bytes before the file is rolled. 0 disables rotation.
        self.rotate_bytes = int(options.get("rotate_bytes", 256 * 1024 * 1024))
        self.keep = int(options.get("keep", 5))
        self._handle: Any = None
        self._written = 0

    async def open(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = self.path.open("a", encoding="utf-8")
            self._written = self.path.stat().st_size
        except OSError as exc:
            raise OutputError(f"could not open {self.path} for streaming: {exc}") from exc

    async def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    async def send(self, records: Sequence[GeneratedRecord]) -> int:
        if self._handle is None:
            await self.open()
        payload = "".join(self.render(record) + "\n" for record in records)
        try:
            self._handle.write(payload)
            self._handle.flush()
        except (OSError, ValueError) as exc:
            # ValueError as well as OSError: a handle closed underneath us -
            # by a rotation that failed, or a filesystem that went away -
            # raises "I/O operation on closed file", and a stream that has run
            # for six hours must not die of it.
            return self._note(0, len(records), 0, str(exc))

        self._written += len(payload)
        if self.rotate_bytes and self._written >= self.rotate_bytes:
            self._rotate()
        return self._note(len(records), 0, len(payload))

    def _rotate(self) -> None:
        """Roll the file, keeping a bounded number of predecessors."""
        assert self._handle is not None
        self._handle.close()
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        rolled = self.path.with_name(f"{self.path.stem}.{stamp}{self.path.suffix}")
        try:
            self.path.rename(rolled)
            previous = sorted(self.path.parent.glob(f"{self.path.stem}.*{self.path.suffix}"))
            for stale in previous[: max(0, len(previous) - self.keep)]:
                stale.unlink(missing_ok=True)
        except OSError as exc:  # pragma: no cover - filesystem specific
            self.stats.last_error = f"rotation failed: {exc}"
        self._handle = self.path.open("a", encoding="utf-8")
        self._written = 0

    def describe(self) -> dict[str, Any]:
        return {**super().describe(), "path": str(self.path)}


# --------------------------------------------------------------------------- #
# Syslog (section 35)
# --------------------------------------------------------------------------- #


class SyslogSink(StreamSink):
    """RFC 5424 (or 3164) messages over UDP or TCP.

    The destination that makes Cacophony a SIEM workload generator: point it at
    a collector and the events arrive looking like events, not like a file
    somebody imported.

    Options:
        ``host`` / ``port``   default ``localhost:514``
        ``protocol``          ``udp`` (default) or ``tcp``
        ``rfc``               ``5424`` (default) or ``3164``
        ``facility`` / ``severity``
        ``app_name``          defaults to the entity's name
    """

    name = "syslog"

    def __init__(self, **options: Any) -> None:
        super().__init__(**options)
        self.host = str(options.get("host") or "localhost")
        self.port = int(options.get("port", 514))
        self.protocol = str(options.get("protocol") or "udp").lower()
        self.rfc = str(options.get("rfc") or "5424")
        self.facility = int(options.get("facility", _FACILITY_LOCAL0))
        self.severity = int(options.get("severity", _SEVERITY_INFO))
        self.app_name = options.get("app_name")
        self.hostname = str(options.get("hostname") or socket.gethostname())
        #: UDP datagrams above this are silently truncated by most stacks, so
        #: the message is trimmed here where it can be done tidily.
        self.max_bytes = int(options.get("max_bytes", 8192 if self.protocol == "udp" else 65535))

        self._socket: Any = None
        self._writer: Any = None

    async def open(self) -> None:
        if self.protocol == "tcp":
            try:
                _reader, self._writer = await asyncio.open_connection(self.host, self.port)
            except OSError as exc:
                raise OutputError(
                    f"could not connect to syslog at {self.host}:{self.port}: {exc}"
                ) from exc
        else:
            self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            # Already gone is a fine way for a connection to be closed.
            with contextlib.suppress(OSError):
                await self._writer.wait_closed()
            self._writer = None
        if self._socket is not None:
            self._socket.close()
            self._socket = None

    async def send(self, records: Sequence[GeneratedRecord]) -> int:
        delivered = 0
        failed = 0
        size = 0
        error: str | None = None

        for record in records:
            message = self.frame(record).encode("utf-8")[: self.max_bytes]
            try:
                if self.protocol == "tcp":
                    if self._writer is None:
                        await self.open()
                    # Octet counting (RFC 6587): a stream needs framing, and
                    # newline framing breaks the moment a value contains one.
                    self._writer.write(f"{len(message)} ".encode() + message)
                else:
                    if self._socket is None:
                        await self.open()
                    self._socket.sendto(message, (self.host, self.port))
                delivered += 1
                size += len(message)
            except OSError as exc:
                failed += 1
                error = str(exc)

        if self.protocol == "tcp" and self._writer is not None and delivered:
            try:
                await self._writer.drain()
            except OSError as exc:  # pragma: no cover - connection lost mid-batch
                error = str(exc)

        return self._note(delivered, failed, size, error)

    def frame(self, record: GeneratedRecord) -> str:
        """One syslog message."""
        priority = self.facility * 8 + self.severity
        app = self.app_name or record.entity
        payload = self.render(record)

        if self.rfc == "3164":
            stamp = datetime.now().strftime("%b %d %H:%M:%S")
            return f"<{priority}>{stamp} {self.hostname} {app}: {payload}"

        stamp = datetime.now().astimezone().isoformat(timespec="milliseconds")
        record_id = record.id or "-"
        return f"<{priority}>1 {stamp} {self.hostname} {app} - {record_id} - {payload}"

    def describe(self) -> dict[str, Any]:
        return {
            **super().describe(),
            "endpoint": f"{self.protocol}://{self.host}:{self.port}",
            "rfc": self.rfc,
        }


# --------------------------------------------------------------------------- #
# HTTP (section 35)
# --------------------------------------------------------------------------- #


class HttpSink(StreamSink):
    """POST batches to an endpoint.

    Options:
        ``url``       required
        ``method``    ``POST`` by default
        ``headers``   a mapping
        ``body``      ``ndjson`` (default), ``array`` or ``single``
        ``timeout_seconds``

    ``ndjson`` is the default because it is what log collectors take, and
    because one request per batch is the difference between a workload
    generator and a denial of service against your own endpoint.
    """

    name = "http"

    def __init__(self, **options: Any) -> None:
        super().__init__(**options)
        url = options.get("url")
        if not url:
            raise OutputError("an http sink needs a 'url'")
        self.url = str(url)
        self.method = str(options.get("method") or "POST").upper()
        self.headers = dict(options.get("headers") or {})
        self.body = str(options.get("body") or "ndjson")
        self.timeout = float(options.get("timeout_seconds", 10.0))
        self._client: Any = None

    async def open(self) -> None:
        import httpx

        self._client = httpx.AsyncClient(timeout=self.timeout)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def send(self, records: Sequence[GeneratedRecord]) -> int:
        import httpx

        if self._client is None:
            await self.open()

        payload, content_type = self._payload(records)
        headers = {"content-type": content_type, **self.headers}

        started = time.monotonic()
        try:
            response = await self._client.request(
                self.method, self.url, content=payload, headers=headers
            )
        except httpx.HTTPError as exc:
            self.stats.seconds_blocked += time.monotonic() - started
            return self._note(0, len(records), 0, str(exc))

        self.stats.seconds_blocked += time.monotonic() - started
        if response.status_code >= 400:
            return self._note(
                0, len(records), 0, f"HTTP {response.status_code}: {response.text[:120]}"
            )
        return self._note(len(records), 0, len(payload))

    def _payload(self, records: Sequence[GeneratedRecord]) -> tuple[bytes, str]:
        rows = [record.to_dict(jsonable=True) for record in records]
        if self.body == "array":
            return json.dumps(rows, default=str).encode("utf-8"), "application/json"
        if self.body == "single":
            return json.dumps(rows[0] if rows else {}, default=str).encode(
                "utf-8"
            ), "application/json"
        lines = "\n".join(json.dumps(row, default=str) for row in rows)
        return lines.encode("utf-8"), "application/x-ndjson"

    def describe(self) -> dict[str, Any]:
        return {**super().describe(), "url": self.url, "body": self.body}


# --------------------------------------------------------------------------- #
# Kafka (section 35)
# --------------------------------------------------------------------------- #


class KafkaSink(StreamSink):
    """Produce to a Kafka topic.

    Needs ``aiokafka``, which is an optional dependency: a Kafka client is a
    substantial thing to install, and most people streaming to syslog or a file
    should not be made to carry one. The import failure says exactly that
    rather than surfacing as ``ModuleNotFoundError`` from inside a stream.

    Options:
        ``brokers``    ``localhost:9092`` by default
        ``topic``      required
        ``key_field``  a field to partition by - an account id keeps one
                       account's events on one partition, and therefore in
                       order, which is usually what a consumer assumes
        ``acks`` / ``compression``
    """

    name = "kafka"

    def __init__(self, **options: Any) -> None:
        super().__init__(**options)
        topic = options.get("topic")
        if not topic:
            raise OutputError("a kafka sink needs a 'topic'")
        self.topic = str(topic)
        self.brokers = str(
            options.get("brokers") or options.get("bootstrap_servers") or "localhost:9092"
        )
        self.key_field = options.get("key_field")
        self.acks = options.get("acks", 1)
        self.compression = options.get("compression")
        self._producer: Any = None

    async def open(self) -> None:
        try:
            from aiokafka import AIOKafkaProducer
        except ImportError as exc:
            raise OutputError(
                "the kafka sink needs the 'aiokafka' package, which Cacophony does not "
                "install by default: pip install 'cacophony[kafka]'"
            ) from exc

        self._producer = AIOKafkaProducer(
            bootstrap_servers=self.brokers,
            acks=self.acks,
            compression_type=self.compression,
        )
        try:
            await self._producer.start()
        except Exception as exc:
            raise OutputError(f"could not reach Kafka at {self.brokers}: {exc}") from exc

    async def close(self) -> None:
        if self._producer is not None:
            await self._producer.stop()
            self._producer = None

    async def send(self, records: Sequence[GeneratedRecord]) -> int:
        if self._producer is None:
            await self.open()

        delivered = 0
        failed = 0
        size = 0
        error: str | None = None
        started = time.monotonic()

        for record in records:
            payload = self.render(record).encode("utf-8")
            key = None
            if self.key_field:
                raw = record.values.get(self.key_field)
                key = str(to_jsonable(raw)).encode("utf-8") if raw is not None else None
            try:
                await self._producer.send_and_wait(self.topic, payload, key=key)
                delivered += 1
                size += len(payload)
            except Exception as exc:
                failed += 1
                error = str(exc)

        self.stats.seconds_blocked += time.monotonic() - started
        return self._note(delivered, failed, size, error)

    def describe(self) -> dict[str, Any]:
        return {**super().describe(), "brokers": self.brokers, "topic": self.topic}


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #


SINK_TYPES: dict[str, type[StreamSink]] = {
    "stdout": StdoutSink,
    "memory": MemorySink,
    "file": FileStreamSink,
    "syslog": SyslogSink,
    "http": HttpSink,
    "kafka": KafkaSink,
}


def create_sink(spec: str | dict[str, Any]) -> StreamSink:
    """Build a sink from ``"syslog"``, ``"syslog://host:514"`` or a mapping."""
    options = _options_for(spec)
    kind = str(options.pop("type", "stdout")).lower()

    sink_class = SINK_TYPES.get(kind)
    if sink_class is None:
        known = ", ".join(sorted(SINK_TYPES))
        raise OutputError(f"unknown stream destination '{kind}'. Available: {known}")
    return sink_class(**options)


def _options_for(spec: str | dict[str, Any]) -> dict[str, Any]:
    """Normalise the three ways a destination can be written."""
    if isinstance(spec, dict):
        return dict(spec)

    text = str(spec).strip()
    if "://" not in text:
        return {"type": text}

    scheme, _, remainder = text.partition("://")
    scheme = scheme.lower()

    if scheme in ("http", "https"):
        return {"type": "http", "url": text}
    if scheme == "file":
        return {"type": "file", "path": remainder}
    if scheme in ("syslog", "syslog+udp", "syslog+tcp"):
        host, _, port = remainder.partition(":")
        return {
            "type": "syslog",
            "host": host or "localhost",
            "port": int(port or 514),
            "protocol": "tcp" if scheme.endswith("tcp") else "udp",
        }
    if scheme == "kafka":
        brokers, _, topic = remainder.rpartition("/")
        return {"type": "kafka", "brokers": brokers or "localhost:9092", "topic": topic}
    if scheme == "memory":
        # ``memory://200`` or bare ``memory://``. How many records to keep is
        # the only thing this destination has to be told.
        keep = remainder.strip("/")
        return {"type": "memory", **({"keep": int(keep)} if keep.isdigit() else {})}
    raise OutputError(f"unknown stream destination scheme '{scheme}://'")

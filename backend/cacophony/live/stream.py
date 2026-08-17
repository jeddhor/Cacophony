"""Continuous generation (design document sections 35, 94).

    Produce approximately 250 authentication events/sec, 50 endpoint
    events/sec, 8 alerts/minute. This transforms Cacophony into a workload
    generator.

A stream is not a run that never finishes. Three things genuinely differ, and
being clear about them is most of the design.

**There is no total.** A run knows it will produce ten million records; a
stream knows only a rate. Record indices simply keep going, which costs
nothing here because a record's seed is derived from its index (section 75) -
event 4,823,913 of a stream is the same record it would have been in a batch
run of the same schema. That is what makes a stream reproducible at all, and
it is why ``--from`` can resume one exactly where it left off.

**Time is now.** A batch dataset covers a period that has already happened; a
stream produces events that are happening. So ``event_time`` reads the wall
clock rather than the project's timeline, and the timeline's *shape* is
reused as a rate multiplier instead - at three in the morning the stream slows
down, because that is what the shape said the world does.

**Subjects interleave.** A batch run lays each subject's events out in a
contiguous block, which is what makes ordered histories and folded state cheap
(see :mod:`cacophony.simulation.allocation`). A stream cannot: events arrive
mixed together, because that is what a stream *is*. So subjects are drawn per
event, and per-subject state is held in memory - bounded by the number of
subjects, which is the same trade a real streaming system makes.

Backpressure is taken seriously. A sink that cannot keep up slows the stream
rather than filling memory; the shortfall is measured and reported, because a
workload generator that silently fails to generate the workload is worse than
no workload generator.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..core.errors import CacophonyError
from .rates import Rate, RateLimiter, parse_rate

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from ..core.record import GeneratedRecord
    from ..generation.engine import GenerationEngine
    from ..schema.plan import CompiledProject
    from .sinks import StreamSink

__all__ = ["EntityStream", "LiveStream", "StreamConfig", "StreamStats"]


@dataclass(slots=True)
class StreamConfig:
    """How a stream behaves."""

    #: Entity name to rate. An entity with no rate does not stream.
    rates: dict[str, Rate] = field(default_factory=dict)
    #: Where records go. Several destinations receive the same records.
    sinks: list[Any] = field(default_factory=list)
    #: Most records to gather before delivering. Higher is more efficient and
    #: less prompt; at eight events a minute the deadline is what matters.
    batch_size: int = 100
    #: Deliver a partial batch after this long, so a slow stream is not silent.
    flush_seconds: float = 1.0
    #: Stop after this long. None runs until interrupted (section 94's
    #: long-running simulations).
    duration_seconds: float | None = None
    #: Stop after this many records, whichever comes first.
    max_records: int | None = None
    #: Start indices here, so a restarted stream continues rather than repeats.
    start_index: int = 0
    #: Timestamp events with the wall clock rather than the project timeline.
    live_time: bool = True
    #: Let the timeline's shape modulate the rate: quiet at night, busy on
    #: Tuesday. A workload that is flat around the clock is not a workload.
    follow_shape: bool = False
    #: Most records one tick may materialise per entity, whatever the rate says.
    #:
    #: This is a memory bound, not a throughput one. Without it a stream asked
    #: for an impossible rate builds a list of that many records in a single
    #: chunk - five million records is gigabytes, and the process dies rather
    #: than reporting that it could not keep up. Tokens above this stay in the
    #: bucket, the stream runs at whatever it can actually do, and `attainment`
    #: says so. Which is what "slows down rather than filling memory" has to
    #: mean.
    max_in_flight: int = 20_000
    #: What to do when a destination rejects records: ``continue`` or ``abort``.
    on_error: str = "continue"
    #: Scenario windows repeat over this many seconds. A stream has no end, so
    #: an incident declared at 0.62 of "the period" has to mean something else:
    #: here it recurs, which is what a detection exercise wants anyway.
    scenario_cycle_seconds: float = 3600.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "rates": {name: rate.render() for name, rate in self.rates.items()},
            "batch_size": self.batch_size,
            "flush_seconds": self.flush_seconds,
            "duration_seconds": self.duration_seconds,
            "max_records": self.max_records,
            "live_time": self.live_time,
            "follow_shape": self.follow_shape,
            "scenario_cycle_seconds": self.scenario_cycle_seconds,
            "max_in_flight": self.max_in_flight,
        }


@dataclass(slots=True)
class StreamStats:
    """What a stream is doing, right now."""

    started_at: float = field(default_factory=time.monotonic)
    generated: int = 0
    delivered: int = 0
    dropped: int = 0
    #: Per-entity counters, for the dashboard's per-stream rows.
    by_entity: dict[str, int] = field(default_factory=dict)
    #: A short window of recent throughput, so the displayed rate reflects now
    #: rather than the average since the stream started six hours ago.
    _window: list[tuple[float, int]] = field(default_factory=list)

    @property
    def elapsed(self) -> float:
        return max(1e-9, time.monotonic() - self.started_at)

    #: What the stream is being asked to produce per second, right now.
    target_rate: float = 0.0
    #: Records the stream *should* have produced by the moment the target last
    #: changed, and when that was. A rate turned up mid-stream (section 94)
    #: makes a lifetime denominator wrong for the whole history - the first ten
    #: minutes were not a shortfall against a rate that was only requested in
    #: the eleventh - so the request is integrated over time rather than
    #: assumed constant.
    _owed: float = 0.0
    _target_since: float = field(default_factory=time.monotonic)

    def set_target(self, rate: float) -> None:
        """Record a new requested rate from this moment on."""
        now = time.monotonic()
        self._owed += self.target_rate * (now - self._target_since)
        self._target_since = now
        self.target_rate = rate

    @property
    def expected(self) -> float:
        """Records the request implies by now, across every rate it has had."""
        return self._owed + self.target_rate * (time.monotonic() - self._target_since)

    @property
    def mean_rate(self) -> float:
        return self.generated / self.elapsed

    @property
    def attainment(self) -> float:
        """Produced over requested, integrated over the run.

        Below 1.0 means the stream could not keep up - almost always because
        generation or a destination is slower than the rate asked for. A
        workload generator that silently produces less workload than requested
        is measuring the wrong thing, so this is reported rather than inferred.
        """
        expected = self.expected
        return self.generated / expected if expected > 0 else 1.0

    #: Samples kept for the recent-throughput window. Bounded because a stream
    #: is meant to run for hours.
    _WINDOW_LIMIT = 512

    def note(self, entity: str, count: int) -> None:
        self.generated += count
        self.by_entity[entity] = self.by_entity.get(entity, 0) + count
        self._window.append((time.monotonic(), count))
        if len(self._window) > self._WINDOW_LIMIT:
            del self._window[: len(self._window) - self._WINDOW_LIMIT]

    def current_rate(self, window_seconds: float = 5.0) -> float:
        """Throughput over the last few seconds."""
        cutoff = time.monotonic() - window_seconds
        while self._window and self._window[0][0] < cutoff:
            self._window.pop(0)
        if not self._window:
            return 0.0
        span = max(1e-9, time.monotonic() - self._window[0][0])
        return sum(count for _moment, count in self._window) / span

    def to_dict(self) -> dict[str, Any]:
        return {
            "elapsed_seconds": round(self.elapsed, 2),
            "generated": self.generated,
            "delivered": self.delivered,
            "dropped": self.dropped,
            "records_per_second": round(self.current_rate(), 2),
            "mean_records_per_second": round(self.mean_rate, 2),
            "target_records_per_second": round(self.target_rate, 2),
            "expected_records": round(self.expected, 1),
            "attainment": round(self.attainment, 4),
            "by_entity": dict(self.by_entity),
        }


class EntityStream:
    """One entity, produced at one rate."""

    def __init__(
        self, entity: str, rate: Rate, *, start_index: int = 0, burst_seconds: float = 1.0
    ) -> None:
        self.entity = entity
        self.limiter = RateLimiter(rate, burst_seconds=burst_seconds)
        #: Where this entity has reached. Kept so a restarted stream continues
        #: the sequence rather than replaying it.
        self.index = start_index
        self.produced = 0

    @property
    def rate(self) -> Rate:
        return self.limiter.rate

    def retarget(self, rate: Rate) -> None:
        self.limiter.retarget(rate)

    def due(self, ceiling: int) -> range:
        """The indices owed right now, at most ``ceiling`` of them."""
        wanted = self.limiter.take(ceiling)
        start = self.index
        self.index += wanted
        self.produced += wanted
        return range(start, start + wanted)

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity": self.entity,
            "index": self.index,
            "produced": self.produced,
            **self.limiter.to_dict(),
        }


class LiveStream:
    """Generates records continuously and delivers them (section 35).

    Owns nothing it did not create: the engine, the sinks and the schema are
    passed in, so the same stream logic serves the CLI, the API and a test.
    """

    def __init__(
        self,
        compiled: CompiledProject,
        config: StreamConfig,
        *,
        engine: GenerationEngine | None = None,
        stream_id: str | None = None,
    ) -> None:
        import uuid

        from ..generation.engine import GenerationEngine as Engine

        self.compiled = compiled
        self.config = config
        self.id = stream_id or str(uuid.uuid4())
        self.stats = StreamStats()

        unknown = [name for name in config.rates if name not in compiled.entities]
        if unknown:
            known = ", ".join(compiled.entity_order)
            raise CacophonyError(
                f"cannot stream unknown entities: {', '.join(unknown)}. Known: {known}"
            )
        if not config.rates:
            raise CacophonyError("a stream needs at least one entity rate, e.g. --rate login=50/s")

        self.engine = engine or Engine(compiled, validate=False)
        # A bucket that cannot hold a batch would cap every delivery at the
        # burst ceiling, whatever `batch_size` said.
        burst = max(1.0, config.flush_seconds)
        self.streams = {
            name: EntityStream(name, rate, start_index=config.start_index, burst_seconds=burst)
            for name, rate in config.rates.items()
        }

        # Events interleave in a stream, so subjects are drawn per event rather
        # than taken from a contiguous block. Without this every record in the
        # stream belongs to subject zero.
        for name in config.rates:
            simulation = self.engine.simulations.get(name)
            if simulation is not None:
                simulation.stream_mode(cycle_seconds=config.scenario_cycle_seconds)

        self._stop = asyncio.Event()
        self._paused = asyncio.Event()
        self._paused.set()
        self.state = "queued"
        self.error: str | None = None
        #: Called with (entity, records) after each delivery, for a dashboard.
        self.on_batch: Any = None

    # -- control -------------------------------------------------------------- #

    def stop(self) -> None:
        self._stop.set()
        self._paused.set()

    def pause(self) -> None:
        self._paused.clear()
        self.state = "paused"

    def unpause(self) -> None:
        self._paused.set()
        self.state = "running"

    def retarget(self, entity: str, rate: Rate | str | float) -> Rate:
        """Change one entity's rate while the stream runs (section 94)."""
        stream = self.streams.get(entity)
        if stream is None:
            known = ", ".join(sorted(self.streams))
            raise CacophonyError(f"'{entity}' is not being streamed. Streaming: {known}")
        parsed = parse_rate(rate)
        stream.retarget(parsed)
        self.config.rates[entity] = parsed
        # Attainment is achieved over *requested*, so the denominator has to
        # move with the request. Without this a stream turned up from 200/s to
        # 800/s reports 400% attainment - the exact "measuring the wrong thing"
        # failure the number exists to prevent.
        self.stats.set_target(self._target_rate())
        return parsed

    def _target_rate(self) -> float:
        return sum(stream.rate.per_second for stream in self.streams.values())

    @property
    def running(self) -> bool:
        return self.state in ("running", "paused")

    # -- the loop -------------------------------------------------------------- #

    async def run(self) -> StreamStats:
        """Generate and deliver until told to stop."""
        sinks: list[StreamSink] = list(self.config.sinks)
        for sink in sinks:
            await sink.open()

        self.state = "running"
        self.stats = StreamStats()
        self.stats.set_target(self._target_rate())
        deadline = (
            self.stats.started_at + self.config.duration_seconds
            if self.config.duration_seconds
            else None
        )

        try:
            while not self._stop.is_set():
                await self._paused.wait()
                if self._stop.is_set():
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    break
                if self.config.max_records and self.stats.generated >= self.config.max_records:
                    break

                began = time.monotonic()
                produced = await self._tick(sinks)

                # Sleep long enough for the next tick to be a *batch*, less
                # whatever this one cost. Without the batching the loop spins,
                # each tick finds one token, and a stream at 250/s makes 250
                # deliveries a second - for an HTTP or Kafka destination that
                # is the difference between a workload generator and a denial
                # of service against your own endpoint. Without subtracting the
                # work, the period is interval *plus* generation time and the
                # stream quietly runs ten per cent slow.
                target = self._tick_interval() if produced else self._idle_wait()
                sleep = max(0.0, target - (time.monotonic() - began))
                if deadline is not None:
                    # Never sleep past the end. A stream told to run for five
                    # seconds that dozes through the last third of a second has
                    # not run for five seconds, and the shortfall shows up as a
                    # rate the operator did not ask for.
                    sleep = min(sleep, max(0.0, deadline - time.monotonic()))
                await asyncio.sleep(sleep)
            # Whatever the buckets still owe. Without this the last partial
            # batch - up to one tick's worth - is left unclaimed, and a stream
            # asked for five seconds at 280/s reports 93% attainment for no
            # reason the operator can see.
            if not self._stop.is_set():
                await self._tick(sinks)

        except asyncio.CancelledError:
            self.state = "cancelled"
            raise
        except CacophonyError as exc:
            self.state = "failed"
            self.error = str(exc)
            raise
        finally:
            for sink in sinks:
                with contextlib.suppress(Exception):
                    await sink.close()
            if self.state == "running":
                self.state = "completed"

        return self.stats

    async def _tick(self, sinks: Sequence[StreamSink]) -> int:
        """Produce and deliver whatever is owed right now."""
        produced = 0

        for stream in self.streams.values():
            # Take everything the bucket owes, not one batch of it. The batch
            # size governs how big a *delivery* is; capping what is owed by it
            # would cap throughput at batch_size per tick, and a tick is only
            # as short as generation allows - which is how a stream ends up
            # quietly running ten per cent under its target.
            ceiling = min(
                int(stream.limiter.ceiling) + self.config.batch_size,
                max(self.config.batch_size, self.config.max_in_flight),
            )
            if self.config.max_records:
                ceiling = min(ceiling, self.config.max_records - self.stats.generated)
            if ceiling <= 0:
                continue

            shaped = max(1, int(ceiling * self._shape_multiplier()))
            indices = stream.due(shaped)
            if not indices:
                continue

            records = await self.engine.generate_chunk(
                self.compiled.entity(stream.entity), list(indices)
            )
            if self.config.live_time:
                _stamp_now(records, self.compiled.entity(stream.entity))

            self.stats.note(stream.entity, len(records))
            produced += len(records)
            await self._deliver(sinks, stream.entity, records)

        return produced

    async def _deliver(
        self, sinks: Sequence[StreamSink], entity: str, records: list[GeneratedRecord]
    ) -> None:
        """Hand records to every destination, in deliveries of ``batch_size``."""
        size = max(1, self.config.batch_size)
        for start in range(0, len(records), size):
            batch = records[start : start + size]
            for sink in sinks:
                delivered = await sink.send(batch)
                self.stats.delivered += delivered
                missing = len(batch) - delivered
                if missing:
                    self.stats.dropped += missing
                    if self.config.on_error == "abort":
                        raise CacophonyError(
                            f"destination {sink.name} rejected {missing} records: "
                            f"{sink.stats.last_error}"
                        )
        if self.on_batch is not None:
            self.on_batch(entity, records)

    def _tick_interval(self) -> float:
        """How long to wait so the next tick collects a full batch.

        Bounded by ``flush_seconds`` at the slow end, so a stream at eight
        events a minute still delivers within a second or two rather than when
        a batch eventually fills.
        """
        total = sum(stream.rate.per_second for stream in self.streams.values())
        if total <= 0:
            return self.config.flush_seconds
        return max(0.001, min(self.config.flush_seconds, self.config.batch_size / total))

    def _idle_wait(self) -> float:
        """Nothing was due: sleep until the earliest limiter has a token.

        A stream at eight events a minute should not burn a core waiting.
        """
        wait = min((stream.limiter.wait_time() for stream in self.streams.values()), default=0.05)
        return max(0.001, min(wait, self.config.flush_seconds))

    def _shape_multiplier(self) -> float:
        """How busy the world should be at this moment (section 25).

        A stream that follows its project's timeline shape is quiet at three in
        the morning and busy on Tuesday afternoon, which is what a realistic
        workload looks like. Off by default: a load test usually wants the rate
        it asked for, not the rate the shape thinks is plausible.
        """
        if not self.config.follow_shape:
            return 1.0

        timeline = getattr(self.engine, "timeline", None)
        if timeline is None:
            return 1.0

        from datetime import datetime

        now = datetime.now()
        weight = timeline.shape.weight_at(now, progress=1.0)
        # Normalised against the shape's own peak so `follow_shape` changes the
        # *pattern* without changing the average rate by an arbitrary factor.
        peak = max(timeline.shape.hours) * max(timeline.shape.weekdays)
        return max(0.02, weight / peak) if peak else 1.0

    # -- description ----------------------------------------------------------- #

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "state": self.state,
            "project": self.compiled.name,
            "config": self.config.to_dict(),
            "stats": self.stats.to_dict(),
            "streams": [stream.to_dict() for stream in self.streams.values()],
            "sinks": [sink.describe() for sink in self.config.sinks],
            "error": self.error,
        }


def _stamp_now(records: Sequence[GeneratedRecord], entity: Any) -> None:
    """Move an entity's event timestamps to the wall clock.

    A streamed event happened *now*. The generated value still decides the
    shape of everything else about the record; only the moment is replaced,
    and only for fields that are actually timestamps of the event.
    """
    from datetime import datetime

    from ..core.types import DataType

    stamps = [
        compiled.name
        for compiled in entity.fields
        if compiled.spec.type in (DataType.DATETIME, DataType.DATE, DataType.TIME)
        and compiled.generator.name in ("event_time", "datetime")
    ]
    if not stamps:
        return

    for record in records:
        now = datetime.now()
        for name in stamps:
            declared = entity.spec.fields[name].type
            if declared is DataType.DATE:
                record.values[name] = now.date()
            elif declared is DataType.TIME:
                record.values[name] = now.time()
            else:
                record.values[name] = now

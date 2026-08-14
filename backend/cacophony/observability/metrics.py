"""Run metrics (design document sections 55, 58 and 86).

Section 86 asks for::

    records generated  records/sec  provider latency  provider errors
    retry rate         validation failures            queue depth

Section 55 wants the same numbers shown live. They are collected in one place
so that the terminal, the WebSocket feed and the stored run summary cannot
disagree about what happened.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

__all__ = ["EntityMetrics", "RunMetrics", "Throughput"]


@dataclass(slots=True)
class Throughput:
    """A moving-average rate, for section 55's live figures.

    A simple total-over-elapsed average is useless on a long run: it is
    dominated by the first minute and barely moves afterwards, so it cannot
    show a slowdown. This keeps a short window instead.
    """

    window_seconds: float = 5.0
    _samples: deque[tuple[float, int]] = field(default_factory=lambda: deque(maxlen=512))
    _total: int = 0
    _started: float = field(default_factory=time.perf_counter)

    def record(self, count: int, *, now: float | None = None) -> None:
        moment = now if now is not None else time.perf_counter()
        self._total += count
        self._samples.append((moment, count))
        cutoff = moment - self.window_seconds
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

    @property
    def total(self) -> int:
        return self._total

    @property
    def rate(self) -> float:
        """Recent items per second."""
        if len(self._samples) < 2:
            return self.mean_rate
        span = self._samples[-1][0] - self._samples[0][0]
        if span <= 0:
            return self.mean_rate
        return sum(count for _, count in self._samples) / span

    @property
    def mean_rate(self) -> float:
        elapsed = time.perf_counter() - self._started
        return self._total / elapsed if elapsed > 0 else 0.0

    @property
    def elapsed(self) -> float:
        return time.perf_counter() - self._started

    def eta_seconds(self, remaining: int) -> float | None:
        rate = self.rate
        return remaining / rate if rate > 0 and remaining > 0 else None


@dataclass(slots=True)
class EntityMetrics:
    """Per-entity counters, which is how section 55 displays progress."""

    entity: str
    requested: int = 0
    generated: int = 0
    written: int = 0
    rejected: int = 0
    repaired: int = 0
    field_failures: int = 0
    throughput: Throughput = field(default_factory=Throughput)

    def record(self, count: int) -> None:
        self.generated += count
        self.written += count
        self.throughput.record(count)

    @property
    def remaining(self) -> int:
        return max(0, self.requested - self.written)

    @property
    def progress(self) -> float:
        return min(1.0, self.written / self.requested) if self.requested else 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity": self.entity,
            "requested": self.requested,
            "generated": self.generated,
            "written": self.written,
            "rejected": self.rejected,
            "repaired": self.repaired,
            "field_failures": self.field_failures,
            "remaining": self.remaining,
            "progress": round(self.progress, 6),
            "records_per_second": round(self.throughput.rate, 2),
        }


@dataclass(slots=True)
class RunMetrics:
    """Everything a run knows about itself while it is happening."""

    run_id: str
    entities: dict[str, EntityMetrics] = field(default_factory=dict)
    records: Throughput = field(default_factory=Throughput)
    tokens: Throughput = field(default_factory=Throughput)
    bytes_written: int = 0

    provider_calls: int = 0
    provider_errors: int = 0
    provider_latency_ms: float = 0.0
    retries: int = 0
    validation_failures: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    queue_depth: int = 0

    def entity(self, name: str, *, requested: int = 0) -> EntityMetrics:
        metrics = self.entities.get(name)
        if metrics is None:
            metrics = EntityMetrics(entity=name, requested=requested)
            self.entities[name] = metrics
        elif requested:
            metrics.requested = requested
        return metrics

    def record_batch(self, entity: str, count: int, *, bytes_written: int = 0) -> None:
        self.entity(entity).record(count)
        self.records.record(count)
        self.bytes_written += bytes_written

    def absorb_provider_stats(self, stats: Any) -> None:
        """Fold the provider layer's counters in (design document section 58)."""
        self.provider_calls = stats.llm_calls
        self.provider_errors = stats.llm_failures
        self.provider_latency_ms = stats.mean_latency_ms
        self.retries = stats.llm_retries
        if stats.completion_tokens:
            self.tokens.record(stats.completion_tokens - int(self.tokens.total))

    @property
    def total_requested(self) -> int:
        return sum(metrics.requested for metrics in self.entities.values())

    @property
    def total_written(self) -> int:
        return sum(metrics.written for metrics in self.entities.values())

    @property
    def progress(self) -> float:
        total = self.total_requested
        return min(1.0, self.total_written / total) if total else 0.0

    @property
    def eta_seconds(self) -> float | None:
        return self.records.eta_seconds(self.total_requested - self.total_written)

    def snapshot(self) -> dict[str, Any]:
        """The payload sent to the live view and stored on completion."""
        return {
            "run_id": self.run_id,
            "progress": round(self.progress, 6),
            "records_written": self.total_written,
            "records_requested": self.total_requested,
            "records_per_second": round(self.records.rate, 2),
            "mean_records_per_second": round(self.records.mean_rate, 2),
            "tokens_per_second": round(self.tokens.rate, 2),
            "elapsed_seconds": round(self.records.elapsed, 3),
            "eta_seconds": round(self.eta_seconds, 1) if self.eta_seconds else None,
            "bytes_written": self.bytes_written,
            "provider_calls": self.provider_calls,
            "provider_errors": self.provider_errors,
            "provider_latency_ms": round(self.provider_latency_ms, 2),
            "retries": self.retries,
            "validation_failures": self.validation_failures,
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "queue_depth": self.queue_depth,
            "entities": {name: metrics.to_dict() for name, metrics in self.entities.items()},
        }

    def quality(self) -> dict[str, float]:
        """Section 58's project score, as far as this phase can compute it."""
        written = self.total_written
        return {
            "constraint_validity": round(
                1.0 - (self.validation_failures / written) if written else 1.0, 6
            ),
            "provider_success": round(
                1.0 - (self.provider_errors / self.provider_calls) if self.provider_calls else 1.0,
                6,
            ),
            "retry_rate": round(
                self.retries / self.provider_calls if self.provider_calls else 0.0, 6
            ),
            "cache_hit_rate": round(
                self.cache_hits / (self.cache_hits + self.cache_misses)
                if (self.cache_hits + self.cache_misses)
                else 0.0,
                6,
            ),
        }

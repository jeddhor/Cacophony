"""Shards and leases (design document section 95).

The reason distributed generation is a small feature rather than a rewrite is
section 75. A record's seed is derived by hashing its *position*, so record
4,823,913 is the same record whoever produces it, in whatever order, on
whatever machine. A unit of distributable work is therefore just an index
range, and there is no RNG state to partition, checkpoint or reconcile.

    shard = (entity, offset, count)

That is the whole protocol's payload. Everything else here is about the part
that is genuinely hard in any distributed system: deciding when a worker that
has stopped answering has actually failed, and making sure the work it was
holding is done exactly once.

**Leases, not assignments.** A worker is granted a shard until a deadline. It
renews while it works. If the lease expires the controller may hand the shard
to somebody else - and the original worker, if it comes back, is told its lease
is stale and drops what it was doing.

**Exactly once, by construction.** A re-leased shard is regenerated from
scratch rather than resumed, and because the output of a shard is a pure
function of its index range, the second attempt produces byte-identical
records. So the failure mode a lease protocol usually has to agonise over -
partial work from a dead worker - costs nothing here: it is discarded and
recomputed, and no consumer can tell.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .capabilities import Capabilities

__all__ = ["Lease", "LeaseState", "Shard"]


class LeaseState(StrEnum):
    """Where a shard is."""

    PENDING = "pending"
    LEASED = "leased"
    COMPLETED = "completed"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self in (LeaseState.COMPLETED, LeaseState.FAILED)


@dataclass(slots=True)
class Shard:
    """A contiguous range of one entity's records."""

    entity: str
    offset: int
    count: int
    #: What a worker must be able to do to take this.
    requires: Capabilities = field(default_factory=Capabilities)
    id: str = field(default_factory=lambda: str(uuid.uuid4()))

    @property
    def end(self) -> int:
        return self.offset + self.count

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "entity": self.entity,
            "offset": self.offset,
            "count": self.count,
            "requires": self.requires.to_list(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Shard:
        return cls(
            entity=str(data["entity"]),
            offset=int(data["offset"]),
            count=int(data["count"]),
            requires=Capabilities.of(data.get("requires") or []),
            id=str(data.get("id") or uuid.uuid4()),
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Shard({self.entity}[{self.offset}:{self.end}])"


@dataclass(slots=True)
class Lease:
    """A shard, and who is holding it.

    ``generation`` is what makes a stale worker harmless. It increments every
    time the shard is leased, so a worker returning results for generation 2
    when the shard is now on generation 3 is told to drop them - it lost the
    lease while it was away, and somebody else has already redone the work.
    """

    shard: Shard
    state: LeaseState = LeaseState.PENDING
    worker_id: str | None = None
    generation: int = 0
    #: Monotonic deadline. A worker renews before this; the controller reclaims
    #: after it.
    expires_at: float = 0.0
    attempts: int = 0
    records: int = 0
    error: str | None = None
    granted_at: float = 0.0
    finished_at: float = 0.0

    #: Seconds a lease is granted for. Long enough that a slow shard renews
    #: rather than expires, short enough that a dead worker is noticed.
    ttl_seconds: float = 30.0

    # -- transitions ---------------------------------------------------------- #

    def grant(self, worker_id: str, *, ttl: float | None = None) -> Lease:
        self.state = LeaseState.LEASED
        self.worker_id = worker_id
        self.generation += 1
        self.attempts += 1
        self.ttl_seconds = ttl or self.ttl_seconds
        self.granted_at = time.monotonic()
        self.expires_at = self.granted_at + self.ttl_seconds
        self.error = None
        return self

    def renew(self, *, ttl: float | None = None) -> None:
        self.expires_at = time.monotonic() + (ttl or self.ttl_seconds)

    def complete(self, records: int) -> None:
        self.state = LeaseState.COMPLETED
        self.records = records
        self.finished_at = time.monotonic()

    def fail(self, reason: str) -> None:
        self.state = LeaseState.FAILED
        self.error = reason
        self.finished_at = time.monotonic()

    def release(self) -> None:
        """Return the shard to the pool, keeping the attempt count."""
        self.state = LeaseState.PENDING
        self.worker_id = None
        self.expires_at = 0.0

    # -- questions ------------------------------------------------------------ #

    @property
    def is_expired(self) -> bool:
        return self.state is LeaseState.LEASED and time.monotonic() > self.expires_at

    @property
    def seconds_remaining(self) -> float:
        return max(0.0, self.expires_at - time.monotonic()) if self.expires_at else 0.0

    @property
    def duration(self) -> float:
        if not self.granted_at:
            return 0.0
        end = self.finished_at or time.monotonic()
        return end - self.granted_at

    def held_by(self, worker_id: str, generation: int) -> bool:
        """Whether this worker still owns what it thinks it owns."""
        return self.worker_id == worker_id and self.generation == generation

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.shard.to_dict(),
            "state": self.state.value,
            "worker_id": self.worker_id,
            "generation": self.generation,
            "attempts": self.attempts,
            "records": self.records,
            "seconds_remaining": round(self.seconds_remaining, 2),
            "duration_seconds": round(self.duration, 3),
            "error": self.error,
        }

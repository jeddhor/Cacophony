"""The Cacophony Controller (design document sections 84, 95).

    Cacophony Controller
          ├── CPU Worker Node
          ├── LLM GPU Node
          ├── InvokeAI Node
          ├── TTS Node
          └── Export Node

Splits a run into shards, hands them out as leases, notices when a worker has
stopped answering, and puts the work back. It holds no records: a shard's
output goes to shared storage or comes back over the wire, and the controller
only ever knows *how many*.

Three things make this smaller than a distributed system usually is.

**Sharding is arithmetic.** A shard is an index range, because a record's seed
comes from its position (section 75). There is no RNG state to split, no
ordering to preserve, and no merge step - a shard's output is byte-identical
whichever worker produced it.

**Retry is regeneration.** A worker that dies mid-shard leaves nothing worth
recovering, so the shard is simply redone. The usual agonising about partial
work does not apply, because the second attempt produces exactly what the first
one would have.

**Routing is a set comparison.** A shard needs some capabilities; a worker
advertises some; the scheduler hands over the first shard the worker can
actually do. That is the whole of section 84's "the scheduler routes jobs
appropriately".

What is genuinely hard here is the same thing that is hard everywhere: deciding
that a silent worker is a dead worker. The answer is leases with deadlines and
a generation counter, so a worker that comes back from the dead is told its
lease is stale rather than allowed to double-write.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..core.errors import CacophonyError
from .capabilities import Capabilities, WorkerProfile, capabilities_for
from .leases import Lease, LeaseState, Shard

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from ..schema.plan import CompiledProject

__all__ = ["Controller", "ControllerStats", "WorkerRecord"]

#: How long after its last word a worker is presumed gone. Generous relative to
#: the lease TTL: a worker busy on a slow shard is not a worker in trouble.
DEFAULT_WORKER_TIMEOUT = 90.0


@dataclass(slots=True)
class WorkerRecord:
    """A worker the controller has heard from."""

    profile: WorkerProfile
    first_seen: float = field(default_factory=time.monotonic)
    last_seen: float = field(default_factory=time.monotonic)
    shards_completed: int = 0
    records_produced: int = 0
    failures: int = 0
    #: Leases this worker is holding right now.
    holding: set[str] = field(default_factory=set)

    @property
    def idle_seconds(self) -> float:
        return time.monotonic() - self.last_seen

    def is_alive(self, timeout: float = DEFAULT_WORKER_TIMEOUT) -> bool:
        return self.idle_seconds <= timeout

    @property
    def throughput(self) -> float:
        elapsed = max(1e-9, self.last_seen - self.first_seen)
        return self.records_produced / elapsed

    def to_dict(self, timeout: float = DEFAULT_WORKER_TIMEOUT) -> dict[str, Any]:
        return {
            **self.profile.to_dict(),
            "alive": self.is_alive(timeout),
            "idle_seconds": round(self.idle_seconds, 2),
            "holding": sorted(self.holding),
            "shards_completed": self.shards_completed,
            "records_produced": self.records_produced,
            "failures": self.failures,
            "records_per_second": round(self.throughput, 2),
        }


@dataclass(slots=True)
class ControllerStats:
    """What the run as a whole is doing."""

    started_at: float = field(default_factory=time.monotonic)
    records: int = 0
    shards_completed: int = 0
    shards_failed: int = 0
    leases_expired: int = 0
    #: Shards handed out a second time because the first holder went quiet.
    reassigned: int = 0

    @property
    def elapsed(self) -> float:
        return max(1e-9, time.monotonic() - self.started_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "elapsed_seconds": round(self.elapsed, 2),
            "records": self.records,
            "records_per_second": round(self.records / self.elapsed, 2),
            "shards_completed": self.shards_completed,
            "shards_failed": self.shards_failed,
            "leases_expired": self.leases_expired,
            "shards_reassigned": self.reassigned,
        }


class Controller:
    """Hands out shards and keeps track of who has what.

    Thread-safe and transport-free. The HTTP routes in
    :mod:`cacophony.api.app` are a thin shell over these methods, which is what
    lets the whole protocol be tested without a socket.
    """

    def __init__(
        self,
        compiled: CompiledProject,
        *,
        shard_size: int = 50_000,
        lease_seconds: float = 30.0,
        worker_timeout: float = DEFAULT_WORKER_TIMEOUT,
        max_attempts: int = 3,
        counts: dict[str, int] | None = None,
    ) -> None:
        self.compiled = compiled
        self.shard_size = max(1, shard_size)
        self.lease_seconds = max(1.0, lease_seconds)
        self.worker_timeout = max(self.lease_seconds, worker_timeout)
        #: A shard that has failed this many times is not tried again. Section
        #: 66's rule, applied to work rather than to model calls: never permit
        #: an infinite retry loop.
        self.max_attempts = max(1, max_attempts)

        self.stats = ControllerStats()
        self.workers: dict[str, WorkerRecord] = {}
        self.leases: dict[str, Lease] = {}
        self._order: list[str] = []
        self._lock = threading.RLock()

        self.schema_hash = _schema_hash(compiled)
        self._plan(counts or {})

    # -- planning -------------------------------------------------------------- #

    def _plan(self, counts: dict[str, int]) -> None:
        """Cut every entity into shards, in dependency order."""
        for name in self.compiled.entity_order:
            entity = self.compiled.entity(name)
            total = counts.get(name, entity.count)
            if total <= 0:
                continue

            requires = capabilities_for(entity)
            for offset in range(0, total, self.shard_size):
                shard = Shard(
                    entity=name,
                    offset=offset,
                    count=min(self.shard_size, total - offset),
                    requires=requires,
                )
                self.leases[shard.id] = Lease(shard=shard, ttl_seconds=self.lease_seconds)
                self._order.append(shard.id)

    @property
    def total_records(self) -> int:
        return sum(lease.shard.count for lease in self.leases.values())

    # -- workers ---------------------------------------------------------------- #

    def register(self, profile: WorkerProfile) -> WorkerRecord:
        """Record a worker, or note that a known one is still alive.

        A worker holding a different schema is refused. Two nodes generating
        from two schemas produce a dataset that is neither, and the failure
        would surface as data nobody could explain rather than as an error.
        """
        if profile.schema_hash and profile.schema_hash != self.schema_hash:
            raise CacophonyError(
                f"worker '{profile.id}' has a different schema "
                f"({profile.schema_hash[:12]} vs {self.schema_hash[:12]}). Its records would "
                "not match this run's."
            )

        with self._lock:
            record = self.workers.get(profile.id)
            if record is None:
                record = WorkerRecord(profile=profile)
                self.workers[profile.id] = record
            else:
                record.profile = profile
                record.last_seen = time.monotonic()
            return record

    def heartbeat(self, worker_id: str) -> WorkerRecord | None:
        with self._lock:
            record = self.workers.get(worker_id)
            if record is not None:
                record.last_seen = time.monotonic()
            return record

    def alive_workers(self) -> list[WorkerRecord]:
        with self._lock:
            return [
                record for record in self.workers.values() if record.is_alive(self.worker_timeout)
            ]

    # -- leasing ---------------------------------------------------------------- #

    def acquire(self, worker_id: str, *, count: int = 1) -> list[Lease]:
        """Grant a worker up to ``count`` shards it is able to run."""
        with self._lock:
            record = self.workers.get(worker_id)
            if record is None:
                raise CacophonyError(f"worker '{worker_id}' has not registered")
            record.last_seen = time.monotonic()

            self._reclaim_locked()
            granted: list[Lease] = []
            capabilities = record.profile.capabilities

            for shard_id in self._order:
                if len(granted) >= count:
                    break
                lease = self.leases[shard_id]
                if lease.state is not LeaseState.PENDING:
                    continue
                if not capabilities.satisfies(lease.shard.requires):
                    continue
                if not self._dependencies_met_locked(lease.shard.entity):
                    continue

                lease.grant(worker_id, ttl=self.lease_seconds)
                record.holding.add(shard_id)
                granted.append(lease)
            return granted

    def _dependencies_met_locked(self, entity: str) -> bool:
        """Whether everything this entity references has been produced.

        A login event's reference resolves by deriving the employee on demand
        rather than by reading a file, so this is not about data being
        *available*. It is about the run being reportable in the order the
        schema declares, and about an entity whose subject population is still
        being decided not being shipped out early.
        """
        compiled = self.compiled.entity(entity)
        for other in compiled.depends_on:
            if any(
                lease.state is not LeaseState.COMPLETED
                for lease in self.leases.values()
                if lease.shard.entity == other
            ):
                return False
        return True

    def _holder_locked(self, worker_id: str, shard_id: str, generation: int) -> Lease | None:
        """The lease this worker still legitimately holds, or nothing.

        Terminal leases are refused as well as reassigned ones. The network
        will lose a reply eventually, and a worker that resends ``complete``
        must not have its records counted twice - a shard is done once,
        whatever the wire did with the acknowledgement.
        """
        lease = self.leases.get(shard_id)
        if lease is None or lease.state.is_terminal:
            return None
        return lease if lease.held_by(worker_id, generation) else None

    def renew(self, worker_id: str, shard_id: str, generation: int) -> bool:
        """Extend a lease the worker still legitimately holds."""
        with self._lock:
            lease = self._holder_locked(worker_id, shard_id, generation)
            if lease is None:
                return False
            lease.renew(ttl=self.lease_seconds)
            record = self.workers.get(worker_id)
            if record is not None:
                record.last_seen = time.monotonic()
            return True

    def complete(self, worker_id: str, shard_id: str, generation: int, records: int) -> bool:
        """Accept a finished shard, or reject it as stale.

        Returning ``False`` means the worker lost the lease while it was
        working and somebody else has already done the shard - or that this is
        a resend of an acknowledgement that went missing. Either way the
        results are discarded, which costs nothing: they were identical.
        """
        with self._lock:
            lease = self._holder_locked(worker_id, shard_id, generation)
            if lease is None:
                return False

            lease.complete(records)
            self.stats.records += records
            self.stats.shards_completed += 1

            record = self.workers.get(worker_id)
            if record is not None:
                record.last_seen = time.monotonic()
                record.holding.discard(shard_id)
                record.shards_completed += 1
                record.records_produced += records
            return True

    def report_failure(self, worker_id: str, shard_id: str, generation: int, reason: str) -> bool:
        """A worker saying it could not do a shard.

        Better than silence: the shard goes back immediately rather than after
        a lease timeout, and the reason is kept so a run that fails everywhere
        says why rather than just stopping.
        """
        with self._lock:
            lease = self._holder_locked(worker_id, shard_id, generation)
            if lease is None:
                return False

            record = self.workers.get(worker_id)
            if record is not None:
                record.holding.discard(shard_id)
                record.failures += 1
                record.last_seen = time.monotonic()

            if lease.attempts >= self.max_attempts:
                lease.fail(reason)
                self.stats.shards_failed += 1
            else:
                lease.error = reason
                lease.release()
            return True

    def reclaim(self) -> list[Lease]:
        """Take back every lease whose holder has gone quiet."""
        with self._lock:
            return self._reclaim_locked()

    def _reclaim_locked(self) -> list[Lease]:
        reclaimed: list[Lease] = []
        for lease in self.leases.values():
            if not lease.is_expired:
                continue

            self.stats.leases_expired += 1
            holder = self.workers.get(lease.worker_id or "")
            if holder is not None:
                holder.holding.discard(lease.shard.id)

            if lease.attempts >= self.max_attempts:
                lease.fail(f"no worker completed this shard in {lease.attempts} attempts")
                self.stats.shards_failed += 1
            else:
                lease.release()
                self.stats.reassigned += 1
            reclaimed.append(lease)
        return reclaimed

    # -- progress ---------------------------------------------------------------- #

    @property
    def is_finished(self) -> bool:
        with self._lock:
            return all(lease.state.is_terminal for lease in self.leases.values())

    @property
    def is_stalled(self) -> bool:
        """Work remains, but nothing alive can do it.

        Reported rather than waited on: a controller that sits forever holding
        image shards nobody can run is a controller that looks like it is
        working.
        """
        with self._lock:
            pending = [
                lease
                for lease in self.leases.values()
                if lease.state in (LeaseState.PENDING, LeaseState.LEASED)
            ]
            if not pending:
                return False
            alive = [
                record for record in self.workers.values() if record.is_alive(self.worker_timeout)
            ]
            if not alive:
                return True
            return not any(
                record.profile.capabilities.satisfies(lease.shard.requires)
                for lease in pending
                for record in alive
            )

    @property
    def progress(self) -> float:
        total = self.total_records
        return self.stats.records / total if total else 1.0

    def unmet_requirements(self) -> set[str]:
        """Capabilities the pending work needs and no live worker has."""
        with self._lock:
            alive = Capabilities(frozenset())
            for record in self.workers.values():
                if record.is_alive(self.worker_timeout):
                    alive = alive | record.profile.capabilities

            missing: set[str] = set()
            for lease in self.leases.values():
                if lease.state in (LeaseState.PENDING, LeaseState.LEASED):
                    missing |= alive.missing_for(lease.shard.requires)
            return missing

    def describe(self) -> dict[str, Any]:
        with self._lock:
            states: dict[str, int] = {}
            for lease in self.leases.values():
                states[lease.state.value] = states.get(lease.state.value, 0) + 1
            return {
                "project": self.compiled.name,
                "schema_hash": self.schema_hash,
                "shards": len(self.leases),
                "shard_size": self.shard_size,
                "total_records": self.total_records,
                "progress": round(self.progress, 6),
                "finished": self.is_finished,
                "stalled": self.is_stalled,
                "unmet_capabilities": sorted(self.unmet_requirements()),
                "states": states,
                "stats": self.stats.to_dict(),
                "workers": [
                    record.to_dict(self.worker_timeout) for record in self.workers.values()
                ],
            }

    def pending(self) -> list[Lease]:
        with self._lock:
            return [lease for lease in self.leases.values() if lease.state is LeaseState.PENDING]

    def failures(self) -> list[Lease]:
        with self._lock:
            return [lease for lease in self.leases.values() if lease.state is LeaseState.FAILED]


def _schema_hash(compiled: CompiledProject) -> str:
    """The identity of a schema, for checking that workers agree about it.

    The same hash a world uses (section 16): what decides the records, and
    nothing that does not.
    """
    from ..simulation.world import schema_hash

    return schema_hash(compiled)


def plan_shards(
    compiled: CompiledProject, *, shard_size: int = 50_000, counts: dict[str, int] | None = None
) -> Sequence[Shard]:
    """The shards a run would be cut into, without a controller to hold them."""
    controller = Controller(compiled, shard_size=shard_size, counts=counts)
    return [lease.shard for lease in controller.leases.values()]

"""A worker node (design document sections 84, 95).

A worker holds the same schema as the controller, announces what it can do,
and then loops: lease a shard, generate its index range, write it, report the
count. It never asks what the other workers are doing, because it does not
need to know - a shard's records are a pure function of its index range
(section 75), so there is nothing to coordinate beyond who is doing which
range.

    while there is work I can do:
        lease  -> generate -> write -> complete

Three things the loop has to get right.

**Renew while working.** A shard of fifty thousand model-written records takes
longer than any sane lease TTL, so the worker renews between batches. A renewal
that is refused means the controller gave the shard to somebody else; the
worker abandons what it has written rather than finishing it, because a second
worker is already producing the same bytes and two copies would double the
dataset.

**Write to a shard-private file.** Workers never append to the same file.
Each shard writes ``entity.part<offset>.jsonl``, named after its offset so the
parts sort into the order a single machine would have written them
(:mod:`cacophony.distributed.assembly` puts them back together).

**Fail loudly, once.** A shard that raises is reported, not retried in place.
The controller decides whether to try it elsewhere - a shard failing because
this node has no GPU should move, and a shard failing because the schema is
wrong should stop the run rather than travel around the cluster failing
everywhere.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import __version__
from ..core.errors import CacophonyError
from ..outputs import OUTPUT_FORMATS, create_writer
from .capabilities import Capabilities, WorkerProfile, describe_host
from .leases import Shard

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..generation.engine import GenerationEngine
    from ..schema.plan import CompiledProject
    from .transport import ControllerTransport

__all__ = ["ShardResult", "Worker", "WorkerStats"]

#: Renew a lease once this fraction of its life has passed. Half leaves room
#: for one lost renewal before the controller gives up on the worker.
RENEW_AT = 0.5

#: Formats whose value is that a whole dataset is in one place. Sharding them
#: would produce a directory of databases, which is not what anybody asking for
#: a database wants.
RELATIONAL_FORMATS = frozenset({"sqlite", "sql"})


@dataclass(slots=True)
class ShardResult:
    """What one shard produced."""

    shard: Shard
    records: int = 0
    path: str | None = None
    seconds: float = 0.0
    error: str | None = None
    #: True when the controller took the shard back mid-flight.
    abandoned: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None and not self.abandoned

    def to_dict(self) -> dict[str, Any]:
        return {
            "shard": self.shard.to_dict(),
            "records": self.records,
            "path": self.path,
            "seconds": round(self.seconds, 3),
            "error": self.error,
            "abandoned": self.abandoned,
        }


@dataclass(slots=True)
class WorkerStats:
    """A worker's own account of its shift."""

    shards: int = 0
    records: int = 0
    failures: int = 0
    abandoned: int = 0
    seconds: float = 0.0
    files: list[str] = field(default_factory=list)

    @property
    def rate(self) -> float:
        return self.records / self.seconds if self.seconds else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "shards": self.shards,
            "records": self.records,
            "failures": self.failures,
            "abandoned": self.abandoned,
            "seconds": round(self.seconds, 3),
            "records_per_second": round(self.rate, 2),
            "files": list(self.files),
        }


class Worker:
    """One node's share of a distributed run."""

    def __init__(
        self,
        compiled: CompiledProject,
        transport: ControllerTransport,
        *,
        output_dir: str | Path,
        output_format: str = "jsonl",
        capabilities: Capabilities | None = None,
        worker_id: str | None = None,
        concurrency: int = 1,
        batch_size: int = 1_000,
        poll_seconds: float = 1.0,
        idle_timeout: float = 30.0,
        counts: dict[str, int] | None = None,
        assets: Any | None = None,
        engine_options: dict[str, Any] | None = None,
    ) -> None:
        if output_format.lower() not in OUTPUT_FORMATS:
            known = ", ".join(sorted(OUTPUT_FORMATS))
            raise CacophonyError(f"Unknown output format '{output_format}'. Available: {known}")
        if output_format.lower() in RELATIONAL_FORMATS:
            # A relational output split across shards is not a relational
            # output: the point of a database is that its foreign keys resolve,
            # and they cannot if each shard is a separate file. Generate to
            # parts and load them, or use ``generate`` on one machine.
            raise CacophonyError(
                f"'{output_format}' cannot be produced by a distributed run - each shard would "
                "be a separate database and the foreign keys would not resolve. Generate "
                "'jsonl' or 'parquet' parts and load them, or run 'cacophony generate'."
            )

        self.compiled = compiled
        self.transport = transport
        self.output_dir = Path(output_dir)
        self.output_format = output_format.lower()
        self.batch_size = max(1, batch_size)
        self.poll_seconds = max(0.0, poll_seconds)
        #: Stop after this long with nothing to do. A worker that waits forever
        #: is indistinguishable from a hung one.
        self.idle_timeout = idle_timeout
        self.counts = dict(counts or {})
        self.assets = assets
        self.engine_options = dict(engine_options or {})

        self.id = worker_id or f"worker-{uuid.uuid4().hex[:8]}"
        self.capabilities = capabilities or self._detect_capabilities()
        self.concurrency = max(1, concurrency)
        self.stats = WorkerStats()
        self.results: list[ShardResult] = []
        self._engine: GenerationEngine | None = None
        self._stop = False

    # -- identity ------------------------------------------------------------ #

    def _detect_capabilities(self) -> Capabilities:
        """What this node can actually do, from its configured providers.

        Advertised rather than declared, for the same reason a shard's
        requirements are read off its compiled generators: a worker that claims
        an image capability it has no provider for takes shards it will then
        fail, and the run discovers the lie one timeout at a time.
        """
        names = {"deterministic", "document"}
        for provider in self.compiled.spec.providers.values():
            if provider.type in ("language_model", "image", "speech"):
                names.add(provider.type)
        return Capabilities.of(names)

    @property
    def profile(self) -> WorkerProfile:
        from ..simulation.world import schema_hash

        return WorkerProfile(
            id=self.id,
            capabilities=self.capabilities,
            concurrency=self.concurrency,
            host=describe_host(),
            schema_hash=schema_hash(self.compiled),
            version=__version__,
        )

    @property
    def engine(self) -> GenerationEngine:
        """One engine for the worker's whole shift.

        Rebuilding it per shard would recompile prompts, rebuild the timeline
        and re-derive the simulation for every fifty thousand records. None of
        that depends on which shard is in hand.
        """
        if self._engine is None:
            from ..generation.engine import GenerationEngine

            self._engine = GenerationEngine(
                self.compiled,
                counts=self.counts,
                assets=self.assets,
                **self.engine_options,
            )
        return self._engine

    def stop(self) -> None:
        """Ask the loop to finish the shard in hand and come home."""
        self._stop = True

    # -- the loop ------------------------------------------------------------- #

    async def run(self, *, max_shards: int | None = None) -> WorkerStats:
        """Work until there is nothing left to do."""
        await self.transport.register(self.profile)
        started = time.monotonic()
        idle_since: float | None = None

        while not self._stop:
            if max_shards is not None and self.stats.shards >= max_shards:
                break

            granted = await self.transport.lease(self.id, count=self.concurrency)
            if not granted:
                status = await self.transport.status()
                if status.get("finished"):
                    break
                idle_since = idle_since or time.monotonic()
                if time.monotonic() - idle_since >= self.idle_timeout:
                    break
                if self.poll_seconds:
                    await asyncio.sleep(self.poll_seconds)
                continue

            idle_since = None
            # Shards run one at a time even when several are leased. The engine
            # already batches within a shard, and two shards in flight on one
            # node would halve the batch each model call could cover
            # (section 11) for no gain.
            for payload in granted:
                await self._run_shard(payload)

        self.stats.seconds = time.monotonic() - started
        return self.stats

    async def _run_shard(self, payload: dict[str, Any]) -> ShardResult:
        shard = Shard.from_dict(payload)
        generation = int(payload.get("generation", 0))
        ttl = float(payload.get("seconds_remaining") or 30.0)
        started = time.monotonic()
        result = ShardResult(shard=shard)

        try:
            records, path = await self._generate(shard, generation, ttl)
        except _LeaseLostError:
            result.abandoned = True
            result.seconds = time.monotonic() - started
            self.stats.abandoned += 1
            self.results.append(result)
            return result
        except Exception as exc:
            # Reported to the controller rather than swallowed: it decides
            # whether this shard should be tried somewhere else.
            result.error = str(exc)
            result.seconds = time.monotonic() - started
            self.stats.failures += 1
            self.results.append(result)
            await self.transport.fail(self.id, shard.id, generation, str(exc))
            return result

        result.records = records
        result.path = str(path)
        result.seconds = time.monotonic() - started

        accepted = await self.transport.complete(
            self.id, shard.id, generation, records, path=str(path)
        )
        if not accepted:
            # Somebody else already did this shard. Their file holds the same
            # bytes; ours would be a duplicate of it.
            _discard(path)
            result.abandoned = True
            self.stats.abandoned += 1
        else:
            self.stats.shards += 1
            self.stats.records += records
            self.stats.files.append(str(path))

        self.results.append(result)
        return result

    async def _generate(self, shard: Shard, generation: int, ttl: float) -> tuple[int, Path]:
        """Produce one shard's records into its own file."""
        entity = self.compiled.entity(shard.entity)
        path = self.shard_path(shard)
        path.parent.mkdir(parents=True, exist_ok=True)

        writer = create_writer(
            self.output_format,
            path,
            columns=entity.spec.field_names(),
            provenance=self.compiled.spec.project.provenance,
            entity=entity,
            entities=self.compiled.entities,
        )

        deadline = time.monotonic() + ttl * RENEW_AT
        written = 0
        await writer.open()
        try:
            async for chunk in self.engine.stream(
                shard.entity,
                count=shard.count,
                offset=shard.offset,
                batch_size=self.batch_size,
            ):
                if chunk.records:
                    await writer.write_batch(chunk.records)
                written += len(chunk.records)

                if time.monotonic() >= deadline:
                    if not await self.transport.renew(self.id, shard.id, generation):
                        raise _LeaseLostError(shard.id)
                    deadline = time.monotonic() + ttl * RENEW_AT
        except BaseException:
            # A shard that did not finish leaves nothing worth keeping. Half a
            # shard is not half a dataset; it is a file the assembler would
            # happily concatenate into a hole.
            await writer.close()
            _discard(path)
            raise
        else:
            await writer.close()

        return written, path

    def shard_path(self, shard: Shard) -> Path:
        """Where a shard's records go.

        Named after the offset, zero-padded, so an ordinary directory listing
        is already in dataset order and the assembler's numeric sort is a
        formality rather than a rescue.
        """
        writer_class = OUTPUT_FORMATS[self.output_format]
        extension = writer_class.extension
        return self.output_dir / f"{shard.entity}.part{shard.offset:09d}{extension}"


class _LeaseLostError(CacophonyError):
    """Raised when the controller has taken a shard back mid-flight."""

    def __init__(self, shard_id: str) -> None:
        super().__init__(f"lease on shard {shard_id} was lost; another worker has it")


def _discard(path: Path) -> None:
    """Remove a shard file nobody is going to use."""
    # A file we cannot remove is not fatal: the shard is being redone, and
    # whoever redoes it writes to this same path.
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)

"""Running a controller and several workers together (design document section 95).

Two shapes of distributed run, one code path.

**On one machine.** ``cacophony cluster project.yaml --workers 4`` starts a
controller and four workers in this process over :class:`LocalTransport`. Useful on its own -
four cores instead of one - and useful as the thing that proves the protocol,
because it exercises leasing, renewal, reassignment and assembly without a
network to be uncertain about.

**Across machines.** ``cacophony controller`` serves the same controller over
HTTP and ``cacophony worker`` joins it. Nothing below changes; the transport
does.

The workers here are asyncio tasks rather than processes. Generation is
overwhelmingly a CPU-bound loop, so four tasks do not give four cores' worth of
deterministic fields - what they do give is real overlap whenever a worker is
waiting on a model, a disk or a socket, which is the case this phase exists
for. A run of purely deterministic fields on one machine is already faster
without them, and the CLI says so rather than implying a speed-up it cannot
deliver.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .controller import Controller
from .transport import LocalTransport
from .worker import Worker

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable

    from ..schema.plan import CompiledProject

__all__ = ["ClusterResult", "run_cluster"]


@dataclass(slots=True)
class ClusterResult:
    """What a local cluster run produced."""

    records: int = 0
    shards: int = 0
    seconds: float = 0.0
    workers: list[dict[str, Any]] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)
    status: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.failures

    @property
    def rate(self) -> float:
        return self.records / self.seconds if self.seconds else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "records": self.records,
            "shards": self.shards,
            "seconds": round(self.seconds, 3),
            "records_per_second": round(self.rate, 2),
            "workers": self.workers,
            "files": self.files,
            "failures": self.failures,
            "status": self.status,
        }


async def run_cluster(
    compiled: CompiledProject,
    *,
    output_dir: str | Path,
    workers: int = 4,
    output_format: str = "jsonl",
    shard_size: int = 50_000,
    batch_size: int = 1_000,
    counts: dict[str, int] | None = None,
    assets: Any | None = None,
    engine_options: dict[str, Any] | None = None,
    lease_seconds: float = 30.0,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    progress_interval: float = 0.25,
) -> ClusterResult:
    """Generate a whole project across ``workers`` in-process workers."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    controller = Controller(
        compiled,
        shard_size=shard_size,
        lease_seconds=lease_seconds,
        counts=counts,
    )
    transport = LocalTransport(controller)

    pool = [
        Worker(
            compiled,
            transport,
            output_dir=output_dir,
            output_format=output_format,
            worker_id=f"local-{index + 1}",
            batch_size=batch_size,
            counts=counts,
            assets=assets,
            engine_options=engine_options,
            # In-process workers do not poll: when the controller has nothing
            # left, it has nothing left, and there is no network partition that
            # could make that answer wrong a second later.
            poll_seconds=0.0,
            idle_timeout=0.0,
        )
        for index in range(max(1, workers))
    ]

    started = time.monotonic()
    reporter = (
        asyncio.create_task(_report(controller, on_progress, progress_interval))
        if on_progress
        else None
    )
    try:
        await asyncio.gather(*(worker.run() for worker in pool))
    finally:
        if reporter is not None:
            reporter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reporter

    result = ClusterResult(
        records=controller.stats.records,
        shards=controller.stats.shards_completed,
        seconds=time.monotonic() - started,
        workers=[worker.stats.to_dict() | {"id": worker.id} for worker in pool],
        files=sorted(path for worker in pool for path in worker.stats.files),
        failures=[lease.to_dict() for lease in controller.failures()],
        status=controller.describe(),
    )
    if on_progress:
        on_progress(result.status)
    return result


async def _report(
    controller: Controller,
    on_progress: Callable[[dict[str, Any]], None],
    interval: float,
) -> None:
    """Publish controller status while the workers work."""
    while True:
        await asyncio.sleep(interval)
        controller.reclaim()
        on_progress(controller.describe())

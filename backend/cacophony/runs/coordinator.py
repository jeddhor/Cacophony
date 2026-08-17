"""The generation Conductor (design document sections 27–32, 55, 56, 65, 108).

Section 108 offers "Conductor" as the branded name for the generation
planner/scheduler, and this is it: the thing that turns a compiled project into
jobs, runs them within the configured limits, checkpoints as it goes, and can
be paused, resumed or cancelled while it does.

What a job is
-------------
One job per entity per output. Section 32's checkpoint example is per entity -
``{"entity": "security_events", "completed": 6830000, "requested": 10000000}`` -
and that is the right granularity because a single number is a complete
checkpoint here. Records are addressed by index and their seeds are derived by
hashing that index (section 75), so there is no RNG stream position to save and
restore. "I finished 6,830,000" is genuinely all a resume needs to know.

Concurrency
-----------
Entities that do not depend on one another run concurrently, up to
``max_workers`` (section 30). Language-model requests are limited separately by
the provider that owns them, so a run with four entity workers against a
provider allowing two concurrent requests does the right thing without either
limit knowing about the other.

Resuming
--------
A job records how many records it has written. Formats that can be appended to
reopen the same file and continue; formats with a footer - JSON arrays, Parquet
- start a new part file instead, because the alternative is either rewriting
what is already on disk or producing a corrupt file. Both are worse.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..core.errors import CacophonyError, GenerationError
from ..core.interfaces import OutputWriter
from ..generation.engine import GenerationEngine
from ..generation.runtime import GenerationRuntime
from ..observability.logging import RunLogger
from ..observability.metrics import RunMetrics
from ..outputs import OUTPUT_FORMATS, align_to_records, create_writer, output_path_for
from ..providers.cache import GenerationCache
from .config import RunConfig
from .events import EventBus, EventKind
from .state import JobState, JobType, RunState

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..schema.plan import CompiledEntity, CompiledProject
    from ..store.repository import Repository

__all__ = ["Conductor", "PlannedJob", "RunHandle", "RunOutcome"]


class RunAborted(CacophonyError):
    """Raised internally to unwind a cancelled run."""


@dataclass(slots=True)
class PlannedJob:
    """One unit of work, before it has been persisted."""

    sequence: int
    type: str
    entity: str
    offset: int
    requested: int
    depends_on: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    part: int = 0

    #: Assigned once the store has a row for this job.
    id: int | None = None
    state: JobState = JobState.QUEUED
    completed: int = 0
    attempts: int = 0

    @property
    def progress(self) -> float:
        return min(1.0, self.completed / self.requested) if self.requested else 1.0

    def to_row(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "type": self.type,
            "entity": self.entity,
            "state": self.state.value,
            "offset": self.offset,
            "requested": self.requested,
            "completed": self.completed,
            "attempts": self.attempts,
            "part": self.part,
            "depends_on": list(self.depends_on),
            "outputs": list(self.outputs),
        }


@dataclass(slots=True)
class RunOutcome:
    """What a finished run produced."""

    run_id: str
    state: RunState
    records: int
    duration_seconds: float
    files: list[str]
    summary: dict[str, Any]
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.state is RunState.COMPLETED


class RunHandle:
    """Controls a run while it is executing (design document section 36).

    ``POST /api/runs/{id}/pause`` needs something to call. This is it - the
    same object the CLI holds, so pausing from a terminal and pausing from the
    API take exactly the same path.
    """

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self._pause = asyncio.Event()
        self._pause.set()  # set means "not paused"
        self._cancelled = False
        self.paused_at: float | None = None

    @property
    def is_paused(self) -> bool:
        return not self._pause.is_set()

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled

    def pause(self) -> None:
        if not self._cancelled:
            self._pause.clear()
            self.paused_at = time.time()

    def resume(self) -> None:
        self._pause.set()
        self.paused_at = None

    def cancel(self) -> None:
        self._cancelled = True
        # Release anything waiting on the pause gate so it can notice.
        self._pause.set()

    async def checkpoint_gate(self) -> None:
        """Await here at safe points: between batches, never mid-record."""
        if self._cancelled:
            raise RunAborted("run cancelled")
        if self.is_paused:
            await self._pause.wait()
            if self._cancelled:
                raise RunAborted("run cancelled")


class Conductor:
    """Plans and executes a run."""

    def __init__(
        self,
        compiled: CompiledProject,
        config: RunConfig,
        *,
        repository: Repository | None = None,
        project_id: int | None = None,
        revision_id: int | None = None,
        runtime: GenerationRuntime | None = None,
        bus: EventBus | None = None,
        run_id: str | None = None,
    ) -> None:
        self.compiled = compiled
        self.config = config
        self.repository = repository if config.record_history else None
        self.project_id = project_id
        self.revision_id = revision_id
        self.run_id = run_id or str(uuid.uuid4())
        self.bus = bus or EventBus()
        self.handle = RunHandle(self.run_id)
        self.metrics = RunMetrics(run_id=self.run_id)
        self.log = RunLogger(self.run_id)

        self.jobs: list[PlannedJob] = []
        self.files: list[str] = []
        self.state = RunState.QUEUED
        self.error: str | None = None
        #: Set by :meth:`resume`; a resumed run reports differently and skips
        #: the planning step because its jobs come from the store.
        self._resumed = False

        if config.seed is not None:
            compiled.spec.project.seed = config.seed

        self.runtime = runtime if runtime is not None else self._build_runtime()
        #: Built lazily: a project with no media fields should not create an
        #: empty `assets/` directory beside every dataset it produces.
        self._asset_store: Any = None
        self._store_sink: Any = None

    # -- setup -------------------------------------------------------------- #

    @property
    def wants_assets(self) -> bool:
        """Whether any field of this project writes a file."""
        return any(
            getattr(compiled.generator, "kind", None) in ("image", "audio", "document")
            for entity in self.compiled.ordered_entities()
            for compiled in entity.fields
        )

    @property
    def assets(self) -> Any:
        """The asset store, created on first use (sections 19, 81)."""
        if self._asset_store is None and self.wants_assets:
            from ..assets.store import AssetStore

            self._asset_store = AssetStore(
                self.config.asset_root,
                deduplicate=self.config.deduplicate_assets,
                overwrite=self.config.overwrite_assets,
            )
            self._asset_store.open()
        return self._asset_store

    def _build_runtime(self) -> GenerationRuntime | None:
        if not self.compiled.spec.providers:
            return None
        cache = GenerationCache(
            self.config.cache_path if self.config.cache_mode.reads else None,
            mode=self.config.cache_mode,
        )
        return GenerationRuntime.for_project(
            self.compiled.spec,
            cache=cache,
            llm_batch_size=self.config.limits.llm_batch_size,
        )

    def _entity_names(self) -> list[str]:
        selected = self.config.entities or list(self.compiled.entity_order)
        unknown = [name for name in selected if name not in self.compiled.entities]
        if unknown:
            known = ", ".join(self.compiled.entity_order)
            raise GenerationError(
                f"No entity named {', '.join(unknown)}. Known entities: {known}"
            )
        # Keep the compiler's topological order whatever order the user listed.
        return [name for name in self.compiled.entity_order if name in set(selected)]

    def _count_for(self, entity: CompiledEntity) -> int:
        return self.config.records if self.config.records is not None else entity.count

    # -- planning ----------------------------------------------------------- #

    def plan(self) -> list[PlannedJob]:
        """Turn the compiled project into jobs (design document section 28)."""
        jobs: list[PlannedJob] = []
        for sequence, name in enumerate(self._entity_names()):
            entity = self.compiled.entity(name)
            count = self._count_for(entity)
            if count <= 0:
                continue
            path = self._output_path(name)
            jobs.append(
                PlannedJob(
                    sequence=sequence,
                    type=JobType.ENTITY_BATCH.value,
                    entity=name,
                    offset=0,
                    requested=count,
                    depends_on=list(entity.depends_on),
                    outputs=[str(path)],
                )
            )
            self.metrics.entity(name, requested=count)
        self.jobs = jobs
        return jobs

    def preflight(self) -> list[str]:
        """Checks worth making before the first record (section 64)."""
        warnings: list[str] = []

        if self.config.output_format.lower() not in OUTPUT_FORMATS:
            raise GenerationError(
                f"Unknown output format '{self.config.output_format}'. "
                f"Available: {', '.join(sorted(OUTPUT_FORMATS))}"
            )

        estimate = self.compiled.plan.estimate if self.compiled.plan else None
        estimated_bytes = estimate.estimated_bytes if estimate else 0
        if self.config.records is not None and estimate and estimate.records:
            # Scale the estimate to the overridden record count.
            planned = sum(job.requested for job in self.jobs)
            estimated_bytes = int(estimated_bytes * planned / max(estimate.records, 1))

        complaint = self.config.limits.check_disk(
            self.config.output_dir, estimated_bytes=estimated_bytes
        )
        if complaint:
            warnings.append(complaint)

        if self.runtime is not None:
            for provider_id, reason in self.runtime.unavailable.items():
                warnings.append(f"provider '{provider_id}' is unavailable: {reason}")

        for warning in warnings:
            self.log.warning(warning, status="preflight")
            self.bus.emit(EventKind.WARNING, self.run_id, message=warning, level="warning")
        return warnings

    # -- persistence -------------------------------------------------------- #

    def _persist_run(self) -> None:
        if self.repository is None or self.project_id is None:
            return
        estimate = self.compiled.plan.estimate.to_dict() if self.compiled.plan else {}
        self.repository.create_run(
            run_id=self.run_id,
            project_id=self.project_id,
            revision_id=self.revision_id,
            seed=self.compiled.seed,
            output_dir=str(self.config.output_dir),
            output_format=self.config.output_format,
            config=self.config.to_dict(),
            estimate=estimate,
            records_requested=sum(job.requested for job in self.jobs),
        )
        rows = self.repository.create_jobs(self.run_id, [job.to_row() for job in self.jobs])
        for planned, row in zip(self.jobs, rows, strict=True):
            planned.id = row["id"]

        # Log lines are buffered and flushed by the sink rather than written
        # one INSERT at a time; a run emits thousands.
        self._store_sink = self.bus.add_sink(self._write_event)

    def _write_event(self, event: Any) -> None:
        if self.repository is None:
            return
        # Progress events fire constantly and would swamp the table; the store
        # keeps the milestones, and the live feed keeps the rest.
        if event.kind in (EventKind.RUN_PROGRESS, EventKind.JOB_PROGRESS):
            return
        try:
            self.repository.add_events([event.to_store_row()])
        except Exception as exc:  # noqa: BLE001 - logging must never break a run
            self.log.warning(f"could not record run event: {exc}", status="store_error")

    def _set_run_state(self, state: RunState, **fields: Any) -> None:
        if not self.state.can_move_to(state) and state is not self.state:
            self.log.warning(
                f"ignored impossible run transition {self.state} -> {state}",
                status="bad_transition",
            )
            return
        self.state = state
        if self.repository is not None:
            self.repository.update_run(self.run_id, state=state.value, **fields)

    def _set_job_state(self, job: PlannedJob, state: JobState, **fields: Any) -> None:
        job.state = state
        if self.repository is not None and job.id is not None:
            self.repository.update_job(job.id, state=state.value, **fields)

    # -- execution ---------------------------------------------------------- #

    async def execute(self) -> RunOutcome:
        """Run to completion, or to the first thing that stops it."""
        from ..store.models import utcnow

        if not self.jobs:
            self.plan()
        if not self.jobs:
            raise GenerationError("Nothing to generate: every selected entity has a count of 0.")

        self._persist_run()
        self.preflight()

        total = sum(job.requested for job in self.jobs)
        self._set_run_state(RunState.RUNNING, started_at=utcnow())
        self.bus.emit(
            EventKind.RUN_STARTED,
            self.run_id,
            message=f"generating {total:,} records",
            data={
                "entities": [job.entity for job in self.jobs],
                "seed": self.compiled.seed,
                "output_dir": str(self.config.output_dir),
                "format": self.config.output_format,
            },
        )
        self.log.info("run started", status="running", record_range=f"0-{total}")
        return await self._drive()

    async def _drive(self) -> RunOutcome:
        """Execute the job graph and translate however it ended into an outcome.

        Shared by a fresh run and a resumed one, because the difference between
        them is entirely in the setup: which jobs exist and how far each has
        already got.
        """
        started = time.perf_counter()
        try:
            await self._run_jobs()
        except RunAborted:
            self._finish(RunState.CANCELLED, "cancelled by request")
        except CacophonyError as exc:
            self.error = str(exc)
            self._finish(RunState.FAILED, self.error)
        except Exception as exc:  # noqa: BLE001 - surfaced in the outcome
            self.error = f"{type(exc).__name__}: {exc}"
            self._finish(RunState.FAILED, self.error)
        else:
            self._finish(RunState.COMPLETED, None)

        return RunOutcome(
            run_id=self.run_id,
            state=self.state,
            records=self.metrics.total_written,
            duration_seconds=time.perf_counter() - started,
            files=list(self.files),
            summary=self.summary(),
            error=self.error,
        )

    async def _run_jobs(self) -> None:
        """Execute jobs, overlapping the ones that do not depend on each other."""
        remaining = [job for job in self.jobs if job.state is not JobState.COMPLETED]
        done: set[str] = {
            job.entity for job in self.jobs if job.state is JobState.COMPLETED
        }
        limit = asyncio.Semaphore(max(1, self.config.limits.max_workers))

        while remaining:
            ready = [job for job in remaining if set(job.depends_on) <= done]
            if not ready:
                # The compiler proves the graph is acyclic, so this can only
                # mean a dependency was excluded by --entity.
                blocked = ", ".join(sorted(job.entity for job in remaining))
                missing = sorted(
                    {dep for job in remaining for dep in job.depends_on} - done
                )
                raise GenerationError(
                    f"Cannot generate {blocked}: {', '.join(missing)} was not selected "
                    "but is depended upon. Include it, or generate the whole project."
                )

            await asyncio.gather(*(self._guarded(job, limit) for job in ready))
            done.update(job.entity for job in ready)
            remaining = [job for job in remaining if job not in ready]

    async def _guarded(self, job: PlannedJob, limit: asyncio.Semaphore) -> None:
        async with limit:
            await self._run_entity_job(job)

    async def _run_entity_job(self, job: PlannedJob) -> None:
        from ..store.models import utcnow

        entity = self.compiled.entity(job.entity)
        engine = self._engine()
        job.attempts += 1

        # A resumed job continues where it stopped; a fresh one starts at zero.
        start_index = job.offset + job.completed
        remaining = job.requested - job.completed
        if remaining <= 0:
            self._set_job_state(job, JobState.COMPLETED, finished_at=utcnow())
            return

        if job.completed > 0:
            start_index, remaining = self._reconcile(job)
            if remaining <= 0:
                self._set_job_state(job, JobState.COMPLETED, finished_at=utcnow())
                return

        writer, path = self._writer_for(job, entity, resuming=job.completed > 0)
        self.files.append(str(path))
        job.outputs = [str(path)]

        self._set_job_state(
            job,
            JobState.RUNNING,
            started_at=utcnow(),
            attempts=job.attempts,
            outputs=job.outputs,
            part=job.part,
        )
        self.bus.emit(
            EventKind.JOB_STARTED,
            self.run_id,
            job_id=job.id,
            entity=job.entity,
            message=f"{job.entity}: {remaining:,} records -> {path.name}",
            data={
                "offset": start_index,
                # The bar wants the whole job, not the slice this attempt will
                # produce, so a resumed run shows 7,500/80,000 rather than
                # 7,500/72,500.
                "requested": job.requested,
                "remaining": remaining,
                "completed": job.completed,
                "path": str(path),
            },
        )

        metrics = self.metrics.entity(job.entity, requested=job.requested)
        since_checkpoint = 0
        batch_started = time.perf_counter()

        await writer.open()
        try:
            async for batch in engine.stream(
                job.entity,
                count=remaining,
                offset=start_index,
                batch_size=self.config.limits.batch_size,
            ):
                await self.handle.checkpoint_gate()

                await writer.write_batch(batch)
                first = job.offset + job.completed
                job.completed += len(batch)
                since_checkpoint += len(batch)

                self.metrics.record_batch(job.entity, len(batch))
                self.log.batch(
                    entity=job.entity,
                    first=first,
                    last=job.offset + job.completed - 1,
                    duration_ms=(time.perf_counter() - batch_started) * 1000,
                    job_id=job.id,
                )
                batch_started = time.perf_counter()

                self._emit_progress(job)

                # Progress is recorded after *every* batch, not every
                # ``checkpoint_every`` records. A checkpoint that lags the file
                # would make a resume duplicate whatever fell in the gap, and a
                # single small UPDATE per batch is far cheaper than being wrong.
                announce = since_checkpoint >= self.config.checkpoint_every
                self._checkpoint(job, announce=announce)
                if announce:
                    since_checkpoint = 0
        except RunAborted:
            self._checkpoint(job)
            self._set_job_state(job, JobState.PAUSED, completed=job.completed)
            raise
        except Exception as exc:
            self._checkpoint(job)
            self._set_job_state(
                job, JobState.FAILED, completed=job.completed, error=str(exc),
                finished_at=utcnow(),
            )
            self.bus.emit(
                EventKind.JOB_FAILED,
                self.run_id,
                job_id=job.id,
                entity=job.entity,
                level="error",
                message=f"{job.entity} failed after {job.completed:,} records: {exc}",
            )
            self.log.error(
                "job failed", entity=job.entity, job_id=job.id, error=str(exc), status="failed"
            )
            raise
        finally:
            await writer.close()

        self._absorb_engine_stats(engine, job.entity)
        self._checkpoint(job)
        self._set_job_state(
            job, JobState.COMPLETED, completed=job.completed, finished_at=utcnow()
        )
        self.bus.emit(
            EventKind.JOB_COMPLETED,
            self.run_id,
            job_id=job.id,
            entity=job.entity,
            message=f"{job.entity}: {job.completed:,} records written",
            data={"records": job.completed, "path": str(path), **metrics.to_dict()},
        )

    # -- helpers ------------------------------------------------------------ #

    def _engine(self) -> GenerationEngine:
        """One engine per run, so validator state and stats accumulate."""
        if not hasattr(self, "_engine_instance"):
            self._engine_instance = GenerationEngine(
                self.compiled,
                validate=self.config.validate,
                drop_invalid=self.config.drop_invalid,
                provenance=self.config.provenance,
                failure_policy=self.config.failure_policy,
                max_attempts=self.config.limits.max_retries,
                run_id=self.run_id,
                runtime=self.runtime,
                assets=self.assets,
                edge_cases=self.config.edge_cases,
                edge_categories=self.config.edge_categories,
                # What this run will actually produce, which is not what the
                # schema declares when --records overrides it. A reference must
                # point inside the run, or a five-record preview cites record
                # seventeen.
                counts={
                    name: self._count_for(self.compiled.entity(name))
                    for name in self.compiled.entity_order
                },
            )
        return self._engine_instance

    def _writer_for(
        self, job: PlannedJob, entity: CompiledEntity, *, resuming: bool
    ) -> tuple[OutputWriter, Path]:
        writer_class = OUTPUT_FORMATS[self.config.output_format.lower()]
        part: int | None = None

        if resuming and not writer_class.appendable:
            # A JSON array or a Parquet file has a footer; continuing means a
            # new part rather than reopening. Readers for both accept a set of
            # parts, so the dataset stays whole.
            job.part += 1
            part = job.part
            self.log.info(
                "resuming into a new part file",
                entity=job.entity,
                job_id=job.id,
                status="resumed",
                part=job.part,
            )

        path = self._output_path(job.entity)
        writer = create_writer(
            self.config.output_format,
            path,
            columns=entity.spec.field_names(),
            provenance=self.config.provenance,
            append=resuming,
            part=part,
            # Only the database writers use these; create_writer drops them for
            # the formats that would reject the keyword.
            entity=entity,
            entities=self.compiled.entities,
            # A chaotic run deliberately produces records the schema forbids,
            # so the DDL must describe the data rather than the intent (section
            # 24). Without this the first nulled field aborts the insert.
            chaos=self._engine().inject_chaos,
        )
        return writer, writer.path  # type: ignore[attr-defined]

    def _output_path(self, entity: str) -> Path:
        """Where one entity's records go.

        Single-file formats collapse to one destination named after the
        project, so a SQLite run produces a database rather than a directory
        of unrelated files.
        """
        return output_path_for(
            self.config.output_dir,
            entity,
            self.config.output_format,
            database_name=_slug(self.compiled.name) or "cacophony",
        )

    def _reconcile(self, job: PlannedJob) -> tuple[int, int]:
        """Make the checkpoint agree with what is actually on disk.

        An unclean stop can leave the two out of step in either direction, so
        the file is counted and the checkpoint corrected to match. Doing this
        before appending is what keeps a resumed dataset free of duplicated and
        skipped records.
        """
        path = Path(job.outputs[0]) if job.outputs else None
        if path is None:
            return job.offset + job.completed, job.requested - job.completed

        actual = align_to_records(
            path, job.completed, self.config.output_format, table=job.entity
        )
        if actual != job.completed:
            self.log.warning(
                "checkpoint disagreed with the file on disk; trusting the file",
                entity=job.entity,
                job_id=job.id,
                status="reconciled",
                data_checkpoint=job.completed,
                data_actual=actual,
            )
            job.completed = actual
            metrics = self.metrics.entity(job.entity)
            metrics.written = actual
            metrics.generated = actual
            if self.repository is not None and job.id is not None:
                self.repository.checkpoint_job(job.id, completed=actual)
        return job.offset + job.completed, job.requested - job.completed

    def _checkpoint(self, job: PlannedJob, *, announce: bool = True) -> None:
        """Persist progress (design document section 32)."""
        if self.repository is None or job.id is None:
            return
        checkpoint = {
            "run": self.run_id,
            "entity": job.entity,
            "completed": job.completed,
            "requested": job.requested,
            "offset": job.offset,
            "part": job.part,
            "seed": self.compiled.seed,
            "last_checkpoint": time.time(),
        }
        self.repository.checkpoint_job(job.id, completed=job.completed, checkpoint=checkpoint)
        if not announce:
            return
        self.repository.update_run(self.run_id, records_written=self.metrics.total_written)
        self.bus.emit(
            EventKind.JOB_CHECKPOINT,
            self.run_id,
            job_id=job.id,
            entity=job.entity,
            message=f"{job.entity}: checkpoint at {job.completed:,}",
            data=checkpoint,
        )

    def _emit_progress(self, job: PlannedJob) -> None:
        self.bus.emit(
            EventKind.JOB_PROGRESS,
            self.run_id,
            job_id=job.id,
            entity=job.entity,
            data={
                "completed": job.completed,
                "requested": job.requested,
                "progress": round(job.progress, 6),
                **self.metrics.snapshot(),
            },
        )

    def _absorb_engine_stats(self, engine: GenerationEngine, entity: str) -> None:
        stats = engine.stats.get(entity)
        if stats is not None:
            metrics = self.metrics.entity(entity)
            metrics.rejected = stats.rejected
            metrics.repaired = stats.repaired
            metrics.field_failures = stats.field_failures
            self.metrics.validation_failures = sum(
                item.rejected for item in engine.stats.values()
            )
        if self.runtime is not None:
            self.metrics.absorb_provider_stats(self.runtime.stats)
            self.metrics.cache_hits = self.runtime.cache.stats.hits
            self.metrics.cache_misses = self.runtime.cache.stats.misses

    def _finish(self, state: RunState, error: str | None) -> None:
        from ..store.models import utcnow

        summary = self.summary()
        self._set_run_state(
            state,
            finished_at=utcnow(),
            records_written=self.metrics.total_written,
            summary=summary,
            error=error,
        )

        if self.repository is not None:
            # The summary's quality, not the metrics', because the summary has
            # the referential and distribution scores merged in.
            self.repository.record_statistics(
                self.run_id, summary.get("quality") or self.metrics.quality(), scope="quality"
            )
            self.repository.record_statistics(
                self.run_id,
                {
                    "records_written": self.metrics.total_written,
                    "records_per_second": round(self.metrics.records.mean_rate, 2),
                    "provider_calls": self.metrics.provider_calls,
                    "validation_failures": self.metrics.validation_failures,
                },
            )
            if self.config.history_limit:
                self.repository.prune_runs(keep=self.config.history_limit)

        kinds = {
            RunState.COMPLETED: EventKind.RUN_COMPLETED,
            RunState.FAILED: EventKind.RUN_FAILED,
            RunState.CANCELLED: EventKind.RUN_CANCELLED,
        }
        self.bus.emit(
            kinds.get(state, EventKind.RUN_COMPLETED),
            self.run_id,
            level="error" if state is RunState.FAILED else "info",
            message=error or f"{self.metrics.total_written:,} records written",
            data=summary,
        )
        self.log.info(
            f"run {state.value}",
            status=state.value,
            error=error,
            duration_ms=round(self.metrics.records.elapsed * 1000, 2),
        )

        if self._store_sink is not None:
            self._store_sink()
            self._store_sink = None

    def summary(self) -> dict[str, Any]:
        data = self.metrics.snapshot()
        data["quality"] = self.metrics.quality()
        data["files"] = list(dict.fromkeys(self.files))

        # What the validators learned, which the metrics counter cannot know:
        # it counts failures, while these describe the dataset (section 58).
        engine = getattr(self, "_engine_instance", None)
        if engine is not None:
            validation = engine.validation_stats()
            if validation:
                data["validation"] = validation
                data["quality"].update(_quality_from_validation(validation))
            if engine.resolver.stats.key_lookups:
                data["relations"] = engine.resolver.describe()

        if self._asset_store is not None and self._asset_store.stats.total:
            data["assets"] = self._asset_store.describe()

        # What the synthetic world did (sections 17, 24, 25, 26).
        if engine is not None:
            if engine.scenarios is not None and engine.scenarios.applied:
                data["scenarios"] = engine.scenarios.describe()
            if engine.simulations:
                data["simulation"] = {
                    name: simulation.describe()
                    for name, simulation in engine.simulations.items()
                }
            damage = {
                name: injector.describe()
                for name, injector in engine._injectors.items()
                if injector.stats.records_damaged or injector.stats.duplicates_emitted
            }
            if damage:
                data["chaos"] = damage

            # What the model repeated (section 59). Reported per entity, and
            # folded into section 58's project score as `uniqueness`, because
            # "how much of this is the same thing twice" is a quality number
            # rather than a curiosity.
            duplication = engine.duplication_reports()
            if duplication:
                data["duplication"] = duplication
                data["quality"].update(_quality_from_duplication(duplication))

            # What was made deliberately awkward (section 79).
            edges = engine.edge_case_reports()
            if edges:
                data["edge_cases"] = edges

        data["jobs"] = [
            {
                "entity": job.entity,
                "state": job.state.value,
                "completed": job.completed,
                "requested": job.requested,
                "part": job.part,
            }
            for job in self.jobs
        ]
        if self.runtime is not None:
            data["providers"] = self.runtime.stats.to_dict()
            data["cache"] = self.runtime.cache.describe()
        return data

    async def aclose(self) -> None:
        if self.runtime is not None:
            await self.runtime.aclose()
        if self._asset_store is not None:
            self._asset_store.close()

    # -- resuming ----------------------------------------------------------- #

    @classmethod
    def resume(
        cls,
        compiled: CompiledProject,
        stored_run: dict[str, Any],
        *,
        repository: Repository,
        runtime: GenerationRuntime | None = None,
        bus: EventBus | None = None,
    ) -> Conductor:
        """Rebuild a conductor from a stored run (design document section 32).

        The configuration is taken from the run rather than from the command
        line, so a resume repeats the original run's decisions - the same seed,
        the same output format, the same provenance mode - rather than quietly
        producing a dataset generated two different ways.
        """
        config = RunConfig.from_dict(stored_run.get("config") or {})
        config.seed = stored_run.get("seed", config.seed)

        conductor = cls(
            compiled,
            config,
            repository=repository,
            project_id=stored_run["project_id"],
            revision_id=stored_run.get("revision_id"),
            runtime=runtime,
            bus=bus,
            run_id=stored_run["id"],
        )
        conductor.state = RunState(stored_run.get("state", "failed"))

        jobs: list[PlannedJob] = []
        for row in repository.get_jobs(stored_run["id"]):
            planned = PlannedJob(
                sequence=row["sequence"],
                type=row["type"],
                entity=row["entity"],
                offset=row["offset"],
                requested=row["requested"],
                depends_on=list(row["depends_on"]),
                outputs=list(row["outputs"]),
                part=row["part"],
            )
            planned.id = row["id"]
            planned.completed = row["completed"]
            planned.attempts = row["attempts"]
            planned.state = JobState(row["state"])
            # Anything that was mid-flight when the process stopped is queued
            # again; its checkpoint says how much of it is already done.
            if planned.state in (JobState.RUNNING, JobState.PAUSED, JobState.RETRYING):
                planned.state = JobState.QUEUED
            jobs.append(planned)

            metrics = conductor.metrics.entity(planned.entity, requested=planned.requested)
            metrics.written = planned.completed
            metrics.generated = planned.completed

        conductor.jobs = jobs
        conductor._resumed = True
        return conductor

    @property
    def is_resume(self) -> bool:
        return self._resumed

    async def execute_resume(self) -> RunOutcome:
        """Continue a stored run from its checkpoints."""
        from ..store.models import utcnow

        completed = sum(job.completed for job in self.jobs)
        self.state = RunState.PAUSED if self.state is RunState.RUNNING else self.state
        self._set_run_state(RunState.RUNNING, started_at=utcnow(), error=None)

        if self.repository is not None:
            self._store_sink = self.bus.add_sink(self._write_event)

        self.bus.emit(
            EventKind.RUN_RESUMED,
            self.run_id,
            message=f"resuming at {completed:,} records",
            data={"completed": completed},
        )
        self.log.info("run resumed", status="resumed", record_range=f"{completed}-")
        return await self._drive()


def _quality_from_validation(validation: dict[str, Any]) -> dict[str, float]:
    """Section 58's referential and statistical scores, across every entity.

    Referential integrity is weighted by how many references each entity
    actually checked, because a hundred-record lookup table with one reference
    should not count as much as ten million events with ten million. The
    distribution score is a plain mean over the fields that declare one, since
    each is already a proportion.
    """
    checked = 0
    broken = 0
    matches: list[float] = []

    for entity in validation.values():
        referential = entity.get("referential")
        if referential:
            checked += int(referential.get("references_checked", 0))
            broken += int(referential.get("broken_references", 0))

        statistical = entity.get("statistical")
        if statistical:
            matches.extend(
                float(check["match"]) for check in statistical.get("checks", []) if "match" in check
            )

    quality: dict[str, float] = {}
    if checked:
        quality["referential_integrity"] = round(1.0 - (broken / checked), 6)
    if matches:
        quality["distribution_match"] = round(sum(matches) / len(matches), 6)
    return quality


def _quality_from_duplication(duplication: dict[str, Any]) -> dict[str, float]:
    """Section 58's uniqueness score, across every entity (section 59).

    Weighted by how many values each entity compared, for the same reason
    referential integrity is: an entity with two prose fields and ten million
    records says more about the dataset than one with a single field and four
    hundred.
    """
    values = 0
    repeated = 0
    for report in duplication.values():
        checked = int(report.get("checked_values", 0))
        values += checked
        repeated += int(report.get("exact", 0)) + int(report.get("normalized", 0))
        repeated += int(report.get("near", 0))

    return {"uniqueness": round(max(0.0, 1.0 - repeated / values), 6)} if values else {}


def _slug(text: str) -> str:
    """A project name reduced to something safe to put in a file name."""
    cleaned = "".join(
        character if character.isalnum() else "-" for character in text.strip().lower()
    )
    return "-".join(part for part in cleaned.split("-") if part)

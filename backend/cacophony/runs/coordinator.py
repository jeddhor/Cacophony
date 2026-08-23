"""The generation Conductor (design document sections 27-32, 55, 56, 65, 108).

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
import json
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..core.errors import CacophonyError, GenerationError, ValidationFailedError
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


class RunAbortedError(CacophonyError):
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
    #: Rows written. Not a position: validation can drop a record and entropy
    #: injection can duplicate one, so this is *output*, not *progress through
    #: the source*.
    completed: int = 0
    attempts: int = 0

    #: The next source index to generate. ``-1`` means "not started", which is
    #: `offset`. Persisted in the checkpoint, because resuming at
    #: ``offset + completed`` is only right for a run that dropped and
    #: duplicated nothing (design document section 32).
    cursor: int = -1
    #: Where the last committed batch, and the current attempt, began - so a
    #: checkpoint that turns out to disagree with the file can be rewound to a
    #: boundary rather than guessed at.
    batch_start_cursor: int = -1
    batch_start_completed: int = 0
    attempt_start_cursor: int = -1
    attempt_start_completed: int = 0

    @property
    def position(self) -> int:
        """The next source index, resolving "not started" to the offset."""
        return self.offset if self.cursor < 0 else self.cursor

    @property
    def consumed(self) -> int:
        """Source records this job has processed, dropped ones included."""
        return max(0, self.position - self.offset)

    @property
    def outstanding(self) -> int:
        """Source records still to process."""
        return max(0, self.requested - self.consumed)

    @property
    def progress(self) -> float:
        # Measured in source records, so a run that legitimately writes fewer
        # rows than it read still reaches 100%.
        return min(1.0, self.consumed / self.requested) if self.requested else 1.0

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
    #: Whether the run stopped because a record failed validation rather than
    #: because something went wrong with the machinery. The two need different
    #: advice: one is fixed in the schema, the other by resuming.
    validation_failure: bool = False

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
            raise RunAbortedError("run cancelled")
        if self.is_paused:
            await self._pause.wait()
            if self._cancelled:
                raise RunAbortedError("run cancelled")


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
        # Anchored before anything is recorded. A run's paths are stored and
        # read back by a resume that may be started from a different working
        # directory - where `out/` is a different directory, and "resuming"
        # would quietly begin a second dataset somewhere else.
        self.config.anchor_paths()
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
        #: Set when the run stopped on a record that failed validation, so the
        #: caller can offer the advice that helps rather than "try resuming".
        self._validation_failure = False
        #: Section 57's semantic verdicts, when the schema asked for them.
        self._semantic: dict[str, Any] = {}
        #: Entities whose uniqueness could not be rechecked across a resume.
        self._unique_gaps: list[str] = []
        #: Images and audio-seconds already folded into the live rates.
        self._assets_seen: tuple[int, float] = (0, 0.0)
        #: How large each destination was when it was last measured, so bytes
        #: are attributed to the file that grew rather than to every writer
        #: that happens to share it.
        self._destination_bytes: dict[str, int] = {}
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
            request_timeout_seconds=self.config.limits.request_timeout_seconds,
        )

    def _entity_names(self) -> list[str]:
        selected = self.config.entities or list(self.compiled.entity_order)
        unknown = [name for name in selected if name not in self.compiled.entities]
        if unknown:
            known = ", ".join(self.compiled.entity_order)
            raise GenerationError(f"No entity named {', '.join(unknown)}. Known entities: {known}")
        # Keep the compiler's topological order whatever order the user listed.
        return [name for name in self.compiled.entity_order if name in set(selected)]

    def _count_for(self, entity: CompiledEntity) -> int:
        """How many records this entity gets: named, blunt, or as declared."""
        named = self.config.record_counts.get(entity.name)
        if named is not None:
            return named
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
        self._claim_destination(jobs)
        return jobs

    def _claim_destination(self, jobs: list[PlannedJob]) -> None:
        """Refuse to write into a destination that already holds a dataset.

        A writer truncates the file it opens, and stops there: a previous run's
        part files and partition directories are not opened, so they survive
        and are read back as part of *this* run's output. One record can arrive
        in a directory that then reports two, and a ten-row run can sit beside a
        stale sixty-row part.

        So a fresh run owns an empty destination, or is told to replace what is
        there. `--overwrite` removes exactly what this run would have written -
        never the whole directory, which may be somebody's home.
        """
        existing = [path for job in jobs for path in self._destinations_of(job.entity)]
        if not existing:
            return

        if not self.config.overwrite:
            listed = ", ".join(sorted(str(path) for path in existing)[:4])
            more = f" and {len(existing) - 4} more" if len(existing) > 4 else ""
            raise GenerationError(
                f"{self.config.output_dir} already holds output from an earlier run "
                f"({listed}{more}). Generating into it would mix two datasets: the writers "
                "replace the files they open and leave every other one in place. Choose an "
                "empty directory, or pass --overwrite to replace what is there."
            )

        for path in existing:
            try:
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
            except OSError as exc:  # pragma: no cover - filesystem specific
                raise GenerationError(f"could not replace {path}: {exc}") from exc
        self.log.info(
            "replaced an earlier run's output",
            status="overwrite",
            data_paths=len(existing),
        )

    def _destinations_of(self, entity: str) -> list[Path]:
        """Everything on disk this entity's job would own, parts included."""
        path = self._output_path(entity)
        found: list[Path] = []
        if path.exists():
            found.append(path)
        # `employee.part0001.jsonl` and friends: written by a resume, owned by
        # the dataset, and invisible to a writer that only opens `employee.jsonl`.
        if not self.config.partition_by and path.suffix:
            found.extend(sorted(path.parent.glob(f"{path.stem}.part[0-9]*{path.suffix}")))
        return found

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
            # A floor is a floor. Below the free-space threshold the run does
            # not start; the estimate-based complaint stays a warning, because
            # section 69 is explicit that an estimate is not a measurement.
            if self.config.limits.below_floor(self.config.output_dir):
                raise GenerationError(complaint)
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
        except Exception as exc:
            # Recording history must never break a run. Generation is the
            # point; the event log is bookkeeping about it.
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
            # Section 57's optional semantic pass, after the records exist and
            # before the summary is assembled. Off unless the schema asked, and
            # it never fails a run: an opinion that could not be obtained is
            # reported as one that could not be obtained.
            await self._judge_semantics()
        except RunAbortedError:
            self._finish(RunState.CANCELLED, "cancelled by request")
        except ValidationFailedError as exc:
            self.error = str(exc)
            self._validation_failure = True
            self._finish(RunState.FAILED, self.error)
        except CacophonyError as exc:
            self.error = str(exc)
            self._finish(RunState.FAILED, self.error)
        except Exception as exc:
            # Surfaced in the outcome rather than raised: the caller gets a
            # RunOutcome describing the failure, with the files written so far.
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
            validation_failure=self._validation_failure,
        )

    async def _judge_semantics(self) -> None:
        """Ask the judge, if there is one. Never raises into the run."""
        if self._engine_instance is None:
            return
        try:
            self._semantic = await self._engine_instance.semantic_reports()
        except Exception as exc:  # pragma: no cover - a provider problem
            self.log.warning("semantic evaluation failed", status="degraded", error=str(exc))

    async def _run_jobs(self) -> None:
        """Execute jobs, overlapping the ones that do not depend on each other."""
        remaining = [job for job in self.jobs if job.state is not JobState.COMPLETED]
        done: set[str] = {job.entity for job in self.jobs if job.state is JobState.COMPLETED}
        limit = asyncio.Semaphore(max(1, self.config.limits.max_workers))

        while remaining:
            ready = [job for job in remaining if set(job.depends_on) <= done]
            if not ready:
                # The compiler proves the graph is acyclic, so this can only
                # mean a dependency was excluded by --entity.
                blocked = ", ".join(sorted(job.entity for job in remaining))
                missing = sorted({dep for job in remaining for dep in job.depends_on} - done)
                raise GenerationError(
                    f"Cannot generate {blocked}: {', '.join(missing)} was not selected "
                    "but is depended upon. Include it, or generate the whole project."
                )

            await self._run_group(ready, limit)
            done.update(job.entity for job in ready)
            remaining = [job for job in remaining if job not in ready]

    async def _run_group(self, ready: list[PlannedJob], limit: asyncio.Semaphore) -> None:
        """Run a layer of jobs, and stop the whole layer if one of them fails.

        `asyncio.gather` propagates the first exception and leaves its siblings
        running: the run was marked failed while another entity carried on
        writing, and finished afterwards - into a dataset that had already been
        declared dead. Every task is cancelled and awaited before the failure
        is re-raised, so a terminal state means nothing is still writing.
        """
        # Everything in this layer is ready and waiting for a worker; the depth
        # falls as each one gets a slot. Section 86 asks for this number, and
        # for a long time it was reported as zero because nothing set it.
        self.metrics.queue_depth = len(ready)
        tasks = [
            asyncio.create_task(self._guarded(job, limit), name=f"cacophony-job-{job.entity}")
            for job in ready
        ]
        completed, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)

        # Every exception is retrieved, not just the first: a short-circuiting
        # search leaves the others unretrieved, and asyncio complains about
        # them at garbage-collection time in somebody else's log.
        failures = [task.exception() for task in completed if task.exception() is not None]
        failure: BaseException | None = failures[0] if failures else None
        if failure is None and not pending:
            return

        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if failure is not None:
            raise failure

    async def _guarded(self, job: PlannedJob, limit: asyncio.Semaphore) -> None:
        async with limit:
            # Off the queue and onto a worker.
            self.metrics.queue_depth = max(0, self.metrics.queue_depth - 1)
            await self._run_entity_job(job)

    async def _run_entity_job(self, job: PlannedJob) -> None:
        from ..store.models import utcnow

        entity = self.compiled.entity(job.entity)
        engine = self._engine()
        job.attempts += 1

        # A resumed job continues from where the *source* got to; a fresh one
        # starts at its offset.
        resuming = job.completed > 0 or job.consumed > 0
        if resuming:
            self._reconcile(job)
            self._restore_uniqueness(job, engine)
        start_index, remaining = job.position, job.outstanding
        if remaining <= 0:
            self._set_job_state(job, JobState.COMPLETED, finished_at=utcnow())
            return

        writer, path = self._writer_for(job, entity, resuming=resuming)
        # Appended, not replaced: a resumed job's earlier parts are still part
        # of the dataset, and the run's file list is what its summary and its
        # byte total are derived from.
        if str(path) not in job.outputs:
            job.outputs.append(str(path))
        if str(path) not in self.files:
            self.files.append(str(path))
        job.attempt_start_cursor = start_index
        job.attempt_start_completed = job.completed

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
        # Where this destination started. A resumed run continues somebody
        # else's file, and only what this attempt adds is this attempt's. Keyed
        # by destination rather than by writer: in a SQLite run every entity
        # writes into one database, and one file's growth counted once per
        # writer is the same bytes counted three times.
        destination = str(Path(writer.path).resolve())
        self._destination_bytes.setdefault(destination, writer.bytes_written)

        await writer.open()
        try:
            async for chunk in engine.stream(
                job.entity,
                count=remaining,
                offset=start_index,
                batch_size=self.config.limits.batch_size,
            ):
                # Before the write, and on every chunk including an empty one:
                # a batch whose records were all dropped is still a place a run
                # can be paused or cancelled.
                await self.handle.checkpoint_gate()

                job.batch_start_cursor = chunk.first_index
                job.batch_start_completed = job.completed
                if chunk.records:
                    await writer.write_batch(chunk.records)
                first = chunk.first_index
                job.completed += len(chunk.records)
                job.cursor = chunk.next_index
                since_checkpoint += len(chunk.records)

                # What actually landed on disk, so the live view can report a
                # byte rate at all: section 55 asks for disk throughput, and
                # this counter was never given anything to count.
                on_disk = writer.bytes_written
                grew = max(0, on_disk - self._destination_bytes.get(destination, 0))
                self._destination_bytes[destination] = max(
                    on_disk, self._destination_bytes.get(destination, 0)
                )
                self.metrics.record_batch(job.entity, len(chunk.records), bytes_written=grew)
                self.log.batch(
                    entity=job.entity,
                    first=first,
                    last=max(first, chunk.next_index - 1),
                    duration_ms=(time.perf_counter() - batch_started) * 1000,
                    job_id=job.id,
                )
                batch_started = time.perf_counter()

                self._absorb_asset_stats()
                # Folded in per batch rather than when the entity finishes: a
                # single-entity run showed zeros for provider calls, tokens and
                # validation failures until the very end, which is exactly when
                # nobody needs them (section 86).
                self._absorb_engine_stats(engine, job.entity)
                self._emit_progress(job)

                # Progress is recorded after *every* batch, not every
                # ``checkpoint_every`` records. A checkpoint that lags the file
                # would make a resume duplicate whatever fell in the gap, and a
                # single small UPDATE per batch is far cheaper than being wrong.
                announce = since_checkpoint >= self.config.checkpoint_every
                self._checkpoint(job, announce=announce)
                if announce:
                    since_checkpoint = 0
        except RunAbortedError:
            self._checkpoint(job)
            self._set_job_state(job, JobState.PAUSED, completed=job.completed)
            raise
        except Exception as exc:
            self._checkpoint(job)
            self._set_job_state(
                job,
                JobState.FAILED,
                completed=job.completed,
                error=str(exc),
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
        self._set_job_state(job, JobState.COMPLETED, completed=job.completed, finished_at=utcnow())
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
                # Retries, not attempts: `max_retries=1` means one retry, so
                # two attempts. Passed straight through, it meant one attempt
                # and no retry at all - a setting that did the opposite of its
                # name (section 64).
                max_attempts=self.config.limits.max_retries + 1,
                run_id=self.run_id,
                runtime=self.runtime,
                assets=self.assets,
                unique_memory_ceiling=self.config.limits.unique_memory_values,
                keep_rejects=self.config.limits.keep_rejects,
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

        # What will actually be built, not what the format alone implies: a
        # partitioned run wraps that writer in one that is never appendable, so
        # asking the format was how a resumed partitioned run came to append
        # into a child file whose last batch may be torn.
        appendable = writer_class.appendable and not self.config.partition_by

        if resuming and not appendable:
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
            # Section 34's output profiles. Empty unless a profile asked for
            # partitioning, in which case the path above becomes a directory.
            partition_by=self.config.partition_by,
            **self.config.output_options,
            # Only the database writers use these; create_writer drops them for
            # the formats that would reject the keyword.
            entity=entity,
            entities=self.compiled.entities,
            # A chaotic run deliberately produces records the schema forbids,
            # so the DDL must describe the data rather than the intent (section
            # 24). Without this the first nulled field aborts the insert.
            chaos=self._engine().inject_chaos,
            # Section 25: a zoned project's datetimes carry an offset, and a
            # column typed `TIMESTAMP` would drop it on the way in.
            zoned=self.compiled.spec.timeline.is_zoned(),
        )
        return writer, writer.path

    def _output_path(self, entity: str) -> Path:
        """Where one entity's records go.

        Single-file formats collapse to one destination named after the
        project, so a SQLite run produces a database rather than a directory
        of unrelated files.
        """
        if self.config.partition_by:
            # A partitioned entity owns a directory rather than a file, and the
            # partition columns name the directories inside it.
            return Path(self.config.output_dir) / entity
        return output_path_for(
            self.config.output_dir,
            entity,
            self.config.output_format,
            database_name=_slug(self.compiled.name) or "cacophony",
        )

    def _restore_uniqueness(self, job: PlannedJob, engine: GenerationEngine) -> None:
        """Show the resumed run what the earlier attempts already wrote.

        Uniqueness is checked against a set held in memory, and memory does not
        survive the process that stopped. Without this a resumed run enforces
        `unique: true` only against its own half - and reports nothing, which
        is the worst of the three possible behaviours.

        Parquet and partitioned trees cannot be read back cheaply, and reading
        the whole dataset is the one thing this project will not do; there the
        run says so, loudly, rather than implying a check it did not make.
        """
        from ..outputs import read_written_values

        fields = engine.unique_fields(job.entity)
        if not fields:
            return

        remembered = 0
        for output in job.outputs:
            values = read_written_values(
                output, self.config.output_format, fields, table=job.entity
            )
            if values is None:
                message = (
                    f"{job.entity}: uniqueness cannot be rechecked across this resume - "
                    f"{self.config.output_format} output written by the earlier attempt "
                    "cannot be read back, so duplicates spanning the two halves will not "
                    "be reported"
                )
                self.log.warning(message, entity=job.entity, job_id=job.id, status="degraded")
                self.bus.emit(EventKind.WARNING, self.run_id, message=message, level="warning")
                self._unique_gaps.append(job.entity)
                return
            remembered += engine.remember_written(job.entity, values)

        if remembered:
            self.log.info(
                "uniqueness restored from the earlier attempt",
                entity=job.entity,
                job_id=job.id,
                status="resumed",
                data_values=remembered,
            )

    def _reconcile(self, job: PlannedJob) -> None:
        """Make the checkpoint and the file on disk agree, exactly.

        An unclean stop leaves them out of step in one of three ways, and each
        has a different right answer. The file may hold *more* rows than the
        checkpoint claims, because a write landed and the checkpoint did not:
        an appendable file is trimmed back to the checkpoint, which is a
        position we know the source index for. It may hold exactly what the
        checkpoint claims, which is the ordinary case. Or it may hold *fewer*,
        because the checkpoint landed and the write did not - so the run rewinds
        to the start of that batch, whose position the checkpoint also records.

        Counting the file and trusting the count, which is what this used to do,
        answers "how many rows are there" and then uses it as "where was the
        source", which are the same number only for a schema that drops and
        duplicates nothing.
        """
        path = Path(job.outputs[-1]) if job.outputs else None
        if path is None:
            return

        actual = align_to_records(path, job.completed, self.config.output_format, table=job.entity)
        if actual == job.completed:
            return

        if actual > job.completed:
            # Untrimmable format (a footer, or a torn part): the rows are there
            # and cannot be removed, so the source position they came from is
            # the only thing that keeps the dataset whole.
            self.log.warning(
                "the file holds more rows than the checkpoint; keeping the file",
                entity=job.entity,
                job_id=job.id,
                status="reconciled",
                data_checkpoint=job.completed,
                data_actual=actual,
            )
            job.completed = actual
        elif job.batch_start_cursor >= 0 and actual <= job.batch_start_completed:
            # The last batch never reached the disk. Its position is recorded,
            # so this rewinds to a boundary rather than to a guess.
            self.log.warning(
                "the last batch did not reach the disk; regenerating it",
                entity=job.entity,
                job_id=job.id,
                status="reconciled",
                data_checkpoint=job.completed,
                data_actual=actual,
            )
            align_to_records(
                path, job.batch_start_completed, self.config.output_format, table=job.entity
            )
            job.completed = job.batch_start_completed
            job.cursor = job.batch_start_cursor
        else:
            # Neither boundary matches: the file is short by part of a batch
            # that cannot be trimmed. Say so rather than quietly continuing
            # from a position that does not correspond to it.
            self.log.warning(
                "the file and the checkpoint cannot be reconciled exactly",
                entity=job.entity,
                job_id=job.id,
                status="reconciled",
                data_checkpoint=job.completed,
                data_actual=actual,
            )
            job.completed = actual

        metrics = self.metrics.entity(job.entity)
        metrics.written = job.completed
        metrics.generated = job.completed
        self._checkpoint(job, announce=False)

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
            # Where the source got to, which is what a resume needs, and the
            # boundaries a disagreement with the file can be rewound to.
            "cursor": job.position,
            "batch_start_cursor": job.batch_start_cursor,
            "batch_start_completed": job.batch_start_completed,
            "attempt_start_cursor": job.attempt_start_cursor,
            "attempt_start_completed": job.attempt_start_completed,
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

    def _absorb_asset_stats(self) -> None:
        """Media produced since the last look, as section 55's two rates."""
        store = self._asset_store
        if store is None:
            return
        images = store.stats.images - self._assets_seen[0]
        seconds = store.stats.audio_seconds - self._assets_seen[1]
        if images or seconds:
            self.metrics.record_assets(images=max(0, images), audio_seconds=max(0.0, seconds))
            self._assets_seen = (store.stats.images, store.stats.audio_seconds)

    def _absorb_engine_stats(self, engine: GenerationEngine, entity: str) -> None:
        stats = engine.stats.get(entity)
        if stats is not None:
            metrics = self.metrics.entity(entity)
            metrics.rejected = stats.rejected
            metrics.repaired = stats.repaired
            metrics.field_failures = stats.field_failures
            self.metrics.validation_failures = sum(item.rejected for item in engine.stats.values())
        if self.runtime is not None:
            self.metrics.absorb_provider_stats(self.runtime.stats)
            self.metrics.cache_hits = self.runtime.cache.stats.hits
            self.metrics.cache_misses = self.runtime.cache.stats.misses

    def _finish(self, state: RunState, error: str | None) -> None:
        from ..store.models import utcnow

        # Before the summary, which reports where they went.
        self._write_rejects()
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
        # Measured from the files rather than carried in a counter. The counter
        # follows buffered writes and knows nothing about a footer, a rotated
        # partition or a database that several entities shared; the run
        # inspector's "output size" should be the size of the output.
        data["bytes_written"] = _size_on_disk(data["files"])

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

        if engine is not None:
            rejections = engine.rejection_summary()
            if rejections:
                # Where they are, as well as how many: a count in a summary is
                # a question, and the file is the answer to it (section 56).
                data["rejected"] = {
                    "entities": rejections,
                    "path": str(self._rejects_dir()),
                }

        overrides = engine.policy_overrides() if engine is not None else {}
        if overrides:
            # Section 65: which fields decided for themselves.
            data["failure_policy_overrides"] = overrides

        if self._unique_gaps:
            # In the summary as well as in the events: a report that quietly
            # omits a check somebody asked for is worse than one that failed it.
            data["uniqueness_unverified"] = sorted(set(self._unique_gaps))

        if self._asset_store is not None and self._asset_store.stats.total:
            data["assets"] = self._asset_store.describe()

        # What the synthetic world did (sections 17, 24, 25, 26).
        if engine is not None:
            if engine.scenarios is not None and engine.scenarios.applied:
                data["scenarios"] = engine.scenarios.describe()
            if engine.simulations:
                data["simulation"] = {
                    name: simulation.describe() for name, simulation in engine.simulations.items()
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

            # What a judge model thought of a sample (section 57), when one was
            # asked. Reported beside the measurements rather than among them,
            # because it is an opinion and carries the name of who held it.
            if self._semantic:
                data["semantic"] = self._semantic

            # What was made deliberately awkward (section 79).
            edges = engine.edge_case_reports()
            if edges:
                data["edge_cases"] = edges

            # What the project's own patch rules did (section 104).
            patched = engine.patch_reports()
            if patched:
                data["patches"] = patched

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

    def _rejects_dir(self) -> Path:
        """Where rejected records go: beside the data, never in the store.

        Section 42 keeps generated data out of the metadata database, and a
        rejected record is generated data - it is a record the run produced and
        then declined to write.
        """
        return Path(self.config.output_dir) / "rejects"

    def _write_rejects(self) -> None:
        """Write the sample of rejected records, one file per entity."""
        engine = getattr(self, "_engine_instance", None)
        if engine is None:
            return
        rejected = engine.rejected_records()
        if not rejected:
            return

        directory = self._rejects_dir()
        try:
            directory.mkdir(parents=True, exist_ok=True)
            for entity, records in rejected.items():
                path = directory / f"{entity}.jsonl"
                with path.open("w", encoding="utf-8") as handle:
                    for record in records:
                        handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
                self.files.append(str(path))
        except OSError as exc:  # pragma: no cover - filesystem specific
            self.log.warning(f"could not write the rejected records: {exc}", status="degraded")

    async def aclose(self) -> None:
        """Let go of everything this run held open.

        A finished conductor stays readable, so nothing else will release
        these: the engine's validators hold the uniqueness spill files, and
        one per spilled field would otherwise survive every run the server
        ever ran.
        """
        # Both closes are idempotent, and the engine shares this runtime: a
        # run that failed before building an engine still has one to release.
        if self.runtime is not None:
            await self.runtime.aclose()
        engine = getattr(self, "_engine_instance", None)
        if engine is not None:
            await engine.aclose()
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
            # The source position, from the checkpoint that recorded it -
            # but only if that checkpoint is describing this many rows. The two
            # are written together, so a disagreement means something else
            # moved the row count, and the honest fallback is the assumption
            # that predates the cursor: rows written *are* the position.
            checkpoint = row.get("checkpoint") or {}
            consistent = int(checkpoint.get("completed", -1)) == row["completed"]
            if consistent and "cursor" in checkpoint:
                planned.cursor = int(checkpoint["cursor"])
                planned.batch_start_cursor = int(checkpoint.get("batch_start_cursor", -1))
                planned.batch_start_completed = int(checkpoint.get("batch_start_completed", 0))
                planned.attempt_start_cursor = int(checkpoint.get("attempt_start_cursor", -1))
                planned.attempt_start_completed = int(checkpoint.get("attempt_start_completed", 0))
            else:
                planned.cursor = row["offset"] + row["completed"]
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
        # Every file the earlier attempts wrote, so the resumed run's summary
        # describes the whole dataset rather than the part it happened to add.
        conductor.files = list(dict.fromkeys(output for job in jobs for output in job.outputs))
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


def _size_on_disk(paths: list[str]) -> int:
    """Total bytes at these paths, counting each file once.

    A partitioned run names a directory, several entities can share one SQLite
    database, and a resumed run lists the same file twice - so this walks
    directories and de-duplicates before it adds anything up.
    """
    seen: dict[Path, int] = {}
    for name in paths:
        path = Path(name)
        try:
            if path.is_dir():
                for child in path.rglob("*"):
                    if child.is_file():
                        seen[child.resolve()] = child.stat().st_size
            elif path.is_file():
                seen[path.resolve()] = path.stat().st_size
        except OSError:  # pragma: no cover - a file removed underneath us
            continue
    return sum(seen.values())


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

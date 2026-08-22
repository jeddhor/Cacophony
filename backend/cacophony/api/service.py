"""The run service (design document sections 36 and 55).

The API needs runs it can start, watch and control after the request that
created them has returned. That is what this holds: the background tasks, the
live conductors, and the event buses feeding the WebSocket.

Runs execute in the server's event loop rather than in a worker process. For a
local-first tool that is the right trade - one process, one store, no broker,
and section 39 is explicit that Redis and Celery should be avoided until
distributed execution is actually needed (section 95). Generation yields to the
loop between batches, so the API stays responsive while a run is going.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..core.errors import CacophonyError, PathNotAllowedError, SchemaError
from ..core.files import atomic_write_text
from ..runs.config import RunConfig
from ..runs.coordinator import Conductor
from ..runs.events import EventBus, EventKind
from ..runs.state import JobState, RunState
from ..schema.compiler import compile_project
from ..schema.loader import load_project, load_project_data
from ..store.database import Database
from ..store.repository import Repository

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import AsyncIterator

    from ..schema.models import ProjectSpec

__all__ = ["RunService"]

#: How many finished runs keep their in-memory metrics. Chosen to match the
#: default `prune --keep`: the number of runs anybody looks back through.
TERMINAL_CONDUCTORS_KEPT = 50


class RunService:
    """Owns the store, the live runs, and the buses that report on them."""

    def __init__(
        self,
        database: Database | None = None,
        *,
        store_path: str | Path | None = None,
        allowed_roots: Sequence[str | Path] | None = None,
    ):
        self.database = database or Database(store_path)
        self.repository = Repository(self.database)
        #: Directories a request may name. ``None`` means anywhere, which is
        #: the right answer on loopback: the API can do what the shell that
        #: started it can do, and pretending otherwise would be theatre. The CLI
        #: requires roots for any bind that is not loopback, where "the shell
        #: that started it" is somebody else's shell.
        self.allowed_roots: list[Path] | None = (
            [Path(root).expanduser().resolve() for root in allowed_roots]
            if allowed_roots is not None
            else None
        )
        self._conductors: dict[str, Conductor] = {}
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._buses: dict[str, EventBus] = {}

    def permitted(self, path: str | Path, *, what: str = "path") -> Path:
        """Resolve a path a request named, refusing anything outside the roots.

        Resolved first, so ``..`` and a symlink both land where they really
        point rather than where they claim to.
        """
        resolved = Path(path).expanduser().resolve()
        if self.allowed_roots is None:
            return resolved
        for root in self.allowed_roots:
            if resolved == root or root in resolved.parents:
                return resolved
        roots = ", ".join(str(root) for root in self.allowed_roots)
        raise PathNotAllowedError(
            f"{what} {resolved} is outside the directories this server may use ({roots})."
        )

    # -- projects ----------------------------------------------------------- #

    def register_project(
        self, *, path: str | None = None, source: str | None = None
    ) -> dict[str, Any]:
        """Record a project from a file path or from inline schema text."""
        if not path and not source:
            raise SchemaError("Provide either 'path' or 'source'.")

        # Checked whenever a path is given, not only when it is the source of
        # the schema. Sending both used to store the path unchecked, and every
        # later read of that project went to it: inline source that names a
        # file is still a request to use that file.
        resolved = str(self.permitted(path, what="project")) if path else None

        if source is not None:
            project = self._parse_source(source)
            text = source
        else:
            assert resolved is not None
            project = load_project(resolved)
            text = Path(resolved).read_text(encoding="utf-8")

        project_id, revision_id = self.repository.upsert_project(
            project, path=resolved, source_text=text
        )
        record = self.repository.get_project(project_id)
        assert record is not None
        record["revision_id"] = revision_id
        return record

    def _parse_source(self, source: str) -> ProjectSpec:
        import json

        import yaml

        try:
            data = json.loads(source)
        except json.JSONDecodeError:
            try:
                data = yaml.safe_load(source)
            except yaml.YAMLError as exc:
                raise SchemaError(f"schema source is neither valid JSON nor YAML: {exc}") from exc
        if not isinstance(data, dict):
            raise SchemaError("schema source must be a mapping at the top level")
        return load_project_data(data, source="<api>")

    def _readable_path(self, record: dict[str, Any]) -> str | None:
        """A stored project file this server may read, or nothing.

        Re-checked rather than trusted, exactly as writing one is: a row
        recorded before the server was confined - or through a hole in how it
        was recorded - must not become a way to read outside the roots.
        """
        path = str(record.get("path") or "")
        if not path or not Path(path).exists():
            return None
        return str(self.permitted(path, what="project"))

    def load_for_run(self, project_id: int) -> tuple[Any, int | None, str]:
        """Compile a project, preferring a changed file over its last revision.

        Section 73 wants a run to record the exact schema revision it used, so
        if the file on disk has moved on, that becomes a new revision *before*
        the run starts rather than being silently ignored.
        """
        record = self.repository.get_project(project_id)
        if record is None:
            raise SchemaError(f"No project with id {project_id}.")

        path = self._readable_path(record)
        if path:
            project = load_project(path)
            text = Path(path).read_text(encoding="utf-8")
            _, revision_id = self.repository.upsert_project(project, path=path, source_text=text)
            return compile_project(project), revision_id, record["name"]

        revisions = record.get("revisions") or []
        if not revisions:
            raise SchemaError(f"Project {project_id} has no stored schema and no readable file.")
        latest = self.repository.get_revision(revisions[-1]["id"], include_source=True)
        assert latest is not None
        project = self._parse_source(latest["source_text"])
        return compile_project(project), latest["id"], record["name"]

    # -- schema editing (section 48) ---------------------------------------- #

    def schema_source(self, project_id: int) -> tuple[str, str]:
        """The schema text exactly as it stands, and its format."""
        record = self.repository.get_project(project_id)
        if record is None:
            raise SchemaError(f"No project with id {project_id}.")

        path = self._readable_path(record)
        if path:
            fmt = "json" if Path(path).suffix.lower() == ".json" else "yaml"
            return Path(path).read_text(encoding="utf-8"), fmt

        revisions = record.get("revisions") or []
        if not revisions:
            raise SchemaError(f"Project {project_id} has no stored schema.")
        latest = self.repository.get_revision(revisions[-1]["id"], include_source=True)
        assert latest is not None
        return latest["source_text"], latest["source_format"]

    def schema_is_editable(self, project_id: int) -> bool:
        """Whether edits can be written back.

        A project registered from inline source has nowhere to save to, so the
        Studio shows it read-only rather than accepting edits it will lose.
        """
        record = self.repository.get_project(project_id)
        if record is None:
            return False
        try:
            return self._readable_path(record) is not None
        except PathNotAllowedError:
            # Outside the roots: readable by this process, but not through this
            # server, so it is not editable through it either.
            return False

    def patch_schema(self, project_id: int, operations: list[dict[str, Any]]) -> dict[str, Any]:
        from ..schema.editor import apply_patch

        source, _fmt = self.schema_source(project_id)
        result = apply_patch(source, operations)
        return {
            **self._save_schema(project_id, result.source),
            "applied": result.applied,
            "changed": result.changed,
        }

    def write_schema(self, project_id: int, source: str) -> dict[str, Any]:
        """Replace a schema outright, refusing anything that will not compile."""
        project = self._parse_source(source)
        compile_project(project)
        return self._save_schema(project_id, source)

    def _save_schema(self, project_id: int, source: str) -> dict[str, Any]:
        record = self.repository.get_project(project_id)
        if record is None:
            raise SchemaError(f"No project with id {project_id}.")
        path = str(record.get("path") or "")
        if not path:
            raise SchemaError(
                "This project was registered from inline source and has no file to "
                "write to. Register it from a path to make it editable."
            )

        project = self._parse_source(source)
        # Re-checked rather than trusted: a project registered before the server
        # was confined would otherwise still be writable through it.
        atomic_write_text(self.permitted(path, what="project"), source)
        _, revision_id = self.repository.upsert_project(project, path=path, source_text=source)
        return {"project_id": project_id, "revision_id": revision_id, "source": source}

    # -- runs --------------------------------------------------------------- #

    async def start_run(self, project_id: int, config: RunConfig) -> dict[str, Any]:
        """Plan a run, register it, and execute it in the background."""
        # Where a run writes is chosen by the caller, so it is checked like any
        # other path a caller names. Without this, a request could write
        # generated files - whose names and contents a schema decides - into any
        # directory the server can reach.
        config.output_dir = self.permitted(config.output_dir, what="output directory")
        if config.assets_dir is not None:
            config.assets_dir = self.permitted(config.assets_dir, what="assets directory")
        if config.cache_path is not None:
            # The provider cache is a file the run creates, directories and
            # all, at a path the caller named. Every other such path is checked
            # and this one was not.
            config.cache_path = self.permitted(config.cache_path, what="cache")

        compiled, revision_id, _name = self.load_for_run(project_id)

        bus = EventBus()
        conductor = Conductor(
            compiled,
            config,
            repository=self.repository,
            project_id=project_id,
            revision_id=revision_id,
            bus=bus,
        )
        conductor.plan()

        self._conductors[conductor.run_id] = conductor
        self._buses[conductor.run_id] = bus
        task = asyncio.create_task(
            self._execute(conductor), name=f"cacophony-run-{conductor.run_id}"
        )
        # Retired from the callback rather than from the run's own `finally`,
        # where the task it is asking about is not done yet.
        task.add_done_callback(lambda _: self._retire())
        self._tasks[conductor.run_id] = task

        # Give the conductor a moment to persist its row, so the response
        # describes a run the caller can immediately fetch.
        await asyncio.sleep(0)
        stored = self.repository.get_run(conductor.run_id)
        return stored or {"id": conductor.run_id, "state": RunState.QUEUED.value}

    async def resume_run(self, run_id: str) -> dict[str, Any]:
        stored = self.repository.get_run(run_id)
        if stored is None:
            raise CacophonyError(f"No run {run_id}.")
        if not RunState(stored["state"]).is_resumable:
            raise CacophonyError(f"Run {run_id} is {stored['state']} and has nothing to resume.")
        if run_id in self._tasks and not self._tasks[run_id].done():
            raise CacophonyError(f"Run {run_id} is already executing.")

        # The revision this run recorded, not the project's head: resuming
        # against an edited schema produces one dataset generated two ways,
        # which is the thing section 73 keeps revisions for.
        from ..runs.recovery import compile_stored_revision

        compiled, complaint = compile_stored_revision(self.repository, stored)
        bus = EventBus()
        if complaint:
            bus.emit(EventKind.WARNING, run_id, message=complaint, level="warning")
        conductor = Conductor.resume(compiled, stored, repository=self.repository, bus=bus)

        self._conductors[run_id] = conductor
        self._buses[run_id] = bus
        task = asyncio.create_task(
            self._execute(conductor, resume=True), name=f"cacophony-resume-{run_id}"
        )
        task.add_done_callback(lambda _: self._retire())
        self._tasks[run_id] = task
        await asyncio.sleep(0)
        return self.repository.get_run(run_id) or stored

    async def _execute(self, conductor: Conductor, *, resume: bool = False) -> Any:
        try:
            if resume:
                return await conductor.execute_resume()
            return await conductor.execute()
        finally:
            await conductor.aclose()

    def _retire(self) -> None:
        """Forget the oldest finished runs, keeping the recent ones readable.

        A finished conductor is kept so its metrics stay live-fresh, but kept
        *forever* it is a server that grows by one compiled project and one
        engine per run it has ever executed. Past the limit the store is the
        record, which is where a finished run's numbers live anyway.
        """
        finished = [run_id for run_id in self._conductors if not self.is_active(run_id)]
        for run_id in finished[: max(0, len(finished) - TERMINAL_CONDUCTORS_KEPT)]:
            self._conductors.pop(run_id, None)
            self._buses.pop(run_id, None)
            self._tasks.pop(run_id, None)

    # -- control ------------------------------------------------------------ #

    def conductor(self, run_id: str) -> Conductor | None:
        return self._conductors.get(run_id)

    def _live(self, run_id: str) -> Conductor | None:
        """The conductor for a run that is still executing, if there is one.

        A finished conductor stays in the registry so its metrics remain
        readable, so "is there a conductor?" is not the same question as "can
        this be controlled?". Pausing a completed run would otherwise answer
        200 and write ``paused`` over a perfectly good ``completed``.
        """
        conductor = self._conductors.get(run_id)
        if conductor is None or not self.is_active(run_id):
            return None
        return conductor if not conductor.state.is_terminal else None

    def pause(self, run_id: str) -> bool:
        conductor = self._live(run_id)
        if conductor is None:
            return False
        conductor.handle.pause()
        self.repository.update_run(run_id, state=RunState.PAUSED.value)
        # The state of the jobs, not only of the run: a paused run whose jobs
        # all say `running` is a run inspector describing something that is not
        # happening. And the event is emitted so it reaches the log the manual
        # says holds it (section 56).
        self._hold_jobs(run_id, JobState.PAUSED)
        conductor.bus.emit(EventKind.RUN_PAUSED, run_id, message="paused", data={"source": "api"})
        return True

    def resume_paused(self, run_id: str) -> bool:
        """Release a run paused in memory. A stopped run needs ``resume_run``."""
        conductor = self._live(run_id)
        if conductor is None or not conductor.handle.is_paused:
            return False
        conductor.handle.resume()
        self.repository.update_run(run_id, state=RunState.RUNNING.value)
        self._hold_jobs(run_id, JobState.RUNNING)
        conductor.bus.emit(EventKind.RUN_RESUMED, run_id, message="resumed", data={"source": "api"})
        return True

    def _hold_jobs(self, run_id: str, state: JobState) -> None:
        """Move this run's in-flight jobs into ``state``."""
        moving = {JobState.RUNNING, JobState.PAUSED}
        for row in self.repository.get_jobs(run_id):
            if JobState(row["state"]) in moving and JobState(row["state"]) is not state:
                self.repository.update_job(row["id"], state=state.value)

    def cancel(self, run_id: str) -> bool:
        conductor = self._live(run_id)
        if conductor is None:
            return False
        conductor.handle.cancel()
        return True

    def is_active(self, run_id: str) -> bool:
        """Whether this run is still executing *and* has not reached an end.

        Both halves matter. A task that has not finished can still belong to a
        run whose state is already `completed` - the conductor is closing its
        writers - and during that window the run's own record is the truth. The
        live metrics measure elapsed time from a clock, so attaching them there
        makes a finished run tick.
        """
        task = self._tasks.get(run_id)
        if task is None or task.done():
            return False
        conductor = self._conductors.get(run_id)
        return conductor is None or not conductor.state.is_terminal

    async def wait(self, run_id: str, timeout: float | None = None) -> Any:
        """Await a run's completion. Chiefly for tests and for ``serve --once``."""
        task = self._tasks.get(run_id)
        if task is None:
            return None
        return await asyncio.wait_for(asyncio.shield(task), timeout=timeout)

    # -- live feed ---------------------------------------------------------- #

    def bus(self, run_id: str) -> EventBus | None:
        return self._buses.get(run_id)

    async def stream(self, run_id: str) -> AsyncIterator[dict[str, Any]]:
        """Replay recent events, then follow the run live (section 55)."""
        bus = self._buses.get(run_id)
        if bus is None:
            return
        for event in bus.replay():
            yield event.to_dict()
        async for event in bus.subscribe():
            yield event.to_dict()

    def active_runs(self) -> list[str]:
        return [run_id for run_id, task in self._tasks.items() if not task.done()]

    # -- lifecycle ---------------------------------------------------------- #

    async def shutdown(self) -> None:
        """Pause every live run, then let its task unwind.

        Cancelling rather than abandoning matters: a cancelled job checkpoints
        on the way out, so a server stopped mid-run leaves something that
        ``resume`` can pick up.
        """
        for conductor in self._conductors.values():
            conductor.handle.cancel()
        tasks = [task for task in self._tasks.values() if not task.done()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.database.close()

    def describe(self) -> dict[str, Any]:
        return {
            "store": self.database.describe(),
            "active_runs": self.active_runs(),
            **self.repository.stats(),
        }

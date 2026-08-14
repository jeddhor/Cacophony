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
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..core.errors import CacophonyError, SchemaError
from ..runs.config import RunConfig
from ..runs.coordinator import Conductor
from ..runs.events import EventBus
from ..runs.state import RunState
from ..schema.compiler import compile_project
from ..schema.loader import load_project, load_project_data
from ..store.database import Database
from ..store.repository import Repository

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import AsyncIterator

    from ..schema.models import ProjectSpec

__all__ = ["RunService"]


class RunService:
    """Owns the store, the live runs, and the buses that report on them."""

    def __init__(self, database: Database | None = None, *, store_path: str | Path | None = None):
        self.database = database or Database(store_path)
        self.repository = Repository(self.database)
        self._conductors: dict[str, Conductor] = {}
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._buses: dict[str, EventBus] = {}

    # -- projects ----------------------------------------------------------- #

    def register_project(
        self, *, path: str | None = None, source: str | None = None
    ) -> dict[str, Any]:
        """Record a project from a file path or from inline schema text."""
        if not path and not source:
            raise SchemaError("Provide either 'path' or 'source'.")

        if source is not None:
            project = self._parse_source(source)
            text = source
            resolved = path
        else:
            resolved = str(Path(path).resolve())  # type: ignore[arg-type]
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

    def load_for_run(self, project_id: int) -> tuple[Any, int | None, str]:
        """Compile a project, preferring a changed file over its last revision.

        Section 73 wants a run to record the exact schema revision it used, so
        if the file on disk has moved on, that becomes a new revision *before*
        the run starts rather than being silently ignored.
        """
        record = self.repository.get_project(project_id)
        if record is None:
            raise SchemaError(f"No project with id {project_id}.")

        path = record.get("path")
        if path and Path(path).exists():
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

    # -- runs --------------------------------------------------------------- #

    async def start_run(self, project_id: int, config: RunConfig) -> dict[str, Any]:
        """Plan a run, register it, and execute it in the background."""
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
        self._tasks[conductor.run_id] = asyncio.create_task(
            self._execute(conductor), name=f"cacophony-run-{conductor.run_id}"
        )

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

        compiled, _revision_id, _name = self.load_for_run(stored["project_id"])
        bus = EventBus()
        conductor = Conductor.resume(compiled, stored, repository=self.repository, bus=bus)

        self._conductors[run_id] = conductor
        self._buses[run_id] = bus
        self._tasks[run_id] = asyncio.create_task(
            self._execute(conductor, resume=True), name=f"cacophony-resume-{run_id}"
        )
        await asyncio.sleep(0)
        return self.repository.get_run(run_id) or stored

    async def _execute(self, conductor: Conductor, *, resume: bool = False) -> Any:
        try:
            if resume:
                return await conductor.execute_resume()
            return await conductor.execute()
        finally:
            await conductor.aclose()

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
        return True

    def resume_paused(self, run_id: str) -> bool:
        """Release a run paused in memory. A stopped run needs ``resume_run``."""
        conductor = self._live(run_id)
        if conductor is None or not conductor.handle.is_paused:
            return False
        conductor.handle.resume()
        self.repository.update_run(run_id, state=RunState.RUNNING.value)
        return True

    def cancel(self, run_id: str) -> bool:
        conductor = self._live(run_id)
        if conductor is None:
            return False
        conductor.handle.cancel()
        return True

    def is_active(self, run_id: str) -> bool:
        task = self._tasks.get(run_id)
        return task is not None and not task.done()

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

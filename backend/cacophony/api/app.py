"""The Cacophony REST API (design document sections 36 and 55).

Section 36's routes, plus the WebSocket feed section 55 needs to make a run
look like something while it happens::

    GET    /api/projects              POST   /api/projects
    GET    /api/projects/{id}         POST   /api/projects/{id}/runs
    POST   /api/projects/{id}/preview

    GET    /api/runs                  GET    /api/runs/{id}
    POST   /api/runs/{id}/pause       POST   /api/runs/{id}/resume
    POST   /api/runs/{id}/cancel      GET    /api/runs/{id}/events
    WS     /api/runs/{id}/stream

    GET    /api/providers             GET    /api/providers/{id}/models
    POST   /api/providers/{id}/test

    GET    /api/generators            GET    /api/system

Errors are translated once, here, so a schema mistake is a 400 with the
compiler's message rather than a 500 with a traceback.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

# FastAPI is imported at module scope on purpose. With postponed annotation
# evaluation - which this file uses - FastAPI resolves a route's annotations
# with ``get_type_hints`` against the *module* namespace. A ``WebSocket``
# imported inside the factory is invisible there, and FastAPI silently decides
# the socket must be a query parameter, then rejects every connection with
# 403. ``cacophony.api.__init__`` is what keeps FastAPI optional for CLI users.
from fastapi import Body, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from .. import __version__
from ..core.errors import (
    CacophonyError,
    ProviderNotFoundError,
    ProviderUnavailableError,
    SchemaError,
)
from ..runs.state import RunState
from .schemas import CreateProjectRequest, CreateRunRequest, PreviewRequest
from .service import RunService

__all__ = ["create_app"]

#: Events after which a run has nothing further to report.
FINAL_EVENT_KINDS = frozenset({"run.completed", "run.failed", "run.cancelled"})


def create_app(
    *,
    store_path: str | Path | None = None,
    service: RunService | None = None,
) -> FastAPI:
    """Build the FastAPI application."""
    runs = service or RunService(store_path=store_path)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> Any:
        yield
        await runs.shutdown()

    app = FastAPI(
        title="Cacophony",
        version=__version__,
        summary="A synthetic reality compiler.",
        lifespan=lifespan,
    )
    app.state.runs = runs

    # -- error translation -------------------------------------------------- #

    @app.exception_handler(SchemaError)
    async def _schema_error(_request: Any, exc: SchemaError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"error": "schema", "detail": str(exc)})

    @app.exception_handler(ProviderUnavailableError)
    async def _provider_down(_request: Any, exc: ProviderUnavailableError) -> JSONResponse:
        return JSONResponse(
            status_code=503, content={"error": "provider_unavailable", "detail": str(exc)}
        )

    @app.exception_handler(ProviderNotFoundError)
    async def _provider_missing(_request: Any, exc: ProviderNotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"error": "provider", "detail": str(exc)})

    @app.exception_handler(CacophonyError)
    async def _cacophony_error(_request: Any, exc: CacophonyError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"error": "cacophony", "detail": str(exc)})

    def _found(value: Any, what: str) -> Any:
        if value is None:
            raise HTTPException(status_code=404, detail=f"no such {what}")
        return value

    # -- system ------------------------------------------------------------- #

    @app.get("/api/system", tags=["system"])
    async def system() -> dict[str, Any]:
        return {"version": __version__, **runs.describe()}

    @app.get("/api/generators", tags=["system"])
    async def generators() -> list[dict[str, Any]]:
        from ..generation.registry import REGISTRY

        return REGISTRY.describe()

    # -- projects ----------------------------------------------------------- #

    @app.get("/api/projects", tags=["projects"])
    async def list_projects() -> list[dict[str, Any]]:
        return runs.repository.list_projects()

    @app.post("/api/projects", status_code=201, tags=["projects"])
    async def create_project(body: CreateProjectRequest) -> dict[str, Any]:
        return runs.register_project(path=body.path, source=body.source)

    @app.get("/api/projects/{project_id}", tags=["projects"])
    async def get_project(project_id: int) -> dict[str, Any]:
        return _found(runs.repository.get_project(project_id), "project")

    @app.get("/api/projects/{project_id}/plan", tags=["projects"])
    async def get_plan(project_id: int) -> dict[str, Any]:
        """The compiled generation plan (design document section 28)."""
        compiled, revision_id, name = runs.load_for_run(project_id)
        assert compiled.plan is not None
        return {"project": name, "revision_id": revision_id, **compiled.plan.to_dict()}

    @app.get("/api/projects/{project_id}/lint", tags=["projects"])
    async def lint_project_route(project_id: int) -> dict[str, Any]:
        """Section 102's warnings, so the UI can show them before a run."""
        from ..schema.linter import lint_project

        compiled, _revision, _name = runs.load_for_run(project_id)
        report = lint_project(compiled)
        return {
            "ok": report.ok,
            "issues": [
                {
                    "code": issue.code,
                    "severity": issue.severity.value,
                    "location": issue.location,
                    "message": issue.message,
                    "hint": issue.hint,
                }
                for issue in report.issues
            ],
        }

    @app.post("/api/projects/{project_id}/preview", tags=["projects"])
    async def preview(project_id: int, body: PreviewRequest) -> dict[str, Any]:
        """Sample records without starting a run (design document section 51)."""
        import time

        from ..generation.engine import GenerationEngine
        from ..generation.runtime import GenerationRuntime

        compiled, _revision, _name = runs.load_for_run(project_id)
        if body.seed is not None:
            compiled.spec.project.seed = body.seed

        entity = body.entity or (compiled.entity_order[0] if compiled.entity_order else None)
        if entity is None or entity not in compiled.entities:
            raise HTTPException(status_code=404, detail=f"no entity '{entity}'")

        runtime = GenerationRuntime.for_project(compiled.spec) if compiled.spec.providers else None
        engine = GenerationEngine(
            compiled,
            runtime=runtime,
            seed_namespace=f"preview-{time.time_ns()}" if body.isolate else None,
        )
        records = await engine.generate_batch(entity, body.count, offset=body.offset)
        if runtime is not None:
            await runtime.aclose()

        compiled_entity = compiled.entity(entity)
        return {
            "entity": entity,
            "columns": compiled_entity.spec.field_names(),
            # Section 51: the preview identifies each column's source.
            "sources": {field.name: field.generator_name for field in compiled_entity.fields},
            "records": [record.to_dict(jsonable=True) for record in records],
        }

    @app.post("/api/projects/{project_id}/runs", status_code=202, tags=["runs"])
    async def create_run(project_id: int, body: CreateRunRequest) -> dict[str, Any]:
        _found(runs.repository.get_project(project_id), "project")
        return await runs.start_run(project_id, body.to_config())

    # -- runs --------------------------------------------------------------- #

    @app.get("/api/runs", tags=["runs"])
    async def list_runs(
        project_id: int | None = Query(default=None),
        state: str | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=500),
    ) -> list[dict[str, Any]]:
        return runs.repository.list_runs(project_id=project_id, state=state, limit=limit)

    @app.get("/api/runs/{run_id}", tags=["runs"])
    async def get_run(run_id: str) -> dict[str, Any]:
        stored = _found(runs.repository.get_run(run_id), "run")
        conductor = runs.conductor(run_id)
        if conductor is not None:
            # A live run's in-memory metrics are fresher than its last
            # checkpoint, which is what a progress bar wants.
            stored["live"] = conductor.metrics.snapshot()
            stored["paused"] = conductor.handle.is_paused
        return stored

    @app.post("/api/runs/{run_id}/pause", tags=["runs"])
    async def pause_run(run_id: str) -> dict[str, Any]:
        _found(runs.repository.get_run(run_id, include_jobs=False), "run")
        if not runs.pause(run_id):
            raise HTTPException(status_code=409, detail="run is not executing")
        return {"id": run_id, "state": RunState.PAUSED.value}

    @app.post("/api/runs/{run_id}/resume", tags=["runs"])
    async def resume_run(run_id: str) -> dict[str, Any]:
        stored = _found(runs.repository.get_run(run_id, include_jobs=False), "run")
        # A run paused in this process just needs releasing; one that stopped
        # has to be rebuilt from its checkpoints.
        if runs.resume_paused(run_id):
            return {"id": run_id, "state": RunState.RUNNING.value, "mode": "unpaused"}
        if not RunState(stored["state"]).is_resumable:
            raise HTTPException(
                status_code=409, detail=f"run is {stored['state']} and has nothing to resume"
            )
        result = await runs.resume_run(run_id)
        return {**result, "mode": "restarted"}

    @app.post("/api/runs/{run_id}/cancel", tags=["runs"])
    async def cancel_run(run_id: str) -> dict[str, Any]:
        _found(runs.repository.get_run(run_id, include_jobs=False), "run")
        if not runs.cancel(run_id):
            raise HTTPException(status_code=409, detail="run is not executing")
        return {"id": run_id, "state": RunState.CANCELLED.value}

    @app.delete("/api/runs/{run_id}", status_code=204, tags=["runs"])
    async def delete_run(run_id: str) -> None:
        if runs.is_active(run_id):
            raise HTTPException(status_code=409, detail="cancel the run before deleting it")
        if not runs.repository.delete_run(run_id):
            raise HTTPException(status_code=404, detail="no such run")

    @app.get("/api/runs/{run_id}/events", tags=["runs"])
    async def run_events(
        run_id: str,
        after: int = Query(default=0, ge=0),
        level: str | None = Query(default=None),
        limit: int = Query(default=200, ge=1, le=2000),
    ) -> list[dict[str, Any]]:
        _found(runs.repository.get_run(run_id, include_jobs=False), "run")
        return runs.repository.get_events(run_id, after_id=after, level=level, limit=limit)

    @app.get("/api/runs/{run_id}/jobs", tags=["runs"])
    async def run_jobs(run_id: str) -> list[dict[str, Any]]:
        _found(runs.repository.get_run(run_id, include_jobs=False), "run")
        return runs.repository.get_jobs(run_id)

    @app.websocket("/api/runs/{run_id}/stream")
    async def run_stream(websocket: WebSocket, run_id: str) -> None:
        """Live progress (design document section 55).

        Recent events are replayed first, so a client that connects a moment
        after starting a run still sees the beginning of it. The socket closes
        once the run reaches a terminal state - there is nothing more coming,
        and holding it open would leave the client waiting on a run that has
        already finished.
        """
        await websocket.accept()
        if runs.bus(run_id) is None:
            await websocket.send_json(
                {
                    "kind": "error",
                    "run_id": run_id,
                    "message": f"run {run_id} is not executing in this process",
                }
            )
            return

        try:
            async for payload in runs.stream(run_id):
                await websocket.send_json(payload)
                if payload["kind"] in FINAL_EVENT_KINDS:
                    return
        except (WebSocketDisconnect, asyncio.CancelledError):
            return

    # -- providers ---------------------------------------------------------- #

    @app.get("/api/providers", tags=["providers"])
    async def list_providers(project_id: int | None = Query(default=None)) -> dict[str, Any]:
        from ..providers.registry import PROVIDER_REGISTRY

        payload: dict[str, Any] = {
            "adapters": PROVIDER_REGISTRY.adapters(),
            "aliases": PROVIDER_REGISTRY.adapter_aliases(),
            "configured": [],
        }
        if project_id is not None:
            compiled, _revision, _name = runs.load_for_run(project_id)
            payload["configured"] = [
                {
                    "id": spec.id,
                    "type": spec.type,
                    "adapter": spec.adapter,
                    "base_url": spec.base_url,
                    "model": spec.model,
                    "concurrency": spec.concurrency,
                    "secret_id": spec.secret,
                }
                for spec in compiled.spec.providers.values()
            ]
        return payload

    @app.get("/api/providers/{provider_id}/models", tags=["providers"])
    async def provider_models(
        provider_id: str, project_id: int = Query(...)
    ) -> list[dict[str, Any]]:
        provider = await _provider(project_id, provider_id)
        lister = getattr(provider, "list_models_async", None)
        models = await lister() if lister else provider.list_models()
        return [
            {
                "name": info.name,
                "family": info.family,
                "parameter_size": info.parameter_size,
                "quantization": info.quantization,
                "context_length": info.context_length,
            }
            for info in models
        ]

    @app.post("/api/providers/{provider_id}/test", tags=["providers"])
    async def provider_test(provider_id: str, project_id: int = Query(...)) -> dict[str, Any]:
        provider = await _provider(project_id, provider_id)
        status = await provider.health_check()
        return {
            "id": provider_id,
            "healthy": status.healthy,
            "message": status.message,
            "latency_ms": status.latency_ms,
            "details": status.details,
        }

    async def _provider(project_id: int, provider_id: str) -> Any:
        from ..generation.runtime import GenerationRuntime

        compiled, _revision, _name = runs.load_for_run(project_id)
        if provider_id not in compiled.spec.providers:
            raise HTTPException(status_code=404, detail=f"no provider '{provider_id}'")
        runtime = GenerationRuntime.for_project(compiled.spec)
        reason = runtime.is_unavailable(provider_id)
        if reason is not None:
            raise ProviderNotFoundError(reason)
        return runtime.providers.get(provider_id)

    @app.post("/api/system/prune", tags=["system"])
    async def prune(keep: int = Body(default=50, embed=True)) -> dict[str, int]:
        return {"deleted": runs.repository.prune_runs(keep=keep)}

    return app

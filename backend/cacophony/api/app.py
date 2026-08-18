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

    POST   /api/projects/{id}/streams GET    /api/streams
    GET    /api/streams/{id}          DELETE /api/streams/{id}
    GET    /api/streams/{id}/records  POST   /api/streams/{id}/retarget
    POST   /api/streams/{id}/pause    POST   /api/streams/{id}/resume
    POST   /api/streams/{id}/stop     WS     /api/streams/{id}/feed

    GET    /api/providers             GET    /api/providers/{id}/models
    POST   /api/providers/{id}/test

    GET    /api/generators            GET    /api/system
    GET    /api/plugins

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
from .schemas import (
    CreateProjectRequest,
    CreateRunRequest,
    CreateStreamRequest,
    PatchSchemaRequest,
    PreviewRequest,
    RetargetRequest,
    WriteSchemaRequest,
)
from .service import RunService
from .streams import StreamService

__all__ = ["create_app"]

#: Events after which a run has nothing further to report.
FINAL_EVENT_KINDS = frozenset({"run.completed", "run.failed", "run.cancelled"})


def _jsonable(value: Any) -> Any:
    """Options may hold anything a generator was configured with."""
    from ..core.record import to_jsonable

    return to_jsonable(value)


def _distribution_of(field: Any) -> dict[str, float] | None:
    """Normalised weights for a categorical field (design document section 52)."""
    reporter = getattr(field.generator, "distribution", None)
    if reporter is None:
        return None
    try:
        return {str(key): round(value, 6) for key, value in reporter().items()}
    except Exception:
        return None


def _reference_of(compiled: Any, field: Any) -> dict[str, Any] | None:
    """Where a field points, if it points anywhere (design document section 15)."""
    target = getattr(field.generator, "target", None)
    if not isinstance(target, str):
        return None

    key = getattr(field.generator, "target_field", None)
    if not key:
        entity = compiled.entities.get(target)
        key = entity.spec.resolved_primary_key() if entity is not None else None

    return {
        "entity": target,
        "field": key,
        "distribution": getattr(field.generator, "distribution", None),
        "unique": bool(getattr(field.generator, "unique", False)),
    }


def _reference_edges(compiled: Any) -> list[dict[str, Any]]:
    """The project's foreign keys, as graph edges."""
    edges: list[dict[str, Any]] = []
    for entity in compiled.ordered_entities():
        for field in entity.fields:
            reference = _reference_of(compiled, field)
            if reference is not None:
                edges.append(
                    {
                        "from_entity": entity.name,
                        "from_field": field.name,
                        "to_entity": reference["entity"],
                        "to_field": reference["field"],
                        "distribution": reference["distribution"],
                        "unique": reference["unique"],
                    }
                )
    return edges


def _asset_root(stored: dict[str, Any]) -> Path | None:
    """Where a stored run put its media.

    Read back from the run's own configuration rather than recomputed, so a run
    that used ``--assets-dir`` is still findable afterwards.
    """
    config = stored.get("config") or {}
    if config.get("assets_dir"):
        return Path(str(config["assets_dir"]))
    output_dir = config.get("output_dir") or stored.get("output_dir")
    return Path(str(output_dir)) / "assets" if output_dir else None


class _TokenGate:
    """Require a per-launch token on every API call (design document section 41).

    Pure ASGI rather than ``@app.middleware("http")``, and that is the point.
    Starlette's HTTP middleware only sees ``scope["type"] == "http"``, so a
    WebSocket handshake passes straight through it - which left
    ``/api/runs/{id}/stream`` and ``/api/streams/{id}/feed`` reachable without a
    token while every other route was guarded. Found by testing the socket
    rather than assuming it behaved like the rest; a gap in a security control
    is worse than no control, because the control is what stops anyone looking.

    The Studio itself is served unauthenticated: static files carrying no data,
    and the window has to load before it can present anything.
    """

    def __init__(self, app: Any, token: str) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        guarded = scope["type"] in ("http", "websocket") and str(scope.get("path", "")).startswith(
            "/api"
        )
        if not guarded or self._authorised(scope):
            await self.app(scope, receive, send)
            return

        if scope["type"] == "websocket":
            # 1008 is "policy violation" - something a client can report, rather
            # than a silent disconnect it would retry forever.
            await send({"type": "websocket.close", "code": 1008})
            return

        response = JSONResponse(
            status_code=401,
            content={"error": "unauthorised", "detail": "a valid token is required"},
        )
        await response(scope, receive, send)

    def _authorised(self, scope: Any) -> bool:
        expected = f"Bearer {self.token}".encode()
        for key, value in scope.get("headers") or []:
            if key == b"authorization" and value == expected:
                return True
        # A WebSocket handshake cannot carry an Authorization header, so the
        # query string is accepted too - for both kinds, since a page that must
        # use it for sockets may as well use it consistently.
        from urllib.parse import parse_qs

        presented = parse_qs(scope.get("query_string", b"").decode("latin-1")).get("token", [])
        return bool(presented) and presented[0] == self.token


def create_app(
    *,
    store_path: str | Path | None = None,
    service: RunService | None = None,
    static_dir: str | Path | None = None,
    token: str | None = None,
) -> FastAPI:
    """Build the FastAPI application.

    ``static_dir`` mounts a built Studio at the root, so one process serves
    both the API and the UI. In development the Vite server proxies to this
    instead, and nothing is mounted.

    ``token`` requires every API call to present it, and is what the desktop
    shell uses (section 41). A local HTTP server is reachable by every process
    on the machine: opening a browser tab is an explicit act by a person, while
    a desktop window is not, and one that quietly exposed an unauthenticated
    generation API to everything else on a shared machine would be a surprise
    nobody asked for. ``cacophony serve`` passes nothing, so the served
    behaviour is unchanged.
    """
    runs = service or RunService(store_path=store_path)
    streams = StreamService()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> Any:
        yield
        # Streams first: a stream stops in milliseconds, and stopping them
        # before the runs means a server going away stops sending somebody's
        # syslog collector traffic it will never explain.
        await streams.shutdown()
        await runs.shutdown()

    app = FastAPI(
        title="Cacophony",
        version=__version__,
        summary="A synthetic reality compiler.",
        lifespan=lifespan,
    )
    app.state.runs = runs
    app.state.streams = streams
    app.state.token = token

    if token:
        app.add_middleware(_TokenGate, token=token)

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
        return {"version": __version__, **runs.describe(), **streams.describe()}

    @app.get("/api/plugins", tags=["system"])
    async def plugins() -> dict[str, Any]:
        """Installed plugins and what they contribute (section 44).

        Loaded rather than cached, so the page reflects what a `pip install`
        just did without restarting the server.
        """
        from ..plugins import CATEGORIES, load_plugins

        registry = load_plugins(force=True)
        return {"categories": sorted(CATEGORIES), **registry.describe()}

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

        from ..generation.engine import FailurePolicy, GenerationEngine
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
            # As for the CLI preview: an invalid sample record is a finding to
            # display, not a 500.
            validation_policy=FailurePolicy.REPORT,
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

    # -- the Schema Studio (section 48) ------------------------------------- #

    @app.get("/api/projects/{project_id}/schema", tags=["studio"])
    async def get_schema(project_id: int) -> dict[str, Any]:
        """The compiled schema, as the Studio needs to render it.

        Both the source text and the compiled shape: the source so an editor
        can show exactly what is on disk, the compiled shape so the Studio can
        show which generator each field resolved to and why.
        """
        compiled, revision_id, name = runs.load_for_run(project_id)
        source, source_format = runs.schema_source(project_id)
        return {
            "project_id": project_id,
            "name": name,
            "revision_id": revision_id,
            "source": source,
            "source_format": source_format,
            "editable": runs.schema_is_editable(project_id),
            "project": compiled.spec.model_dump(mode="json", by_alias=True),
            "entity_order": list(compiled.entity_order),
            "entities": {
                entity.name: {
                    "name": entity.name,
                    "count": entity.count,
                    "description": entity.spec.description,
                    "primary_key": entity.spec.resolved_primary_key(),
                    "depends_on": list(entity.depends_on),
                    "layers": entity.field_layers,
                    # Authored order is what the schema reads like; the
                    # dependency order is an implementation detail.
                    "field_order": entity.spec.field_names(),
                    "fields": {
                        field.name: {
                            "name": field.name,
                            "type": field.spec.type.value,
                            "semantic": field.spec.semantic,
                            "description": field.spec.description,
                            "generator": field.generator_name,
                            "generator_describe": field.generator.describe(),
                            "generator_options": _jsonable(field.generator.options),
                            "inferred": field.inferred_generator,
                            "requires_provider": type(field.generator).requires_provider,
                            "deterministic": field.generator.deterministic,
                            "dependencies": list(field.dependencies),
                            "related_entities": list(field.related_entities),
                            "nullable": field.spec.nullable,
                            "null_probability": field.spec.effective_null_probability,
                            "unique": field.spec.unique,
                            "primary_key": field.spec.primary_key,
                            "tone": field.spec.tone,
                            "constraints": field.spec.constraints.model_dump(
                                mode="json", exclude_none=True
                            ),
                            "distribution": _distribution_of(field),
                            "reference": _reference_of(compiled, field),
                            # Which recipe contributed this field (section 80).
                            # The Studio badges it, so nobody wonders where a
                            # field they did not write came from.
                            "recipe": field.spec.recipe,
                        }
                        for field in entity.fields
                    },
                }
                for entity in compiled.ordered_entities()
            },
            "relationships": [
                relationship.model_dump(mode="json", by_alias=True)
                for relationship in compiled.spec.relationships
            ],
            # Every foreign key in the project, as edges. The Studio's graph
            # draws relationships from this rather than inferring them from
            # `depends_on`, which knows that two entities are connected but not
            # which field connects them (section 15).
            "references": _reference_edges(compiled),
        }

    @app.patch("/api/projects/{project_id}/schema", tags=["studio"])
    async def patch_schema(project_id: int, body: PatchSchemaRequest) -> dict[str, Any]:
        """Apply targeted edits, preserving the rest of the document.

        The whole patch is verified before anything is written, so a rejected
        edit leaves the file exactly as it was.
        """
        return runs.patch_schema(project_id, [op.model_dump() for op in body.operations])

    @app.put("/api/projects/{project_id}/schema", tags=["studio"])
    async def put_schema(project_id: int, body: WriteSchemaRequest) -> dict[str, Any]:
        """Replace the whole schema, for the source editor."""
        return runs.write_schema(project_id, body.source)

    @app.get("/api/schema/operations", tags=["studio"])
    async def schema_operations() -> list[dict[str, Any]]:
        from ..schema.editor import describe_operations

        return describe_operations()

    @app.get("/api/schema/types", tags=["studio"])
    async def schema_types() -> dict[str, Any]:
        """Everything the field editor needs to populate its controls."""
        from ..core.types import DataType
        from ..generation.registry import REGISTRY

        return {
            "types": [
                {
                    "value": data_type.value,
                    "numeric": data_type.is_numeric,
                    "textual": data_type.is_textual,
                    "temporal": data_type.is_temporal,
                    "media": data_type.is_media,
                }
                for data_type in DataType
            ],
            "generators": REGISTRY.describe(),
            "provenance": ["none", "run", "record", "field", "full"],
            "profiles": ["quick_mock", "balanced", "high_realism", "maximum_chaos"],
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

    @app.get("/api/runs/{run_id}/quality", tags=["runs"])
    async def run_quality(run_id: str) -> dict[str, Any]:
        """The quality report for one run (design document section 58).

        A live run reports what it has measured so far rather than nothing:
        referential integrity at four million records is already the answer,
        and waiting for the other six tells nobody anything new.
        """
        stored = _found(runs.repository.get_run(run_id, include_jobs=False), "run")
        conductor = runs.conductor(run_id)
        summary = conductor.summary() if conductor is not None else (stored.get("summary") or {})

        return {
            "run_id": run_id,
            "state": stored["state"],
            "live": conductor is not None,
            "records": summary.get("total_written", stored.get("records_written", 0)),
            "quality": summary.get("quality") or {},
            "validation": summary.get("validation") or {},
            "relations": summary.get("relations"),
            "providers": summary.get("providers"),
            # What the model repeated (section 59). Part of the quality report
            # rather than a route of its own: "how much of this is the same
            # thing twice" is the same question as "is this any good".
            "duplication": summary.get("duplication") or {},
        }

    @app.get("/api/runs/{run_id}/assets", tags=["assets"])
    async def run_assets(
        run_id: str,
        entity: str | None = Query(default=None),
        kind: str | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        """What this run produced besides records (design document section 81).

        Read from the manifest beside the assets rather than from the metadata
        store, because that is where the truth is: section 42 keeps generated
        data out of the database, and an asset is generated data.
        """
        from ..assets.store import AssetStore

        stored = _found(runs.repository.get_run(run_id), "run")
        root = _asset_root(stored)
        if root is None or not root.exists():
            return {"run_id": run_id, "root": None, "total": 0, "assets": []}

        rows = [
            row
            for row in AssetStore(root).manifest()
            if (entity is None or row["entity"] == entity) and (kind is None or row["kind"] == kind)
        ]
        window = rows[offset : offset + limit]
        return {
            "run_id": run_id,
            "root": str(root),
            "total": len(rows),
            "offset": offset,
            "kinds": sorted({row["kind"] for row in rows}),
            "entities": sorted({row["entity"] for row in rows}),
            "assets": [
                {**row, "url": f"/api/runs/{run_id}/assets/file?path={row['path']}"}
                for row in window
            ],
        }

    @app.get("/api/runs/{run_id}/assets/file", tags=["assets"])
    async def run_asset_file(run_id: str, path: str = Query(...)) -> Any:
        """Serve one generated file, so the Studio can show it.

        The path is checked to be inside this run's asset directory before
        anything is opened. A parameter that names a file is a directory
        traversal waiting to happen, and "it is only a local tool" is how local
        tools become the way in.
        """
        from fastapi.responses import FileResponse

        stored = _found(runs.repository.get_run(run_id), "run")
        root = _asset_root(stored)
        if root is None:
            raise HTTPException(status_code=404, detail="this run produced no assets")

        try:
            resolved = Path(path).resolve()
            resolved.relative_to(root.resolve())
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=403, detail="that path is outside the run") from exc

        if not resolved.is_file():
            raise HTTPException(status_code=404, detail="no such asset")
        return FileResponse(resolved)

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

    # -- live streams ------------------------------------------------------- #

    @app.post("/api/projects/{project_id}/streams", status_code=201, tags=["streams"])
    async def start_stream(project_id: int, body: CreateStreamRequest) -> dict[str, Any]:
        """Start a workload generator (design document sections 35, 94).

        The response is the stream's first status, so a page can render the
        dashboard from the reply that started it rather than polling to find
        out what it just created.
        """
        compiled, _revision, name = runs.load_for_run(project_id)
        if body.seed is not None:
            compiled.spec.project.seed = body.seed
        return streams.start(
            compiled,
            project_id=project_id,
            project_name=name,
            rates=dict(body.rates),
            destinations=body.destinations,
            keep_records=body.keep_records,
            **body.to_options(),
        )

    @app.get("/api/streams", tags=["streams"])
    async def list_streams(project_id: int | None = Query(default=None)) -> list[dict[str, Any]]:
        return streams.listing(project_id=project_id)

    @app.get("/api/streams/{stream_id}", tags=["streams"])
    async def get_stream(stream_id: str) -> dict[str, Any]:
        return _found(streams.get(stream_id), "stream").describe()

    @app.get("/api/streams/{stream_id}/records", tags=["streams"])
    async def stream_records(
        stream_id: str,
        limit: int = Query(default=50, ge=1, le=1000),
        entity: str | None = Query(default=None),
    ) -> dict[str, Any]:
        """The last records the stream produced, newest first.

        A window, not a log. The stream keeps a bounded number of records so a
        browser can see what is going past; everything older has already been
        delivered and forgotten, which is what makes a six-hour stream cost the
        same as a six-second one.
        """
        _found(streams.get(stream_id), "stream")
        rows = streams.records(stream_id, limit=limit, entity=entity)
        sink = streams.require(stream_id).memory
        return {
            "stream_id": stream_id,
            "sampled": sink is not None,
            "keep": sink.keep if sink else 0,
            "records": rows,
        }

    @app.post("/api/streams/{stream_id}/retarget", tags=["streams"])
    async def retarget_stream(stream_id: str, body: RetargetRequest) -> dict[str, Any]:
        """Change one entity's rate while the stream runs (section 94).

        The point of the API over the CLI: a rate you can turn up while
        watching what it does to whatever is receiving it.
        """
        _found(streams.get(stream_id), "stream")
        return streams.retarget(stream_id, body.entity, body.rate)

    @app.post("/api/streams/{stream_id}/pause", tags=["streams"])
    async def pause_stream(stream_id: str) -> dict[str, Any]:
        _found(streams.get(stream_id), "stream")
        return {
            "paused": streams.pause(stream_id),
            "state": streams.require(stream_id).stream.state,
        }

    @app.post("/api/streams/{stream_id}/resume", tags=["streams"])
    async def resume_stream(stream_id: str) -> dict[str, Any]:
        _found(streams.get(stream_id), "stream")
        return {
            "resumed": streams.resume(stream_id),
            "state": streams.require(stream_id).stream.state,
        }

    @app.post("/api/streams/{stream_id}/stop", tags=["streams"])
    async def stop_stream(stream_id: str) -> dict[str, Any]:
        """Stop a stream and wait for its destinations to close.

        Waiting matters: a file sink that has not been closed may be holding
        the last batch, and a caller told "stopped" should be able to read the
        file immediately.
        """
        _found(streams.get(stream_id), "stream")
        stopped = await streams.stop(stream_id)
        return {"stopped": stopped, **streams.require(stream_id).describe()}

    @app.delete("/api/streams/{stream_id}", tags=["streams"])
    async def forget_stream(stream_id: str) -> dict[str, Any]:
        """Drop a finished stream. A running one is stopped first."""
        _found(streams.get(stream_id), "stream")
        await streams.stop(stream_id)
        return {"forgotten": streams.forget(stream_id)}

    @app.websocket("/api/streams/{stream_id}/feed")
    async def stream_feed(websocket: WebSocket, stream_id: str) -> None:
        """Push a stream's status until it finishes.

        Polled at a fixed interval rather than pushed per batch. A run emits a
        handful of events a minute; a stream produces tens of thousands of
        records a second, and a frame per batch would make the dashboard the
        most expensive thing in the process.
        """
        await websocket.accept()
        if streams.get(stream_id) is None:
            await websocket.send_json(
                {"kind": "error", "stream_id": stream_id, "message": f"no stream {stream_id}"}
            )
            return

        try:
            async for payload in streams.feed(stream_id):
                await websocket.send_json({"kind": "stream.status", **payload})
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

    _mount_studio(app, static_dir)
    return app


def _mount_studio(app: FastAPI, static_dir: str | Path | None) -> None:
    """Serve a built Studio, if one was pointed at.

    Mounted after every route so it can never shadow the API, and served
    through a fallback so the client-side router owns its own URLs: a browser
    reloading /runs/abc123 must get the application, not a 404.
    """
    root = Path(static_dir) if static_dir else _bundled_studio()
    if root is None or not root.is_dir():
        return

    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    index = root / "index.html"
    assets = root / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def studio(full_path: str) -> Any:
        candidate = (root / full_path).resolve()
        # Only files genuinely inside the build directory are served.
        if full_path and root.resolve() in candidate.parents and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(index)

    app.state.studio_root = root


def _bundled_studio() -> Path | None:
    """The Studio built into an installed package, when there is one."""
    candidate = Path(__file__).resolve().parent / "static"
    return candidate if candidate.is_dir() else None

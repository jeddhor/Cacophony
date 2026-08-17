"""The controller's HTTP face (design document section 95).

A small, separate application rather than routes on the Studio API. The Studio
API is about projects and runs a person is looking at; this is a scheduler that
machines talk to, and the two have different lifetimes, different clients and
different failure modes. Keeping them apart also means a controller can be run
on a box with no database and no UI.

    POST /register  /lease  /renew  /complete  /fail
    GET  /status  /shards  /health

Authentication is a shared bearer token, checked here and sent by
:class:`~cacophony.distributed.transport.HttpTransport`. It is deliberately the
simplest thing that stops an unrelated process from taking shards: a cluster
generating synthetic data does not need an identity system, and a token that
lives in an environment variable does not tempt anybody to put a credential in
a project file (section 63).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..core.errors import CacophonyError
from .capabilities import WorkerProfile

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fastapi import FastAPI

    from .controller import Controller

__all__ = ["create_controller_app"]


def create_controller_app(controller: Controller, *, token: str | None = None) -> FastAPI:
    """Wrap a controller in the six-call protocol."""
    from fastapi import Body, FastAPI, Header, HTTPException
    from fastapi.responses import JSONResponse

    from .. import __version__

    app = FastAPI(
        title="Cacophony Controller",
        version=__version__,
        summary="Distributed generation scheduler.",
    )
    app.state.controller = controller

    @app.exception_handler(CacophonyError)
    async def _cacophony_error(_request: Any, exc: CacophonyError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"error": "cacophony", "detail": str(exc)})

    def _authorise(authorization: str | None) -> None:
        if not token:
            return
        if authorization != f"Bearer {token}":
            raise HTTPException(status_code=401, detail="a valid controller token is required")

    # -- workers -------------------------------------------------------------- #

    @app.post("/register", tags=["workers"])
    async def register(
        body: dict[str, Any] = Body(...),
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _authorise(authorization)
        record = controller.register(WorkerProfile.from_dict(body))
        return {"worker": record.profile.to_dict(), "run": controller.describe()}

    @app.post("/lease", tags=["workers"])
    async def lease(
        body: dict[str, Any] = Body(...),
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _authorise(authorization)
        worker_id = str(body.get("worker_id") or "")
        count = int(body.get("count") or 1)
        leases = controller.acquire(worker_id, count=count)
        return {"leases": [lease.to_dict() for lease in leases]}

    @app.post("/renew", tags=["workers"])
    async def renew(
        body: dict[str, Any] = Body(...),
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _authorise(authorization)
        held = controller.renew(
            str(body.get("worker_id") or ""),
            str(body.get("shard_id") or ""),
            int(body.get("generation") or 0),
        )
        return {"held": held}

    @app.post("/complete", tags=["workers"])
    async def complete(
        body: dict[str, Any] = Body(...),
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _authorise(authorization)
        accepted = controller.complete(
            str(body.get("worker_id") or ""),
            str(body.get("shard_id") or ""),
            int(body.get("generation") or 0),
            int(body.get("records") or 0),
        )
        return {"accepted": accepted, "progress": round(controller.progress, 6)}

    @app.post("/fail", tags=["workers"])
    async def fail(
        body: dict[str, Any] = Body(...),
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _authorise(authorization)
        accepted = controller.report_failure(
            str(body.get("worker_id") or ""),
            str(body.get("shard_id") or ""),
            int(body.get("generation") or 0),
            str(body.get("reason") or "unspecified"),
        )
        return {"accepted": accepted}

    # -- observation ---------------------------------------------------------- #

    @app.get("/status", tags=["run"])
    async def status() -> dict[str, Any]:
        # Reclaiming here as well as on lease means a controller nobody is
        # asking for work still notices a dead worker, so the dashboard says
        # "reassigned" rather than sitting at 99%.
        controller.reclaim()
        return controller.describe()

    @app.get("/shards", tags=["run"])
    async def shards(state: str | None = None) -> list[dict[str, Any]]:
        leases = controller.leases.values()
        return [lease.to_dict() for lease in leases if state is None or lease.state.value == state]

    @app.get("/health", tags=["run"])
    async def health() -> dict[str, Any]:
        return {
            "ok": True,
            "version": __version__,
            "finished": controller.is_finished,
            "stalled": controller.is_stalled,
            "workers": len(controller.alive_workers()),
        }

    return app

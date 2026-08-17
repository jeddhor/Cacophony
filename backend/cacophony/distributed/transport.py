"""How a worker talks to a controller (design document section 95).

The protocol is six calls::

    POST /register              here is what I can do
    POST /lease                 give me work I can do
    POST /renew                 still working
    POST /complete              done, n records
    POST /fail                  could not do it, because
    GET  /status                what is the run doing

Two implementations sit behind one protocol. :class:`LocalTransport` calls the
controller's methods directly; :class:`HttpTransport` posts JSON. The worker
cannot tell them apart, which is what lets the whole lease protocol - expiry,
reassignment, stale generations, capability routing - be tested in a
millisecond without a socket, and lets a single-process run use exactly the
code path a cluster uses.

Every call is idempotent or generation-guarded, because the network will
duplicate some of them. Completing a shard twice is refused the second time;
leasing after a lost reply hands over a different shard rather than the same
one twice.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from ..core.errors import CacophonyError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .capabilities import WorkerProfile
    from .controller import Controller

__all__ = ["ControllerTransport", "HttpTransport", "LocalTransport"]


@runtime_checkable
class ControllerTransport(Protocol):
    """What a worker needs from a controller."""

    async def register(self, profile: WorkerProfile) -> dict[str, Any]: ...

    async def lease(self, worker_id: str, count: int = 1) -> list[dict[str, Any]]: ...

    async def renew(self, worker_id: str, shard_id: str, generation: int) -> bool: ...

    async def complete(
        self, worker_id: str, shard_id: str, generation: int, records: int, **extra: Any
    ) -> bool: ...

    async def fail(self, worker_id: str, shard_id: str, generation: int, reason: str) -> bool: ...

    async def status(self) -> dict[str, Any]: ...

    async def close(self) -> None: ...


class LocalTransport:
    """A controller in this process.

    Not a test double: ``cacophony cluster`` runs a real controller and real
    workers over this, so a laptop uses its cores through the same protocol a
    cluster uses its machines.
    """

    def __init__(self, controller: Controller) -> None:
        self.controller = controller

    async def register(self, profile: WorkerProfile) -> dict[str, Any]:
        record = self.controller.register(profile)
        return {"worker": record.profile.to_dict(), "run": self.controller.describe()}

    async def lease(self, worker_id: str, count: int = 1) -> list[dict[str, Any]]:
        return [lease.to_dict() for lease in self.controller.acquire(worker_id, count=count)]

    async def renew(self, worker_id: str, shard_id: str, generation: int) -> bool:
        return self.controller.renew(worker_id, shard_id, generation)

    async def complete(
        self, worker_id: str, shard_id: str, generation: int, records: int, **extra: Any
    ) -> bool:
        return self.controller.complete(worker_id, shard_id, generation, records)

    async def fail(self, worker_id: str, shard_id: str, generation: int, reason: str) -> bool:
        return self.controller.report_failure(worker_id, shard_id, generation, reason)

    async def status(self) -> dict[str, Any]:
        return self.controller.describe()

    async def close(self) -> None:
        return None


class HttpTransport:
    """A controller on another machine.

    Deliberately thin. Retries are the worker's business - it already has a
    retry story for a lost lease, and a transport that silently retried a
    ``complete`` would be inventing a second one.
    """

    def __init__(self, base_url: str, *, timeout: float = 30.0, token: str | None = None) -> None:
        import httpx

        self.base_url = base_url.rstrip("/")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=timeout, headers=headers)

    async def _post(self, path: str, payload: dict[str, Any]) -> Any:
        import httpx

        try:
            response = await self._client.post(path, json=payload)
        except httpx.HTTPError as exc:
            raise CacophonyError(f"controller at {self.base_url} is unreachable: {exc}") from exc
        if response.status_code >= 400:
            detail = _detail(response)
            raise CacophonyError(f"controller refused {path}: {detail}")
        return response.json()

    async def register(self, profile: WorkerProfile) -> dict[str, Any]:
        return await self._post("/register", profile.to_dict())

    async def lease(self, worker_id: str, count: int = 1) -> list[dict[str, Any]]:
        payload = await self._post("/lease", {"worker_id": worker_id, "count": count})
        return list(payload.get("leases") or [])

    async def renew(self, worker_id: str, shard_id: str, generation: int) -> bool:
        payload = await self._post(
            "/renew", {"worker_id": worker_id, "shard_id": shard_id, "generation": generation}
        )
        return bool(payload.get("held"))

    async def complete(
        self, worker_id: str, shard_id: str, generation: int, records: int, **extra: Any
    ) -> bool:
        payload = await self._post(
            "/complete",
            {
                "worker_id": worker_id,
                "shard_id": shard_id,
                "generation": generation,
                "records": records,
                **extra,
            },
        )
        return bool(payload.get("accepted"))

    async def fail(self, worker_id: str, shard_id: str, generation: int, reason: str) -> bool:
        payload = await self._post(
            "/fail",
            {
                "worker_id": worker_id,
                "shard_id": shard_id,
                "generation": generation,
                "reason": reason,
            },
        )
        return bool(payload.get("accepted"))

    async def status(self) -> dict[str, Any]:
        import httpx

        try:
            response = await self._client.get("/status")
        except httpx.HTTPError as exc:
            raise CacophonyError(f"controller at {self.base_url} is unreachable: {exc}") from exc
        return dict(response.json())

    async def close(self) -> None:
        await self._client.aclose()


def _detail(response: Any) -> str:
    try:
        body = response.json()
    except Exception:  # pragma: no cover - non-JSON error page
        return f"HTTP {response.status_code}"
    if isinstance(body, dict):
        return str(body.get("detail") or body.get("error") or body)
    return str(body)

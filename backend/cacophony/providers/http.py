"""Shared HTTP machinery for remote providers (design document sections 30, 85).

Providers are addressed by URI and Cacophony never owns the models, so every
adapter in the provider layer is an HTTP client. The differences between them
are the request shape and the response shape; the connection handling,
concurrency limiting, timeout policy and transport-error retry are the same,
and live here.

Concurrency is per provider, not global (section 30). A box running Ollama on
one GPU and InvokeAI on another wants four concurrent language-model requests
and one concurrent image request, and the only place that distinction can be
enforced correctly is inside the provider that owns the resource.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

import httpx

from ..core.errors import ProviderError, ProviderUnavailableError
from ..core.interfaces import HealthStatus, Provider
from .secrets import DEFAULT_RESOLVER, SecretResolver

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Mapping

__all__ = ["HttpProvider"]

#: Transport-level failures worth one more attempt. A refused connection or a
#: dropped socket is usually a model being swapped in, not a permanent fault.
_RETRYABLE = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.RemoteProtocolError,
    httpx.WriteError,
)


class HttpProvider(Provider):
    """A provider that talks to an HTTP endpoint."""

    #: Fallback when the project does not set ``base_url``.
    default_base_url: str = "http://localhost:8080"

    def __init__(
        self,
        provider_id: str,
        config: dict[str, Any] | None = None,
        *,
        secrets: SecretResolver | None = None,
    ) -> None:
        super().__init__(provider_id, config)
        self.base_url = str(self.config.get("base_url") or self.default_base_url).rstrip("/")
        self.model = self.config.get("model")
        self.timeout = float(self.config.get("timeout_seconds") or 120.0)
        self.concurrency = max(1, int(self.config.get("concurrency") or 1))
        self.transport_retries = max(0, int(self.config.get("transport_retries") or 1))

        self._secrets = secrets or DEFAULT_RESOLVER
        self._secret_id = self.config.get("secret")
        self._client: httpx.AsyncClient | None = None
        self._semaphore: asyncio.Semaphore | None = None
        self._transport = self.config.get("transport")  # injected in tests

    # -- credentials -------------------------------------------------------- #

    @property
    def credential(self) -> str | None:
        """The resolved credential, if this provider was given a secret id."""
        return self._secrets.resolve(self._secret_id)

    def auth_headers(self) -> dict[str, str]:
        credential = self.credential
        return {"Authorization": f"Bearer {credential}"} if credential else {}

    # -- connection --------------------------------------------------------- #

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                transport=self._transport,
                headers={"User-Agent": "cacophony", **self.auth_headers()},
            )
        return self._client

    def _ensure_semaphore(self) -> asyncio.Semaphore:
        # Created lazily because a Semaphore binds to the running loop, and the
        # provider is constructed before the loop exists.
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self.concurrency)
        return self._semaphore

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- requests ----------------------------------------------------------- #

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: Any | None = None,
        params: Mapping[str, Any] | None = None,
        limit_concurrency: bool = True,
    ) -> tuple[Any, float]:
        """Issue a request and return ``(decoded_json, elapsed_ms)``.

        Every failure mode is translated into a
        :class:`~cacophony.core.errors.ProviderError` naming the provider and
        the URL, because a bare httpx traceback three million records into a
        run tells the user nothing about which of their five providers broke.
        """
        client = self._ensure_client()
        semaphore = self._ensure_semaphore() if limit_concurrency else _NullSemaphore()

        async with semaphore:
            started = time.perf_counter()
            last_error: Exception | None = None

            for attempt in range(self.transport_retries + 1):
                try:
                    response = await client.request(method, path, json=json_body, params=params)
                except _RETRYABLE as exc:
                    last_error = exc
                    if attempt < self.transport_retries:
                        await asyncio.sleep(0.25 * (attempt + 1))
                        continue
                    raise ProviderUnavailableError(
                        f"provider '{self.id}' at {self.base_url}{path} is unreachable: {exc}"
                    ) from exc
                except httpx.HTTPError as exc:
                    raise ProviderError(
                        f"provider '{self.id}' request to {self.base_url}{path} failed: {exc}"
                    ) from exc

                elapsed_ms = (time.perf_counter() - started) * 1000

                if response.status_code >= 400:
                    raise ProviderError(
                        f"provider '{self.id}' returned HTTP {response.status_code} for "
                        f"{path}: {_snippet(response.text)}"
                    )
                try:
                    return response.json(), elapsed_ms
                except ValueError as exc:
                    raise ProviderError(
                        f"provider '{self.id}' returned a non-JSON response for {path}: "
                        f"{_snippet(response.text)}"
                    ) from exc

            raise ProviderUnavailableError(
                f"provider '{self.id}' at {self.base_url}{path} is unreachable: {last_error}"
            )

    # -- health ------------------------------------------------------------- #

    #: Path probed by the default health check.
    health_path: str = "/"

    async def health_check(self) -> HealthStatus:
        try:
            payload, elapsed_ms = await self.request_json(
                "GET", self.health_path, limit_concurrency=False
            )
        except ProviderUnavailableError as exc:
            return HealthStatus.down(str(exc))
        except ProviderError as exc:
            return HealthStatus.down(str(exc))
        return HealthStatus.up(
            f"{self.id} is reachable",
            latency_ms=round(elapsed_ms, 2),
            details=self._health_details(payload),
        )

    def _health_details(self, payload: Any) -> dict[str, Any]:
        return {}

    def describe(self) -> dict[str, Any]:
        return {
            **super().describe(),
            "adapter": type(self).adapter_name,
            "base_url": self.base_url,
            "model": self.model,
            "concurrency": self.concurrency,
            "secret_id": self._secret_id,
        }

    #: Registry key, set by ``@register_adapter``.
    adapter_name: str = ""


class _NullSemaphore:
    """A no-op stand-in for when concurrency limiting is deliberately skipped."""

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc_info: Any) -> None:
        return None


def _snippet(text: str, limit: int = 200) -> str:
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"

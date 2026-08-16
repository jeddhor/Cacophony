"""The InvokeAI image provider (design document sections 18, 19, 85).

InvokeAI runs graphs, not prompts. Its API takes a *workflow* - a node graph
with a prompt node, a noise node, a denoise node and an output - enqueues it on
a session queue, and returns a batch id you then poll. That is a different
shape from "POST a prompt, receive an image", and Cacophony's
:class:`~cacophony.providers.base.ImageProvider` interface deliberately hides
it: a field says what it wants, and the adapter deals with the machinery.

Two things follow.

**A default graph.** A project that names no workflow gets a minimal
text-to-image graph built here, so ``generator: image`` works against a stock
InvokeAI install without anyone editing JSON. A project that *does* name a
workflow has it submitted as-is, with the prompt and seed substituted in.

**Polling with a deadline.** Image generation takes seconds to minutes, so the
adapter enqueues, polls the queue item until it completes, then fetches the
image by name. Every wait is bounded: section 66 forbids infinite retries, and
a wedged GPU must fail a run rather than hang it.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from typing import TYPE_CHECKING, Any

from ...core.errors import ProviderError, ProviderUnavailableError
from ...core.interfaces import Capability
from ..base import ImageProvider, ImageRequest, ImageResult
from ..http import HttpProvider
from ..registry import register_adapter

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..secrets import SecretResolver

__all__ = ["InvokeAIProvider"]

#: How often to ask whether the image is ready.
_POLL_SECONDS = 1.0


@register_adapter("invokeai", aliases=("invoke",))
class InvokeAIProvider(HttpProvider, ImageProvider):
    """Generate images with a local InvokeAI server."""

    kind = "image"
    default_base_url = "http://localhost:9090"
    health_path = "/api/v1/app/version"

    def __init__(
        self,
        provider_id: str,
        config: dict[str, Any] | None = None,
        *,
        secrets: SecretResolver | None = None,
    ) -> None:
        super().__init__(provider_id, config, secrets=secrets)
        #: The queue InvokeAI runs sessions on. "default" on a stock install.
        self.queue_id = str(self.config.get("queue_id") or "default")
        self.scheduler = str(self.config.get("scheduler") or "euler")
        self.steps = int(self.config.get("steps", 30))
        self.guidance = float(self.config.get("guidance", 7.5))
        self.negative_prompt = str(self.config.get("negative_prompt") or "")
        #: A run that waits forever on a wedged GPU is worse than one that fails.
        self.poll_timeout = float(self.config.get("poll_timeout_seconds", 300.0))

    # -- generation --------------------------------------------------------- #

    async def generate(self, request: ImageRequest) -> ImageResult:
        batch = self._batch_for(request)
        enqueued, _elapsed = await self.request_json(
            "POST", f"/api/v1/queue/{self.queue_id}/enqueue_batch", json_body=batch
        )

        item_id = _first_item_id(enqueued)
        if item_id is None:
            raise ProviderError(
                f"provider '{self.id}' enqueued a batch but InvokeAI returned no queue item"
            )

        name = await self._await_image(item_id)
        data, media_type, _ms = await self.request_bytes("GET", f"/api/v1/images/i/{name}/full")

        return ImageResult(
            data=data,
            width=request.width,
            height=request.height,
            media_type=media_type or "image/png",
            provider=self.id,
            workflow=request.workflow or "cacophony:text_to_image",
            seed=request.seed,
            prompt_hash=hashlib.blake2b(request.prompt.encode("utf-8"), digest_size=8).hexdigest(),
            raw={"image_name": name, "queue_item_id": item_id},
        )

    async def _await_image(self, item_id: int | str) -> str:
        """Poll until the queue item finishes, and return the image name."""
        deadline = time.monotonic() + self.poll_timeout

        while time.monotonic() < deadline:
            payload, _ms = await self.request_json(
                "GET", f"/api/v1/queue/{self.queue_id}/i/{item_id}", limit_concurrency=False
            )
            status = str(payload.get("status") or "").lower()

            if status == "completed":
                name = _image_name(payload)
                if name is None:
                    raise ProviderError(
                        f"provider '{self.id}' completed queue item {item_id} but produced "
                        "no image; check the workflow's output node"
                    )
                return name
            if status in ("failed", "canceled", "cancelled"):
                reason = payload.get("error_message") or payload.get("error") or status
                raise ProviderError(f"provider '{self.id}' failed to generate an image: {reason}")

            await asyncio.sleep(_POLL_SECONDS)

        raise ProviderUnavailableError(
            f"provider '{self.id}' did not produce an image within {self.poll_timeout:.0f}s. "
            "Raise 'poll_timeout_seconds' if the model is simply slow."
        )

    # -- the graph ---------------------------------------------------------- #

    def _batch_for(self, request: ImageRequest) -> dict[str, Any]:
        """The enqueue body: a caller's workflow, or the default graph."""
        if isinstance(request.metadata.get("graph"), dict):
            graph = dict(request.metadata["graph"])
            _substitute(graph, request)
        else:
            graph = self._default_graph(request)

        return {
            "prepend": False,
            "batch": {"graph": graph, "runs": 1},
        }

    def _default_graph(self, request: ImageRequest) -> dict[str, Any]:
        """A minimal text-to-image graph.

        Enough to work against a stock install and no more: prompt, noise,
        denoise, decode. A project wanting ControlNet, refiners or LoRAs
        supplies its own workflow, which is exactly the seam section 18 asks
        for ("workflow selection").
        """
        model = request.model or self.model
        graph_id = f"cacophony-{uuid.uuid4()}"

        nodes: dict[str, Any] = {
            "model": {"id": "model", "type": "main_model_loader", "model": model},
            "positive": {
                "id": "positive",
                "type": "compel",
                "prompt": request.prompt,
            },
            "negative": {
                "id": "negative",
                "type": "compel",
                "prompt": request.negative_prompt or self.negative_prompt,
            },
            "noise": {
                "id": "noise",
                "type": "noise",
                "seed": request.seed or 0,
                "width": request.width,
                "height": request.height,
            },
            "denoise": {
                "id": "denoise",
                "type": "denoise_latents",
                "steps": request.steps or self.steps,
                "cfg_scale": request.guidance or self.guidance,
                "scheduler": self.scheduler,
                "denoising_start": 0.0,
                "denoising_end": 1.0,
            },
            "output": {"id": "output", "type": "l2i", "fp32": False, "is_intermediate": False},
        }

        edges = [
            _edge("model", "unet", "denoise", "unet"),
            _edge("model", "clip", "positive", "clip"),
            _edge("model", "clip", "negative", "clip"),
            _edge("model", "vae", "output", "vae"),
            _edge("positive", "conditioning", "denoise", "positive_conditioning"),
            _edge("negative", "conditioning", "denoise", "negative_conditioning"),
            _edge("noise", "noise", "denoise", "noise"),
            _edge("denoise", "latents", "output", "latents"),
        ]
        return {"id": graph_id, "nodes": nodes, "edges": edges}

    # -- health ------------------------------------------------------------- #

    def _health_details(self, payload: Any) -> dict[str, Any]:
        if isinstance(payload, dict) and payload.get("version"):
            return {"invokeai_version": str(payload["version"])}
        return {}

    def capabilities(self) -> list[Capability]:
        return [Capability("text_to_image"), Capability("image_to_image")]


def _edge(
    source: str, source_field: str, destination: str, destination_field: str
) -> dict[str, Any]:
    return {
        "source": {"node_id": source, "field": source_field},
        "destination": {"node_id": destination, "field": destination_field},
    }


def _substitute(graph: dict[str, Any], request: ImageRequest) -> None:
    """Put this record's prompt and seed into a user-supplied workflow.

    A workflow is a template: the user chose the model, the scheduler and the
    node wiring, and Cacophony supplies what changes per record. Nodes are
    matched by type rather than by id, because ids are whatever InvokeAI's
    editor generated.
    """
    for node in (graph.get("nodes") or {}).values():
        if not isinstance(node, dict):
            continue
        node_type = node.get("type")
        if node_type == "compel" and node.get("id") not in ("negative",) and "prompt" in node:
            node["prompt"] = request.prompt
        elif node_type == "noise":
            if request.seed is not None:
                node["seed"] = request.seed
            node["width"] = request.width
            node["height"] = request.height


def _first_item_id(payload: Any) -> int | str | None:
    if not isinstance(payload, dict):
        return None
    for key in ("item_ids", "queue_items"):
        values = payload.get(key)
        if isinstance(values, list) and values:
            first = values[0]
            if isinstance(first, dict):
                return first.get("item_id") or first.get("id")
            return first
    item = payload.get("queue_item")
    if isinstance(item, dict):
        return item.get("item_id") or item.get("id")
    return None


def _image_name(payload: Any) -> str | None:
    """Find the produced image's name in a completed queue item."""
    if not isinstance(payload, dict):
        return None

    outputs = (
        payload.get("session", {}).get("results")
        if isinstance(payload.get("session"), dict)
        else None
    )
    for source in (outputs, payload.get("outputs"), payload):
        if not isinstance(source, dict):
            continue
        for value in source.values():
            if isinstance(value, dict):
                image = value.get("image")
                if isinstance(image, dict) and image.get("image_name"):
                    return str(image["image_name"])
    return None

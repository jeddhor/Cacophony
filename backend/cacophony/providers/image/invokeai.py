"""The InvokeAI image provider (design document sections 18, 19, 85).

InvokeAI runs graphs, not prompts. Its API takes a *workflow* - a node graph
with a prompt node, a noise node, a denoise node and an output - enqueues it on
a session queue, and returns a batch id you then poll. That is a different
shape from "POST a prompt, receive an image", and Cacophony's
:class:`~cacophony.providers.base.ImageProvider` interface deliberately hides
it: a field says what it wants, and the adapter deals with the machinery.

Three things follow.

**Models are identified, not named.** A node does not take ``"Dreamshaper 8"``;
it takes a ``ModelIdentifierField`` of key, hash, name, base and type. So the
adapter reads the server's model list once and resolves whatever the schema
called the model - a name, a key, or nothing at all - into that structure. This
is the single thing most likely to be got wrong by writing the adapter against
the documentation rather than against a server, and it was.

**A default graph, per architecture.** A project that names no workflow gets a
minimal text-to-image graph built here, so ``generator: image`` works against a
stock install without anyone editing JSON. SD-1/SD-2 and SDXL wire differently
- SDXL has two text encoders - so there are two graphs. Anything else (FLUX,
Qwen-Image, Z-Image) has its own topology and needs a workflow, which the
adapter says plainly rather than submitting a graph that will fail.

**Polling with a deadline.** Image generation takes seconds to minutes, so the
adapter enqueues, polls the queue item until it completes, then fetches the
image by name. Every wait is bounded: section 66 forbids infinite retries, and
a wedged GPU must fail a run rather than hang it.

Verified against InvokeAI 6.13.8.
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

#: Model architectures the built-in graphs cover, and how they wire up.
#: ``(loader type, prompt node type, extra edges from loader to prompt)``
_ARCHITECTURES: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "sd-1": ("main_model_loader", "compel", ()),
    "sd-2": ("main_model_loader", "compel", ()),
    "sdxl": ("sdxl_model_loader", "sdxl_compel_prompt", ("clip2",)),
}


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
        #: The server's model list, fetched on first use.
        self._model_cache: list[dict[str, Any]] | None = None

    # -- generation --------------------------------------------------------- #

    async def generate(self, request: ImageRequest) -> ImageResult:
        batch = await self._batch_for(request)
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

    async def _batch_for(self, request: ImageRequest) -> dict[str, Any]:
        """The enqueue body: a caller's workflow, or a default graph."""
        if isinstance(request.metadata.get("graph"), dict):
            graph = dict(request.metadata["graph"])
            _substitute(graph, request)
        else:
            graph = await self._default_graph(request)

        return {"prepend": False, "batch": {"graph": graph, "runs": 1}}

    # -- models -------------------------------------------------------------- #

    async def _models(self) -> list[dict[str, Any]]:
        """The server's model list, fetched once.

        Cached for the lifetime of the provider: a ten-thousand-portrait run
        should ask which models exist once, not ten thousand times.
        """
        if self._model_cache is None:
            payload, _ms = await self.request_json(
                "GET", "/api/v2/models/", limit_concurrency=False
            )
            self._model_cache = list(payload.get("models") or [])
        return self._model_cache

    async def _identify(self, wanted: str | None) -> dict[str, Any]:
        """Turn a model name, or a key, or nothing, into a ModelIdentifierField.

        A node will not take ``"Dreamshaper 8"``. It takes key, hash, name,
        base and type, which only the server can supply - so the schema names
        a model the way a person would and this resolves it.
        """
        models = await self._models()
        mains = [model for model in models if model.get("type") == "main"]
        if not mains:
            raise ProviderError(
                f"provider '{self.id}' has no main models installed; install one in InvokeAI"
            )

        chosen: dict[str, Any] | None = None
        if wanted:
            for model in mains:
                if wanted in (model.get("key"), model.get("name")):
                    chosen = model
                    break
            if chosen is None:
                known = ", ".join(sorted(str(model.get("name")) for model in mains))
                raise ProviderError(
                    f"provider '{self.id}' has no model '{wanted}'. Installed: {known}"
                )
        else:
            # No model named: prefer one the built-in graphs can actually wire.
            chosen = next(
                (model for model in mains if model.get("base") in _ARCHITECTURES), mains[0]
            )

        return {
            "key": chosen["key"],
            "hash": chosen["hash"],
            "name": chosen["name"],
            "base": chosen["base"],
            "type": chosen["type"],
        }

    # -- the graph ----------------------------------------------------------- #

    async def _default_graph(self, request: ImageRequest) -> dict[str, Any]:
        """A minimal text-to-image graph for this model's architecture.

        Enough to work against a stock install and no more: prompt, noise,
        denoise, decode. A project wanting ControlNet, refiners or LoRAs
        supplies its own workflow, which is exactly the seam section 18 asks
        for ("workflow selection").
        """
        identifier = await self._identify(request.model or self.model)
        base = str(identifier["base"])

        architecture = _ARCHITECTURES.get(base)
        if architecture is None:
            supported = ", ".join(sorted(_ARCHITECTURES))
            raise ProviderError(
                f"provider '{self.id}': '{identifier['name']}' is a {base} model, and the "
                f"built-in text-to-image graph covers {supported}. {base.upper()} has its own "
                "node topology, so give the field a 'workflow' exported from InvokeAI, or "
                "name a model of a supported architecture."
            )
        loader_type, prompt_type, extra_clips = architecture

        nodes: dict[str, Any] = {
            "model": {"id": "model", "type": loader_type, "model": identifier},
            "positive": {"id": "positive", "type": prompt_type, "prompt": request.prompt},
            "negative": {
                "id": "negative",
                "type": prompt_type,
                "prompt": request.negative_prompt or self.negative_prompt,
            },
            "noise": {
                "id": "noise",
                "type": "noise",
                "seed": (request.seed or 0) % 0xFFFFFFFF,
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
        # SDXL has a second text encoder, and its prompt nodes want both.
        for clip in extra_clips:
            edges.append(_edge("model", clip, "positive", clip))
            edges.append(_edge("model", clip, "negative", clip))

        return {"id": f"cacophony-{uuid.uuid4()}", "nodes": nodes, "edges": edges}

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
        if node_type in ("compel", "sdxl_compel_prompt") and node.get("id") != "negative":
            if "prompt" in node:
                node["prompt"] = request.prompt
        elif node_type == "noise":
            if request.seed is not None:
                # InvokeAI's seed field is a 32-bit unsigned integer; a
                # Cacophony seed is 64-bit and would be rejected outright.
                node["seed"] = request.seed % 0xFFFFFFFF
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
    """Find the produced image's name in a completed queue item.

    A finished session carries one result per node, keyed by *prepared* node id
    - a UUID InvokeAI assigns, not the id the graph used - so results are
    matched on their declared output type rather than on where they sit. A
    graph with several image nodes (a refiner writing an intermediate, say)
    yields several; the last is the one the graph ended on.
    """
    if not isinstance(payload, dict):
        return None

    session = payload.get("session")
    results = session.get("results") if isinstance(session, dict) else None

    found: list[str] = []
    for source in (results, payload.get("outputs")):
        if not isinstance(source, dict):
            continue
        for value in source.values():
            if not isinstance(value, dict) or value.get("type") != "image_output":
                continue
            image = value.get("image")
            if isinstance(image, dict) and image.get("image_name"):
                found.append(str(image["image_name"]))
    return found[-1] if found else None

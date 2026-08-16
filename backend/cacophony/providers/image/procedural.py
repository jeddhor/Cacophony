"""A procedural image provider (design document sections 18, 111).

Not a diffusion model, and never presented as one. This draws deterministic
placeholder imagery - identicon-style avatars, flat product cards, document
thumbnails - from the seed and prompt it is given.

Why it exists at all, when section 18's target is InvokeAI: an image field
changes the *shape* of a project. Records grow assets, the asset store fills,
paths appear in the output, provenance records a workflow, the run summary
reports files and bytes. All of that deserves to be exercised, and to be
exercisable by someone who has no GPU, on a laptop, in a test suite, in CI.

The images it makes are obviously synthetic, which is the point: nobody should
mistake one for a generated portrait. Point a project at InvokeAI and the same
schema produces real images through the same asset path.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

from ...assets.imaging import Canvas
from ...core.interfaces import Capability, HealthStatus
from ..base import ImageProvider, ImageRequest, ImageResult
from ..registry import register_adapter

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..secrets import SecretResolver

__all__ = ["ProceduralImageProvider"]

#: The Cacophony palette (section 45): graphite, violet, cyan, magenta.
_PALETTE: tuple[tuple[int, int, int], ...] = (
    (26, 26, 36),
    (46, 40, 72),
    (124, 92, 220),
    (179, 136, 255),
    (60, 220, 220),
    (86, 240, 200),
    (236, 88, 180),
    (255, 138, 200),
)

#: What an image looks like, chosen by the field's ``style`` option.
STYLES = ("identicon", "portrait", "card", "document")


@register_adapter("procedural_image", aliases=("procedural", "placeholder_image"))
class ProceduralImageProvider(ImageProvider):
    """Deterministic placeholder imagery, drawn locally."""

    def __init__(
        self,
        provider_id: str,
        config: dict[str, Any] | None = None,
        *,
        secrets: SecretResolver | None = None,
    ) -> None:
        super().__init__(provider_id, config)
        self.style = str(self.config.get("style") or "identicon")
        self.compression = int(self.config.get("compression", 6))

    async def generate(self, request: ImageRequest) -> ImageResult:
        # The seed is what makes this reproducible; the prompt only feeds it,
        # so the same record draws the same picture on every run.
        material = f"{request.seed}\x00{request.prompt}\x00{request.workflow or ''}"
        digest = hashlib.blake2b(material.encode("utf-8"), digest_size=32).digest()

        style = str(request.metadata.get("style") or self.style)
        width = max(16, request.width)
        height = max(16, request.height)
        canvas = Canvas(width, height)

        if style == "portrait":
            _draw_portrait(canvas, digest)
        elif style == "card":
            _draw_card(canvas, digest)
        elif style == "document":
            _draw_document(canvas, digest)
        else:
            _draw_identicon(canvas, digest)

        return ImageResult(
            data=canvas.to_png(level=self.compression),
            width=width,
            height=height,
            media_type="image/png",
            provider=self.id,
            workflow=request.workflow or f"procedural:{style}",
            seed=request.seed,
            prompt_hash=hashlib.blake2b(request.prompt.encode("utf-8"), digest_size=8).hexdigest(),
            raw={"style": style, "synthetic": True},
        )

    async def health_check(self) -> HealthStatus:
        # Nothing to reach: it draws in-process. Saying so is more useful than
        # a green tick that means nothing.
        return HealthStatus.up(
            f"{self.id} draws placeholder images in-process",
            latency_ms=0.0,
            details={"style": self.style, "styles": ", ".join(STYLES)},
        )

    def capabilities(self) -> list[Capability]:
        return [Capability("text_to_image"), Capability("placeholder")]

    def describe(self) -> dict[str, Any]:
        return {**super().describe(), "adapter": "procedural_image", "style": self.style}


# --------------------------------------------------------------------------- #
# Styles
# --------------------------------------------------------------------------- #


def _colours(digest: bytes, count: int, *, offset: int = 0) -> list[tuple[int, int, int]]:
    return [
        _PALETTE[digest[(offset + index) % len(digest)] % len(_PALETTE)] for index in range(count)
    ]


def _draw_identicon(canvas: Canvas, digest: bytes) -> None:
    """A symmetric grid, the shape every identicon has taken since Gravatar."""
    background = _PALETTE[digest[0] % 2]
    canvas.fill(background)
    inset = max(2, canvas.width // 16)
    inner = Canvas(canvas.width - 2 * inset, canvas.height - 2 * inset, background)
    inner.blocks(5, 5, _colours(digest, 15, offset=1))
    _paste(canvas, inner, inset, inset)


def _draw_portrait(canvas: Canvas, digest: bytes) -> None:
    """A head-and-shoulders silhouette. Obviously not a photograph."""
    canvas.vertical_gradient(_PALETTE[digest[0] % 2], _PALETTE[digest[1] % 2 + 1])
    figure = _colours(digest, 1, offset=2)[0]

    width, height = canvas.width, canvas.height
    head = max(4, width // 4)
    canvas.rectangle((width - head) // 2, height // 6, head, head, figure)
    shoulders = max(6, int(width * 0.62))
    canvas.rectangle(
        (width - shoulders) // 2,
        height // 6 + head + max(2, height // 24),
        shoulders,
        height,
        figure,
    )


def _draw_card(canvas: Canvas, digest: bytes) -> None:
    """A product-card shape: a coloured field with a band and a swatch."""
    canvas.fill(_colours(digest, 1)[0])
    accent, band = _colours(digest, 2, offset=3)
    canvas.rectangle(0, int(canvas.height * 0.72), canvas.width, canvas.height, band)
    size = max(4, min(canvas.width, canvas.height) // 3)
    canvas.rectangle((canvas.width - size) // 2, int(canvas.height * 0.22), size, size, accent)


def _draw_document(canvas: Canvas, digest: bytes) -> None:
    """A page with ruled lines, for scanned-document datasets."""
    canvas.fill((246, 244, 240))
    ink = (48, 48, 60)
    margin = max(3, canvas.width // 12)
    line_height = max(2, canvas.height // 28)

    canvas.rectangle(margin, margin, int(canvas.width * 0.45), line_height * 2, ink)
    cursor = margin + line_height * 5
    index = 0
    while cursor < canvas.height - margin:
        width = int((canvas.width - 2 * margin) * (0.45 + (digest[index % len(digest)] % 55) / 100))
        canvas.rectangle(margin, cursor, width, max(1, line_height // 2), ink)
        cursor += line_height * 2
        index += 1


def _paste(target: Canvas, source: Canvas, x: int, y: int) -> None:
    """Copy ``source`` into ``target`` at ``(x, y)``, clipped to the target."""
    for row in range(source.height):
        line = y + row
        if not 0 <= line < target.height:
            continue
        width = min(source.width, target.width - x)
        if width <= 0:
            continue
        start = (line * target.width + x) * 3
        origin = row * source.width * 3
        target.pixels[start : start + width * 3] = source.pixels[origin : origin + width * 3]

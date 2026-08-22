"""Turning a document into a page image, and then spoiling it (section 23).

Section 23 asks, optionally, for documents to be degraded and rasterised: the
input an OCR pipeline actually meets, rather than the clean PDF a generator
would otherwise hand it. A dataset of perfect pages tests nothing, because the
hard part of document processing is the skew, the speckle and the photocopier.

Rendered from the :class:`~cacophony.assets.documents.Document` rather than
from the PDF bytes. Both come from the same laid-out lines, so the image and
the file say the same thing, and rendering the model directly avoids shipping a
PDF engine on three platforms to read back something this program just wrote.
It also means the *text is known*: an OCR dataset without an answer key is a
pile of pictures, so the recognised-text ground truth travels in the asset's
metadata beside the image.

Pillow does the drawing and the spoiling, under the ``ocr`` extra. Everything
here is deterministic in a seed, like every other value in a run.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..core.errors import OutputError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .documents import Document

__all__ = ["Degradation", "render_page"]

#: PDF points to inches. A page is described in points; a scanner in dots.
_POINTS_PER_INCH = 72.0


@dataclass(slots=True)
class Degradation:
    """How badly to treat the page. Every field is off at zero."""

    #: Degrees of skew. A page on a flatbed is never quite square.
    rotate: float = 0.0
    #: Gaussian blur radius, in output pixels. A cheap lens, or a copy of a copy.
    blur: float = 0.0
    #: Fraction of pixels flipped towards black or white - toner and dust.
    speckle: float = 0.0
    #: 1.0 leaves contrast alone; below it greys the page out.
    contrast: float = 1.0
    #: JPEG quality, when the output is JPEG. Low values ring around the text.
    quality: int = 90

    @property
    def is_noop(self) -> bool:
        return (
            self.rotate == 0.0 and self.blur == 0.0 and self.speckle == 0.0 and self.contrast == 1.0
        )

    @classmethod
    def from_options(cls, options: Any) -> Degradation:
        """Build one from a schema's ``degrade:`` block."""
        if not options:
            return cls()
        if not isinstance(options, dict):
            raise OutputError("'degrade' must be a mapping of settings")
        known = {field: options[field] for field in cls.__slots__ if field in options}
        unknown = sorted(set(options) - set(cls.__slots__))
        if unknown:
            available = ", ".join(cls.__slots__)
            raise OutputError(f"unknown degradation {', '.join(unknown)}. Available: {available}")
        return cls(**known)


def render_page(
    document: Document,
    *,
    page: int = 0,
    dpi: int = 150,
    image_format: str = "png",
    degrade: Degradation | None = None,
    seed: int = 0,
) -> tuple[bytes, str]:
    """Render one page of ``document`` as an image, optionally spoiled.

    Returns the bytes and their media type. ``seed`` decides the speckle and
    the direction of the skew, so the same record produces the same scan.
    """
    try:
        from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont
    except ImportError as exc:  # pragma: no cover - depends on the extra
        raise OutputError(
            "rasterising a document needs Pillow: pip install 'cacophony[ocr]'"
        ) from exc

    import io
    import random

    spoil = degrade or Degradation()
    scale = max(36, dpi) / _POINTS_PER_INCH
    width_pt, height_pt = document.dimensions
    size = (int(width_pt * scale), int(height_pt * scale))

    pages = document.pages or []
    if not pages:
        raise OutputError("the document has no pages to rasterise")
    chosen = pages[min(max(0, page), len(pages) - 1)]

    canvas = Image.new("L", size, color=255)
    draw = ImageDraw.Draw(canvas)

    for x_pt, y_pt, text, _font_name, font_size in chosen.lines:
        if not text:
            continue
        # PDF measures y from the bottom of the page; an image measures it from
        # the top, which is the one flip this whole module needs to get right.
        left = x_pt * scale
        top = (height_pt - y_pt) * scale
        font = _font_for(ImageFont, font_size * scale)
        draw.text((left, top), text, fill=20, font=font)

    if spoil.contrast != 1.0:
        canvas = ImageEnhance.Contrast(canvas).enhance(max(0.05, spoil.contrast))

    if spoil.speckle > 0:
        rng = random.Random(seed)
        pixels = canvas.load()
        assert pixels is not None
        total = size[0] * size[1]
        for _ in range(int(total * min(0.2, spoil.speckle))):
            x = rng.randrange(size[0])
            y = rng.randrange(size[1])
            pixels[x, y] = 0 if rng.random() < 0.7 else 255

    if spoil.rotate:
        # Rotated about the centre and expanded, so nothing is cropped; the new
        # corners are page-white rather than black.
        angle = spoil.rotate if seed % 2 == 0 else -spoil.rotate
        canvas = canvas.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True, fillcolor=255)

    if spoil.blur > 0:
        canvas = canvas.filter(ImageFilter.GaussianBlur(radius=spoil.blur))

    buffer = io.BytesIO()
    kind = image_format.lower()
    if kind in ("jpeg", "jpg"):
        canvas.convert("L").save(buffer, format="JPEG", quality=max(1, min(95, spoil.quality)))
        return buffer.getvalue(), "image/jpeg"
    canvas.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue(), "image/png"


def _font_for(module: Any, size: float) -> Any:
    """A font at this size, falling back to Pillow's own.

    A system font would render better and would also make the output depend on
    which fonts the machine happens to have, which is the opposite of what a
    reproducible dataset needs.
    """
    try:
        return module.load_default(size=max(6.0, size))
    except TypeError:  # pragma: no cover - older Pillow
        return module.load_default()

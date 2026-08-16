"""A small raster canvas and a PNG encoder (design document section 18).

Cacophony needs to *write* images, not edit them: a portrait comes back from
InvokeAI as PNG bytes and goes straight to disk, and the procedural provider
draws simple deterministic shapes. Neither needs filters, colour management or
format conversion, and taking Pillow as a dependency to avoid a hundred lines
of zlib would put a wheel with native code in the way of ``pip install
cacophony`` for every user who never generates an image.

So this module writes PNG from the standard library, and offers just enough
drawing to make a recognisable avatar: fill, rectangle, and a linear gradient.
Anything more ambitious belongs to a real image provider.
"""

from __future__ import annotations

import struct
import zlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

__all__ = ["Canvas", "encode_png", "is_png"]

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

RGB = tuple[int, int, int]


def is_png(data: bytes) -> bool:
    """Whether ``data`` begins with the PNG signature."""
    return data[:8] == _PNG_SIGNATURE


def encode_png(width: int, height: int, pixels: bytes, *, level: int = 6) -> bytes:
    """Encode RGB bytes as a PNG.

    ``pixels`` is ``width * height * 3`` bytes, row-major. Every scanline is
    written with filter type 0: the images this produces are flat colour and
    gradients, where a predictive filter buys almost nothing and costs a pass
    over every row.
    """
    expected = width * height * 3
    if len(pixels) != expected:
        raise ValueError(f"expected {expected} bytes of pixel data, got {len(pixels)}")

    stride = width * 3
    raw = bytearray()
    for row in range(height):
        raw.append(0)  # filter: none
        raw += pixels[row * stride : (row + 1) * stride]

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"".join(
        [
            _PNG_SIGNATURE,
            _chunk(b"IHDR", header),
            _chunk(b"IDAT", zlib.compress(bytes(raw), level)),
            _chunk(b"IEND", b""),
        ]
    )


def _chunk(tag: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + tag
        + payload
        + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
    )


class Canvas:
    """A mutable RGB raster.

    Deliberately minimal. Coordinates are clamped rather than validated,
    because a generator computing a rectangle from a hash should produce a
    slightly wrong picture rather than raise on record 4,823,913.
    """

    __slots__ = ("height", "pixels", "width")

    def __init__(self, width: int, height: int, background: RGB = (255, 255, 255)) -> None:
        self.width = max(1, width)
        self.height = max(1, height)
        self.pixels = bytearray(bytes(background) * (self.width * self.height))

    def fill(self, colour: RGB) -> None:
        self.pixels[:] = bytes(colour) * (self.width * self.height)

    def rectangle(self, x: int, y: int, width: int, height: int, colour: RGB) -> None:
        left = max(0, x)
        top = max(0, y)
        right = min(self.width, x + width)
        bottom = min(self.height, y + height)
        if right <= left or bottom <= top:
            return

        row = bytes(colour) * (right - left)
        for line in range(top, bottom):
            start = (line * self.width + left) * 3
            self.pixels[start : start + len(row)] = row

    def vertical_gradient(self, top: RGB, bottom: RGB) -> None:
        """A background that does not look like a placeholder rectangle."""
        span = max(1, self.height - 1)
        for line in range(self.height):
            ratio = line / span
            colour = bytes(
                int(top[channel] + (bottom[channel] - top[channel]) * ratio) for channel in range(3)
            )
            row = colour * self.width
            start = line * self.width * 3
            self.pixels[start : start + len(row)] = row

    def blocks(
        self, columns: int, rows: int, colours: Sequence[RGB], *, mirror: bool = True
    ) -> None:
        """Fill a grid from ``colours``, optionally mirrored left to right.

        This is what makes an identicon: a symmetric arrangement of coloured
        cells is recognisable, distinct between records, and costs nothing.
        """
        if not colours:
            return
        cell_width = max(1, self.width // max(1, columns))
        cell_height = max(1, self.height // max(1, rows))
        half = (columns + 1) // 2 if mirror else columns

        index = 0
        for row in range(rows):
            for column in range(half):
                colour = colours[index % len(colours)]
                index += 1
                self.rectangle(
                    column * cell_width, row * cell_height, cell_width, cell_height, colour
                )
                if mirror:
                    self.rectangle(
                        (columns - 1 - column) * cell_width,
                        row * cell_height,
                        cell_width,
                        cell_height,
                        colour,
                    )

    def to_png(self, *, level: int = 6) -> bytes:
        return encode_png(self.width, self.height, bytes(self.pixels), level=level)

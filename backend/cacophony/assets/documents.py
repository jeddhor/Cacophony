"""Document rendering (design document section 23).

    Structured invoice record → template renderer → PDF → optional print/scan
    degradation → rasterised page image

Cacophony generates the record; this turns it into something that looks like a
document. Two renderers:

``text``/``html``
    A template with ``{field}`` placeholders, filled from the record. Useful on
    its own and the input to the PDF renderer.

``pdf``
    A real PDF, written here rather than by a library. This wants justifying,
    because "write your own PDF" is usually the wrong instinct. What Cacophony
    needs is a page of text in one of the fourteen fonts every reader has
    built in - no images, no embedded fonts, no tables, no colour management.
    That is a few hundred lines of a well-documented format, against a
    dependency that most users generating CSV would never touch. The moment a
    project needs real typesetting, an output plugin is the right seam.

The PDFs produced are PDF 1.4, uncompressed, with an xref table - the format
every reader has handled for twenty years.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "PAGE_SIZES",
    "Document",
    "PdfPage",
    "render_pdf",
    "render_template",
]

#: Page sizes in PostScript points (1/72 inch).
PAGE_SIZES: dict[str, tuple[float, float]] = {
    "a4": (595.28, 841.89),
    "a5": (419.53, 595.28),
    "letter": (612.0, 792.0),
    "legal": (612.0, 1008.0),
}

#: The base-14 fonts a PDF reader supplies itself, so nothing is embedded.
_FONTS = {
    "helvetica": "Helvetica",
    "helvetica-bold": "Helvetica-Bold",
    "helvetica-oblique": "Helvetica-Oblique",
    "times": "Times-Roman",
    "times-bold": "Times-Bold",
    "times-italic": "Times-Italic",
    "courier": "Courier",
    "courier-bold": "Courier-Bold",
}

_PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_.]*)\}")


def render_template(template: str, values: dict[str, Any], *, on_missing: str = "empty") -> str:
    """Fill ``{field}`` placeholders from ``values``.

    Dotted names read a nested mapping, so ``{customer.name}`` works on a
    record that carries a related record. Deliberately not Jinja2: a document
    template ships inside a project file that people share, and a template
    language with expressions in it is a code-execution surface.
    """

    def substitute(match: re.Match[str]) -> str:
        name = match.group(1)
        value: Any = values
        for part in name.split("."):
            if isinstance(value, dict) and part in value:
                value = value[part]
            else:
                value = None
                break
        if value is None:
            if on_missing == "keep":
                return match.group(0)
            if on_missing == "error":
                raise KeyError(f"the template refers to '{name}', which the record does not have")
            return ""
        return str(value)

    return _PLACEHOLDER.sub(substitute, template)


@dataclass(slots=True)
class PdfPage:
    """One page of text, already laid out into lines."""

    lines: list[tuple[float, float, str, str, float]] = field(default_factory=list)
    """``(x, y, text, font, size)``, with ``y`` measured from the bottom."""

    def text(
        self, x: float, y: float, value: str, *, font: str = "helvetica", size: float = 11.0
    ) -> None:
        self.lines.append((x, y, value, _FONTS.get(font, "Helvetica"), size))


@dataclass(slots=True)
class Document:
    """A paginated text document."""

    title: str = ""
    page_size: str = "a4"
    margin: float = 56.0
    font: str = "helvetica"
    size: float = 11.0
    leading: float = 1.45
    pages: list[PdfPage] = field(default_factory=list)

    @property
    def dimensions(self) -> tuple[float, float]:
        return PAGE_SIZES.get(self.page_size.lower(), PAGE_SIZES["a4"])

    def layout(self, text: str) -> Document:
        """Flow ``text`` into pages, wrapping and breaking as needed."""
        width, height = self.dimensions
        usable = width - 2 * self.margin
        step = self.size * self.leading
        top = height - self.margin

        page = PdfPage()
        self.pages = [page]
        cursor = top

        for paragraph in text.split("\n"):
            for line in _wrap(paragraph, usable, self.size) or [""]:
                if cursor < self.margin:
                    page = PdfPage()
                    self.pages.append(page)
                    cursor = top
                page.text(self.margin, cursor, line, font=self.font, size=self.size)
                cursor -= step
        return self

    def to_pdf(self) -> bytes:
        return render_pdf(self)


def _wrap(paragraph: str, width: float, size: float) -> list[str]:
    """Greedy word wrap, measured in the average width of a Helvetica glyph.

    Approximate on purpose. Exact metrics would mean shipping the AFM tables
    for fourteen fonts to typeset synthetic invoices nobody reads closely, and
    a line that runs 4% short is not a defect worth that.
    """
    if not paragraph.strip():
        return [""]

    per_character = size * 0.5
    limit = max(1, int(width / per_character))

    lines: list[str] = []
    current = ""
    for word in paragraph.split():
        candidate = f"{current} {word}".strip()
        if len(candidate) <= limit or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def render_pdf(document: Document) -> bytes:
    """Serialise a :class:`Document` as PDF 1.4."""
    width, height = document.dimensions
    pages = document.pages or [PdfPage()]

    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    # 1 catalogue, 2 page tree, 3.. fonts, then a page and a stream for each.
    catalogue = add(b"<< /Type /Catalog /Pages 2 0 R >>")
    tree = add(b"")  # patched once the page object numbers are known

    used_fonts = {line[3] for page in pages for line in page.lines} or {"Helvetica"}
    font_numbers: dict[str, int] = {}
    for name in sorted(used_fonts):
        font_numbers[name] = add(
            b"<< /Type /Font /Subtype /Type1 /BaseFont /"
            + name.encode("ascii")
            + b" /Encoding /WinAnsiEncoding >>"
        )

    resources = (
        b"<< /Font << "
        + b" ".join(
            b"/F" + str(number).encode("ascii") + b" " + str(number).encode("ascii") + b" 0 R"
            for number in font_numbers.values()
        )
        + b" >> >>"
    )

    page_numbers: list[int] = []
    for page in pages:
        stream = _content_stream(page, font_numbers)
        content = add(
            b"<< /Length "
            + str(len(stream)).encode("ascii")
            + b" >>\nstream\n"
            + stream
            + b"\nendstream"
        )
        page_numbers.append(
            add(
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 "
                + f"{width:.2f} {height:.2f}".encode("ascii")
                + b"] /Resources "
                + resources
                + b" /Contents "
                + str(content).encode("ascii")
                + b" 0 R >>"
            )
        )

    kids = b" ".join(str(number).encode("ascii") + b" 0 R" for number in page_numbers)
    objects[tree - 1] = (
        b"<< /Type /Pages /Kids ["
        + kids
        + b"] /Count "
        + str(len(page_numbers)).encode("ascii")
        + b" >>"
    )

    info = add(
        b"<< /Producer (Cacophony) /Title (" + _escape(document.title).encode("utf-8") + b") >>"
    )

    return _assemble(objects, root=catalogue, info=info)


def _content_stream(page: PdfPage, font_numbers: dict[str, int]) -> bytes:
    parts = [b"BT"]
    for x, y, text, font, size in page.lines:
        number = font_numbers.get(font, next(iter(font_numbers.values()), 1))
        parts.append(f"/F{number} {size:.2f} Tf".encode("ascii"))
        parts.append(f"1 0 0 1 {x:.2f} {y:.2f} Tm".encode("ascii"))
        parts.append(b"(" + _escape(text).encode("latin-1", "replace") + b") Tj")
    parts.append(b"ET")
    return b"\n".join(parts)


def _escape(text: str) -> str:
    """Escape the three characters a PDF literal string cannot carry raw."""
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _assemble(objects: list[bytes], *, root: int, info: int) -> bytes:
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: list[int] = []

    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += str(number).encode("ascii") + b" 0 obj\n" + body + b"\nendobj\n"

    xref_at = len(out)
    out += b"xref\n0 " + str(len(objects) + 1).encode("ascii") + b"\n"
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode("ascii")

    out += (
        b"trailer\n<< /Size "
        + str(len(objects) + 1).encode("ascii")
        + b" /Root "
        + str(root).encode("ascii")
        + b" 0 R /Info "
        + str(info).encode("ascii")
        + b" 0 R >>\nstartxref\n"
        + str(xref_at).encode("ascii")
        + b"\n%%EOF\n"
    )
    return bytes(out)

#!/usr/bin/env python3
"""Build the Cacophony manual.

Assembles the HTML sources in ``src/`` into one document, numbers the parts,
chapters and sections, highlights the code, builds a table of contents whose
page numbers WeasyPrint fills in, and renders a PDF.

    python docs/manual/build.py [-o docs/Cacophony-Manual.pdf]

Requires WeasyPrint and Pygments. Neither is a dependency of Cacophony itself -
the manual is built, not served - so this script is run with whatever
interpreter has them, and says so plainly if it cannot find them.
"""

from __future__ import annotations

import argparse
import html
import re
import sys
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "src"
ROOT = HERE.parent.parent

#: Source order. Front matter first, then the parts, then the appendices.
PARTS = [
    "00-front.html",
    "01-beginning.html",
    "02-concepts.html",
    "03-schema.html",
    "04-generators.html",
    "05-relational.html",
    "06-worlds.html",
    "07-running.html",
    "08-scale.html",
    "09-quality.html",
    "10-interfaces.html",
    "11-extending.html",
    "12-operating.html",
    "13-appendices.html",
]


# --------------------------------------------------------------------------- #
# Slugs and numbering
# --------------------------------------------------------------------------- #


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"[^\w\s-]", "", text.lower())
    return re.sub(r"[-\s]+", "-", text).strip("-") or "section"


class Numberer:
    """Assign part, chapter and section numbers, and collect the contents.

    Numbers are computed here rather than left to CSS counters because the
    table of contents needs the same strings the headings show, and a counter
    that exists only in the stylesheet cannot be read back out.
    """

    def __init__(self) -> None:
        self.part = 0
        self.chapter = 0
        self.section = 0
        self.subsection = 0
        self.appendix = 0
        self.in_appendix = False
        self.toc: list[dict[str, str]] = []
        self.seen: set[str] = set()

    def unique(self, slug: str) -> str:
        candidate, n = slug, 2
        while candidate in self.seen:
            candidate, n = f"{slug}-{n}", n + 1
        self.seen.add(candidate)
        return candidate

    def label_chapter(self) -> str:
        if self.in_appendix:
            return chr(ord("A") + self.appendix - 1)
        return str(self.chapter)


def number_document(body: str) -> tuple[str, list[dict[str, str]]]:
    """Rewrite headings with numbers and ids, returning the contents too."""
    state = Numberer()

    part_pattern = re.compile(
        r'<section class="part-page"[^>]*>\s*<h1[^>]*>(?P<title>.*?)</h1>'
        r'(?P<rest>.*?)</section>',
        re.S,
    )

    def part_repl(match: re.Match[str]) -> str:
        # The part is numbered here but recorded in the heading pass below, so
        # that the contents come out in document order rather than parts first.
        state.part += 1
        title = match.group("title").strip()
        slug = state.unique("part-" + slugify(title))
        plain = html.escape(_plain(title), quote=True)
        return (
            f'<section class="part-page">'
            f'<div class="part-number">Part {_roman(state.part)}</div>'
            f'<h1 class="part-title" id="{slug}" data-title="{plain}" '
            f'data-part="{_roman(state.part)}">{title}</h1>'
            f'{match.group("rest")}</section>'
        )

    body = part_pattern.sub(part_repl, body)

    heading = re.compile(r"<h(?P<level>[1-4])(?P<attrs>[^>]*)>(?P<title>.*?)</h(?P=level)>", re.S)

    def repl(match: re.Match[str]) -> str:
        level = int(match.group("level"))
        attrs = match.group("attrs")
        title = match.group("title").strip()
        if "part-title" in attrs:
            given = re.search(r'id="([^"]+)"', attrs)
            roman = re.search(r'data-part="([^"]+)"', attrs)
            if given and roman:
                state.toc.append(
                    {
                        "level": "part",
                        "number": roman.group(1),
                        "title": _plain(title),
                        "id": given.group(1),
                    }
                )
            return match.group(0)
        if "no-number" in attrs:
            return match.group(0)

        if level == 1:
            if "appendix" in attrs:
                state.in_appendix = True
                state.appendix += 1
            else:
                state.chapter += 1
            state.section = state.subsection = 0
            number = state.label_chapter()
            kind = "appendix" if state.in_appendix else "chapter"
        elif level == 2:
            state.section += 1
            state.subsection = 0
            number = f"{state.label_chapter()}.{state.section}"
            kind = "section"
        elif level == 3:
            state.subsection += 1
            number = f"{state.label_chapter()}.{state.section}.{state.subsection}"
            kind = "subsection"
        else:
            return match.group(0)

        given = re.search(r'id="([^"]+)"', attrs)
        slug = state.unique(given.group(1) if given else slugify(title))
        attrs = re.sub(r'\s*id="[^"]+"', "", attrs)
        state.toc.append({"level": kind, "number": number, "title": title, "id": slug})

        plain = html.escape(_plain(title), quote=True)
        if level == 1:
            word = "Appendix" if kind == "appendix" else "Chapter"
            return (
                f'<section class="chapter-open">'
                f'<div class="chapter-eyebrow">{word}</div>'
                f'<div class="chapter-number">{number}</div>'
                f'<h1 id="{slug}"{attrs} data-title="{plain}" data-number="{number}">'
                f'<span class="heading-number">{number}</span>{title}</h1>'
                f"</section>"
            )
        return (
            f'<h{level} id="{slug}"{attrs} data-title="{plain}" data-number="{number}">'
            f'<span class="heading-number">{number}</span>{title}</h{level}>'
        )

    return heading.sub(repl, body), state.toc


def _plain(markup: str) -> str:
    """Heading text with the tags taken out, for running heads and bookmarks."""
    return html.unescape(re.sub(r"<[^>]+>", "", markup)).strip()


def _roman(n: int) -> str:
    numerals = [(10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")]
    out = ""
    for value, glyph in numerals:
        while n >= value:
            out += glyph
            n -= value
    return out


# --------------------------------------------------------------------------- #
# Contents
# --------------------------------------------------------------------------- #


def add_part_contents(body: str, toc: list[dict[str, str]]) -> str:
    """List a part's chapters on its opening page.

    A part page with nothing but a title is a page the reader turns past. With
    the chapters on it, it is the second most useful navigation surface in the
    book after the contents.
    """
    grouped: dict[str, list[dict[str, str]]] = {}
    current = None
    for entry in toc:
        if entry["level"] == "part":
            current = entry["id"]
            grouped[current] = []
        elif entry["level"] in ("chapter", "appendix") and current is not None:
            grouped[current].append(entry)

    def repl(match: re.Match[str]) -> str:
        inner = match.group(1)
        found = re.search(r'class="part-title" id="([^"]+)"', inner)
        if not found or not grouped.get(found.group(1)):
            return match.group(0)
        rows = "".join(
            f'<li><span class="part-chapter-number">{item["number"]}</span>'
            f'<span class="part-chapter-title">{item["title"]}</span></li>'
            for item in grouped[found.group(1)]
        )
        return f'<section class="part-page">{inner}<ol class="part-contents">{rows}</ol></section>'

    return re.sub(r'<section class="part-page">(.*?)</section>', repl, body, flags=re.S)


def render_toc(toc: list[dict[str, str]]) -> str:
    rows = []
    for entry in toc:
        if entry["level"] == "subsection":
            continue
        if entry["level"] == "part":
            rows.append(
                f'<li class="toc-part"><a href="#{entry["id"]}">'
                f'<span class="toc-label">Part {entry["number"]}</span>'
                f'<span class="toc-title">{entry["title"]}</span></a></li>'
            )
        elif entry["level"] in ("chapter", "appendix"):
            rows.append(
                f'<li class="toc-chapter"><a href="#{entry["id"]}">'
                f'<span class="toc-number">{entry["number"]}</span>'
                f'<span class="toc-title">{entry["title"]}</span></a></li>'
            )
        else:
            rows.append(
                f'<li class="toc-section"><a href="#{entry["id"]}">'
                f'<span class="toc-number">{entry["number"]}</span>'
                f'<span class="toc-title">{entry["title"]}</span></a></li>'
            )
    return (
        '<section class="frontmatter toc" id="contents">'
        '<h1 class="no-number">Contents</h1>'
        f'<ul class="toc-list">{"".join(rows)}</ul></section>'
    )


# --------------------------------------------------------------------------- #
# Index
# --------------------------------------------------------------------------- #


#: How an id prefix is described in the index.
INDEX_KINDS = {
    "gen-": "generator",
    "cmd-": "command",
    "key-": "schema key",
    "op-": "operation",
    "term-": "term",
}


def collect_index(body: str) -> dict[str, list[tuple[str, str]]]:
    """Index entries: every reference anchor, plus anything marked by hand.

    Anchors are named by convention - ``gen-uuid``, ``cmd-generate`` - so the
    index is derived from the document rather than maintained beside it, which
    is the only way an index of this size stays true. The term comes from the
    anchor rather than from the heading text, because a heading is a sentence
    ("Changing a dataset that exists") and an index entry is a name
    ("patches").
    """
    entries: dict[str, list[tuple[str, str]]] = {}

    def add(term: str, slug: str, kind: str) -> None:
        rows = entries.setdefault(term, [])
        if slug not in {existing for existing, _ in rows}:
            rows.append((slug, kind))

    for match in re.finditer(r'<h[1-4][^>]*\bid="([^"]+)"', body):
        slug = match.group(1)
        for prefix, kind in INDEX_KINDS.items():
            if slug.startswith(prefix):
                rest = slug[len(prefix) :]
                add(rest.replace("-", " " if kind == "term" else "_"), slug, kind)
                break

    for pattern in (
        r'<[^>]*\bdata-index="([^"]+)"[^>]*\bid="([^"]+)"',
        r'<[^>]*\bid="([^"]+)"[^>]*\bdata-index="([^"]+)"',
    ):
        for match in re.finditer(pattern, body):
            first, second = match.group(1), match.group(2)
            term, slug = (first, second) if "data-index" in match.group(0)[: match.start(1) - match.start()] else (second, first)
            add(html.unescape(term), slug, "")

    return entries


def render_index(entries: dict[str, list[tuple[str, str]]]) -> str:
    out: list[str] = ['<div class="index-columns">']
    letter = ""
    for term in sorted(entries, key=lambda t: (t.lstrip("-_").lower(), t)):
        first = term.lstrip("-_")[:1].upper() or "#"
        if first != letter:
            letter = first
            out.append(f'<div class="index-letter">{letter}</div>')
        kind = entries[term][0][1]
        refs = " ".join(f'<a href="#{slug}"></a>' for slug, _ in entries[term])
        label = f'<span class="index-kind">{kind}</span>' if kind else ""
        out.append(
            f'<div class="index-entry"><span class="index-term">{html.escape(term)}</span>'
            f"{refs}{label}</div>"
        )
    out.append("</div>")
    return "".join(out)


# --------------------------------------------------------------------------- #
# Code
# --------------------------------------------------------------------------- #


def highlight_code(body: str) -> str:
    """Colour every fenced block, leaving unlabelled ones alone."""
    from pygments import highlight
    from pygments.formatters import HtmlFormatter
    from pygments.lexers import get_lexer_by_name

    formatter = HtmlFormatter(nowrap=True, classprefix="pyg-")
    pattern = re.compile(
        r'<pre(?P<pre>[^>]*)><code class="language-(?P<lang>[\w+-]+)">(?P<code>.*?)</code></pre>',
        re.S,
    )

    def repl(match: re.Match[str]) -> str:
        code = html.unescape(match.group("code"))
        try:
            lexer = get_lexer_by_name(match.group("lang"))
        except Exception:
            return match.group(0)
        marked = highlight(code, lexer, formatter).rstrip("\n")
        return f'<pre{match.group("pre")} data-lang="{match.group("lang")}">{marked}</pre>'

    return pattern.sub(repl, body)


# --------------------------------------------------------------------------- #
# Assembly
# --------------------------------------------------------------------------- #


def assemble() -> str:
    missing = [name for name in PARTS if not (SRC / name).is_file()]
    if missing:
        raise SystemExit("missing manual sources: " + ", ".join(missing))

    pieces = [(SRC / name).read_text(encoding="utf-8") for name in PARTS]
    body = "\n".join(pieces)
    body, toc = number_document(body)

    marker = "<!--CONTENTS-->"
    if marker not in body:
        raise SystemExit("the front matter is missing its <!--CONTENTS--> marker")
    body = add_part_contents(body, toc)
    body = body.replace(marker, render_toc(toc))

    if "<!--INDEX-->" in body:
        body = body.replace("<!--INDEX-->", render_index(collect_index(body)))
    body = highlight_code(body)

    css = (HERE / "manual.css").read_text(encoding="utf-8")
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<title>Cacophony - The Manual</title>"
        f"<style>{css}</style></head><body>{body}</body></html>"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the Cacophony manual.")
    parser.add_argument("-o", "--out", type=Path, default=ROOT / "docs" / "Cacophony-Manual.pdf")
    parser.add_argument("--html", type=Path, help="Also write the assembled HTML here.")
    args = parser.parse_args()

    try:
        from weasyprint import HTML
    except ImportError:
        print(
            "WeasyPrint is not installed. It is a build dependency of the manual,\n"
            "not of Cacophony:  pip install weasyprint pygments",
            file=sys.stderr,
        )
        return 2

    document = assemble()
    if args.html:
        args.html.write_text(document, encoding="utf-8")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    HTML(string=document, base_url=str(HERE)).write_pdf(args.out)
    size = args.out.stat().st_size
    print(f"{args.out}  ({size / 1024:.0f} kB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

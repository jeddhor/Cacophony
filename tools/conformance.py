#!/usr/bin/env python3
"""Render the conformance matrix, and keep the counted claims honest.

    python tools/conformance.py            # rewrite docs/CONFORMANCE.md and the claims
    python tools/conformance.py --check    # fail if anything is out of date

Two jobs, one script, because they are the same job: saying only things that are
true. The matrix is data in ``docs/conformance.yaml`` rendered to Markdown, and
the claims - how many pages the manual has, how many tests there are, how many
generators exist - are measured and written between markers rather than typed.

Every number in this repository that drifted did so because a person typed it
once. README said a 165-page manual when it was 173; the manual said 1,560 tests
when there were 1,619. Neither was a lie when it was written.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
MATRIX = ROOT / "docs" / "conformance.yaml"
RENDERED = ROOT / "docs" / "CONFORMANCE.md"
MANUAL = ROOT / "docs" / "Cacophony-Manual.pdf"

STATES = ("implemented", "partial", "deferred", "declined")

#: How a state reads in the rendered table.
BADGE = {
    "implemented": "built",
    "partial": "partial",
    "deferred": "deferred",
    "declined": "declined",
}


# --------------------------------------------------------------------------- #
# Measuring
# --------------------------------------------------------------------------- #


def section_titles() -> dict[int, str]:
    """Every numbered section of the design document, by number."""
    spec = (ROOT / "CACOPHONY.md").read_text(encoding="utf-8")
    return {
        int(number): title.strip()
        for number, title in re.findall(r"^#{1,4}\s*(\d{1,3})\.\s+(.+?)\s*$", spec, re.M)
    }


def count_tests() -> int | None:
    """How many tests exist, asked of pytest rather than remembered."""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q", "tests/"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=600,
        )
    except (OSError, subprocess.TimeoutExpired):  # pragma: no cover - environment
        return None
    # Two shapes, because -q changes what pytest prints: a summary line, or one
    # count per file when the project's own addopts are already quiet.
    summary = re.search(r"(\d+)\s+tests? collected", result.stdout)
    if summary:
        return int(summary.group(1))
    per_file = re.findall(r"^\S+\.py: (\d+)$", result.stdout, re.M)
    return sum(int(count) for count in per_file) if per_file else None


def count_pages() -> int | None:
    """How long the manual is, asked of the PDF."""
    if not MANUAL.is_file():
        return None
    try:
        import pypdf
    except ImportError:
        # The page count lives in the PDF; without a reader, leave the existing
        # claim alone rather than replacing it with a guess.
        return None
    return len(pypdf.PdfReader(str(MANUAL)).pages)


def count_registry() -> dict[str, int | None]:
    """Sizes that are facts about the code: generators, recipes, commands."""
    counts: dict[str, int | None] = {"generators": None, "recipes": None, "commands": None}
    try:
        sys.path.insert(0, str(ROOT / "backend"))
        from cacophony.generation.registry import REGISTRY
        from cacophony.schema.recipes import load_library

        counts["generators"] = len(REGISTRY.describe())
        counts["recipes"] = len(load_library().names())

        # Counted the way the CLI presents them, which includes anything
        # registered as a sub-command rather than by decorator: the field was
        # declared here and never measured, and the manual drifted by one.
        import typer

        from cacophony.cli.main import app as cli

        counts["commands"] = len(getattr(typer.main.get_command(cli), "commands", {}))
    except Exception:  # pragma: no cover - a partial install should not fail the docs
        pass
    return counts


def measure() -> dict[str, Any]:
    matrix = load_matrix()
    tally = {state: sum(1 for row in matrix if row["state"] == state) for state in STATES}
    refusals = sum(len(row.get("refuses") or []) for row in matrix)
    numbers: dict[str, Any] = {
        "sections": len(matrix),
        "refusals": refusals,
        # The refusals as a sentence, not only as a number. Two of them were
        # added to the matrix and not to the prose that lists them, which is
        # exactly the drift these markers exist to stop.
        "refused_items": refused_items(matrix),
        **{f"sections_{state}": tally[state] for state in STATES},
    }
    pages = count_pages()
    tests = count_tests()
    if pages is not None:
        numbers["pages"] = pages
    if tests is not None:
        numbers["tests"] = tests
    numbers.update({key: value for key, value in count_registry().items() if value is not None})
    return numbers


def refused_items(matrix: list[dict[str, Any]]) -> str:
    """Every refused sub-item, in the matrix's own words, as a readable list."""
    items = [
        f"{refusal['item']} (\u00a7{row['section']})"
        for row in matrix
        for refusal in row.get("refuses") or []
    ]
    if len(items) < 2:
        return items[0] if items else "nothing"
    return ", ".join(items[:-1]) + " and " + items[-1]


# --------------------------------------------------------------------------- #
# The matrix
# --------------------------------------------------------------------------- #


def load_matrix() -> list[dict[str, Any]]:
    import yaml

    data = yaml.safe_load(MATRIX.read_text(encoding="utf-8"))
    rows = data["sections"]
    titles = section_titles()

    seen: set[int] = set()
    for row in rows:
        number = row["section"]
        if number in seen:
            raise SystemExit(f"conformance.yaml lists section {number} twice")
        seen.add(number)
        if number not in titles:
            raise SystemExit(f"conformance.yaml lists section {number}, which does not exist")
        if row["state"] not in STATES:
            raise SystemExit(
                f"section {number} has state {row['state']!r}; expected one of {STATES}"
            )
        row["title"] = titles[number]

    missing = sorted(set(titles) - seen)
    if missing:
        raise SystemExit(
            "conformance.yaml does not mention section(s): "
            + ", ".join(str(number) for number in missing)
        )
    return sorted(rows, key=lambda row: row["section"])


def render_matrix(numbers: dict[str, Any]) -> str:
    rows = load_matrix()
    lines = [
        "# Conformance",
        "",
        "Cacophony against [CACOPHONY.md](../CACOPHONY.md), section by section.",
        "",
        "*Generated from [conformance.yaml](conformance.yaml) by "
        "`python tools/conformance.py`. Do not edit this file.*",
        "",
        f"{numbers['sections']} sections: "
        f"**{numbers['sections_implemented']} built**, "
        f"**{numbers['sections_partial']} partial**, "
        f"**{numbers['sections_deferred']} deferred**, "
        f"**{numbers['sections_declined']} declined**, "
        f"with {numbers['refusals']} sub-items deliberately refused.",
        "",
        "A section is *partial* when part of what it describes is built and part",
        "is not; the note says which. A *refusal* is narrower and more",
        "deliberate: something the document asks for that was considered and",
        "declined, with the reason recorded. Those are collected at the end.",
        "",
        "| § | Section | State | Notes |",
        "|---|---|---|---|",
    ]

    for row in rows:
        note = " ".join((row.get("note") or "").split())
        if row.get("defers"):
            note += f" Not built: {', '.join(row['defers'])}."
        for refusal in row.get("refuses") or []:
            note += f" **Refused:** {refusal['item']}."
        lines.append(
            f"| {row['section']} | {row['title']} | {BADGE[row['state']]} | {note.strip()} |"
        )

    lines += ["", "## What was refused, and why", ""]
    for row in rows:
        for refusal in row.get("refuses") or []:
            why = " ".join(refusal["why"].split())
            lines += [f"**§{row['section']} — {refusal['item']}.** {why}", ""]

    lines += [
        "## What is deferred",
        "",
        "In the design, not built, and not refused - which means it is a"
        " schedule rather than a decision.",
        "",
    ]
    for row in rows:
        if row["state"] == "deferred":
            lines.append(f"- **§{row['section']} {row['title']}.** {' '.join(row['note'].split())}")
        for item in row.get("defers") or []:
            lines.append(f"- **§{row['section']}** {item}")
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Claims
# --------------------------------------------------------------------------- #

#: Files whose counted claims are rewritten, and the marker syntax each uses.
CLAIM_FILES = {
    ROOT / "README.md": ("<!-- claim:{key} -->", "<!-- /claim -->"),
    ROOT / "docs" / "manual" / "src" / "12-operating.html": (
        "<!-- claim:{key} -->",
        "<!-- /claim -->",
    ),
    ROOT / "docs" / "manual" / "src" / "00-front.html": ("<!-- claim:{key} -->", "<!-- /claim -->"),
    ROOT / "docs" / "manual" / "src" / "13-appendices.html": (
        "<!-- claim:{key} -->",
        "<!-- /claim -->",
    ),
    ROOT / "docs" / "manual" / "src" / "07-running.html": (
        "<!-- claim:{key} -->",
        "<!-- /claim -->",
    ),
}


def rewrite_claims(numbers: dict[str, Any], *, check: bool) -> list[str]:
    """Replace what is between the markers with what was measured."""
    stale: list[str] = []
    for path, (open_marker, close) in CLAIM_FILES.items():
        if not path.is_file():
            continue
        original = path.read_text(encoding="utf-8")
        updated = original
        for key, value in numbers.items():
            start = open_marker.format(key=key)
            pattern = re.compile(re.escape(start) + r".*?" + re.escape(close), re.S)
            rendered = f"{value:,}" if isinstance(value, int) and value >= 10_000 else str(value)
            updated = pattern.sub(start + rendered + close, updated)
        if updated != original:
            stale.append(str(path.relative_to(ROOT)))
            if not check:
                path.write_text(updated, encoding="utf-8")
    return stale


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Fail if anything is out of date.")
    parser.add_argument("--json", action="store_true", help="Print the measurements and stop.")
    args = parser.parse_args()

    numbers = measure()
    if args.json:
        print(json.dumps(numbers, indent=2, sort_keys=True))
        return 0

    rendered = render_matrix(numbers)
    stale = rewrite_claims(numbers, check=args.check)
    matrix_stale = not RENDERED.is_file() or RENDERED.read_text(encoding="utf-8") != rendered

    if args.check:
        if matrix_stale:
            stale.append(str(RENDERED.relative_to(ROOT)))
        if stale:
            print("out of date: " + ", ".join(stale), file=sys.stderr)
            print("run: python tools/conformance.py", file=sys.stderr)
            return 1
        print(
            f"claims are current ({numbers.get('tests', '?')} tests, "
            f"{numbers.get('pages', '?')} pages)"
        )
        return 0

    if matrix_stale:
        RENDERED.write_text(rendered, encoding="utf-8")
    print(f"{RENDERED.relative_to(ROOT)}: {numbers['sections']} sections")
    for path in stale:
        print(f"{path}: claims updated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

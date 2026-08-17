"""Transforming a dataset that already exists (sections 104, 105).

    cacophony transform out/employee.jsonl \\
        --set 'email = mask:4' --where "department == 'Finance'" \\
        --out out/employee.masked.jsonl

**It streams.** A transform over forty gigabytes must not materialise it, so
records are read one at a time, patched, and written on. Memory is bounded by
one record whatever the file size - the same requirement section 31 puts on
generation, for the same reason.

**It never writes over its input.** A transform that failed halfway through an
in-place rewrite would leave a file that is neither the old dataset nor the new
one, and there is nothing to recover it from short of regenerating. In-place is
therefore done by writing beside and swapping at the end, so the original
survives until the new file is complete.

**It records what it did.** A transformed dataset carries a sidecar naming the
rules that produced it, because a masked column is indistinguishable from a
column that was always masked - and somebody looking at the file in three months
needs to be able to tell.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..core.errors import OutputError
from .rules import PatchSet

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterator, Sequence

    from .rules import PatchRule

__all__ = ["TRANSFORMABLE", "TransformResult", "transform_file"]

#: Formats a transform can read and write a record at a time.
#:
#: Parquet is absent on purpose: its records live in column chunks, so a
#: streaming row-by-row rewrite would mean decoding and re-encoding every
#: chunk - which is a different piece of work from this one, and doing it badly
#: would silently lose the schema. Convert to JSON Lines, transform, convert
#: back.
TRANSFORMABLE = ("jsonl", "ndjson", "csv", "json")

#: Where the record of what happened goes.
SIDECAR_SUFFIX = ".transform.json"


@dataclass(slots=True)
class TransformResult:
    """What a transform pass did."""

    source: Path
    destination: Path
    fmt: str
    read: int = 0
    written: int = 0
    edited: int = 0
    dropped: int = 0
    values_changed: int = 0
    rules: list[str] = field(default_factory=list)
    by_rule: dict[str, int] = field(default_factory=dict)
    sidecar: Path | None = None

    @property
    def unchanged(self) -> int:
        return self.written - self.edited

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": str(self.source),
            "destination": str(self.destination),
            "format": self.fmt,
            "records_read": self.read,
            "records_written": self.written,
            "records_edited": self.edited,
            "records_dropped": self.dropped,
            "values_changed": self.values_changed,
            "rules": list(self.rules),
            "by_rule": dict(self.by_rule),
        }


def transform_file(
    source: str | Path,
    rules: Sequence[PatchRule],
    *,
    destination: str | Path | None = None,
    fmt: str | None = None,
    in_place: bool = False,
    overwrite: bool = False,
    entity: str = "",
    write_sidecar: bool = True,
) -> TransformResult:
    """Apply ``rules`` to every record of a file."""
    path = Path(source)
    if not path.is_file():
        raise OutputError(f"no such file: {path}")

    kind = (fmt or _format_of(path)).lower()
    if kind not in TRANSFORMABLE:
        raise OutputError(
            f"'{kind}' cannot be transformed a record at a time. Transformable: "
            f"{', '.join(TRANSFORMABLE)}. Convert to jsonl, transform, convert back."
        )

    if in_place and destination is not None:
        raise OutputError("pass either --out or --in-place, not both")
    if not in_place and destination is None:
        raise OutputError("a transform needs somewhere to write: pass --out or --in-place")

    target = path if in_place else Path(destination)  # type: ignore[arg-type]
    if not in_place and target.exists() and not overwrite:
        raise OutputError(f"{target} already exists. Pass --force to replace it.")
    if target.resolve() == path.resolve() and not in_place:
        raise OutputError("the destination is the source. Pass --in-place if that is the intent.")

    patches = PatchSet(rules, entity=entity)
    result = TransformResult(
        source=path,
        destination=target,
        fmt=kind,
        rules=[rule.name for rule in patches.rules],
    )

    # Always written beside, never over. An in-place transform that failed
    # halfway would otherwise destroy the only copy.
    scratch = target.with_name(target.name + ".partial")
    scratch.parent.mkdir(parents=True, exist_ok=True)

    try:
        _run(path, scratch, kind, patches, result)
    except BaseException:
        scratch.unlink(missing_ok=True)
        raise

    scratch.replace(target)

    result.edited = patches.stats.records_edited
    result.dropped = patches.stats.records_dropped
    result.values_changed = patches.stats.values_changed
    result.by_rule = dict(patches.stats.by_rule)

    if write_sidecar:
        result.sidecar = _write_sidecar(target, result)
    return result


def _run(
    source: Path,
    scratch: Path,
    kind: str,
    patches: PatchSet,
    result: TransformResult,
) -> None:
    """Stream records from ``source`` through ``patches`` into ``scratch``."""
    if kind in ("jsonl", "ndjson"):
        with source.open(encoding="utf-8") as reader, scratch.open("w", encoding="utf-8") as out:
            for record in _read_jsonl(reader):
                result.read += 1
                patched = patches.apply(record)
                if patched is None:
                    continue
                out.write(json.dumps(patched, ensure_ascii=False, default=str) + "\n")
                result.written += 1
        return

    if kind == "csv":
        import csv

        with source.open(newline="", encoding="utf-8") as reader:
            rows = csv.DictReader(reader)
            columns = list(rows.fieldnames or [])
            with scratch.open("w", newline="", encoding="utf-8") as handle:
                csv_writer = csv.DictWriter(handle, fieldnames=columns)
                csv_writer.writeheader()
                for row in rows:
                    result.read += 1
                    patched = patches.apply(dict(row))
                    if patched is None:
                        continue
                    # A rule may add a column; CSV cannot grow one mid-file, so
                    # say so rather than dropping the value silently.
                    unknown = set(patched) - set(columns)
                    if unknown:
                        raise OutputError(
                            f"a rule set {', '.join(sorted(unknown))}, which is not a column "
                            f"of {source.name}. CSV has a fixed header; use jsonl to add fields."
                        )
                    csv_writer.writerow({name: _flatten(patched.get(name)) for name in columns})
                    result.written += 1
        return

    # A JSON array. Read incrementally where possible; a top-level array has to
    # be parsed as one document, which is a limitation of the format rather than
    # of this code - and the reason `jsonl` is the default everywhere else.
    payload = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise OutputError(f"{source} is not a JSON array of records")
    kept: list[dict[str, Any]] = []
    for record in payload:
        result.read += 1
        if not isinstance(record, dict):
            raise OutputError(f"{source}: element {result.read} is not an object")
        patched = patches.apply(dict(record))
        if patched is not None:
            kept.append(patched)
            result.written += 1
    scratch.write_text(
        json.dumps(kept, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
    )


def _read_jsonl(reader: Any) -> Iterator[dict[str, Any]]:
    for number, line in enumerate(reader, start=1):
        text = line.strip()
        if not text:
            continue
        try:
            record = json.loads(text)
        except json.JSONDecodeError as exc:
            raise OutputError(f"line {number} is not valid JSON: {exc}") from exc
        if not isinstance(record, dict):
            raise OutputError(f"line {number} is not a JSON object")
        yield record


def _flatten(value: Any) -> Any:
    """CSV holds text; a nested value becomes JSON rather than a repr."""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return "" if value is None else value


def _format_of(path: Path) -> str:
    suffix = path.suffix.lower().lstrip(".")
    if suffix in ("jsonl", "ndjson"):
        return "jsonl"
    if suffix in ("csv", "json"):
        return suffix
    raise OutputError(f"cannot tell the format of {path.name}. Pass --format jsonl, csv or json.")


def _write_sidecar(target: Path, result: TransformResult) -> Path:
    """Record what produced this file.

    A masked column is indistinguishable from a column that was always masked,
    so a transformed dataset says which rules were applied to it. Provenance
    about the *dataset* rather than about a record, which is what section 60
    covers.
    """
    from datetime import UTC, datetime

    from .. import __version__

    path = target.with_name(target.name + SIDECAR_SUFFIX)
    payload = {
        "cacophony_version": __version__,
        "transformed_at": datetime.now(UTC).isoformat(timespec="seconds"),
        **result.to_dict(),
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path

"""Putting a distributed run's parts back together (design document section 95).

Workers write shard-private files. This turns a directory of them into the
dataset a single machine would have produced - and, for the line-oriented
formats, produces a file that is *byte-identical* to it.

That claim is the whole point of the phase, so it is worth being precise about
why it holds. Record *n*'s seed is derived by hashing *n* (section 75), so
record *n* is the same record whichever worker built it. Shards are contiguous
index ranges that tile the entity exactly once. Concatenating them in offset
order therefore reproduces the single-machine byte stream, provided the format
has no per-file framing that concatenation would repeat - which is why CSV
headers are dropped after the first part, JSON arrays are re-bracketed, and
Parquet is left as a directory of parts rather than pretended into one file.

Assembly is a convenience, not a requirement. Every reader Cacophony writes for
- and every reader worth using - takes a directory of parts. A run that ends
with three hundred ``.jsonl`` files is already finished.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from ..core.errors import OutputError

__all__ = ["AssemblyResult", "assemble", "shard_parts"]

#: ``employee.part000050000.jsonl``
_PART = re.compile(r"^(?P<entity>.+)\.part(?P<offset>\d+)(?P<suffix>\.[A-Za-z0-9]+)$")

#: Formats a concatenation can legitimately produce.
CONCATENABLE = frozenset({"jsonl", "ndjson", "csv", "json"})


@dataclass(slots=True)
class AssemblyResult:
    """What assembly produced for one entity."""

    entity: str
    path: Path
    parts: int
    records: int

    def to_dict(self) -> dict[str, object]:
        return {
            "entity": self.entity,
            "path": str(self.path),
            "parts": self.parts,
            "records": self.records,
        }


def shard_parts(directory: str | Path, entity: str, suffix: str) -> list[Path]:
    """One entity's part files, in dataset order.

    Sorted on the offset as a *number*. The zero padding in the filename means
    a lexical sort would usually agree, and 'usually' is not a property worth
    relying on when the failure mode is a silently reordered dataset.
    """
    found: list[tuple[int, Path]] = []
    for path in Path(directory).glob(f"{entity}.part*{suffix}"):
        match = _PART.match(path.name)
        if match and match.group("entity") == entity:
            found.append((int(match.group("offset")), path))
    return [path for _offset, path in sorted(found)]


def assemble(
    directory: str | Path,
    entity: str,
    fmt: str = "jsonl",
    *,
    destination: str | Path | None = None,
    remove_parts: bool = False,
) -> AssemblyResult:
    """Join one entity's parts into a single file."""
    fmt = fmt.lower()
    if fmt not in CONCATENABLE:
        raise OutputError(
            f"'{fmt}' parts cannot be concatenated - the format has a per-file footer. "
            "Read the directory of parts instead; every reader for this format accepts one."
        )

    directory = Path(directory)
    suffix = ".jsonl" if fmt in ("jsonl", "ndjson") else f".{fmt}"
    parts = shard_parts(directory, entity, suffix)
    if not parts:
        raise OutputError(f"no {entity} parts found in {directory}")

    target = Path(destination) if destination else directory / f"{entity}{suffix}"
    if target in parts:
        raise OutputError(f"refusing to assemble {entity} parts over one of themselves: {target}")

    records = _JOINERS[fmt](parts, target)

    if remove_parts:
        for part in parts:
            part.unlink(missing_ok=True)

    return AssemblyResult(entity=entity, path=target, parts=len(parts), records=records)


def _join_lines(parts: list[Path], target: Path) -> int:
    """Concatenate byte for byte.

    Copied in chunks rather than read into memory: a hundred-gigabyte dataset
    is exactly the kind that got distributed in the first place.
    """
    records = 0
    with target.open("wb") as out:
        for part in parts:
            with part.open("rb") as handle:
                records += _copy_counting_lines(handle, out)
    return records


def _copy_counting_lines(handle: BinaryIO, out: BinaryIO, chunk: int = 1 << 20) -> int:
    lines = 0
    trailing_newline = True
    while True:
        block = handle.read(chunk)
        if not block:
            break
        lines += block.count(b"\n")
        trailing_newline = block.endswith(b"\n")
        out.write(block)
    if not trailing_newline:
        # A part without a final newline would glue its last record to the next
        # part's first one.
        out.write(b"\n")
        lines += 1
    return lines


def _join_csv(parts: list[Path], target: Path) -> int:
    """Concatenate, keeping exactly one header."""
    records = 0
    with target.open("wb") as out:
        for position, part in enumerate(parts):
            with part.open("rb") as handle:
                header = handle.readline()
                if position == 0:
                    out.write(header)
                records += _copy_counting_lines(handle, out)
    return records


def _join_json(parts: list[Path], target: Path) -> int:
    """Re-bracket a set of JSON arrays into one.

    The only format here that cannot be joined without parsing, and the reason
    ``jsonl`` is the default for distributed runs.
    """
    records = 0
    with target.open("w", encoding="utf-8") as out:
        out.write("[\n")
        first = True
        for part in parts:
            rows = json.loads(part.read_text(encoding="utf-8"))
            for row in rows:
                if not first:
                    out.write(",\n")
                out.write(json.dumps(row, ensure_ascii=False, default=str))
                first = False
                records += 1
        out.write("\n]\n")
    return records


_JOINERS = {
    "jsonl": _join_lines,
    "ndjson": _join_lines,
    "csv": _join_csv,
    "json": _join_json,
}


def collect_assets(sources: list[str | Path], destination: str | Path) -> int:
    """Gather several workers' asset directories into one (section 95).

    Shared artifact storage, for deployments that do not have any. A worker
    writing to a mounted volume needs none of this; a worker on a machine of
    its own needs its images collected before the dataset means anything.

    Files are addressed by content hash (section 81), so two workers producing
    the same asset produce the same filename, and a collision is agreement
    rather than conflict.
    """
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    copied = 0
    for source in sources:
        source = Path(source)
        if not source.is_dir():
            continue
        for path in source.rglob("*"):
            if not path.is_file():
                continue
            target = destination / path.relative_to(source)
            if target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            copied += 1
    return copied

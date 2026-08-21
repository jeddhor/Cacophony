"""Remembering which values a unique field has already produced.

Section 31 says memory must not grow with the dataset, and enforcing
``unique: true`` is the one check that argued with it: a set of every value seen
is exactly a structure whose size is the number of records. On a ten-million-row
run that is several hundred megabytes of Python objects held for the duration,
which is the difference between "a dataset larger than RAM costs the same as a
small one" being true and being nearly true.

The answer here keeps the check exact and moves the memory. Values live in a set
until it reaches a stated ceiling; after that the whole set is written to a
SQLite table with a unique index and every later value is asked of that. Most
runs never reach the ceiling and pay nothing; the ones that do trade memory for
disk and a slower check, which is the right trade for a run that was going to
take a while anyway.

Exactness is not negotiated away. A Bloom filter would be smaller and faster and
would report duplicates that are not duplicates, and a validator that cries wolf
about a primary key is worse than no validator. What is stored is the value, not
a digest of it, so a collision cannot be mistaken for a repeat.
"""

from __future__ import annotations

import contextlib
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

__all__ = ["DEFAULT_MEMORY_CEILING", "UniqueTracker"]

#: How many values one field may hold in memory before the set is spilled to
#: disk. Two hundred and fifty thousand short strings is on the order of tens of
#: megabytes - large enough that ordinary runs never spill, small enough that a
#: run which does has not already lost.
DEFAULT_MEMORY_CEILING = 250_000


def _key(value: Any) -> str:
    """A stable text form that cannot confuse one type for another.

    ``1`` and ``"1"`` are different values and must not collide, so the type is
    part of the key. Lists and dicts arrive from array and object fields and are
    made hashable by rendering them.
    """
    if isinstance(value, (list, tuple)):
        return "l:" + repr([_key(item) for item in value])
    if isinstance(value, dict):
        return "d:" + repr(sorted((str(k), _key(v)) for k, v in value.items()))
    return f"{type(value).__name__}:{value}"


class UniqueTracker:
    """One field's seen values: in memory, then on disk.

    ``add`` returns True when the value is new. ``forget`` gives values back,
    which a record discarded after validation needs - otherwise it holds a value
    on behalf of a record nobody has.
    """

    def __init__(
        self,
        field: str,
        *,
        memory_ceiling: int = DEFAULT_MEMORY_CEILING,
        directory: str | Path | None = None,
    ) -> None:
        self.field = field
        self.memory_ceiling = max(1, memory_ceiling)
        self._directory = directory
        self._memory: set[str] = set()
        self._connection: sqlite3.Connection | None = None
        self._path: Path | None = None
        #: A temporary directory this tracker made for itself, to be removed
        #: with it. ``None`` when the caller named one.
        self._scratch: Path | None = None
        self.spilled_at: int | None = None

    # -- the check ---------------------------------------------------------- #

    def add(self, value: Any) -> bool:
        key = _key(value)
        if self._connection is None:
            if key in self._memory:
                return False
            self._memory.add(key)
            if len(self._memory) > self.memory_ceiling:
                self._spill()
            return True

        try:
            self._connection.execute("INSERT INTO seen (key) VALUES (?)", (key,))
        except sqlite3.IntegrityError:
            return False
        return True

    def forget(self, values: list[Any]) -> None:
        """Give values back, for a record that was not kept."""
        keys = [_key(value) for value in values]
        if self._connection is None:
            self._memory.difference_update(keys)
            return
        self._connection.executemany("DELETE FROM seen WHERE key = ?", [(key,) for key in keys])

    # -- state -------------------------------------------------------------- #

    @property
    def held_in_memory(self) -> int:
        return len(self._memory)

    def summary(self) -> dict[str, Any]:
        return {
            "field": self.field,
            # Whether it spilled during the run, not whether the file is still
            # open: the run report is written after the run has let go of it.
            "spilled": self.spilled_at is not None,
            "spilled_after": self.spilled_at,
            "held_in_memory": len(self._memory),
        }

    def reset(self) -> None:
        self.close()
        self._memory = set()
        self.spilled_at = None

    def close(self) -> None:
        """Release the spill file, the connection and the directory holding them.

        Called when a run ends rather than left to the garbage collector: the
        API keeps finished runs around to read their metrics from, so nothing
        was collecting these, and a long-lived server accumulated one open
        database and one temporary directory per spilled field.
        """
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        if self._path is not None:
            # A scratch file, and nothing else refers to it.
            self._path.unlink(missing_ok=True)
            self._path = None
        if self._scratch is not None:
            with contextlib.suppress(OSError):
                self._scratch.rmdir()
            self._scratch = None

    # -- internals ---------------------------------------------------------- #

    def _spill(self) -> None:
        """Move everything to disk and keep going from there."""
        borrowed = self._directory is not None
        directory = Path(self._directory) if self._directory else Path(tempfile.mkdtemp())
        directory.mkdir(parents=True, exist_ok=True)
        #: Removed on close, but only if this tracker is what created it: a
        #: directory the caller named belongs to the caller.
        self._scratch = None if borrowed else directory
        self._path = directory / f"unique-{self.field}-{id(self):x}.db"

        connection = sqlite3.connect(str(self._path))
        # Durability buys nothing here: the file is scratch, and losing it to a
        # crash costs a run that has already stopped.
        connection.execute("PRAGMA journal_mode = OFF")
        connection.execute("PRAGMA synchronous = OFF")
        connection.execute("CREATE TABLE seen (key TEXT PRIMARY KEY) WITHOUT ROWID")
        connection.executemany(
            "INSERT OR IGNORE INTO seen (key) VALUES (?)", ((key,) for key in self._memory)
        )
        connection.commit()

        self.spilled_at = len(self._memory)
        self._connection = connection
        self._memory = set()

    def __del__(self) -> None:  # pragma: no cover - best effort
        with contextlib.suppress(Exception):
            self.close()

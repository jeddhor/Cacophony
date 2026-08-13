"""The generation cache (design document section 76).

Generative content is expensive. A schema that asks a 8B model for ten thousand
biographies costs real minutes of GPU time, and re-running the same project
after fixing an unrelated field should not pay that again.

Requests are keyed by content: provider, model, prompt, generation settings and
seed. Anything that could change the answer is in the key, so a cache hit is
always a legitimate answer to *this* question, not a nearly-right answer to a
similar one.

Modes (section 76)::

    disabled     never read, never write
    read_only    serve hits, but do not record new entries
    read_write   serve hits and record misses

Storage is one SQLite file. Thousands of small files in a directory would be
simpler to write and considerably worse to live with: slower to enumerate, ugly
to clean up, and prone to inode exhaustion on a long-running box.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

__all__ = ["CacheMode", "CacheStats", "GenerationCache", "cache_key"]


class CacheMode(StrEnum):
    DISABLED = "disabled"
    READ_ONLY = "read_only"
    READ_WRITE = "read_write"

    @property
    def reads(self) -> bool:
        return self is not CacheMode.DISABLED

    @property
    def writes(self) -> bool:
        return self is CacheMode.READ_WRITE


@dataclass(slots=True)
class CacheStats:
    hits: int = 0
    misses: int = 0
    writes: int = 0

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "writes": self.writes,
            "hit_rate": round(self.hit_rate, 4),
        }


def cache_key(
    *,
    provider: str,
    model: str | None,
    prompt: str,
    settings: dict[str, Any] | None = None,
    seed: int | None = None,
) -> str:
    """Derive a content key from everything that could change the answer.

    Settings are serialised with sorted keys so that two logically identical
    requests written in a different order share a key.
    """
    hasher = hashlib.blake2b(digest_size=20)
    for part in (
        provider,
        model or "",
        prompt,
        json.dumps(settings or {}, sort_keys=True, default=str),
        "" if seed is None else str(seed),
    ):
        encoded = part.encode("utf-8")
        hasher.update(len(encoded).to_bytes(4, "little"))
        hasher.update(encoded)
    return hasher.hexdigest()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    key         TEXT PRIMARY KEY,
    provider    TEXT NOT NULL,
    model       TEXT,
    payload     TEXT NOT NULL,
    created_at  REAL NOT NULL,
    hits        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS entries_provider ON entries(provider);
CREATE INDEX IF NOT EXISTS entries_created ON entries(created_at);
"""


class GenerationCache:
    """A content-addressed cache for provider responses."""

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        mode: CacheMode | str = CacheMode.READ_WRITE,
    ) -> None:
        self.mode = CacheMode(mode)
        self.path = Path(path) if path is not None else None
        self.stats = CacheStats()
        self._lock = threading.Lock()
        self._memory: dict[str, str] = {}
        self._connection: sqlite3.Connection | None = None

        if self.path is not None and self.mode.reads:
            self._open()

    # -- storage ------------------------------------------------------------ #

    def _open(self) -> None:
        assert self.path is not None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False because the engine may hand batches to a
        # thread pool later; every access is already under self._lock.
        self._connection = sqlite3.connect(str(self.path), check_same_thread=False)
        self._connection.executescript(_SCHEMA)
        self._connection.commit()

    @property
    def is_persistent(self) -> bool:
        return self._connection is not None

    # -- access ------------------------------------------------------------- #

    def get(self, key: str) -> Any | None:
        """Return the cached payload for ``key``, or ``None`` on a miss."""
        if not self.mode.reads:
            return None

        with self._lock:
            raw = self._memory.get(key)
            if raw is None and self._connection is not None:
                row = self._connection.execute(
                    "SELECT payload FROM entries WHERE key = ?", (key,)
                ).fetchone()
                if row is not None:
                    raw = row[0]
                    self._memory[key] = raw
                    self._connection.execute(
                        "UPDATE entries SET hits = hits + 1 WHERE key = ?", (key,)
                    )

            if raw is None:
                self.stats.misses += 1
                return None

            self.stats.hits += 1

        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            # A corrupt entry is a cache problem, not a data problem: drop it
            # and let the caller regenerate.
            self.delete(key)
            return None

    def put(self, key: str, payload: Any, *, provider: str = "", model: str | None = None) -> None:
        if not self.mode.writes:
            return
        raw = json.dumps(payload, default=str)
        with self._lock:
            self._memory[key] = raw
            self.stats.writes += 1
            if self._connection is not None:
                self._connection.execute(
                    "INSERT OR REPLACE INTO entries "
                    "(key, provider, model, payload, created_at, hits) VALUES (?, ?, ?, ?, ?, 0)",
                    (key, provider, model, raw, time.time()),
                )
                self._connection.commit()

    def delete(self, key: str) -> None:
        with self._lock:
            self._memory.pop(key, None)
            if self._connection is not None:
                self._connection.execute("DELETE FROM entries WHERE key = ?", (key,))
                self._connection.commit()

    def clear(self) -> None:
        with self._lock:
            self._memory.clear()
            if self._connection is not None:
                self._connection.execute("DELETE FROM entries")
                self._connection.commit()
            self.stats = CacheStats()

    def __len__(self) -> int:
        with self._lock:
            if self._connection is not None:
                return int(self._connection.execute("SELECT COUNT(*) FROM entries").fetchone()[0])
            return len(self._memory)

    def __bool__(self) -> bool:
        """A cache is always truthy, however empty it is.

        Without this, ``__len__`` makes a brand-new cache falsy, and the
        entirely natural ``cache or default_cache()`` silently throws away the
        caller's cache on its first run - when it is empty by definition.
        """
        return True

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def describe(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "path": str(self.path) if self.path else None,
            "entries": len(self),
            **self.stats.to_dict(),
        }

    def __enter__(self) -> GenerationCache:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

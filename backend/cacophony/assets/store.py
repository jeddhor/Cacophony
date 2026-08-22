"""The asset store (design document sections 19, 60, 72, 81).

    Employee
       ├── portrait.png
       ├── id_badge.pdf
       ├── voicemail.wav
       └── signature.png

One record can own several files. Section 81 asks that assets reference their
parent so the store can answer "what belongs to E48291?" without scanning the
dataset, and section 72 asks that generated media live beside a project rather
than inside its metadata database.

Three decisions worth stating.

**Paths are derived, not allocated.** An asset's filename comes from its entity,
record index and field, so the same record always writes the same file. That is
what lets an interrupted run resume without orphaning what it already wrote,
and it means the path can be computed *before* the expensive part - so a run
that already has an asset can skip generating it again.

**Content is addressed by hash.** A thousand employees given the same
placeholder portrait cost one file, not a thousand. Deduplication is by digest
of the bytes, so it is exact rather than a guess, and the manifest records
every logical asset that points at each stored file.

**The manifest is a sidecar, not a database.** ``assets/manifest.jsonl`` is one
line per asset - readable, appendable, diffable, and no more privileged than
the files it describes. Section 42 is explicit that generated data does not
belong in the metadata store.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import threading
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..core.errors import OutputError
from ..core.record import GeneratedAsset

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterator

__all__ = [
    "MANIFEST_NAME",
    "AssetStats",
    "AssetStore",
    "StoredAsset",
    "extension_for",
]

#: One line per asset, beside the assets themselves.
MANIFEST_NAME = "manifest.jsonl"

#: Media type to file extension. Only what Cacophony can actually produce; an
#: unknown type gets ``.bin`` rather than a guess that misleads a file browser.
_EXTENSIONS: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/flac": ".flac",
    "audio/mpeg": ".mp3",
    "audio/ogg": ".ogg",
    "application/pdf": ".pdf",
    "text/html": ".html",
    "text/plain": ".txt",
    "application/json": ".json",
}


def extension_for(media_type: str | None) -> str:
    """The file extension for a media type."""
    if not media_type:
        return ".bin"
    return _EXTENSIONS.get(media_type.split(";")[0].strip().lower(), ".bin")


@dataclass(slots=True)
class AssetStats:
    """What the store did, for the run summary."""

    written: int = 0
    deduplicated: int = 0
    reused: int = 0
    bytes_written: int = 0
    #: Counted by kind, for section 55's image and audio rates. Audio is kept
    #: in seconds of material, because "clips per minute" says nothing about
    #: how much audio a run is producing.
    images: int = 0
    audio_clips: int = 0
    audio_seconds: float = 0.0

    @property
    def total(self) -> int:
        return self.written + self.deduplicated + self.reused

    def to_dict(self) -> dict[str, Any]:
        return {
            "assets": self.total,
            "files_written": self.written,
            "deduplicated": self.deduplicated,
            "reused_from_disk": self.reused,
            "bytes_written": self.bytes_written,
            "images": self.images,
            "audio_clips": self.audio_clips,
            "audio_seconds": round(self.audio_seconds, 2),
        }


@dataclass(slots=True)
class StoredAsset:
    """One asset, as the manifest records it."""

    entity: str
    record_index: int
    field: str
    kind: str
    path: Path
    media_type: str
    size_bytes: int
    digest: str
    record_id: str | None = None
    metadata: dict[str, Any] = dataclass_field(default_factory=dict)

    def to_asset(self, *, relative_to: Path | None = None) -> GeneratedAsset:
        """The record-facing view of this asset (section 99)."""
        path = self.path
        if relative_to is not None:
            with contextlib.suppress(ValueError):
                path = self.path.relative_to(relative_to)
        return GeneratedAsset(
            field=self.field,
            kind=self.kind,
            path=path,
            media_type=self.media_type,
            size_bytes=self.size_bytes,
            metadata=dict(self.metadata),
        )

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "entity": self.entity,
            "record_index": self.record_index,
            "field": self.field,
            "kind": self.kind,
            "path": str(self.path),
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
            "digest": self.digest,
        }
        if self.record_id is not None:
            data["record_id"] = self.record_id
        if self.metadata:
            data["metadata"] = self.metadata
        return data


class AssetStore:
    """Where generated media goes, and what is known about it.

    Built once per run and shared by every media generator. Thread-safe because
    a run may write assets from more than one entity worker at a time, and the
    manifest is a single append-only file.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        deduplicate: bool = True,
        manifest: bool = True,
        manifest_name: str | None = None,
        overwrite: bool = False,
    ) -> None:
        self.root = Path(root)
        #: Which manifest file *this* store appends to. A distributed run
        #: (section 95) points several nodes at one shared directory, and two
        #: machines appending to one file over a network filesystem can
        #: interleave a line. Each node writes ``manifest.<node>.jsonl``
        #: instead; :meth:`manifest` reads all of them, so a reader cannot tell
        #: whether the run was distributed.
        self.manifest_name = manifest_name or MANIFEST_NAME
        #: Identical bytes are stored once. Placeholder portraits and silent
        #: audio are common enough that this is worth doing by default.
        self.deduplicate = deduplicate
        self.write_manifest = manifest
        #: Rewrite a file that already exists. Off by default so a resumed run
        #: keeps what it produced rather than paying for it twice.
        self.overwrite = overwrite

        self.stats = AssetStats()
        self._digests: dict[str, Path] = {}
        self._lock = threading.Lock()
        self._manifest: Any = None
        self._opened = False

    # -- lifecycle ---------------------------------------------------------- #

    def open(self) -> None:
        if self._opened:
            return
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise OutputError(f"could not create the asset directory {self.root}: {exc}") from exc
        self._opened = True

    def close(self) -> None:
        with self._lock:
            if self._manifest is not None:
                self._manifest.close()
                self._manifest = None
        self._opened = False

    def __enter__(self) -> AssetStore:
        self.open()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- paths -------------------------------------------------------------- #

    def path_for(
        self,
        entity: str,
        record_index: int,
        field_name: str,
        *,
        media_type: str | None = None,
        extension: str | None = None,
    ) -> Path:
        """Where this record's asset for this field belongs.

        Derived rather than allocated, so it is the same on every run and can
        be computed before anything expensive happens. Records are foldered in
        groups of a thousand: a directory holding ten million files is one that
        most tools refuse to list.
        """
        suffix = extension or extension_for(media_type)
        bucket = f"{record_index // 1000 * 1000:08d}"
        name = f"{entity}_{record_index:08d}_{field_name}{suffix}"
        return self.root / entity / bucket / name

    def exists(self, path: Path) -> bool:
        return path.exists() and path.stat().st_size > 0

    def note_reuse(self, stored: StoredAsset) -> StoredAsset:
        """Record an asset that was already on disk and so was never generated.

        A generator that finds its file present returns before
        :meth:`write` is reached, which is the whole point - the second run of
        a portrait-heavy project is hundreds of times faster than the first.
        Counting it here is what makes that saving visible instead of a
        suspicious absence in the summary.
        """
        with self._lock:
            self.stats.reused += 1
            self._count(stored)
            self._record(stored)
        return stored

    # -- writing ------------------------------------------------------------ #

    def write(
        self,
        data: bytes,
        *,
        entity: str,
        record_index: int,
        field_name: str,
        kind: str,
        media_type: str,
        record_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        overwrite: bool | None = None,
    ) -> StoredAsset:
        """Store ``data`` and return what was stored.

        ``overwrite`` lets one caller override the store's default. A field
        that asked to be regenerated has already paid for the bytes, and
        declining to write them would be the worst of both.

        Deduplication links rather than copies where the filesystem allows it,
        and falls back to copying where it does not - a Windows volume or a
        filesystem without hard links should cost disk space, not correctness.
        """
        self.open()
        path = self.path_for(entity, record_index, field_name, media_type=media_type)
        digest = hashlib.blake2b(data, digest_size=16).hexdigest()

        stored = StoredAsset(
            entity=entity,
            record_index=record_index,
            field=field_name,
            kind=kind,
            path=path,
            media_type=media_type,
            size_bytes=len(data),
            digest=digest,
            record_id=record_id,
            metadata=dict(metadata or {}),
        )

        replace = self.overwrite if overwrite is None else overwrite
        with self._lock:
            if not replace and self.exists(path):
                self.stats.reused += 1
                self._count(stored)
                self._record(stored)
                return stored

            twin = self._digests.get(digest) if self.deduplicate else None
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                if twin is not None and twin != path and twin.exists():
                    _link_or_copy(twin, path)
                    self.stats.deduplicated += 1
                else:
                    path.write_bytes(data)
                    self.stats.written += 1
                    self.stats.bytes_written += len(data)
                    if self.deduplicate:
                        self._digests[digest] = path
            except OSError as exc:
                raise OutputError(f"could not write the asset {path}: {exc}") from exc

            self._count(stored)
            self._record(stored)
        return stored

    def _count(self, stored: StoredAsset) -> None:
        """Tally an asset by kind. Called with the lock held.

        Counted however it was stored - written, deduplicated or reused - because
        the question these answer is "what is this run producing", and a
        deduplicated image is still an image the run produced.
        """
        if stored.kind == "image" or stored.media_type.startswith("image/"):
            self.stats.images += 1
        elif stored.kind in ("audio", "speech") or stored.media_type.startswith("audio/"):
            self.stats.audio_clips += 1
            seconds = (stored.metadata or {}).get("duration_seconds")
            if isinstance(seconds, (int, float)):
                self.stats.audio_seconds += float(seconds)

    def _record(self, stored: StoredAsset) -> None:
        """Append one manifest line. Called with the lock held."""
        if not self.write_manifest:
            return
        if self._manifest is None:
            try:
                self._manifest = (self.root / self.manifest_name).open("a", encoding="utf-8")
            except OSError as exc:
                raise OutputError(f"could not open the asset manifest: {exc}") from exc
        self._manifest.write(json.dumps(stored.to_dict(), ensure_ascii=False) + "\n")
        self._manifest.flush()

    # -- reading ------------------------------------------------------------ #

    def manifest(self) -> Iterator[dict[str, Any]]:
        """Every asset this store has recorded.

        Reads every manifest in the directory, not only this store's, so a
        directory several nodes wrote into reads back as one run.
        """
        stem = Path(MANIFEST_NAME).stem
        paths = sorted({*self.root.glob(f"{stem}.jsonl"), *self.root.glob(f"{stem}.*.jsonl")})
        for path in paths:
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if line:
                        yield json.loads(line)

    def assets_of(self, entity: str, record_index: int) -> list[dict[str, Any]]:
        """Section 81's question: what belongs to this record?"""
        return [
            row
            for row in self.manifest()
            if row["entity"] == entity and row["record_index"] == record_index
        ]

    def describe(self) -> dict[str, Any]:
        return {"root": str(self.root), **self.stats.to_dict()}


def _link_or_copy(source: Path, destination: Path) -> None:
    """Point ``destination`` at the same bytes as ``source``.

    A hard link where the filesystem supports one, a copy where it does not.
    Either way the caller gets a real file at the path it expected.
    """
    try:
        os.link(source, destination)
    except (OSError, AttributeError, NotImplementedError):
        destination.write_bytes(source.read_bytes())

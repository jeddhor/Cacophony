"""Project portability (design document section 72).

    Projects should export as bundles.

        my_project.cacophony

    Internally perhaps: project.yaml, schemas/, templates/, workflows/,
    scripts/, assets/

    Generated datasets should normally be separate.

A bundle is a zip archive with a manifest. That last clause of section 72 is the
one that shapes the design: a bundle carries what *makes* a dataset, never the
dataset. A project schema is kilobytes and a dataset is gigabytes, and somebody
sending a colleague "the thing that generates our test data" means the former.

Two problems are genuinely hard, and both are about a file leaving one machine
and arriving on another.

**Absolute paths do not travel.** A project whose lookup generator reads
``/home/me/names.csv`` works perfectly until it is opened by somebody else. On
export, a path inside the project directory is copied into the bundle and
rewritten to a relative one; a path outside it cannot be, so export *refuses*
and says which field. Silently dropping the reference would produce a bundle
that imports cleanly and fails on its first record.

**An archive from somebody else is untrusted input.** A zip entry may be named
``../../.bashrc``, or be a symlink, or be an absolute path. Every entry is
checked against the destination before anything is written, and an archive
containing one bad name is rejected whole rather than half-extracted. The
project file inside is *not* trusted either: it is loaded and compiled during
``inspect``, so a malformed schema is a readable error rather than a surprise
later.

The manifest records a content hash per file, so ``inspect`` can say whether
what arrived is what was sent.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from ..core.errors import SchemaError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterable

__all__ = [
    "BUNDLE_SUFFIX",
    "MANIFEST_NAME",
    "BundleManifest",
    "export_bundle",
    "import_bundle",
    "inspect_bundle",
]

#: The extension section 72 names.
BUNDLE_SUFFIX = ".cacophony"
MANIFEST_NAME = "cacophony.json"
PROJECT_NAME = "project.yaml"

#: The bundle format's own version, so a future reader can refuse a bundle it
#: does not understand rather than misreading it.
FORMAT_VERSION = 1

#: The directories section 72 lists, plus ``worlds`` - a named world is part of
#: what reproduces a dataset (section 16), so a bundle that omitted it would
#: travel without the thing that makes its people the same people.
BUNDLE_DIRS = ("recipes", "schemas", "templates", "workflows", "scripts", "assets", "worlds")

#: Never bundled, whatever directory it is sitting in. Generated datasets are
#: the explicit exclusion of section 72; the rest is machine state.
_EXCLUDED_NAMES = frozenset(
    {"__pycache__", ".git", ".venv", "node_modules", ".cacophony", ".DS_Store"}
)
_EXCLUDED_SUFFIXES = frozenset(
    {".jsonl", ".ndjson", ".parquet", ".db", ".sqlite", ".sqlite3", ".pyc", BUNDLE_SUFFIX}
)

#: Largest archive this will extract, and largest single entry. A zip that
#: expands to a terabyte is not a project.
MAX_TOTAL_BYTES = 512 * 1024 * 1024
MAX_ENTRIES = 10_000


@dataclass(slots=True)
class BundleManifest:
    """What a bundle says about itself."""

    format_version: int = FORMAT_VERSION
    project: str = ""
    description: str = ""
    cacophony_version: str = ""
    created_at: str = ""
    #: Archive-relative path to SHA-256, so ``inspect`` can verify contents.
    files: dict[str, str] = field(default_factory=dict)
    #: Recipes the project's entities ask for, so a reader can see at a glance
    #: what the bundle depends on that is not inside it.
    recipes_used: list[str] = field(default_factory=list)
    entities: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "format_version": self.format_version,
            "project": self.project,
            "description": self.description,
            "cacophony_version": self.cacophony_version,
            "created_at": self.created_at,
            "entities": dict(self.entities),
            "recipes_used": list(self.recipes_used),
            "notes": list(self.notes),
            "files": dict(sorted(self.files.items())),
        }

    @classmethod
    def from_dict(cls, data: Any) -> BundleManifest:
        if not isinstance(data, dict):
            raise SchemaError("the bundle manifest is not a mapping")
        version = int(data.get("format_version", 0))
        if version > FORMAT_VERSION:
            raise SchemaError(
                f"this bundle is format version {version}; this Cacophony understands "
                f"up to {FORMAT_VERSION}. Upgrade Cacophony to open it."
            )
        return cls(
            format_version=version,
            project=str(data.get("project") or ""),
            description=str(data.get("description") or ""),
            cacophony_version=str(data.get("cacophony_version") or ""),
            created_at=str(data.get("created_at") or ""),
            files={str(key): str(value) for key, value in (data.get("files") or {}).items()},
            recipes_used=[str(item) for item in (data.get("recipes_used") or [])],
            entities={str(key): int(value) for key, value in (data.get("entities") or {}).items()},
            notes=[str(item) for item in (data.get("notes") or [])],
        )


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_excluded(path: Path) -> bool:
    if any(part in _EXCLUDED_NAMES for part in path.parts):
        return True
    return path.suffix.lower() in _EXCLUDED_SUFFIXES


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #


def export_bundle(
    project_path: str | Path,
    destination: str | Path | None = None,
    *,
    include: Iterable[str] | None = None,
    overwrite: bool = False,
) -> tuple[Path, BundleManifest]:
    """Pack a project and its supporting files into a ``.cacophony`` archive."""
    import yaml

    from .. import __version__
    from .loader import load_project

    source = Path(project_path).resolve()
    if not source.is_file():
        raise SchemaError(f"project file not found: {source}")

    root = source.parent
    raw = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise SchemaError(f"{source}: expected a mapping at the top level")

    # Compiled, so an unexportable project is refused before an archive exists.
    project = load_project(source)

    referenced: set[str] = set()
    rewritten, notes = _relativise(raw, root=root, referenced=referenced)

    # A second pass over the *expanded* project, purely to find files. A recipe
    # can reference a lookup table of its own, and the authored document does not
    # mention it - the first bundle tested went out without the CSV its own
    # recipe needed. The expansion is thrown away; only the paths are kept, so
    # the bundle still carries the schema somebody wrote rather than the one the
    # compiler saw.
    from .recipes import expand_recipes

    try:
        expanded = expand_recipes(raw, project_dir=root)
    except SchemaError:
        # A project that does not expand is a project that does not compile, and
        # `load_project` above has already said so.
        expanded = None
    if expanded is not None:
        # Deliberately outside the guard above. A refusal from here is the
        # unexportable-path refusal, and swallowing it would let a bundle go out
        # missing a file its own recipe needs - which is the exact failure this
        # pass exists to prevent.
        _relativise(expanded, root=root, referenced=referenced)

    target = Path(destination) if destination else root / f"{_slug(project.name)}{BUNDLE_SUFFIX}"
    if target.suffix != BUNDLE_SUFFIX:
        target = target.with_suffix(BUNDLE_SUFFIX)
    if target.exists() and not overwrite:
        raise SchemaError(f"{target} already exists. Pass --force to replace it.")

    manifest = BundleManifest(
        project=project.name,
        description=project.project.description or "",
        cacophony_version=__version__,
        created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        entities={name: entity.count for name, entity in project.entities.items()},
        recipes_used=sorted(
            {
                str(recipe)
                for entity in (raw.get("entities") or {}).values()
                if isinstance(entity, dict)
                for recipe in (entity.get("recipes") or [])
            }
        ),
        notes=notes,
    )

    wanted = set(include) if include is not None else set(BUNDLE_DIRS)
    target.parent.mkdir(parents=True, exist_ok=True)

    payload: dict[str, bytes] = {
        PROJECT_NAME: yaml.safe_dump(
            rewritten, sort_keys=False, allow_unicode=True, width=100
        ).encode("utf-8")
    }

    for directory in sorted(wanted):
        folder = root / directory
        if not folder.is_dir():
            continue
        for item in sorted(folder.rglob("*")):
            if not item.is_file() or item.is_symlink() or _is_excluded(item.relative_to(root)):
                continue
            payload[PurePosixPath(item.relative_to(root)).as_posix()] = item.read_bytes()

    # Files the schema actually names, wherever they sit. A lookup table in
    # `data/` is as much part of the project as one in `templates/`.
    for name in sorted(referenced):
        item = root / name
        if item.is_file() and not item.is_symlink() and name not in payload:
            payload[name] = item.read_bytes()
            notes.append(f"packed {name}, referenced by the schema")

    manifest.files = {name: _digest(data) for name, data in payload.items()}

    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(MANIFEST_NAME, json.dumps(manifest.to_dict(), indent=2) + "\n")
        for name, data in payload.items():
            archive.writestr(name, data)

    return target, manifest


def _relativise(
    raw: Any, *, root: Path, trail: str = "", referenced: set[str] | None = None
) -> tuple[Any, list[str]]:
    """Rewrite paths that point inside the project; refuse ones that do not.

    Only ``path``, ``file`` and ``source`` keys are considered, which is what the
    generators that read files call them. Refusing rather than dropping matters:
    a bundle that quietly lost a lookup table would import cleanly and fail on
    its first record, and the person who exported it would be somewhere else by
    then.

    Every surviving path is added to ``referenced``, so export can pack the file
    itself. Packing only the directories section 72 names was not enough - the
    first project tested put its lookup table in ``data/``, and the bundle went
    out without it.
    """
    notes: list[str] = []
    if referenced is None:
        referenced = set()

    if isinstance(raw, dict):
        result: dict[str, Any] = {}
        for key, value in raw.items():
            where = f"{trail}.{key}" if trail else str(key)
            if key in ("path", "file", "source") and isinstance(value, str) and value:
                candidate = Path(value)
                if candidate.is_absolute():
                    resolved = candidate.resolve()
                    try:
                        relative = resolved.relative_to(root)
                    except ValueError:
                        raise SchemaError(
                            f"{where}: '{value}' is outside the project directory, so it "
                            "cannot travel in a bundle. Move the file inside "
                            f"{root} and reference it relatively, or remove the field."
                        ) from None
                    result[key] = relative.as_posix()
                    referenced.add(relative.as_posix())
                    notes.append(f"rewrote {where} to a project-relative path")
                    continue
                if ".." in candidate.parts:
                    raise SchemaError(
                        f"{where}: '{value}' points outside the project directory, so it "
                        "cannot travel in a bundle."
                    )
                if (root / candidate).is_file():
                    referenced.add(PurePosixPath(candidate).as_posix())
                else:
                    notes.append(f"{where}: '{value}' does not exist and was not packed")
            child, child_notes = _relativise(value, root=root, trail=where, referenced=referenced)
            result[key] = child
            notes.extend(child_notes)
        return result, notes

    if isinstance(raw, list):
        items: list[Any] = []
        for index, value in enumerate(raw):
            child, child_notes = _relativise(
                value, root=root, trail=f"{trail}[{index}]", referenced=referenced
            )
            items.append(child)
            notes.extend(child_notes)
        return items, notes

    return raw, notes


def _slug(text: str) -> str:
    cleaned = "".join(char if char.isalnum() else "-" for char in text.strip().lower())
    return "-".join(part for part in cleaned.split("-") if part) or "project"


# --------------------------------------------------------------------------- #
# Inspect and import
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class BundleReport:
    """What ``inspect`` found, without extracting anything."""

    path: Path
    manifest: BundleManifest
    entries: list[str] = field(default_factory=list)
    total_bytes: int = 0
    tampered: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)
    project_ok: bool = False
    project_error: str = ""

    @property
    def ok(self) -> bool:
        return self.project_ok and not self.tampered and not self.missing

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "ok": self.ok,
            "manifest": self.manifest.to_dict(),
            "entries": list(self.entries),
            "total_bytes": self.total_bytes,
            "tampered": list(self.tampered),
            "missing": list(self.missing),
            "extra": list(self.extra),
            "project_ok": self.project_ok,
            "project_error": self.project_error,
        }


def inspect_bundle(path: str | Path) -> BundleReport:
    """Read a bundle's manifest, verify its contents, and compile its project.

    Nothing is written. The project is compiled from memory, so "will this
    bundle work" is answerable before deciding whether to unpack it.
    """
    archive_path = Path(path)
    if not archive_path.is_file():
        raise SchemaError(f"bundle not found: {archive_path}")

    with _open_archive(archive_path) as archive:
        names = [info.filename for info in archive.infolist() if not info.is_dir()]
        if MANIFEST_NAME not in names:
            raise SchemaError(
                f"{archive_path} has no {MANIFEST_NAME}, so it is not a Cacophony bundle."
            )
        manifest = BundleManifest.from_dict(json.loads(archive.read(MANIFEST_NAME)))

        report = BundleReport(path=archive_path, manifest=manifest, entries=sorted(names))
        report.total_bytes = sum(info.file_size for info in archive.infolist())

        for name, expected in manifest.files.items():
            if name not in names:
                report.missing.append(name)
                continue
            if _digest(archive.read(name)) != expected:
                report.tampered.append(name)
        report.extra = sorted(
            name for name in names if name != MANIFEST_NAME and name not in manifest.files
        )

        if PROJECT_NAME in names:
            report.project_ok, report.project_error = _try_compile(archive, names)
        else:
            report.project_error = f"the bundle has no {PROJECT_NAME}"

    return report


def _try_compile(archive: zipfile.ZipFile, names: list[str]) -> tuple[bool, str]:
    """Compile the bundle as a whole, reporting rather than raising.

    Extracted to a temporary directory rather than parsed from memory, because
    a project is not only its ``project.yaml``: it may use a recipe from its own
    ``recipes/`` directory and a lookup table from ``data/``, and a compile that
    could not see either would report a broken bundle that is in fact fine.
    That happened on the first bundle tested.

    The temporary directory is discarded, so ``inspect`` still writes nothing a
    caller has to clean up.
    """
    import tempfile

    from .compiler import compile_project
    from .loader import load_project

    try:
        with tempfile.TemporaryDirectory(prefix="cacophony-inspect-") as scratch:
            root = Path(scratch)
            for name in names:
                if name == MANIFEST_NAME:
                    continue
                out = _safe_destination(archive.getinfo(name), root)
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(archive.read(name))
            compile_project(load_project(root / PROJECT_NAME))
    except (SchemaError, ValueError, OSError, UnicodeDecodeError) as exc:
        return False, str(exc)
    return True, ""


def import_bundle(
    path: str | Path,
    destination: str | Path,
    *,
    overwrite: bool = False,
) -> tuple[Path, BundleReport]:
    """Unpack a bundle into a directory, refusing anything unsafe.

    The whole archive is checked before a byte is written, so a bundle with one
    bad entry leaves nothing behind rather than half a project.
    """
    report = inspect_bundle(path)
    target = Path(destination).resolve()

    if target.exists() and any(target.iterdir()) and not overwrite:
        raise SchemaError(f"{target} is not empty. Pass --force to write into it anyway.")

    with _open_archive(Path(path)) as archive:
        infos = [info for info in archive.infolist() if not info.is_dir()]
        if len(infos) > MAX_ENTRIES:
            raise SchemaError(
                f"this bundle holds {len(infos):,} files, above the {MAX_ENTRIES:,} limit."
            )
        total = sum(info.file_size for info in infos)
        if total > MAX_TOTAL_BYTES:
            raise SchemaError(
                f"this bundle expands to {total / 1e6:,.0f} MB, above the "
                f"{MAX_TOTAL_BYTES / 1e6:,.0f} MB limit. A project is not a dataset "
                "(design document section 72)."
            )

        # Every destination is resolved and checked first. An archive from
        # somebody else is untrusted input, and one entry named `../../.bashrc`
        # must not be written on the way to discovering the next one.
        planned: list[tuple[zipfile.ZipInfo, Path]] = []
        for info in infos:
            planned.append((info, _safe_destination(info, target)))

        target.mkdir(parents=True, exist_ok=True)
        for info, out in planned:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(archive.read(info))

    return target, report


def _safe_destination(info: zipfile.ZipInfo, target: Path) -> Path:
    """Where an archive entry may be written, or an error."""
    name = info.filename

    # Symlinks and special files carry their target in the payload rather than
    # as a path, so extracting one would create a link pointing anywhere.
    mode = info.external_attr >> 16
    if mode and (mode & 0o170000) not in (0o100000, 0):
        raise SchemaError(f"bundle entry '{name}' is not a regular file; refusing to extract it.")

    pure = PurePosixPath(name)
    if pure.is_absolute() or name.startswith("/") or ":" in name.split("/")[0]:
        raise SchemaError(f"bundle entry '{name}' is an absolute path; refusing to extract it.")
    if ".." in pure.parts:
        raise SchemaError(
            f"bundle entry '{name}' escapes the destination directory; refusing to extract it."
        )

    out = (target / Path(*pure.parts)).resolve()
    if out != target and target not in out.parents:
        raise SchemaError(f"bundle entry '{name}' resolves outside {target}; refusing.")
    return out


def _open_archive(path: Path) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise SchemaError(f"{path} is not a readable zip archive: {exc}") from exc
    except OSError as exc:
        raise SchemaError(f"could not open {path}: {exc}") from exc

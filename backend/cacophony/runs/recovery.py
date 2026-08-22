"""Recovering the schema a run actually used (design document sections 32, 73).

A resumed run must continue the dataset it started, and a dataset generated
from two different schemas is not one dataset. The store keeps the exact
revision text for that reason; this is the one place that turns it back into a
compiled project, so the command line and the API cannot disagree about which
schema a resume uses - which they did, the API compiling whatever the project
file says *now*.

The project's directory travels with it. A schema that reads a lookup table
beside itself has to resolve that path the same way on the second attempt as on
the first, and recompiling the stored text without saying where it came from
lost exactly that.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..core.errors import CacophonyError
from ..schema.compiler import compile_project
from ..schema.loader import load_project, load_project_data

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..schema.plan import CompiledProject
    from ..store.repository import Repository

__all__ = ["SchemaUnavailableError", "compile_stored_revision"]


class SchemaUnavailableError(CacophonyError):
    """The schema a run used cannot be recovered."""


def compile_stored_revision(
    repository: Repository, stored: dict[str, Any]
) -> tuple[CompiledProject, str | None]:
    """The exact revision a run recorded, and a warning if it could not be had.

    Falls back to the project file as it stands now only when the run recorded
    no revision at all - and says so, because that is the situation this
    function exists to avoid.
    """
    record = repository.get_project(stored["project_id"])
    project_dir = _project_dir(record)

    revision_id = stored.get("revision_id")
    if revision_id is not None:
        revision = repository.get_revision(revision_id, include_source=True)
        if revision is not None:
            import yaml

            text = revision["source_text"]
            data = json.loads(text) if revision["source_format"] == "json" else yaml.safe_load(text)
            project = load_project_data(
                data, source=f"revision {revision_id}", project_dir=project_dir
            )
            return compile_project(project), None

    if record and record.get("path") and Path(str(record["path"])).exists():
        return (
            compile_project(load_project(str(record["path"]))),
            "this run recorded no schema revision; using the project file as it stands now",
        )

    raise SchemaUnavailableError("the schema this run used is no longer available")


def _project_dir(record: dict[str, Any] | None) -> Path | None:
    path = str((record or {}).get("path") or "")
    if not path:
        return None
    parent = Path(path).parent
    return parent if parent.is_dir() else None

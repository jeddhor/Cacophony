"""Loading and saving project schemas (design document sections 72 and 74).

Projects are YAML or JSON on disk. Loading reports Pydantic's structural
complaints as a single readable :class:`SchemaError` rather than a wall of
validation noise, because the person reading it is usually editing the file by
hand.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from ..core.errors import SchemaError
from .models import ProjectSpec

__all__ = ["dump_project", "load_project", "load_project_data", "save_project"]

_YAML_SUFFIXES = {".yaml", ".yml"}
_JSON_SUFFIXES = {".json"}


def load_project_data(
    data: dict[str, Any],
    *,
    source: str = "<memory>",
    project_dir: Path | None = None,
    expand: bool = True,
) -> ProjectSpec:
    """Build a :class:`ProjectSpec` from an already-parsed mapping.

    Recipes are expanded first (section 80), so what the models validate is an
    ordinary project. Everything downstream - compiler, linter, Studio, patcher,
    writers - therefore never learns what a recipe is.

    ``expand=False`` is for the schema editor, which patches the document a
    person wrote rather than the one expansion produced.
    """
    if expand:
        from .recipes import expand_recipes

        data = expand_recipes(data, project_dir=project_dir)

    try:
        return ProjectSpec.model_validate(data)
    except ValidationError as exc:
        raise SchemaError(_format_validation_error(exc, source)) from exc


def load_project(path: str | Path, *, expand: bool = True) -> ProjectSpec:
    """Load a project schema from a YAML or JSON file.

    A ``recipes/`` directory beside the file is part of the project (section
    72's bundle layout), so recipe resolution is told where the file lives.
    """
    file_path = Path(path)
    if not file_path.exists():
        raise SchemaError(f"Project file not found: {file_path}")

    text = file_path.read_text(encoding="utf-8")
    suffix = file_path.suffix.lower()

    try:
        if suffix in _JSON_SUFFIXES:
            data = json.loads(text)
        elif suffix in _YAML_SUFFIXES or not suffix:
            data = yaml.safe_load(text)
        else:
            raise SchemaError(
                f"Unsupported project file type '{suffix}'. Use .yaml, .yml or .json."
            )
    except yaml.YAMLError as exc:
        raise SchemaError(f"{file_path}: invalid YAML - {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SchemaError(f"{file_path}: invalid JSON - {exc}") from exc

    if data is None:
        raise SchemaError(f"{file_path}: the project file is empty.")
    if not isinstance(data, dict):
        raise SchemaError(
            f"{file_path}: expected a mapping at the top level, got {type(data).__name__}."
        )

    project = load_project_data(
        data, source=str(file_path), project_dir=file_path.parent, expand=expand
    )
    # So a relative path in the schema resolves against the schema.
    project.base_dir = file_path.parent.resolve()
    return project


def dump_project(project: ProjectSpec, *, fmt: str = "yaml") -> str:
    """Serialise a project back to text.

    ``exclude_defaults`` keeps round-tripped files small and Git-friendly: a
    diff should show what the user changed, not every default in the model.
    """
    data = project.model_dump(mode="json", by_alias=True, exclude_defaults=True, exclude_none=True)
    if fmt == "json":
        return json.dumps(data, indent=2) + "\n"
    if fmt in ("yaml", "yml"):
        return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100)
    raise ValueError(f"Unsupported dump format '{fmt}'. Use 'yaml' or 'json'.")


def save_project(project: ProjectSpec, path: str | Path) -> Path:
    """Write a project schema to disk, inferring the format from the suffix."""
    file_path = Path(path)
    fmt = "json" if file_path.suffix.lower() in _JSON_SUFFIXES else "yaml"
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(dump_project(project, fmt=fmt), encoding="utf-8")
    return file_path


def _format_validation_error(exc: ValidationError, source: str) -> str:
    lines = [f"{source}: the project schema is invalid."]
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "<root>"
        lines.append(f"  - {location}: {error['msg']}")
    return "\n".join(lines)

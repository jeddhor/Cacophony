"""Construction helpers shared by the tests.

Kept out of ``conftest.py`` so they can be imported by name rather than through
pytest's fixture machinery.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from cacophony.core.context import GenerationContext
from cacophony.core.seeds import SeedChain
from cacophony.schema.compiler import compile_project
from cacophony.schema.loader import load_project_data
from cacophony.schema.models import EntitySpec, FieldSpec, ProjectSpec

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = REPO_ROOT / "templates"
EXAMPLES = REPO_ROOT / "examples"


#: Blocks that sit beside ``project:`` rather than inside it.
_TOP_LEVEL = (
    "providers",
    "scenarios",
    "timeline",
    "chaos",
    "quality",
    "patches",
    "requires",
    "outputs",
    "relationships",
)


def make_project(
    entities: dict[str, Any],
    *,
    providers: dict[str, Any] | None = None,
    **keys: Any,
) -> ProjectSpec:
    """Build a project from a plain mapping, the way a YAML file would.

    Keys naming a top-level block go beside ``project:``; anything else goes
    inside it, so ``seed=1`` and ``timeline={...}`` both land where a YAML
    author would have put them.
    """
    top = {name: keys.pop(name) for name in list(keys) if name in _TOP_LEVEL}
    payload: dict[str, Any] = {
        "project": {"name": "Test Project", "seed": 1234, **keys},
        "entities": entities,
        **top,
    }
    if providers:
        payload["providers"] = providers
    return load_project_data(payload)


def make_context(
    generator_field: FieldSpec | None = None,
    *,
    record_index: int = 0,
    seed: int = 99,
    values: dict[str, Any] | None = None,
) -> GenerationContext:
    """A minimal context for exercising one generator in isolation."""
    field_spec = generator_field or FieldSpec(name="value")
    entity = EntitySpec(name="thing", fields={field_spec.name: field_spec})
    project = ProjectSpec(project={"name": "t", "seed": seed}, entities={"thing": entity})
    record: dict[str, Any] = dict(values or {})
    return GenerationContext(
        project=project,
        entity=entity,
        record_index=record_index,
        seeds=SeedChain.root(seed).entity("thing").record(record_index).field(field_spec.name),
        field=field_spec,
        current_record=record,
    )


def compile_from(
    entities: dict[str, Any],
    *,
    providers: dict[str, Any] | None = None,
    **project_keys: Any,
) -> Any:
    return compile_project(make_project(entities, providers=providers, **project_keys))

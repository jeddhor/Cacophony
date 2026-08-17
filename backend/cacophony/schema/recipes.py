"""Generation recipes (design document sections 80 and 106).

    Allow reusable generator fragments.

        US Corporate Employee Identity

    Contains: first name, last name, email, username, employee ID,
    manager relationship.

A recipe is a named fragment of schema. Written out by hand, section 80's
example is forty lines of YAML that everybody writes the same way and nobody
enjoys writing; as a recipe it is one line::

    entities:
      employee:
        count: 5000
        recipes: [us_corporate_identity]

**Expansion happens before validation, on the raw mapping.** A recipe's fields
become ordinary fields, so the compiler, the linter, the Studio, the patcher and
every writer see a normal project and none of them needs to learn what a recipe
is. The alternative - a second kind of field, resolved late - would put a
special case in every one of those places.

**Expansion is visible or it is a trap.** A schema that silently gains eight
fields is a schema nobody can debug, so each expanded field records the recipe
it came from in ``recipe:``, and ``cacophony plan`` prints it. Somebody reading
the plan can see that ``username`` was not their idea, and where to go to find
out what it does.

**Overriding must not require forking.** Naming a field the recipe already
defines overrides it, key by key, in the place the recipe put it - so a project
can change one template without restating the other seven fields, and without
the field order jumping around. Naming a *different* generator replaces the
recipe's generator and its options wholesale, because merging
``generator: llm`` onto ``generator: template`` would leave the template's
options behind as junk the language model has no use for.

``$self`` is the one piece of substitution. Section 80's example includes a
manager relationship, which is a reference from an entity to itself - and a
recipe cannot know the name of the entity it will be expanded into.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..core.errors import SchemaError
from .models import _FIELD_KEYS

__all__ = [
    "CATALOGUE_DIR",
    "Recipe",
    "RecipeLibrary",
    "expand_recipes",
    "load_library",
]

#: Where the built-in catalogue lives (section 106).
CATALOGUE_DIR = Path(__file__).parent / "catalogue"

#: The token a recipe uses to mean "the entity I am being expanded into".
SELF = "$self"


@dataclass(slots=True)
class Recipe:
    """One reusable fragment."""

    name: str
    description: str = ""
    group: str = "custom"
    #: Other recipes to expand first. A recipe for an employee includes the one
    #: for a person rather than restating it.
    includes: tuple[str, ...] = ()
    #: Field name to its raw mapping, in the order the recipe declares them.
    fields: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Free-text note about what the recipe assumes - a timeline, a provider, a
    #: sibling entity. Shown by ``cacophony recipes`` rather than enforced,
    #: because a recipe that refused to expand until its assumptions held would
    #: be harder to adopt than writing the fields out.
    requires: str = ""
    source: str = "built-in"

    @property
    def field_names(self) -> list[str]:
        return list(self.fields)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "group": self.group,
            "includes": list(self.includes),
            "fields": self.field_names,
            "requires": self.requires,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, name: str, data: Any, *, source: str = "built-in") -> Recipe:
        if not isinstance(data, dict):
            raise SchemaError(f"recipe '{name}' must be a mapping, not {type(data).__name__}")

        fields = data.get("fields") or {}
        if not isinstance(fields, dict):
            raise SchemaError(f"recipe '{name}': 'fields' must be a mapping of field names")
        for field_name, spec in fields.items():
            if not isinstance(spec, dict):
                raise SchemaError(
                    f"recipe '{name}': field '{field_name}' must be a mapping, "
                    f"not {type(spec).__name__}"
                )

        includes = data.get("includes") or data.get("include") or []
        if isinstance(includes, str):
            includes = [includes]

        if not fields and not includes:
            raise SchemaError(f"recipe '{name}' defines no fields and includes nothing")

        return cls(
            name=name,
            description=str(data.get("description") or "").strip(),
            group=str(data.get("group") or "custom"),
            includes=tuple(str(item) for item in includes),
            fields={str(key): dict(value) for key, value in fields.items()},
            requires=str(data.get("requires") or "").strip(),
            source=source,
        )


class RecipeLibrary:
    """Every recipe available to a project.

    Three sources, in increasing precedence: the built-in catalogue, a
    ``recipes/`` directory beside the project file, and the project's own
    ``recipes:`` block. A project may therefore replace a built-in recipe by
    defining one with the same name, which is what somebody wants when the
    catalogue is nearly right.
    """

    def __init__(self) -> None:
        self._recipes: dict[str, Recipe] = {}

    def add(self, recipe: Recipe) -> None:
        self._recipes[recipe.name] = recipe

    def get(self, name: str) -> Recipe:
        try:
            return self._recipes[name]
        except KeyError:
            available = ", ".join(sorted(self._recipes)) or "<none>"
            raise SchemaError(f"no recipe named '{name}'. Available: {available}") from None

    def __contains__(self, name: str) -> bool:
        return name in self._recipes

    def __len__(self) -> int:
        return len(self._recipes)

    def names(self) -> list[str]:
        return sorted(self._recipes)

    def groups(self) -> dict[str, list[Recipe]]:
        grouped: dict[str, list[Recipe]] = {}
        for recipe in self._recipes.values():
            grouped.setdefault(recipe.group, []).append(recipe)
        for recipes in grouped.values():
            recipes.sort(key=lambda item: item.name)
        return dict(sorted(grouped.items()))

    def describe(self) -> list[dict[str, Any]]:
        return [self._recipes[name].to_dict() for name in self.names()]

    # -- resolution ----------------------------------------------------------- #

    def resolve(self, name: str, *, seen: tuple[str, ...] = ()) -> dict[str, dict[str, Any]]:
        """A recipe's fields, with its includes expanded first.

        Includes are resolved depth-first and merged in order, so a recipe that
        includes another can override one of its fields the same way a project
        can override one of the recipe's.
        """
        if name in seen:
            cycle = " -> ".join([*seen, name])
            raise SchemaError(f"recipe includes form a cycle: {cycle}")

        recipe = self.get(name)
        fields: dict[str, dict[str, Any]] = {}
        for included in recipe.includes:
            for field_name, spec in self.resolve(included, seen=(*seen, name)).items():
                fields[field_name] = _merge_field(fields.get(field_name), spec)

        for field_name, spec in recipe.fields.items():
            merged = _merge_field(fields.get(field_name), spec)
            # Attribution names the recipe that was asked for, not the include
            # that happens to define the field: somebody who wrote
            # `recipes: [employee]` wants to be told "employee".
            merged.setdefault("recipe", name)
            fields[field_name] = merged
        return fields


def _merge_field(base: dict[str, Any] | None, override: dict[str, Any]) -> dict[str, Any]:
    """Layer one field mapping over another.

    Field-level keys merge. Generator options do not: if the override names a
    generator, it brings its own options and the base's are dropped. Merging
    them would leave a template's ``template:`` sitting in a language model's
    option bag, where it is at best ignored and at worst mistaken for a real
    setting.
    """
    if base is None:
        return dict(override)

    if "generator" in override and override["generator"] != base.get("generator"):
        kept = {key: value for key, value in base.items() if key in _FIELD_KEYS}
        kept.pop("generator", None)
        return {**kept, **override}

    return {**base, **override}


def load_library(
    *,
    project_dir: Path | None = None,
    inline: dict[str, Any] | None = None,
    include_catalogue: bool = True,
) -> RecipeLibrary:
    """Build the library for one project."""
    library = RecipeLibrary()

    if include_catalogue:
        for path in sorted(CATALOGUE_DIR.glob("*.yaml")):
            _load_file(library, path, source=f"catalogue:{path.stem}")

    if project_dir is not None:
        directory = Path(project_dir) / "recipes"
        if directory.is_dir():
            for path in sorted(directory.glob("*.y*ml")):
                _load_file(library, path, source=str(path))

    for name, data in (inline or {}).items():
        library.add(Recipe.from_dict(str(name), data, source="project"))

    return library


def _load_file(library: RecipeLibrary, path: Path, *, source: str) -> None:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise SchemaError(f"{path}: invalid YAML in recipe file - {exc}") from exc
    except OSError as exc:
        raise SchemaError(f"{path}: could not read recipe file - {exc}") from exc

    if payload is None:
        return
    if not isinstance(payload, dict):
        raise SchemaError(f"{path}: a recipe file must be a mapping of recipe names")

    # A file may hold one recipe under `name:` or several keyed by name. The
    # second form is what the catalogue uses; the first is what somebody writes
    # when they have exactly one.
    if "fields" in payload or "includes" in payload:
        library.add(Recipe.from_dict(payload.get("name") or path.stem, payload, source=source))
        return

    group = str(payload.pop("group", "") or "") or path.stem
    for name, data in payload.items():
        if isinstance(data, dict):
            data = {"group": group, **data}
        library.add(Recipe.from_dict(str(name), data, source=source))


# --------------------------------------------------------------------------- #
# Expansion
# --------------------------------------------------------------------------- #


def expand_recipes(
    data: dict[str, Any],
    *,
    project_dir: Path | None = None,
    library: RecipeLibrary | None = None,
) -> dict[str, Any]:
    """Replace every entity's ``recipes:`` with the fields they stand for.

    Returns a new mapping; the input is not modified, because the caller may be
    holding the text a person is editing.
    """
    entities = data.get("entities")
    if not isinstance(entities, dict):
        return data

    wanted = any(isinstance(entity, dict) and entity.get("recipes") for entity in entities.values())
    if not wanted and not data.get("recipes"):
        # Nothing asked for a recipe, so nothing is loaded. A project that uses
        # none should not pay to read the catalogue.
        return data

    resolved = library or load_library(project_dir=project_dir, inline=data.get("recipes"))

    expanded: dict[str, Any] = dict(data)
    expanded.pop("recipes", None)
    expanded["entities"] = {
        name: _expand_entity(name, entity, resolved) for name, entity in entities.items()
    }
    return expanded


def _expand_entity(name: str, entity: Any, library: RecipeLibrary) -> Any:
    if not isinstance(entity, dict):
        return entity

    requested = entity.get("recipes")
    if not requested:
        return entity

    if isinstance(requested, str):
        requested = [requested]
    if not isinstance(requested, list):
        raise SchemaError(
            f"entity '{name}': 'recipes' must be a list of recipe names, "
            f"not {type(requested).__name__}"
        )

    fields: dict[str, dict[str, Any]] = {}
    for recipe_name in requested:
        for field_name, spec in library.resolve(str(recipe_name)).items():
            fields[field_name] = _merge_field(fields.get(field_name), spec)

    # The entity's own fields override the recipe's, in the place the recipe put
    # them, so overriding one template does not reorder the record.
    own = entity.get("fields") or {}
    if not isinstance(own, dict):
        raise SchemaError(f"entity '{name}': 'fields' must be a mapping")

    for field_name, spec in own.items():
        if not isinstance(spec, dict):
            # A field declared as something other than a mapping is somebody
            # else's error to report; pass it through untouched.
            fields[field_name] = spec
            continue
        merged = _merge_field(fields.get(field_name), spec)
        if field_name in fields and "recipe" in fields[field_name]:
            # Still the recipe's field, now adjusted. Saying so is the point:
            # a reader should be able to see that this line is an override
            # rather than an original.
            merged["recipe"] = fields[field_name]["recipe"]
        fields[field_name] = merged

    result = dict(entity)
    result.pop("recipes", None)
    result["fields"] = {
        field_name: _substitute(spec, entity=name) for field_name, spec in fields.items()
    }
    return result


def _substitute(spec: Any, *, entity: str) -> Any:
    """Replace ``$self`` with the entity being expanded into.

    Section 80's example needs it: a manager relationship is a reference from an
    entity to itself, and a recipe cannot know what that entity is called.
    """
    if isinstance(spec, dict):
        return {key: _substitute(value, entity=entity) for key, value in spec.items()}
    if isinstance(spec, list):
        return [_substitute(item, entity=entity) for item in spec]
    if isinstance(spec, str) and SELF in spec:
        return spec.replace(SELF, entity)
    return spec

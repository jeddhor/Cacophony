"""Editing a schema without destroying it (design document sections 48, 74).

Section 48 makes the Schema Studio "the heart of the UI", and section 74 wants
project files that a team reviews in Git. Those two pull in opposite
directions: a GUI that saves by re-serialising its own model produces a correct
file with every comment, every blank line and every deliberate ordering
stripped out. The first time someone edits a documented schema through the
Studio and opens the diff, they stop using the Studio.

So edits are applied as *targeted patches* to the YAML document itself, in
ruamel's round-trip mode. Changing an entity's count changes one scalar; the
comment three lines above it survives, because nothing else was touched.

A patch is atomic. Every operation is applied to an in-memory copy, the result
is parsed and compiled, and only a document that survives both is written back.
A schema editor that can leave a project in a state that no longer loads is
worse than one that refuses the edit.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.error import YAMLError

from ..core.errors import SchemaError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

__all__ = ["EditOperation", "PatchResult", "apply_patch", "describe_operations"]

#: Operations the Studio can perform. Anything not listed is refused, so a
#: malformed request cannot reach the document.
OPERATIONS = (
    "set_project",
    "set_entity",
    "add_entity",
    "remove_entity",
    "set_field",
    "unset_field",
    "add_field",
    "remove_field",
    "rename_field",
    "move_field",
)


@dataclass(slots=True)
class EditOperation:
    """One change to a schema document."""

    op: str
    entity: str | None = None
    field: str | None = None
    key: str | None = None
    value: Any = None
    name: str | None = None
    index: int | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EditOperation:
        op = str(data.get("op", ""))
        if op not in OPERATIONS:
            raise SchemaError(f"Unknown edit operation '{op}'. Available: {', '.join(OPERATIONS)}")
        return cls(
            op=op,
            entity=data.get("entity"),
            field=data.get("field"),
            key=data.get("key"),
            value=data.get("value"),
            name=data.get("name"),
            index=data.get("index"),
        )

    def describe(self) -> str:
        parts = [self.op]
        if self.entity:
            parts.append(self.entity)
        if self.field:
            parts.append(self.field)
        if self.key:
            parts.append(f"{self.key}=")
        return " ".join(parts)


@dataclass(slots=True)
class PatchResult:
    """The rewritten document, and what it took to get there."""

    source: str
    applied: list[str]
    changed: bool


def _yaml() -> YAML:
    parser = YAML()
    parser.preserve_quotes = True
    # Match the indentation the shipped templates use, so a patched file does
    # not reindent itself the first time it is saved.
    parser.indent(mapping=2, sequence=4, offset=2)
    parser.width = 100
    return parser


def _load(source: str) -> Any:
    try:
        document = _yaml().load(source)
    except YAMLError as exc:
        raise SchemaError(f"the schema is not valid YAML: {exc}") from exc
    if not isinstance(document, dict):
        raise SchemaError("the schema must be a mapping at the top level")
    return document


def _dump(document: Any) -> str:
    stream = io.StringIO()
    _yaml().dump(document, stream)
    return stream.getvalue()


def apply_patch(source: str, operations: Sequence[EditOperation | dict[str, Any]]) -> PatchResult:
    """Apply ``operations`` to a schema document, or refuse the lot.

    The result is parsed and compiled before it is returned, so a caller that
    receives a :class:`PatchResult` has a document that still loads.
    """
    parsed = [
        operation if isinstance(operation, EditOperation) else EditOperation.from_dict(operation)
        for operation in operations
    ]
    if not parsed:
        return PatchResult(source=source, applied=[], changed=False)

    document = _load(source)
    applied: list[str] = []

    for operation in parsed:
        _apply(document, operation)
        applied.append(operation.describe())

    updated = _dump(document)
    _verify(updated)
    return PatchResult(source=updated, applied=applied, changed=updated != source)


def _verify(source: str) -> None:
    """Refuse a patch whose result would not load or compile."""
    from .compiler import compile_project
    from .loader import load_project_data

    document = _load(source)
    project = load_project_data(dict(document), source="<edited schema>")
    compile_project(project)


# --------------------------------------------------------------------------- #
# Operations
# --------------------------------------------------------------------------- #


def _apply(document: Any, operation: EditOperation) -> None:
    handler = _HANDLERS[operation.op]
    handler(document, operation)


def _entities(document: Any) -> Any:
    entities = document.get("entities")
    if not isinstance(entities, dict):
        raise SchemaError("the schema has no 'entities' mapping to edit")
    return entities


def _entity(document: Any, name: str | None) -> Any:
    if not name:
        raise SchemaError("this operation needs an 'entity'")
    entities = _entities(document)
    if name not in entities:
        known = ", ".join(entities) or "<none>"
        raise SchemaError(f"no entity '{name}'. Known entities: {known}")
    return entities[name]


def _fields(document: Any, entity_name: str | None) -> Any:
    entity = _entity(document, entity_name)
    fields = entity.get("fields")
    if not isinstance(fields, dict):
        raise SchemaError(f"entity '{entity_name}' has no 'fields' mapping")
    return fields


def _field(document: Any, operation: EditOperation) -> Any:
    if not operation.field:
        raise SchemaError("this operation needs a 'field'")
    fields = _fields(document, operation.entity)
    if operation.field not in fields:
        known = ", ".join(fields) or "<none>"
        raise SchemaError(
            f"entity '{operation.entity}' has no field '{operation.field}'. Fields: {known}"
        )
    return fields[operation.field]


def _set_project(document: Any, operation: EditOperation) -> None:
    if not operation.key:
        raise SchemaError("set_project needs a 'key'")
    meta = document.setdefault("project", CommentedMap())
    _assign(meta, operation.key, operation.value)


def _set_entity(document: Any, operation: EditOperation) -> None:
    if not operation.key:
        raise SchemaError("set_entity needs a 'key'")
    _assign(_entity(document, operation.entity), operation.key, operation.value)


def _add_entity(document: Any, operation: EditOperation) -> None:
    name = operation.name or operation.entity
    if not name:
        raise SchemaError("add_entity needs a 'name'")
    entities = document.setdefault("entities", CommentedMap())
    if name in entities:
        raise SchemaError(f"entity '{name}' already exists")

    entity = CommentedMap()
    entity["count"] = (
        operation.value.get("count", 100) if isinstance(operation.value, dict) else 100
    )
    fields = CommentedMap()
    # A new entity with no fields will not compile, so it starts with one.
    fields["id"] = CommentedMap({"type": "string", "generator": "sequence"})
    entity["fields"] = fields
    entities[name] = entity


def _remove_entity(document: Any, operation: EditOperation) -> None:
    entities = _entities(document)
    name = operation.name or operation.entity
    if name not in entities:
        raise SchemaError(f"no entity '{name}'")
    if len(entities) == 1:
        raise SchemaError("a project needs at least one entity")
    del entities[name]


def _set_field(document: Any, operation: EditOperation) -> None:
    if not operation.key:
        raise SchemaError("set_field needs a 'key'")
    _assign(_field(document, operation), operation.key, operation.value)


def _unset_field(document: Any, operation: EditOperation) -> None:
    if not operation.key:
        raise SchemaError("unset_field needs a 'key'")
    field = _field(document, operation)
    if operation.key in field:
        del field[operation.key]


def _add_field(document: Any, operation: EditOperation) -> None:
    name = operation.name or operation.field
    if not name:
        raise SchemaError("add_field needs a 'name'")
    fields = _fields(document, operation.entity)
    if name in fields:
        raise SchemaError(f"field '{name}' already exists on '{operation.entity}'")

    payload = operation.value if isinstance(operation.value, dict) else {}
    field = CommentedMap()
    field["type"] = payload.get("type", "string")
    for key, value in payload.items():
        if key != "type":
            field[key] = value
    fields[name] = field

    if operation.index is not None:
        _reorder(fields, name, operation.index)


def _remove_field(document: Any, operation: EditOperation) -> None:
    fields = _fields(document, operation.entity)
    name = operation.name or operation.field
    if name not in fields:
        raise SchemaError(f"entity '{operation.entity}' has no field '{name}'")
    if len(fields) == 1:
        raise SchemaError(f"entity '{operation.entity}' needs at least one field")
    del fields[name]


def _rename_field(document: Any, operation: EditOperation) -> None:
    if not operation.field or not operation.name:
        raise SchemaError("rename_field needs 'field' and 'name'")
    fields = _fields(document, operation.entity)
    if operation.field not in fields:
        raise SchemaError(f"entity '{operation.entity}' has no field '{operation.field}'")
    if operation.name in fields:
        raise SchemaError(f"field '{operation.name}' already exists")

    order = list(fields)
    index = order.index(operation.field)
    fields[operation.name] = fields.pop(operation.field)
    _reorder(fields, operation.name, index)


def _move_field(document: Any, operation: EditOperation) -> None:
    if operation.index is None:
        raise SchemaError("move_field needs an 'index'")
    fields = _fields(document, operation.entity)
    name = operation.name or operation.field
    if not name:
        raise SchemaError("move_field needs a 'name'")
    if name not in fields:
        raise SchemaError(f"entity '{operation.entity}' has no field '{name}'")
    _reorder(fields, name, operation.index)


def _reorder(mapping: Any, key: str, index: int) -> None:
    """Move ``key`` to ``index``, keeping every other key in order.

    ruamel maps have no move operation, so the tail is popped and reinserted.
    Comments attached to the moved keys travel with them.
    """
    order = [name for name in mapping if name != key]
    index = max(0, min(index, len(order)))
    order.insert(index, key)

    values = {name: mapping[name] for name in order}
    for name in list(mapping):
        del mapping[name]
    for name in order:
        mapping[name] = values[name]


def _assign(mapping: Any, key: str, value: Any) -> None:
    """Set a key, or remove it when the value is ``None``.

    Writing ``null`` into a schema is almost never what a form meant; clearing
    a field in the Studio should delete the key and let the default apply.
    """
    if value is None:
        if key in mapping:
            del mapping[key]
        return
    mapping[key] = value


_HANDLERS = {
    "set_project": _set_project,
    "set_entity": _set_entity,
    "add_entity": _add_entity,
    "remove_entity": _remove_entity,
    "set_field": _set_field,
    "unset_field": _unset_field,
    "add_field": _add_field,
    "remove_field": _remove_field,
    "rename_field": _rename_field,
    "move_field": _move_field,
}


def describe_operations() -> list[dict[str, Any]]:
    """Documentation for the API, so the Studio can be built against it."""
    return [
        {"op": "set_project", "needs": ["key"], "optional": ["value"]},
        {"op": "set_entity", "needs": ["entity", "key"], "optional": ["value"]},
        {"op": "add_entity", "needs": ["name"], "optional": ["value"]},
        {"op": "remove_entity", "needs": ["name"]},
        {"op": "set_field", "needs": ["entity", "field", "key"], "optional": ["value"]},
        {"op": "unset_field", "needs": ["entity", "field", "key"]},
        {"op": "add_field", "needs": ["entity", "name"], "optional": ["value", "index"]},
        {"op": "remove_field", "needs": ["entity", "name"]},
        {"op": "rename_field", "needs": ["entity", "field", "name"]},
        {"op": "move_field", "needs": ["entity", "name", "index"]},
    ]

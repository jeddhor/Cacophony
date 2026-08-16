"""AI-assisted schema creation (design document section 50).

    "Generate a schema representing employees, company laptops, login activity
     and security alerts for a 5,000-person company."

    Cacophony proposes Company, Location, Department, Employee, Device,
    LoginEvent, SecurityAlert - plus relationships and fields. The user
    approves or edits it.

The division of labour is the whole design here, and it is not "ask the model
for a schema".

**The model proposes structure.** What entities exist, what each one is called,
what fields it has, what each field *means*, and which entity points at which.
That is a question about the world, and a language model is good at it.

**Cacophony chooses generators.** Which of the twenty-five generators produces
a given field is a question about Cacophony, and the recommendation engine
(section 68) answers it better than a model can - it knows that a field called
``email`` wants Faker on a reserved domain rather than a paragraph of prose,
and it will not invent a generator that does not exist. So a proposal carries
``semantic:`` rather than ``generator:`` wherever the model has no strong
opinion, and the compiler fills the rest in.

**Nothing is accepted untested.** A proposal is compiled and linted before it
is shown. If it does not compile, the failure is handed back to the model once
with the error attached (the repair rung of section 66's ladder). What reaches
the user is a schema that is known to work, or an honest report that one could
not be produced.

"Known to work" means runnable, not merely compilable. A field whose meaning
reads like prose is routed to a language model by the recommendation engine
whatever its declared type, so a proposal that named no provider would compile,
lint, and then fail on its first record. The assistant therefore writes the
provider it is already holding - the one that just designed the schema - into
the proposal, and marks any field needing a backend it cannot supply as a
placeholder.

The result is YAML rather than an internal object, because section 50 ends with
"the user approves or edits it" - and what a person edits is a file.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..core.errors import CacophonyError
from ..core.types import DataType
from .compiler import compile_project
from .linter import lint_project
from .loader import load_project_data

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..providers.base import LanguageModelProvider
    from .linter import LintReport
    from .plan import CompiledProject

__all__ = [
    "SchemaAssistant",
    "SchemaProposal",
    "SchemaProposalError",
    "proposal_json_schema",
    "to_project_data",
    "to_yaml",
]

#: Types a proposal may use. Deliberately a subset of :class:`DataType`.
#:
#: The model does not need ``binary`` or ``object`` to describe a business
#: domain, and every name it can choose from is a name that will compile.
#: ``text`` is absent for a sharper reason: it means long-form prose, and a
#: proposal is meant to be mostly deterministic so that running it is cheap.
#:
#: This list does not, on its own, keep a proposal free of language-model
#: fields - the recommendation engine reads the *semantic* as well as the type,
#: so "the model of the laptop hardware" on a plain string field is routed to a
#: model however it was declared. Runnability is therefore settled after
#: compilation, in :meth:`SchemaAssistant._make_runnable`, which writes the
#: provider the assistant is already holding into the proposal.
_PROPOSABLE_TYPES = (
    "string",
    "integer",
    "float",
    "decimal",
    "boolean",
    "date",
    "datetime",
    "enum",
    "uuid",
)

#: How many records an entity gets when the model does not say.
_DEFAULT_COUNT = 1000

#: An upper bound on what a proposal may ask for. A model that reads "a
#: 5,000-person company" and writes 5,000,000,000 has made a typo, not a
#: decision, and the first the user would know of it is a full disk.
_MAX_COUNT = 50_000_000


class SchemaProposalError(CacophonyError):
    """A proposal could not be turned into a schema that compiles."""


def proposal_json_schema() -> dict[str, Any]:
    """The JSON Schema a proposal must satisfy (section 13).

    Structured output is enforced here for the same reason it is enforced for
    records: what comes back is text until something checks it.
    """
    field_schema = {
        "type": "object",
        "required": ["name", "type"],
        "additionalProperties": False,
        "properties": {
            "name": {
                "type": "string",
                "description": "snake_case field name",
            },
            "type": {"type": "string", "enum": list(_PROPOSABLE_TYPES)},
            "semantic": {
                "type": "string",
                "description": (
                    "What the field means, in a short phrase. This is the most "
                    "important property: Cacophony chooses a generator from it."
                ),
            },
            "choices": {
                "type": "array",
                "items": {"type": "string"},
                "description": "For type 'enum' only: the permitted values.",
            },
            "references": {
                "type": "string",
                "description": (
                    "The name of another entity this field points at, if it is a "
                    "foreign key. Omit otherwise."
                ),
            },
            "primary_key": {"type": "boolean"},
            "unique": {"type": "boolean"},
            "nullable": {"type": "boolean"},
        },
    }

    entity_schema = {
        "type": "object",
        "required": ["name", "count", "fields"],
        "additionalProperties": False,
        "properties": {
            "name": {"type": "string", "description": "snake_case, singular"},
            "description": {"type": "string"},
            "count": {"type": "integer", "minimum": 1, "maximum": _MAX_COUNT},
            "fields": {"type": "array", "items": field_schema, "minItems": 1},
        },
    }

    return {
        "type": "object",
        "required": ["name", "entities"],
        "additionalProperties": False,
        "properties": {
            "name": {"type": "string", "description": "A short title for the project"},
            "description": {"type": "string"},
            "entities": {"type": "array", "items": entity_schema, "minItems": 1},
        },
    }


_SYSTEM_PROMPT = """\
You design data schemas for Cacophony, a synthetic data generator.

Given a description of a domain, propose the entities it contains, the fields \
of each entity, and the relationships between them. Answer with JSON matching \
the provided schema and nothing else.

Rules:
- Name entities in singular snake_case: `login_event`, not `LoginEvents`.
- Give every entity exactly one field with `"primary_key": true`.
- Express a relationship by setting `"references"` on a field to the name of \
another entity in the same proposal. Name such fields after the entity they \
point at.
- Order entities so that anything referenced appears before what references it.
- Set `count` to something proportional to the domain: if the description says \
5,000 employees, an employee entity has 5000 records and their login events \
have far more.
- Write a `semantic` for every field that is not a key or a foreign key. This \
is a short phrase describing what the value means - "the department this \
person works in", "when the ticket was opened". Do not name generators, \
formats or providers; Cacophony chooses those itself.
- Use `type: enum` with `choices` where a field has a small fixed vocabulary.
- Prefer fewer, well-described fields over many thin ones.
"""


@dataclass(slots=True)
class SchemaProposal:
    """A proposed schema, and what happened when Cacophony checked it."""

    yaml: str
    data: dict[str, Any]
    compiled: CompiledProject | None = None
    lint: LintReport | None = None
    attempts: int = 1
    raw: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.compiled is not None

    @property
    def entity_names(self) -> list[str]:
        return list(self.compiled.entity_order) if self.compiled else []

    def summary(self) -> dict[str, Any]:
        return {
            "entities": self.entity_names,
            "records": sum(e.count for e in self.compiled.ordered_entities())
            if self.compiled
            else 0,
            "attempts": self.attempts,
            "lint_issues": len(self.lint) if self.lint is not None else 0,
            "notes": list(self.notes),
        }


class SchemaAssistant:
    """Turns a description of a domain into a schema (section 50)."""

    def __init__(
        self,
        provider: LanguageModelProvider,
        *,
        model: str | None = None,
        temperature: float = 0.2,
        max_attempts: int = 2,
    ) -> None:
        self.provider = provider
        self.model = model
        #: Low but not zero. A schema is a creative act; a deterministic one
        #: would be the same seven entities for every description that rhymed.
        self.temperature = temperature
        #: One proposal, and at most one repair. Section 66's ladder, shortened:
        #: there is a person waiting for this, not a batch job.
        self.max_attempts = max(1, max_attempts)

    async def propose(
        self,
        description: str,
        *,
        seed: int | None = None,
        scale: int | None = None,
    ) -> SchemaProposal:
        """Propose a schema for ``description``.

        ``scale`` multiplies the counts the model chose, for the common case of
        liking the shape but wanting a tenth of the volume.
        """
        from ..providers.base import GenerationRequest

        if not description.strip():
            raise SchemaProposalError("Describe the data you want before asking for a schema.")

        prompt = self._prompt(description)
        problems: list[str] = []
        raw = ""

        for attempt in range(1, self.max_attempts + 1):
            request = GenerationRequest(
                prompt=prompt if attempt == 1 else self._repair_prompt(description, raw, problems),
                system=_SYSTEM_PROMPT,
                model=self.model,
                temperature=self.temperature,
                seed=seed,
                json_schema=proposal_json_schema(),
                max_tokens=4096,
            )
            result = await self.provider.generate(request)
            raw = result.text

            try:
                proposal = self._build(raw, scale=scale)
            except SchemaProposalError as exc:
                problems = [str(exc)]
                continue

            proposal.attempts = attempt
            proposal.raw = raw
            return proposal

        raise SchemaProposalError(
            "The model did not produce a schema that compiles after "
            f"{self.max_attempts} attempts. Last problem: " + ("; ".join(problems) or "unknown")
        )

    # -- prompt ------------------------------------------------------------- #

    def _prompt(self, description: str) -> str:
        return (
            f"Design a Cacophony schema for the following data.\n\n{description.strip()}\n\n"
            "Answer with JSON only."
        )

    def _repair_prompt(self, description: str, raw: str, problems: list[str]) -> str:
        """Hand the failure back with the error attached (section 66)."""
        return (
            f"Design a Cacophony schema for the following data.\n\n{description.strip()}\n\n"
            "Your previous answer could not be used:\n\n"
            + "\n".join(f"- {problem}" for problem in problems)
            + f"\n\nThat answer was:\n{raw[:2000]}\n\n"
            "Correct those problems and answer with JSON only."
        )

    # -- assembly ----------------------------------------------------------- #

    def _build(self, raw: str, *, scale: int | None) -> SchemaProposal:
        from ..generation.structured import extract_json

        try:
            payload = extract_json(raw)
        except Exception as exc:
            raise SchemaProposalError(f"the answer was not JSON ({exc})") from exc

        if not isinstance(payload, dict):
            raise SchemaProposalError("the answer was not a JSON object")

        data = to_project_data(payload, scale=scale)

        try:
            spec = load_project_data(data)
            compiled = compile_project(spec)
        except CacophonyError as exc:
            raise SchemaProposalError(f"the proposed schema does not compile: {exc}") from exc

        notes = _notes(payload, data)
        # Compiling is not enough. A field whose meaning reads like prose is
        # routed to a language model by the recommendation engine, and a
        # proposal that declares no provider would compile, lint, and then fail
        # on its first record. Settle that here, while a working provider is
        # still in hand.
        data, compiled, provider_notes = self._make_runnable(data, compiled)
        notes.extend(provider_notes)

        return SchemaProposal(
            yaml=to_yaml(data),
            data=data,
            compiled=compiled,
            lint=lint_project(compiled),
            notes=notes,
        )

    def _make_runnable(
        self, data: dict[str, Any], compiled: CompiledProject
    ) -> tuple[dict[str, Any], CompiledProject, list[str]]:
        """Ensure every field of the proposal can actually produce a value.

        Two outcomes, in order of preference. A field needing a language model
        gets one: the very provider that wrote this schema, written into the
        proposal so the file is self-contained. A field needing something the
        assistant cannot supply - an image backend, say - is set to emit a
        marked placeholder instead, which keeps the proposal runnable and
        obviously incomplete rather than runnable-looking and broken.
        """
        needed: set[str] = set()
        for entity in compiled.ordered_entities():
            for field_view in entity.fields:
                kind = type(field_view.generator).requires_provider
                if kind:
                    needed.add(kind)

        if not needed:
            return data, compiled, []

        notes: list[str] = []
        if "language_model" in needed:
            data["providers"] = {self.provider_id: self._provider_block()}
            notes.append(
                f"some fields are written by a language model; the proposal points at "
                f"{self._describe_provider()}"
            )
            needed.discard("language_model")

        for kind in sorted(needed):
            marked = _mark_unavailable(data, compiled, kind)
            if marked:
                notes.append(
                    f"no {kind.replace('_', ' ')} provider was available, so "
                    f"{', '.join(marked)} will emit a placeholder until you configure one"
                )

        try:
            compiled = compile_project(load_project_data(data))
        except CacophonyError as exc:  # pragma: no cover - the edit is additive
            raise SchemaProposalError(f"the proposed schema does not compile: {exc}") from exc
        return data, compiled, notes

    @property
    def provider_id(self) -> str:
        return getattr(self.provider, "id", None) or "local_llm"

    def _provider_block(self) -> dict[str, Any]:
        """The provider that wrote this schema, as a project declares one.

        Credentials are never written: ``secret`` is a logical id resolved at
        run time from the keychain or the environment (section 63).
        """
        config = getattr(self.provider, "config", {}) or {}
        block: dict[str, Any] = {
            "type": "language_model",
            "adapter": getattr(type(self.provider), "adapter_name", "ollama"),
        }
        for key in ("base_url", "model", "secret"):
            value = self.model if key == "model" else config.get(key)
            if key == "model":
                value = self.model or config.get("model")
            if value:
                block[key] = value
        return block

    def _describe_provider(self) -> str:
        adapter = getattr(type(self.provider), "adapter_name", "the provider")
        return f"{adapter} {self.model or ''}".strip()


# --------------------------------------------------------------------------- #
# Translation
# --------------------------------------------------------------------------- #


def to_project_data(payload: dict[str, Any], *, scale: int | None = None) -> dict[str, Any]:
    """Turn a model's proposal into Cacophony project data.

    Everything the model could get wrong is corrected here rather than trusted:
    unknown types become strings, references to entities that were not proposed
    are dropped to plain fields, counts are clamped, and a field that named a
    generator does not get one - the recommendation engine decides.
    """
    entities_in = payload.get("entities")
    if not isinstance(entities_in, list) or not entities_in:
        raise SchemaProposalError("the proposal contains no entities")

    known: list[str] = []
    for entity in entities_in:
        if isinstance(entity, dict) and isinstance(entity.get("name"), str):
            known.append(_identifier(entity["name"]))

    entities: dict[str, Any] = {}
    for entity in entities_in:
        if not isinstance(entity, dict):
            continue
        name = _identifier(str(entity.get("name", "")))
        if not name:
            continue
        entities[name] = _entity_data(entity, known=known, scale=scale)

    if not entities:
        raise SchemaProposalError("the proposal contains no usable entities")

    return {
        "project": {
            "name": str(payload.get("name") or "Proposed Schema").strip(),
            "description": str(payload.get("description") or "").strip() or None,
            "seed": 42,
        },
        "entities": entities,
    }


def _entity_data(entity: dict[str, Any], *, known: list[str], scale: int | None) -> dict[str, Any]:
    fields_in = entity.get("fields")
    if not isinstance(fields_in, list) or not fields_in:
        raise SchemaProposalError(f"entity '{entity.get('name')}' has no fields")

    count = entity.get("count")
    count = _DEFAULT_COUNT if not isinstance(count, int) or count < 1 else count
    if scale:
        count = max(1, count // scale)

    fields: dict[str, Any] = {}
    primary: str | None = None
    for item in fields_in:
        if not isinstance(item, dict):
            continue
        name = _identifier(str(item.get("name", "")))
        if not name or name in fields:
            continue
        field_data, is_primary = _field_data(item, known=known)
        if is_primary and primary is None:
            primary = name
        fields[name] = field_data

    if not fields:
        raise SchemaProposalError(f"entity '{entity.get('name')}' has no usable fields")

    data: dict[str, Any] = {"count": min(count, _MAX_COUNT), "fields": fields}
    description = entity.get("description")
    if isinstance(description, str) and description.strip():
        data["description"] = description.strip()
    if primary:
        data["primary_key"] = primary
    return data


def _field_data(item: dict[str, Any], *, known: list[str]) -> tuple[dict[str, Any], bool]:
    data: dict[str, Any] = {}

    target = item.get("references")
    if isinstance(target, str):
        target = _identifier(target)
    # A reference to something that was not proposed is worse than no
    # reference: it would not compile, and the field it describes is still
    # meaningful without it.
    if target and target in known:
        data["generator"] = "reference"
        data["entity"] = target
        # Event-shaped children outnumber their parents, and real activity
        # concentrates. Uniform would be the safer-looking default and the
        # less honest one (section 15).
        data["distribution"] = "skewed"
        return data, False

    declared = str(item.get("type") or "string").lower()
    data["type"] = declared if declared in _PROPOSABLE_TYPES else "string"

    if data["type"] == "enum":
        choices = [str(choice) for choice in item.get("choices") or [] if str(choice).strip()]
        if len(choices) >= 2:
            data["generator"] = "weighted"
            data["choices"] = choices
        else:
            # An enum with nothing to choose from is a string.
            data["type"] = "string"

    semantic = item.get("semantic")
    if isinstance(semantic, str) and semantic.strip():
        data["semantic"] = semantic.strip()

    is_primary = bool(item.get("primary_key"))
    if is_primary:
        # A key is the one field whose generator is not a matter of taste.
        data["generator"] = "sequence"
        data["unique"] = True
        data.pop("semantic", None)
        if data["type"] not in ("integer", "string", "uuid"):
            data["type"] = "integer"
    else:
        if item.get("unique"):
            data["unique"] = True
        if item.get("nullable"):
            data["nullable"] = True

    if "generator" not in data and "semantic" not in data:
        # Nothing to go on but the name, which is what the recommendation
        # engine reads anyway (section 68).
        data.setdefault("type", "string")

    return data, is_primary


def _mark_unavailable(data: dict[str, Any], compiled: CompiledProject, kind: str) -> list[str]:
    """Set ``on_unavailable: placeholder`` on fields needing ``kind``.

    Returns the field names it touched, so the caller can say which.
    """
    marked: list[str] = []
    for entity in compiled.ordered_entities():
        for field_view in entity.fields:
            if type(field_view.generator).requires_provider != kind:
                continue
            entity_data = data["entities"].get(entity.name)
            field_data = (entity_data or {}).get("fields", {}).get(field_view.name)
            if field_data is None:
                continue
            field_data["on_unavailable"] = "placeholder"
            marked.append(f"{entity.name}.{field_view.name}")
    return marked


def _identifier(name: str) -> str:
    """``Login Events`` becomes ``login_events``."""
    cleaned = "".join(
        character if character.isalnum() else "_" for character in name.strip().lower()
    )
    return "_".join(part for part in cleaned.split("_") if part)


def _notes(payload: dict[str, Any], data: dict[str, Any]) -> list[str]:
    """What was changed on the way in, so nothing is quietly different."""
    notes: list[str] = []

    proposed = payload.get("entities")
    proposed_count = len(proposed) if isinstance(proposed, list) else 0
    kept = len(data["entities"])
    if kept < proposed_count:
        notes.append(f"{proposed_count - kept} proposed entity/entities could not be used")

    without_key = [name for name, entity in data["entities"].items() if "primary_key" not in entity]
    if without_key:
        notes.append("no primary key was proposed for: " + ", ".join(sorted(without_key)))

    return notes


def to_yaml(data: dict[str, Any]) -> str:
    """Render project data as the YAML a person would have written.

    Written by hand rather than dumped, because the output of this function is
    the thing section 50 asks the user to approve or edit, and a dump orders
    keys alphabetically, quotes what does not need quoting, and loses the blank
    line between entities that makes a schema readable.
    """
    lines: list[str] = []
    project = data["project"]

    lines.append("# Proposed by Cacophony from a description (design document section 50).")
    lines.append("# Review it before generating: the shape is a suggestion, the counts")
    lines.append("# are a guess, and every field is yours to change.")
    lines.append("")
    lines.append("project:")
    lines.append(f"  name: {_scalar(project['name'])}")
    if project.get("description"):
        lines.append(f"  description: {_scalar(project['description'])}")
    lines.append(f"  seed: {project.get('seed', 42)}")

    # Written only when a field actually needs one, and never carrying a
    # credential - `secret` is a logical id resolved at run time (section 63).
    for provider_id, provider in (data.get("providers") or {}).items():
        if not lines[-1].startswith("providers:"):
            lines.extend(["", "providers:"])
        lines.append(f"  {provider_id}:")
        lines.extend(f"    {key}: {_scalar(value)}" for key, value in provider.items())

    lines.append("")
    lines.append("entities:")

    for entity_name, entity in data["entities"].items():
        lines.append("")
        lines.append(f"  {entity_name}:")
        if entity.get("description"):
            lines.append(f"    description: {_scalar(entity['description'])}")
        lines.append(f"    count: {entity['count']}")
        if entity.get("primary_key"):
            lines.append(f"    primary_key: {entity['primary_key']}")
        lines.append("    fields:")
        for field_name, field_data in entity["fields"].items():
            lines.append("")
            lines.append(f"      {field_name}:")
            for key, value in field_data.items():
                if key == "choices":
                    lines.append("        choices:")
                    lines.extend(f"          - {_scalar(choice)}" for choice in value)
                else:
                    lines.append(f"        {key}: {_scalar(value)}")

    return "\n".join(lines) + "\n"


#: Text that is unambiguously a plain YAML string. An allow-list rather than a
#: list of things to escape, because the ways YAML can reinterpret a bare
#: scalar are not enumerable from memory - ``12:30`` is the integer 750, ``no``
#: is false, ``1.2`` is a float, ``0x1f`` is 31 - and the failure is silent.
#: Anything that is not obviously safe gets quoted, which is never wrong.
_PLAIN_SCALAR = re.compile(r"^[A-Za-z][A-Za-z0-9 ,._'()/-]*[A-Za-z0-9.')]$")

#: Words YAML reads as booleans or null even though they look like words.
_RESERVED_WORDS = frozenset({"true", "false", "null", "yes", "no", "on", "off", "y", "n", "~"})


def _scalar(value: Any) -> str:
    """Render a value as YAML, quoting anything YAML could misread."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)

    text = str(value)
    if _PLAIN_SCALAR.match(text) and " #" not in text and text.lower() not in _RESERVED_WORDS:
        return text
    return json.dumps(text)


def data_type_names() -> tuple[str, ...]:
    """The types a proposal may use, for anything that wants to show them."""
    return tuple(name for name in _PROPOSABLE_TYPES if name in {t.value for t in DataType})

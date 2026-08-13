"""The Prompt Compiler (design document section 12).

    "Users should rarely need to manually engineer prompts."  - section 9

That sentence is the whole design constraint. The user writes what a field
*means*; the compiler turns that, plus everything else the schema already
knows, into a provider-specific prompt and a JSON Schema to enforce it.

Section 12 lists what the compiler must understand: field descriptions, types,
constraints, dependencies, examples, forbidden values, tone, locale and entity
context. All of that is already in the schema, which is why the user does not
have to repeat it in prose.

The output is a :class:`CompiledPrompt` carrying a stable ``version`` and
``hash``. Those matter for two reasons the design document is firm about:
reproducibility metadata must record the prompt (section 4), and the cache key
must change whenever the prompt does (section 76).

Prompts are compiled **once per field group at compile time**, not once per
record. Only the "known values" block varies between records, so it is rendered
separately and appended - which turns prompt construction for a ten-million-row
run from ten million string builds into ten million small ones.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import TYPE_CHECKING, Any

from ..core.record import to_jsonable
from ..core.types import DataType

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from ..schema.models import EntitySpec, FieldSpec, ProjectSpec
    from ..schema.plan import CompiledField

__all__ = ["PROMPT_VERSION", "CompiledPrompt", "PromptCompiler", "json_schema_for_fields"]

#: Bumped whenever the wording changes in a way that alters model output.
#: Recorded in provenance and mixed into the cache key.
PROMPT_VERSION = 1

#: JSON Schema type for each Cacophony primitive, plus a format hint where one
#: exists. Types the model should not be asked to invent map to strings.
_JSON_TYPES: dict[DataType, tuple[str, str | None]] = {
    DataType.STRING: ("string", None),
    DataType.TEXT: ("string", None),
    DataType.INTEGER: ("integer", None),
    DataType.FLOAT: ("number", None),
    DataType.DECIMAL: ("number", None),
    DataType.BOOLEAN: ("boolean", None),
    DataType.UUID: ("string", "uuid"),
    DataType.DATE: ("string", "date"),
    DataType.TIME: ("string", "time"),
    DataType.DATETIME: ("string", "date-time"),
    DataType.DURATION: ("string", "duration"),
    DataType.ENUM: ("string", None),
    DataType.ARRAY: ("array", None),
    DataType.OBJECT: ("object", None),
    DataType.URI: ("string", "uri"),
    DataType.IP_ADDRESS: ("string", "ipv4"),
    DataType.CIDR: ("string", None),
    DataType.MAC_ADDRESS: ("string", None),
    DataType.HOSTNAME: ("string", "hostname"),
    DataType.EMAIL: ("string", "email"),
    DataType.PHONE: ("string", None),
    DataType.JSON: ("object", None),
}

_DEFAULT_SYSTEM = (
    "You generate synthetic test data. Everything you produce is fictional and must "
    "not describe any real person, organisation, address, account or identifier.\n"
    "Return only JSON. No commentary, no markdown fences, no explanation."
)


@dataclass(slots=True)
class CompiledPrompt:
    """A prompt plus the schema that constrains its answer."""

    system: str
    instruction: str
    json_schema: dict[str, Any]
    fields: tuple[str, ...]
    entity: str
    mode: str
    version: int = PROMPT_VERSION
    batch_size: int = 1
    examples: list[dict[str, Any]] = dataclass_field(default_factory=list)

    @property
    def hash(self) -> str:
        """A stable digest of everything that determines the model's answer."""
        hasher = hashlib.blake2b(digest_size=16)
        for part in (
            str(self.version),
            self.system,
            self.instruction,
            json.dumps(self.json_schema, sort_keys=True),
        ):
            encoded = part.encode("utf-8")
            hasher.update(len(encoded).to_bytes(4, "little"))
            hasher.update(encoded)
        return hasher.hexdigest()

    def render(self, known_values: Any = None) -> str:
        """The full user prompt for one call.

        ``known_values`` is a mapping for per-record modes, or a list of
        mappings for a batch.
        """
        if known_values is None:
            return self.instruction
        return f"{self.instruction}\n\n{_render_known(known_values)}"

    def describe(self) -> str:
        return f"{self.entity}[{', '.join(self.fields)}] {self.mode} v{self.version}"


class PromptCompiler:
    """Turns schema definitions into provider-specific prompts."""

    def __init__(self, project: ProjectSpec) -> None:
        self.project = project

    # -- entry point -------------------------------------------------------- #

    def compile(
        self,
        entity: EntitySpec,
        fields: Sequence[CompiledField],
        *,
        mode: str = "per_record",
        context_fields: Sequence[str] = (),
        batch_size: int = 1,
    ) -> CompiledPrompt:
        """Compile one prompt covering ``fields`` of ``entity``."""
        schema = json_schema_for_fields(fields)
        if mode == "batch":
            # minItems and maxItems are load-bearing: a provider doing
            # constrained decoding uses them to emit exactly the right number
            # of records, and everything else uses them as the contract the
            # parser checks the answer against.
            schema = {
                "type": "object",
                "properties": {
                    "records": {
                        "type": "array",
                        "items": schema,
                        "minItems": batch_size,
                        "maxItems": batch_size,
                    }
                },
                "required": ["records"],
                "additionalProperties": False,
            }

        return CompiledPrompt(
            system=self._system_prompt(),
            instruction=self._instruction(
                entity, fields, mode=mode, context_fields=context_fields, batch_size=batch_size
            ),
            json_schema=schema,
            fields=tuple(compiled.name for compiled in fields),
            entity=entity.name,
            mode=mode,
            batch_size=batch_size,
            examples=self._examples(fields),
        )

    # -- pieces ------------------------------------------------------------- #

    def _system_prompt(self) -> str:
        lines = [_DEFAULT_SYSTEM]
        locale = self.project.project.locale
        if locale and locale != "en_US":
            lines.append(f"Write in the conventions of the locale {locale}.")
        if self.project.project.profile == "high_realism":
            lines.append(
                "Favour specific, plausible detail over generic phrasing, while keeping "
                "every detail fictional."
            )
        elif self.project.project.profile == "quick_mock":
            lines.append("Keep answers short and plain; this data is for smoke tests.")
        return "\n".join(lines)

    def _instruction(
        self,
        entity: EntitySpec,
        fields: Sequence[CompiledField],
        *,
        mode: str,
        context_fields: Sequence[str],
        batch_size: int,
    ) -> str:
        subject = _humanise(entity.name)
        sections: list[str] = []

        if mode == "batch":
            sections.append(
                f"Generate {batch_size} fictional {subject} records. Each must be "
                f"internally consistent and clearly distinct from the others."
            )
        else:
            sections.append(f"Generate one fictional {subject} record.")

        if entity.description:
            sections.append(f"About this record type:\n{_clean(entity.description)}")

        sections.append("Fields to produce:\n" + self._field_block(fields))

        requirements = self._requirements(fields, context_fields, mode)
        if requirements:
            sections.append("Requirements:\n" + "\n".join(f"- {line}" for line in requirements))

        if mode == "batch":
            sections.append(
                'Return STRICT JSON of the form {"records": [ ... ]} with exactly '
                f"{batch_size} entries, each an object containing every field above."
            )
        else:
            sections.append(
                "Return STRICT JSON: a single object containing exactly the fields above."
            )

        return "\n\n".join(sections)

    def _field_block(self, fields: Sequence[CompiledField]) -> str:
        """One block per field: name, type, meaning, and every constraint."""
        blocks: list[str] = []
        for compiled in fields:
            spec = compiled.spec
            lines = [f'  "{spec.name}" ({_type_label(spec)})']
            if spec.meaning:
                lines.append(f"      meaning: {_clean(spec.meaning)}")
            if spec.tone:
                lines.append(f"      style: {_clean(spec.tone)}")
            for line in _constraint_lines(spec):
                lines.append(f"      {line}")
            if spec.examples:
                rendered = ", ".join(json.dumps(to_jsonable(item)) for item in spec.examples[:3])
                lines.append(f"      examples: {rendered}")
            blocks.append("\n".join(lines))
        return "\n".join(blocks)

    def _requirements(
        self,
        fields: Sequence[CompiledField],
        context_fields: Sequence[str],
        mode: str,
    ) -> list[str]:
        requirements: list[str] = []

        if context_fields:
            named = ", ".join(f'"{name}"' for name in context_fields)
            requirements.append(
                f"Every value must be consistent with the known values supplied below "
                f"({named}). Do not contradict them and do not repeat them in your answer."
            )

        # Section 14: cross-field coherence, stated as an instruction rather
        # than left to chance.
        if len(fields) > 1:
            requirements.append(
                "The fields you produce must be mutually consistent - they describe one "
                "record, not several unrelated ones."
            )

        for compiled in fields:
            spec = compiled.spec
            if spec.constraints.forbidden:
                values = ", ".join(json.dumps(item) for item in spec.constraints.forbidden)
                requirements.append(f'"{spec.name}" must never be any of: {values}.')
            if spec.effective_null_probability > 0:
                requirements.append(f'"{spec.name}" may be null where that is realistic.')

        # Section 61: models can reproduce real information they memorised.
        requirements.append(
            "Do not use real people, real companies, real domains or real account "
            "numbers. Any email address must end in example.com."
        )

        if mode == "batch":
            requirements.append(
                "Vary the records. Do not reuse the same names, phrasing or sentence "
                "structure across entries."
            )

        return requirements

    def _examples(self, fields: Sequence[CompiledField]) -> list[dict[str, Any]]:
        """A worked example row, when enough fields supply one."""
        example: dict[str, Any] = {}
        for compiled in fields:
            if compiled.spec.examples:
                example[compiled.name] = to_jsonable(compiled.spec.examples[0])
        return [example] if len(example) == len(fields) and example else []


# --------------------------------------------------------------------------- #
# JSON Schema
# --------------------------------------------------------------------------- #


def json_schema_for_fields(fields: Sequence[CompiledField]) -> dict[str, Any]:
    """Build a JSON Schema describing exactly the fields a model must return.

    Section 13 says to use JSON Schema internally where practical. Providers
    that support constrained decoding are handed this directly, which makes
    malformed output impossible rather than merely unlikely; the rest get it as
    the validation contract.
    """
    properties: dict[str, Any] = {}
    required: list[str] = []

    for compiled in fields:
        spec = compiled.spec
        properties[spec.name] = _field_schema(spec)
        if spec.effective_null_probability <= 0:
            required.append(spec.name)

    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _field_schema(spec: FieldSpec) -> dict[str, Any]:
    json_type, json_format = _JSON_TYPES.get(spec.type, ("string", None))
    schema: dict[str, Any] = {"type": json_type}

    if json_format:
        schema["format"] = json_format
    if spec.meaning:
        schema["description"] = _clean(spec.meaning)

    constraints = spec.constraints
    if constraints.enum:
        schema["enum"] = list(constraints.enum)
    if json_type == "string":
        if constraints.min_length is not None:
            schema["minLength"] = constraints.min_length
        if constraints.max_length is not None:
            schema["maxLength"] = constraints.max_length
        if constraints.pattern:
            schema["pattern"] = constraints.pattern
    if json_type in ("integer", "number"):
        if isinstance(constraints.min, (int, float)):
            schema["minimum"] = constraints.min
        if isinstance(constraints.max, (int, float)):
            schema["maximum"] = constraints.max
    if json_type == "array":
        schema["items"] = {"type": "string"}
        if constraints.min_length is not None:
            schema["minItems"] = constraints.min_length
        if constraints.max_length is not None:
            schema["maxItems"] = constraints.max_length

    if spec.effective_null_probability > 0:
        schema["type"] = [json_type, "null"]

    return schema


# --------------------------------------------------------------------------- #
# Rendering helpers
# --------------------------------------------------------------------------- #


def _render_known(known: Any) -> str:
    """The per-record block: values the model must stay consistent with."""
    if isinstance(known, list):
        blocks = [
            f"Record {index + 1}:\n{_render_mapping(values)}" for index, values in enumerate(known)
        ]
        return "Known values for each record:\n\n" + "\n\n".join(blocks)
    return "Known values:\n" + _render_mapping(known)


def _render_mapping(values: Any) -> str:
    if not values:
        return "  (none)"
    return "\n".join(
        f"  {name}: {json.dumps(to_jsonable(value), ensure_ascii=False)}"
        for name, value in values.items()
    )


def _constraint_lines(spec: FieldSpec) -> list[str]:
    lines: list[str] = []
    constraints = spec.constraints

    if constraints.min_length is not None and constraints.max_length is not None:
        lines.append(f"length: {constraints.min_length}-{constraints.max_length} characters")
    elif constraints.max_length is not None:
        lines.append(f"length: at most {constraints.max_length} characters")
    elif constraints.min_length is not None:
        lines.append(f"length: at least {constraints.min_length} characters")

    if constraints.min is not None and constraints.max is not None:
        lines.append(f"range: {constraints.min} to {constraints.max}")
    elif constraints.min is not None:
        lines.append(f"minimum: {constraints.min}")
    elif constraints.max is not None:
        lines.append(f"maximum: {constraints.max}")

    if constraints.enum:
        rendered = ", ".join(json.dumps(item) for item in constraints.enum)
        lines.append(f"must be exactly one of: {rendered}")
    if constraints.pattern:
        lines.append(f"must match the regular expression: {constraints.pattern}")
    if spec.effective_null_probability > 0:
        lines.append("may be null")

    return lines


def _type_label(spec: FieldSpec) -> str:
    if spec.type is DataType.TEXT:
        return "long text"
    return spec.type.value.replace("_", " ")


def _humanise(name: str) -> str:
    return name.replace("_", " ")


def _clean(text: str) -> str:
    """Collapse the whitespace YAML block scalars leave behind."""
    return " ".join(text.split())

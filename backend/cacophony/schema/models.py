"""The project schema (design document sections 3.1, 6, 42 and 72).

Everything begins with a schema. It is portable, human-readable, and designed
to be diffed in Git (section 74), which is why it is expressed as plain
YAML/JSON with no binary state and why the models below preserve field
*ordering* as authored.

The canonical form is the one shown in section 3.1::

    project:
      name: Example Corporate Dataset

    entities:
      employee:
        count: 10000
        fields:
          employee_id:
            type: string
            generator: sequence
            format: "EMP-{000000}"

Note that ``format`` sits beside ``generator`` rather than nested underneath
it. That shorthand is load-bearing for readability, so :class:`FieldSpec`
accepts unknown keys and folds them into the generator's options. The fully
explicit form is also accepted::

          employee_id:
            type: string
            generator:
              type: sequence
              format: "EMP-{000000}"
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from ..core.provenance import ProvenanceMode
from ..core.types import DataType

__all__ = [
    "ChaosSpec",
    "ConstraintSpec",
    "DuplicationSpec",
    "EntitySpec",
    "FieldSpec",
    "GeneratorSpec",
    "OutputProfileSpec",
    "ProjectSpec",
    "ProviderSpec",
    "QualitySpec",
    "RelationshipSpec",
    "ScenarioSpec",
    "SimulationSpec",
    "TimelineSpec",
]

#: Keys that belong to the field itself; every other key becomes a generator option.
_FIELD_KEYS = frozenset(
    {
        "name",
        "type",
        "semantic",
        "description",
        "generator",
        "nullable",
        "null_probability",
        "unique",
        "constraints",
        "depends_on",
        "context",
        "transform",
        "examples",
        "privacy",
        "tone",
        "locale",
        "primary_key",
        # Which recipe this field came from (section 80). A field key rather
        # than a generator option, so it round-trips through `dump_project` and
        # shows up wherever a field is described - expansion that cannot be
        # seen is a trap.
        "recipe",
    }
)


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# --------------------------------------------------------------------------- #
# Generators and constraints
# --------------------------------------------------------------------------- #


class GeneratorSpec(_Base):
    """Which generation strategy produces a field (section 8)."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    type: str
    options: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _normalise(cls, data: Any) -> Any:
        """Accept ``"sequence"``, ``{"type": "sequence", ...}`` and ``{"sequence": {...}}``."""
        if isinstance(data, str):
            return {"type": data, "options": {}}
        if not isinstance(data, dict):
            return data

        payload = dict(data)
        generator_type = payload.pop("type", None) or payload.pop("generator", None)

        # Single-key mapping form: ``{sequence: {format: "..."}}``
        if generator_type is None and len(payload) == 1:
            ((only_key, only_value),) = payload.items()
            if isinstance(only_value, dict):
                return {"type": only_key, "options": dict(only_value)}

        if generator_type is None:
            raise ValueError("generator specification requires a 'type'")

        options = dict(payload.pop("options", {}) or {})
        options.update(payload)  # leftover keys are generator options
        return {"type": str(generator_type), "options": options}

    def option(self, key: str, default: Any = None) -> Any:
        return self.options.get(key, default)

    def __str__(self) -> str:
        return self.type


class ConstraintSpec(_Base):
    """Value constraints, enforced by the constraint validator (section 57)."""

    min: int | float | str | None = None
    max: int | float | str | None = None
    min_length: int | None = None
    max_length: int | None = None
    pattern: str | None = None
    enum: list[Any] | None = None
    forbidden: list[Any] = Field(default_factory=list)
    multiple_of: int | float | None = None
    precision: int | None = None

    def is_empty(self) -> bool:
        return all(
            value in (None, [], {})
            for value in (
                self.min,
                self.max,
                self.min_length,
                self.max_length,
                self.pattern,
                self.enum,
                self.forbidden,
                self.multiple_of,
                self.precision,
            )
        )


# --------------------------------------------------------------------------- #
# Fields
# --------------------------------------------------------------------------- #


class FieldSpec(_Base):
    """A component of an entity (section 6).

    ``semantic`` is the feature the whole product rests on (section 9): a
    natural-language statement of what the field *means*, which the prompt
    compiler turns into generation instructions and the recommendation engine
    uses to pick a generator.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    name: str = ""
    type: DataType = DataType.STRING
    semantic: str | None = None
    description: str | None = None
    generator: GeneratorSpec | None = None

    nullable: bool = False
    null_probability: float = 0.0
    unique: bool = False
    primary_key: bool = False

    constraints: ConstraintSpec = Field(default_factory=ConstraintSpec)

    #: Explicit dependencies, merged with the ones the generator declares.
    depends_on: list[str] = Field(default_factory=list)
    #: Related records the generator may consult (``context: [employee, device]``).
    context: list[str] = Field(default_factory=list)

    transform: list[str | dict[str, Any]] = Field(default_factory=list)
    examples: list[Any] = Field(default_factory=list)
    privacy: str | None = None
    tone: str | None = None
    locale: str | None = None
    #: The recipe that contributed this field (section 80). Set by expansion,
    #: never by hand - though writing it by hand is harmless.
    recipe: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _fold_generator_shorthand(cls, data: Any) -> Any:
        """Move unrecognised keys into the generator's option bag.

        This is what makes ``generator: sequence`` + ``format: "EMP-{000000}"``
        work, and it also lets a generator gain options without this model
        having to learn about them.
        """
        if not isinstance(data, dict):
            return data

        payload = dict(data)
        extras = {key: payload.pop(key) for key in list(payload) if key not in _FIELD_KEYS}

        # A field may also spell the option bag out, which is what someone
        # writes when a generator option shares a name with a field key. Both
        # forms mean the same thing, so the explicit one is unwrapped here
        # rather than becoming an option called "options".
        explicit = extras.pop("options", None)
        if isinstance(explicit, dict):
            extras = {**extras, **explicit}

        if not extras:
            return payload

        generator = payload.get("generator")
        if generator is None:
            # No generator named yet - hold the options until one is inferred.
            payload["generator"] = {"type": "__inferred__", "options": extras}
        elif isinstance(generator, str):
            payload["generator"] = {"type": generator, "options": extras}
        elif isinstance(generator, dict):
            merged = dict(generator)
            merged.setdefault("options", {})
            merged["options"] = {**extras, **dict(merged["options"] or {})}
            payload["generator"] = merged
        return payload

    @field_validator("null_probability")
    @classmethod
    def _check_null_probability(cls, value: float) -> float:
        if not 0.0 <= value <= 1.0:
            raise ValueError("null_probability must be between 0.0 and 1.0")
        return value

    @property
    def has_explicit_generator(self) -> bool:
        return self.generator is not None and self.generator.type != "__inferred__"

    @property
    def pending_options(self) -> dict[str, Any]:
        """Options captured before a generator was inferred."""
        if self.generator is not None and self.generator.type == "__inferred__":
            return dict(self.generator.options)
        return {}

    @property
    def effective_null_probability(self) -> float:
        """``nullable: true`` with no probability means "occasionally null"."""
        if self.null_probability > 0.0:
            return self.null_probability
        return 0.05 if self.nullable else 0.0

    @property
    def meaning(self) -> str | None:
        return self.semantic or self.description


# --------------------------------------------------------------------------- #
# Entities and relationships
# --------------------------------------------------------------------------- #


class RelationshipSpec(_Base):
    """Connects two entities (section 6).

    Declared in phase one so schemas are forward-compatible; the dependency
    ordering it implies is honoured by the compiler already, while foreign-key
    generation arrives with the relational phase.
    """

    name: str = ""
    from_entity: str = Field(alias="from")
    to_entity: str = Field(alias="to")
    cardinality: Literal["one_to_one", "one_to_many", "many_to_one", "many_to_many"] = "one_to_many"
    field: str | None = None
    required: bool = True
    description: str | None = None


class SimulationSpec(_Base):
    """How an entity's records behave as a *history* (sections 25, 26).

    An entity that declares this stops being a bag of independent rows and
    becomes a stream of events belonging to somebody: each subject gets a
    contiguous block of events, ordered in time, optionally carrying state
    forward.

    ```yaml
    transaction:
      count: 500000
      simulation:
        subject: account          # whose events these are
        distribution: skewed      # how many each gets
        state:
          balance:
            initial: "500 + subject * 7 % 4000"
            update: "balance + amount"
            min: 0
            precision: 2
    ```
    """

    #: The entity whose members own these events.
    subject: str = ""
    #: How events are shared out: uniform, skewed or zipf.
    distribution: Literal["uniform", "skewed", "zipf"] = "uniform"
    skew: float = Field(default=1.6, gt=0.0)
    #: Events every subject gets before the rest is shared out.
    minimum: int = Field(default=0, ge=0)
    #: State variables folded over each subject's events.
    state: dict[str, Any] = Field(default_factory=dict)
    #: Order events within a subject by the project timeline.
    ordered: bool = True

    def is_enabled(self) -> bool:
        return bool(self.subject)


class TimelineSpec(_Base):
    """The period a project's events happen in (section 25)."""

    start: str | None = None
    end: str | None = None
    #: A named activity curve: flat, business_hours, retail, evening.
    shape: str = "flat"
    holidays: list[str] = Field(default_factory=list)
    holiday_weight: float = Field(default=0.0, ge=0.0, le=1.0)
    #: Relative weight per month, by name or number.
    months: dict[str, float] = Field(default_factory=dict)
    #: ``{start, end, multiplier}`` windows - promotions, incidents.
    spikes: list[dict[str, Any]] = Field(default_factory=list)
    #: Activity at the end of the period relative to the start.
    growth: float = Field(default=1.0, gt=0.0)

    def is_enabled(self) -> bool:
        return bool(self.start or self.end)


class EntitySpec(_Base):
    """A logical record type (section 6)."""

    name: str = ""
    count: int = Field(default=100, ge=0)
    description: str | None = None
    #: Recipes to expand into this entity's fields (section 80). Consumed by
    #: :func:`cacophony.schema.recipes.expand_recipes` before this model is
    #: built, so a validated `EntitySpec` normally has none - it is declared
    #: here so that a project loaded without expansion still validates rather
    #: than rejecting a key it should understand.
    recipes: list[str] = Field(default_factory=list)
    fields: dict[str, FieldSpec] = Field(default_factory=dict)
    primary_key: str | None = None
    seed: int | None = None
    tags: list[str] = Field(default_factory=list)
    simulation: SimulationSpec = Field(default_factory=SimulationSpec)

    @model_validator(mode="after")
    def _stamp_field_names(self) -> EntitySpec:
        for field_name, spec in self.fields.items():
            if not spec.name:
                spec.name = field_name
        return self

    @property
    def field_list(self) -> list[FieldSpec]:
        """Fields in authored order."""
        return list(self.fields.values())

    def field_names(self) -> list[str]:
        return list(self.fields.keys())

    def resolved_primary_key(self) -> str | None:
        if self.primary_key:
            return self.primary_key
        for name, spec in self.fields.items():
            if spec.primary_key:
                return name
        return None


# --------------------------------------------------------------------------- #
# Providers, scenarios, chaos, outputs
# --------------------------------------------------------------------------- #


class ProviderSpec(_Base):
    """A generation backend, addressed by URI (sections 43 and 85).

    Credentials never live here. ``secret`` holds a logical secret id that is
    resolved at run time from the OS keychain, an environment variable or an
    encrypted store (section 63).
    """

    id: str = ""
    type: Literal["language_model", "image", "speech", "custom"] = "language_model"
    adapter: str = "ollama"
    base_url: str | None = None
    model: str | None = None
    secret: str | None = None
    concurrency: int = Field(default=1, ge=1)
    timeout_seconds: float = Field(default=120.0, gt=0)
    options: dict[str, Any] = Field(default_factory=dict)

    @field_validator("secret")
    @classmethod
    def _reject_inline_secrets(cls, value: str | None, info: ValidationInfo) -> str | None:
        """Guard against a literal key being pasted into a committed project file."""
        if value and (len(value) > 60 or value.startswith(("sk-", "hf_", "ghp_"))):
            raise ValueError(
                "'secret' must be a logical secret id, not a credential value. "
                "Store the credential in the OS keychain or an environment variable "
                "and reference it by name (design document section 63)."
            )
        return value


class ScenarioSpec(_Base):
    """A reusable behavioural pattern applied to generated records (section 17)."""

    name: str = ""
    description: str | None = None
    applies_to: list[str] = Field(default_factory=list)
    affects_fraction: float = Field(default=0.0, ge=0.0, le=1.0)
    parameters: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class ChaosSpec(_Base):
    """Entropy injection, a.k.a. Discord (sections 24 and 78).

    Values are fractions of records affected. A ``preset`` sets them all at
    once; anything stated explicitly overrides the preset. See
    :mod:`cacophony.simulation.chaos` for what each one does to a record.
    """

    preset: Literal["pristine", "realistic", "messy", "hostile_qa", "absolute"] | None = None
    outliers: float = Field(default=0.0, ge=0.0, le=1.0)
    missing_data: float = Field(default=0.0, ge=0.0, le=1.0)
    duplicates: float = Field(default=0.0, ge=0.0, le=1.0)
    malformed_text: float = Field(default=0.0, ge=0.0, le=1.0)
    unexpected_unicode: float = Field(default=0.0, ge=0.0, le=1.0)
    temporal_anomalies: float = Field(default=0.0, ge=0.0, le=1.0)
    referential_anomalies: float = Field(default=0.0, ge=0.0, le=1.0)

    def is_enabled(self) -> bool:
        return self.preset not in (None, "pristine") or any(
            value > 0.0
            for value in (
                self.outliers,
                self.missing_data,
                self.duplicates,
                self.malformed_text,
                self.unexpected_unicode,
                self.temporal_anomalies,
                self.referential_anomalies,
            )
        )


class DuplicationSpec(_Base):
    """Duplicate detection thresholds (design document section 59).

    ``fields`` defaults to the fields where repetition actually happens - the
    long-form text a model wrote - because comparing every employee id against
    every other one finds nothing and costs a great deal. ``["*"]`` compares
    whole records instead.
    """

    enabled: bool | None = None
    fields: list[str] = Field(default_factory=list)
    methods: list[str] = Field(default_factory=lambda: ["exact", "normalized", "minhash"])
    #: Fractions of values, not of records: a record with three compared fields
    #: contributes three.
    max_exact: float | None = Field(default=None, ge=0.0, le=1.0)
    max_near: float | None = Field(default=None, ge=0.0, le=1.0)
    #: Jaccard similarity at which two texts count as the same thing.
    #:
    #: Calibrated rather than guessed. Measured on a sixty-word biography with
    #: only the name changed - the canonical way a model repeats itself - word
    #: trigram Jaccard is 0.82; with a clause rewritten too it is 0.69; two
    #: biographies sharing an opening sentence and nothing else score 0.13.
    #: 0.7 therefore catches real repetition with an order of magnitude of
    #: headroom above the false positives.
    similarity: float = Field(default=0.7, gt=0.0, lt=1.0)
    #: Word n-gram width. Larger finds only longer shared phrasing, and drops
    #: sharply on small edits: the same name-swapped biography scores 0.88 at
    #: bigrams, 0.82 at trigrams and 0.67 at 8-grams.
    shingle: int = Field(default=3, ge=1, le=20)
    signature_size: int = Field(default=64, ge=8, le=512)
    #: Recent values held for near-duplicate comparison. Model repetition is
    #: local, so a window catches it at any dataset size; see
    #: :mod:`cacophony.validation.duplication`.
    window: int = Field(default=50_000, ge=2)
    #: Bloom filter false-positive target for the exact and normalised checks.
    error_rate: float = Field(default=0.001, gt=0.0, lt=0.5)

    def is_enabled(self) -> bool:
        if self.enabled is not None:
            return self.enabled
        # A threshold is a request to measure. Nobody writes `max_near` and
        # means "but do not check".
        return self.max_exact is not None or self.max_near is not None or bool(self.fields)


class QualitySpec(_Base):
    """The ``quality:`` block (design document sections 58, 59)."""

    duplication: DuplicationSpec = Field(default_factory=DuplicationSpec)


class OutputProfileSpec(_Base):
    """One way of writing the same logical dataset (section 34)."""

    name: str = ""
    format: str = "jsonl"
    path: str = "out"
    entities: list[str] = Field(default_factory=list)
    partition_by: list[str] = Field(default_factory=list)
    options: dict[str, Any] = Field(default_factory=dict)


class ProjectMeta(_Base):
    """The ``project:`` block."""

    name: str
    description: str | None = None
    version: int = 1
    seed: int = 0
    locale: str = "en_US"
    profile: Literal["quick_mock", "balanced", "high_realism", "maximum_chaos"] = "balanced"
    provenance: ProvenanceMode = ProvenanceMode.NONE


class ProjectSpec(_Base):
    """A complete generation workspace (section 6)."""

    #: Where the file this was loaded from lives, when it came from one.
    #:
    #: Excluded from serialisation, deliberately: it describes *this machine*,
    #: and writing it into a schema would put a local absolute path in a
    #: document meant to be reviewed in Git (section 74) and shared in a bundle
    #: (section 72). It exists so a relative path in the schema can be resolved
    #: against the schema rather than against the working directory.
    base_dir: Path | None = Field(default=None, exclude=True, repr=False)

    project: ProjectMeta
    entities: dict[str, EntitySpec] = Field(default_factory=dict)
    relationships: list[RelationshipSpec] = Field(default_factory=list)
    providers: dict[str, ProviderSpec] = Field(default_factory=dict)
    scenarios: dict[str, ScenarioSpec] = Field(default_factory=dict)
    timeline: TimelineSpec = Field(default_factory=TimelineSpec)
    chaos: ChaosSpec = Field(default_factory=ChaosSpec)
    quality: QualitySpec = Field(default_factory=QualitySpec)
    #: Project-local recipe definitions (section 80). Consumed by expansion.
    recipes: dict[str, Any] = Field(default_factory=dict)
    outputs: dict[str, OutputProfileSpec] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _stamp_names(self) -> ProjectSpec:
        for name, entity in self.entities.items():
            if not entity.name:
                entity.name = name
        for name, provider in self.providers.items():
            if not provider.id:
                provider.id = name
        for name, scenario in self.scenarios.items():
            if not scenario.name:
                scenario.name = name
        for name, output in self.outputs.items():
            if not output.name:
                output.name = name
        for index, relationship in enumerate(self.relationships):
            if not relationship.name:
                relationship.name = f"{relationship.from_entity}_{relationship.to_entity}_{index}"
        return self

    # -- convenience -------------------------------------------------------- #

    @property
    def name(self) -> str:
        return self.project.name

    @property
    def seed(self) -> int:
        return self.project.seed

    def entity(self, name: str) -> EntitySpec:
        try:
            return self.entities[name]
        except KeyError as exc:
            known = ", ".join(sorted(self.entities)) or "<none>"
            raise KeyError(f"No entity named '{name}'. Known entities: {known}") from exc

    def total_records(self) -> int:
        return sum(entity.count for entity in self.entities.values())


#: Convenience alias used in annotations where a positive record count is meant.
RecordCount = Annotated[int, Field(ge=0)]

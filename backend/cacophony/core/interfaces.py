"""Key backend interfaces (design document section 97).

These four abstractions are the seams along which Cacophony is meant to grow::

    Generator     produces a value
    Provider      talks to an external generation backend
    Validator     decides whether a value is acceptable
    OutputWriter  streams accepted records to a destination

Everything else in the system depends on these interfaces rather than on
concrete implementations, which is what allows the image, speech, scenario and
plugin phases to be additive.

A note on ``async``
------------------
The design document specifies asynchronous generators, and the LLM, image and
speech generators genuinely need that. Deterministic generators do not, and
section 89 targets tens to hundreds of thousands of fields per second - a
budget that coroutine machinery would eat into for no benefit. The compromise
is :class:`SyncGenerator`: it satisfies the asynchronous interface, but also
exposes ``generate_sync`` so the engine can call straight through on the hot
path.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from ..schema.models import EntitySpec, FieldSpec
    from ..validation.results import ValidationResult
    from .context import GenerationContext
    from .provenance import FieldProvenance
    from .record import GeneratedAsset, GeneratedRecord

__all__ = [
    "Capability",
    "GeneratedValue",
    "Generator",
    "HealthStatus",
    "OutputWriter",
    "Provider",
    "SyncGenerator",
    "Validator",
]


# --------------------------------------------------------------------------- #
# Generator
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class GeneratedValue:
    """What a generator returns: a value, plus anything it wants recorded."""

    value: Any
    provenance: FieldProvenance | None = None
    assets: list[GeneratedAsset] = field(default_factory=list)

    @classmethod
    def of(cls, value: Any) -> GeneratedValue:
        return cls(value=value)


class Generator(ABC):
    """Produces values for one field.

    A generator instance is built once per field at *compile* time and then
    reused for every record, so per-record work belongs in ``generate`` and
    per-field setup belongs in ``__init__`` or :meth:`prepare`.
    """

    #: Registry key, e.g. ``"sequence"``. Set by ``@register_generator``.
    name: ClassVar[str] = ""

    #: Whether identical seed plus identical configuration yields identical
    #: output. A plain class attribute rather than a ``ClassVar`` because a
    #: composite generator is only as deterministic as the steps it was given,
    #: which is not known until it is configured.
    deterministic: bool = True

    #: Provider kind required at run time (``"language_model"``, ``"image"``, ...).
    requires_provider: ClassVar[str | None] = None

    #: Rough relative cost, used by the planner's workload estimate.
    cost_class: ClassVar[str] = "cpu"

    def __init__(
        self,
        options: dict[str, Any] | None = None,
        *,
        field: FieldSpec | None = None,
        entity: EntitySpec | None = None,
    ) -> None:
        self.options: dict[str, Any] = dict(options or {})
        self.field = field
        self.entity = entity
        self.prepare()

    def prepare(self) -> None:  # noqa: B027 - an optional hook, not an abstract method
        """Validate and normalise options once, at compile time.

        Implementations should raise
        :class:`~cacophony.core.errors.GeneratorConfigError` for bad options so
        that mistakes surface during ``cacophony validate`` rather than three
        million records into a run.
        """

    def dependencies(self) -> Sequence[str]:
        """Field names this generator reads from the record being built.

        The schema compiler uses this to build the dependency graph
        (section 101), so generators declare their own edges instead of the
        compiler having to know about every generator type.
        """
        return ()

    @abstractmethod
    async def generate(self, context: GenerationContext) -> GeneratedValue:
        """Produce one value."""
        raise NotImplementedError

    def describe(self) -> str:
        """One-line human description, shown in plans and preview headers."""
        return self.name

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} {self.name}>"


class SyncGenerator(Generator):
    """A generator whose work is pure CPU and needs no event loop."""

    @abstractmethod
    def generate_sync(self, context: GenerationContext) -> Any:
        """Produce one value synchronously."""
        raise NotImplementedError

    async def generate(self, context: GenerationContext) -> GeneratedValue:
        return GeneratedValue.of(self.generate_sync(context))


# --------------------------------------------------------------------------- #
# Provider
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Capability:
    """One thing a provider can do, e.g. ``text_generation``."""

    name: str
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return self.name


@dataclass(slots=True)
class HealthStatus:
    """The result of probing a provider."""

    healthy: bool
    message: str = ""
    latency_ms: float | None = None
    version: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def up(cls, message: str = "ok", **kwargs: Any) -> HealthStatus:
        return cls(healthy=True, message=message, **kwargs)

    @classmethod
    def down(cls, message: str, **kwargs: Any) -> HealthStatus:
        return cls(healthy=False, message=message, **kwargs)


class Provider(ABC):
    """An external generation backend.

    Providers are addressed by URI (section 85); Cacophony never owns models.
    """

    #: ``"language_model"``, ``"image"``, ``"speech"``, ...
    kind: ClassVar[str] = "generic"

    def __init__(self, provider_id: str, config: dict[str, Any] | None = None) -> None:
        self.id = provider_id
        self.config: dict[str, Any] = dict(config or {})

    @abstractmethod
    async def health_check(self) -> HealthStatus:
        """Probe the backend without generating anything meaningful."""
        raise NotImplementedError

    @abstractmethod
    def capabilities(self) -> Sequence[Capability]:
        """Advertise what this provider can do (section 43)."""
        raise NotImplementedError

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.kind,
            "capabilities": [capability.name for capability in self.capabilities()],
        }


# --------------------------------------------------------------------------- #
# Validator
# --------------------------------------------------------------------------- #


class Validator(ABC):
    """Decides whether a generated value or record is acceptable (section 57)."""

    #: ``structural``, ``constraint``, ``referential``, ``logical``,
    #: ``statistical`` or ``semantic``.
    category: ClassVar[str] = "structural"

    #: Semantic validation costs LLM calls, so expensive validators are opt-in.
    expensive: ClassVar[bool] = False

    @abstractmethod
    async def validate(self, value: Any, context: GenerationContext) -> ValidationResult:
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# Output writer
# --------------------------------------------------------------------------- #


class OutputWriter(ABC):
    """Streams accepted records to a destination.

    Writers are batch-oriented on purpose: section 31 forbids holding a
    complete dataset in memory, so the engine hands over one bounded batch at a
    time and expects the writer to release it.
    """

    #: Registry key, e.g. ``"parquet"``.
    format: ClassVar[str] = ""

    #: Conventional file extension, used when deriving default paths.
    extension: ClassVar[str] = ""

    #: Whether an interrupted run can reopen this destination and continue
    #: writing to it. Line-oriented formats can; a JSON array or a Parquet
    #: file has a footer, so continuing means starting a new part file
    #: instead (design document section 32).
    appendable: ClassVar[bool] = False

    @abstractmethod
    async def open(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def write_batch(self, records: Sequence[GeneratedRecord]) -> None:
        raise NotImplementedError

    @abstractmethod
    async def close(self) -> None:
        raise NotImplementedError

    async def __aenter__(self) -> OutputWriter:
        await self.open()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.close()

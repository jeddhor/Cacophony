"""Cross-record coherence (design document sections 8, 15, 26).

    "Managers should actually exist. Employees should reference valid managers.
     Computers should belong to employees. Login events should reference those
     computers."  - section 15

The obvious way to do that is to keep every parent record, or at least every
parent key, in memory while the children generate. For five thousand employees
that is fine. For ten million it is a data structure larger than most of the
datasets Cacophony is asked to produce, held for the duration of a run that
produces something else.

Cacophony does not need it. A record's seed is derived by hashing its position
(section 75), so employee 4,823,913 can be reconstructed *directly*, without
the 4,823,912 records before it. A foreign key is therefore not a lookup into a
table - it is a computation:

    pick an index in the parent's range, then generate exactly the fields
    needed to produce that parent's key at that index.

Which means references cost no memory at all, work at any scale, and give the
same answer whichever order the entities were generated in.

Two caches sit on top, because the arithmetic is cheap but not free: recently
derived keys, and recently derived whole parent records for the fields that
want more than the key. Both are bounded, and both are pure accelerators - a
cold cache changes speed and nothing else.
"""

from __future__ import annotations

import math
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..core.errors import GenerationError, SchemaError

if TYPE_CHECKING:  # pragma: no cover - typing only
    import random
    from collections.abc import Sequence

    from ..core.record import GeneratedRecord
    from ..schema.plan import CompiledEntity, CompiledProject

__all__ = [
    "REFERENCE_DISTRIBUTIONS",
    "EntityResolver",
    "ResolverStats",
    "pick_index",
]

#: How a child chooses which parent to point at.
REFERENCE_DISTRIBUTIONS = ("uniform", "skewed", "sequential", "round_robin")

#: Default cache sizes. Keys are small and wanted constantly; whole records are
#: large and wanted only by fields that read a parent's other columns.
DEFAULT_KEY_CACHE = 50_000
DEFAULT_RECORD_CACHE = 2_000


@dataclass(slots=True)
class ResolverStats:
    """What the resolver did, for the run summary."""

    key_lookups: int = 0
    key_hits: int = 0
    record_lookups: int = 0
    record_hits: int = 0
    derived_records: int = 0

    @property
    def key_hit_rate(self) -> float:
        return self.key_hits / self.key_lookups if self.key_lookups else 0.0

    @property
    def record_hit_rate(self) -> float:
        return self.record_hits / self.record_lookups if self.record_lookups else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "key_lookups": self.key_lookups,
            "key_hit_rate": round(self.key_hit_rate, 4),
            "record_lookups": self.record_lookups,
            "record_hit_rate": round(self.record_hit_rate, 4),
            "derived_parent_records": self.derived_records,
        }


class _Lru(OrderedDict):
    """A bounded most-recently-used mapping."""

    def __init__(self, capacity: int) -> None:
        super().__init__()
        self.capacity = max(1, capacity)

    def take(self, key: Any) -> Any:
        if key not in self:
            return _MISSING
        self.move_to_end(key)
        return self[key]

    def give(self, key: Any, value: Any) -> None:
        self[key] = value
        self.move_to_end(key)
        while len(self) > self.capacity:
            self.popitem(last=False)


_MISSING = object()


def pick_index(
    rng: random.Random,
    count: int,
    *,
    distribution: str = "uniform",
    record_index: int = 0,
    skew: float = 1.6,
) -> int:
    """Choose which parent record a child points at.

    ``uniform``
        Every parent equally likely. The right default, and wrong for almost
        every real dataset - which is why the others exist.

    ``skewed``
        A power law: the parents at the head of the range attract
        disproportionately many references, and the tail attracts few. Real
        activity looks like this, and a uniform reference distribution
        produces data that is *valid* and behaves nothing like the thing it is
        imitating.

        ``skew`` sets how steep it is, and the effect is exact enough to
        choose deliberately: the busiest tenth of parents take
        ``0.1 ** (1 / skew)`` of the references.

        ===========  ==========================
        ``skew``     share taken by the top 10%
        ===========  ==========================
        1.0          10%  (uniform)
        1.6          24%  (the default)
        2.0          32%
        3.3          50%
        7.2          80%  ("80/20", near enough)
        ===========  ==========================

        The default is deliberately moderate. Over-skewing by default would
        put most of a dataset on a handful of parents and leave the rest
        barely exercised, which is a worse failure than being too even: too
        even is obvious, while too concentrated looks like real data until
        someone queries the tail.

    ``sequential`` / ``round_robin``
        Parent ``record_index % count``. Deterministic, exhaustive, and what
        you want when every parent must appear.
    """
    if count <= 0:
        raise GenerationError("cannot reference an entity with no records")
    if count == 1:
        return 0

    if distribution in ("sequential", "round_robin"):
        return record_index % count

    if distribution == "skewed":
        # Inverse-transform sampling on a bounded power law. Not a true Zipf -
        # normalising one over ten million ranks costs more than the realism
        # buys - but it has the shape that matters: a heavy head and a long
        # tail, and it costs two multiplications.
        draw = rng.random()
        exponent = max(0.05, 1.0 / max(skew, 1e-6))
        shaped = draw ** (1.0 / exponent) if exponent < 1 else draw**exponent
        return min(count - 1, int(count * shaped))

    return rng.randrange(count)


class EntityResolver:
    """Answers "what is parent *n* of this entity?" without keeping them.

    Built once per run and handed to every generator through the context.
    """

    def __init__(
        self,
        compiled: CompiledProject,
        *,
        key_cache: int = DEFAULT_KEY_CACHE,
        record_cache: int = DEFAULT_RECORD_CACHE,
        counts: dict[str, int] | None = None,
    ) -> None:
        self.compiled = compiled
        #: How many records each entity will actually have in this run. A run
        #: that overrides the counts (``--records 5``) must reference within
        #: what it produced, or a five-record preview points at record 17.
        self.counts = dict(counts or {})
        self.stats = ResolverStats()
        self._keys: dict[str, _Lru] = {}
        self._records: _Lru = _Lru(record_cache)
        self._key_cache_size = key_cache
        #: Set by the engine; deriving a parent record means generating one.
        self._generate_partial: Any = None
        #: Cached transitive dependency closures, per (entity, key field).
        self._closures: dict[tuple[str, str], tuple[str, ...]] = {}

    def bind(self, generate_partial: Any) -> None:
        """Attach the engine's partial-generation callable.

        The resolver needs to generate records and the engine needs the
        resolver, so one of them has to be handed to the other after
        construction. This is that seam.
        """
        self._generate_partial = generate_partial

    # -- shape -------------------------------------------------------------- #

    def entity(self, name: str) -> CompiledEntity:
        try:
            return self.compiled.entity(name)
        except KeyError as exc:
            raise SchemaError(str(exc)) from exc

    def count_of(self, name: str) -> int:
        override = self.counts.get(name)
        return override if override is not None else self.entity(name).count

    def key_field(self, name: str, field_name: str | None = None) -> str:
        """Which field of ``name`` a reference points at.

        An explicit field wins; otherwise the entity's primary key; otherwise
        its first field, because a reference to an entity that declares no key
        is still more useful than an error.
        """
        entity = self.entity(name)
        if field_name:
            if field_name not in entity.spec.fields:
                known = ", ".join(entity.spec.field_names())
                raise SchemaError(f"entity '{name}' has no field '{field_name}'. Fields: {known}")
            return field_name

        primary = entity.spec.resolved_primary_key()
        if primary:
            return primary
        first = entity.field_order[0] if entity.field_order else None
        if first is None:
            raise SchemaError(f"entity '{name}' has no fields to reference")
        return first

    def closure_for(self, entity_name: str, field_name: str) -> tuple[str, ...]:
        """Every field that must exist before ``field_name`` can be produced.

        A key is often a plain sequence, in which case this is one field. When
        it is a template over a name, it is three. Computing it once per
        (entity, field) keeps the per-reference cost to the generation itself.
        """
        cached = self._closures.get((entity_name, field_name))
        if cached is not None:
            return cached

        entity = self.entity(entity_name)
        by_name = {compiled.name: compiled for compiled in entity.fields}
        needed: list[str] = []
        seen: set[str] = set()

        def visit(name: str) -> None:
            if name in seen:
                return
            seen.add(name)
            compiled = by_name.get(name)
            if compiled is None:
                return
            for dependency in compiled.dependencies:
                visit(dependency)
            needed.append(name)

        visit(field_name)
        # Keep the entity's own order, so generation happens in a valid one.
        order = [name for name in entity.field_order if name in seen]
        result = tuple(order or needed)
        self._closures[(entity_name, field_name)] = result
        return result

    # -- resolution --------------------------------------------------------- #

    def key_at(self, entity_name: str, index: int, field_name: str | None = None) -> Any:
        """The value of ``entity_name``'s key field at ``index``."""
        key_field = self.key_field(entity_name, field_name)
        cache = self._keys.setdefault(entity_name + "." + key_field, _Lru(self._key_cache_size))

        self.stats.key_lookups += 1
        cached = cache.take(index)
        if cached is not _MISSING:
            self.stats.key_hits += 1
            return cached

        values = self._derive(entity_name, index, self.closure_for(entity_name, key_field))
        value = values.get(key_field)
        cache.give(index, value)
        return value

    def record_at(self, entity_name: str, index: int) -> GeneratedRecord:
        """The whole of ``entity_name``'s record at ``index``.

        Used by fields that read a parent's other columns - an email built from
        a company's domain, a login event that needs its employee's timezone.
        Costlier than a key, so cached separately and more tightly.
        """
        self.stats.record_lookups += 1
        cached = self._records.take((entity_name, index))
        if cached is not _MISSING:
            self.stats.record_hits += 1
            return cached

        record = self._derive_record(entity_name, index)
        self._records.give((entity_name, index), record)
        return record

    def _derive(self, entity_name: str, index: int, fields: Sequence[str]) -> dict[str, Any]:
        if self._generate_partial is None:
            raise GenerationError(
                "references need a generation engine; the resolver was never bound"
            )
        self.stats.derived_records += 1
        return self._generate_partial(entity_name, index, fields)

    def _derive_record(self, entity_name: str, index: int) -> GeneratedRecord:
        from ..core.record import GeneratedRecord

        entity = self.entity(entity_name)
        # Provider-backed fields are skipped: resolving a parent must not make
        # a model call, or one login event would cost a biography.
        wanted = [
            compiled.name
            for compiled in entity.fields
            if type(compiled.generator).requires_provider is None
        ]
        values = self._derive(entity_name, index, wanted)
        return GeneratedRecord(
            entity=entity_name,
            id=str(values.get(self.key_field(entity_name), index)),
            values=values,
        )

    # -- validation support -------------------------------------------------- #

    def is_valid_key(self, entity_name: str, field_name: str | None, value: Any) -> bool:
        """Whether ``value`` could be a key of ``entity_name``.

        Only meaningful for keys derived from the record index, which covers
        every generator Cacophony recommends for a primary key. Anything else
        returns ``True`` rather than guessing, because a referential check that
        raises false alarms is worse than one that admits its limits.
        """
        key_field = self.key_field(entity_name, field_name)
        entity = self.entity(entity_name)
        compiled = next((item for item in entity.fields if item.name == key_field), None)
        if compiled is None or compiled.generator.name != "sequence":
            return True

        generator = compiled.generator
        start = getattr(generator, "start", 1)
        step = getattr(generator, "step", 1)
        try:
            index = (_sequence_number(value) - start) // step
        except (TypeError, ValueError):
            return False
        return 0 <= index < self.count_of(entity_name) and self.key_at(entity_name, index) == value

    def describe(self) -> dict[str, Any]:
        return self.stats.to_dict()


def _sequence_number(value: Any) -> int:
    """Recover the counter from a sequence value, formatted or not."""
    if isinstance(value, int):
        return value
    digits = "".join(character for character in str(value) if character.isdigit())
    if not digits:
        raise ValueError("no digits in the key")
    return int(digits)


@dataclass(slots=True)
class ReferenceLink:
    """A reference a record made, recorded so related fields can follow it."""

    entity: str
    index: int
    key: Any
    field: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"entity": self.entity, "index": self.index, "key": self.key}


@dataclass(slots=True)
class RecordLinks:
    """Every reference one record made, keyed by the entity it points at."""

    links: dict[str, ReferenceLink] = field(default_factory=dict)

    def add(self, link: ReferenceLink) -> None:
        self.links[link.entity] = link

    def index_of(self, entity: str) -> int | None:
        link = self.links.get(entity)
        return link.index if link else None

    def clear(self) -> None:
        self.links.clear()

    def __bool__(self) -> bool:
        return bool(self.links)


def harmonic(count: int) -> float:
    """H(n), used when describing how skewed a reference distribution is."""
    if count <= 0:
        return 0.0
    if count < 1000:
        return sum(1.0 / rank for rank in range(1, count + 1))
    # Euler-Mascheroni approximation; exact enough for a description.
    return math.log(count) + 0.5772156649 + 1.0 / (2 * count)

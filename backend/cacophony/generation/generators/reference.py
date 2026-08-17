"""Foreign-key generation (design document sections 8, 15).

    Company 1 ---- N Employee
    Employee 1 ---- N LoginEvent

A reference picks a parent record and returns its key. Because parent records
are addressable by index (section 75), picking one is arithmetic rather than a
lookup - so a hundred million login events pointing at five thousand employees
cost no memory beyond a small cache.

The interesting option is ``distribution``. Uniform references produce data
that is valid and behaves nothing like reality: real activity is concentrated,
with the busy parents attracting disproportionately many children. ``skewed``
is usually the more honest choice for event data - see
:func:`~cacophony.generation.relations.pick_index` for what ``skew`` does to
the shape - and ``sequential`` for the cases where every parent must appear
exactly once.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ...core.errors import GenerationError
from ...core.interfaces import SyncGenerator
from ..registry import register_generator
from ..relations import REFERENCE_DISTRIBUTIONS, pick_index
from .base import OptionsMixin
from .deferred import PlaceholderMixin

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from ...core.context import GenerationContext

__all__ = ["ReferenceGenerator"]

#: Key under which a record's references are recorded, so that a later field
#: reading ``company.domain`` knows which company this record chose.
LINKS_KEY = "__references__"


@register_generator("reference", aliases=("fk", "foreign_key", "belongs_to"))
class ReferenceGenerator(OptionsMixin, PlaceholderMixin, SyncGenerator):
    """A foreign-key-style reference to another entity.

    Options:
        ``entity``        the entity referenced (required)
        ``field``         which field to point at; defaults to its primary key
        ``distribution``  ``uniform`` (default), ``skewed``, ``sequential``
                          or ``round_robin``
        ``skew``          how concentrated ``skewed`` is; higher is heavier
        ``unique``        one child per parent, in order
        ``null_probability`` inherited from the field, as everywhere else

    ``on_unavailable`` still applies, for the case where the referenced entity
    is not part of the current run.

    **A self-reference points backwards.** A field referencing its own entity -
    a manager, a parent comment, a superseded ticket - may only choose a record
    with a lower index. Two reasons, and either would be sufficient.

    A management chain that can point forwards is a management chain with cycles
    in it, and no query over it terminates. And an enforced foreign key is
    checked on insert, so a row pointing at a row that does not exist yet fails
    immediately - which is exactly what happened the first time a recipe in the
    catalogue used one.

    Record zero has nobody to point at, so it gets null. That is correct: the
    top of a hierarchy has no parent.
    """

    deterministic = True
    cost_class = "cpu"

    def prepare(self) -> None:
        target = self.opt_str("entity", None, "references", "to")
        if target is None:
            raise self._fail("option 'entity' is required")
        self.target: str = target
        self.target_field = self.opt_str("field", None, "key")

        # `unique: true` written on the field means the same thing here as it
        # would in an option bag - a column of foreign keys with no repeats is
        # exactly "one child per parent" - and the field spec claims the key
        # before the option bag sees it. Reading both is what makes the
        # obvious spelling work.
        self.unique = self.opt_bool("unique", False) or bool(
            self.field is not None and self.field.unique
        )
        self.distribution = self.opt_choice(
            "distribution", REFERENCE_DISTRIBUTIONS, "sequential" if self.unique else "uniform"
        )
        if self.unique and self.distribution not in ("sequential", "round_robin"):
            # "One child per parent" and "pick at random" cannot both hold, and
            # uniqueness is the harder promise: a duplicate breaks a database
            # constraint, while an unskewed distribution merely looks tidy.
            self.distribution = "sequential"

        self.skew = self.opt_float("skew", 1.6) or 1.6
        if self.skew <= 0:
            raise self._fail("option 'skew' must be positive")

        self.on_unavailable = self.opt_choice(
            "on_unavailable", ("error", "placeholder", "null"), "error"
        )
        #: Whether the referenced record should also be made available to other
        #: fields of this record. Set by the compiler when something asks for
        #: ``<entity>.<field>``.
        self.expose_record = self.opt_bool("expose", False)

    def dependencies(self) -> Sequence[str]:
        return ()

    def generate_sync(self, context: GenerationContext) -> Any:
        resolver = getattr(context, "resolver", None)
        if resolver is None:
            return self._unavailable(
                context,
                f"this field references '{self.target}', but no entity resolver is "
                "attached. Generate the referenced entity in the same run, or set "
                "'on_unavailable: placeholder'.",
            )

        try:
            count = resolver.count_of(self.target)
        except Exception as exc:
            return self._unavailable(context, str(exc))

        if count <= 0:
            return self._unavailable(
                context, f"entity '{self.target}' generates no records to reference"
            )

        # A self-reference may only look backwards; see the class docstring.
        if self.target == context.entity.name:
            count = min(count, context.record_index)
            if count <= 0:
                return None

        index = pick_index(
            context.rng(),
            count,
            distribution=self.distribution,
            record_index=context.record_index,
            skew=self.skew,
        )

        try:
            key = resolver.key_at(self.target, index, self.target_field)
        except Exception as exc:
            return self._unavailable(context, str(exc))

        # Record the choice so a sibling field reading `<target>.<field>` gets
        # the record this row actually pointed at, rather than a different one.
        links = context.extras.setdefault(LINKS_KEY, {})
        links[self.target] = index
        return key

    def _unavailable(self, context: GenerationContext, reason: str) -> Any:
        if self.on_unavailable == "null":
            return None
        if self.on_unavailable == "placeholder":
            return self._fit(self.placeholder(context))
        raise GenerationError(reason)

    def placeholder(self, context: GenerationContext) -> Any:
        return f"[ref:{self.target}#{context.record_index}]"

    def describe(self) -> str:
        target = self.target + (f".{self.target_field}" if self.target_field else "")
        detail = self.distribution if self.distribution != "uniform" else ""
        return f"reference({target}{', ' + detail if detail else ''})"

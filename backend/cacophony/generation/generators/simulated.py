"""Generators that read the simulation (design document sections 17, 25, 26).

These four are the surface a schema touches. Everything they need - which
subject a record belongs to, where it falls in that subject's history, what
state has accumulated, whether a scenario has it - is computed once per record
by the engine and left in the context. A generator here only asks.

``event_time``   when this event happened, in order, shaped by the timeline
``subject``      which subject it belongs to, as that subject's key
``state``        a value folded over this subject's earlier events
``scenario``     what a scenario is doing to this record, if anything

The division matters: a generator that recomputed a subject's history per field
would turn one fold into five.
"""

from __future__ import annotations

import datetime as _dt
from typing import TYPE_CHECKING, Any

from ...core.errors import GenerationError
from ...core.interfaces import SyncGenerator
from ...core.types import DataType
from ..registry import register_generator
from .base import OptionsMixin

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from ...core.context import GenerationContext

__all__ = [
    "EventTimeGenerator",
    "ScenarioGenerator",
    "StateGenerator",
    "SubjectGenerator",
]

#: Where the engine leaves the simulation's answer for this record.
SIMULATION_KEY = "__simulation__"


def _references(source: str) -> list[str]:
    """Every free name an expression reads."""
    import ast

    try:
        tree = ast.parse(source, mode="eval")
    except SyntaxError:
        return []
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    return [
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name) and node.id not in called
    ]


def _frame(context: GenerationContext, what: str) -> Any:
    frame = context.extras.get(SIMULATION_KEY)
    if frame is None:
        raise GenerationError(
            f"{context.location}: '{what}' needs a simulation, but this entity declares "
            "none. Add a 'simulation:' block naming the subject entity."
        )
    return frame


@register_generator("event_time", aliases=("occurred_at", "timeline"))
class EventTimeGenerator(OptionsMixin, SyncGenerator):
    """When this event happened (section 25).

    Options:
        ``jitter``   how much an event may move within its own slot (0-1)
        ``offset``   hours added afterwards, for a per-record timezone
        ``spread``   ``ordered`` (default) or ``random``

    ``ordered`` places the *k*-th of a subject's *n* events at the point where
    the *k*-th event would fall given the timeline's shape, so a subject's
    events come out in chronological order without being sorted. ``random``
    draws independently, which is right for events that have no sequence.
    """

    def prepare(self) -> None:
        self.jitter = self.opt_float("jitter", 0.8) or 0.0
        self.offset_hours = self.opt_float("offset", 0.0, "timezone_offset") or 0.0
        self.offset_field = self.opt_str("offset_field", None, "timezone_field")
        self.spread = self.opt_choice("spread", ("ordered", "random"), "ordered")

    def dependencies(self) -> Sequence[str]:
        return (self.offset_field,) if self.offset_field else ()

    def generate_sync(self, context: GenerationContext) -> Any:
        frame = _frame(context, self.name)
        timeline = frame.timeline
        if timeline is None:
            raise GenerationError(
                f"{context.location}: this project declares no 'timeline:', so there is "
                "no period for events to happen in."
            )

        if self.spread == "random":
            moment = timeline.sample(context.rng())
        else:
            placement = frame.placement
            jitter = context.rng().random() * self.jitter if self.jitter else 0.0
            moment = timeline.ordered(placement.ordinal, placement.total, jitter=jitter)

        zoning = getattr(timeline, "zoning", None)
        offset = self.offset_hours
        if self.offset_field:
            offset += float(context.value(self.offset_field, 0.0) or 0.0)

        if zoning is not None and zoning.is_enabled:
            if offset:
                raise GenerationError(
                    f"{context.location}: this field shifts the clock by {offset} hours "
                    "and the project declares 'timeline.timezone'. Those are two answers "
                    "to the same question - an hour offset is the approximation real "
                    "zones replace. Drop 'offset'."
                )
            from ...simulation.timeline import localise

            moment = localise(
                moment,
                zoning.zone_for(
                    frame.placement.subject,
                    resolver=getattr(context, "resolver", None),
                    subject_entity=frame.subject_entity,
                ),
            )
        elif offset:
            moment += _dt.timedelta(hours=offset)

        field_type = self.field.type if self.field else DataType.DATETIME
        if field_type is DataType.DATE:
            return moment.date()
        if field_type is DataType.TIME:
            return moment.time()
        if field_type.is_textual:
            return moment.isoformat()
        return moment

    def describe(self) -> str:
        return f"event_time({self.spread})"


@register_generator("subject", aliases=("belongs_to_subject", "actor"))
class SubjectGenerator(OptionsMixin, SyncGenerator):
    """The subject this event belongs to (sections 15, 25).

    A reference, but not a *chosen* one: the allocation already decided whose
    event this is, so this reads that decision and resolves it to the subject's
    key. Which is what makes a subject's events consecutive and countable,
    where ``generator: reference`` makes them scattered and anonymous.

    Options:
        ``field``  which field of the subject to point at; default its key
    """

    deterministic = True

    def prepare(self) -> None:
        self.target_field = self.opt_str("field", None, "key")
        #: Set by the compiler when a sibling field reads the subject's record.
        self.expose_record = self.opt_bool("expose", False)

        # The entity this points at is on the *entity's* simulation block, not
        # on the field - the field says "the subject", and the entity says who
        # the subject is. Reading it here is what lets the compiler, the
        # referential validator and the database writers treat this as the
        # foreign key it is.
        simulation = getattr(self.entity, "simulation", None) if self.entity else None
        self.target: str = getattr(simulation, "subject", "") or ""
        if not self.target:
            raise self._fail(
                "this field is a subject reference, but the entity declares no "
                "'simulation:' block naming the subject entity"
            )

    def generate_sync(self, context: GenerationContext) -> Any:
        frame = _frame(context, self.name)
        resolver = getattr(context, "resolver", None)
        if resolver is None:
            raise GenerationError(f"{context.location}: no entity resolver is attached")

        # Record the choice the same way a reference does, so a field reading
        # `employee.department` gets *this* record's subject.
        from .reference import LINKS_KEY

        links = context.extras.setdefault(LINKS_KEY, {})
        links[frame.subject_entity] = frame.placement.subject
        return resolver.key_at(frame.subject_entity, frame.placement.subject, self.target_field)

    def describe(self) -> str:
        return f"subject({self.target})"


@register_generator("state", aliases=("running", "accumulated"))
class StateGenerator(OptionsMixin, SyncGenerator):
    """A value carried forward through this subject's events (section 26).

    Options:
        ``variable``  which state variable to read; defaults to the field name

    The fold itself is declared in the entity's ``simulation.state`` block and
    computed once per record. This only reads the result, so ten state fields
    cost one fold rather than ten.
    """

    def prepare(self) -> None:
        self.variable = self.opt_str("variable", None, "name", "of")

    def dependencies(self) -> Sequence[str]:
        """The fields the fold reads, so this lands in a later layer than they do.

        The fold is applied once per record after each dependency layer, so a
        state field must be ordered *after* everything its update expression
        reads - a balance needs the transaction's amount to exist. Declaring
        that here is what lets the compiler work it out, exactly as it does for
        a template or an expression.
        """
        simulation = getattr(self.entity, "simulation", None) if self.entity else None
        declared = getattr(simulation, "state", None) or {}
        if not declared:
            return ()

        own = set(self.entity.fields) if self.entity else set()
        variables = set(declared)
        names: list[str] = []
        for body in declared.values():
            sources = (
                [body]
                if isinstance(body, str)
                else [
                    body.get("initial"),
                    body.get("update"),
                ]
            )
            for source in sources:
                if not source:
                    continue
                names.extend(_references(str(source)))
        # A state variable reading another state variable is a fold, not a
        # field dependency, and this field itself is never its own input.
        return tuple(
            name
            for name in dict.fromkeys(names)
            if name in own
            and name not in variables
            and name != (self.field.name if self.field else "")
        )

    def generate_sync(self, context: GenerationContext) -> Any:
        frame = _frame(context, self.name)
        state = frame.resolve_state()
        name = self.variable or (self.field.name if self.field else "")
        if name not in state:
            known = ", ".join(sorted(state)) or "<none declared>"
            raise GenerationError(
                f"{context.location}: no state variable '{name}'. Declared: {known}"
            )
        return state[name]

    def describe(self) -> str:
        return f"state({self.variable or 'by field name'})"


@register_generator("scenario", aliases=("incident", "affected_by"))
class ScenarioGenerator(OptionsMixin, SyncGenerator):
    """What a scenario is doing to this record (section 17).

    Options:
        ``report``   ``name`` (default), ``phase``, ``involved`` or ``position``
        ``normal``   the value for a record no scenario touches

    A field like this is what makes a scenario *visible* in the data: without
    it a dataset contains an incident nobody can point at, which is fine for a
    detection exercise and useless for checking that the generator did what it
    was asked.
    """

    def prepare(self) -> None:
        self.report = self.opt_choice("report", ("name", "phase", "involved", "position"), "name")
        self.normal = self.opt("normal", None, "otherwise")

    def generate_sync(self, context: GenerationContext) -> Any:
        frame = context.extras.get(SIMULATION_KEY)
        involvement = getattr(frame, "involvement", None) if frame else None

        if involvement is None:
            if self.report == "involved":
                return False
            return self.normal

        if self.report == "involved":
            return True
        if self.report == "phase":
            return involvement.phase
        if self.report == "position":
            return round(involvement.position, 6)
        return involvement.scenario

    def describe(self) -> str:
        return f"scenario({self.report})"

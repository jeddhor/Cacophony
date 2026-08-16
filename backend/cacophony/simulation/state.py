"""Stateful simulation (design document section 26).

    Account balance: $500
    purchase:        -$30
    new balance:     $470

Some data cannot be generated as independent events. A balance is the sum of
everything that came before it; a device's firmware version only ever goes up;
a ticket that was closed cannot be closed again.

This is the one place where Cacophony's central property - record *n* is a pure
function of *n* - does not hold on its own, and it is worth being precise about
what replaces it rather than quietly giving it up.

**State is a fold over a partition, not over the dataset.** A balance belongs to
an account, not to the run. :mod:`cacophony.simulation.allocation` lays each
subject's events out contiguously, so the state of event *k* depends only on
events *0..k of that subject* - never on another account's transactions, and
never on the order in which subjects were generated.

**Replay, not persistence.** To resume at event 4,823,913 the machine does not
need a saved balance: it needs that subject's block, which is bounded by how
many events one account has. It replays from the start of the block, which
costs a few hundred cheap derivations, and continues. So a resumed run produces
the same balances as an uninterrupted one, and nothing has to be serialised
into a checkpoint and kept in step with the file.

**What is given up.** Events within one subject must be produced in order. That
is inherent - a running total has an order or it is not a running total - and
it costs nothing in practice, because the parallelism worth having is across
subjects, and there are usually thousands of those.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..core.errors import GenerationError, SchemaError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from .allocation import Allocation, Placement

__all__ = ["StateMachine", "StateVariable", "SubjectState"]


@dataclass(slots=True)
class StateVariable:
    """One value carried forward through a subject's events.

    ``initial`` and ``update`` are expressions evaluated by the same restricted
    evaluator the ``expression`` generator uses (section 8): an allow-list of
    node types and functions, no attribute access beyond dotted record lookups,
    no imports. A project file is something people share, and a state machine
    that could run arbitrary code would make opening one dangerous.
    """

    name: str
    initial: str = "0"
    update: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    #: Round to this many decimal places after each update. Money that drifts
    #: into 470.00000000000006 after nine transactions is money nobody trusts.
    precision: int | None = None

    def clamp(self, value: Any) -> Any:
        """Apply the bounds and rounding to a folded value.

        ``Decimal`` counts as a number here. It is the type money is generated
        as, and money is what state variables are mostly for - a bound that
        silently did nothing for the one type it was written for would let a
        balance declared ``min: 0`` go negative.
        """
        from decimal import Decimal
        from numbers import Real

        if isinstance(value, bool) or not isinstance(value, (Real, Decimal)):
            return value

        if self.minimum is not None and value < _like(self.minimum, value):
            value = _like(self.minimum, value)
        if self.maximum is not None and value > _like(self.maximum, value):
            value = _like(self.maximum, value)
        if self.precision is not None:
            value = round(value, self.precision)
        return value


@dataclass(slots=True)
class SubjectState:
    """The state of one subject, and how far through its block it has been run."""

    subject: int
    values: dict[str, Any] = field(default_factory=dict)
    #: The next ordinal this state is valid *for*. 0 means "initialised, no
    #: events applied".
    ordinal: int = 0

    def snapshot(self) -> dict[str, Any]:
        return dict(self.values)


class StateMachine:
    """Folds an entity's state variables over each subject's events.

    One machine per entity per run. It keeps a single subject's state at a
    time: blocks are contiguous, so generating an entity front to back visits
    each subject once and the cache never needs to be larger than one.
    """

    def __init__(
        self,
        variables: Sequence[StateVariable],
        allocation: Allocation,
        *,
        evaluate: Any = None,
        cache_subjects: int = 4,
    ) -> None:
        if not variables:
            raise SchemaError("a simulation declares no state variables")
        self.variables = list(variables)
        self.allocation = allocation
        #: Supplied by the engine: evaluates an expression against a namespace.
        self.evaluate = evaluate
        #: A handful of subjects, not one: concurrent entity workers may
        #: interleave, and re-replaying a block on every alternation would turn
        #: a linear fold into a quadratic one.
        self.cache_subjects = max(1, cache_subjects)

        self._states: dict[int, SubjectState] = {}
        self._order: list[int] = []
        self.replays = 0
        self.steps = 0

    # -- the fold ------------------------------------------------------------- #

    def initial_for(self, subject: int, context: dict[str, Any]) -> dict[str, Any]:
        """The state a subject starts with, before any of its events."""
        values: dict[str, Any] = {}
        namespace = {**context, "subject": subject}
        for variable in self.variables:
            values[variable.name] = variable.clamp(
                self._eval(variable.initial, {**namespace, **values}, variable.name, "initial")
            )
        return values

    def advance(
        self,
        placement: Placement,
        record: dict[str, Any],
        *,
        replay: Any = None,
        seed_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """The state as of ``placement``, having applied this record's event.

        ``replay`` regenerates an earlier event of the same subject, and is
        only called when the machine is asked for an ordinal it has not reached
        - a resumed run, or a preview that starts in the middle of a block.
        """
        state = self._states.get(placement.subject)
        if state is None or state.ordinal > placement.ordinal:
            state = SubjectState(
                subject=placement.subject,
                values=self.initial_for(placement.subject, seed_context or {}),
            )
            self._remember(state)

        # Catch up to this event, replaying whatever was skipped.
        while state.ordinal < placement.ordinal:
            if replay is None:
                raise GenerationError(
                    f"the simulation needs event {state.ordinal} of subject "
                    f"{placement.subject} to reach event {placement.ordinal}, but no "
                    "replay callable was provided"
                )
            self.replays += 1
            earlier = replay(placement.subject, state.ordinal)
            self._apply(state, earlier)

        self._apply(state, record)
        return state.snapshot()

    def _apply(self, state: SubjectState, record: dict[str, Any]) -> None:
        """Fold one event into a subject's state."""
        namespace = {**record, **state.values, "subject": state.subject, "ordinal": state.ordinal}
        updated = dict(state.values)
        for variable in self.variables:
            if variable.update is None:
                continue
            updated[variable.name] = variable.clamp(
                self._eval(
                    variable.update,
                    {**namespace, **updated},
                    variable.name,
                    "update",
                )
            )
        state.values = updated
        state.ordinal += 1
        self.steps += 1

    def _eval(self, source: str, namespace: dict[str, Any], name: str, what: str) -> Any:
        if self.evaluate is None:
            raise GenerationError("the simulation has no expression evaluator attached")
        try:
            return self.evaluate(source, namespace)
        except GenerationError:
            raise
        except Exception as exc:
            raise GenerationError(
                f"state variable '{name}' failed its {what} expression: {exc}"
            ) from exc

    def _remember(self, state: SubjectState) -> None:
        self._states[state.subject] = state
        self._order.append(state.subject)
        while len(self._order) > self.cache_subjects:
            self._states.pop(self._order.pop(0), None)

    # -- description ----------------------------------------------------------- #

    def describe(self) -> dict[str, Any]:
        return {
            "variables": [variable.name for variable in self.variables],
            "events_folded": self.steps,
            "replayed": self.replays,
            "replay_rate": round(self.replays / self.steps, 4) if self.steps else 0.0,
        }


def variables_from(declared: dict[str, Any]) -> list[StateVariable]:
    """Read state variables out of a schema block."""
    variables: list[StateVariable] = []
    for name, body in declared.items():
        if isinstance(body, str):
            variables.append(StateVariable(name=name, initial=body))
            continue
        if not isinstance(body, dict):
            raise SchemaError(
                f"state variable '{name}' must be an expression or a mapping, "
                f"got {type(body).__name__}"
            )
        variables.append(
            StateVariable(
                name=name,
                initial=str(body.get("initial", body.get("start", "0"))),
                update=str(body["update"]) if body.get("update") is not None else None,
                minimum=_number(body.get("min")),
                maximum=_number(body.get("max")),
                precision=int(body["precision"]) if body.get("precision") is not None else None,
            )
        )
    return variables


def _number(value: Any) -> float | None:
    return None if value is None else float(value)


def _like(bound: float, value: Any) -> Any:
    """The bound, as the same type as the value it is compared with.

    Comparing a ``Decimal`` with a ``float`` works, but assigning the float
    would change the column's type halfway down the dataset.
    """
    from decimal import Decimal

    return Decimal(str(bound)) if isinstance(value, Decimal) else bound

"""The per-record simulation frame (design document sections 17, 25, 26, 93).

One object per entity per run, and one small frozen answer per record.

The engine asks this once, before a record's fields are generated: whose event
is this, where does it sit in that subject's history, what state has
accumulated, and is a scenario doing anything to it. Every simulation-aware
generator then reads the answer rather than recomputing it - which is the
difference between one fold per record and one per field.

Ordering is the subtle part. State depends on the record's *own* fields (a
balance needs the transaction's amount), and those fields are generated after
this frame is built. So the frame is built in two steps: everything that is
known from the record's position up front, and the state fold applied once the
fields it reads exist. The engine drives both.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..core.errors import GenerationError, SchemaError
from ..simulation.allocation import Allocation, Placement
from ..simulation.state import StateMachine, variables_from

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..schema.plan import CompiledEntity, CompiledProject
    from ..simulation.scenarios import Involvement, ScenarioEngine
    from ..simulation.timeline import Timeline

__all__ = ["EntitySimulation", "SimulationFrame", "build_simulations"]


@dataclass(slots=True)
class SimulationFrame:
    """What the simulation knows about one record."""

    placement: Placement
    subject_entity: str
    timeline: Timeline | None = None
    state: dict[str, Any] = field(default_factory=dict)
    involvement: Involvement | None = None
    effects: dict[str, Any] = field(default_factory=dict)
    #: Applies this record's event to its subject's state. Called by the first
    #: `state` field to be generated rather than after every layer: a fold
    #: needs the fields its update expression reads, and only the dependency
    #: graph knows when those exist. Cleared once run, so ten state fields cost
    #: one fold.
    fold: Any = None

    def resolve_state(self) -> dict[str, Any]:
        """The state as of this event, folding it in if that has not happened."""
        if self.fold is not None:
            self.fold()
            self.fold = None
        return self.state

    @property
    def subject(self) -> int:
        return self.placement.subject

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "subject": self.placement.subject,
            "ordinal": self.placement.ordinal,
            "of": self.placement.total,
        }
        if self.involvement is not None:
            data["scenario"] = self.involvement.to_dict()
        if self.state:
            data["state"] = dict(self.state)
        return data


class EntitySimulation:
    """Everything one entity needs to behave as a history."""

    def __init__(
        self,
        entity: CompiledEntity,
        *,
        subject_entity: str,
        allocation: Allocation,
        timeline: Timeline | None = None,
        machine: StateMachine | None = None,
        scenarios: ScenarioEngine | None = None,
    ) -> None:
        self.entity = entity
        self.subject_entity = subject_entity
        self.allocation = allocation
        self.timeline = timeline
        self.machine = machine
        self.scenarios = scenarios

        #: Set by a live stream. A batch run lays each subject's events out in
        #: a contiguous block, which is what makes ordered histories and folded
        #: state cheap. A stream cannot: its events arrive mixed together,
        #: because that is what a stream *is*. So in stream mode the subject is
        #: drawn per event and the ordinal is counted in memory.
        self.streaming = False
        #: Live scenario windows repeat over this many seconds, so an incident
        #: recurs instead of happening once at the start of an endless stream.
        self.cycle_seconds = 3600.0
        self._counters: dict[int, int] = {}
        self._started = 0.0

    # -- per record ----------------------------------------------------------- #

    def stream_mode(self, *, cycle_seconds: float = 3600.0) -> None:
        """Switch to the layout a continuous stream needs (sections 35, 94)."""
        import time

        self.streaming = True
        self.cycle_seconds = max(1.0, cycle_seconds)
        self._counters = {}
        self._started = time.monotonic()

    def _stream_placement(self, index: int) -> Placement:
        """Whose event this is, when events interleave.

        The subject is drawn from the same weighted distribution the batch
        allocation would have used, seeded by the event's index - so a stream
        is still reproducible, and still concentrates on the busy subjects,
        without needing to know how many events there will ever be.
        """
        subject = self.allocation.sample_subject(index)
        ordinal = self._counters.get(subject, 0)
        self._counters[subject] = ordinal + 1
        # A stream has no total, so the subject's own count so far stands in.
        # Nothing that matters in stream mode divides by it: timestamps come
        # from the wall clock and scenario windows from the cycle.
        return Placement(subject=subject, ordinal=ordinal, total=ordinal + 1)

    def _stream_position(self) -> float:
        """Where the stream is within a scenario cycle."""
        import time

        return ((time.monotonic() - self._started) % self.cycle_seconds) / self.cycle_seconds

    def frame_for(self, index: int) -> SimulationFrame:
        """What is known before the record's fields exist."""
        placement = (
            self._stream_placement(index) if self.streaming else self.allocation.locate(index)
        )
        frame = SimulationFrame(
            placement=placement,
            subject_entity=self.subject_entity,
            timeline=self.timeline,
        )

        if self.scenarios is not None and not self.scenarios.is_noop:
            position = self._stream_position() if self.streaming else placement.quantile
            found = self.scenarios.involvement(
                self.entity.name, placement.subject, position=position
            )
            if found is not None:
                scenario, involvement = found
                frame.involvement = involvement
                frame.effects = self.scenarios.effects_for(scenario, involvement)
        return frame

    def fold_state(
        self, frame: SimulationFrame, values: dict[str, Any], *, replay: Any = None
    ) -> None:
        """Apply this record's event to its subject's state."""
        if self.machine is None:
            return
        frame.state = self.machine.advance(
            frame.placement,
            values,
            replay=replay,
            seed_context={"subject": frame.placement.subject, "of": frame.placement.total},
        )

    @property
    def has_state(self) -> bool:
        return self.machine is not None

    def describe(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "subject": self.subject_entity,
            "allocation": self.allocation.describe(),
        }
        if self.machine is not None:
            data["state"] = self.machine.describe()
        return data


def build_simulations(
    compiled: CompiledProject,
    *,
    timeline: Timeline | None = None,
    scenarios: ScenarioEngine | None = None,
    evaluate: Any = None,
    counts: dict[str, int] | None = None,
) -> dict[str, EntitySimulation]:
    """Compile every entity that declares a ``simulation:`` block."""
    built: dict[str, EntitySimulation] = {}
    sizes = counts or {}

    for entity in compiled.ordered_entities():
        spec = getattr(entity.spec, "simulation", None)
        if spec is None or not spec.is_enabled():
            continue

        subject_name = spec.subject
        if subject_name not in compiled.entities:
            known = ", ".join(compiled.entity_order)
            raise SchemaError(
                f"entity '{entity.name}' simulates events of '{subject_name}', which is "
                f"not an entity. Known entities: {known}"
            )

        events = sizes.get(entity.name, entity.count)
        subjects = sizes.get(subject_name, compiled.entity(subject_name).count)
        if subjects <= 0:
            raise SchemaError(
                f"entity '{entity.name}' simulates events of '{subject_name}', which "
                "generates no records"
            )

        allocation = Allocation(
            events,
            subjects,
            distribution=spec.distribution,
            skew=spec.skew,
            seed=compiled.seed,
            minimum=spec.minimum,
        )

        machine: StateMachine | None = None
        if spec.state:
            machine = StateMachine(variables_from(spec.state), allocation, evaluate=evaluate)

        built[entity.name] = EntitySimulation(
            entity,
            subject_entity=subject_name,
            allocation=allocation,
            timeline=timeline,
            machine=machine,
            scenarios=scenarios,
        )
    return built


def evaluator() -> Any:
    """An expression evaluator for state variables.

    The same restricted evaluator the ``expression`` generator uses: an
    allow-list of node types and functions, no attribute access beyond dotted
    lookups, no imports. State expressions arrive in shared project files and
    must not be a way to run code.
    """
    from .generators.text import ExpressionGenerator

    cache: dict[str, Any] = {}

    def evaluate(source: str, namespace: dict[str, Any]) -> Any:
        generator = cache.get(source)
        if generator is None:
            generator = ExpressionGenerator({"expression": source})
            cache[source] = generator
        return _run(generator, namespace)

    return evaluate


def _run(generator: Any, namespace: dict[str, Any]) -> Any:
    """Evaluate a compiled expression against a plain mapping."""
    scope = _StateNamespace(namespace, generator.FUNCTIONS)
    try:
        return eval(generator._code, {"__builtins__": {}}, scope)
    except GenerationError:
        raise
    except Exception as exc:
        raise GenerationError(str(exc)) from exc


class _StateNamespace(dict):
    """Resolves names in a state expression, and says so when it cannot."""

    def __init__(self, values: dict[str, Any], functions: dict[str, Any]) -> None:
        super().__init__()
        self._values = values
        self._functions = functions

    def __missing__(self, key: str) -> Any:
        if key in self._functions:
            return self._functions[key]
        if key in self._values:
            return self._values[key]
        known = ", ".join(sorted(self._values)) or "<nothing yet>"
        raise GenerationError(
            f"a state expression reads '{key}', which is neither a field of this "
            f"record, a state variable, nor a function. Available: {known}"
        )

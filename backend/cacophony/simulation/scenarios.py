"""The scenario engine (design document sections 6, 17, 71).

    Ransomware Incident

    User:     Robert Chen
    Endpoint: LAPTOP-RCHEN-493

    08:02 login              08:22 credential access
    08:17 phishing opened    08:26 lateral movement
    08:19 payload execution  08:31 file encryption begins

A scenario is a *reusable behavioural pattern* that modifies otherwise-normal
generated records. It is what turns a dataset of plausible rows into a dataset
something happened in - and that is the difference between data you can test a
detection rule against and data you cannot.

Three ideas carry the whole design.

**A scenario picks subjects, not records.** "Two per cent of employees are
compromised" selects employees, deterministically, from the project seed. Every
entity that references a selected employee then knows it is caught up in the
incident, so a login, an alert and a ticket agree about who was involved
without any of them being told separately.

**A scenario has a window.** Selected subjects are affected between two moments
inside the project's timeline, and the phases within that window are ordered:
initial access before execution before exfiltration. A record knows which phase
it falls in, so its severity and its message can differ accordingly.

**A scenario overrides, it does not generate.** Effects are field values -
constants, weighted choices, or expressions - applied after normal generation.
That keeps scenarios composable with everything else (validation still runs,
provenance still records what happened) and keeps them out of the generation
path for the 98% of records they do not touch.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..core.errors import SchemaError
from ..core.seeds import mix_seed
from .timeline import parse_moment

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from .timeline import Timeline

__all__ = [
    "Involvement",
    "Phase",
    "Scenario",
    "ScenarioEngine",
    "compile_scenarios",
]

#: Distinguishes scenario subject selection from every other seed derivation.
_SALT = 0x5CE_1A2B


@dataclass(slots=True)
class Phase:
    """One stage of a scenario, occupying a share of its window."""

    name: str
    #: Where the phase begins and ends within the window, as fractions.
    start: float = 0.0
    end: float = 1.0
    #: Field values applied to records falling in this phase.
    effects: dict[str, Any] = field(default_factory=dict)

    def covers(self, position: float) -> bool:
        return self.start <= position <= self.end


@dataclass(slots=True)
class Scenario:
    """A behavioural pattern applied to a fraction of an entity's records."""

    name: str
    description: str | None = None
    #: The entity whose members are selected - employees, accounts, devices.
    subject_entity: str = ""
    #: Which entities' records the scenario touches. Empty means every entity
    #: that references the subject.
    applies_to: tuple[str, ...] = ()
    #: The share of subjects caught up in it.
    affects_fraction: float = 0.0
    #: Values applied to every affected record, whatever its phase.
    effects: dict[str, Any] = field(default_factory=dict)
    phases: tuple[Phase, ...] = ()
    #: The window, as fractions of the project timeline. A scenario that
    #: occupies the whole period is a background condition; one that occupies
    #: 0.4-0.45 is an incident.
    window: tuple[float, float] = (0.0, 1.0)
    #: How many events an affected subject produces relative to normal. A
    #: compromised account is *busier*, and a dataset where the incident does
    #: not change the volume is one where volume is not a signal.
    rate_multiplier: float = 1.0
    enabled: bool = True
    parameters: dict[str, Any] = field(default_factory=dict)

    def touches(self, entity: str) -> bool:
        return self.enabled and (not self.applies_to or entity in self.applies_to)

    def phase_at(self, position: float) -> Phase | None:
        """Which phase a record at ``position`` through the window falls in."""
        for phase in self.phases:
            if phase.covers(position):
                return phase
        return None


@dataclass(frozen=True, slots=True)
class Involvement:
    """A record's part in a scenario, if it has one."""

    scenario: str
    subject: int
    phase: str | None
    position: float
    started_at: _dt.datetime | None = None
    ended_at: _dt.datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "scenario": self.scenario,
            "subject": self.subject,
            "position": round(self.position, 6),
        }
        if self.phase:
            data["phase"] = self.phase
        if self.started_at:
            data["started_at"] = self.started_at.isoformat()
        return data


class ScenarioEngine:
    """Decides which records a scenario touches, and what it does to them.

    Subject selection is a hash, not a sample: whether employee 4,823 is
    compromised is a pure function of the project seed and the scenario's name,
    so it is the same on every run, in every order, at any scale, and no list
    of chosen subjects is ever held.
    """

    def __init__(
        self,
        scenarios: Sequence[Scenario],
        *,
        seed: int = 0,
        timeline: Timeline | None = None,
    ) -> None:
        self.scenarios = [scenario for scenario in scenarios if scenario.enabled]
        self.seed = seed
        self.timeline = timeline
        self.applied = 0
        self._counts: dict[str, int] = {}

    # -- selection ------------------------------------------------------------ #

    def selects(self, scenario: Scenario, subject: int) -> bool:
        """Whether this subject is caught up in this scenario."""
        if scenario.affects_fraction <= 0.0:
            return False
        if scenario.affects_fraction >= 1.0:
            return True
        # A 32-bit slice of the mixed seed, compared against the fraction.
        draw = mix_seed(self.seed, _SALT, subject ^ _name_seed(scenario.name)) & 0xFFFFFFFF
        return draw / 0x100000000 < scenario.affects_fraction

    def subjects_for(self, scenario: Scenario, population: int) -> list[int]:
        """Which subjects a scenario selects. For reporting, not for generation."""
        return [subject for subject in range(population) if self.selects(scenario, subject)]

    # -- application ----------------------------------------------------------- #

    def involvement(
        self, entity: str, subject: int, *, position: float
    ) -> tuple[Scenario, Involvement] | None:
        """The scenario touching this record, if any.

        ``position`` is how far through the *subject's own history* the record
        falls, which the allocation already knows. That is what places a record
        inside or outside a scenario's window without needing its timestamp.
        """
        for scenario in self.scenarios:
            if not scenario.touches(entity) or not self.selects(scenario, subject):
                continue
            low, high = scenario.window
            if not low <= position <= high:
                continue

            span = high - low
            within = (position - low) / span if span > 0 else 0.0
            phase = scenario.phase_at(within)
            return scenario, Involvement(
                scenario=scenario.name,
                subject=subject,
                phase=phase.name if phase else None,
                position=within,
                started_at=self.timeline.at(low) if self.timeline else None,
                ended_at=self.timeline.at(high) if self.timeline else None,
            )
        return None

    def effects_for(self, scenario: Scenario, involvement: Involvement) -> dict[str, Any]:
        """The field values this record should take on.

        Phase effects win over scenario-wide ones: a scenario says "this
        account is compromised", a phase says "and right now it is exfiltrating".
        """
        effects = dict(scenario.effects)
        phase = scenario.phase_at(involvement.position)
        if phase is not None:
            effects.update(phase.effects)
        return effects

    def record_applied(self, scenario: str) -> None:
        self.applied += 1
        self._counts[scenario] = self._counts.get(scenario, 0) + 1

    def rate_for(self, entity: str, subject: int) -> float:
        """How much busier this subject is than a normal one."""
        multiplier = 1.0
        for scenario in self.scenarios:
            if scenario.touches(entity) and self.selects(scenario, subject):
                multiplier *= scenario.rate_multiplier
        return multiplier

    # -- description ------------------------------------------------------------ #

    @property
    def is_noop(self) -> bool:
        return not self.scenarios

    def describe(self) -> dict[str, Any]:
        return {
            "scenarios": [scenario.name for scenario in self.scenarios],
            "records_affected": self.applied,
            "by_scenario": dict(self._counts),
        }


def _name_seed(name: str) -> int:
    """A stable integer for a scenario's name, so two scenarios select
    different subjects even at the same fraction."""
    value = 0
    for character in name:
        value = (value * 131 + ord(character)) & 0xFFFFFFFF
    return value


# --------------------------------------------------------------------------- #
# Compilation from the schema
# --------------------------------------------------------------------------- #


def compile_scenarios(declared: Sequence[Any], *, entities: Sequence[str]) -> list[Scenario]:
    """Turn ``scenarios:`` declarations into engine scenarios."""
    compiled: list[Scenario] = []
    for spec in declared:
        parameters = dict(getattr(spec, "parameters", {}) or {})
        subject = str(parameters.get("subject") or parameters.get("subject_entity") or "")
        applies = tuple(getattr(spec, "applies_to", ()) or ())

        for name in applies:
            if name not in entities:
                raise SchemaError(
                    f"scenario '{spec.name}' applies to '{name}', which is not an entity. "
                    f"Known entities: {', '.join(entities)}"
                )
        if subject and subject not in entities:
            raise SchemaError(
                f"scenario '{spec.name}' has subject '{subject}', which is not an entity. "
                f"Known entities: {', '.join(entities)}"
            )

        compiled.append(
            Scenario(
                name=spec.name,
                description=getattr(spec, "description", None),
                subject_entity=subject,
                applies_to=applies,
                affects_fraction=float(getattr(spec, "affects_fraction", 0.0)),
                effects=dict(parameters.get("effects") or {}),
                phases=tuple(_phases(parameters.get("phases") or [], spec.name)),
                window=_window(parameters.get("window"), spec.name),
                rate_multiplier=float(parameters.get("rate_multiplier", 1.0)),
                enabled=bool(getattr(spec, "enabled", True)),
                parameters=parameters,
            )
        )
    return compiled


def _phases(declared: Any, scenario: str) -> list[Phase]:
    """Read phases, sharing the window evenly where they do not say."""
    if isinstance(declared, dict):
        declared = [{"name": name, **(body or {})} for name, body in declared.items()]
    if not isinstance(declared, list):
        raise SchemaError(f"scenario '{scenario}': 'phases' must be a list or a mapping")

    phases: list[Phase] = []
    count = len(declared)
    for index, body in enumerate(declared):
        if not isinstance(body, dict):
            raise SchemaError(f"scenario '{scenario}': every phase must be a mapping")
        name = str(body.get("name") or f"phase_{index + 1}")
        start = float(body["start"]) if "start" in body else index / count
        end = float(body["end"]) if "end" in body else (index + 1) / count
        phases.append(
            Phase(name=name, start=start, end=end, effects=dict(body.get("effects") or {}))
        )
    return phases


def _window(declared: Any, scenario: str) -> tuple[float, float]:
    """The scenario's share of the timeline, as fractions.

    Accepts fractions directly, or ``{at: 0.4, duration: 0.05}`` for the common
    case of "an incident, this far in, this long".
    """
    if declared is None:
        return (0.0, 1.0)
    if isinstance(declared, (list, tuple)) and len(declared) == 2:
        return (float(declared[0]), float(declared[1]))
    if isinstance(declared, dict):
        if "start" in declared and "end" in declared:
            return (float(declared["start"]), float(declared["end"]))
        at = float(declared.get("at", 0.0))
        duration = float(declared.get("duration", 1.0 - at))
        return (at, min(1.0, at + duration))
    raise SchemaError(f"scenario '{scenario}': 'window' must be [start, end] or {{at, duration}}")


def resolve_moment(value: Any, timeline: Timeline) -> _dt.datetime:
    """A window bound written as a date rather than a fraction."""
    return parse_moment(value, what="a scenario window bound")

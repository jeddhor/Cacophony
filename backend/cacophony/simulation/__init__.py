"""Synthetic worlds (design document sections 16, 17, 24, 25, 26, 78, 93).

The layer that makes a dataset something that *happened* rather than a pile of
plausible rows.

:mod:`~cacophony.simulation.timeline`
    When things happen. A period plus a shape - weekday, hour, season,
    holidays, spikes - compiled into a distribution that can be sampled
    randomly or walked in order.

:mod:`~cacophony.simulation.allocation`
    Whose things they are. Events are laid out in contiguous blocks per
    subject, so "the fortieth login of this employee" is a question a record
    can answer in O(log P) without sorting or holding anything.

:mod:`~cacophony.simulation.state`
    What carries forward. Balances, counters and statuses folded over a
    subject's own events, replayed rather than persisted so a resumed run
    agrees with an uninterrupted one.

:mod:`~cacophony.simulation.scenarios`
    What went wrong. A reusable behavioural pattern applied to a fraction of
    subjects over a window, in phases, correlated across every entity that
    references them.

:mod:`~cacophony.simulation.chaos`
    What is broken on purpose. Section 78's Discord controls, recorded in
    provenance and exempted from validation so deliberate damage is not
    reported as a defect.

:mod:`~cacophony.simulation.world`
    What persists. A named seed and schema revision, so a second dataset can
    be generated against the same five thousand people.
"""

from .allocation import SHARE_DISTRIBUTIONS, Allocation, Placement
from .chaos import CHAOS_PRESETS, ChaosInjector, ChaosStats
from .scenarios import Involvement, Phase, Scenario, ScenarioEngine, compile_scenarios
from .state import StateMachine, StateVariable, variables_from
from .timeline import SHAPES, Timeline, TimelineShape, parse_moment
from .world import World, WorldStore

__all__ = [
    "CHAOS_PRESETS",
    "SHAPES",
    "SHARE_DISTRIBUTIONS",
    "Allocation",
    "ChaosInjector",
    "ChaosStats",
    "Involvement",
    "Phase",
    "Placement",
    "Scenario",
    "ScenarioEngine",
    "StateMachine",
    "StateVariable",
    "Timeline",
    "TimelineShape",
    "World",
    "WorldStore",
    "compile_scenarios",
    "parse_moment",
    "variables_from",
]

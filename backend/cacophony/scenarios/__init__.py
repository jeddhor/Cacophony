"""The scenario engine (design document sections 17 and 71).

A scenario modifies otherwise-normal generated behaviour: a compromised
employee produces suspicious logins, impossible travel, MFA failures, abnormal
downloads and alert records, correlated across entities and across time.

:class:`cacophony.schema.models.ScenarioSpec` already carries the declaration,
so a project written today records its scenarios and they survive into the
phase that executes them.
"""

#: Scenario behaviours contributed by plugins (section 44).
#:
#: The declarative form in `ScenarioSpec` covers the scenarios the templates
#: need - phases, windows, weighted effects - and a `ScenarioPlugin` is for the
#: ones that need code: a behaviour that reads the record it is modifying and
#: decides something a weighted choice cannot express.
_EXTRA_SCENARIOS: dict[str, type] = {}


def extra_scenarios() -> dict[str, type]:
    """The scenario behaviours plugins have contributed."""
    return _EXTRA_SCENARIOS


__all__ = ["extra_scenarios"]

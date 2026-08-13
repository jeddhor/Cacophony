"""The scenario engine (design document sections 17 and 71).

A scenario modifies otherwise-normal generated behaviour: a compromised
employee produces suspicious logins, impossible travel, MFA failures, abnormal
downloads and alert records, correlated across entities and across time.

:class:`cacophony.schema.models.ScenarioSpec` already carries the declaration,
so a project written today records its scenarios and they survive into the
phase that executes them.
"""

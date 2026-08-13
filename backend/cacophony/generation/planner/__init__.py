"""Generation planning (design document section 28).

The planner itself lives in :mod:`cacophony.schema.compiler` and
:mod:`cacophony.schema.plan`, because a plan is a compiled artifact of the
schema. This package holds planning strategies that need run-time information
rather than schema information - sampling policies, batch sizing and workload
shaping - which arrive with the job system.
"""

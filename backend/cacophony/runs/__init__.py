"""The job system (design document sections 29-32, 55, 56, 64, 65).

A run is a set of jobs, each with a state and a checkpoint. The Conductor
plans them, executes them within the configured limits, and can be paused,
resumed or cancelled while it does.

Checkpointing is cheap here for a structural reason. Records are addressed by
index and their seeds are derived by hashing that index (section 75), so a
checkpoint is one integer per job - "I finished 6,830,000" - rather than a
serialised RNG state. That is what makes section 32's "Resume Run" a small
feature instead of a large one.
"""

from .config import ResourceLimits, RunConfig
from .coordinator import Conductor, PlannedJob, RunHandle, RunOutcome
from .events import EventBus, EventKind, RunEvent
from .state import JobState, JobType, RunState

__all__ = [
    "Conductor",
    "EventBus",
    "EventKind",
    "JobState",
    "JobType",
    "PlannedJob",
    "ResourceLimits",
    "RunConfig",
    "RunEvent",
    "RunHandle",
    "RunOutcome",
    "RunState",
]

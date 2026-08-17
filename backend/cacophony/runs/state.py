"""Run and job lifecycles (design document section 29).

Section 29 gives the job states outright::

    Queued  Running  Paused  Retrying  Completed  Failed  Cancelled

Transitions are declared rather than implied. A long run is exactly the place
where an impossible transition - a cancelled job quietly restarting, a
completed one going back to running - would corrupt a checkpoint and be found
out hours later, so the rules are data and are checked.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["JobState", "JobType", "RunState", "TERMINAL_JOB_STATES", "TERMINAL_RUN_STATES"]


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    RETRYING = "retrying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in TERMINAL_JOB_STATES

    @property
    def is_resumable(self) -> bool:
        """Whether a job in this state still has work that can be picked up."""
        return self in {JobState.QUEUED, JobState.RUNNING, JobState.PAUSED, JobState.RETRYING,
                        JobState.FAILED}

    def can_move_to(self, target: JobState) -> bool:
        return target in _JOB_TRANSITIONS.get(self, frozenset())


TERMINAL_JOB_STATES = frozenset({JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED})

_JOB_TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.QUEUED: frozenset({JobState.RUNNING, JobState.CANCELLED, JobState.PAUSED}),
    JobState.RUNNING: frozenset(
        {
            JobState.COMPLETED,
            JobState.FAILED,
            JobState.PAUSED,
            JobState.RETRYING,
            JobState.CANCELLED,
        }
    ),
    JobState.RETRYING: frozenset({JobState.RUNNING, JobState.FAILED, JobState.CANCELLED}),
    JobState.PAUSED: frozenset({JobState.RUNNING, JobState.CANCELLED, JobState.QUEUED}),
    # A failed job may be resumed, which is the whole point of checkpointing.
    JobState.FAILED: frozenset({JobState.QUEUED, JobState.RUNNING}),
    JobState.COMPLETED: frozenset(),
    JobState.CANCELLED: frozenset(),
}


class RunState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in TERMINAL_RUN_STATES

    @property
    def is_resumable(self) -> bool:
        """A completed run has nothing to resume; anything else might.

        ``running`` counts as resumable because a process killed mid-run leaves
        its run marked running for ever - nothing gets the chance to write a
        different state. Refusing to resume those would make an unclean
        shutdown unrecoverable, which is precisely the case section 32 exists
        to handle.
        """
        return self is not RunState.COMPLETED

    def can_move_to(self, target: RunState) -> bool:
        return target in _RUN_TRANSITIONS.get(self, frozenset())


TERMINAL_RUN_STATES = frozenset({RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED})

_RUN_TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    RunState.QUEUED: frozenset({RunState.RUNNING, RunState.CANCELLED}),
    RunState.RUNNING: frozenset(
        {RunState.COMPLETED, RunState.FAILED, RunState.PAUSED, RunState.CANCELLED}
    ),
    RunState.PAUSED: frozenset({RunState.RUNNING, RunState.CANCELLED, RunState.FAILED}),
    RunState.FAILED: frozenset({RunState.RUNNING}),
    RunState.CANCELLED: frozenset({RunState.RUNNING}),
    RunState.COMPLETED: frozenset(),
}


class JobType(StrEnum):
    """Section 29's job types.

    ``entity_batch`` and ``export`` are what a run is made of today.
    ``llm_batch``, ``image`` and ``audio`` are named because section 29 names
    them, and because they become separate leased units in the distributed
    phase (section 95) where a GPU node picks up image work independently. For
    now that work happens inside the entity job that needs it, which is both
    simpler and faster: no scheduling round trip between a record and the
    biography that belongs to it.
    """

    ENTITY_BATCH = "entity_batch"
    LLM_BATCH = "llm_batch"
    IMAGE = "image"
    AUDIO = "audio"
    EXPORT = "export"
    VALIDATION = "validation"

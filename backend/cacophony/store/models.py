"""The project metadata database (design document sections 42 and 73).

Section 42 lists the tables a Cacophony store should have, and is explicit that
generated datasets themselves must **not** live here. This is bookkeeping: what
was run, from which schema, how far it got, and what happened.

A deliberate departure from section 42's table list
---------------------------------------------------
Section 42 names ``entities``, ``fields``, ``relationships``,
``generator_configs`` and ``scenarios`` as tables. They are not tables here.
They are all part of the project *schema*, and section 74 wants that schema to
be readable YAML that a team reviews in Git. Shredding it into rows would
create a second source of truth that has to be kept in step with the file, and
the first time the two disagreed the file would be right.

Instead the schema is stored whole, as text, in :class:`SchemaRevision` - which
is what section 73 actually asks for: "Generation runs should record the exact
schema revision used." A run points at the revision it used, so a dataset can
always be traced back to the exact schema that produced it, even after the file
on disk has moved on.

``providers`` is likewise declared in the schema; what the store records is
observed *behaviour* - health, latency, error counts - which belongs to a run
rather than to a configuration.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

__all__ = [
    "SCHEMA_VERSION",
    "Base",
    "Job",
    "Project",
    "Run",
    "RunEvent",
    "RunStatistic",
    "SchemaRevision",
    "utcnow",
]

#: Bumped when the store's shape changes in a way that needs a migration.
SCHEMA_VERSION = 1


def utcnow() -> datetime:
    """Timezone-aware UTC. A run may well outlive the session that started it."""
    return datetime.now(UTC)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


class Project(Base):
    """A generation workspace (design document section 6)."""

    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), index=True)
    #: Where the schema file lives, when it came from one.
    path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    revisions: Mapped[list[SchemaRevision]] = relationship(
        back_populates="project", cascade="all, delete-orphan", order_by="SchemaRevision.version"
    )
    runs: Mapped[list[Run]] = relationship(
        back_populates="project", cascade="all, delete-orphan", order_by="Run.started_at.desc()"
    )

    __table_args__ = (UniqueConstraint("path", name="projects_path_unique"),)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "path": self.path,
            "description": self.description,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }


class SchemaRevision(Base):
    """One version of a project's schema (design document section 73).

    The source text is stored verbatim, not re-serialised, so a revision is
    byte-for-byte what the user wrote. ``source_hash`` is what makes revisions
    cheap: an unchanged schema does not create a new row, so a hundred runs of
    the same project share one revision.
    """

    __tablename__ = "schema_revisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    version: Mapped[int] = mapped_column(Integer)
    source_text: Mapped[str] = mapped_column(Text)
    source_hash: Mapped[str] = mapped_column(String(64), index=True)
    source_format: Mapped[str] = mapped_column(String(16), default="yaml")
    #: A summary of the shape, so the API can describe a revision without
    #: recompiling it: entity names, counts, field counts.
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    project: Mapped[Project] = relationship(back_populates="revisions")
    runs: Mapped[list[Run]] = relationship(back_populates="revision")

    __table_args__ = (
        UniqueConstraint("project_id", "version", name="schema_revisions_version_unique"),
        Index("schema_revisions_project_hash", "project_id", "source_hash"),
    )

    def to_dict(self, *, include_source: bool = False) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "project_id": self.project_id,
            "version": self.version,
            "source_hash": self.source_hash,
            "source_format": self.source_format,
            "summary": self.summary or {},
            "created_at": _iso(self.created_at),
        }
        if include_source:
            data["source_text"] = self.source_text
        return data


class Run(Base):
    """A single execution of a project (design document sections 6 and 56).

    Stores configuration, progress, statistics, provenance, logs and output
    locations - which is section 6's list, verbatim.
    """

    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    revision_id: Mapped[int | None] = mapped_column(
        ForeignKey("schema_revisions.id"), nullable=True
    )

    state: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    seed: Mapped[int] = mapped_column(Integer, default=0)
    output_dir: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    output_format: Mapped[str] = mapped_column(String(32), default="jsonl")

    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    #: Section 69's pre-flight estimate, kept so the inspector can compare it
    #: with what actually happened.
    estimate: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    records_requested: Mapped[int] = mapped_column(Integer, default=0)
    records_written: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    project: Mapped[Project] = relationship(back_populates="runs")
    revision: Mapped[SchemaRevision | None] = relationship(back_populates="runs")
    jobs: Mapped[list[Job]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="Job.sequence"
    )
    events: Mapped[list[RunEvent]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="RunEvent.id"
    )
    statistics: Mapped[list[RunStatistic]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )

    @property
    def duration_seconds(self) -> float | None:
        # SQLite has no timezone type, so a stored timestamp comes back naive
        # however it went in. Both ends are normalised before subtracting.
        if self.started_at is None:
            return None
        end = _aware(self.finished_at) if self.finished_at is not None else utcnow()
        return (end - _aware(self.started_at)).total_seconds()

    @property
    def progress(self) -> float:
        if not self.records_requested:
            return 0.0
        return min(1.0, self.records_written / self.records_requested)

    def to_dict(self, *, include_jobs: bool = False) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "project_id": self.project_id,
            "revision_id": self.revision_id,
            "state": self.state,
            "seed": self.seed,
            "output_dir": self.output_dir,
            "output_format": self.output_format,
            "records_requested": self.records_requested,
            "records_written": self.records_written,
            "progress": round(self.progress, 6),
            "duration_seconds": self.duration_seconds,
            "config": self.config or {},
            "estimate": self.estimate or {},
            "summary": self.summary or {},
            "error": self.error,
            "created_at": _iso(self.created_at),
            "started_at": _iso(self.started_at),
            "finished_at": _iso(self.finished_at),
        }
        if include_jobs:
            data["jobs"] = [job.to_dict() for job in self.jobs]
        return data


class Job(Base):
    """One unit of a run (design document section 29).

    ``completed`` is the checkpoint of section 32: the number of records this
    job has finished and written. Because a record's seed is derived from its
    index rather than from RNG state, that single number is enough to resume -
    there is no stream position to restore.
    """

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    sequence: Mapped[int] = mapped_column(Integer, default=0)

    type: Mapped[str] = mapped_column(String(32), default="entity_batch")
    entity: Mapped[str | None] = mapped_column(String(200), nullable=True)
    state: Mapped[str] = mapped_column(String(16), default="queued", index=True)

    offset: Mapped[int] = mapped_column(Integer, default=0)
    requested: Mapped[int] = mapped_column(Integer, default=0)
    completed: Mapped[int] = mapped_column(Integer, default=0)
    #: How many times this job has been started, including resumes.
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    #: Which output part this job is writing, for formats that cannot append.
    part: Mapped[int] = mapped_column(Integer, default=0)

    depends_on: Mapped[list[Any]] = mapped_column(JSON, default=list)
    outputs: Mapped[list[Any]] = mapped_column(JSON, default=list)
    checkpoint: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    checkpointed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    run: Mapped[Run] = relationship(back_populates="jobs")

    __table_args__ = (Index("jobs_run_state", "run_id", "state"),)

    @property
    def remaining(self) -> int:
        return max(0, self.requested - self.completed)

    @property
    def progress(self) -> float:
        if not self.requested:
            return 1.0
        return min(1.0, self.completed / self.requested)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "run_id": self.run_id,
            "sequence": self.sequence,
            "type": self.type,
            "entity": self.entity,
            "state": self.state,
            "offset": self.offset,
            "requested": self.requested,
            "completed": self.completed,
            "remaining": self.remaining,
            "progress": round(self.progress, 6),
            "attempts": self.attempts,
            "part": self.part,
            "depends_on": list(self.depends_on or []),
            "outputs": list(self.outputs or []),
            "checkpoint": self.checkpoint or {},
            "error": self.error,
            "started_at": _iso(self.started_at),
            "finished_at": _iso(self.finished_at),
            "checkpointed_at": _iso(self.checkpointed_at),
        }


class RunEvent(Base):
    """A structured log line belonging to a run (design document section 86).

    Section 86's fields - timestamp, run_id, job_id, provider, entity,
    record_range, duration, status, error - are the columns and the ``data``
    blob between them.
    """

    __tablename__ = "run_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    job_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    level: Mapped[str] = mapped_column(String(16), default="info", index=True)
    event: Mapped[str] = mapped_column(String(64), index=True)
    entity: Mapped[str | None] = mapped_column(String(200), nullable=True)
    message: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    run: Mapped[Run] = relationship(back_populates="events")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "run_id": self.run_id,
            "job_id": self.job_id,
            "timestamp": _iso(self.timestamp),
            "level": self.level,
            "event": self.event,
            "entity": self.entity,
            "message": self.message,
            "data": self.data or {},
        }


class RunStatistic(Base):
    """A named measurement taken during a run (sections 56, 58 and 86)."""

    __tablename__ = "run_statistics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    scope: Mapped[str] = mapped_column(String(64), default="run")
    name: Mapped[str] = mapped_column(String(128))
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    run: Mapped[Run] = relationship(back_populates="statistics")

    __table_args__ = (UniqueConstraint("run_id", "scope", "name", name="run_statistics_unique"),)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "name": self.name,
            "value": self.value,
            "detail": self.detail or {},
        }


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _aware(value: datetime) -> datetime:
    """SQLite hands back naive datetimes; treat those as the UTC they were."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _iso(value: datetime | None) -> str | None:
    return _aware(value).isoformat() if value is not None else None


def summarise_schema(project: Any) -> dict[str, Any]:
    """A compact description of a schema, stored alongside its revision."""
    return {
        "name": project.project.name,
        "version": project.project.version,
        "entities": {
            name: {"count": entity.count, "fields": len(entity.fields)}
            for name, entity in project.entities.items()
        },
        "total_records": project.total_records(),
        "providers": sorted(project.providers),
    }


def json_dumps(value: Any) -> str:
    return json.dumps(value, default=str, sort_keys=True)

"""Queries the CLI and the API both need (design document sections 36, 42, 56).

Everything above this layer works in dictionaries and identifiers, never in
SQLAlchemy sessions. That keeps session lifetime in one place, which matters
because a run holds the store open for hours while an HTTP request holds it
open for milliseconds.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, func, select

from .database import Database
from .models import (
    Job,
    Project,
    Run,
    RunEvent,
    RunStatistic,
    SchemaRevision,
    summarise_schema,
    utcnow,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..schema.models import ProjectSpec

__all__ = ["Repository"]


class Repository:
    """High-level access to the metadata store."""

    def __init__(self, database: Database) -> None:
        self.db = database

    # -- projects ----------------------------------------------------------- #

    def upsert_project(
        self,
        project: ProjectSpec,
        *,
        path: str | Path | None = None,
        source_text: str | None = None,
        source_format: str = "yaml",
    ) -> tuple[int, int | None]:
        """Record a project and its schema, returning ``(project_id, revision_id)``.

        An unchanged schema does not create a new revision: the source hash is
        checked first, so running the same project a hundred times leaves one
        revision row and a hundred runs pointing at it.
        """
        resolved = str(Path(path).resolve()) if path else None

        with self.db.transaction() as session:
            # A project is identified by its file path when it has one. Two
            # projects may legitimately share a name; two schema files at the
            # same path are the same project by definition.
            if resolved is not None:
                record = session.scalar(select(Project).where(Project.path == resolved))
            else:
                record = session.scalar(
                    select(Project).where(
                        Project.name == project.project.name, Project.path.is_(None)
                    )
                )

            if record is None:
                record = Project(
                    name=project.project.name,
                    path=resolved,
                    description=project.project.description,
                )
                session.add(record)
                session.flush()
            else:
                record.name = project.project.name
                record.description = project.project.description
                record.updated_at = utcnow()

            revision_id: int | None = None
            if source_text is not None:
                revision_id = self._ensure_revision(
                    session, record, project, source_text, source_format
                )
            return record.id, revision_id

    def _ensure_revision(
        self,
        session: Any,
        record: Project,
        project: ProjectSpec,
        source_text: str,
        source_format: str,
    ) -> int:
        digest = hashlib.blake2b(source_text.encode("utf-8"), digest_size=32).hexdigest()
        existing = session.scalar(
            select(SchemaRevision).where(
                SchemaRevision.project_id == record.id, SchemaRevision.source_hash == digest
            )
        )
        if existing is not None:
            return existing.id

        highest = session.scalar(
            select(func.max(SchemaRevision.version)).where(SchemaRevision.project_id == record.id)
        )
        revision = SchemaRevision(
            project_id=record.id,
            version=(highest or 0) + 1,
            source_text=source_text,
            source_hash=digest,
            source_format=source_format,
            summary=summarise_schema(project),
        )
        session.add(revision)
        session.flush()
        return revision.id

    def list_projects(self) -> list[dict[str, Any]]:
        with self.db.transaction() as session:
            rows = session.scalars(select(Project).order_by(Project.updated_at.desc())).all()
            return [row.to_dict() for row in rows]

    def get_project(self, project_id: int) -> dict[str, Any] | None:
        with self.db.transaction() as session:
            record = session.get(Project, project_id)
            if record is None:
                return None
            data = record.to_dict()
            data["revisions"] = [revision.to_dict() for revision in record.revisions]
            data["run_count"] = session.scalar(
                select(func.count(Run.id)).where(Run.project_id == project_id)
            )
            return data

    def get_revision(
        self, revision_id: int, *, include_source: bool = True
    ) -> dict[str, Any] | None:
        with self.db.transaction() as session:
            revision = session.get(SchemaRevision, revision_id)
            return revision.to_dict(include_source=include_source) if revision else None

    # -- runs --------------------------------------------------------------- #

    def create_run(
        self,
        *,
        run_id: str,
        project_id: int,
        revision_id: int | None,
        seed: int,
        output_dir: str | None,
        output_format: str,
        config: dict[str, Any],
        estimate: dict[str, Any],
        records_requested: int,
    ) -> dict[str, Any]:
        with self.db.transaction() as session:
            run = Run(
                id=run_id,
                project_id=project_id,
                revision_id=revision_id,
                seed=seed,
                output_dir=output_dir,
                output_format=output_format,
                config=config,
                estimate=estimate,
                records_requested=records_requested,
                state="queued",
            )
            session.add(run)
            session.flush()
            return run.to_dict()

    def update_run(self, run_id: str, **fields: Any) -> None:
        with self.db.transaction() as session:
            run = session.get(Run, run_id)
            if run is None:
                return
            for key, value in fields.items():
                setattr(run, key, value)

    def get_run(self, run_id: str, *, include_jobs: bool = True) -> dict[str, Any] | None:
        with self.db.transaction() as session:
            run = session.get(Run, run_id)
            if run is None:
                return None
            data = run.to_dict(include_jobs=include_jobs)
            data["statistics"] = [stat.to_dict() for stat in run.statistics]
            return data

    def list_runs(
        self,
        *,
        project_id: int | None = None,
        state: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        with self.db.transaction() as session:
            query = select(Run).order_by(Run.created_at.desc()).limit(limit)
            if project_id is not None:
                query = query.where(Run.project_id == project_id)
            if state is not None:
                query = query.where(Run.state == state)
            return [run.to_dict() for run in session.scalars(query).all()]

    def resumable_runs(self, *, project_id: int | None = None) -> list[dict[str, Any]]:
        """Runs that stopped with work left to do (design document section 32)."""
        with self.db.transaction() as session:
            query = select(Run).where(Run.state.in_(("paused", "failed", "cancelled", "running")))
            if project_id is not None:
                query = query.where(Run.project_id == project_id)
            return [
                run.to_dict()
                for run in session.scalars(query.order_by(Run.created_at.desc())).all()
            ]

    def delete_run(self, run_id: str) -> bool:
        with self.db.transaction() as session:
            run = session.get(Run, run_id)
            if run is None:
                return False
            session.delete(run)
            return True

    # -- jobs --------------------------------------------------------------- #

    def create_jobs(self, run_id: str, jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        with self.db.transaction() as session:
            records = [Job(run_id=run_id, **payload) for payload in jobs]
            session.add_all(records)
            session.flush()
            return [record.to_dict() for record in records]

    def update_job(self, job_id: int, **fields: Any) -> None:
        with self.db.transaction() as session:
            job = session.get(Job, job_id)
            if job is None:
                return
            for key, value in fields.items():
                setattr(job, key, value)

    def checkpoint_job(
        self, job_id: int, *, completed: int, checkpoint: dict[str, Any] | None = None
    ) -> None:
        """Record progress (design document section 32).

        Deliberately the smallest possible write: one row, three columns. A run
        checkpoints thousands of times, and a checkpoint that costs a page of
        JSON is a checkpoint people turn off.
        """
        with self.db.transaction() as session:
            job = session.get(Job, job_id)
            if job is None:
                return
            job.completed = completed
            job.checkpointed_at = utcnow()
            if checkpoint is not None:
                job.checkpoint = checkpoint

    def get_jobs(self, run_id: str) -> list[dict[str, Any]]:
        with self.db.transaction() as session:
            rows = session.scalars(
                select(Job).where(Job.run_id == run_id).order_by(Job.sequence)
            ).all()
            return [row.to_dict() for row in rows]

    # -- events and statistics ---------------------------------------------- #

    def add_event(
        self,
        run_id: str,
        *,
        event: str,
        message: str = "",
        level: str = "info",
        job_id: int | None = None,
        entity: str | None = None,
        data: dict[str, Any] | None = None,
        timestamp: datetime | None = None,
    ) -> None:
        with self.db.transaction() as session:
            session.add(
                RunEvent(
                    run_id=run_id,
                    job_id=job_id,
                    level=level,
                    event=event,
                    entity=entity,
                    message=message,
                    data=data or {},
                    timestamp=timestamp or utcnow(),
                )
            )

    def add_events(self, records: list[dict[str, Any]]) -> None:
        """Bulk insert, used to flush a buffered batch of log lines."""
        if not records:
            return
        with self.db.transaction() as session:
            session.add_all([RunEvent(**payload) for payload in records])

    def get_events(
        self, run_id: str, *, level: str | None = None, limit: int = 200, after_id: int = 0
    ) -> list[dict[str, Any]]:
        with self.db.transaction() as session:
            query = (
                select(RunEvent)
                .where(RunEvent.run_id == run_id, RunEvent.id > after_id)
                .order_by(RunEvent.id)
                .limit(limit)
            )
            if level is not None:
                query = query.where(RunEvent.level == level)
            return [row.to_dict() for row in session.scalars(query).all()]

    def record_statistic(
        self,
        run_id: str,
        name: str,
        value: float | None = None,
        *,
        scope: str = "run",
        detail: dict[str, Any] | None = None,
    ) -> None:
        with self.db.transaction() as session:
            existing = session.scalar(
                select(RunStatistic).where(
                    RunStatistic.run_id == run_id,
                    RunStatistic.scope == scope,
                    RunStatistic.name == name,
                )
            )
            if existing is None:
                session.add(
                    RunStatistic(
                        run_id=run_id,
                        scope=scope,
                        name=name,
                        value=value,
                        detail=detail or {},
                    )
                )
            else:
                existing.value = value
                existing.detail = detail or {}
                existing.recorded_at = utcnow()

    def record_statistics(self, run_id: str, values: dict[str, Any], *, scope: str = "run") -> None:
        """Store a flat mapping, splitting numbers from everything else."""
        for name, value in values.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                self.record_statistic(run_id, name, float(value), scope=scope)
            else:
                self.record_statistic(run_id, name, None, scope=scope, detail={"value": value})

    # -- maintenance -------------------------------------------------------- #

    def prune_runs(self, *, keep: int = 50, project_id: int | None = None) -> int:
        """Drop the oldest runs beyond ``keep``. Events dominate store size."""
        with self.db.transaction() as session:
            query = select(Run.id).order_by(Run.created_at.desc()).offset(keep)
            if project_id is not None:
                query = query.where(Run.project_id == project_id)
            doomed = list(session.scalars(query).all())
            if not doomed:
                return 0
            session.execute(delete(Run).where(Run.id.in_(doomed)))
            return len(doomed)

    def stats(self) -> dict[str, Any]:
        with self.db.transaction() as session:
            return {
                "projects": session.scalar(select(func.count(Project.id))) or 0,
                "revisions": session.scalar(select(func.count(SchemaRevision.id))) or 0,
                "runs": session.scalar(select(func.count(Run.id))) or 0,
                "jobs": session.scalar(select(func.count(Job.id))) or 0,
                "events": session.scalar(select(func.count(RunEvent.id))) or 0,
                **self.db.describe(),
            }

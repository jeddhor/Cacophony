"""The job system and the Conductor (design document sections 29-32, 64, 65).

The tests that matter most here are the resume ones. A run that resumes into
duplicated or skipped records is worse than a run that cannot resume at all,
because the damage is silent - so those cases are checked against the bytes on
disk rather than against the checkpoint that claims to describe them.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from cacophony.core.errors import GenerationError
from cacophony.observability.metrics import RunMetrics, Throughput
from cacophony.outputs import align_to_records
from cacophony.runs.config import ResourceLimits, RunConfig
from cacophony.runs.coordinator import Conductor
from cacophony.runs.events import EventBus, EventKind, RunEvent
from cacophony.runs.state import JobState, JobType, RunState
from cacophony.store import Database, Repository
from helpers import compile_from, make_project

SCHEMA = {
    "employee": {
        "count": 500,
        "primary_key": "employee_id",
        "fields": {
            "employee_id": {"type": "string", "generator": "sequence", "format": "EMP-{000000}"},
            "name": {"type": "string", "generator": "faker", "provider": "name"},
            "department": {"generator": "weighted", "choices": ["Eng", "Sales"]},
        },
    },
    "device": {
        "count": 300,
        "fields": {
            "asset": {"type": "string", "generator": "sequence", "format": "AST-{00000}"},
            "os": {"generator": "weighted", "choices": ["Windows", "macOS"]},
        },
    },
}


def build(tmp_path: Path, **overrides: Any) -> tuple[Conductor, Repository]:
    compiled = compile_from(SCHEMA, name="Runs Test", seed=99)
    repo = Repository(Database(tmp_path / "store.db"))
    project_id, revision_id = repo.upsert_project(
        make_project(SCHEMA, name="Runs Test", seed=99),
        path=str(tmp_path / "p.yaml"),
        source_text="project:\n  name: Runs Test\n",
    )
    config = RunConfig(
        output_dir=overrides.pop("output_dir", tmp_path / "out"),
        output_format=overrides.pop("output_format", "jsonl"),
        limits=ResourceLimits(batch_size=overrides.pop("batch_size", 50), max_workers=2),
        checkpoint_every=overrides.pop("checkpoint_every", 100),
        **overrides,
    )
    conductor = Conductor(
        compiled, config, repository=repo, project_id=project_id, revision_id=revision_id
    )
    return conductor, repo


def lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# --------------------------------------------------------------------------- #
# States (section 29)
# --------------------------------------------------------------------------- #


class TestStates:
    def test_section_29_job_states_all_exist(self) -> None:
        assert {state.value for state in JobState} == {
            "queued",
            "running",
            "paused",
            "retrying",
            "completed",
            "failed",
            "cancelled",
        }

    def test_section_29_job_types_all_exist(self) -> None:
        assert {job.value for job in JobType} >= {
            "entity_batch",
            "llm_batch",
            "image",
            "audio",
            "export",
            "validation",
        }

    def test_terminal_states(self) -> None:
        assert JobState.COMPLETED.is_terminal and not JobState.RUNNING.is_terminal
        assert RunState.FAILED.is_terminal and not RunState.PAUSED.is_terminal

    def test_a_failed_job_may_be_resumed(self) -> None:
        """Which is the entire point of checkpointing it."""
        assert JobState.FAILED.can_move_to(JobState.QUEUED)
        assert JobState.FAILED.is_resumable

    def test_a_completed_job_goes_nowhere(self) -> None:
        for target in JobState:
            assert not JobState.COMPLETED.can_move_to(target)

    def test_a_run_left_running_is_resumable(self) -> None:
        """A killed process never gets to write a different state."""
        assert RunState.RUNNING.is_resumable
        assert not RunState.COMPLETED.is_resumable


# --------------------------------------------------------------------------- #
# Planning and execution
# --------------------------------------------------------------------------- #


class TestPlanning:
    def test_one_job_per_entity(self, tmp_path) -> None:
        conductor, _ = build(tmp_path)
        jobs = conductor.plan()
        assert [job.entity for job in jobs] == ["employee", "device"]
        assert [job.requested for job in jobs] == [500, 300]

    def test_record_override_applies_to_every_entity(self, tmp_path) -> None:
        conductor, _ = build(tmp_path, records=40)
        assert [job.requested for job in conductor.plan()] == [40, 40]

    def test_entity_selection(self, tmp_path) -> None:
        conductor, _ = build(tmp_path, entities=["device"])
        assert [job.entity for job in conductor.plan()] == ["device"]

    def test_unknown_entity_is_reported(self, tmp_path) -> None:
        conductor, _ = build(tmp_path, entities=["ghost"])
        with pytest.raises(GenerationError, match="ghost"):
            conductor.plan()

    def test_zero_count_entities_are_skipped(self, tmp_path) -> None:
        conductor, _ = build(tmp_path, records=0)
        assert conductor.plan() == []


class TestExecution:
    def test_a_run_writes_every_entity(self, tmp_path) -> None:
        conductor, _ = build(tmp_path, records=120)
        outcome = asyncio.run(conductor.execute())
        assert outcome.ok
        assert outcome.records == 240
        assert len(lines(tmp_path / "out" / "employee.jsonl")) == 120
        assert len(lines(tmp_path / "out" / "device.jsonl")) == 120

    def test_the_run_is_recorded(self, tmp_path) -> None:
        conductor, repo = build(tmp_path, records=60)
        outcome = asyncio.run(conductor.execute())
        stored = repo.get_run(outcome.run_id)
        assert stored["state"] == "completed"
        assert stored["records_written"] == 120
        assert stored["revision_id"] is not None
        assert [job["state"] for job in stored["jobs"]] == ["completed", "completed"]

    def test_events_are_stored(self, tmp_path) -> None:
        conductor, repo = build(tmp_path, records=60)
        outcome = asyncio.run(conductor.execute())
        kinds = [event["event"] for event in repo.get_events(outcome.run_id)]
        assert "run.started" in kinds
        assert kinds.count("job.completed") == 2
        assert "run.completed" in kinds

    def test_progress_events_are_not_stored(self, tmp_path) -> None:
        """They fire constantly; the live feed carries them, the store does not."""
        conductor, repo = build(tmp_path, records=200, batch_size=10)
        outcome = asyncio.run(conductor.execute())
        kinds = [event["event"] for event in repo.get_events(outcome.run_id, limit=1000)]
        assert "job.progress" not in kinds

    def test_quality_statistics_are_recorded(self, tmp_path) -> None:
        conductor, repo = build(tmp_path, records=50)
        outcome = asyncio.run(conductor.execute())
        stats = {stat["name"]: stat["value"] for stat in repo.get_run(outcome.run_id)["statistics"]}
        assert stats["constraint_validity"] == 1.0
        assert stats["records_written"] == 100.0

    def test_history_can_be_switched_off(self, tmp_path) -> None:
        conductor, repo = build(tmp_path, records=20, record_history=False)
        outcome = asyncio.run(conductor.execute())
        assert outcome.ok
        assert repo.list_runs() == []

    def test_independent_entities_overlap(self, tmp_path) -> None:
        """Section 30: entities that do not depend on each other run together."""
        conductor, _ = build(tmp_path, records=100, batch_size=10)
        conductor.plan()
        started: list[str] = []
        conductor.bus.add_sink(
            lambda event: (
                started.append(event.entity) if event.kind is EventKind.JOB_STARTED else None
            )
        )
        asyncio.run(conductor.execute())
        assert set(started) == {"employee", "device"}

    def test_a_dependency_excluded_by_selection_is_reported(self, tmp_path) -> None:
        schema = {
            "company": {"count": 2, "fields": {"id": {"generator": "sequence"}}},
            "worker": {
                "count": 4,
                "fields": {
                    "employer": {
                        "type": "string",
                        "generator": "reference",
                        "entity": "company",
                        "on_unavailable": "placeholder",
                    }
                },
            },
        }
        compiled = compile_from(schema, name="deps", seed=1)
        config = RunConfig(output_dir=tmp_path / "out", entities=["worker"], record_history=False)
        conductor = Conductor(compiled, config)
        conductor.plan()
        outcome = asyncio.run(conductor.execute())
        assert outcome.state is RunState.FAILED
        assert "company" in (outcome.error or "")


# --------------------------------------------------------------------------- #
# Section 32: checkpointing and resume
# --------------------------------------------------------------------------- #


class TestCheckpointAndResume:
    def _interrupt_at(self, conductor: Conductor, threshold: int) -> Any:
        async def drive() -> Any:
            task = asyncio.create_task(conductor.execute())
            while conductor.metrics.total_written < threshold and not task.done():
                await asyncio.sleep(0.001)
            conductor.handle.cancel()
            return await task

        return asyncio.run(drive())

    def test_cancelling_leaves_a_checkpoint(self, tmp_path) -> None:
        conductor, repo = build(
            tmp_path, records=400, entities=["employee"], batch_size=20, checkpoint_every=40
        )
        conductor.plan()
        outcome = self._interrupt_at(conductor, 100)

        assert outcome.state is RunState.CANCELLED
        job = repo.get_jobs(outcome.run_id)[0]
        assert 0 < job["completed"] < 400
        assert job["checkpoint"]["entity"] == "employee"

    def test_resume_completes_the_run(self, tmp_path) -> None:
        conductor, repo = build(
            tmp_path, records=400, entities=["employee"], batch_size=20, checkpoint_every=40
        )
        conductor.plan()
        first = self._interrupt_at(conductor, 100)

        stored = repo.get_run(first.run_id)
        resumed = Conductor.resume(
            compile_from(SCHEMA, name="Runs Test", seed=99), stored, repository=repo
        )
        second = asyncio.run(resumed.execute_resume())

        assert second.state is RunState.COMPLETED
        assert len(lines(tmp_path / "out" / "employee.jsonl")) == 400

    def test_resumed_output_has_no_duplicates_or_gaps(self, tmp_path) -> None:
        """The failure mode that matters: silent damage to the dataset."""
        conductor, repo = build(
            tmp_path, records=400, entities=["employee"], batch_size=20, checkpoint_every=200
        )
        conductor.plan()
        first = self._interrupt_at(conductor, 120)

        stored = repo.get_run(first.run_id)
        resumed = Conductor.resume(
            compile_from(SCHEMA, name="Runs Test", seed=99), stored, repository=repo
        )
        asyncio.run(resumed.execute_resume())

        ids = [row["employee_id"] for row in lines(tmp_path / "out" / "employee.jsonl")]
        assert len(ids) == 400
        assert len(set(ids)) == 400
        assert ids == [f"EMP-{index:06d}" for index in range(1, 401)]

    def test_a_resumed_run_matches_an_uninterrupted_one(self, tmp_path) -> None:
        conductor, repo = build(
            tmp_path, records=300, entities=["employee"], batch_size=20, checkpoint_every=100
        )
        conductor.plan()
        first = self._interrupt_at(conductor, 80)
        resumed = Conductor.resume(
            compile_from(SCHEMA, name="Runs Test", seed=99),
            repo.get_run(first.run_id),
            repository=repo,
        )
        asyncio.run(resumed.execute_resume())
        interrupted = (tmp_path / "out" / "employee.jsonl").read_bytes()

        reference_dir = tmp_path / "reference"
        straight = Conductor(
            compile_from(SCHEMA, name="Runs Test", seed=99),
            RunConfig(
                output_dir=reference_dir,
                entities=["employee"],
                records=300,
                record_history=False,
                limits=ResourceLimits(batch_size=20),
            ),
        )
        asyncio.run(straight.execute())
        assert interrupted == (reference_dir / "employee.jsonl").read_bytes()

    def test_a_stale_checkpoint_is_corrected_from_the_file(self, tmp_path) -> None:
        """An unclean stop can leave the store behind the file. The file wins."""
        conductor, repo = build(
            tmp_path, records=200, entities=["employee"], batch_size=20, checkpoint_every=1000
        )
        conductor.plan()
        first = self._interrupt_at(conductor, 60)

        stored = repo.get_run(first.run_id)
        job = stored["jobs"][0]
        on_disk = len(lines(tmp_path / "out" / "employee.jsonl"))
        # Pretend the process died before the last checkpoint landed.
        repo.checkpoint_job(job["id"], completed=max(0, on_disk - 25))

        resumed = Conductor.resume(
            compile_from(SCHEMA, name="Runs Test", seed=99),
            repo.get_run(first.run_id),
            repository=repo,
        )
        asyncio.run(resumed.execute_resume())

        ids = [row["employee_id"] for row in lines(tmp_path / "out" / "employee.jsonl")]
        assert len(ids) == len(set(ids)) == 200

    #: Half the records fail validation, so rows written and source records
    #: consumed are different numbers from the first batch onwards.
    FILTERED = {
        "employee": {
            "count": 500,
            "primary_key": "employee_id",
            "fields": {
                "employee_id": {
                    "type": "string",
                    "generator": "sequence",
                    "format": "EMP-{000000}",
                },
                "parity": {
                    "type": "integer",
                    "generator": "expression",
                    "expression": "index % 2",
                    "constraints": {"max": 0},
                },
                "index": {"type": "integer", "generator": "sequence", "start": 0},
            },
        }
    }

    def _filtered(self, tmp_path: Path, **overrides: Any) -> tuple[Conductor, Repository]:
        compiled = compile_from(self.FILTERED, name="Filtered", seed=7)
        repo = Repository(Database(tmp_path / "store.db"))
        project_id, revision_id = repo.upsert_project(
            make_project(self.FILTERED, name="Filtered", seed=7),
            path=str(tmp_path / "p.yaml"),
            source_text="project:\n  name: Filtered\n",
        )
        config = RunConfig(
            output_dir=tmp_path / "out",
            records=overrides.pop("records", 100),
            drop_invalid=True,
            failure_policy="skip",
            limits=ResourceLimits(batch_size=overrides.pop("batch_size", 10), max_workers=1),
            checkpoint_every=overrides.pop("checkpoint_every", 10),
            record_history=True,
        )
        return (
            Conductor(
                compiled, config, repository=repo, project_id=project_id, revision_id=revision_id
            ),
            repo,
        )

    def test_a_resumed_run_that_drops_records_does_not_skip_or_repeat(self, tmp_path) -> None:
        """Rows written are not a position (design document section 32).

        Half of these records fail validation and are dropped, so resuming at
        `offset + rows written` restarts in the wrong place: the run used to
        come back with more rows than it ever generated positions for, and with
        the wrong ones.
        """
        conductor, repo = self._filtered(tmp_path, records=100)
        conductor.plan()
        first = self._interrupt_at(conductor, 20)
        assert first.state is RunState.CANCELLED

        resumed = Conductor.resume(
            compile_from(self.FILTERED, name="Filtered", seed=7),
            repo.get_run(first.run_id),
            repository=repo,
        )
        asyncio.run(resumed.execute_resume())

        rows = lines(tmp_path / "out" / "employee.jsonl")
        indices = [row["index"] for row in rows]
        # A hundred source records, the even half of which survive validation.
        assert indices == list(range(0, 100, 2))
        assert len(indices) == len(set(indices)) == 50

    def test_the_same_run_uninterrupted_produces_exactly_that(self, tmp_path) -> None:
        """The comparison that makes the previous test mean something."""
        conductor, _repo = self._filtered(tmp_path, records=100)
        conductor.plan()
        asyncio.run(conductor.execute())

        indices = [row["index"] for row in lines(tmp_path / "out" / "employee.jsonl")]
        assert indices == list(range(0, 100, 2))

    #: One badge for every two records, so the second half of a resumed run
    #: sees values the first half already used.
    COLLIDING = {
        "person": {
            "count": 400,
            "fields": {
                "badge": {
                    "type": "integer",
                    "generator": "expression",
                    "expression": "int(index) % 200",
                    "unique": True,
                },
                "index": {"type": "integer", "generator": "sequence", "start": 0},
            },
        }
    }

    def _colliding(self, tmp_path: Path, fmt: str = "jsonl") -> tuple[Conductor, Repository]:
        compiled = compile_from(self.COLLIDING, name="Colliding", seed=6)
        repo = Repository(Database(tmp_path / "store.db"))
        project_id, revision_id = repo.upsert_project(
            make_project(self.COLLIDING, name="Colliding", seed=6),
            path=str(tmp_path / "p.yaml"),
            source_text="project:\n  name: Colliding\n",
        )
        config = RunConfig(
            output_dir=tmp_path / "out",
            output_format=fmt,
            records=400,
            drop_invalid=True,
            failure_policy="skip",
            limits=ResourceLimits(batch_size=20, max_workers=1),
            checkpoint_every=20,
            record_history=True,
        )
        return (
            Conductor(
                compiled, config, repository=repo, project_id=project_id, revision_id=revision_id
            ),
            repo,
        )

    def test_uniqueness_survives_a_resume(self, tmp_path) -> None:
        """The tracker lives in memory, and memory does not survive the stop."""
        conductor, repo = self._colliding(tmp_path)
        conductor.plan()
        first = self._interrupt_at(conductor, 60)
        assert first.state is RunState.CANCELLED

        resumed = Conductor.resume(
            compile_from(self.COLLIDING, name="Colliding", seed=6),
            repo.get_run(first.run_id),
            repository=repo,
        )
        asyncio.run(resumed.execute_resume())

        badges = [row["badge"] for row in lines(tmp_path / "out" / "person.jsonl")]
        assert len(badges) == len(set(badges)) == 200

    def test_a_format_that_cannot_be_read_back_says_so(self, tmp_path) -> None:
        """Parquet cannot be re-read cheaply; the run reports the gap."""
        pytest.importorskip("pyarrow")
        conductor, repo = self._colliding(tmp_path, fmt="parquet")
        conductor.plan()
        first = self._interrupt_at(conductor, 60)

        resumed = Conductor.resume(
            compile_from(self.COLLIDING, name="Colliding", seed=6),
            repo.get_run(first.run_id),
            repository=repo,
        )
        outcome = asyncio.run(resumed.execute_resume())

        assert outcome.summary.get("uniqueness_unverified") == ["person"]

    def test_a_partitioned_resume_opens_a_new_part(self, tmp_path) -> None:
        """A partitioned writer is never appendable, whatever the format is."""
        conductor, repo = build(
            tmp_path,
            records=200,
            entities=["employee"],
            batch_size=20,
            partition_by=["department"],
        )
        conductor.plan()
        first = self._interrupt_at(conductor, 60)

        resumed = Conductor.resume(
            compile_from(SCHEMA, name="Runs Test", seed=99),
            repo.get_run(first.run_id),
            repository=repo,
        )
        asyncio.run(resumed.execute_resume())

        job = repo.get_jobs(first.run_id)[0]
        assert job["part"] >= 1
        parts = sorted((tmp_path / "out" / "employee").rglob("*.part[0-9]*.jsonl"))
        assert parts, "a resumed partitioned run must write a new part"

    def test_a_fresh_run_refuses_a_destination_that_is_already_full(self, tmp_path) -> None:
        """Two datasets in one directory look exactly like one dataset."""
        first, _repo = build(tmp_path, records=40, entities=["employee"])
        first.plan()
        asyncio.run(first.execute())

        second, _repo2 = build(tmp_path, records=10, entities=["employee"])
        with pytest.raises(GenerationError, match="already holds output"):
            second.plan()

    def test_overwrite_replaces_what_it_would_have_written(self, tmp_path) -> None:
        first, _repo = build(tmp_path, records=40, entities=["employee"])
        first.plan()
        asyncio.run(first.execute())
        # A part file from an earlier resume, which no writer would truncate.
        stale = tmp_path / "out" / "employee.part0001.jsonl"
        stale.write_text('{"employee_id": "EMP-999999"}\n', encoding="utf-8")

        second, _repo2 = build(tmp_path, records=10, entities=["employee"], overwrite=True)
        second.plan()
        outcome = asyncio.run(second.execute())

        assert not stale.exists()
        assert len(lines(tmp_path / "out" / "employee.jsonl")) == 10
        assert (
            outcome.summary["bytes_written"] == (tmp_path / "out" / "employee.jsonl").stat().st_size
        )

    def test_a_resumed_summary_describes_the_whole_dataset(self, tmp_path) -> None:
        """Both parts, and both parts' bytes - not just what this attempt added."""
        pytest.importorskip("pyarrow")
        conductor, repo = build(
            tmp_path, records=300, entities=["employee"], batch_size=20, output_format="parquet"
        )
        conductor.plan()
        first = self._interrupt_at(conductor, 80)

        resumed = Conductor.resume(
            compile_from(SCHEMA, name="Runs Test", seed=99),
            repo.get_run(first.run_id),
            repository=repo,
        )
        outcome = asyncio.run(resumed.execute_resume())

        on_disk = sorted(path for path in (tmp_path / "out").rglob("*.parquet") if path.is_file())
        assert len(on_disk) == 2, "the resume should have written a second part"
        assert sorted(Path(name) for name in outcome.summary["files"]) == on_disk
        assert outcome.summary["bytes_written"] == sum(path.stat().st_size for path in on_disk)

    def test_resume_from_another_directory_continues_the_same_dataset(
        self, tmp_path, monkeypatch
    ) -> None:
        """`out/` means "here", and a resume is often started somewhere else."""
        here = tmp_path / "here"
        there = tmp_path / "there"
        here.mkdir()
        there.mkdir()
        monkeypatch.chdir(here)

        conductor, repo = build(
            tmp_path, records=100, entities=["employee"], batch_size=10, output_dir=Path("out")
        )
        conductor.plan()
        first = self._interrupt_at(conductor, 30)
        assert first.state is RunState.CANCELLED

        monkeypatch.chdir(there)
        resumed = Conductor.resume(
            compile_from(SCHEMA, name="Runs Test", seed=99),
            repo.get_run(first.run_id),
            repository=repo,
        )
        asyncio.run(resumed.execute_resume())

        assert not (there / "out").exists(), "the resume started a second dataset"
        ids = [row["employee_id"] for row in lines(here / "out" / "employee.jsonl")]
        assert len(ids) == len(set(ids)) == 100

    def test_resume_reuses_the_original_configuration(self, tmp_path) -> None:
        """Section 73: a resumed dataset must not be generated two ways."""
        conductor, repo = build(
            tmp_path, records=200, entities=["employee"], batch_size=20, seed=4242
        )
        conductor.plan()
        first = self._interrupt_at(conductor, 60)
        resumed = Conductor.resume(
            compile_from(SCHEMA, name="Runs Test", seed=99),
            repo.get_run(first.run_id),
            repository=repo,
        )
        assert resumed.config.seed == 4242
        assert resumed.config.output_format == "jsonl"
        assert resumed.compiled.seed == 4242

    def test_resuming_a_finished_job_does_nothing(self, tmp_path) -> None:
        conductor, repo = build(tmp_path, records=40, entities=["employee"])
        outcome = asyncio.run(conductor.execute())
        resumed = Conductor.resume(
            compile_from(SCHEMA, name="Runs Test", seed=99),
            repo.get_run(outcome.run_id),
            repository=repo,
        )
        asyncio.run(resumed.execute_resume())
        assert len(lines(tmp_path / "out" / "employee.jsonl")) == 40

    def test_a_footered_format_resumes_into_a_new_part(self, tmp_path) -> None:
        """Parquet and JSON arrays cannot be appended to; parts keep them whole."""
        conductor, repo = build(
            tmp_path,
            records=300,
            entities=["employee"],
            batch_size=20,
            output_format="json",
            checkpoint_every=40,
        )
        conductor.plan()
        first = self._interrupt_at(conductor, 80)

        resumed = Conductor.resume(
            compile_from(SCHEMA, name="Runs Test", seed=99),
            repo.get_run(first.run_id),
            repository=repo,
        )
        asyncio.run(resumed.execute_resume())

        parts = sorted((tmp_path / "out").glob("employee*.json"))
        assert len(parts) == 2
        total = sum(len(json.loads(part.read_text(encoding="utf-8"))) for part in parts)
        assert total == 300


class TestAlignToRecords:
    def test_trims_a_file_to_a_record_count(self, tmp_path) -> None:
        path = tmp_path / "a.jsonl"
        path.write_text("".join(f'{{"n": {i}}}\n' for i in range(10)), encoding="utf-8")
        assert align_to_records(path, 4, "jsonl") == 4
        assert len(path.read_text().splitlines()) == 4

    def test_reports_a_short_file_honestly(self, tmp_path) -> None:
        path = tmp_path / "a.jsonl"
        path.write_text('{"n": 0}\n{"n": 1}\n', encoding="utf-8")
        assert align_to_records(path, 10, "jsonl") == 2

    def test_a_partial_final_line_is_dropped(self, tmp_path) -> None:
        path = tmp_path / "a.jsonl"
        path.write_text('{"n": 0}\n{"n": 1}\n{"n": 2', encoding="utf-8")
        assert align_to_records(path, 10, "jsonl") == 2
        assert path.read_text().endswith('{"n": 1}\n')

    def test_csv_keeps_its_header(self, tmp_path) -> None:
        path = tmp_path / "a.csv"
        path.write_text("a,b\n1,2\n3,4\n5,6\n", encoding="utf-8")
        assert align_to_records(path, 2, "csv") == 2
        assert path.read_text() == "a,b\n1,2\n3,4\n"

    def test_a_footered_format_is_left_alone(self, tmp_path) -> None:
        path = tmp_path / "a.parquet"
        path.write_bytes(b"not really parquet")
        assert align_to_records(path, 7, "parquet") == 7
        assert path.read_bytes() == b"not really parquet"

    def test_a_missing_file_is_zero(self, tmp_path) -> None:
        assert align_to_records(tmp_path / "nope.jsonl", 5, "jsonl") == 0


# --------------------------------------------------------------------------- #
# Pause and cancel
# --------------------------------------------------------------------------- #


class TestControl:
    def test_pause_holds_and_resume_releases(self, tmp_path) -> None:
        conductor, _ = build(tmp_path, records=600, entities=["employee"], batch_size=20)
        conductor.plan()

        async def drive() -> Any:
            task = asyncio.create_task(conductor.execute())
            while conductor.metrics.total_written < 100 and not task.done():
                await asyncio.sleep(0.001)
            conductor.handle.pause()
            paused_at = conductor.metrics.total_written
            await asyncio.sleep(0.05)
            # Nothing moves while paused.
            assert conductor.metrics.total_written == paused_at
            conductor.handle.resume()
            return await task

        outcome = asyncio.run(drive())
        assert outcome.ok and outcome.records == 600

    def test_cancel_stops_the_run(self, tmp_path) -> None:
        conductor, _ = build(tmp_path, records=1000, entities=["employee"], batch_size=20)
        conductor.plan()

        async def drive() -> Any:
            task = asyncio.create_task(conductor.execute())
            while conductor.metrics.total_written < 60 and not task.done():
                await asyncio.sleep(0.001)
            conductor.handle.cancel()
            return await task

        outcome = asyncio.run(drive())
        assert outcome.state is RunState.CANCELLED
        assert outcome.records < 1000

    def test_cancelling_before_the_start_produces_nothing(self, tmp_path) -> None:
        conductor, _ = build(tmp_path, records=100, entities=["employee"])
        conductor.plan()
        conductor.handle.cancel()
        outcome = asyncio.run(conductor.execute())
        assert outcome.state is RunState.CANCELLED
        assert outcome.records == 0


# --------------------------------------------------------------------------- #
# Section 64: resource controls
# --------------------------------------------------------------------------- #


class TestResourceLimits:
    def test_a_full_disk_is_reported_before_the_run(self, tmp_path) -> None:
        conductor, _ = build(tmp_path, records=10)
        conductor.config.limits.min_free_disk_mb = 10**9  # more than any disk has
        conductor.plan()
        warnings = conductor.preflight()
        assert any("free" in warning for warning in warnings)

    def test_a_healthy_disk_produces_no_complaint(self, tmp_path) -> None:
        conductor, _ = build(tmp_path, records=10)
        conductor.config.limits.min_free_disk_mb = 0
        conductor.plan()
        assert conductor.preflight() == []

    def test_limits_round_trip_through_a_stored_config(self) -> None:
        config = RunConfig(limits=ResourceLimits(max_workers=9, batch_size=13))
        restored = RunConfig.from_dict(config.to_dict())
        assert restored.limits.max_workers == 9
        assert restored.limits.batch_size == 13

    def test_an_unknown_format_is_refused_up_front(self, tmp_path) -> None:
        conductor, _ = build(tmp_path, records=10, output_format="papyrus")
        conductor.plan()
        with pytest.raises(GenerationError, match="Unknown output format"):
            conductor.preflight()


# --------------------------------------------------------------------------- #
# Events and metrics (sections 55, 86)
# --------------------------------------------------------------------------- #


class TestEventBus:
    def test_sinks_receive_events(self) -> None:
        bus = EventBus()
        seen: list[RunEvent] = []
        bus.add_sink(seen.append)
        bus.emit(EventKind.RUN_STARTED, "r", message="go")
        assert seen[0].kind is EventKind.RUN_STARTED

    def test_a_sink_can_be_removed(self) -> None:
        bus = EventBus()
        seen: list[RunEvent] = []
        remove = bus.add_sink(seen.append)
        remove()
        bus.emit(EventKind.RUN_STARTED, "r")
        assert seen == []

    def test_history_is_replayed_for_late_subscribers(self) -> None:
        bus = EventBus(history=10)
        for index in range(3):
            bus.emit(EventKind.JOB_PROGRESS, "r", data={"n": index})
        assert len(bus.replay()) == 3

    def test_a_slow_subscriber_loses_events_rather_than_blocking(self) -> None:
        """A stalled browser tab must not become backpressure on a run."""

        async def stalled(bus: EventBus) -> None:
            async for _ in bus.subscribe():
                await asyncio.sleep(10)  # never comes back for the next one

        async def drive() -> int:
            bus = EventBus(queue_size=2)
            task = asyncio.ensure_future(stalled(bus))
            await asyncio.sleep(0)  # let the subscriber register its queue
            for index in range(20):
                bus.emit(EventKind.JOB_PROGRESS, "r", data={"n": index})
            task.cancel()
            return bus.dropped

        assert asyncio.run(drive()) > 0

    def test_events_convert_to_store_rows(self) -> None:
        event = RunEvent(kind=EventKind.JOB_FAILED, run_id="r", entity="e", message="bad")
        row = event.to_store_row()
        assert row["event"] == "job.failed" and row["entity"] == "e"


class TestMetrics:
    def test_throughput_reports_a_recent_rate(self) -> None:
        throughput = Throughput(window_seconds=10)
        for index in range(10):
            throughput.record(100, now=index * 0.1)
        assert throughput.total == 1000
        assert throughput.rate > 0

    def test_per_entity_progress(self) -> None:
        metrics = RunMetrics(run_id="r")
        metrics.entity("employee", requested=100)
        metrics.record_batch("employee", 25)
        snapshot = metrics.snapshot()
        assert snapshot["records_written"] == 25
        assert snapshot["entities"]["employee"]["progress"] == 0.25
        assert snapshot["progress"] == 0.25

    def test_quality_scores(self) -> None:
        metrics = RunMetrics(run_id="r")
        metrics.entity("e", requested=100)
        metrics.record_batch("e", 100)
        metrics.validation_failures = 1
        assert metrics.quality()["constraint_validity"] == 0.99

    def test_an_empty_run_scores_perfectly_rather_than_dividing_by_zero(self) -> None:
        assert RunMetrics(run_id="r").quality()["constraint_validity"] == 1.0

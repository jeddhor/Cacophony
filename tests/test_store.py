"""The metadata store (design document sections 42, 56, 73)."""

from __future__ import annotations

import pytest

from cacophony.store import Database, Repository
from cacophony.store.database import default_store_path
from cacophony.store.models import SCHEMA_VERSION
from helpers import make_project

SCHEMA = {
    "widget": {
        "count": 25,
        "fields": {
            "sku": {"type": "string", "generator": "sequence", "format": "SKU-{0000}"},
            "colour": {"generator": "weighted", "choices": ["red", "blue"]},
        },
    }
}


@pytest.fixture
def repo() -> Repository:
    return Repository(Database())


@pytest.fixture
def project():
    return make_project(SCHEMA, name="Store Test", seed=7)


class TestDatabase:
    def test_in_memory_store(self) -> None:
        db = Database()
        assert db.describe()["schema_version"] == SCHEMA_VERSION
        db.close()

    def test_file_store_creates_its_directory(self, tmp_path) -> None:
        path = tmp_path / "nested" / "deep" / "store.db"
        with Database(path) as db:
            assert path.exists()
            assert db.describe()["path"] == str(path)

    def test_reopening_keeps_the_data(self, tmp_path, project) -> None:
        path = tmp_path / "store.db"
        with Database(path) as db:
            Repository(db).upsert_project(project, path="a.yaml", source_text="x: 1")
        with Database(path) as db:
            assert len(Repository(db).list_projects()) == 1

    def test_a_newer_store_is_refused(self, tmp_path) -> None:
        """Better to say so than to fail obscurely three tables in."""
        from sqlalchemy import text

        path = tmp_path / "store.db"
        with Database(path) as db, db.engine.begin() as connection:
            connection.execute(text("UPDATE schema_version SET version = 99"))
        with pytest.raises(RuntimeError, match="newer version"):
            Database(path)

    def test_default_path_sits_beside_the_project(self, tmp_path) -> None:
        path = default_store_path(tmp_path / "project.yaml")
        assert path.parent == tmp_path / ".cacophony"
        assert path.name == "cacophony.db"


class TestProjectsAndRevisions:
    def test_project_is_recorded(self, repo, project) -> None:
        project_id, revision_id = repo.upsert_project(
            project, path="p.yaml", source_text="project: {name: x}"
        )
        record = repo.get_project(project_id)
        assert record["name"] == "Store Test"
        assert revision_id is not None
        assert len(record["revisions"]) == 1

    def test_an_unchanged_schema_reuses_its_revision(self, repo, project) -> None:
        """A hundred runs of one project should not leave a hundred revisions."""
        text = "project:\n  name: Store Test\n"
        first = repo.upsert_project(project, path="p.yaml", source_text=text)[1]
        for _ in range(5):
            again = repo.upsert_project(project, path="p.yaml", source_text=text)[1]
            assert again == first
        assert repo.stats()["revisions"] == 1

    def test_an_edited_schema_creates_a_revision(self, repo, project) -> None:
        """Section 73: projects evolve, and runs must record which version."""
        first = repo.upsert_project(project, path="p.yaml", source_text="a: 1")[1]
        second = repo.upsert_project(project, path="p.yaml", source_text="a: 2")[1]
        assert second != first
        versions = [r["version"] for r in repo.get_project(1)["revisions"]]
        assert versions == [1, 2]

    def test_revision_source_is_stored_verbatim(self, repo, project) -> None:
        source = "project:\n  name: Store Test   # a comment\n"
        _, revision_id = repo.upsert_project(project, path="p.yaml", source_text=source)
        assert repo.get_revision(revision_id)["source_text"] == source

    def test_revision_summary_describes_the_shape(self, repo, project) -> None:
        _, revision_id = repo.upsert_project(project, path="p.yaml", source_text="x")
        summary = repo.get_revision(revision_id)["summary"]
        assert summary["entities"]["widget"]["count"] == 25
        assert summary["entities"]["widget"]["fields"] == 2

    def test_the_path_identifies_the_project(self, repo, project) -> None:
        """Two projects may share a name; two files at one path may not."""
        first = repo.upsert_project(project, path="a.yaml", source_text="x")[0]
        second = repo.upsert_project(project, path="b.yaml", source_text="x")[0]
        assert first != second
        assert repo.upsert_project(project, path="a.yaml", source_text="x")[0] == first

    def test_pathless_projects_are_matched_by_name(self, repo, project) -> None:
        first = repo.upsert_project(project, source_text="x")[0]
        assert repo.upsert_project(project, source_text="x")[0] == first


class TestRuns:
    def _run(self, repo, project, run_id="r1") -> str:
        project_id, revision_id = repo.upsert_project(project, path="p.yaml", source_text="x")
        repo.create_run(
            run_id=run_id,
            project_id=project_id,
            revision_id=revision_id,
            seed=7,
            output_dir="out",
            output_format="jsonl",
            config={"records": 25},
            estimate={"records": 25},
            records_requested=25,
        )
        return run_id

    def test_create_and_fetch(self, repo, project) -> None:
        run_id = self._run(repo, project)
        run = repo.get_run(run_id)
        assert run["state"] == "queued"
        assert run["records_requested"] == 25
        assert run["revision_id"] is not None

    def test_update(self, repo, project) -> None:
        run_id = self._run(repo, project)
        repo.update_run(run_id, state="completed", records_written=25)
        run = repo.get_run(run_id)
        assert run["state"] == "completed" and run["progress"] == 1.0

    def test_listing_and_filtering(self, repo, project) -> None:
        self._run(repo, project, "a")
        self._run(repo, project, "b")
        repo.update_run("a", state="completed")
        assert len(repo.list_runs()) == 2
        assert [r["id"] for r in repo.list_runs(state="completed")] == ["a"]

    def test_resumable_runs_exclude_completed(self, repo, project) -> None:
        self._run(repo, project, "done")
        self._run(repo, project, "stopped")
        repo.update_run("done", state="completed")
        repo.update_run("stopped", state="failed")
        assert [r["id"] for r in repo.resumable_runs()] == ["stopped"]

    def test_deleting_a_run_takes_its_jobs_and_events(self, repo, project) -> None:
        run_id = self._run(repo, project)
        repo.create_jobs(run_id, [{"sequence": 0, "entity": "widget", "requested": 25}])
        repo.add_event(run_id, event="run.started")
        assert repo.delete_run(run_id) is True
        assert repo.stats()["jobs"] == 0 and repo.stats()["events"] == 0

    def test_pruning_keeps_the_newest(self, repo, project) -> None:
        for index in range(6):
            self._run(repo, project, f"run{index}")
        assert repo.prune_runs(keep=2) == 4
        assert len(repo.list_runs()) == 2


class TestJobsAndCheckpoints:
    def _job(self, repo, project) -> tuple[str, int]:
        project_id, _ = repo.upsert_project(project, path="p.yaml", source_text="x")
        repo.create_run(
            run_id="r",
            project_id=project_id,
            revision_id=None,
            seed=1,
            output_dir="out",
            output_format="jsonl",
            config={},
            estimate={},
            records_requested=1000,
        )
        rows = repo.create_jobs(
            "r", [{"sequence": 0, "entity": "widget", "requested": 1000, "state": "queued"}]
        )
        return "r", rows[0]["id"]

    def test_checkpoint_records_progress(self, repo, project) -> None:
        """Section 32's checkpoint, which is what makes a resume possible."""
        run_id, job_id = self._job(repo, project)
        repo.checkpoint_job(
            job_id,
            completed=6830,
            checkpoint={"run": run_id, "entity": "widget", "completed": 6830},
        )
        job = repo.get_jobs(run_id)[0]
        assert job["completed"] == 6830
        assert job["checkpoint"]["completed"] == 6830
        assert job["checkpointed_at"] is not None

    def test_remaining_and_progress(self, repo, project) -> None:
        run_id, job_id = self._job(repo, project)
        repo.checkpoint_job(job_id, completed=250)
        job = repo.get_jobs(run_id)[0]
        assert job["remaining"] == 750 and job["progress"] == 0.25

    def test_state_updates(self, repo, project) -> None:
        run_id, job_id = self._job(repo, project)
        repo.update_job(job_id, state="failed", error="disk full")
        job = repo.get_jobs(run_id)[0]
        assert job["state"] == "failed" and job["error"] == "disk full"


class TestEventsAndStatistics:
    def _run(self, repo, project) -> str:
        project_id, _ = repo.upsert_project(project, path="p.yaml", source_text="x")
        repo.create_run(
            run_id="r",
            project_id=project_id,
            revision_id=None,
            seed=1,
            output_dir="o",
            output_format="jsonl",
            config={},
            estimate={},
            records_requested=1,
        )
        return "r"

    def test_events_carry_section_86_fields(self, repo, project) -> None:
        run_id = self._run(repo, project)
        repo.add_event(
            run_id,
            event="job.completed",
            entity="widget",
            message="done",
            data={"record_range": "0-999", "duration_ms": 12.5},
        )
        event = repo.get_events(run_id)[0]
        assert event["event"] == "job.completed"
        assert event["entity"] == "widget"
        assert event["data"]["record_range"] == "0-999"

    def test_events_can_be_polled_incrementally(self, repo, project) -> None:
        run_id = self._run(repo, project)
        for index in range(5):
            repo.add_event(run_id, event=f"e{index}")
        first_two = repo.get_events(run_id, limit=2)
        rest = repo.get_events(run_id, after_id=first_two[-1]["id"])
        assert len(rest) == 3

    def test_events_can_be_filtered_by_level(self, repo, project) -> None:
        run_id = self._run(repo, project)
        repo.add_event(run_id, event="ok")
        repo.add_event(run_id, event="bad", level="error")
        assert len(repo.get_events(run_id, level="error")) == 1

    def test_statistics_are_upserted(self, repo, project) -> None:
        run_id = self._run(repo, project)
        repo.record_statistic(run_id, "records_written", 100.0)
        repo.record_statistic(run_id, "records_written", 200.0)
        stats = repo.get_run(run_id)["statistics"]
        assert len(stats) == 1 and stats[0]["value"] == 200.0

    def test_statistics_split_numbers_from_everything_else(self, repo, project) -> None:
        run_id = self._run(repo, project)
        repo.record_statistics(run_id, {"count": 5, "note": "hello", "flag": True})
        stats = {s["name"]: s for s in repo.get_run(run_id)["statistics"]}
        assert stats["count"]["value"] == 5.0
        assert stats["note"]["value"] is None
        assert stats["note"]["detail"]["value"] == "hello"

    def test_scopes_are_separate(self, repo, project) -> None:
        run_id = self._run(repo, project)
        repo.record_statistic(run_id, "rate", 1.0, scope="run")
        repo.record_statistic(run_id, "rate", 0.5, scope="quality")
        assert len(repo.get_run(run_id)["statistics"]) == 2

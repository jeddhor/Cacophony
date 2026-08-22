"""The REST API and live feed (design document sections 36, 51, 55).

Section 36's routes are driven through FastAPI's test client; the WebSocket
feed is exercised through :meth:`RunService.stream`, which is the logic the
socket route wraps. Starlette's test client deadlocks on a socket that closes
as fast as a small run finishes, so testing the route that way would be
testing the test client.
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from cacophony.api.app import create_app  # noqa: E402
from cacophony.api.service import RunService  # noqa: E402
from cacophony.runs.config import ResourceLimits, RunConfig  # noqa: E402
from helpers import TEMPLATES  # noqa: E402

CORPORATE = str(TEMPLATES / "corporate-directory.yaml")
RETAIL = str(TEMPLATES / "retail-commerce.yaml")


@pytest.fixture
def service(tmp_path: Path) -> RunService:
    return RunService(store_path=tmp_path / "store.db")


@pytest.fixture
def client(service: RunService):
    with TestClient(create_app(service=service)) as test_client:
        yield test_client


@pytest.fixture
def project_id(client) -> int:
    return client.post("/api/projects", json={"path": CORPORATE}).json()["id"]


def await_run(client, run_id: str, *, tries: int = 400) -> dict[str, Any]:
    import time

    for _ in range(tries):
        payload = client.get(f"/api/runs/{run_id}").json()
        if payload["state"] in ("completed", "failed", "cancelled"):
            return payload
        time.sleep(0.02)
    raise AssertionError(f"run {run_id} did not finish")


@pytest.fixture
def relational_project_id(client) -> int:
    """A project with real foreign keys, for the relational routes."""
    return client.post("/api/projects", json={"path": RETAIL}).json()["id"]


# --------------------------------------------------------------------------- #
# Projects
# --------------------------------------------------------------------------- #


class TestProjects:
    def test_register_from_a_path(self, client) -> None:
        response = client.post("/api/projects", json={"path": CORPORATE})
        assert response.status_code == 201
        body = response.json()
        assert body["name"] == "Corporate Directory"
        assert body["revision_id"] is not None

    def test_register_from_inline_source(self, client) -> None:
        source = """
project:
  name: Inline
  seed: 3
entities:
  thing:
    count: 4
    fields:
      id:
        type: string
        generator: sequence
"""
        body = client.post("/api/projects", json={"source": source}).json()
        assert body["name"] == "Inline"

    def test_registering_twice_reuses_the_project(self, client) -> None:
        first = client.post("/api/projects", json={"path": CORPORATE}).json()
        second = client.post("/api/projects", json={"path": CORPORATE}).json()
        assert first["id"] == second["id"]
        assert first["revision_id"] == second["revision_id"]

    def test_neither_path_nor_source_is_rejected(self, client) -> None:
        assert client.post("/api/projects", json={}).status_code == 422

    def test_a_broken_schema_is_a_400(self, client) -> None:
        response = client.post("/api/projects", json={"source": "project: {}"})
        assert response.status_code == 400
        assert response.json()["error"] == "schema"

    def test_listing(self, client, project_id) -> None:
        rows = client.get("/api/projects").json()
        assert [row["id"] for row in rows] == [project_id]

    def test_fetching_one(self, client, project_id) -> None:
        body = client.get(f"/api/projects/{project_id}").json()
        assert body["revisions"]
        assert body["run_count"] == 0

    def test_missing_project_is_a_404(self, client) -> None:
        assert client.get("/api/projects/999").status_code == 404

    def test_plan(self, client, project_id) -> None:
        """Section 28's plan, so the UI can show it before anything runs."""
        body = client.get(f"/api/projects/{project_id}/plan").json()
        assert body["entity_order"] == ["employee", "device", "location"]
        assert body["estimate"]["records"] == 11205

    def test_lint(self, client, project_id) -> None:
        body = client.get(f"/api/projects/{project_id}/lint").json()
        assert body["ok"] is True
        assert isinstance(body["issues"], list)

    def test_preview_labels_each_column_s_source(self, client, project_id) -> None:
        """Section 51: the preview identifies generation sources."""
        body = client.post(
            f"/api/projects/{project_id}/preview", json={"entity": "employee", "count": 3}
        ).json()
        assert len(body["records"]) == 3
        assert body["sources"]["first_name"] == "faker"
        assert body["sources"]["email"] == "template"
        assert body["records"][0]["employee_id"] == "EMP-000001"

    def test_preview_of_an_unknown_entity_is_a_404(self, client, project_id) -> None:
        response = client.post(
            f"/api/projects/{project_id}/preview", json={"entity": "ghost", "count": 1}
        )
        assert response.status_code == 404


# --------------------------------------------------------------------------- #
# Runs
# --------------------------------------------------------------------------- #


class TestRuns:
    def _start(self, client, project_id, tmp_path, **body: Any) -> str:
        payload = {
            "output_dir": str(tmp_path / "out"),
            "records": 60,
            "limits": {"batch_size": 20},
            **body,
        }
        response = client.post(f"/api/projects/{project_id}/runs", json=payload)
        assert response.status_code == 202, response.text
        return response.json()["id"]

    def test_a_run_completes_and_writes_files(self, client, project_id, tmp_path) -> None:
        run_id = self._start(client, project_id, tmp_path)
        final = await_run(client, run_id)
        assert final["state"] == "completed"
        assert final["records_written"] == 180
        assert (tmp_path / "out" / "employee.jsonl").exists()

    def test_jobs_are_reported(self, client, project_id, tmp_path) -> None:
        run_id = self._start(client, project_id, tmp_path)
        await_run(client, run_id)
        jobs = client.get(f"/api/runs/{run_id}/jobs").json()
        assert {job["entity"] for job in jobs} == {"employee", "device", "location"}
        assert all(job["state"] == "completed" for job in jobs)

    def test_events_are_reported(self, client, project_id, tmp_path) -> None:
        run_id = self._start(client, project_id, tmp_path)
        await_run(client, run_id)
        kinds = [event["event"] for event in client.get(f"/api/runs/{run_id}/events").json()]
        assert "run.started" in kinds and "run.completed" in kinds

    def test_listing_and_filtering(self, client, project_id, tmp_path) -> None:
        run_id = self._start(client, project_id, tmp_path, records=20)
        await_run(client, run_id)
        assert len(client.get("/api/runs").json()) == 1
        assert len(client.get("/api/runs", params={"state": "completed"}).json()) == 1
        assert len(client.get("/api/runs", params={"state": "failed"}).json()) == 0

    def test_entity_selection(self, client, project_id, tmp_path) -> None:
        run_id = self._start(client, project_id, tmp_path, entities=["device"], records=25)
        final = await_run(client, run_id)
        assert final["records_written"] == 25
        assert not (tmp_path / "out" / "employee.jsonl").exists()

    def test_seed_override_changes_the_data(self, client, project_id, tmp_path) -> None:
        first = self._start(client, project_id, tmp_path / "a", records=10, seed=1)
        await_run(client, first)
        second = self._start(client, project_id, tmp_path / "b", records=10, seed=2)
        await_run(client, second)
        assert (tmp_path / "a" / "out" / "employee.jsonl").read_bytes() != (
            tmp_path / "b" / "out" / "employee.jsonl"
        ).read_bytes()

    def test_an_unknown_project_is_a_404(self, client, tmp_path) -> None:
        response = client.post("/api/projects/999/runs", json={"output_dir": str(tmp_path)})
        assert response.status_code == 404

    def test_an_unknown_format_is_rejected(self, client, project_id, tmp_path) -> None:
        response = client.post(
            f"/api/projects/{project_id}/runs",
            json={"output_dir": str(tmp_path), "output_format": "papyrus"},
        )
        assert response.status_code == 422

    def test_an_output_profile_decides_the_layout(
        self, client, project_id, tmp_path, monkeypatch
    ) -> None:
        """Section 34: naming a layout is not the same as retyping it."""
        # A profile's path is relative to wherever the server runs, which for
        # this test needs to be somewhere disposable.
        monkeypatch.chdir(tmp_path)
        response = client.post(
            f"/api/projects/{project_id}/runs",
            json={"output_profile": "developer_db", "records": 5, "entities": ["device"]},
        )
        assert response.status_code == 202, response.text
        final = await_run(client, response.json()["id"])
        assert final["state"] == "completed"
        assert final["output_format"] == "jsonl"
        assert final["config"]["output_profile"] == "developer_db"
        assert (tmp_path / "out" / "corporate" / "device.jsonl").exists()

    def test_an_explicit_directory_beats_the_profile(self, client, project_id, tmp_path) -> None:
        """The precedence `generate --output-profile x -d here` already uses."""
        response = client.post(
            f"/api/projects/{project_id}/runs",
            json={
                "output_profile": "developer_db",
                "output_dir": str(tmp_path / "elsewhere"),
                "records": 5,
                "entities": ["device"],
            },
        )
        assert response.status_code == 202, response.text
        await_run(client, response.json()["id"])
        assert (tmp_path / "elsewhere" / "device.jsonl").exists()

    def test_an_unknown_profile_names_the_declared_ones(self, client, project_id) -> None:
        response = client.post(
            f"/api/projects/{project_id}/runs",
            json={"output_profile": "nonesuch", "records": 5},
        )
        assert response.status_code == 422
        assert "developer_db" in response.text

    def test_finished_runs_stop_accumulating_in_memory(
        self, client, service, project_id, tmp_path, monkeypatch
    ) -> None:
        """A server that never forgets a run grows by one engine per run.

        Finished conductors are kept so their metrics stay live-fresh; past a
        limit the store is the record, which is where a finished run's numbers
        live anyway.
        """
        import time

        from cacophony.api import service as service_module

        monkeypatch.setattr(service_module, "TERMINAL_CONDUCTORS_KEPT", 2)

        ids = []
        for index in range(4):
            run_id = self._start(client, project_id, tmp_path / f"r{index}", records=2)
            await_run(client, run_id)
            ids.append(run_id)

        # Retirement happens on the task's done-callback, which fires just
        # after the run's own record says completed.
        for _ in range(200):
            if len(service._conductors) <= 2:
                break
            time.sleep(0.01)

        assert len(service._conductors) == 2
        assert ids[0] not in service._conductors
        assert ids[-1] in service._conductors

        # Forgotten in memory, still completely readable from the store.
        oldest = client.get(f"/api/runs/{ids[0]}").json()
        assert oldest["state"] == "completed"
        assert oldest["records_written"] == 6
        assert "live" not in oldest

    def test_a_finished_run_stops_reporting_itself_as_live(
        self, client, project_id, tmp_path
    ) -> None:
        """The clock does not stop, so the answer must not come from it.

        A finished conductor is kept so its metrics stay readable, and the API
        used to treat its presence as activity - so a run that took 42ms
        reported a minute and counting, beside its own correct duration.
        """
        import time

        run_id = self._start(client, project_id, tmp_path, records=5)
        final = await_run(client, run_id)
        assert final["state"] == "completed"
        assert "live" not in final
        assert client.get(f"/api/runs/{run_id}/quality").json()["live"] is False

        elapsed = final["summary"]["elapsed_seconds"]
        time.sleep(0.4)
        later = client.get(f"/api/runs/{run_id}").json()
        assert "live" not in later
        assert later["summary"]["elapsed_seconds"] == elapsed

    def test_a_running_run_still_reports_live_metrics(self, client, project_id, tmp_path) -> None:
        """The check is 'executing', not 'finished' - a paused run is neither."""
        run_id = self._start(client, project_id, tmp_path, records=20_000)
        for _ in range(400):
            row = client.get(f"/api/runs/{run_id}").json()
            if "live" in row:
                assert row["live"]["run_id"] == run_id
                assert row["paused"] is False
                break
            if row["state"] in ("completed", "failed"):
                pytest.skip("the run finished before it could be observed running")
        else:  # pragma: no cover - timing
            pytest.fail("no live metrics were ever reported")
        await_run(client, run_id)

    def test_the_output_size_is_the_size_of_the_output(self, client, project_id, tmp_path) -> None:
        """It was always zero: nothing ever gave the counter a byte to count."""
        run_id = self._start(client, project_id, tmp_path, records=30)
        final = await_run(client, run_id)

        written = sum(
            path.stat().st_size for path in (tmp_path / "out").rglob("*") if path.is_file()
        )
        assert written > 0
        assert final["summary"]["bytes_written"] == written

    def test_resuming_uses_the_schema_the_run_recorded(self, client, tmp_path) -> None:
        """Section 73: one dataset, generated one way.

        The API used to recompile the project's *head* revision, so editing a
        schema between a cancelled run and its resume produced a file whose
        first half came from one schema and whose second half came from
        another - silently, and only visible in the data.
        """
        import shutil
        import time

        # A copy: this test edits the schema, and the shipped template is not
        # this test's to edit.
        project_path = tmp_path / "project.yaml"
        shutil.copy(CORPORATE, project_path)
        project_id = client.post("/api/projects", json={"path": str(project_path)}).json()["id"]

        started = client.post(
            f"/api/projects/{project_id}/runs",
            json={
                "output_dir": str(tmp_path / "out"),
                "records": 400,
                "limits": {"batch_size": 20},
            },
        ).json()
        run_id = started["id"]
        for _ in range(400):
            live = client.get(f"/api/runs/{run_id}").json().get("live") or {}
            if live.get("records_written", 0) > 40:
                break
            time.sleep(0.01)
        client.post(f"/api/runs/{run_id}/cancel")
        cancelled = await_run(client, run_id)
        assert cancelled["state"] == "cancelled"

        patched = client.patch(
            f"/api/projects/{project_id}/schema",
            json={
                "operations": [
                    {
                        "op": "set_field",
                        "entity": "employee",
                        "field": "first_name",
                        "key": "generator",
                        "value": "constant",
                    },
                    {
                        "op": "set_field",
                        "entity": "employee",
                        "field": "first_name",
                        "key": "value",
                        "value": "AFTERWARDS",
                    },
                ]
            },
        )
        assert patched.status_code == 200 and patched.json()["changed"], patched.text

        assert client.post(f"/api/runs/{run_id}/resume").status_code == 200
        await_run(client, run_id)

        rows = [
            json.loads(line)
            for line in (tmp_path / "out" / "employee.jsonl").read_text().splitlines()
        ]
        assert rows, "the resumed run wrote nothing"
        assert not any(row.get("first_name") == "AFTERWARDS" for row in rows)

    def test_the_api_takes_what_the_command_line_takes(self, client, project_id, tmp_path) -> None:
        """Section 36's claim, which the API had been quietly short of."""
        response = client.post(
            f"/api/projects/{project_id}/runs",
            json={
                "output_dir": str(tmp_path / "out"),
                "record_counts": {"employee": 7, "device": 3},
                "entities": ["employee", "device"],
                "failure_policy": "report",
                "edge_cases": 0.25,
                "edge_categories": ["emoji"],
                "overwrite_assets": True,
                "record_history": True,
            },
        )
        assert response.status_code == 202, response.text
        final = await_run(client, response.json()["id"])
        assert final["state"] == "completed"
        assert final["records_written"] == 10
        assert final["config"]["edge_cases"] == 0.25
        assert final["config"]["failure_policy"] == "report"

    def test_an_unknown_option_is_refused_rather_than_ignored(
        self, client, project_id, tmp_path
    ) -> None:
        """A misspelled option that silently does nothing is the expensive kind."""
        response = client.post(
            f"/api/projects/{project_id}/runs",
            json={"output_dir": str(tmp_path / "out"), "recrods": 10},
        )
        assert response.status_code == 422
        assert "recrods" in response.text

    def test_an_unknown_edge_case_category_is_named(self, client, project_id, tmp_path) -> None:
        response = client.post(
            f"/api/projects/{project_id}/runs",
            json={"output_dir": str(tmp_path / "out"), "edge_categories": ["glitter"]},
        )
        assert response.status_code == 422
        assert "glitter" in response.text

    def test_pausing_says_so_in_the_jobs_and_in_the_events(
        self, client, project_id, tmp_path
    ) -> None:
        """A paused run whose jobs all say `running` describes nothing real."""
        import time

        started = client.post(
            f"/api/projects/{project_id}/runs",
            json={
                "output_dir": str(tmp_path / "out"),
                "records": 4_000,
                "limits": {"batch_size": 50, "max_workers": 1},
            },
        ).json()
        run_id = started["id"]

        for _ in range(400):
            if client.post(f"/api/runs/{run_id}/pause").status_code == 200:
                break
            time.sleep(0.01)
        else:  # pragma: no cover - timing
            pytest.skip("the run finished before it could be paused")

        jobs = client.get(f"/api/runs/{run_id}/jobs").json()
        assert not any(job["state"] == "running" for job in jobs)
        kinds = [event["event"] for event in client.get(f"/api/runs/{run_id}/events").json()]
        assert "run.paused" in kinds

        assert client.post(f"/api/runs/{run_id}/resume").status_code == 200
        kinds = [event["event"] for event in client.get(f"/api/runs/{run_id}/events").json()]
        assert "run.resumed" in kinds
        client.post(f"/api/runs/{run_id}/cancel")
        await_run(client, run_id)

    def test_rejected_records_can_be_looked_at(self, client, tmp_path) -> None:
        """Section 56 asks to browse them, not to be told how many there were."""
        source = """
project: {name: Rejects, seed: 4}
entities:
  reading:
    count: 60
    fields:
      celsius:
        type: integer
        generator: expression
        expression: "int(index) % 100"
        constraints: {max: 49}
      index: {type: integer, generator: sequence, start: 0}
"""
        project_id = client.post("/api/projects", json={"source": source}).json()["id"]
        run_id = client.post(
            f"/api/projects/{project_id}/runs",
            json={
                "output_dir": str(tmp_path / "out"),
                "drop_invalid": True,
                "failure_policy": "skip",
            },
        ).json()["id"]
        await_run(client, run_id)

        body = client.get(f"/api/runs/{run_id}/rejects").json()
        assert body["total"] > 0
        assert body["entities"]["reading"]["rejected"] > 0

        first = body["rejects"][0]
        assert first["entity"] == "reading"
        assert "constraint" in first["categories"]
        assert first["values"]["celsius"] > 49
        assert first["issues"], "a rejected record should say why"

    def test_the_reject_sample_is_capped_and_says_so(self, client, tmp_path) -> None:
        """Section 31: nothing here may grow with the dataset."""
        source = """
project: {name: Rejects, seed: 4}
entities:
  reading:
    count: 400
    fields:
      celsius:
        type: integer
        generator: constant
        value: 500
        constraints: {max: 49}
"""
        project_id = client.post("/api/projects", json={"source": source}).json()["id"]
        run_id = client.post(
            f"/api/projects/{project_id}/runs",
            json={
                "output_dir": str(tmp_path / "out"),
                "drop_invalid": True,
                "failure_policy": "skip",
                "limits": {"keep_rejects": 25},
            },
        ).json()["id"]
        await_run(client, run_id)

        counts = client.get(f"/api/runs/{run_id}/rejects").json()["entities"]["reading"]
        assert counts["rejected"] == 400
        assert counts["kept"] == 25
        assert counts["sampled"] is True

    def test_a_run_that_rejected_nothing_has_nothing_to_show(
        self, client, project_id, tmp_path
    ) -> None:
        run_id = self._start(client, project_id, tmp_path, records=5)
        await_run(client, run_id)
        body = client.get(f"/api/runs/{run_id}/rejects").json()
        assert body["total"] == 0
        assert body["rejects"] == []

    def test_a_missing_run_is_a_404(self, client) -> None:
        assert client.get("/api/runs/nope").status_code == 404

    def test_deleting_a_finished_run(self, client, project_id, tmp_path) -> None:
        run_id = self._start(client, project_id, tmp_path, records=10)
        await_run(client, run_id)
        assert client.delete(f"/api/runs/{run_id}").status_code == 204
        assert client.get(f"/api/runs/{run_id}").status_code == 404

    def test_controlling_a_finished_run_is_a_409(self, client, project_id, tmp_path) -> None:
        run_id = self._start(client, project_id, tmp_path, records=10)
        await_run(client, run_id)
        assert client.post(f"/api/runs/{run_id}/pause").status_code == 409
        assert client.post(f"/api/runs/{run_id}/cancel").status_code == 409
        # A completed run has nothing to resume.
        assert client.post(f"/api/runs/{run_id}/resume").status_code == 409


# --------------------------------------------------------------------------- #
# The Schema Studio (sections 48, 49, 52)
# --------------------------------------------------------------------------- #


class TestStudio:
    @pytest.fixture
    def editable(self, client, tmp_path: Path) -> tuple[int, Path]:
        """A copy of a documented template, so edits can be written back."""
        import shutil

        path = tmp_path / "project.yaml"
        shutil.copy(TEMPLATES / "corporate-directory.yaml", path)
        project_id = client.post("/api/projects", json={"path": str(path)}).json()["id"]
        return project_id, path

    def test_the_schema_view_carries_what_the_studio_needs(self, client, project_id) -> None:
        body = client.get(f"/api/projects/{project_id}/schema").json()
        assert body["entity_order"] == ["employee", "device", "location"]
        assert body["source"].startswith("# Corporate Directory")

        employee = body["entities"]["employee"]
        assert employee["count"] == 5000
        assert employee["primary_key"] == "employee_id"
        # Authored order, not dependency order: the schema as it reads.
        assert employee["field_order"][0] == "employee_id"
        assert len(employee["layers"]) >= 1

    def test_fields_report_their_generator_and_dependencies(self, client, project_id) -> None:
        fields = client.get(f"/api/projects/{project_id}/schema").json()["entities"]["employee"][
            "fields"
        ]
        assert fields["email"]["generator"] == "template"
        assert fields["email"]["dependencies"] == ["first_name", "last_name"]
        # Section 68: a field with only a semantic description still gets one.
        assert fields["first_name"]["generator"] == "faker"
        assert fields["first_name"]["inferred"] is True
        assert fields["employee_id"]["inferred"] is False

    def test_categorical_fields_report_a_distribution(self, client, project_id) -> None:
        """Section 52: the Studio draws these as bars."""
        fields = client.get(f"/api/projects/{project_id}/schema").json()["entities"]["employee"][
            "fields"
        ]
        distribution = fields["department"]["distribution"]
        assert distribution is not None
        assert abs(sum(distribution.values()) - 1.0) < 1e-6
        assert distribution["Engineering"] > distribution["Legal"]

    def test_a_non_categorical_field_has_no_distribution(self, client, project_id) -> None:
        fields = client.get(f"/api/projects/{project_id}/schema").json()["entities"]["employee"][
            "fields"
        ]
        assert fields["employee_id"]["distribution"] is None

    def test_editability_is_reported_honestly(self, client, tmp_path) -> None:
        inline = client.post(
            "/api/projects",
            json={
                "source": "project:\n  name: Inline\nentities:\n  e:\n    fields:\n"
                "      id:\n        type: string\n"
            },
        ).json()["id"]
        assert client.get(f"/api/projects/{inline}/schema").json()["editable"] is False

    def test_patching_a_field_preserves_the_document(self, client, editable) -> None:
        project_id, path = editable
        before = path.read_text(encoding="utf-8")

        response = client.patch(
            f"/api/projects/{project_id}/schema",
            json={
                "operations": [
                    {
                        "op": "set_field",
                        "entity": "employee",
                        "field": "age",
                        "key": "semantic",
                        "value": "Age in whole years",
                    }
                ]
            },
        )
        assert response.status_code == 200
        after = path.read_text(encoding="utf-8")
        assert "# Corporate Directory" in after
        assert "Age in whole years" in after
        assert len(after.splitlines()) >= len(before.splitlines())

    def test_a_patch_creates_a_new_schema_revision(self, client, editable) -> None:
        """Section 73: every state a run could have used is recorded."""
        project_id, _path = editable
        first = client.get(f"/api/projects/{project_id}/schema").json()["revision_id"]
        response = client.patch(
            f"/api/projects/{project_id}/schema",
            json={
                "operations": [
                    {"op": "set_entity", "entity": "device", "key": "count", "value": 42}
                ]
            },
        )
        assert response.json()["revision_id"] != first
        assert len(client.get(f"/api/projects/{project_id}").json()["revisions"]) == 2

    def test_an_invalid_patch_leaves_the_file_alone(self, client, editable) -> None:
        project_id, path = editable
        before = path.read_text(encoding="utf-8")
        response = client.patch(
            f"/api/projects/{project_id}/schema",
            json={
                "operations": [
                    {
                        "op": "set_field",
                        "entity": "employee",
                        "field": "age",
                        "key": "type",
                        "value": "banana",
                    }
                ]
            },
        )
        assert response.status_code == 400
        assert path.read_text(encoding="utf-8") == before

    def test_the_plan_reflects_an_edit_immediately(self, client, editable) -> None:
        project_id, _path = editable
        client.patch(
            f"/api/projects/{project_id}/schema",
            json={
                "operations": [
                    {"op": "set_entity", "entity": "location", "key": "count", "value": 11}
                ]
            },
        )
        plan = client.get(f"/api/projects/{project_id}/plan").json()
        step = next(item for item in plan["steps"] if item["entity"] == "location")
        assert step["count"] == 11

    def test_writing_a_whole_schema(self, client, editable) -> None:
        project_id, path = editable
        source = (
            "project:\n  name: Replaced\n  seed: 5\n"
            "entities:\n  thing:\n    count: 3\n    fields:\n"
            "      id:\n        type: string\n        generator: sequence\n"
        )
        response = client.put(f"/api/projects/{project_id}/schema", json={"source": source})
        assert response.status_code == 200
        assert path.read_text(encoding="utf-8") == source
        assert client.get(f"/api/projects/{project_id}/schema").json()["name"] == "Replaced"

    def test_writing_a_schema_that_will_not_compile_is_refused(self, client, editable) -> None:
        project_id, path = editable
        before = path.read_text(encoding="utf-8")
        response = client.put(f"/api/projects/{project_id}/schema", json={"source": "project: {}"})
        assert response.status_code == 400
        assert path.read_text(encoding="utf-8") == before

    def test_editing_a_project_with_no_file_is_refused(self, client) -> None:
        inline = client.post(
            "/api/projects",
            json={
                "source": "project:\n  name: Inline\nentities:\n  e:\n    fields:\n"
                "      id:\n        type: string\n"
            },
        ).json()["id"]
        response = client.patch(
            f"/api/projects/{inline}/schema",
            json={"operations": [{"op": "set_project", "key": "seed", "value": 1}]},
        )
        assert response.status_code == 400
        assert "no file to write to" in response.json()["detail"]

    def test_the_editor_controls_are_described(self, client) -> None:
        body = client.get("/api/schema/types").json()
        assert any(entry["value"] == "string" for entry in body["types"])
        assert any(generator["name"] == "llm" for generator in body["generators"])
        assert "field" in body["provenance"]

    def test_the_operations_are_documented(self, client) -> None:
        operations = {entry["op"] for entry in client.get("/api/schema/operations").json()}
        assert {"set_field", "add_field", "rename_field"} <= operations


# --------------------------------------------------------------------------- #
# Serving the built Studio
# --------------------------------------------------------------------------- #


class TestStaticStudio:
    def test_the_studio_is_served_when_it_has_been_built(self, service, tmp_path) -> None:
        static = tmp_path / "static"
        (static / "assets").mkdir(parents=True)
        (static / "index.html").write_text("<div id='root'></div>", encoding="utf-8")
        (static / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")

        with TestClient(create_app(service=service, static_dir=static)) as client:
            assert client.get("/").text == "<div id='root'></div>"
            assert "console.log" in client.get("/assets/app.js").text
            # The client router owns its own URLs, so a deep link must not 404.
            assert client.get("/runs/abc123").text == "<div id='root'></div>"
            # And the API must not be shadowed by the fallback.
            assert client.get("/api/system").status_code == 200

    def test_without_a_build_the_root_explains_itself(self, service) -> None:
        """It used to be a bare 404, which reads as "the server is broken".

        The Studio is generated output and is not committed, so a fresh clone
        genuinely has none - and being told `{"detail":"Not Found"}` when you
        ask for the interface sends you looking at the wrong thing entirely.
        """
        with TestClient(create_app(service=service, static_dir="/nonexistent")) as client:
            assert client.get("/api/system").status_code == 200

            page = client.get("/")
            assert page.status_code == 200
            assert "not built" in page.text
            assert "npm" in page.text

            # The API is a client of this server too, and keeps its own 404s.
            missing = client.get("/api/nothing-here")
            assert missing.status_code == 404
            assert missing.json() == {"detail": "Not Found"}


# --------------------------------------------------------------------------- #
# Providers and system
# --------------------------------------------------------------------------- #


class TestProvidersAndSystem:
    def test_adapters_are_listed(self, client) -> None:
        body = client.get("/api/providers").json()
        assert "ollama" in body["adapters"] and "mock" in body["adapters"]

    def test_configured_providers_for_a_project(self, client) -> None:
        project_id = client.post(
            "/api/projects", json={"path": str(TEMPLATES / "conversational-ai.yaml")}
        ).json()["id"]
        body = client.get("/api/providers", params={"project_id": project_id}).json()
        assert [item["id"] for item in body["configured"]] == ["assistant"]

    def test_provider_health(self, client) -> None:
        project_id = client.post(
            "/api/projects", json={"path": str(TEMPLATES / "conversational-ai.yaml")}
        ).json()["id"]
        body = client.post(
            "/api/providers/assistant/test", params={"project_id": project_id}
        ).json()
        assert body["healthy"] is True

    def test_provider_models(self, client) -> None:
        project_id = client.post(
            "/api/projects", json={"path": str(TEMPLATES / "conversational-ai.yaml")}
        ).json()["id"]
        models = client.get(
            "/api/providers/assistant/models", params={"project_id": project_id}
        ).json()
        assert models[0]["name"] == "mock-1"

    def test_an_unknown_provider_is_a_404(self, client, project_id) -> None:
        response = client.post("/api/providers/ghost/test", params={"project_id": project_id})
        assert response.status_code == 404

    def test_output_formats_come_from_the_registry(self, client) -> None:
        """Six formats, including the two the Studio used to leave out."""
        body = client.get("/api/outputs").json()
        formats = {entry["name"]: entry for entry in body["formats"]}
        assert {"jsonl", "json", "csv", "parquet", "sqlite", "sql"} <= set(formats)
        assert formats["sqlite"]["single_file"] is True
        assert formats["sqlite"]["extension"] == ".db"
        # An alias is folded into the format it names rather than offered twice.
        assert "ndjson" in formats["jsonl"]["aliases"]
        assert "ndjson" not in formats

    def test_a_projects_declared_layouts_are_offered(self, client, project_id) -> None:
        body = client.get("/api/outputs", params={"project_id": project_id}).json()
        profiles = {entry["name"]: entry for entry in body["profiles"]}
        assert profiles["analytics"]["format"] == "parquet"
        assert profiles["developer_db"]["path"] == "out/corporate"
        assert body["chaos"] is False

    def test_generators_are_listed(self, client) -> None:
        names = {row["name"] for row in client.get("/api/generators").json()}
        assert {"sequence", "faker", "llm"} <= names

    def test_system_reports_the_store(self, client, project_id) -> None:
        body = client.get("/api/system").json()
        assert body["projects"] == 1
        assert "schema_version" in body["store"]

    def test_pruning(self, client, project_id, tmp_path) -> None:
        for index in range(3):
            run_id = client.post(
                f"/api/projects/{project_id}/runs",
                json={"output_dir": str(tmp_path / f"o{index}"), "records": 5},
            ).json()["id"]
            await_run(client, run_id)
        assert client.post("/api/system/prune", json={"keep": 1}).json()["deleted"] == 2


# --------------------------------------------------------------------------- #
# The live feed (section 55)
# --------------------------------------------------------------------------- #


class TestLiveFeed:
    def _run(self, service: RunService, tmp_path: Path, records: int = 400) -> Any:
        record = service.register_project(path=CORPORATE)
        config = RunConfig(
            output_dir=tmp_path / "out",
            entities=["employee"],
            records=records,
            limits=ResourceLimits(batch_size=20),
            checkpoint_every=100,
        )
        return record["id"], config

    def test_the_stream_reports_progress_and_finishes(self, service, tmp_path) -> None:
        async def drive() -> list[dict[str, Any]]:
            project_id, config = self._run(service, tmp_path)
            run = await service.start_run(project_id, config)
            seen: list[dict[str, Any]] = []
            async for payload in service.stream(run["id"]):
                seen.append(payload)
                if payload["kind"] in ("run.completed", "run.failed", "run.cancelled"):
                    break
            await service.shutdown()
            return seen

        events = asyncio.run(drive())
        kinds = Counter(event["kind"] for event in events)
        assert kinds["run.started"] == 1
        assert kinds["job.progress"] > 1
        assert kinds["run.completed"] == 1

    def test_the_final_event_carries_section_55_figures(self, service, tmp_path) -> None:
        async def drive() -> dict[str, Any]:
            project_id, config = self._run(service, tmp_path)
            run = await service.start_run(project_id, config)
            final: dict[str, Any] = {}
            async for payload in service.stream(run["id"]):
                if payload["kind"] == "run.completed":
                    final = payload
                    break
            await service.shutdown()
            return final

        data = asyncio.run(drive())["data"]
        assert data["records_written"] == 400
        assert data["mean_records_per_second"] > 0
        assert data["entities"]["employee"]["progress"] == 1.0
        assert "quality" in data

    def test_pause_and_resume_through_the_service(self, service, tmp_path) -> None:
        async def drive() -> Any:
            project_id, config = self._run(service, tmp_path, records=1500)
            run = await service.start_run(project_id, config)
            run_id = run["id"]

            conductor = service.conductor(run_id)
            while conductor.metrics.total_written < 100:
                await asyncio.sleep(0.001)

            assert service.pause(run_id) is True
            paused_at = conductor.metrics.total_written
            await asyncio.sleep(0.05)
            assert conductor.metrics.total_written == paused_at

            assert service.resume_paused(run_id) is True
            outcome = await service.wait(run_id, timeout=30)
            await service.shutdown()
            return outcome

        outcome = asyncio.run(drive())
        assert outcome.ok and outcome.records == 1500

    def test_cancel_through_the_service(self, service, tmp_path) -> None:
        async def drive() -> Any:
            project_id, config = self._run(service, tmp_path, records=4000)
            run = await service.start_run(project_id, config)
            conductor = service.conductor(run["id"])
            while conductor.metrics.total_written < 60:
                await asyncio.sleep(0.001)
            assert service.cancel(run["id"]) is True
            outcome = await service.wait(run["id"], timeout=30)
            await service.shutdown()
            return outcome

        outcome = asyncio.run(drive())
        assert outcome.state.value == "cancelled"
        assert outcome.records < 4000

    def test_a_stopped_run_can_be_restarted_from_its_checkpoints(self, service, tmp_path) -> None:
        async def drive() -> Any:
            project_id, config = self._run(service, tmp_path, records=1200)
            run = await service.start_run(project_id, config)
            conductor = service.conductor(run["id"])
            while conductor.metrics.total_written < 100:
                await asyncio.sleep(0.001)
            service.cancel(run["id"])
            await service.wait(run["id"], timeout=30)

            await service.resume_run(run["id"])
            outcome = await service.wait(run["id"], timeout=60)
            await service.shutdown()
            return outcome

        outcome = asyncio.run(drive())
        assert outcome.ok
        lines = (tmp_path / "out" / "employee.jsonl").read_text().splitlines()
        assert len(lines) == 1200

    def test_streaming_an_unknown_run_yields_nothing(self, service) -> None:
        async def drive() -> list[Any]:
            return [payload async for payload in service.stream("nope")]

        assert asyncio.run(drive()) == []


# --------------------------------------------------------------------------- #
# Relationships and quality (design document sections 15, 57, 58)
# --------------------------------------------------------------------------- #


class TestReferences:
    def test_the_schema_reports_every_foreign_key_as_an_edge(
        self, client, relational_project_id
    ) -> None:
        schema = client.get(f"/api/projects/{relational_project_id}/schema").json()
        edges = {
            (edge["from_entity"], edge["from_field"], edge["to_entity"], edge["to_field"])
            for edge in schema["references"]
        }
        assert ("order", "customer", "customer", "customer_id") in edges
        assert ("order_item", "product", "product", "sku") in edges

    def test_a_reference_field_carries_where_it_points(self, client, relational_project_id) -> None:
        schema = client.get(f"/api/projects/{relational_project_id}/schema").json()
        reference = schema["entities"]["order"]["fields"]["customer"]["reference"]
        assert reference == {
            "entity": "customer",
            "field": "customer_id",
            "distribution": "skewed",
            "unique": False,
        }

    def test_an_ordinary_field_points_nowhere(self, client, relational_project_id) -> None:
        schema = client.get(f"/api/projects/{relational_project_id}/schema").json()
        assert schema["entities"]["customer"]["fields"]["email"]["reference"] is None


class TestQualityRoute:
    def _start(self, client, project_id, tmp_path, **body: Any) -> str:
        payload = {
            "output_dir": str(tmp_path / "out"),
            "records": 200,
            "limits": {"batch_size": 100},
            **body,
        }
        response = client.post(f"/api/projects/{project_id}/runs", json=payload)
        assert response.status_code == 202, response.text
        return response.json()["id"]

    def test_it_reports_referential_integrity(
        self, client, relational_project_id, tmp_path
    ) -> None:
        run_id = self._start(client, relational_project_id, tmp_path)
        await_run(client, run_id)

        report = client.get(f"/api/runs/{run_id}/quality").json()
        assert report["state"] == "completed"
        assert report["quality"]["referential_integrity"] == 1.0
        assert report["relations"]["key_lookups"] > 0

    def test_it_reports_which_distribution_drifted(
        self, client, relational_project_id, tmp_path
    ) -> None:
        run_id = self._start(client, relational_project_id, tmp_path)
        await_run(client, run_id)

        report = client.get(f"/api/runs/{run_id}/quality").json()
        checks = report["validation"]["customer"]["statistical"]["checks"]
        fields = {check["field"] for check in checks}
        assert {"country", "tier"} <= fields
        assert all(0.0 <= check["match"] <= 1.0 for check in checks)

    def test_a_project_with_no_references_reports_none(self, client, project_id, tmp_path) -> None:
        run_id = self._start(client, project_id, tmp_path)
        await_run(client, run_id)

        report = client.get(f"/api/runs/{run_id}/quality").json()
        assert "referential_integrity" not in report["quality"]
        assert report["relations"] is None

    def test_an_unknown_run_is_a_404(self, client) -> None:
        assert client.get("/api/runs/nope/quality").status_code == 404


class TestDatabaseRuns:
    def test_a_sqlite_run_produces_one_database_with_working_joins(
        self, client, relational_project_id, tmp_path
    ) -> None:
        import sqlite3

        response = client.post(
            f"/api/projects/{relational_project_id}/runs",
            json={
                "output_dir": str(tmp_path / "db"),
                "output_format": "sqlite",
                "records": 200,
                "limits": {"batch_size": 100},
            },
        )
        assert response.status_code == 202, response.text
        await_run(client, response.json()["id"])

        files = list((tmp_path / "db").glob("*.db"))
        assert len(files) == 1

        connection = sqlite3.connect(files[0])
        try:
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
            joined = connection.execute(
                "SELECT COUNT(*) FROM order_item i JOIN product p ON i.product = p.sku"
            ).fetchone()[0]
            assert joined == 200
        finally:
            connection.close()

    def test_an_unknown_output_format_is_rejected(self, client, project_id) -> None:
        response = client.post(f"/api/projects/{project_id}/runs", json={"output_format": "parqet"})
        assert response.status_code == 422
        assert "parquet" in response.text


# --------------------------------------------------------------------------- #
# Assets (design document sections 19, 81)
# --------------------------------------------------------------------------- #


MULTIMODAL = str(TEMPLATES / "multimodal-support.yaml")


class TestAssetRoutes:
    @pytest.fixture
    def media_run(self, client, tmp_path) -> tuple[str, dict[str, Any]]:
        project_id = client.post("/api/projects", json={"path": MULTIMODAL}).json()["id"]
        response = client.post(
            f"/api/projects/{project_id}/runs",
            json={"output_dir": str(tmp_path / "out"), "records": 3},
        )
        assert response.status_code == 202, response.text
        run_id = response.json()["id"]
        await_run(client, run_id)
        return run_id, client.get(f"/api/runs/{run_id}/assets").json()

    def test_it_lists_what_the_run_produced(self, media_run) -> None:
        _run_id, listing = media_run
        assert listing["total"] == 12
        assert set(listing["kinds"]) == {"image", "audio", "document"}
        assert set(listing["entities"]) == {"employee", "support_call"}

    def test_every_asset_names_the_record_it_belongs_to(self, media_run) -> None:
        """Section 81: assets reference their parent."""
        _run_id, listing = media_run
        for asset in listing["assets"]:
            assert asset["record_id"]
            assert asset["field"]

    def test_filtering_by_kind(self, client, media_run) -> None:
        run_id, _listing = media_run
        audio = client.get(f"/api/runs/{run_id}/assets?kind=audio").json()
        assert audio["total"] == 3
        assert all(asset["kind"] == "audio" for asset in audio["assets"])

    def test_filtering_by_entity(self, client, media_run) -> None:
        run_id, _listing = media_run
        employees = client.get(f"/api/runs/{run_id}/assets?entity=employee").json()
        assert all(asset["entity"] == "employee" for asset in employees["assets"])

    def test_a_file_can_be_fetched(self, client, media_run) -> None:
        listing = media_run[1]
        image = next(asset for asset in listing["assets"] if asset["kind"] == "image")
        response = client.get(image["url"])
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"
        assert response.content[:8] == b"\x89PNG\r\n\x1a\n"

    def test_a_path_outside_the_run_is_refused(self, client, media_run) -> None:
        """A path parameter naming a file is a traversal waiting to happen."""
        run_id = media_run[0]
        assert client.get(f"/api/runs/{run_id}/assets/file?path=/etc/passwd").status_code == 403
        assert (
            client.get(f"/api/runs/{run_id}/assets/file?path=../../../etc/passwd").status_code
            == 403
        )

    def test_a_run_with_no_assets_reports_none(self, client, project_id, tmp_path) -> None:
        response = client.post(
            f"/api/projects/{project_id}/runs",
            json={"output_dir": str(tmp_path / "plain"), "records": 5},
        )
        run_id = response.json()["id"]
        await_run(client, run_id)
        listing = client.get(f"/api/runs/{run_id}/assets").json()
        assert listing["total"] == 0

    def test_an_unknown_run_is_a_404(self, client) -> None:
        assert client.get("/api/runs/nope/assets").status_code == 404

    def test_the_summary_counts_the_assets(self, client, media_run) -> None:
        run_id, _listing = media_run
        summary = client.get(f"/api/runs/{run_id}").json()["summary"]
        assert summary["assets"]["assets"] == 12
        assert summary["assets"]["bytes_written"] > 0


# --------------------------------------------------------------------------- #
# Live streams
# --------------------------------------------------------------------------- #


def await_stream_records(client, stream_id: str, *, tries: int = 200) -> list[dict[str, Any]]:
    """Wait for a stream to have actually produced something."""
    import time

    for _ in range(tries):
        rows = client.get(f"/api/streams/{stream_id}/records").json()["records"]
        if rows:
            return rows
        time.sleep(0.05)
    return []


class TestStreams:
    """Sections 35 and 94, over HTTP."""

    def _start(self, client, project_id: int, **body: Any) -> dict[str, Any]:
        payload = {"rates": {"employee": "200/s"}, "keep_records": 50, **body}
        response = client.post(f"/api/projects/{project_id}/streams", json=payload)
        assert response.status_code == 201, response.text
        return response.json()

    def test_starting_one_returns_its_status(self, client, project_id) -> None:
        stream = self._start(client, project_id)
        assert stream["state"] in ("queued", "running")
        assert stream["config"]["rates"] == {"employee": "200/s"}
        assert [entity["entity"] for entity in stream["entities"]] == ["employee"]
        client.post(f"/api/streams/{stream['id']}/stop")

    def test_a_stream_needs_somewhere_to_go(self, client, project_id) -> None:
        """No destination and no sample window is a stream nobody can observe."""
        response = client.post(
            f"/api/projects/{project_id}/streams",
            json={"rates": {"employee": "10/s"}, "keep_records": 0},
        )
        assert response.status_code == 400
        assert "somewhere to go" in response.json()["detail"]

    def test_an_unknown_entity_is_refused(self, client, project_id) -> None:
        response = client.post(
            f"/api/projects/{project_id}/streams", json={"rates": {"nonexistent": "10/s"}}
        )
        assert response.status_code == 400
        assert "nonexistent" in response.json()["detail"]

    def test_it_produces_records_a_browser_can_read(self, client, project_id) -> None:
        stream = self._start(client, project_id)
        rows = await_stream_records(client, stream["id"])
        assert rows, "the stream produced nothing"
        assert rows[0]["entity"] == "employee"
        assert "employee_id" in rows[0]["record"]
        # Newest first, and the window is bounded.
        assert rows[0]["seq"] >= rows[-1]["seq"]
        payload = client.get(f"/api/streams/{stream['id']}/records?limit=5").json()
        assert payload["sampled"] and len(payload["records"]) <= 5
        client.post(f"/api/streams/{stream['id']}/stop")

    def test_the_window_does_not_grow(self, client, project_id) -> None:
        """A six-hour stream must cost what a six-second one does."""
        stream = self._start(client, project_id, rates={"employee": "2000/s"}, keep_records=20)
        await_stream_records(client, stream["id"])
        import time

        time.sleep(0.4)
        payload = client.get(f"/api/streams/{stream['id']}/records?limit=1000").json()
        assert len(payload["records"]) <= 20
        client.post(f"/api/streams/{stream['id']}/stop")

    def test_retarget_changes_the_rate_while_it_runs(self, client, project_id) -> None:
        """Section 94's steering - the reason these routes exist."""
        stream = self._start(client, project_id)
        response = client.post(
            f"/api/streams/{stream['id']}/retarget", json={"entity": "employee", "rate": "50/s"}
        )
        assert response.status_code == 200
        assert response.json()["per_second"] == 50.0

        status = client.get(f"/api/streams/{stream['id']}").json()
        assert status["config"]["rates"]["employee"] == "50/s"
        client.post(f"/api/streams/{stream['id']}/stop")

    def test_retargeting_an_entity_that_is_not_streaming(self, client, project_id) -> None:
        stream = self._start(client, project_id)
        response = client.post(
            f"/api/streams/{stream['id']}/retarget", json={"entity": "device", "rate": "5/s"}
        )
        assert response.status_code == 400
        assert "not being streamed" in response.json()["detail"]
        client.post(f"/api/streams/{stream['id']}/stop")

    def test_pause_and_resume(self, client, project_id) -> None:
        stream = self._start(client, project_id)
        await_stream_records(client, stream["id"])

        assert client.post(f"/api/streams/{stream['id']}/pause").json()["paused"]
        assert client.get(f"/api/streams/{stream['id']}").json()["state"] == "paused"
        assert client.post(f"/api/streams/{stream['id']}/resume").json()["resumed"]
        assert client.get(f"/api/streams/{stream['id']}").json()["state"] == "running"
        client.post(f"/api/streams/{stream['id']}/stop")

    def test_stopping_waits_for_the_destinations(self, client, project_id, tmp_path) -> None:
        """A caller told "stopped" should be able to read the file at once."""
        path = tmp_path / "stream.jsonl"
        stream = self._start(
            client, project_id, destinations=[f"file://{path}"], rates={"employee": "500/s"}
        )
        await_stream_records(client, stream["id"])

        stopped = client.post(f"/api/streams/{stream['id']}/stop").json()
        assert stopped["stopped"]
        assert stopped["state"] in ("stopped", "completed", "finished")
        assert path.exists() and path.read_text(encoding="utf-8").strip()

    def test_listing_and_filtering(self, client, project_id) -> None:
        stream = self._start(client, project_id)
        assert any(row["id"] == stream["id"] for row in client.get("/api/streams").json())
        filtered = client.get(f"/api/streams?project_id={project_id}").json()
        assert any(row["id"] == stream["id"] for row in filtered)
        assert client.get("/api/streams?project_id=9999").json() == []
        client.post(f"/api/streams/{stream['id']}/stop")

    def test_forgetting_stops_it_first(self, client, project_id) -> None:
        stream = self._start(client, project_id)
        assert client.delete(f"/api/streams/{stream['id']}").json()["forgotten"]
        assert client.get(f"/api/streams/{stream['id']}").status_code == 404

    def test_unknown_streams_are_404(self, client) -> None:
        assert client.get("/api/streams/nope").status_code == 404
        assert client.get("/api/streams/nope/records").status_code == 404
        assert client.post("/api/streams/nope/pause").status_code == 404
        assert (
            client.post("/api/streams/nope/retarget", json={"entity": "e", "rate": "1/s"})
        ).status_code == 404

    def test_the_feed_pushes_status(self, client, project_id) -> None:
        stream = self._start(client, project_id, rates={"employee": "300/s"})
        with client.websocket_connect(f"/api/streams/{stream['id']}/feed") as socket:
            first = socket.receive_json()
            assert first["kind"] == "stream.status"
            assert first["id"] == stream["id"]
            assert "attainment" in first["stats"]
        client.post(f"/api/streams/{stream['id']}/stop")

    def test_the_feed_says_so_when_there_is_no_stream(self, client) -> None:
        with client.websocket_connect("/api/streams/nope/feed") as socket:
            assert socket.receive_json()["kind"] == "error"

    def test_the_system_route_counts_streams(self, client, project_id) -> None:
        stream = self._start(client, project_id)
        system = client.get("/api/system").json()
        assert system["streams"] >= 1
        assert stream["id"] in system["active_streams"]
        client.post(f"/api/streams/{stream['id']}/stop")

    def test_a_bounded_stream_finishes_on_its_own(self, client, project_id) -> None:
        stream = self._start(
            client, project_id, rates={"employee": "500/s"}, max_records=40, keep_records=100
        )
        import time

        for _ in range(200):
            status = client.get(f"/api/streams/{stream['id']}").json()
            if status["state"] not in ("queued", "running"):
                break
            time.sleep(0.05)
        assert status["stats"]["generated"] >= 40
        assert status["state"] not in ("queued", "running")


class TestAdapterKinds:
    """`/api/providers` says what each adapter is for.

    The Studio used to caption its adapter list "Image and speech adapters
    arrive in the multimodal phase" — while listing invokeai, piper and
    openai_speech directly underneath. The caption was two phases stale and
    read as a missing feature. Grouping them needs the kinds, so the API
    reports them.
    """

    def test_every_adapter_has_a_kind(self, service) -> None:
        with TestClient(create_app(service=service)) as client:
            payload = client.get("/api/providers").json()
        assert set(payload["kinds"]) == set(payload["adapters"])

    def test_the_media_adapters_are_labelled_as_such(self, service) -> None:
        with TestClient(create_app(service=service)) as client:
            kinds = client.get("/api/providers").json()["kinds"]
        assert kinds["invokeai"] == "image"
        assert kinds["procedural_image"] == "image"
        assert kinds["piper"] == "speech"
        assert kinds["openai_speech"] == "speech"
        assert kinds["ollama"] == "language_model"

    def test_the_kind_comes_from_the_interface_not_a_label(self) -> None:
        """So an adapter cannot be registered under the wrong heading."""
        from cacophony.providers.base import ImageProvider
        from cacophony.providers.registry import PROVIDER_REGISTRY

        kinds = PROVIDER_REGISTRY.adapter_kinds()
        for name, kind in kinds.items():
            adapter = PROVIDER_REGISTRY.adapter_class(name)
            if kind == "image":
                assert issubclass(adapter, ImageProvider)

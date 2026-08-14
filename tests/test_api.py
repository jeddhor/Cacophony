"""The REST API and live feed (design document sections 36, 51, 55).

Section 36's routes are driven through FastAPI's test client; the WebSocket
feed is exercised through :meth:`RunService.stream`, which is the logic the
socket route wraps. Starlette's test client deadlocks on a socket that closes
as fast as a small run finishes, so testing the route that way would be
testing the test client.
"""

from __future__ import annotations

import asyncio
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

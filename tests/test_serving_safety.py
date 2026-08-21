"""What the API refuses (design document sections 36, 63, 74).

`cacophony serve` on loopback is as powerful as the shell that started it: it
registers projects by path, rewrites their schemas, and writes runs where the
caller says. That is the right description of a local tool, and it is a very
different proposition on a port other machines can reach — where "the shell that
started it" is somebody else's shell.

These tests fix the boundary: what a confined server refuses, and what the CLI
refuses to start.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from cacophony.api.service import RunService
from cacophony.cli.main import app
from cacophony.core.errors import PathNotAllowedError
from cacophony.core.files import atomic_write_text

runner = CliRunner()

PROJECT = """
project:
  name: Confined
  seed: 1
entities:
  thing:
    count: 2
    fields:
      id: {type: integer, generator: sequence}
"""


@pytest.fixture
def inside(tmp_path: Path) -> Path:
    root = tmp_path / "allowed"
    root.mkdir()
    path = root / "project.yaml"
    path.write_text(PROJECT, encoding="utf-8")
    return path


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    root = tmp_path / "elsewhere"
    root.mkdir()
    path = root / "secret.yaml"
    path.write_text(PROJECT, encoding="utf-8")
    return path


def service_for(root: Path, store: Path) -> RunService:
    return RunService(store_path=store, allowed_roots=[root])


class TestPathConfinement:
    def test_a_project_inside_the_roots_registers(self, inside: Path, tmp_path: Path) -> None:
        service = service_for(inside.parent, tmp_path / "store.db")
        assert service.register_project(path=str(inside))["id"]

    def test_a_project_outside_them_is_refused(
        self, inside: Path, outside: Path, tmp_path: Path
    ) -> None:
        service = service_for(inside.parent, tmp_path / "store.db")
        with pytest.raises(PathNotAllowedError, match="outside the directories"):
            service.register_project(path=str(outside))

    def test_traversal_does_not_help(self, inside: Path, outside: Path, tmp_path: Path) -> None:
        """Resolved before it is checked, so `..` lands where it really points."""
        service = service_for(inside.parent, tmp_path / "store.db")
        sideways = inside.parent / ".." / "elsewhere" / "secret.yaml"
        with pytest.raises(PathNotAllowedError):
            service.register_project(path=str(sideways))

    def test_a_symlink_does_not_help_either(
        self, inside: Path, outside: Path, tmp_path: Path
    ) -> None:
        link = inside.parent / "innocent.yaml"
        link.symlink_to(outside)
        service = service_for(inside.parent, tmp_path / "store.db")
        with pytest.raises(PathNotAllowedError):
            service.register_project(path=str(link))

    def test_an_unconfined_service_still_takes_anything(
        self, outside: Path, tmp_path: Path
    ) -> None:
        """Loopback keeps the behaviour it always had; confinement is opt-in."""
        service = RunService(store_path=tmp_path / "store.db")
        assert service.register_project(path=str(outside))["id"]

    def test_a_run_cannot_write_outside_the_roots(self, inside: Path, tmp_path: Path) -> None:
        """The sharp edge: output_dir is chosen by the caller."""
        import asyncio

        from cacophony.runs.config import RunConfig

        service = service_for(inside.parent, tmp_path / "store.db")
        project_id = service.register_project(path=str(inside))["id"]
        config = RunConfig(output_dir=tmp_path / "elsewhere" / "loot", record_history=False)

        with pytest.raises(PathNotAllowedError, match="output directory"):
            asyncio.run(service.start_run(project_id, config))

    def test_the_schema_cannot_be_rewritten_from_outside(
        self, inside: Path, outside: Path, tmp_path: Path
    ) -> None:
        """A project registered before confinement must not stay writable."""
        loose = RunService(store_path=tmp_path / "store.db")
        project_id = loose.register_project(path=str(outside))["id"]

        confined = RunService(database=loose.database, allowed_roots=[inside.parent])
        with pytest.raises(PathNotAllowedError):
            confined.write_schema(project_id, PROJECT.replace("Confined", "Rewritten"))
        assert "Rewritten" not in outside.read_text(encoding="utf-8")


class TestTheHolesAnAuditFound:
    """Four caller-named paths that never reached :meth:`permitted`.

    Each of these was reachable through a confined server, and each is the same
    mistake: a path arrives in a request, something is opened at it, and the
    check that every other path goes through was not on that route.
    """

    def test_inline_source_cannot_smuggle_a_path_past_the_check(
        self, inside: Path, outside: Path, tmp_path: Path
    ) -> None:
        """`path` alone was refused; `path` *and* `source` stored it unchecked.

        The stored path is what every later read uses, so this was a way to
        read any file that parses as a project through a confined server.
        """
        service = service_for(inside.parent, tmp_path / "store.db")
        with pytest.raises(PathNotAllowedError, match="project"):
            service.register_project(path=str(outside), source=PROJECT)

    def test_a_project_stored_with_an_outside_path_cannot_be_read_back(
        self, inside: Path, outside: Path, tmp_path: Path
    ) -> None:
        """Reads are re-checked, exactly as writes already were."""
        loose = RunService(store_path=tmp_path / "store.db")
        project_id = loose.register_project(path=str(outside))["id"]

        confined = RunService(database=loose.database, allowed_roots=[inside.parent])
        with pytest.raises(PathNotAllowedError):
            confined.schema_source(project_id)
        with pytest.raises(PathNotAllowedError):
            confined.load_for_run(project_id)
        # And it is not offered as editable either.
        assert confined.schema_is_editable(project_id) is False

    def test_the_provider_cache_cannot_be_written_outside_the_roots(
        self, inside: Path, tmp_path: Path
    ) -> None:
        import asyncio

        from cacophony.providers.cache import CacheMode
        from cacophony.runs.config import RunConfig

        service = service_for(inside.parent, tmp_path / "store.db")
        project_id = service.register_project(path=str(inside))["id"]
        config = RunConfig(
            output_dir=inside.parent / "out",
            cache_mode=CacheMode.READ_WRITE,
            cache_path=tmp_path / "elsewhere" / "cache.db",
            record_history=False,
        )

        with pytest.raises(PathNotAllowedError, match="cache"):
            asyncio.run(service.start_run(project_id, config))

    def test_a_stream_cannot_append_records_outside_the_roots(
        self, inside: Path, tmp_path: Path
    ) -> None:
        """The one caller-named path with a writer on the end of it."""
        fastapi = pytest.importorskip("fastapi")
        assert fastapi
        from fastapi.testclient import TestClient

        from cacophony.api.app import create_app

        service = service_for(inside.parent, tmp_path / "store.db")
        project_id = service.register_project(path=str(inside))["id"]
        target = tmp_path / "elsewhere" / "stream.jsonl"

        with TestClient(create_app(service=service)) as client:
            response = client.post(
                f"/api/projects/{project_id}/streams",
                json={
                    "rates": {"thing": "20/s"},
                    "destinations": [f"file://{target}"],
                    "duration_seconds": 0.2,
                },
            )

        assert response.status_code == 403, response.text
        assert "stream destination" in response.text
        assert not target.exists()

    def test_a_stream_inside_the_roots_still_writes(self, inside: Path, tmp_path: Path) -> None:
        """The check must refuse the destination, not the feature."""
        import time

        pytest.importorskip("fastapi")
        from fastapi.testclient import TestClient

        from cacophony.api.app import create_app

        service = service_for(inside.parent, tmp_path / "store.db")
        project_id = service.register_project(path=str(inside))["id"]
        target = inside.parent / "stream.jsonl"

        with TestClient(create_app(service=service)) as client:
            response = client.post(
                f"/api/projects/{project_id}/streams",
                json={
                    "rates": {"thing": "20/s"},
                    "destinations": [f"file://{target}"],
                    "duration_seconds": 0.3,
                },
            )
            assert response.status_code == 201, response.text
            stream_id = response.json()["id"]
            for _ in range(100):
                if client.get(f"/api/streams/{stream_id}").json()["state"] in (
                    "completed",
                    "stopped",
                    "failed",
                ):
                    break
                time.sleep(0.05)

        assert target.exists() and target.stat().st_size > 0

    def test_a_destination_given_as_a_mapping_is_checked_too(
        self, inside: Path, tmp_path: Path
    ) -> None:
        """`file://x` and `{"type": "file", "path": "x"}` are the same request."""
        pytest.importorskip("fastapi")
        from fastapi.testclient import TestClient

        from cacophony.api.app import create_app

        service = service_for(inside.parent, tmp_path / "store.db")
        project_id = service.register_project(path=str(inside))["id"]
        target = tmp_path / "elsewhere" / "mapped.jsonl"

        with TestClient(create_app(service=service)) as client:
            response = client.post(
                f"/api/projects/{project_id}/streams",
                json={
                    "rates": {"thing": "20/s"},
                    "destinations": [{"type": "file", "path": str(target)}],
                    "duration_seconds": 0.2,
                },
            )

        assert response.status_code == 403, response.text
        assert not target.exists()


class TestASchemaIsNotAWayIn:
    """What a *schema* can make a confined server read (sections 8, 23, 36).

    Every check before this one was on a path a request named. A schema names
    paths too - a lookup table, a document template - and those arrive as data,
    reach the filesystem through the compiler, and pass no route on the way. A
    confined server was reading any file they named.
    """

    def _project(self, path: Path) -> str:
        return f"""
project: {{name: Lookup, seed: 1}}
entities:
  thing:
    count: 3
    fields:
      value: {{type: string, generator: lookup, path: "{path}"}}
"""

    def _client(self, inside: Path, tmp_path: Path):
        pytest.importorskip("fastapi")
        from fastapi.testclient import TestClient

        from cacophony.api.app import create_app

        service = service_for(inside.parent, tmp_path / "store.db")
        return TestClient(create_app(service=service))

    def test_a_lookup_table_outside_the_roots_is_refused(
        self, inside: Path, tmp_path: Path
    ) -> None:
        secret = tmp_path / "elsewhere" / "secret.txt"
        secret.parent.mkdir(exist_ok=True)
        secret.write_text("sentinel\n", encoding="utf-8")

        with self._client(inside, tmp_path) as client:
            project_id = client.post(
                "/api/projects", json={"source": self._project(secret)}
            ).json()["id"]
            response = client.post(
                f"/api/projects/{project_id}/preview", json={"entity": "thing", "count": 3}
            )

        assert response.status_code == 403, response.text
        assert "sentinel" not in response.text

    def test_a_lookup_table_inside_them_still_works(self, inside: Path, tmp_path: Path) -> None:
        table = inside.parent / "cities.txt"
        table.write_text("Austin\nBerlin\n", encoding="utf-8")

        with self._client(inside, tmp_path) as client:
            project_id = client.post("/api/projects", json={"source": self._project(table)}).json()[
                "id"
            ]
            response = client.post(
                f"/api/projects/{project_id}/preview", json={"entity": "thing", "count": 3}
            )

        assert response.status_code == 200, response.text
        assert "Austin" in response.text or "Berlin" in response.text

    def test_a_symlink_inside_the_roots_does_not_help(self, inside: Path, tmp_path: Path) -> None:
        secret = tmp_path / "elsewhere" / "secret.txt"
        secret.parent.mkdir(exist_ok=True)
        secret.write_text("sentinel\n", encoding="utf-8")
        link = inside.parent / "innocent.txt"
        link.symlink_to(secret)

        with self._client(inside, tmp_path) as client:
            project_id = client.post("/api/projects", json={"source": self._project(link)}).json()[
                "id"
            ]
            response = client.post(
                f"/api/projects/{project_id}/preview", json={"entity": "thing", "count": 3}
            )

        assert response.status_code == 403, response.text
        assert "sentinel" not in response.text

    def test_a_document_template_is_checked_the_same_way(
        self, inside: Path, tmp_path: Path
    ) -> None:
        template = tmp_path / "elsewhere" / "letter.txt"
        template.parent.mkdir(exist_ok=True)
        template.write_text("Dear {value}, sentinel.", encoding="utf-8")
        source = f"""
project: {{name: Docs, seed: 1}}
entities:
  letter:
    count: 2
    fields:
      value: {{type: string, generator: constant, value: Ada}}
      body:
        type: string
        generator: document
        format: txt
        template_path: "{template}"
"""
        with self._client(inside, tmp_path) as client:
            project_id = client.post("/api/projects", json={"source": source}).json()["id"]
            response = client.post(
                f"/api/projects/{project_id}/preview", json={"entity": "letter", "count": 1}
            )

        assert response.status_code == 403, response.text
        assert "sentinel" not in response.text

    def test_the_command_line_is_not_confined_by_any_of_this(self, tmp_path: Path) -> None:
        """The shell can read what the shell can read; only a server is confined."""
        import yaml

        from cacophony.schema.compiler import compile_project
        from cacophony.schema.loader import load_project_data

        table = tmp_path / "cities.txt"
        table.write_text("Austin\n", encoding="utf-8")
        compiled = compile_project(load_project_data(yaml.safe_load(self._project(table))))
        assert compiled.entities["thing"].fields[0].generator.describe().startswith("lookup")


class TestStoredAssetsAreRecheckedNow:
    """A run recorded before confinement is not a key to what it wrote."""

    def _run_with_assets(self, tmp_path: Path) -> tuple[RunService, str]:
        """A completed run, recorded by a server with no confinement at all."""
        import asyncio

        from cacophony.runs.config import RunConfig

        project = tmp_path / "elsewhere" / "docs.yaml"
        project.parent.mkdir(exist_ok=True)
        project.write_text(
            """
project: {name: Docs, seed: 1}
entities:
  letter:
    count: 2
    fields:
      name: {type: string, generator: constant, value: Ada}
      body: {type: string, generator: document, format: txt, template: "Dear {name}."}
""",
            encoding="utf-8",
        )
        loose = RunService(store_path=tmp_path / "store.db")
        project_id = loose.register_project(path=str(project))["id"]

        async def go() -> str:
            started = await loose.start_run(
                project_id,
                RunConfig(output_dir=tmp_path / "elsewhere" / "out", record_history=True),
            )
            run_id = str(started["id"])
            await loose.wait(run_id, timeout=30)
            return run_id

        return loose, asyncio.run(go())

    def test_a_confined_server_will_not_list_assets_outside_its_roots(
        self, inside: Path, tmp_path: Path
    ) -> None:
        pytest.importorskip("fastapi")
        from fastapi.testclient import TestClient

        from cacophony.api.app import create_app

        loose, run_id = self._run_with_assets(tmp_path)
        confined = RunService(database=loose.database, allowed_roots=[inside.parent])

        with TestClient(create_app(service=confined)) as client:
            listing = client.get(f"/api/runs/{run_id}/assets")

        assert listing.status_code == 403, listing.text


class TestTheContextTravels:
    """The policy has to survive the hop into a run's background task."""

    def test_a_task_created_under_a_policy_keeps_it(self) -> None:
        import asyncio

        from cacophony.core.paths import active_policy, confined_to

        async def scenario() -> object:
            with confined_to(["/tmp"]):
                task = asyncio.create_task(_seen())
            # The block has exited by the time the task runs; the task copied
            # the context when it was created, so it still sees the policy.
            return await task

        async def _seen() -> object:
            await asyncio.sleep(0)
            return active_policy()

        assert asyncio.run(scenario()) is not None


class TestAtomicWrites:
    def test_the_content_arrives(self, tmp_path: Path) -> None:
        target = tmp_path / "schema.yaml"
        atomic_write_text(target, "hello")
        assert target.read_text(encoding="utf-8") == "hello"

    def test_no_temporary_file_is_left_behind(self, tmp_path: Path) -> None:
        target = tmp_path / "schema.yaml"
        atomic_write_text(target, "hello")
        assert [child.name for child in tmp_path.iterdir()] == ["schema.yaml"]

    def test_a_failed_write_leaves_the_original(self, tmp_path: Path) -> None:
        """The point of the exercise: a crash costs the write, not the file."""
        from cacophony.core.errors import OutputError

        target = tmp_path / "schema.yaml"
        target.write_text("original", encoding="utf-8")
        with pytest.raises(OutputError):
            atomic_write_text(target / "impossible", "new")
        assert target.read_text(encoding="utf-8") == "original"


class TestTheCliRefusesToExposeItself:
    def test_loopback_needs_nothing(self) -> None:
        """Unchanged: the common case must not grow ceremony."""
        from cacophony.cli.main import _is_loopback

        assert _is_loopback("127.0.0.1")
        assert _is_loopback("localhost")
        assert _is_loopback("::1")

    def test_every_interface_is_not_loopback(self) -> None:
        from cacophony.cli.main import _is_loopback

        assert not _is_loopback("0.0.0.0")
        assert not _is_loopback("::")
        assert not _is_loopback("192.168.1.10")

    def test_a_name_it_cannot_parse_is_treated_as_reachable(self) -> None:
        """Guessing that somebody's hostname is private is not a good guess."""
        from cacophony.cli.main import _is_loopback

        assert not _is_loopback("my-laptop.local")

    def test_serving_off_loopback_without_a_token_exits_two(self, monkeypatch) -> None:
        monkeypatch.delenv("CACOPHONY_TOKEN", raising=False)
        result = runner.invoke(app, ["serve", "--host", "0.0.0.0", "--port", "8199"])
        assert result.exit_code == 2
        assert "refusing to serve" in result.stderr
        assert "CACOPHONY_TOKEN" in result.stderr
        assert "--insecure" in result.stderr

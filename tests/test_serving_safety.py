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

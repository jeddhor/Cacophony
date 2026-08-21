"""Shared test fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from cacophony.schema.loader import load_project
from cacophony.schema.models import ProjectSpec
from helpers import EXAMPLES, TEMPLATES


def pytest_configure(config: pytest.Config) -> None:
    """Refuse to run on a deprecated test transport, where that is fair.

    Starlette 1.6 deprecates driving its TestClient with httpx and 2.x will
    drop it; `httpx2` is a dev dependency so the suite uses what the next
    Starlette will hand it, and any *other* Starlette deprecation becomes an
    error we find rather than a warning nobody reads.

    Two escapes, both deliberate. The filter names a class, so it lives here
    rather than in `pyproject.toml`: importing it would fail for anyone running
    the suite without the API extra, who is entitled to skip those tests rather
    than to a configuration error. And an environment that simply predates the
    dependency is told to update it rather than failed at collection.
    """
    try:
        from starlette.exceptions import StarletteDeprecationWarning
    except ImportError:  # pragma: no cover - the API extra is optional
        return

    try:
        import httpx2  # noqa: F401
    except ImportError:  # pragma: no cover - a venv older than the dependency
        # Escalating here would turn "your virtualenv predates a dependency"
        # into a collection error with no advice in it.
        config.issue_config_time_warning(
            pytest.PytestConfigWarning(
                "starlette will drive its TestClient with httpx2; this environment has "
                "only httpx. Run: pip install -e '.[dev]'"
            ),
            stacklevel=2,
        )
        return

    config.addinivalue_line(
        "filterwarnings",
        f"error::{StarletteDeprecationWarning.__module__}.StarletteDeprecationWarning",
    )


@pytest.fixture
def templates_dir() -> Path:
    return TEMPLATES


def _shipped_schemas() -> list[Path]:
    return sorted(
        [*TEMPLATES.glob("*.yaml"), *EXAMPLES.glob("*.yaml")],
        key=lambda path: path.name,
    )


@pytest.fixture(params=_shipped_schemas(), ids=lambda path: path.name)
def template_path(request: pytest.FixtureRequest) -> Path:
    """Every shipped template and example, one per test invocation."""
    return request.param


@pytest.fixture
def corporate_project() -> ProjectSpec:
    return load_project(TEMPLATES / "corporate-directory.yaml")

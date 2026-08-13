"""Shared test fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from cacophony.schema.loader import load_project
from cacophony.schema.models import ProjectSpec
from helpers import EXAMPLES, TEMPLATES


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

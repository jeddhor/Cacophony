"""The REST API (design document sections 36 and 55).

FastAPI over the same objects the CLI uses, so starting a run from a terminal
and starting one from HTTP take exactly the same path through the Conductor.

FastAPI is an optional dependency: a local CLI user should not have to install
a web framework to generate a CSV. Import the app factory only when serving::

    from cacophony.api import create_app
    app = create_app(store_path=".cacophony/cacophony.db")
"""

from .service import RunService

__all__ = ["RunService", "create_app"]


def create_app(**kwargs: object) -> object:
    """Build the FastAPI application, reporting a missing install clearly."""
    try:
        from .app import create_app as _create_app
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise ImportError(
            "The Cacophony API needs FastAPI and uvicorn. Install them with: "
            "pip install 'cacophony[api]'"
        ) from exc
    return _create_app(**kwargs)  # type: ignore[arg-type]

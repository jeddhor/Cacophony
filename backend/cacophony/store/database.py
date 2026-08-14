"""Metadata database connection management (design document section 42).

    "Use SQLite initially."

One file, no server, no configuration. It lives beside the project by default,
in ``.cacophony/cacophony.db``, so a project directory carries its own history
and copying the directory copies the history with it.

On migrations
-------------
Section 39 recommends Alembic, and this will use it - but not yet. Alembic
earns its keep when there are deployed databases to migrate *from*, and every
store in existence today was created by a pre-release version. Until the shape
settles there is a version stamp and an explicit upgrade ladder in
:func:`_migrate`, which does the same job honestly at a hundredth of the
scaffolding. The stamp is checked on every open, so the day a real migration is
needed the store will say so rather than fail obscurely.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from .models import SCHEMA_VERSION, Base

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterator

    from sqlalchemy.engine import Engine

__all__ = ["DEFAULT_STORE_DIR", "Database", "default_store_path"]

#: Conventional directory for per-project Cacophony state.
DEFAULT_STORE_DIR = ".cacophony"
DEFAULT_STORE_NAME = "cacophony.db"


def default_store_path(project_path: str | Path | None = None) -> Path:
    """Where a project's store lives by default: beside the schema file."""
    base = Path(project_path).parent if project_path else Path.cwd()
    return base / DEFAULT_STORE_DIR / DEFAULT_STORE_NAME


class Database:
    """A SQLite metadata store."""

    def __init__(self, path: str | Path | None = None, *, echo: bool = False) -> None:
        #: ``None`` means an in-memory store, used by tests and by
        #: ``--no-history``.
        self.path = Path(path) if path is not None else None
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            url = f"sqlite+pysqlite:///{self.path}"
        else:
            url = "sqlite+pysqlite:///:memory:"

        self.engine: Engine = create_engine(
            url,
            echo=echo,
            future=True,
            # A run's coordinator and the API may touch the store from
            # different threads; every session is short-lived and serialised by
            # SQLite's own locking.
            connect_args={"check_same_thread": False},
        )
        _configure_sqlite(self.engine)
        self._session_factory = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)
        self._create_or_migrate()

    # -- schema ------------------------------------------------------------- #

    def _create_or_migrate(self) -> None:
        Base.metadata.create_all(self.engine)
        with self.engine.begin() as connection:
            connection.execute(
                text("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
            )
            row = connection.execute(text("SELECT version FROM schema_version")).fetchone()
            if row is None:
                connection.execute(
                    text("INSERT INTO schema_version (version) VALUES (:v)"),
                    {"v": SCHEMA_VERSION},
                )
                return
            found = int(row[0])

        if found > SCHEMA_VERSION:
            raise RuntimeError(
                f"{self.path or 'in-memory store'} was written by a newer version of "
                f"Cacophony (store schema {found}, this build understands {SCHEMA_VERSION}). "
                "Upgrade Cacophony, or point --store at a different file."
            )
        if found < SCHEMA_VERSION:
            _migrate(self.engine, found)

    # -- sessions ----------------------------------------------------------- #

    def session(self) -> Session:
        return self._session_factory()

    @contextmanager
    def transaction(self) -> Iterator[Session]:
        """A session that commits on success and rolls back on failure."""
        session = self.session()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def close(self) -> None:
        self.engine.dispose()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def describe(self) -> dict[str, Any]:
        return {
            "path": str(self.path) if self.path else ":memory:",
            "schema_version": SCHEMA_VERSION,
        }


def _configure_sqlite(engine: Engine) -> None:
    """Pragmas that matter for a store written to during a long run."""

    @event.listens_for(engine, "connect")
    def _on_connect(connection: Any, _record: Any) -> None:  # pragma: no cover - driver hook
        cursor = connection.cursor()
        # WAL lets the API read progress while the coordinator is writing it.
        cursor.execute("PRAGMA journal_mode=WAL")
        # NORMAL trades a vanishingly small durability window for not calling
        # fsync on every checkpoint of a multi-hour run.
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()


def _migrate(engine: Engine, from_version: int) -> None:
    """Upgrade an older store in place.

    Each step is a plain function from version *n* to *n+1*. There are none
    yet, because version 1 is the first shape that has existed.
    """
    steps: dict[int, Any] = {}
    version = from_version
    while version < SCHEMA_VERSION:
        step = steps.get(version)
        if step is None:
            raise RuntimeError(
                f"No migration from store schema {version} to {version + 1}. "
                "Delete the store to start fresh, or use the version of Cacophony "
                "that created it."
            )
        step(engine)
        version += 1

    with engine.begin() as connection:
        connection.execute(text("UPDATE schema_version SET version = :v"), {"v": SCHEMA_VERSION})

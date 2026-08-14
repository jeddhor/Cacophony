"""The project metadata database (design document section 42).

Bookkeeping only: projects, the exact schema revision each run used, runs,
jobs, checkpoints, structured events and statistics.

Generated datasets are **not** stored here. Section 42 says so, and it is the
right call: a metadata database that also held ten million generated rows would
be slow to query, awkward to back up, and would make the one thing this store
is for - answering "what happened, and can I resume it?" - the slowest question
it could be asked.
"""

from .database import DEFAULT_STORE_DIR, Database, default_store_path
from .models import (
    SCHEMA_VERSION,
    Base,
    Job,
    Project,
    Run,
    RunEvent,
    RunStatistic,
    SchemaRevision,
    summarise_schema,
)
from .repository import Repository

__all__ = [
    "DEFAULT_STORE_DIR",
    "SCHEMA_VERSION",
    "Base",
    "Database",
    "Job",
    "Project",
    "Repository",
    "Run",
    "RunEvent",
    "RunStatistic",
    "SchemaRevision",
    "default_store_path",
    "summarise_schema",
]

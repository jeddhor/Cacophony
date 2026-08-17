"""Request and response bodies for the REST API (design document section 36)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from ..core.provenance import ProvenanceMode
from ..providers.cache import CacheMode
from ..runs.config import ResourceLimits, RunConfig

__all__ = [
    "CreateProjectRequest",
    "CreateRunRequest",
    "CreateStreamRequest",
    "PatchSchemaRequest",
    "PreviewRequest",
    "ProviderTestResponse",
    "RetargetRequest",
    "SchemaOperation",
    "WriteSchemaRequest",
]


class CreateProjectRequest(BaseModel):
    """Register a project, by path or by inline schema text."""

    path: str | None = None
    source: str | None = Field(
        default=None, description="Schema text, YAML or JSON, when there is no file."
    )

    @model_validator(mode="after")
    def _one_of(self) -> CreateProjectRequest:
        if not self.path and not self.source:
            raise ValueError("provide either 'path' or 'source'")
        return self


class LimitsRequest(BaseModel):
    """Section 64's resource controls, as an API body."""

    max_workers: int = Field(default=4, ge=1, le=64)
    batch_size: int = Field(default=1000, ge=1, le=1_000_000)
    llm_batch_size: int = Field(default=20, ge=1, le=500)
    min_free_disk_mb: int = Field(default=512, ge=0)
    memory_ceiling_mb: int | None = Field(default=None, ge=0)
    request_timeout_seconds: float = Field(default=120.0, gt=0)
    max_retries: int = Field(default=3, ge=1, le=10)

    def to_limits(self) -> ResourceLimits:
        return ResourceLimits(**self.model_dump())


class CreateRunRequest(BaseModel):
    """``POST /api/projects/{id}/runs``."""

    output_dir: str = "out"
    #: Validated against the writer registry rather than pinned to a literal,
    #: so a newly registered format is offered here the moment it exists.
    output_format: str = "jsonl"
    entities: list[str] = Field(default_factory=list)
    records: int | None = Field(default=None, ge=0)
    seed: int | None = None

    validate_records: bool = Field(default=True, alias="validate")
    drop_invalid: bool = False
    provenance: ProvenanceMode = ProvenanceMode.NONE
    failure_policy: Literal["abort", "retry", "skip", "placeholder", "incomplete"] = "abort"

    cache_mode: CacheMode = CacheMode.DISABLED
    cache_path: str | None = None

    checkpoint_every: int = Field(default=10_000, ge=1)
    limits: LimitsRequest = Field(default_factory=LimitsRequest)

    model_config = {"populate_by_name": True}

    @field_validator("output_format")
    @classmethod
    def _known_format(cls, value: str) -> str:
        from ..outputs import OUTPUT_FORMATS

        if value.lower() not in OUTPUT_FORMATS:
            known = ", ".join(sorted(OUTPUT_FORMATS))
            raise ValueError(f"unknown output format '{value}'. Available: {known}")
        return value.lower()

    def to_config(self) -> RunConfig:
        return RunConfig(
            output_dir=Path(self.output_dir),
            output_format=self.output_format,
            entities=list(self.entities),
            records=self.records,
            seed=self.seed,
            validate=self.validate_records,
            drop_invalid=self.drop_invalid,
            provenance=self.provenance,
            failure_policy=self.failure_policy,
            cache_mode=self.cache_mode,
            cache_path=Path(self.cache_path) if self.cache_path else None,
            checkpoint_every=self.checkpoint_every,
            limits=self.limits.to_limits(),
        )


class PreviewRequest(BaseModel):
    """``POST /api/projects/{id}/preview`` - section 51's sample records."""

    entity: str | None = None
    count: int = Field(default=10, ge=1, le=500)
    offset: int = Field(default=0, ge=0)
    seed: int | None = None
    isolate: bool = False


class CreateStreamRequest(BaseModel):
    """``POST /api/projects/{id}/streams`` - section 94's workload generator."""

    #: ``{"authentication": "250/s", "alert": "8 per minute"}``. Written the way
    #: people say rates, and parsed by the same parser the CLI uses.
    rates: dict[str, str | float] = Field(min_length=1)
    #: ``"syslog://host:514"``, ``"https://host/ingest"``, or a mapping of
    #: options. Omit for a stream that is only sampled in the browser.
    destinations: list[str | dict[str, Any]] = Field(default_factory=list)
    #: How many recent records to keep for the Studio to display. Zero makes
    #: this a pure workload generator with nothing held in memory.
    keep_records: int = Field(default=200, ge=0, le=5_000)

    batch_size: int = Field(default=100, ge=1, le=100_000)
    flush_seconds: float = Field(default=1.0, gt=0, le=60)
    duration_seconds: float | None = Field(default=None, gt=0)
    max_records: int | None = Field(default=None, ge=1)
    start_index: int = Field(default=0, ge=0)
    live_time: bool = True
    follow_shape: bool = False
    scenario_cycle_seconds: float = Field(default=3600.0, gt=0)
    on_error: Literal["continue", "abort"] = "continue"
    seed: int | None = None

    def to_options(self) -> dict[str, Any]:
        """Everything :class:`~cacophony.live.stream.StreamConfig` takes."""
        return {
            "batch_size": self.batch_size,
            "flush_seconds": self.flush_seconds,
            "duration_seconds": self.duration_seconds,
            "max_records": self.max_records,
            "start_index": self.start_index,
            "live_time": self.live_time,
            "follow_shape": self.follow_shape,
            "scenario_cycle_seconds": self.scenario_cycle_seconds,
            "on_error": self.on_error,
        }


class RetargetRequest(BaseModel):
    """``POST /api/streams/{id}/retarget`` - change a rate while it runs."""

    entity: str = Field(min_length=1)
    rate: str | float


class ProviderTestResponse(BaseModel):
    """``POST /api/providers/{id}/test``."""

    id: str
    healthy: bool
    message: str
    latency_ms: float | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class SchemaOperation(BaseModel):
    """One targeted edit to a schema document (design document section 48)."""

    op: str
    entity: str | None = None
    field: str | None = None
    key: str | None = None
    value: Any = None
    name: str | None = None
    index: int | None = None


class PatchSchemaRequest(BaseModel):
    """``PATCH /api/projects/{id}/schema`` - applied as a single transaction."""

    operations: list[SchemaOperation] = Field(min_length=1)


class WriteSchemaRequest(BaseModel):
    """``PUT /api/projects/{id}/schema`` - replace the document wholesale."""

    source: str = Field(min_length=1)

"""Request and response bodies for the REST API (design document section 36)."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from ..core.provenance import ProvenanceMode
from ..providers.cache import CacheMode
from ..runs.config import ResourceLimits, RunConfig
from ..schema.models import OutputProfileSpec

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
    #: A layout declared under ``outputs:`` in the project (section 34). What
    #: it sets - format, directory, entities, partitioning - applies unless
    #: this request named the same thing itself, exactly as ``--output-profile``
    #: behaves on the command line.
    output_profile: str = ""
    entities: list[str] = Field(default_factory=list)
    records: int | None = Field(default=None, ge=0)
    seed: int | None = None

    #: Per-entity counts, the API's half of `-n employee=5000`.
    record_counts: dict[str, int] = Field(default_factory=dict)

    validate_records: bool = Field(default=True, alias="validate")
    drop_invalid: bool = False
    provenance: ProvenanceMode = ProvenanceMode.NONE
    failure_policy: Literal["abort", "retry", "skip", "placeholder", "incomplete", "report"] = (
        "abort"
    )

    #: Section 79's deliberate awkwardness, which the API could not ask for.
    edge_cases: float = Field(default=0.0, ge=0.0, le=1.0)
    edge_categories: list[str] = Field(default_factory=list)

    #: Where generated media goes, and whether to pay for it again.
    assets_dir: str | None = None
    overwrite_assets: bool = False

    #: Recording this run in the store. Off is the API's `--no-history`.
    record_history: bool = True

    cache_mode: CacheMode = CacheMode.DISABLED
    cache_path: str | None = None

    checkpoint_every: int = Field(default=10_000, ge=1)
    #: Replace an earlier run's output at this destination. A run refuses to
    #: share one by default, because two datasets in a directory look like one.
    overwrite: bool = False
    limits: LimitsRequest = Field(default_factory=LimitsRequest)

    # Unknown fields are refused rather than ignored: a misspelled option that
    # silently does nothing is the most expensive kind of typo, because the run
    # succeeds and the setting was never applied.
    model_config = {"populate_by_name": True, "extra": "forbid"}

    @field_validator("edge_categories")
    @classmethod
    def _known_categories(cls, value: list[str]) -> list[str]:
        from ..simulation.edges import CATEGORIES

        unknown = [name for name in value if name not in CATEGORIES]
        if unknown:
            known = ", ".join(sorted(CATEGORIES))
            raise ValueError(f"unknown edge-case category {', '.join(unknown)}. Available: {known}")
        return value

    @field_validator("output_format")
    @classmethod
    def _known_format(cls, value: str) -> str:
        from ..outputs import OUTPUT_FORMATS

        if value.lower() not in OUTPUT_FORMATS:
            known = ", ".join(sorted(OUTPUT_FORMATS))
            raise ValueError(f"unknown output format '{value}'. Available: {known}")
        return value.lower()

    def to_config(self, profiles: Mapping[str, OutputProfileSpec] | None = None) -> RunConfig:
        """The run this request describes, with any named profile applied.

        ``profiles`` is the project's ``outputs:`` block. An explicit field in
        the request beats the profile and the profile beats the default, so
        naming a profile and a directory writes the profile's layout where the
        caller said - the precedence ``generate --output-profile`` uses.
        """
        profile = self._profile(profiles)
        stated = self.model_fields_set

        config = RunConfig(
            output_dir=Path(
                self.output_dir if "output_dir" in stated or profile is None else profile.path
            ),
            output_format=(
                self.output_format
                if "output_format" in stated or profile is None
                else profile.format
            ),
            entities=list(self.entities),
            records=self.records,
            record_counts=dict(self.record_counts),
            edge_cases=self.edge_cases,
            edge_categories=list(self.edge_categories),
            assets_dir=Path(self.assets_dir) if self.assets_dir else None,
            overwrite_assets=self.overwrite_assets,
            record_history=self.record_history,
            seed=self.seed,
            validate=self.validate_records,
            drop_invalid=self.drop_invalid,
            provenance=self.provenance,
            failure_policy=self.failure_policy,
            cache_mode=self.cache_mode,
            cache_path=Path(self.cache_path) if self.cache_path else None,
            checkpoint_every=self.checkpoint_every,
            overwrite=self.overwrite,
            limits=self.limits.to_limits(),
        )

        if profile is not None:
            config.output_profile = profile.name or self.output_profile
            config.partition_by = list(profile.partition_by)
            config.output_options = dict(profile.options)
            if not config.entities:
                config.entities = list(profile.entities)
        return config

    def _profile(
        self, profiles: Mapping[str, OutputProfileSpec] | None
    ) -> OutputProfileSpec | None:
        if not self.output_profile:
            return None
        declared = profiles or {}
        profile = declared.get(self.output_profile)
        if profile is None:
            known = ", ".join(sorted(declared)) or "none are declared"
            raise ValueError(
                f"no output profile '{self.output_profile}'. Declared under 'outputs:': {known}"
            )
        return profile


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

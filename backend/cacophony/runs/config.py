"""Run configuration and resource controls (design document sections 30, 64, 65).

Section 64 is blunt about why this exists: "Generation can consume substantial
computing resources." A synthetic-data tool that fills a disk or exhausts a
machine's memory in the small hours is worse than one that refuses to start.

So the limits are checked *before* a run begins where that is possible - free
disk space against the estimate - and enforced during it where it is not.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.provenance import ProvenanceMode
from ..providers.cache import CacheMode

__all__ = ["ResourceLimits", "RunConfig"]


@dataclass(slots=True)
class ResourceLimits:
    """Ceilings a run must respect (design document section 64)."""

    #: Entities generated concurrently. Deterministic generation is CPU-bound
    #: and single-threaded per entity, so this is about overlapping entities,
    #: not about splitting one.
    max_workers: int = 4
    #: Records held between writes. The real memory ceiling (section 31).
    batch_size: int = 1_000
    #: Values one unique field may hold in memory before the check spills to a
    #: disk-backed index (section 31). The check stays exact either way; this
    #: decides where the memory goes.
    unique_memory_values: int = 250_000
    #: Records per language-model call in batch mode.
    llm_batch_size: int = 20
    #: Refuse to start if the destination has less than this much space free.
    min_free_disk_mb: int = 512
    #: Advisory ceiling, reported in the run summary rather than enforced;
    #: enforcing it would mean killing a run mid-batch, which loses less work
    #: to checkpoint than to abort.
    memory_ceiling_mb: int | None = None
    #: Seconds before a provider request is abandoned.
    request_timeout_seconds: float = 120.0
    #: Section 66: never permit infinite retry loops.
    max_retries: int = 3

    def below_floor(self, destination: Path | str) -> bool:
        """Whether free space is under `min_free_disk_mb`, which is a refusal.

        Distinct from the estimate-based warning beside it: one is a measured
        fact about the disk, the other is an order-of-magnitude guess about the
        run, and only the first is worth stopping for.
        """
        import shutil as _shutil

        target = Path(destination)
        while not target.exists() and target.parent != target:
            target = target.parent
        try:
            usage = _shutil.disk_usage(target)
        except OSError:
            return False
        return usage.free / (1024 * 1024) < self.min_free_disk_mb

    def check_disk(self, destination: Path, *, estimated_bytes: int = 0) -> str | None:
        """Return a complaint if the destination cannot hold the run.

        Checked before the first record rather than after the last, because a
        disk that fills at 90% of a nine-hour run has wasted eight hours.
        """
        target = destination
        while not target.exists() and target.parent != target:
            target = target.parent
        try:
            usage = shutil.disk_usage(target)
        except OSError:
            return None

        free_mb = usage.free / (1024 * 1024)
        if free_mb < self.min_free_disk_mb:
            return (
                f"{destination} has {free_mb:,.0f} MB free, below the "
                f"{self.min_free_disk_mb:,} MB floor."
            )
        if estimated_bytes and usage.free < estimated_bytes:
            return (
                f"{destination} has {free_mb:,.0f} MB free but the run is estimated to "
                f"need about {estimated_bytes / (1024 * 1024):,.0f} MB. Estimates are "
                "approximate (section 69), so this is a warning worth heeding rather "
                "than a certainty."
            )
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_workers": self.max_workers,
            "batch_size": self.batch_size,
            "llm_batch_size": self.llm_batch_size,
            "min_free_disk_mb": self.min_free_disk_mb,
            "memory_ceiling_mb": self.memory_ceiling_mb,
            "request_timeout_seconds": self.request_timeout_seconds,
            "max_retries": self.max_retries,
        }


@dataclass(slots=True)
class RunConfig:
    """Everything that decides what a run does, and how."""

    output_dir: Path = Path("out")
    output_format: str = "jsonl"
    entities: list[str] = field(default_factory=list)
    #: The ``outputs:`` profile this configuration came from, recorded so a run
    #: says which layout it wrote (design document section 34).
    output_profile: str = ""
    #: Columns whose values become directories: ``year=2026/month=03/``. Empty
    #: means one file per entity, which is the ordinary case.
    partition_by: list[str] = field(default_factory=list)
    #: Format-specific writer options from the profile, e.g. Parquet's
    #: ``compression``.
    output_options: dict[str, Any] = field(default_factory=dict)
    #: Override every entity's declared count.
    records: int | None = None
    #: Override one entity's count, which ``--records ticket=100000`` sets. Takes
    #: precedence over the blunt form above, so the two can be combined.
    record_counts: dict[str, int] = field(default_factory=dict)
    seed: int | None = None

    validate: bool = True
    drop_invalid: bool = False
    #: Fraction of records given a legal-but-awkward value (section 79). Zero
    #: is off. Distinct from chaos: an edge case is valid data an application
    #: should handle, not invalid data it should reject.
    edge_cases: float = 0.0
    #: Which categories of edge case to draw from. Empty means all of them.
    edge_categories: list[str] = field(default_factory=list)
    provenance: ProvenanceMode = ProvenanceMode.NONE
    failure_policy: str = "abort"

    cache_mode: CacheMode = CacheMode.DISABLED
    cache_path: Path | None = None

    limits: ResourceLimits = field(default_factory=ResourceLimits)

    #: Write a checkpoint every this many records (design document section 32).
    #: Small enough that a crash costs seconds of work, large enough that the
    #: store is not the bottleneck.
    checkpoint_every: int = 10_000
    #: Where generated media goes (sections 19, 81). Defaults to an ``assets``
    #: directory beside the data, so a run produces one self-contained folder.
    assets_dir: Path | None = None
    #: Store identical files once. Placeholder portraits are common enough that
    #: this is worth doing by default.
    deduplicate_assets: bool = True
    #: Regenerate media that is already on disk. Off by default: a resumed run
    #: that repeats a thousand diffusion calls has learned nothing.
    overwrite_assets: bool = False

    #: Persist run history at all.
    record_history: bool = True
    #: Keep at most this many runs per store; older ones are pruned.
    history_limit: int = 100
    #: Replace an earlier run's output at this destination instead of refusing
    #: to share it. Off by default: mixing two datasets in one directory is
    #: silent, and the mixture looks exactly like a dataset.
    overwrite: bool = False

    def anchor_paths(self) -> None:
        """Resolve every path this run owns, once, before it is recorded.

        A relative path means "here", and a resumed run is often started
        somewhere else - so `out/` in a stored configuration is an instruction
        whose meaning depends on who reads it. Resolved at the start of the
        run, it means the same directory to everyone afterwards.
        """
        self.output_dir = Path(self.output_dir).expanduser().resolve()
        if self.assets_dir is not None:
            self.assets_dir = Path(self.assets_dir).expanduser().resolve()
        if self.cache_path is not None:
            self.cache_path = Path(self.cache_path).expanduser().resolve()

    def to_dict(self) -> dict[str, Any]:
        return {
            "output_dir": str(self.output_dir),
            "output_format": self.output_format,
            "output_profile": self.output_profile,
            "partition_by": list(self.partition_by),
            "output_options": dict(self.output_options),
            "entities": list(self.entities),
            "records": self.records,
            "record_counts": dict(self.record_counts),
            "seed": self.seed,
            "validate": self.validate,
            "drop_invalid": self.drop_invalid,
            "edge_cases": self.edge_cases,
            "edge_categories": list(self.edge_categories),
            "provenance": self.provenance.value,
            "failure_policy": self.failure_policy,
            "cache_mode": self.cache_mode.value,
            "cache_path": str(self.cache_path) if self.cache_path else None,
            "checkpoint_every": self.checkpoint_every,
            "assets_dir": str(self.assets_dir) if self.assets_dir else None,
            "deduplicate_assets": self.deduplicate_assets,
            "overwrite_assets": self.overwrite_assets,
            "overwrite": self.overwrite,
            "limits": self.limits.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunConfig:
        """Rebuild a config from a stored run, so a resume repeats it exactly."""
        limits_data = data.get("limits") or {}
        return cls(
            output_dir=Path(data.get("output_dir", "out")),
            output_format=data.get("output_format", "jsonl"),
            output_profile=data.get("output_profile", ""),
            partition_by=list(data.get("partition_by") or []),
            output_options=dict(data.get("output_options") or {}),
            entities=list(data.get("entities") or []),
            records=data.get("records"),
            record_counts={
                str(name): int(count) for name, count in (data.get("record_counts") or {}).items()
            },
            seed=data.get("seed"),
            validate=bool(data.get("validate", True)),
            drop_invalid=bool(data.get("drop_invalid", False)),
            edge_cases=float(data.get("edge_cases", 0.0)),
            edge_categories=list(data.get("edge_categories") or []),
            provenance=ProvenanceMode(data.get("provenance", "none")),
            failure_policy=data.get("failure_policy", "abort"),
            cache_mode=CacheMode(data.get("cache_mode", "disabled")),
            cache_path=Path(data["cache_path"]) if data.get("cache_path") else None,
            assets_dir=Path(data["assets_dir"]) if data.get("assets_dir") else None,
            deduplicate_assets=bool(data.get("deduplicate_assets", True)),
            overwrite_assets=bool(data.get("overwrite_assets", False)),
            overwrite=bool(data.get("overwrite", False)),
            checkpoint_every=int(data.get("checkpoint_every", 10_000)),
            limits=ResourceLimits(**limits_data) if limits_data else ResourceLimits(),
        )

    @property
    def asset_root(self) -> Path:
        """Where media goes: beside the data unless told otherwise."""
        return self.assets_dir if self.assets_dir is not None else self.output_dir / "assets"

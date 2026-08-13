"""Generated-data provenance (design document section 60).

Provenance answers "why was this value produced?" (section 4, *Inspectable*).
It is configurable because recording it for every field of a ten-million-record
run can cost more storage than the dataset itself.

Modes, in increasing order of cost::

    none        record nothing
    run         one provenance block per run
    record      one provenance block per record
    field       per-field generator attribution
    full        per-field attribution plus prompts, seeds and raw responses
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

__all__ = ["FieldProvenance", "ProvenanceMode", "RecordProvenance"]


class ProvenanceMode(StrEnum):
    NONE = "none"
    RUN = "run"
    RECORD = "record"
    FIELD = "field"
    FULL = "full"

    @property
    def tracks_records(self) -> bool:
        return self in {ProvenanceMode.RECORD, ProvenanceMode.FIELD, ProvenanceMode.FULL}

    @property
    def tracks_fields(self) -> bool:
        return self in {ProvenanceMode.FIELD, ProvenanceMode.FULL}

    @property
    def tracks_payloads(self) -> bool:
        """Whether prompts and raw provider responses are retained.

        These may contain sensitive user-supplied content (section 87), so
        ``full`` must always be an explicit opt-in.
        """
        return self is ProvenanceMode.FULL


@dataclass(slots=True)
class FieldProvenance:
    """How a single field's value came to exist."""

    generator: str
    seed: int | None = None
    provider: str | None = None
    model: str | None = None
    prompt_version: int | None = None
    prompt: str | None = None
    raw_response: str | None = None
    attempts: int = 1
    cached: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self, *, include_payloads: bool = False) -> dict[str, Any]:
        data: dict[str, Any] = {"generator": self.generator}
        for key in ("seed", "provider", "model", "prompt_version"):
            value = getattr(self, key)
            if value is not None:
                data[key] = value
        if self.attempts != 1:
            data["attempts"] = self.attempts
        if self.cached:
            data["cached"] = True
        if include_payloads:
            if self.prompt is not None:
                data["prompt"] = self.prompt
            if self.raw_response is not None:
                data["raw_response"] = self.raw_response
        data.update(self.extra)
        return data


@dataclass(slots=True)
class RecordProvenance:
    """Provenance attached to a whole record."""

    entity: str
    record_index: int
    seed: int | None = None
    run_id: str | None = None
    schema_version: int | None = None
    fields: dict[str, FieldProvenance] = field(default_factory=dict)

    def to_dict(self, mode: ProvenanceMode = ProvenanceMode.RECORD) -> dict[str, Any]:
        if mode in (ProvenanceMode.NONE, ProvenanceMode.RUN):
            return {}
        data: dict[str, Any] = {"entity": self.entity, "record_index": self.record_index}
        for key in ("seed", "run_id", "schema_version"):
            value = getattr(self, key)
            if value is not None:
                data[key] = value
        if mode.tracks_fields and self.fields:
            data["fields"] = {
                name: prov.to_dict(include_payloads=mode.tracks_payloads)
                for name, prov in self.fields.items()
            }
        return data

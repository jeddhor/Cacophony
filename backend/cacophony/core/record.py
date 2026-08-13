"""The provider-neutral record representation (design document section 99).

Before it reaches any output writer, every generated record exists as a
:class:`GeneratedRecord`: plain Python values plus optional assets and
provenance. Writers translate that single representation into CSV rows,
Parquet columns, SQL inserts or files on disk - they never see generator
internals.
"""

from __future__ import annotations

# 'field' is aliased because these dataclasses have an attribute named 'field',
# which would otherwise shadow the dataclasses helper inside the class body.
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

from .provenance import ProvenanceMode, RecordProvenance

__all__ = ["GeneratedAsset", "GeneratedRecord", "to_jsonable"]


@dataclass(slots=True)
class GeneratedAsset:
    """A derived artifact belonging to a record (design document section 81).

    One employee record may own a portrait, an ID badge, a voicemail and a
    signature. Assets always reference their parent so the asset manager can
    answer "what belongs to E48291?" without scanning the dataset.
    """

    field: str
    kind: str
    path: Path
    media_type: str | None = None
    size_bytes: int | None = None
    metadata: dict[str, Any] = dataclass_field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "field": self.field,
            "kind": self.kind,
            "path": str(self.path),
        }
        if self.media_type:
            data["media_type"] = self.media_type
        if self.size_bytes is not None:
            data["size_bytes"] = self.size_bytes
        if self.metadata:
            data["metadata"] = self.metadata
        return data


@dataclass(slots=True)
class GeneratedRecord:
    """One generated record in Cacophony's internal representation."""

    entity: str
    id: str | None = None
    values: dict[str, Any] = dataclass_field(default_factory=dict)
    assets: list[GeneratedAsset] = dataclass_field(default_factory=list)
    provenance: RecordProvenance | None = None

    def get(self, name: str, default: Any = None) -> Any:
        return self.values.get(name, default)

    def __getitem__(self, name: str) -> Any:
        return self.values[name]

    def __setitem__(self, name: str, value: Any) -> None:
        self.values[name] = value

    def __contains__(self, name: str) -> bool:
        return name in self.values

    def to_dict(
        self,
        *,
        provenance_mode: ProvenanceMode = ProvenanceMode.NONE,
        include_assets: bool = True,
        jsonable: bool = False,
    ) -> dict[str, Any]:
        """Flatten into a plain dictionary suitable for serialisation.

        Metadata keys are prefixed with ``_`` so they cannot collide with a
        user-defined field named ``assets`` or ``provenance``.
        """
        data: dict[str, Any] = dict(self.values)
        if jsonable:
            data = {key: to_jsonable(value) for key, value in data.items()}
        if include_assets and self.assets:
            data["_assets"] = [asset.to_dict() for asset in self.assets]
        if self.provenance is not None and provenance_mode.tracks_records:
            block = self.provenance.to_dict(provenance_mode)
            if block:
                data["_provenance"] = block
        return data


def to_jsonable(value: Any) -> Any:
    """Convert a generated value into something ``json.dumps`` accepts.

    Kept here rather than in each writer so that CSV, JSON, JSONL and any
    future writer agree on how a ``datetime`` is spelled.
    """
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        import base64

        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(item) for item in value]
    return str(value)

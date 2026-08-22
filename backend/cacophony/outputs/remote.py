"""Outputs that are not files (design document section 33).

Section 33's "Later" list names two destinations that are not a path on this
machine: a search index and object storage. They are here for the same reason
the database writers are - a dataset people cannot load is a dataset nobody
uses - but they are a different shape from every other writer, and the two
differences are worth stating before the code.

**A search index has no file to count.** So the Elasticsearch writer gives every
document a deterministic id derived from the entity and the record's position.
Re-sending a document overwrites it, which makes a resumed or re-run job
*idempotent* rather than duplicating: the index converges on the dataset
whatever happened on the way.

**Object storage is not a filesystem.** So the object writer does not pretend:
it writes the ordinary local file with the ordinary local writer, and uploads
it when it closes. The dataset exists on disk and in the bucket, resume behaves
exactly as it does locally, and nothing in the run has to learn about
multi-part uploads to write a hundred rows.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..core.errors import OutputError
from ..core.interfaces import OutputWriter
from ..core.record import to_jsonable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from ..core.record import GeneratedRecord

__all__ = ["ElasticsearchWriter", "ObjectStoreWriter"]


class ElasticsearchWriter(OutputWriter):
    """Index records through the bulk API (section 33).

    Speaks to Elasticsearch and OpenSearch alike: the bulk endpoint and the
    action line are the same in both, and this uses nothing either of them has
    since diverged over.

    Options:
        ``url``       the cluster, e.g. ``http://localhost:9200``. Required.
        ``index``     the index name. Defaults to the entity's name.
        ``api_key``   sent as ``Authorization: ApiKey …``
        ``username`` / ``password``   basic authentication instead
        ``pipeline``  an ingest pipeline to run documents through
        ``bulk_size`` documents per request. Default 500.
    """

    format = "elasticsearch"
    extension = ""
    appendable = True

    def __init__(self, path: str | Path, **options: Any) -> None:
        url = options.get("url") or options.get("base_url")
        if not url:
            raise OutputError(
                "an elasticsearch output needs a 'url' - the cluster to index into. "
                "Set it in the output profile's options, or use a file format."
            )
        # Kept so the run has something to name this destination by; nothing
        # is written there.
        self.path = Path(path)
        self.url = str(url).rstrip("/")
        entity = options.get("entity")
        self.index = str(options.get("index") or getattr(entity, "name", None) or self.path.stem)
        self.pipeline = options.get("pipeline")
        self.bulk_size = max(1, int(options.get("bulk_size", 500)))
        self.timeout = float(options.get("timeout_seconds", 30.0))
        self.api_key = options.get("api_key")
        self.username = options.get("username")
        self.password = options.get("password")
        self.records_written = 0
        self._client: Any = None
        self._bytes = 0
        #: Only used for a record with neither a key nor provenance, which is
        #: a dataset nothing can address anyway.
        self._counter = 0

    async def open(self) -> None:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - httpx is a core dependency
            raise OutputError("indexing needs httpx, which should already be installed") from exc

        headers = {"content-type": "application/x-ndjson"}
        if self.api_key:
            headers["authorization"] = f"ApiKey {self.api_key}"
        auth = (str(self.username), str(self.password)) if self.username and self.password else None
        self._client = httpx.AsyncClient(timeout=self.timeout, headers=headers, auth=auth)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def write_batch(self, records: Sequence[GeneratedRecord]) -> None:
        if not records:
            return
        if self._client is None:
            await self.open()
        assert self._client is not None

        for start in range(0, len(records), self.bulk_size):
            chunk = records[start : start + self.bulk_size]
            await self._bulk(chunk)
        self.records_written += len(records)

    @property
    def bytes_written(self) -> int:
        """Bytes sent to the cluster. There is no file to measure instead."""
        return self._bytes

    def describe(self) -> str:
        return f"elasticsearch:{self.url}/{self.index}"

    # -- internals ----------------------------------------------------------- #

    async def _bulk(self, records: Sequence[GeneratedRecord]) -> None:
        import httpx

        lines: list[str] = []
        for record in records:
            action: dict[str, Any] = {"_index": self.index, "_id": self._document_id(record)}
            if self.pipeline:
                action["pipeline"] = self.pipeline
            lines.append(json.dumps({"index": action}, default=str))
            lines.append(json.dumps(record.to_dict(jsonable=True), default=str))
        payload = ("\n".join(lines) + "\n").encode("utf-8")

        try:
            response = await self._client.post(f"{self.url}/_bulk", content=payload)
        except httpx.HTTPError as exc:
            raise OutputError(f"could not reach {self.url}: {exc}") from exc

        if response.status_code >= 400:
            raise OutputError(
                f"{self.url} refused the batch: HTTP {response.status_code} {response.text[:200]}"
            )

        # A 200 with `errors: true` is a partial failure, and a writer that
        # treated it as success would report a dataset the index does not hold.
        body = response.json() if response.content else {}
        if body.get("errors"):
            first = next(
                (
                    item["index"]["error"]
                    for item in body.get("items", [])
                    if isinstance(item, dict) and item.get("index", {}).get("error")
                ),
                "unknown",
            )
            raise OutputError(f"{self.url} rejected documents: {first}")

        self._bytes += len(payload)

    def _document_id(self, record: GeneratedRecord) -> str:
        """A stable id, so re-running converges rather than duplicating.

        The record's own id where it has one - which is its primary key - and
        its position where it does not. Both are properties of *where the
        record is*, never of when it was produced, so the same run indexes the
        same documents however it was interrupted (section 75).
        """
        if record.id:
            return f"{record.entity}-{record.id}"
        position = (record.provenance.record_index if record.provenance else None) or 0
        self._counter += 1
        return f"{record.entity}-{position or self._counter}"


class ObjectStoreWriter(OutputWriter):
    """Write the ordinary file, then put it in a bucket (section 33).

    Wraps a real writer rather than replacing one: the format's own writer
    produces the file, and this uploads it on close. Everything a local run can
    do - resume, parts, partitioning, byte accounting - keeps working, because
    locally that is exactly what this is.

    Options:
        ``bucket``      the bucket. Required.
        ``prefix``      key prefix inside it.
        ``format``      the file format to write. Default ``jsonl``.
        ``endpoint_url`` for S3-compatible storage (MinIO, R2, Ceph).
        ``region``      passed to the client.
        ``keep_local``  leave the file behind as well. Default true.
    """

    format = "s3"
    extension = ""
    appendable = False

    def __init__(self, path: str | Path, **options: Any) -> None:
        bucket = options.pop("bucket", None)
        if not bucket:
            raise OutputError(
                "an object-storage output needs a 'bucket'. Set it in the output profile's options."
            )
        self.bucket = str(bucket)
        self.prefix = str(options.pop("prefix", "") or "").strip("/")
        self.endpoint_url = options.pop("endpoint_url", None)
        self.region = options.pop("region", None)
        self.keep_local = bool(options.pop("keep_local", True))
        #: Injectable, so the local half can be tested without a network and
        #: without a mock of somebody else's SDK.
        self._upload = options.pop("uploader", None)

        inner_format = str(options.pop("format", "jsonl"))
        from . import OUTPUT_FORMATS

        writer_class = OUTPUT_FORMATS.get(inner_format.lower())
        if writer_class is None or writer_class is ObjectStoreWriter:
            known = ", ".join(sorted(name for name in OUTPUT_FORMATS if name != "s3"))
            raise OutputError(f"unknown format '{inner_format}' for object storage. Try: {known}")

        self.path = Path(path).with_suffix(writer_class.extension)
        self._inner: OutputWriter = writer_class(self.path, **options)
        self.uploaded: list[str] = []

    async def open(self) -> None:
        await self._inner.open()

    async def write_batch(self, records: Sequence[GeneratedRecord]) -> None:
        await self._inner.write_batch(records)

    async def close(self) -> None:
        await self._inner.close()
        self._put()

    @property
    def records_written(self) -> int:
        return int(getattr(self._inner, "records_written", 0))

    @property
    def bytes_written(self) -> int:
        return self._inner.bytes_written

    def describe(self) -> str:
        return f"s3://{self.bucket}/{self.key()}"

    def key(self) -> str:
        name = Path(getattr(self._inner, "path", self.path)).name
        return f"{self.prefix}/{name}" if self.prefix else name

    # -- internals ----------------------------------------------------------- #

    def _put(self) -> None:
        source = Path(getattr(self._inner, "path", self.path))
        if not source.exists():
            return

        upload = self._upload or self._boto_uploader()
        try:
            upload(source, self.bucket, self.key())
        except Exception as exc:  # pragma: no cover - network specific
            raise OutputError(f"could not upload {source} to {self.bucket}: {exc}") from exc

        self.uploaded.append(self.key())
        if not self.keep_local:
            source.unlink(missing_ok=True)

    def _boto_uploader(self) -> Any:
        try:
            import boto3
        except ImportError as exc:  # pragma: no cover - depends on the extra
            raise OutputError("object storage needs boto3: pip install 'cacophony[s3]'") from exc

        client = boto3.client(
            "s3",
            endpoint_url=self.endpoint_url,
            **({"region_name": self.region} if self.region else {}),
        )

        def upload(source: Path, bucket: str, key: str) -> None:
            client.upload_file(str(source), bucket, key)

        return upload


def _jsonable(value: Any) -> Any:  # pragma: no cover - re-exported for symmetry
    return to_jsonable(value)

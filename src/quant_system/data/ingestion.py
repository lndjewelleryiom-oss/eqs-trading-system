from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .catalog import DatasetCatalog
from .contracts import DatasetManifest
from .raw_store import ImmutableRawStore, StoredBlob


class DatasetConnector(Protocol):
    source_name: str

    def fetch(self) -> bytes: ...


@dataclass(frozen=True, slots=True)
class IngestionReceipt:
    raw_blob: StoredBlob
    manifest_fingerprint: str


def ingest_payload(
    connector: DatasetConnector,
    *,
    raw_store: ImmutableRawStore,
    catalog: DatasetCatalog,
    dataset_id: str,
    schema_version: str,
    timezone_name: str = "UTC",
    latency_class: str = "unknown",
    revision_policy: str = "append-revisions",
) -> IngestionReceipt:
    payload = connector.fetch()
    if not isinstance(payload, bytes):
        raise TypeError("connector.fetch() must return bytes")
    blob = raw_store.put(payload)
    manifest = DatasetManifest.create(
        dataset_id=dataset_id,
        source=connector.source_name,
        schema_version=schema_version,
        timezone_name=timezone_name,
        latency_class=latency_class,
        revision_policy=revision_policy,
        content_hashes=(blob.content_hash,),
    )
    fingerprint = catalog.put(manifest)
    return IngestionReceipt(blob, fingerprint)

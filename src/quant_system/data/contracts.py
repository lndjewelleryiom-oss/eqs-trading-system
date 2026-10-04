from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Iterable


@dataclass(frozen=True, slots=True)
class DatasetManifest:
    dataset_id: str
    source: str
    schema_version: str
    timezone_name: str
    latency_class: str
    revision_policy: str
    content_hashes: tuple[str, ...]
    created_at: datetime

    @staticmethod
    def create(
        *, dataset_id: str, source: str, schema_version: str,
        timezone_name: str = "UTC", latency_class: str = "unknown",
        revision_policy: str = "append-revisions", content_hashes: Iterable[str] = (),
    ) -> "DatasetManifest":
        return DatasetManifest(
            dataset_id=dataset_id,
            source=source,
            schema_version=schema_version,
            timezone_name=timezone_name,
            latency_class=latency_class,
            revision_policy=revision_policy,
            content_hashes=tuple(content_hashes),
            created_at=datetime.now(timezone.utc),
        )

    def fingerprint(self) -> str:
        payload = {
            "dataset_id": self.dataset_id,
            "source": self.source,
            "schema_version": self.schema_version,
            "timezone_name": self.timezone_name,
            "latency_class": self.latency_class,
            "revision_policy": self.revision_policy,
            "content_hashes": self.content_hashes,
        }
        return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

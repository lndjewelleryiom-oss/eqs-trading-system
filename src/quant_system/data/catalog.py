from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from pathlib import Path
import json

from .contracts import DatasetManifest


class DatasetCatalog:
    """Immutable on-disk manifest catalog keyed by dataset fingerprint."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, manifest: DatasetManifest) -> str:
        fingerprint = manifest.fingerprint()
        path = self.root / f"{fingerprint}.json"
        payload = {
            "dataset_id": manifest.dataset_id,
            "source": manifest.source,
            "schema_version": manifest.schema_version,
            "timezone_name": manifest.timezone_name,
            "latency_class": manifest.latency_class,
            "revision_policy": manifest.revision_policy,
            "content_hashes": list(manifest.content_hashes),
            "created_at": manifest.created_at.isoformat(),
            "fingerprint": fingerprint,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        if path.exists():
            if path.read_text() != encoded:
                raise RuntimeError("manifest fingerprint collision or catalog tampering")
        else:
            path.write_text(encoded)
        return fingerprint

    def get(self, fingerprint: str) -> DatasetManifest:
        path = self.root / f"{fingerprint}.json"
        payload = json.loads(path.read_text())
        manifest = DatasetManifest(
            dataset_id=payload["dataset_id"],
            source=payload["source"],
            schema_version=payload["schema_version"],
            timezone_name=payload["timezone_name"],
            latency_class=payload["latency_class"],
            revision_policy=payload["revision_policy"],
            content_hashes=tuple(payload["content_hashes"]),
            created_at=datetime.fromisoformat(payload["created_at"]),
        )
        if manifest.fingerprint() != fingerprint or payload.get("fingerprint") != fingerprint:
            raise RuntimeError("manifest integrity verification failed")
        return manifest

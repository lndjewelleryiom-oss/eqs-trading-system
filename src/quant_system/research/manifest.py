from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping


def _normalise(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _normalise(v) for k, v in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple)):
        return [_normalise(v) for v in value]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("manifest values must be finite")
        return value
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    raise TypeError(f"unsupported manifest value: {type(value).__name__}")


@dataclass(frozen=True, slots=True)
class ExperimentManifest:
    """Immutable description of everything required to reproduce an experiment.

    ``created_at`` is audit metadata and is deliberately excluded from the content
    fingerprint. Identical experiment inputs therefore receive the same
    ``experiment_id`` even when recorded at different wall-clock times.
    """

    strategy_id: str
    hypothesis: str
    data_fingerprints: Mapping[str, str]
    code_version: str
    parameters: Mapping[str, Any]
    cost_assumptions: Mapping[str, Any]
    split_spec: Mapping[str, Any]
    random_seed: int
    software_versions: Mapping[str, str] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        if not self.strategy_id.strip():
            raise ValueError("strategy_id is required")
        if not self.hypothesis.strip():
            raise ValueError("hypothesis is required")
        if not self.data_fingerprints:
            raise ValueError("at least one data fingerprint is required")
        if not self.code_version.strip():
            raise ValueError("code_version is required")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        # Fail early on values that cannot be canonically represented.
        self.canonical_payload()

    def canonical_payload(self) -> dict[str, Any]:
        return _normalise({
            "strategy_id": self.strategy_id,
            "hypothesis": self.hypothesis,
            "data_fingerprints": self.data_fingerprints,
            "code_version": self.code_version,
            "parameters": self.parameters,
            "cost_assumptions": self.cost_assumptions,
            "split_spec": self.split_spec,
            "random_seed": self.random_seed,
            "software_versions": self.software_versions,
        })

    @property
    def fingerprint(self) -> str:
        encoded = json.dumps(
            self.canonical_payload(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @property
    def experiment_id(self) -> str:
        return f"EXP-{self.fingerprint[:16]}"

    def to_record(self) -> dict[str, Any]:
        return {
            "experiment_id": self.experiment_id,
            "fingerprint": self.fingerprint,
            "created_at": self.created_at.isoformat(),
            "manifest": self.canonical_payload(),
        }

    def write(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps(self.to_record(), sort_keys=True, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    @classmethod
    def read(cls, path: str | Path) -> "ExperimentManifest":
        record = json.loads(Path(path).read_text(encoding="utf-8"))
        payload = record["manifest"]
        manifest = cls(
            strategy_id=payload["strategy_id"],
            hypothesis=payload["hypothesis"],
            data_fingerprints=payload["data_fingerprints"],
            code_version=payload["code_version"],
            parameters=payload["parameters"],
            cost_assumptions=payload["cost_assumptions"],
            split_spec=payload["split_spec"],
            random_seed=int(payload["random_seed"]),
            software_versions=payload.get("software_versions", {}),
            created_at=datetime.fromisoformat(record["created_at"]),
        )
        if manifest.fingerprint != record["fingerprint"]:
            raise ValueError("experiment manifest fingerprint mismatch")
        if manifest.experiment_id != record["experiment_id"]:
            raise ValueError("experiment manifest id mismatch")
        return manifest

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
import json
from pathlib import Path


class DatasetConsumer(StrEnum):
    FEATURE_ENGINE = "feature_engine"
    REPLAY = "replay"
    PAPER = "paper"
    SHADOW = "shadow"
    TERMINAL = "terminal"
    ALPHA = "alpha"
    OOS = "oos"
    LIVE = "live"
    BROKER = "broker"


_ALLOWED = frozenset({
    DatasetConsumer.FEATURE_ENGINE,
    DatasetConsumer.REPLAY,
    DatasetConsumer.PAPER,
    DatasetConsumer.SHADOW,
    DatasetConsumer.TERMINAL,
})
class InfrastructureDatasetGateError(PermissionError):
    pass


@dataclass(frozen=True, slots=True)
class InfrastructureDatasetBoundary:
    path: Path
    fingerprint: str
    payload: dict[str, object]

    @classmethod
    def load(cls, path: str | Path) -> "InfrastructureDatasetBoundary":
        manifest_path = Path(path)
        raw = manifest_path.read_bytes()
        payload = json.loads(raw)
        if payload.get("schema_version") != "eqs-infrastructure-only-dataset-manifest-v1":
            raise InfrastructureDatasetGateError("unsupported infrastructure manifest schema")
        policy = payload.get("policy", {})
        if policy.get("strict_pit_alpha_oos_admitted") is not False:
            raise InfrastructureDatasetGateError("manifest is not fail-closed for strict PIT")
        if policy.get("locked_oos_read") is not False or policy.get("broker_submission_enabled") is not False:
            raise InfrastructureDatasetGateError("manifest safety policy is not closed")
        return cls(manifest_path, sha256(raw).hexdigest(), payload)

    def authorize(self, consumer: DatasetConsumer | str) -> None:
        try:
            requested = DatasetConsumer(consumer)
        except ValueError as exc:
            raise InfrastructureDatasetGateError(f"unknown dataset consumer: {consumer}") from exc
        if requested not in _ALLOWED:
            raise InfrastructureDatasetGateError(
                f"{requested.value} cannot consume infrastructure-only dataset {self.fingerprint}"
            )
    def admitted_record(self, venue: str, record_id: str) -> dict[str, object]:
        admitted = self.payload.get("admitted_records", {})
        for record in admitted.get(venue.upper(), []):
            if record.get("record_id") == record_id:
                return record
        raise InfrastructureDatasetGateError(
            f"record is not admitted by infrastructure manifest: {venue}:{record_id}"
        )

    @property
    def explicit_exclusions(self) -> tuple[dict[str, object], ...]:
        return tuple(self.payload.get("explicit_exclusions", []))

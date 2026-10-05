from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from hashlib import sha256
import json
import re
from typing import Any, Mapping

_SHA = re.compile(r"^[a-f0-9]{64}$")
SUPPORTED_ASSET_CLASSES = frozenset({"EQUITIES_ETFS", "FX", "GOLD_SPOT_XAUUSD", "RATES_FIXED_INCOME"})


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        default=lambda item: item.isoformat() if isinstance(item, datetime) else str(item),
    ).encode("utf-8")


def _hash(value: object) -> str:
    return sha256(_canonical(value)).hexdigest()


def _aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class HistoricalPartitionDescriptor:
    partition_id: str
    asset_class: str
    source_id: str
    instrument_id: str
    data_kind: str
    period_start: datetime
    period_end: datetime
    earliest_available_at: datetime
    latest_available_at: datetime
    row_count: int
    content_sha256: str
    schema_version: str
    retrieval_sha256: str

    def __post_init__(self) -> None:
        if self.asset_class not in SUPPORTED_ASSET_CLASSES:
            raise ValueError("unsupported asset_class")
        for name in ("partition_id", "source_id", "instrument_id", "data_kind", "schema_version"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} is required")
        for name in ("period_start", "period_end", "earliest_available_at", "latest_available_at"):
            _aware(getattr(self, name), name)
        if self.period_end < self.period_start:
            raise ValueError("partition period invalid")
        if self.latest_available_at < self.earliest_available_at:
            raise ValueError("availability bounds invalid")
        if self.row_count <= 0:
            raise ValueError("row_count must be positive")
        if not _SHA.fullmatch(self.content_sha256) or not _SHA.fullmatch(self.retrieval_sha256):
            raise ValueError("partition hashes must be SHA-256")

    @property
    def fingerprint(self) -> str:
        return _hash(asdict(self))


@dataclass(frozen=True, slots=True)
class HistoricalResearchArchiveManifest:
    dataset_id: str
    asset_class: str
    source_id: str
    format_version: str
    decision_time: datetime
    historical_start: datetime
    historical_end: datetime
    partitions: tuple[HistoricalPartitionDescriptor, ...]
    universe_fingerprint: str
    availability_policy: str
    revision_policy: str
    gap_policy: str
    corporate_action_policy: str | None
    survivorship_safe_universe: bool
    historical_pit_authority: bool
    fabricated_volume: bool
    broker_submission_enabled: bool = False
    live_authority: bool = False

    def __post_init__(self) -> None:
        if self.asset_class not in SUPPORTED_ASSET_CLASSES:
            raise ValueError("unsupported asset_class")
        for name in ("dataset_id", "source_id", "format_version", "availability_policy", "revision_policy", "gap_policy"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} is required")
        for name in ("decision_time", "historical_start", "historical_end"):
            _aware(getattr(self, name), name)
        if self.historical_end < self.historical_start or self.historical_end > self.decision_time:
            raise ValueError("historical bounds must be ordered and not exceed decision time")
        if not self.partitions:
            raise ValueError("archive must contain partitions")
        if any(part.asset_class != self.asset_class or part.source_id != self.source_id for part in self.partitions):
            raise ValueError("partition asset/source binding mismatch")
        if len({part.partition_id for part in self.partitions}) != len(self.partitions):
            raise ValueError("duplicate partition_id")
        if not _SHA.fullmatch(self.universe_fingerprint):
            raise ValueError("universe_fingerprint must be SHA-256")
        if self.broker_submission_enabled or self.live_authority:
            raise ValueError("research archive cannot grant trading authority")
        if self.fabricated_volume:
            raise ValueError("fabricated volume is forbidden")

    @property
    def fingerprint(self) -> str:
        return _hash({
            "dataset_id": self.dataset_id,
            "asset_class": self.asset_class,
            "source_id": self.source_id,
            "format_version": self.format_version,
            "decision_time": self.decision_time.isoformat(),
            "historical_start": self.historical_start.isoformat(),
            "historical_end": self.historical_end.isoformat(),
            "partitions": [asdict(part) for part in sorted(self.partitions, key=lambda p: p.partition_id)],
            "universe_fingerprint": self.universe_fingerprint,
            "availability_policy": self.availability_policy,
            "revision_policy": self.revision_policy,
            "gap_policy": self.gap_policy,
            "corporate_action_policy": self.corporate_action_policy,
            "survivorship_safe_universe": self.survivorship_safe_universe,
            "historical_pit_authority": self.historical_pit_authority,
            "fabricated_volume": self.fabricated_volume,
            "broker_submission_enabled": False,
            "live_authority": False,
        })


@dataclass(frozen=True, slots=True)
class HistoricalArchiveCertification:
    status: str
    asset_class: str
    dataset_id: str
    manifest_fingerprint: str
    checks: tuple[tuple[str, bool], ...]
    blockers: tuple[str, ...]
    training_authorised: bool
    validation_authorised: bool
    locked_oos_authorised: bool
    broker_submission_enabled: bool = False
    live_authority: bool = False

    @property
    def record_sha256(self) -> str:
        return _hash(asdict(self))


def certify_historical_archive(
    manifest: HistoricalResearchArchiveManifest,
    *,
    split_spec: Mapping[str, datetime],
    open_locked_oos: bool = False,
) -> HistoricalArchiveCertification:
    required = {"training_start", "training_end", "validation_start", "validation_end", "oos_start", "oos_end"}
    if set(split_spec) != required:
        raise ValueError("split_spec requires exact train/validation/oos boundaries")
    for name, value in split_spec.items():
        _aware(value, name)
    ordered = [split_spec[k] for k in ("training_start", "training_end", "validation_start", "validation_end", "oos_start", "oos_end")]
    split_order_ok = all(a < b for a, b in zip(ordered, ordered[1:]))

    parts = manifest.partitions
    availability_ok = all(part.latest_available_at <= manifest.decision_time for part in parts)
    coverage_ok = min(part.period_start for part in parts) <= manifest.historical_start and max(part.period_end for part in parts) >= split_spec["validation_end"]
    hashes_ok = all(_SHA.fullmatch(part.content_sha256) and _SHA.fullmatch(part.retrieval_sha256) for part in parts)
    unique_fingerprints = len({part.fingerprint for part in parts}) == len(parts)
    pit_ok = manifest.historical_pit_authority and bool(manifest.availability_policy.strip()) and bool(manifest.revision_policy.strip())
    gap_ok = bool(manifest.gap_policy.strip())
    universe_ok = manifest.survivorship_safe_universe

    asset_checks: dict[str, bool] = {}
    if manifest.asset_class == "EQUITIES_ETFS":
        asset_checks = {
            "corporate_action_policy_present": bool(manifest.corporate_action_policy and manifest.corporate_action_policy.strip()),
            "survivorship_safe_universe": manifest.survivorship_safe_universe,
        }
    elif manifest.asset_class in {"FX", "GOLD_SPOT_XAUUSD"}:
        asset_checks = {
            "no_fabricated_exchange_volume": manifest.fabricated_volume is False,
            "source_gap_policy_present": bool(manifest.gap_policy.strip()),
        }
    elif manifest.asset_class == "RATES_FIXED_INCOME":
        asset_checks = {
            "historical_pit_authority": manifest.historical_pit_authority,
            "revision_policy_present": bool(manifest.revision_policy.strip()),
        }

    checks = {
        "split_order": split_order_ok,
        "partition_availability_within_decision_time": availability_ok,
        "training_validation_coverage": coverage_ok,
        "partition_hashes": hashes_ok,
        "partition_identity_unique": unique_fingerprints,
        "point_in_time_authority": pit_ok,
        "gap_policy": gap_ok,
        "survivorship_safe_universe": universe_ok,
        **asset_checks,
    }
    blockers = tuple(sorted(name.upper() + "_FAILED" for name, passed in checks.items() if not passed))
    eligible = not blockers
    oos_within_archive = manifest.historical_start <= split_spec["oos_start"] and manifest.historical_end >= split_spec["oos_end"]
    oos_authorised = bool(eligible and open_locked_oos and oos_within_archive)
    if open_locked_oos and not oos_within_archive:
        blockers = tuple(sorted((*blockers, "LOCKED_OOS_COVERAGE_FAILED")))
        eligible = False
        oos_authorised = False
    return HistoricalArchiveCertification(
        status="PASS" if eligible else "BLOCKED",
        asset_class=manifest.asset_class,
        dataset_id=manifest.dataset_id,
        manifest_fingerprint=manifest.fingerprint,
        checks=tuple(sorted(checks.items())),
        blockers=blockers,
        training_authorised=eligible,
        validation_authorised=eligible,
        locked_oos_authorised=oos_authorised,
    )

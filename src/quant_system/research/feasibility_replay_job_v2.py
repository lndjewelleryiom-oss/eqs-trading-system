from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from typing import Any

from quant_system.operations.storage_guard import evaluate_bounded_offhost_write, evaluate_storage_guard
from quant_system.research.feasibility_v2 import archive_urls, load_and_validate


@dataclass(frozen=True, slots=True)
class ReplayJobDecision:
    schema_id: str
    campaign_id: str
    campaign_fingerprint: str
    status: str
    blockers: tuple[str, ...]
    expected_archive_objects: int
    admitted_archive_objects: int
    destination: str
    storage_state: str
    broker_submission_enabled: bool
    live_authority: bool
    locked_oos_opened: bool
    counts_toward_168h_100_trade_gate: bool

    def to_record(self) -> dict[str, object]:
        record = asdict(self)
        record["blockers"] = list(self.blockers)
        record["decision_sha256"] = sha256(
            json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        return record


def _load(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else None


def validate_acquisition_manifest(
    manifest: dict[str, Any] | None,
    *,
    campaign_id: str,
    campaign_fingerprint: str,
    expected_plan: tuple[dict[str, str], ...],
) -> tuple[str, ...]:
    blockers: list[str] = []
    if manifest is None:
        return ("ACQUISITION_MANIFEST_MISSING",)
    if manifest.get("schema_id") != "EQS-FEASIBILITY-V2-ACQUISITION-MANIFEST-V1":
        blockers.append("ACQUISITION_MANIFEST_SCHEMA_INVALID")
    if manifest.get("campaign_id") != campaign_id:
        blockers.append("ACQUISITION_CAMPAIGN_ID_MISMATCH")
    if manifest.get("campaign_fingerprint") != campaign_fingerprint:
        blockers.append("ACQUISITION_CAMPAIGN_FINGERPRINT_MISMATCH")
    if manifest.get("classification") != "EXPLORATORY_NON_EVIDENTIARY":
        blockers.append("ACQUISITION_CLASSIFICATION_INVALID")
    if manifest.get("locked_oos_opened") is not False:
        blockers.append("LOCKED_OOS_MUST_REMAIN_CLOSED")
    if manifest.get("r13_admission_authority") is not False:
        blockers.append("R13_AUTHORITY_MUST_BE_FALSE")
    if manifest.get("broker_submission_enabled") is not False:
        blockers.append("BROKER_SUBMISSION_MUST_BE_FALSE")
    if manifest.get("live_authority") is not False:
        blockers.append("LIVE_AUTHORITY_MUST_BE_FALSE")

    receipts = manifest.get("receipts")
    if not isinstance(receipts, list):
        blockers.append("ACQUISITION_RECEIPTS_MISSING")
        receipts = []
    expected_urls = {(row["series"], row["month"]): row["url"] for row in expected_plan}
    expected = set(expected_urls)
    observed: set[tuple[str, str]] = set()
    for receipt in receipts:
        if not isinstance(receipt, dict):
            blockers.append("ACQUISITION_RECEIPT_INVALID")
            continue
        key = (str(receipt.get("series", "")), str(receipt.get("month", "")))
        if key in observed:
            blockers.append("ACQUISITION_DUPLICATE_SERIES_MONTH")
        observed.add(key)
        if key in expected_urls and str(receipt.get("source_url", "")) != expected_urls[key]:
            blockers.append("ACQUISITION_SOURCE_URL_MISMATCH")
        for name in ("archive_sha256", "receipt_sha256"):
            value = str(receipt.get(name, ""))
            if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value.lower()):
                blockers.append(f"ACQUISITION_{name.upper()}_INVALID")
    if observed != expected:
        blockers.append("ACQUISITION_COVERAGE_INCOMPLETE")
    if int(manifest.get("object_count", -1)) != len(expected):
        blockers.append("ACQUISITION_OBJECT_COUNT_INVALID")
    return tuple(sorted(set(blockers)))


def prepare_replay_job(
    *,
    project_root: str | Path,
    acquisition_root: str | Path,
) -> ReplayJobDecision:
    root = Path(project_root)
    destination = Path(acquisition_root).resolve()
    campaign_path = root / "research" / "preregistrations" / "v2" / "FEAS-BINANCE-BTC-MA-001.json"
    campaign_record = load_and_validate(campaign_path, project_root=root)
    campaign = campaign_record["campaign"]
    plan = archive_urls(campaign_record)
    manifest_path = destination / "FEAS-BINANCE-BTC-MA-001-acquisition-manifest.json"
    manifest = _load(manifest_path)
    blockers = list(
        validate_acquisition_manifest(
            manifest,
            campaign_id=str(campaign["campaign_id"]),
            campaign_fingerprint=str(campaign_record["campaign_fingerprint"]),
            expected_plan=plan,
        )
    )
    guard = evaluate_storage_guard(destination if destination.exists() else destination.parent)
    storage_state = guard.state
    if manifest is not None and manifest.get("storage_authority") == "BOUNDED_OFFHOST":
        try:
            expected_bytes = int(manifest.get("expected_compressed_bytes", 0))
            offhost = evaluate_bounded_offhost_write(
                destination if destination.exists() else destination.parent,
                expected_bytes=expected_bytes,
                local_safety_path=root,
                max_expected_bytes=64 * 1024 ** 2,
                destination_floor_bytes=1024 ** 3,
                local_critical_bytes=4 * 1024 ** 3,
                cache_multiplier=4,
            )
            storage_state = offhost.state
            if not offhost.allowed:
                blockers.append("BOUNDED_OFFHOST_STORAGE_NO_LONGER_SAFE")
        except (ValueError, OSError):
            blockers.append("BOUNDED_OFFHOST_STORAGE_REVALIDATION_FAILED")
    elif not guard.allow_new_research:
        blockers.append("STORAGE_GUARD_BLOCKS_NEW_RESEARCH")
    blockers = sorted(set(blockers))
    admitted = 0
    if manifest is not None and isinstance(manifest.get("receipts"), list):
        admitted = len(manifest["receipts"])
    return ReplayJobDecision(
        schema_id="EQS-FEASIBILITY-V2-REPLAY-JOB-DECISION-V1",
        campaign_id=str(campaign["campaign_id"]),
        campaign_fingerprint=str(campaign_record["campaign_fingerprint"]),
        status="READY_FOR_REAL_TRAIN_VALIDATION_REPLAY" if not blockers else "BLOCKED",
        blockers=tuple(blockers),
        expected_archive_objects=len(plan),
        admitted_archive_objects=admitted,
        destination=str(destination),
        storage_state=storage_state,
        broker_submission_enabled=False,
        live_authority=False,
        locked_oos_opened=False,
        counts_toward_168h_100_trade_gate=False,
    )

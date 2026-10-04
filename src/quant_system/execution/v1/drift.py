from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
import json
from typing import Any, Mapping

from .models import ReconciliationStatus
from .private_reconciliation import (
    PrivateReadReconciliationReport,
    PrivateTruthDomain,
    PrivateTruthState,
)


class DriftStatus(StrEnum):
    BASELINE_ESTABLISHED = "BASELINE_ESTABLISHED"
    STABLE = "STABLE"
    DRIFT_DETECTED = "DRIFT_DETECTED"
    DRIFT_RESOLVED = "DRIFT_RESOLVED"
    BLOCKED_UNKNOWN = "BLOCKED_UNKNOWN"


def _wire(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, tuple) or isinstance(value, frozenset):
        return [_wire(v) for v in value]
    if isinstance(value, Mapping):
        return {str(k): _wire(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if hasattr(value, "__dataclass_fields__"):
        return {name: _wire(getattr(value, name)) for name in value.__dataclass_fields__}
    return value


def _hash(value: Any) -> str:
    raw = json.dumps(_wire(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return sha256(raw).hexdigest()


@dataclass(frozen=True, slots=True)
class DomainDrift:
    domain: PrivateTruthDomain
    changed: bool
    previous_hash: str | None
    current_hash: str | None
    previous_count: int
    current_count: int
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    modified: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return _wire(self)


@dataclass(frozen=True, slots=True)
class PrivateReadDriftReport:
    drift_id: str
    venue_id: str
    connection_id: str
    previous_reconciliation_id: str | None
    current_reconciliation_id: str
    detected_at: datetime
    status: DriftStatus
    unresolved: bool
    domains: tuple[DomainDrift, ...]
    reason: str
    schema_version: str = field(default="EQS-EXEC-PRIVATE-DRIFT-v1.0", init=False)

    @property
    def payload_hash(self) -> str:
        return _hash(self._base_payload())

    def _base_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "drift_id": self.drift_id,
            "venue_id": self.venue_id,
            "connection_id": self.connection_id,
            "previous_reconciliation_id": self.previous_reconciliation_id,
            "current_reconciliation_id": self.current_reconciliation_id,
            "detected_at": _wire(self.detected_at),
            "status": self.status.value,
            "unresolved": self.unresolved,
            "domains": [item.to_payload() for item in self.domains],
            "reason": self.reason,
        }

    def to_payload(self) -> dict[str, Any]:
        payload = self._base_payload()
        payload["payload_hash"] = self.payload_hash
        return payload


_DOMAIN_META: dict[PrivateTruthDomain, tuple[str, str, frozenset[str]]] = {
    PrivateTruthDomain.ORDERS: ("orders", "client_order_id", frozenset()),
    PrivateTruthDomain.FILLS: ("fills", "fill_id", frozenset({"observed_at"})),
    PrivateTruthDomain.FEES: ("fees", "fee_id", frozenset({"observed_at"})),
    PrivateTruthDomain.BALANCES: ("balances", "asset", frozenset({"observed_at"})),
    PrivateTruthDomain.POSITIONS: ("positions", "instrument_id", frozenset()),
}


def _records(report: Mapping[str, Any], domain: PrivateTruthDomain) -> dict[str, dict[str, Any]]:
    field_name, key_name, ignored = _DOMAIN_META[domain]
    raw = report.get(field_name, [])
    if not isinstance(raw, list):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for item in raw:
        if not isinstance(item, dict) or key_name not in item:
            continue
        normalized = {str(k): v for k, v in item.items() if str(k) not in ignored}
        result[str(item[key_name])] = normalized
    return result


def _domain_drift(previous: Mapping[str, Any] | None, current: Mapping[str, Any], domain: PrivateTruthDomain) -> DomainDrift:
    before = {} if previous is None else _records(previous, domain)
    after = _records(current, domain)
    before_keys = set(before)
    after_keys = set(after)
    added = tuple(sorted(after_keys - before_keys))
    removed = tuple(sorted(before_keys - after_keys))
    modified = tuple(sorted(key for key in before_keys & after_keys if before[key] != after[key]))
    changed = bool(added or removed or modified)
    return DomainDrift(
        domain=domain,
        changed=changed,
        previous_hash=None if previous is None else _hash(before),
        current_hash=_hash(after),
        previous_count=len(before),
        current_count=len(after),
        added=added,
        removed=removed,
        modified=modified,
    )


def compare_private_read_domains(
    previous_report: Mapping[str, Any] | None, current_report: Mapping[str, Any]
) -> tuple[DomainDrift, ...]:
    return tuple(_domain_drift(previous_report, current_report, domain) for domain in PrivateTruthDomain)


def assess_private_read_drift(
    previous_report: Mapping[str, Any] | None,
    current_report: PrivateReadReconciliationReport,
    *,
    previous_drift: Mapping[str, Any] | None = None,
    detected_at: datetime,
) -> PrivateReadDriftReport:
    if detected_at.tzinfo is None or detected_at.utcoffset() is None:
        raise ValueError("drift detection timestamp must be timezone-aware")
    current = current_report.to_payload()
    recon = current_report.reconciliation
    if previous_report is not None:
        previous_recon = previous_report.get("reconciliation", {})
        if str(previous_recon.get("venue_id")) != recon.venue_id or str(previous_recon.get("connection_id")) != recon.connection_id:
            raise ValueError("private drift comparison cannot span venue/connection scopes")
        previous_reconciliation_id = str(previous_recon.get("reconciliation_id"))
    else:
        previous_reconciliation_id = None

    domains = compare_private_read_domains(previous_report, current)
    changed_domains = tuple(item.domain.value for item in domains if item.changed)
    complete = all(item.state == PrivateTruthState.COMPLETE for item in current_report.domains)
    matched = current_report.reconciliation.status == ReconciliationStatus.MATCHED and current_report.commissioning_ready
    previous_unresolved = bool(previous_drift and previous_drift.get("unresolved"))

    if not complete or not matched:
        status = DriftStatus.BLOCKED_UNKNOWN
        unresolved = True
        reason = "CURRENT_PRIVATE_TRUTH_NOT_COMPLETE_AND_MATCHED"
    elif previous_report is None:
        status = DriftStatus.BASELINE_ESTABLISHED
        unresolved = False
        reason = "FIRST_COMPLETE_MATCHED_PRIVATE_TRUTH_BASELINE"
    elif changed_domains:
        status = DriftStatus.DRIFT_DETECTED
        unresolved = True
        reason = "DRIFT_REQUIRES_STABLE_CONFIRMATION:" + ",".join(changed_domains)
    elif previous_unresolved:
        status = DriftStatus.DRIFT_RESOLVED
        unresolved = False
        reason = "PREVIOUS_DRIFT_CONFIRMED_STABLE_BY_SUBSEQUENT_MATCHED_RECONCILIATION"
    else:
        status = DriftStatus.STABLE
        unresolved = False
        reason = "NO_PRIVATE_TRUTH_DRIFT"

    drift_id = "DRIFT-" + sha256(
        json.dumps(
            {
                "venue_id": recon.venue_id,
                "connection_id": recon.connection_id,
                "previous_reconciliation_id": previous_reconciliation_id,
                "current_reconciliation_id": recon.reconciliation_id,
                "status": status.value,
                "domains": [item.to_payload() for item in domains],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:48]
    return PrivateReadDriftReport(
        drift_id=drift_id,
        venue_id=recon.venue_id,
        connection_id=recon.connection_id,
        previous_reconciliation_id=previous_reconciliation_id,
        current_reconciliation_id=recon.reconciliation_id,
        detected_at=detected_at,
        status=status,
        unresolved=unresolved,
        domains=domains,
        reason=reason,
    )


__all__ = [
    "DriftStatus", "DomainDrift", "PrivateReadDriftReport", "assess_private_read_drift",
    "compare_private_read_domains",
]

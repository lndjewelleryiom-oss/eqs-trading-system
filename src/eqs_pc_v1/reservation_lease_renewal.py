from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

RESERVATION_LEASE_RENEWAL_APPROVAL_VERSION = "1.0.0"
RESERVATION_LEASE_RENEWAL_APPROVAL_TYPE = "EQS_PC_RESERVATION_LEASE_RENEWAL"
CAPITAL_RISK_AUTHORITY = "EQS-SHARED-03_CAPITAL_RISK"
RENEWAL_APPROVAL_DOMAIN = b"EQS-PC-RESERVATION-LEASE-RENEWAL-APPROVAL-V1\x00"
RENEWAL_BOUNDS_DOMAIN = b"EQS-PC-RESERVATION-LEASE-RENEWAL-BOUNDS-V1\x00"


class ReservationLeaseRenewalError(ValueError):
    pass


def canonical_json(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def _hash(domain: bytes, obj: Any) -> str:
    return hashlib.sha256(domain + canonical_json(obj)).hexdigest()


def _parse_ts(v: str) -> datetime:
    dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ReservationLeaseRenewalError("timestamp must be timezone-aware")
    return dt.astimezone(timezone.utc)


def renewal_bounds_sha256(bounds: Dict[str, Any]) -> str:
    return _hash(RENEWAL_BOUNDS_DOMAIN, bounds)


def seal_reservation_lease_renewal_approval(approval: Dict[str, Any]) -> str:
    core = copy.deepcopy(approval)
    core.pop("approval_sha256", None)
    return _hash(RENEWAL_APPROVAL_DOMAIN, core)


def _whole_seconds_between(a: str, b: str) -> int:
    seconds = (_parse_ts(b) - _parse_ts(a)).total_seconds()
    if seconds <= 0 or int(seconds) != seconds:
        raise ReservationLeaseRenewalError("extension must be a positive whole number of seconds")
    return int(seconds)


def build_reservation_lease_renewal_approval(
    lease_state: Dict[str, Any] | Any,
    *,
    reapproved_at: str,
    new_expires_at: str,
    renewal_bounds: Dict[str, Any],
    capital_risk_evidence_hash: str,
    reason_codes: List[str],
    renewal_approval_id: Optional[str] = None,
    authority: str = CAPITAL_RISK_AUTHORITY,
) -> Dict[str, Any]:
    if hasattr(lease_state, "to_dict"):
        lease_state = lease_state.to_dict()
    if not isinstance(lease_state, dict):
        raise ReservationLeaseRenewalError("lease_state must be a mapping or expose to_dict()")
    current_expiry = lease_state["expires_at"]
    extension_seconds = _whole_seconds_between(current_expiry, new_expires_at)
    bounds = copy.deepcopy(renewal_bounds)
    bounds_hash = renewal_bounds_sha256(bounds)
    approval = {
        "schema_version": RESERVATION_LEASE_RENEWAL_APPROVAL_VERSION,
        "approval_type": RESERVATION_LEASE_RENEWAL_APPROVAL_TYPE,
        "authority": authority,
        "renewal_approval_id": renewal_approval_id or f"RLA-{_hash(RENEWAL_APPROVAL_DOMAIN, {'lease_id': lease_state['lease_id'], 'sequence': lease_state.get('renewal_count', 0) + 1, 'new_expires_at': new_expires_at})[:20]}",
        "reservation_id": lease_state["reservation_id"],
        "reservation_request_id": lease_state["reservation_request_id"],
        "portfolio_run_id": lease_state["portfolio_run_id"],
        "lease_id": lease_state["lease_id"],
        "policy_generation_pin_sha256": lease_state.get("policy_generation_pin_sha256"),
        "approved_target_set_hash": lease_state["approved_target_set_hash"],
        "renewal_sequence": int(lease_state.get("renewal_count", 0)) + 1,
        "current_expires_at": current_expiry,
        "new_expires_at": new_expires_at,
        "extension_seconds": extension_seconds,
        "renewal_bounds": bounds,
        "renewal_bounds_sha256": bounds_hash,
        "capital_risk_evidence_hash": capital_risk_evidence_hash,
        "reason_codes": list(reason_codes),
        "reapproved_at": reapproved_at,
        "approval_sha256": "",
    }
    approval["approval_sha256"] = seal_reservation_lease_renewal_approval(approval)
    return approval


def validate_reservation_lease_renewal_approval(
    approval: Dict[str, Any],
    *,
    reservation_id: str,
    reservation_request_id: str,
    portfolio_run_id: str,
    lease_id: str,
    policy_generation_pin_sha256: Optional[str],
    approved_target_set_hash: str,
    issued_at: str,
    current_expires_at: str,
    renewal_count: int,
    total_extension_seconds: int,
    existing_renewal_bounds_sha256: Optional[str],
) -> List[str]:
    errors: List[str] = []
    try:
        canonical_json(approval)
    except Exception as exc:
        return [f"RENEWAL_APPROVAL_NONCANONICAL:{exc}"]
    if approval.get("schema_version") != RESERVATION_LEASE_RENEWAL_APPROVAL_VERSION:
        errors.append("RENEWAL_APPROVAL_VERSION_INVALID")
    if approval.get("approval_type") != RESERVATION_LEASE_RENEWAL_APPROVAL_TYPE:
        errors.append("RENEWAL_APPROVAL_TYPE_INVALID")
    if approval.get("authority") != CAPITAL_RISK_AUTHORITY:
        errors.append("RENEWAL_APPROVAL_AUTHORITY_INVALID")
    expected = {
        "reservation_id": reservation_id,
        "reservation_request_id": reservation_request_id,
        "portfolio_run_id": portfolio_run_id,
        "lease_id": lease_id,
        "policy_generation_pin_sha256": policy_generation_pin_sha256,
        "approved_target_set_hash": approved_target_set_hash,
        "current_expires_at": current_expires_at,
    }
    for field, value in expected.items():
        if approval.get(field) != value:
            errors.append(f"RENEWAL_APPROVAL_{field.upper()}_MISMATCH")
    if approval.get("renewal_sequence") != renewal_count + 1:
        errors.append("RENEWAL_APPROVAL_SEQUENCE_INVALID")
    bounds = approval.get("renewal_bounds")
    if not isinstance(bounds, dict):
        errors.append("RENEWAL_BOUNDS_MISSING")
        bounds = {}
    try:
        bounds_hash = renewal_bounds_sha256(bounds)
    except Exception as exc:
        bounds_hash = None
        errors.append(f"RENEWAL_BOUNDS_NONCANONICAL:{exc}")
    if approval.get("renewal_bounds_sha256") != bounds_hash:
        errors.append("RENEWAL_BOUNDS_HASH_MISMATCH")
    if existing_renewal_bounds_sha256 is not None and bounds_hash != existing_renewal_bounds_sha256:
        errors.append("RENEWAL_BOUNDS_CANNOT_EXPAND_OR_CHANGE_IN_FLIGHT")
    try:
        reapproved = _parse_ts(approval["reapproved_at"])
        issued = _parse_ts(issued_at)
        current_expiry = _parse_ts(current_expires_at)
        new_expiry = _parse_ts(approval["new_expires_at"])
        absolute_not_after = _parse_ts(bounds["absolute_not_after"])
        if not (issued <= reapproved < current_expiry):
            errors.append("RENEWAL_REAPPROVAL_OUTSIDE_ACTIVE_WINDOW")
        if new_expiry <= current_expiry:
            errors.append("RENEWAL_NEW_EXPIRY_NOT_LATER")
        if new_expiry > absolute_not_after:
            errors.append("RENEWAL_ABSOLUTE_NOT_AFTER_EXCEEDED")
        actual_extension = _whole_seconds_between(current_expires_at, approval["new_expires_at"])
        if approval.get("extension_seconds") != actual_extension:
            errors.append("RENEWAL_EXTENSION_SECONDS_MISMATCH")
        max_single = int(bounds["max_single_extension_seconds"])
        max_total = int(bounds["max_total_extension_seconds"])
        max_count = int(bounds["max_renewal_count"])
        if max_single <= 0 or max_total <= 0 or max_count <= 0:
            errors.append("RENEWAL_BOUNDS_MUST_BE_POSITIVE")
        if actual_extension > max_single:
            errors.append("RENEWAL_SINGLE_EXTENSION_LIMIT_EXCEEDED")
        if total_extension_seconds + actual_extension > max_total:
            errors.append("RENEWAL_TOTAL_EXTENSION_LIMIT_EXCEEDED")
        if renewal_count + 1 > max_count:
            errors.append("RENEWAL_COUNT_LIMIT_EXCEEDED")
        if _parse_ts(bounds["absolute_not_after"]) < _parse_ts(current_expires_at):
            errors.append("RENEWAL_ABSOLUTE_NOT_AFTER_BEFORE_CURRENT_EXPIRY")
    except Exception as exc:
        errors.append(f"RENEWAL_TEMPORAL_OR_BOUND_INVALID:{exc}")
    if not isinstance(approval.get("reason_codes"), list) or not approval.get("reason_codes") or not all(isinstance(x, str) and x for x in approval.get("reason_codes", [])):
        errors.append("RENEWAL_REASON_CODES_INVALID")
    evidence = approval.get("capital_risk_evidence_hash")
    if not isinstance(evidence, str) or len(evidence) != 64 or any(c not in "0123456789abcdef" for c in evidence):
        errors.append("RENEWAL_CAPITAL_RISK_EVIDENCE_HASH_INVALID")
    if approval.get("approval_sha256") != seal_reservation_lease_renewal_approval(approval):
        errors.append("RENEWAL_APPROVAL_SEAL_INVALID")
    return errors

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Dict, List

CAPITAL_RISK_POLICY_ATTESTATION_VERSION = "1.0.0"
CAPITAL_RISK_POLICY_ATTESTATION_TYPE = "EQS_PC_RESERVATION_POLICY_BINDING"
CAPITAL_RISK_AUTHORITY = "EQS-SHARED-03_CAPITAL_RISK"
ATTESTATION_DOMAIN = b"EQS-PC-CAPITAL-RISK-POLICY-ATTESTATION-V1\x00"


def canonical_json(obj: Any) -> bytes:
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _domain_hash(domain: bytes, obj: Any) -> str:
    return hashlib.sha256(domain + canonical_json(obj)).hexdigest()


def seal_reservation_policy_attestation(attestation: Dict[str, Any]) -> str:
    core = copy.deepcopy(attestation)
    core.pop("attestation_sha256", None)
    return _domain_hash(ATTESTATION_DOMAIN, core)


def build_reservation_policy_attestation(
    reservation_request: Dict[str, Any],
    response: Dict[str, Any],
    *,
    authority: str = CAPITAL_RISK_AUTHORITY,
) -> Dict[str, Any]:
    pin = reservation_request.get("policy_generation_pin_sha256")
    if not pin:
        raise ValueError("authoritative reservation policy attestation requires a policy generation pin")
    attestation = {
        "schema_version": CAPITAL_RISK_POLICY_ATTESTATION_VERSION,
        "attestation_type": CAPITAL_RISK_POLICY_ATTESTATION_TYPE,
        "authority": authority,
        "reservation_request_id": reservation_request["reservation_request_id"],
        "portfolio_run_id": reservation_request["portfolio_run_id"],
        "policy_generation_pin_sha256": pin,
        "decision": response["decision"],
        "reservation_id": response.get("reservation_id"),
        "approved_target_set_hash": response.get("approved_target_set_hash"),
        "capital_risk_evidence_hash": response["capital_risk_evidence_hash"],
        "decided_at": response["decided_at"],
        "attestation_sha256": "",
    }
    attestation["attestation_sha256"] = seal_reservation_policy_attestation(attestation)
    return attestation


def attest_capital_risk_response(
    reservation_request: Dict[str, Any],
    response: Dict[str, Any],
    *,
    authority: str = CAPITAL_RISK_AUTHORITY,
) -> Dict[str, Any]:
    out = copy.deepcopy(response)
    pin = reservation_request.get("policy_generation_pin_sha256")
    if not pin:
        # Synthetic/unpinned requests intentionally do not acquire an authority claim.
        out.pop("policy_generation_pin_sha256", None)
        out.pop("reservation_policy_attestation", None)
        return out
    out["policy_generation_pin_sha256"] = pin
    out["reservation_policy_attestation"] = build_reservation_policy_attestation(
        reservation_request, out, authority=authority
    )
    return out


def validate_capital_risk_policy_binding(
    reservation_request: Dict[str, Any],
    response: Dict[str, Any],
) -> List[str]:
    errors: List[str] = []
    request_pin = reservation_request.get("policy_generation_pin_sha256")
    response_pin = response.get("policy_generation_pin_sha256")
    attestation = response.get("reservation_policy_attestation")

    if not request_pin:
        if response_pin not in (None, ""):
            errors.append("UNPINNED_REQUEST_HAS_RESPONSE_POLICY_PIN")
        if attestation not in (None, {}):
            errors.append("UNPINNED_REQUEST_HAS_POLICY_ATTESTATION")
        return errors

    if response_pin != request_pin:
        errors.append("CAPITAL_RISK_RESPONSE_POLICY_PIN_MISMATCH")
    if not isinstance(attestation, dict):
        errors.append("CAPITAL_RISK_POLICY_ATTESTATION_MISSING")
        return errors

    expected_fields = {
        "schema_version": CAPITAL_RISK_POLICY_ATTESTATION_VERSION,
        "attestation_type": CAPITAL_RISK_POLICY_ATTESTATION_TYPE,
        "authority": CAPITAL_RISK_AUTHORITY,
        "reservation_request_id": reservation_request.get("reservation_request_id"),
        "portfolio_run_id": reservation_request.get("portfolio_run_id"),
        "policy_generation_pin_sha256": request_pin,
        "decision": response.get("decision"),
        "reservation_id": response.get("reservation_id"),
        "approved_target_set_hash": response.get("approved_target_set_hash"),
        "capital_risk_evidence_hash": response.get("capital_risk_evidence_hash"),
        "decided_at": response.get("decided_at"),
    }
    for field, expected in expected_fields.items():
        if attestation.get(field) != expected:
            errors.append(f"CAPITAL_RISK_POLICY_ATTESTATION_{field.upper()}_MISMATCH")

    try:
        calculated = seal_reservation_policy_attestation(attestation)
    except Exception as exc:
        errors.append(f"CAPITAL_RISK_POLICY_ATTESTATION_NONCANONICAL:{type(exc).__name__}:{exc}")
    else:
        if attestation.get("attestation_sha256") != calculated:
            errors.append("CAPITAL_RISK_POLICY_ATTESTATION_SEAL_MISMATCH")
    return errors

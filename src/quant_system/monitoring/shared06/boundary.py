from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Callable, Mapping, Sequence, Any

from .aggregator import ReferenceAggregator, ENGINE_ID, ENGINE_VERSION

_ALLOWED_MODES = {"PAPER", "SHADOW"}
_REQUIRED_BOUNDARY_GATES = ("OPS_G04_EXECUTION_HEALTH", "OPS_G06_MODE_INTEGRITY", "OPS_G10_SAFETY_INTERFACE")


def _canonical(payload: object) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str).encode("utf-8")


@dataclass(frozen=True, slots=True)
class OperationalCertificationDecision:
    allowed: bool
    runtime_mode: str
    overall_outcome: str
    reason_codes: tuple[str, ...]
    certificate_sha256: str | None = None
    seal_sha256: str | None = None
    boundary_fingerprint: str | None = None
    gate_hashes: tuple[tuple[str, str], ...] = ()

    def lineage(self) -> dict[str, object]:
        return {"shared06_engine_id": ENGINE_ID, "shared06_engine_version": ENGINE_VERSION,
                "shared06_runtime_mode": self.runtime_mode, "shared06_operational_outcome": self.overall_outcome,
                "shared06_certificate_sha256": self.certificate_sha256, "shared06_seal_sha256": self.seal_sha256,
                "shared06_boundary_fingerprint": self.boundary_fingerprint,
                "shared06_gate_hashes": dict(self.gate_hashes), "shared06_broker_submission_enabled": False}


class OperationalCertificationBlocked(RuntimeError):
    def __init__(self, message: str, decision: OperationalCertificationDecision | None = None):
        super().__init__(message)
        self.decision = decision


class OperationalCertificationBoundary:
    """Fail-closed EQS-SHARED-06 certification boundary for PAPER/SHADOW only."""

    def __init__(self, *, aggregator: ReferenceAggregator,
                 envelopes_provider: Callable[[str, datetime], Sequence[dict[str, Any]]],
                 certification_policies: Mapping[str, Mapping[str, Any]],
                 promotion_policies: Mapping[str, Mapping[str, Any]] | None = None,
                 external_gate_provider: Callable[[str, datetime], Sequence[Mapping[str, Any]]] | None = None):
        self.aggregator = aggregator
        self.envelopes_provider = envelopes_provider
        self.certification_policies = dict(certification_policies)
        self.promotion_policies = dict(promotion_policies or {})
        self.external_gate_provider = external_gate_provider

    def evaluate(self, runtime_mode: str, *, broker_submission_enabled: bool,
                 certification_as_of: datetime | None = None) -> OperationalCertificationDecision:
        mode = str(runtime_mode)
        if mode not in _ALLOWED_MODES:
            return self._blocked(mode, "SHARED06_NONLIVE_MODE_REQUIRED")
        if broker_submission_enabled:
            return self._blocked(mode, "SHARED06_BROKER_SUBMISSION_ENABLED")
        policy = self.certification_policies.get(mode)
        if policy is None:
            return self._blocked(mode, "SHARED06_CERTIFICATION_POLICY_MISSING")
        if policy.get("environment") != mode:
            return self._blocked(mode, "SHARED06_CERTIFICATION_POLICY_MODE_MISMATCH")
        at = certification_as_of or datetime.now(timezone.utc)
        if at.tzinfo is None or at.utcoffset() is None:
            return self._blocked(mode, "SHARED06_CERTIFICATION_TIME_NAIVE")
        try:
            envelopes = list(self.envelopes_provider(mode, at))
            external = [] if self.external_gate_provider is None else list(self.external_gate_provider(mode, at))
            result = self.aggregator.certify(envelopes, policy, at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"), mode,
                                             self.promotion_policies.get(mode), external)
            verified = self.aggregator.verify_output(result)
        except Exception as exc:
            return self._blocked(mode, f"SHARED06_CERTIFICATION_EXCEPTION:{type(exc).__name__}")
        cert = result["operational_certificate"]
        seal = result["final_seal"]
        gates = {g["gate_id"]: g for g in result["gate_results"]}
        reasons: list[str] = []
        if not verified.get("verified"):
            reasons.append("SHARED06_OUTPUT_SEAL_INVALID")
        if cert.get("environment") != mode:
            reasons.append("SHARED06_CERTIFICATE_MODE_MISMATCH")
        if cert.get("overall_outcome") != "PASS":
            reasons.append("SHARED06_OPERATIONAL_CERTIFICATION_" + str(cert.get("overall_outcome", "UNKNOWN")))
        if seal.get("seal_status") != "VERIFIED_PASS":
            reasons.append("SHARED06_FINAL_SEAL_NOT_PASS")
        if cert.get("canonical_programme_mutation") is not False or cert.get("capital_promotion_performed") is not False:
            reasons.append("SHARED06_AUTHORITY_BOUNDARY_BREACH")
        for gate_id in _REQUIRED_BOUNDARY_GATES:
            if gates.get(gate_id, {}).get("outcome") != "PASS":
                reasons.append(f"SHARED06_{gate_id}_NOT_PASS")
        gate_hashes = tuple(sorted((gid, str(g.get("gate_result_sha256"))) for gid, g in gates.items()))
        payload = {"runtime_mode": mode, "certificate_sha256": cert.get("certificate_sha256"),
                   "seal_sha256": seal.get("seal_sha256"), "gate_hashes": gate_hashes,
                   "broker_submission_enabled": False, "reason_codes": sorted(set(reasons))}
        fp = sha256(_canonical(payload)).hexdigest()
        return OperationalCertificationDecision(not reasons, mode, str(cert.get("overall_outcome", "UNKNOWN")),
                                                tuple(sorted(set(reasons))), cert.get("certificate_sha256"),
                                                seal.get("seal_sha256"), fp, gate_hashes)

    def execute_certified_cycle(self, runtime_mode: str, *, broker_submission_state: Callable[[], bool],
                                certification_as_of: datetime, action: Callable[[OperationalCertificationDecision], Any]) -> tuple[Any, OperationalCertificationDecision]:
        mode = str(runtime_mode)
        pre = self.evaluate(mode, broker_submission_enabled=bool(broker_submission_state()), certification_as_of=certification_as_of)
        self._require_allowed(pre)
        if broker_submission_state():
            blocked = self._blocked(mode, "SHARED06_BROKER_SUBMISSION_ENABLED_POST_CERTIFICATION")
            raise OperationalCertificationBlocked("SHARED06_OPERATIONAL_BLOCK:" + blocked.reason_codes[0], blocked)
        post = self.evaluate(mode, broker_submission_enabled=False, certification_as_of=certification_as_of)
        self._require_allowed(post)
        if post.boundary_fingerprint != pre.boundary_fingerprint:
            blocked = self._blocked(mode, "SHARED06_BOUNDARY_CHANGED_DURING_CERTIFICATION")
            raise OperationalCertificationBlocked("SHARED06_OPERATIONAL_BLOCK:" + blocked.reason_codes[0], blocked)
        result = action(pre)
        if broker_submission_state():
            blocked = self._blocked(mode, "SHARED06_BROKER_SUBMISSION_ENABLED_POST_CYCLE")
            raise OperationalCertificationBlocked("SHARED06_OPERATIONAL_BLOCK:" + blocked.reason_codes[0], blocked)
        final = self.evaluate(mode, broker_submission_enabled=False, certification_as_of=certification_as_of)
        self._require_allowed(final)
        if final.boundary_fingerprint != pre.boundary_fingerprint:
            blocked = self._blocked(mode, "SHARED06_BOUNDARY_CHANGED_DURING_CYCLE")
            raise OperationalCertificationBlocked("SHARED06_OPERATIONAL_BLOCK:" + blocked.reason_codes[0], blocked)
        return result, pre

    def guard_broker_facing_read_or_evaluation(self, runtime_mode: str, *, broker_submission_state: Callable[[], bool],
                                                certification_as_of: datetime) -> OperationalCertificationDecision:
        mode = str(runtime_mode)
        decision = self.evaluate(mode, broker_submission_enabled=bool(broker_submission_state()), certification_as_of=certification_as_of)
        self._require_allowed(decision)
        if broker_submission_state():
            blocked = self._blocked(mode, "SHARED06_BROKER_SUBMISSION_ENABLED_AT_BROKER_BOUNDARY")
            raise OperationalCertificationBlocked("SHARED06_OPERATIONAL_BLOCK:" + blocked.reason_codes[0], blocked)
        return decision

    @staticmethod
    def _require_allowed(decision: OperationalCertificationDecision) -> None:
        if not decision.allowed:
            reason = "SHARED06_OPERATIONAL_BLOCK:" + ",".join(decision.reason_codes)
            raise OperationalCertificationBlocked(reason, decision)

    @staticmethod
    def _blocked(mode: str, reason: str) -> OperationalCertificationDecision:
        payload = {"runtime_mode": mode, "allowed": False, "reason_codes": [reason], "broker_submission_enabled": False}
        return OperationalCertificationDecision(False, mode, "UNKNOWN", (reason,), boundary_fingerprint=sha256(_canonical(payload)).hexdigest())

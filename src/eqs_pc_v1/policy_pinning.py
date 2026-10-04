from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Sequence
import hashlib

from .validator import canonical_json

POLICY_GENERATION_PIN_VERSION = "EQS-PC-POLICY-GENERATION-PIN-v1"
PIN_DOMAIN = b"EQS-PC-POLICY-GENERATION-PIN-V1\x00"


class PolicyGenerationPinError(ValueError):
    pass


def _domain_hash(obj: Any) -> str:
    return hashlib.sha256(PIN_DOMAIN + canonical_json(obj)).hexdigest()


@dataclass(frozen=True)
class PolicyGenerationPin:
    pin_version: str
    run_id: str
    request_sha256: str
    authority: str
    policy_id: str
    policy_version: str
    policy_generation: int
    policy_seal_sha256: str
    policy_envelope_sha256: str
    policy_payload_sha256: str
    authority_evidence_id: str
    authority_evidence_sha256: str
    authority_snapshot_id: str
    authority_snapshot_sha256: str
    lifecycle_id: str
    lifecycle_event_count: int
    lifecycle_head_sha256: str
    lifecycle_state_sha256: str
    resolution_digest_sha256: str
    decision_time: str
    pin_sha256: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pin_version": self.pin_version,
            "run_id": self.run_id,
            "request_sha256": self.request_sha256,
            "authority": self.authority,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "policy_generation": self.policy_generation,
            "policy_seal_sha256": self.policy_seal_sha256,
            "policy_envelope_sha256": self.policy_envelope_sha256,
            "policy_payload_sha256": self.policy_payload_sha256,
            "authority_evidence_id": self.authority_evidence_id,
            "authority_evidence_sha256": self.authority_evidence_sha256,
            "authority_snapshot_id": self.authority_snapshot_id,
            "authority_snapshot_sha256": self.authority_snapshot_sha256,
            "lifecycle_id": self.lifecycle_id,
            "lifecycle_event_count": self.lifecycle_event_count,
            "lifecycle_head_sha256": self.lifecycle_head_sha256,
            "lifecycle_state_sha256": self.lifecycle_state_sha256,
            "resolution_digest_sha256": self.resolution_digest_sha256,
            "decision_time": self.decision_time,
            "pin_sha256": self.pin_sha256,
        }

    @classmethod
    def from_dict(cls, obj: Mapping[str, Any]) -> "PolicyGenerationPin":
        return cls(**{k: obj[k] for k in cls.__dataclass_fields__.keys()})

    def verify(self) -> None:
        d = self.to_dict()
        actual = d.pop("pin_sha256")
        expected = _domain_hash(d)
        if actual != expected:
            raise PolicyGenerationPinError("policy generation pin hash mismatch")
        if self.policy_generation < 1:
            raise PolicyGenerationPinError("policy generation pin requires generation >= 1")
        if self.lifecycle_event_count < 1:
            raise PolicyGenerationPinError("policy generation pin requires lifecycle events")


def build_policy_generation_pin(
    *,
    run_id: str,
    request_sha256: str,
    policy_resolution: Mapping[str, Any],
    authority_snapshot: Mapping[str, Any],
    lifecycle_events: Sequence[Mapping[str, Any]],
) -> PolicyGenerationPin:
    if not lifecycle_events:
        raise PolicyGenerationPinError("authoritative policy pin requires lifecycle events")
    required_snapshot = [
        "lifecycle_id", "lifecycle_event_count", "lifecycle_head_sha256", "lifecycle_state_sha256"
    ]
    missing = [k for k in required_snapshot if not authority_snapshot.get(k)]
    if missing:
        raise PolicyGenerationPinError("authority snapshot lacks lifecycle proof: " + ",".join(missing))
    if int(authority_snapshot["lifecycle_event_count"]) != len(lifecycle_events):
        raise PolicyGenerationPinError("authority snapshot lifecycle event count mismatch")
    if authority_snapshot["lifecycle_head_sha256"] != lifecycle_events[-1].get("event_sha256"):
        raise PolicyGenerationPinError("authority snapshot lifecycle head mismatch")

    cross_checks = [
        (authority_snapshot.get("active_policy_id"), policy_resolution.get("policy_id"), "policy id"),
        (authority_snapshot.get("active_policy_version"), policy_resolution.get("policy_version"), "policy version"),
        (authority_snapshot.get("active_policy_generation"), policy_resolution.get("policy_generation"), "policy generation"),
        (authority_snapshot.get("active_policy_seal_sha256"), policy_resolution.get("policy_seal_sha256"), "policy seal"),
        (authority_snapshot.get("active_policy_envelope_sha256"), policy_resolution.get("policy_envelope_sha256"), "policy envelope hash"),
        (authority_snapshot.get("authority_evidence_id"), policy_resolution.get("authority_evidence_id"), "authority evidence id"),
        (authority_snapshot.get("authority_evidence_sha256"), policy_resolution.get("authority_evidence_sha256"), "authority evidence hash"),
    ]
    for actual, expected, label in cross_checks:
        if actual != expected:
            raise PolicyGenerationPinError(f"policy generation pin {label} mismatch")

    core = {
        "pin_version": POLICY_GENERATION_PIN_VERSION,
        "run_id": run_id,
        "request_sha256": request_sha256,
        "authority": policy_resolution["authority"],
        "policy_id": policy_resolution["policy_id"],
        "policy_version": policy_resolution["policy_version"],
        "policy_generation": int(policy_resolution["policy_generation"]),
        "policy_seal_sha256": policy_resolution["policy_seal_sha256"],
        "policy_envelope_sha256": policy_resolution["policy_envelope_sha256"],
        "policy_payload_sha256": policy_resolution["policy_payload_sha256"],
        "authority_evidence_id": policy_resolution["authority_evidence_id"],
        "authority_evidence_sha256": policy_resolution["authority_evidence_sha256"],
        "authority_snapshot_id": policy_resolution["authority_snapshot_id"],
        "authority_snapshot_sha256": policy_resolution["authority_snapshot_sha256"],
        "lifecycle_id": authority_snapshot["lifecycle_id"],
        "lifecycle_event_count": int(authority_snapshot["lifecycle_event_count"]),
        "lifecycle_head_sha256": authority_snapshot["lifecycle_head_sha256"],
        "lifecycle_state_sha256": authority_snapshot["lifecycle_state_sha256"],
        "resolution_digest_sha256": policy_resolution["resolution_digest_sha256"],
        "decision_time": policy_resolution["decision_time"],
    }
    pin = PolicyGenerationPin(**core, pin_sha256=_domain_hash(core))
    pin.verify()
    return pin

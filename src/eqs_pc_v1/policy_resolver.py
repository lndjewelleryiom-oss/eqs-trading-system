from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping
import copy
import hashlib

from .allocator import ALLOCATION_INTERFACE_VERSION, FIXED_RISK_BUDGET_METHOD_VERSION
from .validator import SchemaRegistry, canonical_json, parse_ts, sha256_obj

POLICY_RESOLVER_VERSION = "EQS-PC-POLICY-RESOLVER-v1"
POLICY_BINDING_VERSION = "EQS-PC-POLICY-BINDING-v1"
AUTHORITY_EVIDENCE_VERSION = "EQS-PC-AUTHORITY-EVIDENCE-v1"
POLICY_AUTHORITY_SNAPSHOT_VERSION = "EQS-PC-POLICY-AUTHORITY-SNAPSHOT-v1"

AUTHORISED_PRODUCTION_AUTHORITIES = {"EQS-00", "CAPITAL_RISK", "EQS-00/CAPITAL_RISK"}


class PolicyResolutionError(ValueError):
    """Fail-closed allocation-policy authority/binding violation."""


def domain_sha256(domain: str, obj: Any) -> str:
    data = domain.encode("utf-8") + b"\x00" + canonical_json(obj)
    return hashlib.sha256(data).hexdigest()


def seal_authority_evidence(evidence: Mapping[str, Any]) -> str:
    body = copy.deepcopy(dict(evidence))
    body.pop("evidence_seal_sha256", None)
    return domain_sha256("EQS-PC-AUTHORITY-EVIDENCE-V1", body)


def seal_policy_envelope(envelope: Mapping[str, Any]) -> str:
    body = copy.deepcopy(dict(envelope))
    body.pop("policy_seal_sha256", None)
    return domain_sha256("EQS-PC-ALLOCATION-POLICY-ENVELOPE-V1", body)


def seal_authority_snapshot(snapshot: Mapping[str, Any]) -> str:
    body = copy.deepcopy(dict(snapshot))
    body.pop("snapshot_seal_sha256", None)
    return domain_sha256("EQS-PC-POLICY-AUTHORITY-SNAPSHOT-V1", body)


@dataclass(frozen=True)
class ResolvedPolicyBinding:
    resolver_version: str
    binding_version: str
    policy_id: str
    policy_version: str
    policy_generation: int
    authority: str
    authority_evidence_id: str
    authority_evidence_sha256: str
    authority_snapshot_id: str
    authority_snapshot_sha256: str
    policy_seal_sha256: str
    policy_envelope_sha256: str
    policy_payload_sha256: str
    decision_time: str
    resolved_policy: Dict[str, Any]
    resolution_digest_sha256: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "resolver_version": self.resolver_version,
            "binding_version": self.binding_version,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "policy_generation": self.policy_generation,
            "authority": self.authority,
            "authority_evidence_id": self.authority_evidence_id,
            "authority_evidence_sha256": self.authority_evidence_sha256,
            "authority_snapshot_id": self.authority_snapshot_id,
            "authority_snapshot_sha256": self.authority_snapshot_sha256,
            "policy_seal_sha256": self.policy_seal_sha256,
            "policy_envelope_sha256": self.policy_envelope_sha256,
            "policy_payload_sha256": self.policy_payload_sha256,
            "decision_time": self.decision_time,
            "resolved_policy": copy.deepcopy(self.resolved_policy),
            "resolution_digest_sha256": self.resolution_digest_sha256,
        }


class AllocationPolicyResolver:
    """Deterministic resolver for externally-issued allocation policy.

    Trust model:
      * this module NEVER issues production limits or authority evidence;
      * the caller supplies a frozen authority-evidence record, sealed policy envelope,
        and sealed authority snapshot from the authoritative control plane;
      * the resolver verifies exact content-addressed identities, authority scope,
        version/generation, predecessor chain, revocation state and validity windows;
      * the resolved payload is returned only if every check passes.

    SHA-256 seals provide immutability/integrity binding, not identity by themselves.
    Authenticity derives from the authoritative evidence/snapshot source supplied to the
    runtime by EQS-00 / Capital & Risk infrastructure.
    """

    def __init__(self, schema_dir: str | Path):
        self.schemas = SchemaRegistry(schema_dir)

    def _schema(self, name: str, obj: Mapping[str, Any]) -> None:
        errors = self.schemas.validate(name, dict(obj))
        if errors:
            raise PolicyResolutionError(f"{name} invalid: " + "; ".join(errors))

    @staticmethod
    def _window_contains(valid_from: str, valid_until: str, moment: str, label: str) -> None:
        start = parse_ts(valid_from)
        end = parse_ts(valid_until)
        t = parse_ts(moment)
        if not start < end:
            raise PolicyResolutionError(f"{label} validity window is empty or reversed")
        if not (start <= t < end):
            raise PolicyResolutionError(f"{label} is not valid at {moment}")

    def resolve(
        self,
        *,
        decision_time: str,
        envelope: Mapping[str, Any],
        authority_evidence: Mapping[str, Any],
        authority_snapshot: Mapping[str, Any],
    ) -> ResolvedPolicyBinding:
        # Canonical serialization is the first trust boundary and rejects NaN/Infinity.
        canonical_json({
            "decision_time": decision_time,
            "envelope": envelope,
            "authority_evidence": authority_evidence,
            "authority_snapshot": authority_snapshot,
        })
        parse_ts(decision_time)

        self._schema("authority_evidence.schema.json", authority_evidence)
        self._schema("allocation_policy_envelope.schema.json", envelope)
        self._schema("policy_authority_snapshot.schema.json", authority_snapshot)

        if authority_evidence.get("evidence_version") != AUTHORITY_EVIDENCE_VERSION:
            raise PolicyResolutionError("authority evidence version mismatch")
        if envelope.get("binding_version") != POLICY_BINDING_VERSION:
            raise PolicyResolutionError("policy binding version mismatch")
        if authority_snapshot.get("snapshot_version") != POLICY_AUTHORITY_SNAPSHOT_VERSION:
            raise PolicyResolutionError("authority snapshot version mismatch")

        evidence_seal = seal_authority_evidence(authority_evidence)
        if evidence_seal != authority_evidence.get("evidence_seal_sha256"):
            raise PolicyResolutionError("authority evidence seal mismatch")
        evidence_sha = sha256_obj(dict(authority_evidence))

        policy_seal = seal_policy_envelope(envelope)
        if policy_seal != envelope.get("policy_seal_sha256"):
            raise PolicyResolutionError("policy envelope seal mismatch")
        envelope_sha = sha256_obj(dict(envelope))

        snapshot_seal = seal_authority_snapshot(authority_snapshot)
        if snapshot_seal != authority_snapshot.get("snapshot_seal_sha256"):
            raise PolicyResolutionError("authority snapshot seal mismatch")
        snapshot_sha = sha256_obj(dict(authority_snapshot))

        payload = envelope.get("payload")
        if not isinstance(payload, dict):
            raise PolicyResolutionError("policy payload missing")
        self._schema("fixed_risk_budget_policy.schema.json", payload)
        payload_sha = sha256_obj(payload)
        if payload_sha != envelope.get("payload_sha256"):
            raise PolicyResolutionError("policy payload hash mismatch")

        authority = envelope.get("authority")
        if authority not in AUTHORISED_PRODUCTION_AUTHORITIES:
            raise PolicyResolutionError("policy issuer is not authorised for production")
        if authority_evidence.get("authority") != authority:
            raise PolicyResolutionError("authority evidence issuer mismatch")
        if authority_snapshot.get("authority") != authority:
            raise PolicyResolutionError("authority snapshot issuer mismatch")
        if payload.get("authority") != authority:
            raise PolicyResolutionError("policy payload issuer mismatch")
        if payload.get("policy_mode") != "PRODUCTION" or envelope.get("policy_mode") != "PRODUCTION":
            raise PolicyResolutionError("authoritative resolver accepts PRODUCTION policies only")
        if payload.get("production_limits_authority") != "EQS-00/CAPITAL_RISK":
            raise PolicyResolutionError("production limit ownership is not EQS-00/CAPITAL_RISK")

        if envelope.get("authority_evidence_id") != authority_evidence.get("evidence_id"):
            raise PolicyResolutionError("policy authority evidence id mismatch")
        if envelope.get("authority_evidence_sha256") != evidence_sha:
            raise PolicyResolutionError("policy authority evidence hash mismatch")
        if payload.get("authority_ref") != authority_evidence.get("evidence_ref"):
            raise PolicyResolutionError("policy payload authority_ref mismatch")
        if payload.get("authority_sha256") != evidence_sha:
            raise PolicyResolutionError("policy payload authority_sha256 mismatch")

        if authority_snapshot.get("authority_evidence_id") != authority_evidence.get("evidence_id"):
            raise PolicyResolutionError("authority snapshot evidence id mismatch")
        if authority_snapshot.get("authority_evidence_sha256") != evidence_sha:
            raise PolicyResolutionError("authority snapshot evidence hash mismatch")
        if evidence_sha in set(authority_snapshot.get("revoked_authority_evidence_sha256", [])):
            raise PolicyResolutionError("authority evidence is revoked")

        for expected, actual, label in [
            (ALLOCATION_INTERFACE_VERSION, envelope.get("interface_version"), "envelope interface"),
            (FIXED_RISK_BUDGET_METHOD_VERSION, envelope.get("method_version"), "envelope method"),
            ("FIXED_RISK_BUDGET", envelope.get("policy_type"), "envelope policy type"),
            (envelope.get("interface_version"), payload.get("interface_version"), "payload interface"),
            (envelope.get("method_version"), payload.get("method_version"), "payload method"),
            (envelope.get("policy_type"), payload.get("policy_type"), "payload policy type"),
            (envelope.get("policy_id"), payload.get("policy_id"), "payload policy id"),
            (envelope.get("policy_version"), payload.get("policy_version"), "payload policy version"),
        ]:
            if expected != actual:
                raise PolicyResolutionError(f"{label} mismatch")

        if envelope.get("interface_version") not in authority_evidence.get("allowed_interface_versions", []):
            raise PolicyResolutionError("authority evidence does not permit allocation interface")
        if envelope.get("method_version") not in authority_evidence.get("allowed_method_versions", []):
            raise PolicyResolutionError("authority evidence does not permit allocation method")
        if envelope.get("policy_type") not in authority_evidence.get("allowed_policy_types", []):
            raise PolicyResolutionError("authority evidence does not permit policy type")

        issued_at = envelope["issued_at"]
        valid_from = envelope["valid_from"]
        valid_until = envelope["valid_until"]
        if parse_ts(issued_at) > parse_ts(valid_from):
            raise PolicyResolutionError("policy issued_at must be <= valid_from")
        self._window_contains(valid_from, valid_until, decision_time, "policy")
        self._window_contains(authority_evidence["valid_from"], authority_evidence["valid_until"], issued_at, "authority evidence at policy issue")
        self._window_contains(authority_evidence["valid_from"], authority_evidence["valid_until"], decision_time, "authority evidence")
        if parse_ts(authority_evidence["issued_at"]) > parse_ts(issued_at):
            raise PolicyResolutionError("policy predates its authority evidence")

        snapshot_time = authority_snapshot["as_of_time"]
        if parse_ts(snapshot_time) > parse_ts(decision_time):
            raise PolicyResolutionError("authority snapshot is from the future")
        self._window_contains(authority_snapshot["valid_from"], authority_snapshot["valid_until"], decision_time, "authority snapshot")

        generation = int(envelope["policy_generation"])
        previous = envelope.get("previous_policy_seal_sha256")
        if generation == 1 and previous is not None:
            raise PolicyResolutionError("generation 1 policy must not name a predecessor")
        if generation > 1 and not previous:
            raise PolicyResolutionError("policy generation >1 requires predecessor seal")

        exact_pointer_checks = [
            (authority_snapshot.get("active_policy_id"), envelope.get("policy_id"), "active policy id"),
            (authority_snapshot.get("active_policy_version"), envelope.get("policy_version"), "active policy version"),
            (authority_snapshot.get("active_policy_generation"), generation, "active policy generation"),
            (authority_snapshot.get("active_policy_seal_sha256"), policy_seal, "active policy seal"),
            (authority_snapshot.get("active_policy_envelope_sha256"), envelope_sha, "active policy envelope hash"),
            (authority_snapshot.get("predecessor_policy_seal_sha256"), previous, "predecessor policy seal"),
        ]
        for expected, actual, label in exact_pointer_checks:
            if expected != actual:
                raise PolicyResolutionError(f"authority snapshot {label} mismatch")

        if policy_seal in set(authority_snapshot.get("revoked_policy_seal_sha256", [])):
            raise PolicyResolutionError("active policy seal is revoked")

        core = {
            "resolver_version": POLICY_RESOLVER_VERSION,
            "binding_version": POLICY_BINDING_VERSION,
            "policy_id": envelope["policy_id"],
            "policy_version": envelope["policy_version"],
            "policy_generation": generation,
            "authority": authority,
            "authority_evidence_id": authority_evidence["evidence_id"],
            "authority_evidence_sha256": evidence_sha,
            "authority_snapshot_id": authority_snapshot["snapshot_id"],
            "authority_snapshot_sha256": snapshot_sha,
            "policy_seal_sha256": policy_seal,
            "policy_envelope_sha256": envelope_sha,
            "policy_payload_sha256": payload_sha,
            "decision_time": decision_time,
        }
        resolution_digest = domain_sha256("EQS-PC-POLICY-RESOLUTION-V1", core)
        result = ResolvedPolicyBinding(
            resolver_version=POLICY_RESOLVER_VERSION,
            binding_version=POLICY_BINDING_VERSION,
            policy_id=envelope["policy_id"],
            policy_version=envelope["policy_version"],
            policy_generation=generation,
            authority=authority,
            authority_evidence_id=authority_evidence["evidence_id"],
            authority_evidence_sha256=evidence_sha,
            authority_snapshot_id=authority_snapshot["snapshot_id"],
            authority_snapshot_sha256=snapshot_sha,
            policy_seal_sha256=policy_seal,
            policy_envelope_sha256=envelope_sha,
            policy_payload_sha256=payload_sha,
            decision_time=decision_time,
            resolved_policy=copy.deepcopy(payload),
            resolution_digest_sha256=resolution_digest,
        )
        self._schema("resolved_policy_binding.schema.json", result.to_dict())
        return result
    def resolve_with_lifecycle(
        self,
        *,
        decision_time: str,
        envelope: Mapping[str, Any],
        authority_evidence: Mapping[str, Any],
        authority_snapshot: Mapping[str, Any],
        lifecycle_events: Any,
    ) -> ResolvedPolicyBinding:
        """Resolve production policy only after immutable lifecycle replay.

        This is the runtime-facing authority path. The lower-level ``resolve`` method
        remains available for validating a frozen authority snapshot in isolation, while
        this method additionally proves that the snapshot is derivable from issuance /
        rotation / revocation lifecycle history.
        """
        from .policy_lifecycle import ImmutablePolicyLifecycle, PolicyLifecycleError

        if not isinstance(lifecycle_events, (list, tuple)) or not lifecycle_events:
            raise PolicyResolutionError("authoritative production policy requires policy lifecycle events")
        try:
            for i, event in enumerate(lifecycle_events):
                errors = self.schemas.validate("policy_lifecycle_event.schema.json", dict(event))
                if errors:
                    raise PolicyLifecycleError(f"lifecycle event {i+1} schema invalid: " + "; ".join(errors))
            lifecycle = ImmutablePolicyLifecycle.from_events(lifecycle_events)
            state = lifecycle.validate_authority_snapshot(authority_snapshot)
        except Exception as exc:
            if isinstance(exc, PolicyResolutionError):
                raise
            raise PolicyResolutionError(f"policy lifecycle validation failed: {exc}") from exc

        result = self.resolve(
            decision_time=decision_time,
            envelope=envelope,
            authority_evidence=authority_evidence,
            authority_snapshot=authority_snapshot,
        )
        if lifecycle.authority != result.authority or lifecycle.policy_id != result.policy_id:
            raise PolicyResolutionError("resolved policy identity does not match lifecycle identity")
        if state.active_policy_generation != result.policy_generation:
            raise PolicyResolutionError("resolved policy generation does not match lifecycle active generation")
        if state.active_policy_seal_sha256 != result.policy_seal_sha256:
            raise PolicyResolutionError("resolved policy seal does not match lifecycle active policy")
        if state.active_policy_envelope_sha256 != result.policy_envelope_sha256:
            raise PolicyResolutionError("resolved policy envelope does not match lifecycle active policy")
        return result


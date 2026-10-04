from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple
import copy
import hashlib
import json

from .validator import canonical_json, parse_ts, sha256_obj
from .policy_resolver import (
    POLICY_BINDING_VERSION,
    seal_authority_evidence,
    seal_policy_envelope,
    seal_authority_snapshot,
)

POLICY_LIFECYCLE_VERSION = "EQS-PC-POLICY-LIFECYCLE-v1"
POLICY_LIFECYCLE_EVENT_VERSION = "EQS-PC-POLICY-LIFECYCLE-EVENT-v1"
POLICY_LIFECYCLE_STATE_VERSION = "EQS-PC-POLICY-LIFECYCLE-STATE-v1"

EVENT_DOMAIN = b"EQS-PC-POLICY-LIFECYCLE-EVENT-V1\x00"
STATE_DOMAIN = b"EQS-PC-POLICY-LIFECYCLE-STATE-V1\x00"
EMPTY_HEAD = hashlib.sha256(b"EQS-PC-POLICY-LIFECYCLE-EMPTY-V1\x00").hexdigest()


class PolicyLifecycleError(ValueError):
    """Fail-closed policy lifecycle / generation-chain violation."""


def _domain_hash(domain: bytes, obj: Any) -> str:
    return hashlib.sha256(domain + canonical_json(obj)).hexdigest()


@dataclass(frozen=True)
class PolicyLifecycleEvent:
    event_version: str
    lifecycle_id: str
    sequence: int
    event_type: str
    occurred_at: str
    authority: str
    policy_id: str
    payload_json: str
    payload_sha256: str
    previous_event_sha256: str
    event_sha256: str

    @property
    def payload(self) -> Dict[str, Any]:
        return json.loads(self.payload_json)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_version": self.event_version,
            "lifecycle_id": self.lifecycle_id,
            "sequence": self.sequence,
            "event_type": self.event_type,
            "occurred_at": self.occurred_at,
            "authority": self.authority,
            "policy_id": self.policy_id,
            "payload": self.payload,
            "payload_sha256": self.payload_sha256,
            "previous_event_sha256": self.previous_event_sha256,
            "event_sha256": self.event_sha256,
        }

    @staticmethod
    def from_dict(obj: Mapping[str, Any]) -> "PolicyLifecycleEvent":
        payload_json = canonical_json(obj["payload"]).decode("utf-8")
        return PolicyLifecycleEvent(
            event_version=str(obj["event_version"]),
            lifecycle_id=str(obj["lifecycle_id"]),
            sequence=int(obj["sequence"]),
            event_type=str(obj["event_type"]),
            occurred_at=str(obj["occurred_at"]),
            authority=str(obj["authority"]),
            policy_id=str(obj["policy_id"]),
            payload_json=payload_json,
            payload_sha256=str(obj["payload_sha256"]),
            previous_event_sha256=str(obj["previous_event_sha256"]),
            event_sha256=str(obj["event_sha256"]),
        )


@dataclass(frozen=True)
class PolicyLifecycleState:
    state_version: str
    lifecycle_id: str
    authority: str
    policy_id: str
    as_of_time: str
    event_count: int
    lifecycle_head_sha256: str
    highest_issued_generation: int
    highest_effective_generation: int
    active_policy_version: Optional[str]
    active_policy_generation: Optional[int]
    active_policy_seal_sha256: Optional[str]
    active_policy_envelope_sha256: Optional[str]
    predecessor_policy_seal_sha256: Optional[str]
    revoked_policy_seal_sha256: Tuple[str, ...]
    state_sha256: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state_version": self.state_version,
            "lifecycle_id": self.lifecycle_id,
            "authority": self.authority,
            "policy_id": self.policy_id,
            "as_of_time": self.as_of_time,
            "event_count": self.event_count,
            "lifecycle_head_sha256": self.lifecycle_head_sha256,
            "highest_issued_generation": self.highest_issued_generation,
            "highest_effective_generation": self.highest_effective_generation,
            "active_policy_version": self.active_policy_version,
            "active_policy_generation": self.active_policy_generation,
            "active_policy_seal_sha256": self.active_policy_seal_sha256,
            "active_policy_envelope_sha256": self.active_policy_envelope_sha256,
            "predecessor_policy_seal_sha256": self.predecessor_policy_seal_sha256,
            "revoked_policy_seal_sha256": list(self.revoked_policy_seal_sha256),
            "state_sha256": self.state_sha256,
        }


@dataclass(frozen=True)
class ImmutablePolicyLifecycle:
    lifecycle_id: str
    authority: str
    policy_id: str
    events: Tuple[PolicyLifecycleEvent, ...] = ()

    @property
    def head_sha256(self) -> str:
        return self.events[-1].event_sha256 if self.events else EMPTY_HEAD

    @staticmethod
    def from_events(events: Sequence[Mapping[str, Any]]) -> "ImmutablePolicyLifecycle":
        if not events:
            raise PolicyLifecycleError("policy lifecycle requires at least one event")
        first = events[0]
        ledger = ImmutablePolicyLifecycle(
            lifecycle_id=str(first["lifecycle_id"]),
            authority=str(first["authority"]),
            policy_id=str(first["policy_id"]),
            events=tuple(PolicyLifecycleEvent.from_dict(x) for x in events),
        )
        ok, errors = ledger.verify()
        if not ok:
            raise PolicyLifecycleError("invalid policy lifecycle: " + "; ".join(errors))
        return ledger

    def _append(self, event_type: str, occurred_at: str, payload: Mapping[str, Any]) -> "ImmutablePolicyLifecycle":
        parse_ts(occurred_at)
        payload_json = canonical_json(dict(payload)).decode("utf-8")
        frozen_payload = json.loads(payload_json)
        payload_sha = sha256_obj(frozen_payload)
        core = {
            "event_version": POLICY_LIFECYCLE_EVENT_VERSION,
            "lifecycle_id": self.lifecycle_id,
            "sequence": len(self.events) + 1,
            "event_type": event_type,
            "occurred_at": occurred_at,
            "authority": self.authority,
            "policy_id": self.policy_id,
            "payload_sha256": payload_sha,
            "previous_event_sha256": self.head_sha256,
        }
        event = PolicyLifecycleEvent(
            event_version=POLICY_LIFECYCLE_EVENT_VERSION,
            lifecycle_id=self.lifecycle_id,
            sequence=core["sequence"],
            event_type=event_type,
            occurred_at=occurred_at,
            authority=self.authority,
            policy_id=self.policy_id,
            payload_json=payload_json,
            payload_sha256=payload_sha,
            previous_event_sha256=self.head_sha256,
            event_sha256=_domain_hash(EVENT_DOMAIN, core),
        )
        candidate = ImmutablePolicyLifecycle(
            lifecycle_id=self.lifecycle_id,
            authority=self.authority,
            policy_id=self.policy_id,
            events=self.events + (event,),
        )
        ok, errors = candidate.verify()
        if not ok:
            raise PolicyLifecycleError("event would violate lifecycle invariants: " + "; ".join(errors))
        return candidate

    @staticmethod
    def _issue_payload(envelope: Mapping[str, Any], authority_evidence: Mapping[str, Any]) -> Dict[str, Any]:
        policy_seal = seal_policy_envelope(envelope)
        if policy_seal != envelope.get("policy_seal_sha256"):
            raise PolicyLifecycleError("policy envelope seal mismatch")
        evidence_seal = seal_authority_evidence(authority_evidence)
        if evidence_seal != authority_evidence.get("evidence_seal_sha256"):
            raise PolicyLifecycleError("authority evidence seal mismatch")
        evidence_sha = sha256_obj(dict(authority_evidence))
        if envelope.get("authority_evidence_sha256") != evidence_sha:
            raise PolicyLifecycleError("policy authority evidence hash mismatch")
        if envelope.get("authority_evidence_id") != authority_evidence.get("evidence_id"):
            raise PolicyLifecycleError("policy authority evidence id mismatch")
        if envelope.get("authority") != authority_evidence.get("authority"):
            raise PolicyLifecycleError("policy/evidence authority mismatch")
        if envelope.get("binding_version") != POLICY_BINDING_VERSION:
            raise PolicyLifecycleError("policy binding version mismatch")
        return {
            "policy_version": envelope["policy_version"],
            "policy_generation": int(envelope["policy_generation"]),
            "issued_at": envelope["issued_at"],
            "valid_from": envelope["valid_from"],
            "valid_until": envelope["valid_until"],
            "policy_seal_sha256": policy_seal,
            "policy_envelope_sha256": sha256_obj(dict(envelope)),
            "authority_evidence_id": authority_evidence["evidence_id"],
            "authority_evidence_sha256": evidence_sha,
            "previous_policy_seal_sha256": envelope.get("previous_policy_seal_sha256"),
        }

    def record_issuance(self, *, envelope: Mapping[str, Any], authority_evidence: Mapping[str, Any]) -> "ImmutablePolicyLifecycle":
        if self.events:
            raise PolicyLifecycleError("POLICY_ISSUED is valid only for an empty lifecycle")
        if envelope.get("authority") != self.authority or envelope.get("policy_id") != self.policy_id:
            raise PolicyLifecycleError("issuance authority/policy identity mismatch")
        payload = self._issue_payload(envelope, authority_evidence)
        if payload["policy_generation"] != 1:
            raise PolicyLifecycleError("initial issuance must be generation 1")
        if payload["previous_policy_seal_sha256"] is not None:
            raise PolicyLifecycleError("generation 1 issuance must not name a predecessor")
        return self._append("POLICY_ISSUED", str(envelope["issued_at"]), payload)

    def record_rotation(self, *, envelope: Mapping[str, Any], authority_evidence: Mapping[str, Any]) -> "ImmutablePolicyLifecycle":
        if not self.events:
            raise PolicyLifecycleError("rotation requires an existing issuance")
        if envelope.get("authority") != self.authority or envelope.get("policy_id") != self.policy_id:
            raise PolicyLifecycleError("rotation authority/policy identity mismatch")
        payload = self._issue_payload(envelope, authority_evidence)
        issued = self._issued_rows()
        previous = issued[-1]
        if payload["policy_generation"] != previous["policy_generation"] + 1:
            raise PolicyLifecycleError("rotation must advance policy generation by exactly one")
        if payload["previous_policy_seal_sha256"] != previous["policy_seal_sha256"]:
            raise PolicyLifecycleError("rotation predecessor seal does not match prior generation")
        if parse_ts(payload["issued_at"]) < parse_ts(previous["issued_at"]):
            raise PolicyLifecycleError("rotation issuance time regresses")
        return self._append("POLICY_ROTATED", str(envelope["issued_at"]), payload)

    def record_revocation(
        self,
        *,
        policy_seal_sha256: str,
        effective_at: str,
        reason_code: str,
    ) -> "ImmutablePolicyLifecycle":
        if not self.events:
            raise PolicyLifecycleError("revocation requires an issued policy")
        if not reason_code:
            raise PolicyLifecycleError("revocation reason_code is required")
        issued = {row["policy_seal_sha256"]: row for row in self._issued_rows()}
        if policy_seal_sha256 not in issued:
            raise PolicyLifecycleError("cannot revoke an unknown policy seal")
        already = self._revocation_rows()
        if policy_seal_sha256 in {x["target_policy_seal_sha256"] for x in already}:
            raise PolicyLifecycleError("policy seal is already revoked")
        row = issued[policy_seal_sha256]
        if parse_ts(effective_at) < parse_ts(row["issued_at"]):
            raise PolicyLifecycleError("revocation predates policy issuance")
        payload = {
            "target_policy_version": row["policy_version"],
            "target_policy_generation": row["policy_generation"],
            "target_policy_seal_sha256": row["policy_seal_sha256"],
            "effective_at": effective_at,
            "reason_code": reason_code,
        }
        return self._append("POLICY_REVOKED", effective_at, payload)

    def _issued_rows(self) -> list[Dict[str, Any]]:
        return [e.payload for e in self.events if e.event_type in {"POLICY_ISSUED", "POLICY_ROTATED"}]

    def _revocation_rows(self) -> list[Dict[str, Any]]:
        return [e.payload for e in self.events if e.event_type == "POLICY_REVOKED"]

    def verify(self) -> tuple[bool, list[str]]:
        errors: list[str] = []
        previous_hash = EMPTY_HEAD
        previous_time = None
        issued: list[Dict[str, Any]] = []
        revoked: set[str] = set()
        for expected_seq, event in enumerate(self.events, 1):
            prefix = f"event {expected_seq}"
            if event.event_version != POLICY_LIFECYCLE_EVENT_VERSION:
                errors.append(f"{prefix}: event_version mismatch")
            if event.lifecycle_id != self.lifecycle_id:
                errors.append(f"{prefix}: lifecycle_id mismatch")
            if event.authority != self.authority:
                errors.append(f"{prefix}: authority mismatch")
            if event.policy_id != self.policy_id:
                errors.append(f"{prefix}: policy_id mismatch")
            if event.sequence != expected_seq:
                errors.append(f"{prefix}: sequence mismatch")
            if event.previous_event_sha256 != previous_hash:
                errors.append(f"{prefix}: previous event hash mismatch")
            try:
                t = parse_ts(event.occurred_at)
                if previous_time is not None and t < previous_time:
                    errors.append(f"{prefix}: lifecycle time regresses")
                previous_time = t
            except Exception as exc:
                errors.append(f"{prefix}: invalid occurred_at: {exc}")
            try:
                payload = event.payload
                if sha256_obj(payload) != event.payload_sha256:
                    errors.append(f"{prefix}: payload hash mismatch")
            except Exception as exc:
                errors.append(f"{prefix}: invalid payload: {exc}")
                payload = {}
            core = {
                "event_version": event.event_version,
                "lifecycle_id": event.lifecycle_id,
                "sequence": event.sequence,
                "event_type": event.event_type,
                "occurred_at": event.occurred_at,
                "authority": event.authority,
                "policy_id": event.policy_id,
                "payload_sha256": event.payload_sha256,
                "previous_event_sha256": event.previous_event_sha256,
            }
            if _domain_hash(EVENT_DOMAIN, core) != event.event_sha256:
                errors.append(f"{prefix}: event hash mismatch")

            if event.event_type in {"POLICY_ISSUED", "POLICY_ROTATED"}:
                try:
                    gen = int(payload["policy_generation"])
                    if parse_ts(payload["issued_at"]) != parse_ts(event.occurred_at):
                        errors.append(f"{prefix}: event time must equal policy issued_at")
                    if not parse_ts(payload["valid_from"]) < parse_ts(payload["valid_until"]):
                        errors.append(f"{prefix}: invalid policy validity window")
                    if event.event_type == "POLICY_ISSUED":
                        if expected_seq != 1 or issued:
                            errors.append(f"{prefix}: issuance must be first policy event")
                        if gen != 1:
                            errors.append(f"{prefix}: initial generation must be 1")
                        if payload.get("previous_policy_seal_sha256") is not None:
                            errors.append(f"{prefix}: generation 1 predecessor must be null")
                    else:
                        if not issued:
                            errors.append(f"{prefix}: rotation without prior issuance")
                        else:
                            prev = issued[-1]
                            if gen != int(prev["policy_generation"]) + 1:
                                errors.append(f"{prefix}: generation must advance exactly one")
                            if payload.get("previous_policy_seal_sha256") != prev.get("policy_seal_sha256"):
                                errors.append(f"{prefix}: predecessor seal mismatch")
                    if any(int(x["policy_generation"]) == gen for x in issued):
                        errors.append(f"{prefix}: duplicate policy generation")
                    if any(x["policy_seal_sha256"] == payload.get("policy_seal_sha256") for x in issued):
                        errors.append(f"{prefix}: duplicate policy seal")
                    issued.append(payload)
                except Exception as exc:
                    errors.append(f"{prefix}: malformed issuance/rotation payload: {exc}")
            elif event.event_type == "POLICY_REVOKED":
                target = payload.get("target_policy_seal_sha256")
                known = {x.get("policy_seal_sha256"): x for x in issued}
                if target not in known:
                    errors.append(f"{prefix}: revocation targets unknown policy seal")
                else:
                    target_row = known[target]
                    if payload.get("target_policy_generation") != target_row.get("policy_generation"):
                        errors.append(f"{prefix}: revocation generation mismatch")
                    if payload.get("target_policy_version") != target_row.get("policy_version"):
                        errors.append(f"{prefix}: revocation version mismatch")
                    try:
                        if parse_ts(payload.get("effective_at")) != parse_ts(event.occurred_at):
                            errors.append(f"{prefix}: effective_at must equal event occurred_at")
                        if parse_ts(event.occurred_at) < parse_ts(target_row["issued_at"]):
                            errors.append(f"{prefix}: revocation predates issuance")
                    except Exception as exc:
                        errors.append(f"{prefix}: invalid revocation time: {exc}")
                if not payload.get("reason_code"):
                    errors.append(f"{prefix}: revocation reason_code missing")
                if target in revoked:
                    errors.append(f"{prefix}: duplicate revocation")
                if target:
                    revoked.add(target)
            else:
                errors.append(f"{prefix}: unsupported event_type {event.event_type!r}")
            previous_hash = event.event_sha256
        return (not errors, errors)

    def state_at(self, as_of_time: str) -> PolicyLifecycleState:
        ok, errors = self.verify()
        if not ok:
            raise PolicyLifecycleError("cannot replay invalid lifecycle: " + "; ".join(errors))
        t = parse_ts(as_of_time)
        issued_rows = self._issued_rows()
        issued_by_seal = {x["policy_seal_sha256"]: x for x in issued_rows}
        effective_revoked: set[str] = set()
        for event in self.events:
            if event.event_type == "POLICY_REVOKED" and parse_ts(event.occurred_at) <= t:
                effective_revoked.add(event.payload["target_policy_seal_sha256"])

        effective = [x for x in issued_rows if parse_ts(x["valid_from"]) <= t]
        highest_effective = max((int(x["policy_generation"]) for x in effective), default=0)
        highest_issued = max((int(x["policy_generation"]) for x in issued_rows), default=0)

        # Anti-rollback rule: once generation N is effective, revoking N never silently
        # reactivates generation N-1. A higher generation must become effective.
        active = None
        if highest_effective:
            highest_rows = [x for x in effective if int(x["policy_generation"]) == highest_effective]
            candidate = highest_rows[0]
            if candidate["policy_seal_sha256"] not in effective_revoked and t < parse_ts(candidate["valid_until"]):
                active = candidate

        core = {
            "state_version": POLICY_LIFECYCLE_STATE_VERSION,
            "lifecycle_id": self.lifecycle_id,
            "authority": self.authority,
            "policy_id": self.policy_id,
            "as_of_time": as_of_time,
            "event_count": len(self.events),
            "lifecycle_head_sha256": self.head_sha256,
            "highest_issued_generation": highest_issued,
            "highest_effective_generation": highest_effective,
            "active_policy_version": active.get("policy_version") if active else None,
            "active_policy_generation": int(active["policy_generation"]) if active else None,
            "active_policy_seal_sha256": active.get("policy_seal_sha256") if active else None,
            "active_policy_envelope_sha256": active.get("policy_envelope_sha256") if active else None,
            "predecessor_policy_seal_sha256": active.get("previous_policy_seal_sha256") if active else None,
            "revoked_policy_seal_sha256": sorted(effective_revoked),
        }
        state_hash = _domain_hash(STATE_DOMAIN, core)
        return PolicyLifecycleState(
            state_version=core["state_version"], lifecycle_id=self.lifecycle_id,
            authority=self.authority, policy_id=self.policy_id, as_of_time=as_of_time,
            event_count=len(self.events), lifecycle_head_sha256=self.head_sha256,
            highest_issued_generation=highest_issued, highest_effective_generation=highest_effective,
            active_policy_version=core["active_policy_version"],
            active_policy_generation=core["active_policy_generation"],
            active_policy_seal_sha256=core["active_policy_seal_sha256"],
            active_policy_envelope_sha256=core["active_policy_envelope_sha256"],
            predecessor_policy_seal_sha256=core["predecessor_policy_seal_sha256"],
            revoked_policy_seal_sha256=tuple(core["revoked_policy_seal_sha256"]),
            state_sha256=state_hash,
        )

    def validate_authority_snapshot(self, snapshot: Mapping[str, Any]) -> PolicyLifecycleState:
        if seal_authority_snapshot(snapshot) != snapshot.get("snapshot_seal_sha256"):
            raise PolicyLifecycleError("authority snapshot seal mismatch")
        if snapshot.get("authority") != self.authority:
            raise PolicyLifecycleError("authority snapshot authority mismatch")
        state = self.state_at(str(snapshot["as_of_time"]))
        if state.active_policy_seal_sha256 is None:
            raise PolicyLifecycleError("no active non-revoked policy exists at authority snapshot time")
        checks = [
            (snapshot.get("active_policy_id"), self.policy_id, "active policy id"),
            (snapshot.get("active_policy_version"), state.active_policy_version, "active policy version"),
            (snapshot.get("active_policy_generation"), state.active_policy_generation, "active policy generation"),
            (snapshot.get("active_policy_seal_sha256"), state.active_policy_seal_sha256, "active policy seal"),
            (snapshot.get("active_policy_envelope_sha256"), state.active_policy_envelope_sha256, "active policy envelope hash"),
            (snapshot.get("predecessor_policy_seal_sha256"), state.predecessor_policy_seal_sha256, "predecessor policy seal"),
            (sorted(snapshot.get("revoked_policy_seal_sha256", [])), list(state.revoked_policy_seal_sha256), "revoked policy set"),
        ]
        for actual, expected, label in checks:
            if actual != expected:
                raise PolicyLifecycleError(f"authority snapshot {label} is inconsistent with lifecycle replay")
        proof_checks = [
            (snapshot.get("lifecycle_id"), self.lifecycle_id, "lifecycle_id"),
            (snapshot.get("lifecycle_event_count"), len(self.events), "lifecycle_event_count"),
            (snapshot.get("lifecycle_head_sha256"), self.head_sha256, "lifecycle_head_sha256"),
            (snapshot.get("lifecycle_state_sha256"), state.state_sha256, "lifecycle_state_sha256"),
        ]
        for actual, expected, label in proof_checks:
            if actual != expected:
                raise PolicyLifecycleError(f"authority snapshot {label} proof mismatch")
        return state

    def to_event_dicts(self) -> list[Dict[str, Any]]:
        return [e.to_dict() for e in self.events]

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from threading import RLock
from typing import Any, Dict, Iterable, List, Optional, Tuple
import copy
import hashlib
import json

from .reservation_lease_renewal import validate_reservation_lease_renewal_approval

RESERVATION_LEASE_VERSION = "1.1.0"
RESERVATION_LEASE_EVENT_VERSION = "1.1.0"
RESERVATION_LEASE_LIFECYCLE_VERSION = "1.1.0"
LEASE_EVENT_DOMAIN = b"EQS-PC-RESERVATION-LEASE-EVENT-V1\x00"
LEASE_STATE_DOMAIN = b"EQS-PC-RESERVATION-LEASE-STATE-V1\x00"
LEASE_LIFECYCLE_DOMAIN = b"EQS-PC-RESERVATION-LEASE-LIFECYCLE-V1\x00"

TERMINAL_STATUSES = {"CONSUMED", "CANCELLED", "EXPIRED"}
EVENT_TYPES = {"ISSUED", "RENEWED", "CONSUMED", "CANCELLED", "EXPIRED"}


class ReservationLeaseError(ValueError):
    pass


def canonical_json(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def _hash(domain: bytes, obj: Any) -> str:
    return hashlib.sha256(domain + canonical_json(obj)).hexdigest()


def _parse_ts(v: str) -> datetime:
    dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ReservationLeaseError("timestamp must be timezone-aware")
    return dt.astimezone(timezone.utc)


def _stable_id(prefix: str, obj: Any, length: int = 20) -> str:
    return f"{prefix}-{_hash(LEASE_STATE_DOMAIN, obj)[:length]}"


@dataclass(frozen=True)
class ReservationLeaseEvent:
    schema_version: str
    lifecycle_id: str
    lease_id: str
    reservation_id: str
    reservation_request_id: str
    portfolio_run_id: str
    sequence: int
    event_type: str
    occurred_at: str
    expires_at: str
    approved_target_set_hash: str
    policy_generation_pin_sha256: Optional[str]
    execution_handoff_id: Optional[str]
    reason: Optional[str]
    renewal_approval: Optional[Dict[str, Any]]
    previous_event_sha256: str
    event_sha256: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ReservationLeaseState:
    schema_version: str
    lifecycle_id: str
    lease_id: str
    reservation_id: str
    reservation_request_id: str
    portfolio_run_id: str
    status: str
    issued_at: str
    initial_expires_at: str
    expires_at: str
    terminal_at: Optional[str]
    terminal_reason: Optional[str]
    execution_handoff_id: Optional[str]
    approved_target_set_hash: str
    policy_generation_pin_sha256: Optional[str]
    renewal_count: int
    total_extension_seconds: int
    renewal_bounds_sha256: Optional[str]
    last_renewal_approval_sha256: Optional[str]
    event_count: int
    issue_event_sha256: str
    head_sha256: str
    state_sha256: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ImmutableReservationLeaseLifecycle:
    lifecycle_id: str
    events: Tuple[ReservationLeaseEvent, ...] = ()

    @staticmethod
    def issue(
        *, reservation_id: str, reservation_request_id: str, portfolio_run_id: str,
        issued_at: str, expires_at: str, approved_target_set_hash: str,
        policy_generation_pin_sha256: Optional[str] = None,
    ) -> "ImmutableReservationLeaseLifecycle":
        if not reservation_id or not reservation_request_id or not portfolio_run_id:
            raise ReservationLeaseError("reservation_id, reservation_request_id and portfolio_run_id are required")
        if _parse_ts(issued_at) >= _parse_ts(expires_at):
            raise ReservationLeaseError("reservation lease must expire after issuance")
        identity = {
            "reservation_id": reservation_id,
            "reservation_request_id": reservation_request_id,
            "portfolio_run_id": portfolio_run_id,
            "approved_target_set_hash": approved_target_set_hash,
            "policy_generation_pin_sha256": policy_generation_pin_sha256,
        }
        lifecycle_id = _stable_id("RLL", identity)
        lease_id = _stable_id("RL", identity)
        core = {
            "schema_version": RESERVATION_LEASE_EVENT_VERSION,
            "lifecycle_id": lifecycle_id,
            "lease_id": lease_id,
            "reservation_id": reservation_id,
            "reservation_request_id": reservation_request_id,
            "portfolio_run_id": portfolio_run_id,
            "sequence": 1,
            "event_type": "ISSUED",
            "occurred_at": issued_at,
            "expires_at": expires_at,
            "approved_target_set_hash": approved_target_set_hash,
            "policy_generation_pin_sha256": policy_generation_pin_sha256,
            "execution_handoff_id": None,
            "reason": None,
            "renewal_approval": None,
            "previous_event_sha256": "0" * 64,
        }
        event = ReservationLeaseEvent(**core, event_sha256=_hash(LEASE_EVENT_DOMAIN, core))
        return ImmutableReservationLeaseLifecycle(lifecycle_id, (event,))

    @staticmethod
    def from_dict(obj: Dict[str, Any]) -> "ImmutableReservationLeaseLifecycle":
        try:
            events = tuple(ReservationLeaseEvent(**copy.deepcopy(e)) for e in obj["events"])
            life = ImmutableReservationLeaseLifecycle(obj["lifecycle_id"], events)
        except Exception as exc:
            raise ReservationLeaseError(f"invalid lifecycle object: {exc}") from exc
        ok, errors = life.verify()
        if not ok:
            raise ReservationLeaseError("invalid lease lifecycle: " + "; ".join(errors))
        expected = life.to_dict()
        if obj.get("state") != expected["state"]:
            raise ReservationLeaseError("reservation lease state does not match deterministic replay")
        if obj.get("lifecycle_sha256") != expected["lifecycle_sha256"]:
            raise ReservationLeaseError("reservation lease lifecycle seal mismatch")
        return life

    @property
    def head_sha256(self) -> str:
        return self.events[-1].event_sha256 if self.events else "0" * 64

    @property
    def lease_id(self) -> Optional[str]:
        return self.events[0].lease_id if self.events else None

    @property
    def reservation_id(self) -> Optional[str]:
        return self.events[0].reservation_id if self.events else None

    def _replay(self) -> Tuple[Dict[str, Any], List[str]]:
        errors: List[str] = []
        if not self.events:
            return {}, ["LEASE_LIFECYCLE_EMPTY"]
        first = self.events[0]
        if first.event_type != "ISSUED" or first.sequence != 1 or first.previous_event_sha256 != "0" * 64 or first.renewal_approval is not None:
            errors.append("LEASE_ISSUANCE_EVENT_INVALID")
        prior = "0" * 64
        terminal_count = 0
        last_time: Optional[datetime] = None
        current_expiry = first.expires_at
        renewal_count = 0
        total_extension_seconds = 0
        renewal_bounds_hash: Optional[str] = None
        last_renewal_approval_hash: Optional[str] = None
        terminal_at = None
        terminal_reason = None
        execution_handoff_id = None
        status = "ACTIVE"
        for idx, ev in enumerate(self.events, 1):
            if ev.lifecycle_id != self.lifecycle_id:
                errors.append(f"LEASE_LIFECYCLE_ID_MISMATCH:{idx}")
            if ev.sequence != idx:
                errors.append(f"LEASE_SEQUENCE_MISMATCH:{idx}:{ev.sequence}")
            if ev.event_type not in EVENT_TYPES:
                errors.append(f"LEASE_EVENT_TYPE_INVALID:{idx}:{ev.event_type}")
            if ev.previous_event_sha256 != prior:
                errors.append(f"LEASE_PREVIOUS_HASH_MISMATCH:{idx}")
            core = ev.to_dict(); core.pop("event_sha256", None)
            if ev.event_sha256 != _hash(LEASE_EVENT_DOMAIN, core):
                errors.append(f"LEASE_EVENT_HASH_MISMATCH:{idx}")
            if (ev.lease_id, ev.reservation_id, ev.reservation_request_id, ev.portfolio_run_id,
                ev.approved_target_set_hash, ev.policy_generation_pin_sha256) != (
                first.lease_id, first.reservation_id, first.reservation_request_id, first.portfolio_run_id,
                first.approved_target_set_hash, first.policy_generation_pin_sha256):
                errors.append(f"LEASE_IDENTITY_DRIFT:{idx}")
            try:
                t = _parse_ts(ev.occurred_at)
                if last_time is not None and t < last_time:
                    errors.append(f"LEASE_CHRONOLOGY_REGRESSION:{idx}")
                last_time = t
            except Exception as exc:
                errors.append(f"LEASE_TIMESTAMP_INVALID:{idx}:{exc}")
            if ev.event_type == "RENEWED":
                if terminal_count:
                    errors.append(f"LEASE_RENEWAL_AFTER_TERMINAL:{idx}")
                approval = ev.renewal_approval
                if not isinstance(approval, dict):
                    errors.append(f"LEASE_RENEWAL_APPROVAL_MISSING:{idx}")
                else:
                    renewal_errors = validate_reservation_lease_renewal_approval(
                        approval,
                        reservation_id=first.reservation_id,
                        reservation_request_id=first.reservation_request_id,
                        portfolio_run_id=first.portfolio_run_id,
                        lease_id=first.lease_id,
                        policy_generation_pin_sha256=first.policy_generation_pin_sha256,
                        approved_target_set_hash=first.approved_target_set_hash,
                        issued_at=first.occurred_at,
                        current_expires_at=current_expiry,
                        renewal_count=renewal_count,
                        total_extension_seconds=total_extension_seconds,
                        existing_renewal_bounds_sha256=renewal_bounds_hash,
                    )
                    errors.extend(f"LEASE_RENEWAL_INVALID:{idx}:{e}" for e in renewal_errors)
                    if ev.occurred_at != approval.get("reapproved_at"):
                        errors.append(f"LEASE_RENEWAL_EVENT_TIME_MISMATCH:{idx}")
                    if ev.expires_at != approval.get("new_expires_at"):
                        errors.append(f"LEASE_RENEWAL_EXPIRY_MISMATCH:{idx}")
                    if not renewal_errors:
                        current_expiry = approval["new_expires_at"]
                        renewal_count += 1
                        total_extension_seconds += int(approval["extension_seconds"])
                        renewal_bounds_hash = approval["renewal_bounds_sha256"]
                        last_renewal_approval_hash = approval["approval_sha256"]
            elif ev.event_type == "ISSUED":
                if idx != 1:
                    errors.append(f"LEASE_DUPLICATE_ISSUANCE:{idx}")
                if ev.expires_at != current_expiry:
                    errors.append(f"LEASE_ISSUE_EXPIRY_MISMATCH:{idx}")
            else:
                if ev.renewal_approval is not None:
                    errors.append(f"LEASE_NONRENEW_EVENT_HAS_RENEWAL_APPROVAL:{idx}")
                if ev.expires_at != current_expiry:
                    errors.append(f"LEASE_EXPIRY_CHAIN_DRIFT:{idx}")
                if ev.event_type in TERMINAL_STATUSES:
                    terminal_count += 1
                    status = ev.event_type
                    terminal_at = ev.occurred_at
                    terminal_reason = ev.reason
                    execution_handoff_id = ev.execution_handoff_id if ev.event_type == "CONSUMED" else None
                    if idx != len(self.events):
                        errors.append(f"LEASE_EVENT_AFTER_TERMINAL:{idx}")
            prior = ev.event_sha256
        if terminal_count > 1:
            errors.append("LEASE_MULTIPLE_TERMINAL_EVENTS")
        replay = {
            "status": status,
            "issued_at": first.occurred_at,
            "initial_expires_at": first.expires_at,
            "expires_at": current_expiry,
            "terminal_at": terminal_at,
            "terminal_reason": terminal_reason,
            "execution_handoff_id": execution_handoff_id,
            "renewal_count": renewal_count,
            "total_extension_seconds": total_extension_seconds,
            "renewal_bounds_sha256": renewal_bounds_hash,
            "last_renewal_approval_sha256": last_renewal_approval_hash,
        }
        return replay, errors

    def verify(self) -> Tuple[bool, List[str]]:
        _, errors = self._replay()
        return (not errors), errors

    def state(self) -> ReservationLeaseState:
        replay, errors = self._replay()
        if errors:
            raise ReservationLeaseError("invalid lease lifecycle: " + "; ".join(errors))
        first = self.events[0]
        core = {
            "schema_version": RESERVATION_LEASE_VERSION,
            "lifecycle_id": self.lifecycle_id,
            "lease_id": first.lease_id,
            "reservation_id": first.reservation_id,
            "reservation_request_id": first.reservation_request_id,
            "portfolio_run_id": first.portfolio_run_id,
            "status": replay["status"],
            "issued_at": replay["issued_at"],
            "initial_expires_at": replay["initial_expires_at"],
            "expires_at": replay["expires_at"],
            "terminal_at": replay["terminal_at"],
            "terminal_reason": replay["terminal_reason"],
            "execution_handoff_id": replay["execution_handoff_id"],
            "approved_target_set_hash": first.approved_target_set_hash,
            "policy_generation_pin_sha256": first.policy_generation_pin_sha256,
            "renewal_count": replay["renewal_count"],
            "total_extension_seconds": replay["total_extension_seconds"],
            "renewal_bounds_sha256": replay["renewal_bounds_sha256"],
            "last_renewal_approval_sha256": replay["last_renewal_approval_sha256"],
            "event_count": len(self.events),
            "issue_event_sha256": first.event_sha256,
            "head_sha256": self.head_sha256,
        }
        return ReservationLeaseState(**core, state_sha256=_hash(LEASE_STATE_DOMAIN, core))

    def renew(self, approval: Dict[str, Any]) -> "ImmutableReservationLeaseLifecycle":
        st = self.state()
        if st.status != "ACTIVE":
            raise ReservationLeaseError(f"reservation lease already terminal: {st.status}")
        errors = validate_reservation_lease_renewal_approval(
            approval,
            reservation_id=st.reservation_id,
            reservation_request_id=st.reservation_request_id,
            portfolio_run_id=st.portfolio_run_id,
            lease_id=st.lease_id,
            policy_generation_pin_sha256=st.policy_generation_pin_sha256,
            approved_target_set_hash=st.approved_target_set_hash,
            issued_at=st.issued_at,
            current_expires_at=st.expires_at,
            renewal_count=st.renewal_count,
            total_extension_seconds=st.total_extension_seconds,
            existing_renewal_bounds_sha256=st.renewal_bounds_sha256,
        )
        if errors:
            raise ReservationLeaseError("renewal approval invalid: " + "; ".join(errors))
        core = {
            "schema_version": RESERVATION_LEASE_EVENT_VERSION,
            "lifecycle_id": self.lifecycle_id,
            "lease_id": st.lease_id,
            "reservation_id": st.reservation_id,
            "reservation_request_id": st.reservation_request_id,
            "portfolio_run_id": st.portfolio_run_id,
            "sequence": len(self.events) + 1,
            "event_type": "RENEWED",
            "occurred_at": approval["reapproved_at"],
            "expires_at": approval["new_expires_at"],
            "approved_target_set_hash": st.approved_target_set_hash,
            "policy_generation_pin_sha256": st.policy_generation_pin_sha256,
            "execution_handoff_id": None,
            "reason": None,
            "renewal_approval": copy.deepcopy(approval),
            "previous_event_sha256": self.head_sha256,
        }
        ev = ReservationLeaseEvent(**core, event_sha256=_hash(LEASE_EVENT_DOMAIN, core))
        updated = ImmutableReservationLeaseLifecycle(self.lifecycle_id, self.events + (ev,))
        ok, verify_errors = updated.verify()
        if not ok:
            raise ReservationLeaseError("renewed lifecycle invalid: " + "; ".join(verify_errors))
        return updated

    def _terminal(self, event_type: str, occurred_at: str, *, execution_handoff_id: Optional[str] = None, reason: Optional[str] = None) -> "ImmutableReservationLeaseLifecycle":
        if event_type not in TERMINAL_STATUSES:
            raise ReservationLeaseError("terminal event type required")
        st = self.state()
        if st.status != "ACTIVE":
            raise ReservationLeaseError(f"reservation lease already terminal: {st.status}")
        when = _parse_ts(occurred_at); issued = _parse_ts(st.issued_at); expiry = _parse_ts(st.expires_at)
        if when < issued:
            raise ReservationLeaseError("terminal event cannot predate issuance")
        if event_type == "CONSUMED":
            if when >= expiry:
                raise ReservationLeaseError("expired reservation lease cannot be consumed")
            if not execution_handoff_id:
                raise ReservationLeaseError("consumption requires execution_handoff_id")
        elif event_type == "EXPIRED":
            if when < expiry:
                raise ReservationLeaseError("expiry event cannot precede expires_at")
            execution_handoff_id = None
        elif event_type == "CANCELLED":
            if when >= expiry:
                raise ReservationLeaseError("expired lease must terminate as EXPIRED, not CANCELLED")
            execution_handoff_id = None
        core = {
            "schema_version": RESERVATION_LEASE_EVENT_VERSION,
            "lifecycle_id": self.lifecycle_id,
            "lease_id": st.lease_id,
            "reservation_id": st.reservation_id,
            "reservation_request_id": st.reservation_request_id,
            "portfolio_run_id": st.portfolio_run_id,
            "sequence": len(self.events) + 1,
            "event_type": event_type,
            "occurred_at": occurred_at,
            "expires_at": st.expires_at,
            "approved_target_set_hash": st.approved_target_set_hash,
            "policy_generation_pin_sha256": st.policy_generation_pin_sha256,
            "execution_handoff_id": execution_handoff_id,
            "reason": reason,
            "renewal_approval": None,
            "previous_event_sha256": self.head_sha256,
        }
        ev = ReservationLeaseEvent(**core, event_sha256=_hash(LEASE_EVENT_DOMAIN, core))
        return ImmutableReservationLeaseLifecycle(self.lifecycle_id, self.events + (ev,))

    def consume(self, occurred_at: str, execution_handoff_id: str) -> "ImmutableReservationLeaseLifecycle":
        return self._terminal("CONSUMED", occurred_at, execution_handoff_id=execution_handoff_id)

    def cancel(self, occurred_at: str, reason: str) -> "ImmutableReservationLeaseLifecycle":
        return self._terminal("CANCELLED", occurred_at, reason=reason)

    def expire(self, occurred_at: str, reason: str = "LEASE_EXPIRY") -> "ImmutableReservationLeaseLifecycle":
        return self._terminal("EXPIRED", occurred_at, reason=reason)

    def to_dict(self) -> Dict[str, Any]:
        st = self.state()
        core = {
            "schema_version": RESERVATION_LEASE_LIFECYCLE_VERSION,
            "lifecycle_id": self.lifecycle_id,
            "events": [e.to_dict() for e in self.events],
            "state": st.to_dict(),
        }
        core["lifecycle_sha256"] = _hash(LEASE_LIFECYCLE_DOMAIN, core)
        return core


class ReservationLeaseRegistry:
    """Thread-safe live registry whose stored values are immutable lifecycle chains."""
    def __init__(self, lifecycles: Optional[Iterable[ImmutableReservationLeaseLifecycle]] = None):
        self._lock = RLock()
        self._by_reservation: Dict[str, ImmutableReservationLeaseLifecycle] = {}
        for life in lifecycles or ():
            self._by_reservation[str(life.reservation_id)] = life

    @classmethod
    def from_snapshot(cls, snapshot: Dict[str, Any]) -> "ReservationLeaseRegistry":
        core = {"schema_version": snapshot.get("schema_version"), "lifecycles": snapshot.get("lifecycles", [])}
        if snapshot.get("registry_sha256") != _hash(LEASE_LIFECYCLE_DOMAIN, core):
            raise ReservationLeaseError("reservation lease registry seal mismatch")
        lives = [ImmutableReservationLeaseLifecycle.from_dict(x) for x in core["lifecycles"]]
        ids = [x.reservation_id for x in lives]
        if len(ids) != len(set(ids)):
            raise ReservationLeaseError("duplicate reservation id in registry snapshot")
        return cls(lives)

    def reset(self) -> None:
        with self._lock:
            self._by_reservation.clear()

    def get(self, reservation_id: str) -> Optional[ImmutableReservationLeaseLifecycle]:
        with self._lock:
            return self._by_reservation.get(reservation_id)

    def issue(self, *, reservation_id: str, reservation_request_id: str, portfolio_run_id: str, issued_at: str,
              expires_at: str, approved_target_set_hash: str, policy_generation_pin_sha256: Optional[str]) -> ImmutableReservationLeaseLifecycle:
        with self._lock:
            existing = self._by_reservation.get(reservation_id)
            if existing is not None:
                st = existing.state()
                raise ReservationLeaseError(f"reservation id already issued with status {st.status}")
            life = ImmutableReservationLeaseLifecycle.issue(
                reservation_id=reservation_id, reservation_request_id=reservation_request_id,
                portfolio_run_id=portfolio_run_id, issued_at=issued_at, expires_at=expires_at,
                approved_target_set_hash=approved_target_set_hash,
                policy_generation_pin_sha256=policy_generation_pin_sha256,
            )
            self._by_reservation[reservation_id] = life
            return life

    def renew(self, reservation_id: str, approval: Dict[str, Any]) -> ImmutableReservationLeaseLifecycle:
        with self._lock:
            life = self._by_reservation.get(reservation_id)
            if life is None:
                raise ReservationLeaseError("reservation lease not issued")
            updated = life.renew(approval)
            self._by_reservation[reservation_id] = updated
            return updated

    def consume(self, reservation_id: str, occurred_at: str, execution_handoff_id: str) -> ImmutableReservationLeaseLifecycle:
        with self._lock:
            life = self._by_reservation.get(reservation_id)
            if life is None:
                raise ReservationLeaseError("reservation lease not issued")
            updated = life.consume(occurred_at, execution_handoff_id)
            self._by_reservation[reservation_id] = updated
            return updated

    def cancel(self, reservation_id: str, occurred_at: str, reason: str) -> ImmutableReservationLeaseLifecycle:
        with self._lock:
            life = self._by_reservation.get(reservation_id)
            if life is None:
                raise ReservationLeaseError("reservation lease not issued")
            updated = life.cancel(occurred_at, reason)
            self._by_reservation[reservation_id] = updated
            return updated

    def expire(self, reservation_id: str, occurred_at: str, reason: str = "LEASE_EXPIRY") -> ImmutableReservationLeaseLifecycle:
        with self._lock:
            life = self._by_reservation.get(reservation_id)
            if life is None:
                raise ReservationLeaseError("reservation lease not issued")
            updated = life.expire(occurred_at, reason)
            self._by_reservation[reservation_id] = updated
            return updated

    def sweep_expired(self, at: str) -> List[str]:
        expired: List[str] = []
        with self._lock:
            when = _parse_ts(at)
            for rid, life in list(self._by_reservation.items()):
                st = life.state()
                if st.status == "ACTIVE" and when >= _parse_ts(st.expires_at):
                    self._by_reservation[rid] = life.expire(at)
                    expired.append(rid)
        return sorted(expired)

    def terminal_reservation_ids(self) -> List[str]:
        with self._lock:
            return sorted(rid for rid, life in self._by_reservation.items() if life.state().status in TERMINAL_STATUSES)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            rows = [self._by_reservation[r].to_dict() for r in sorted(self._by_reservation)]
        core = {"schema_version": RESERVATION_LEASE_VERSION, "lifecycles": rows}
        core["registry_sha256"] = _hash(LEASE_LIFECYCLE_DOMAIN, core)
        return core

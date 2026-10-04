from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from hashlib import sha256
import json
from typing import Any


class ReconciliationWatchdogStatus(StrEnum):
    BASELINE_REQUIRED = "BASELINE_REQUIRED"
    HEALTHY = "HEALTHY"
    DUE = "DUE"
    MISSED_CADENCE = "MISSED_CADENCE"
    MAX_AGE_EXCEEDED = "MAX_AGE_EXCEEDED"


class ReconciliationWatchdogRecoveryPhase(StrEnum):
    NONE = "NONE"
    REQUIRE_FRESH_RECONCILIATION = "REQUIRE_FRESH_RECONCILIATION"
    REQUIRE_HEALTHY_FOLLOW_UP = "REQUIRE_HEALTHY_FOLLOW_UP"


LEGACY_WATCHDOG_SCHEMA_VERSION = "EQS-EXEC-RECON-WATCHDOG-v1.0"
HYSTERESIS_WATCHDOG_SCHEMA_VERSION = "EQS-EXEC-RECON-WATCHDOG-v1.1"
CURRENT_WATCHDOG_SCHEMA_VERSION = "EQS-EXEC-RECON-WATCHDOG-v1.2"
SUPPORTED_WATCHDOG_SCHEMA_VERSIONS = frozenset({
    LEGACY_WATCHDOG_SCHEMA_VERSION,
    HYSTERESIS_WATCHDOG_SCHEMA_VERSION,
    CURRENT_WATCHDOG_SCHEMA_VERSION,
})
_WATCHDOG_SCHEMA_RANK = {
    LEGACY_WATCHDOG_SCHEMA_VERSION: 0,
    HYSTERESIS_WATCHDOG_SCHEMA_VERSION: 1,
    CURRENT_WATCHDOG_SCHEMA_VERSION: 2,
}


def _wire_time(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("watchdog timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_wire_time(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("watchdog timestamps must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _hash(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return sha256(raw).hexdigest()


@dataclass(frozen=True, slots=True)
class ReconciliationCadencePolicy:
    policy_id: str
    venue_id: str
    connection_id: str
    cadence_seconds: int
    maximum_age_seconds: int
    anchor_at: datetime
    trading_capable: bool
    schema_version: str = field(default="EQS-EXEC-RECON-WATCHDOG-POLICY-v1.0", init=False)

    def __post_init__(self) -> None:
        if not self.policy_id.strip() or not self.venue_id.strip() or not self.connection_id.strip():
            raise ValueError("watchdog policy identity fields are required")
        _wire_time(self.anchor_at)
        if self.cadence_seconds <= 0:
            raise ValueError("cadence_seconds must be positive")
        if self.maximum_age_seconds < self.cadence_seconds:
            raise ValueError("maximum_age_seconds must be >= cadence_seconds")

    def _base_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "policy_id": self.policy_id,
            "venue_id": self.venue_id,
            "connection_id": self.connection_id,
            "cadence_seconds": self.cadence_seconds,
            "maximum_age_seconds": self.maximum_age_seconds,
            "anchor_at": _wire_time(self.anchor_at),
            "trading_capable": self.trading_capable,
        }

    @property
    def payload_hash(self) -> str:
        return _hash(self._base_payload())

    def to_payload(self) -> dict[str, Any]:
        payload = self._base_payload()
        payload["payload_hash"] = self.payload_hash
        return payload


@dataclass(frozen=True, slots=True)
class ReconciliationWatchdogRecoveryContext:
    schema_version: str
    assessment_id: str
    assessed_at: datetime
    last_reconciliation_id: str | None
    predecessor_assessment_id: str | None
    status: ReconciliationWatchdogStatus
    commissioning_hold: bool
    recovery_phase: ReconciliationWatchdogRecoveryPhase
    recovery_breach_status: ReconciliationWatchdogStatus | None
    recovery_breach_at: datetime | None
    recovery_reconciliation_id: str | None


@dataclass(frozen=True, slots=True)
class ReconciliationWatchdogAssessment:
    assessment_id: str
    policy_id: str
    venue_id: str
    connection_id: str
    assessed_at: datetime
    last_reconciliation_id: str | None
    last_reconciliation_completed_at: datetime | None
    next_due_at: datetime
    age_seconds: int | None
    missed_intervals: int
    status: ReconciliationWatchdogStatus
    commissioning_hold: bool
    reason: str
    recovery_phase: ReconciliationWatchdogRecoveryPhase
    recovery_breach_status: ReconciliationWatchdogStatus | None
    recovery_breach_at: datetime | None
    recovery_reconciliation_id: str | None
    predecessor_assessment_id: str | None
    schema_version: str = field(default=CURRENT_WATCHDOG_SCHEMA_VERSION, init=False)

    def _base_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "assessment_id": self.assessment_id,
            "policy_id": self.policy_id,
            "venue_id": self.venue_id,
            "connection_id": self.connection_id,
            "assessed_at": _wire_time(self.assessed_at),
            "last_reconciliation_id": self.last_reconciliation_id,
            "predecessor_assessment_id": self.predecessor_assessment_id,
            "last_reconciliation_completed_at": None if self.last_reconciliation_completed_at is None else _wire_time(self.last_reconciliation_completed_at),
            "next_due_at": _wire_time(self.next_due_at),
            "age_seconds": self.age_seconds,
            "missed_intervals": self.missed_intervals,
            "status": self.status.value,
            "commissioning_hold": self.commissioning_hold,
            "reason": self.reason,
            "recovery_phase": self.recovery_phase.value,
            "recovery_breach_status": None if self.recovery_breach_status is None else self.recovery_breach_status.value,
            "recovery_breach_at": None if self.recovery_breach_at is None else _wire_time(self.recovery_breach_at),
            "recovery_reconciliation_id": self.recovery_reconciliation_id,
        }

    @property
    def payload_hash(self) -> str:
        return _hash(self._base_payload())

    def to_payload(self) -> dict[str, Any]:
        payload = self._base_payload()
        payload["payload_hash"] = self.payload_hash
        return payload


def recovery_context_from_payload(payload: dict[str, Any]) -> ReconciliationWatchdogRecoveryContext:
    schema_version = str(payload.get("schema_version", ""))
    if schema_version not in SUPPORTED_WATCHDOG_SCHEMA_VERSIONS:
        raise ValueError(f"unsupported watchdog schema version:{schema_version or 'MISSING'}")
    status = ReconciliationWatchdogStatus(str(payload.get("status")))
    assessed_at = _parse_wire_time(str(payload.get("assessed_at")))
    if assessed_at is None:
        raise ValueError("watchdog assessment assessed_at is required")
    if schema_version == LEGACY_WATCHDOG_SCHEMA_VERSION:
        recovery_phase = (
            ReconciliationWatchdogRecoveryPhase.REQUIRE_FRESH_RECONCILIATION
            if status in {ReconciliationWatchdogStatus.MISSED_CADENCE, ReconciliationWatchdogStatus.MAX_AGE_EXCEEDED}
            and bool(payload.get("commissioning_hold"))
            else ReconciliationWatchdogRecoveryPhase.NONE
        )
        breach_status = status if recovery_phase != ReconciliationWatchdogRecoveryPhase.NONE else None
        breach_at = assessed_at if breach_status is not None else None
        recovery_reconciliation_id = None
    else:
        recovery_phase = ReconciliationWatchdogRecoveryPhase(str(payload.get("recovery_phase", "NONE")))
        raw_breach_status = payload.get("recovery_breach_status")
        breach_status = None if raw_breach_status is None else ReconciliationWatchdogStatus(str(raw_breach_status))
        breach_at = _parse_wire_time(payload.get("recovery_breach_at"))
        recovery_reconciliation_id = None if payload.get("recovery_reconciliation_id") is None else str(payload.get("recovery_reconciliation_id"))
    predecessor_assessment_id = (
        None if schema_version != CURRENT_WATCHDOG_SCHEMA_VERSION or payload.get("predecessor_assessment_id") is None
        else str(payload.get("predecessor_assessment_id"))
    )
    return ReconciliationWatchdogRecoveryContext(
        schema_version=schema_version,
        assessment_id=str(payload.get("assessment_id", "")),
        assessed_at=assessed_at,
        last_reconciliation_id=None if payload.get("last_reconciliation_id") is None else str(payload.get("last_reconciliation_id")),
        predecessor_assessment_id=predecessor_assessment_id,
        status=status,
        commissioning_hold=bool(payload.get("commissioning_hold")),
        recovery_phase=recovery_phase,
        recovery_breach_status=breach_status,
        recovery_breach_at=breach_at,
        recovery_reconciliation_id=recovery_reconciliation_id,
    )



def advance_recovery_context(
    previous: ReconciliationWatchdogRecoveryContext | None,
    payload: dict[str, Any],
) -> ReconciliationWatchdogRecoveryContext:
    """Replay one watchdog record across schema versions without weakening recovery state.

    Legacy v1.0 did not encode recovery hysteresis.  A legacy breach therefore remains
    unresolved across later legacy non-breach rows until the current runtime observes a
    fresh reconciliation and emits current-version recovery evidence.  Once v1.1 has appeared for a
    scope, returning to v1.0 is a schema downgrade and is rejected.
    """
    current = recovery_context_from_payload(payload)
    if previous is not None and _WATCHDOG_SCHEMA_RANK[current.schema_version] < _WATCHDOG_SCHEMA_RANK[previous.schema_version]:
        raise ValueError(f"watchdog schema downgrade from {previous.schema_version} to {current.schema_version}")
    if (
        current.schema_version == LEGACY_WATCHDOG_SCHEMA_VERSION
        and previous is not None
        and previous.recovery_phase != ReconciliationWatchdogRecoveryPhase.NONE
        and current.recovery_phase == ReconciliationWatchdogRecoveryPhase.NONE
    ):
        return ReconciliationWatchdogRecoveryContext(
            schema_version=current.schema_version,
            assessment_id=current.assessment_id,
            assessed_at=current.assessed_at,
            last_reconciliation_id=current.last_reconciliation_id,
            predecessor_assessment_id=current.predecessor_assessment_id,
            status=current.status,
            commissioning_hold=True,
            recovery_phase=ReconciliationWatchdogRecoveryPhase.REQUIRE_FRESH_RECONCILIATION,
            recovery_breach_status=previous.recovery_breach_status,
            recovery_breach_at=previous.recovery_breach_at,
            recovery_reconciliation_id=None,
        )
    return current

def deterministic_policy_id(*, venue_id: str, connection_id: str, cadence_seconds: int, maximum_age_seconds: int, anchor_at: datetime, trading_capable: bool) -> str:
    payload = {
        "venue_id": venue_id,
        "connection_id": connection_id,
        "cadence_seconds": cadence_seconds,
        "maximum_age_seconds": maximum_age_seconds,
        "anchor_at": _wire_time(anchor_at),
        "trading_capable": trading_capable,
    }
    return "RCPOL-" + _hash(payload)[:48]


def make_reconciliation_policy(*, venue_id: str, connection_id: str, cadence_seconds: int, maximum_age_seconds: int, anchor_at: datetime, trading_capable: bool) -> ReconciliationCadencePolicy:
    return ReconciliationCadencePolicy(
        policy_id=deterministic_policy_id(
            venue_id=venue_id,
            connection_id=connection_id,
            cadence_seconds=cadence_seconds,
            maximum_age_seconds=maximum_age_seconds,
            anchor_at=anchor_at,
            trading_capable=trading_capable,
        ),
        venue_id=venue_id,
        connection_id=connection_id,
        cadence_seconds=cadence_seconds,
        maximum_age_seconds=maximum_age_seconds,
        anchor_at=anchor_at,
        trading_capable=trading_capable,
    )


def _timing_state(policy: ReconciliationCadencePolicy, *, assessed_at: datetime, last_reconciliation_completed_at: datetime | None) -> tuple[datetime, int | None, int, ReconciliationWatchdogStatus, bool, str]:
    _wire_time(assessed_at)
    if last_reconciliation_completed_at is not None:
        _wire_time(last_reconciliation_completed_at)
        if last_reconciliation_completed_at > assessed_at:
            raise ValueError("last reconciliation cannot be future-dated")
        next_due = last_reconciliation_completed_at + timedelta(seconds=policy.cadence_seconds)
        age_seconds = int((assessed_at - last_reconciliation_completed_at).total_seconds())
        missed_intervals = max(0, age_seconds // policy.cadence_seconds)
        if age_seconds > policy.maximum_age_seconds:
            return next_due, age_seconds, missed_intervals, ReconciliationWatchdogStatus.MAX_AGE_EXCEEDED, policy.trading_capable, "PRIVATE_RECONCILIATION_MAXIMUM_AGE_EXCEEDED"
        if assessed_at > next_due:
            return next_due, age_seconds, missed_intervals, ReconciliationWatchdogStatus.MISSED_CADENCE, policy.trading_capable, "PRIVATE_RECONCILIATION_CADENCE_MISSED"
        if assessed_at == next_due:
            return next_due, age_seconds, missed_intervals, ReconciliationWatchdogStatus.DUE, False, "PRIVATE_RECONCILIATION_DUE"
        return next_due, age_seconds, missed_intervals, ReconciliationWatchdogStatus.HEALTHY, False, "PRIVATE_RECONCILIATION_WITHIN_CADENCE"

    next_due = policy.anchor_at
    if assessed_at > policy.anchor_at:
        return next_due, None, 0, ReconciliationWatchdogStatus.MISSED_CADENCE, policy.trading_capable, "INITIAL_PRIVATE_RECONCILIATION_CADENCE_MISSED"
    if assessed_at == policy.anchor_at:
        return next_due, None, 0, ReconciliationWatchdogStatus.DUE, False, "INITIAL_PRIVATE_RECONCILIATION_DUE"
    return next_due, None, 0, ReconciliationWatchdogStatus.BASELINE_REQUIRED, False, "INITIAL_PRIVATE_RECONCILIATION_PENDING"


def assess_reconciliation_watchdog(
    policy: ReconciliationCadencePolicy,
    *,
    assessed_at: datetime,
    last_reconciliation_id: str | None,
    last_reconciliation_completed_at: datetime | None,
    last_reconciliation_complete: bool = True,
    previous_assessment: ReconciliationWatchdogRecoveryContext | None = None,
) -> ReconciliationWatchdogAssessment:
    next_due, age_seconds, missed_intervals, status, timing_hold, reason = _timing_state(
        policy, assessed_at=assessed_at, last_reconciliation_completed_at=last_reconciliation_completed_at
    )

    breach_status: ReconciliationWatchdogStatus | None = None
    breach_at: datetime | None = None
    recovery_reconciliation_id: str | None = None
    recovery_phase = ReconciliationWatchdogRecoveryPhase.NONE
    hold = timing_hold

    if status in {ReconciliationWatchdogStatus.MISSED_CADENCE, ReconciliationWatchdogStatus.MAX_AGE_EXCEEDED}:
        recovery_phase = ReconciliationWatchdogRecoveryPhase.REQUIRE_FRESH_RECONCILIATION
        breach_status = status
        breach_at = assessed_at
        hold = policy.trading_capable
    elif previous_assessment is not None and previous_assessment.recovery_phase == ReconciliationWatchdogRecoveryPhase.REQUIRE_FRESH_RECONCILIATION:
        breach_status = previous_assessment.recovery_breach_status
        breach_at = previous_assessment.recovery_breach_at
        fresh_reconciliation = (
            last_reconciliation_id is not None
            and last_reconciliation_id != previous_assessment.last_reconciliation_id
            and last_reconciliation_completed_at is not None
            and breach_at is not None
            and last_reconciliation_completed_at >= breach_at
        )
        if fresh_reconciliation and last_reconciliation_complete and status == ReconciliationWatchdogStatus.HEALTHY:
            recovery_phase = ReconciliationWatchdogRecoveryPhase.REQUIRE_HEALTHY_FOLLOW_UP
            recovery_reconciliation_id = last_reconciliation_id
            hold = policy.trading_capable
            reason = "PRIVATE_RECONCILIATION_RECOVERY_HEALTHY_FOLLOW_UP_REQUIRED"
        else:
            recovery_phase = ReconciliationWatchdogRecoveryPhase.REQUIRE_FRESH_RECONCILIATION
            hold = policy.trading_capable
            if fresh_reconciliation and not last_reconciliation_complete:
                reason = "PRIVATE_RECONCILIATION_RECOVERY_RECONCILIATION_INCOMPLETE"
            else:
                reason = "PRIVATE_RECONCILIATION_RECOVERY_FRESH_RECONCILIATION_REQUIRED"
    elif previous_assessment is not None and previous_assessment.recovery_phase == ReconciliationWatchdogRecoveryPhase.REQUIRE_HEALTHY_FOLLOW_UP:
        breach_status = previous_assessment.recovery_breach_status
        breach_at = previous_assessment.recovery_breach_at
        recovery_reconciliation_id = previous_assessment.recovery_reconciliation_id
        if not last_reconciliation_complete:
            recovery_phase = ReconciliationWatchdogRecoveryPhase.REQUIRE_FRESH_RECONCILIATION
            hold = policy.trading_capable
            recovery_reconciliation_id = None
            breach_at = assessed_at
            reason = "PRIVATE_RECONCILIATION_RECOVERY_RECONCILIATION_INCOMPLETE"
        elif status == ReconciliationWatchdogStatus.HEALTHY and assessed_at > previous_assessment.assessed_at:
            recovery_phase = ReconciliationWatchdogRecoveryPhase.NONE
            hold = False
            reason = "PRIVATE_RECONCILIATION_RECOVERY_CONFIRMED"
        else:
            recovery_phase = ReconciliationWatchdogRecoveryPhase.REQUIRE_HEALTHY_FOLLOW_UP
            hold = policy.trading_capable
            reason = "PRIVATE_RECONCILIATION_RECOVERY_HEALTHY_FOLLOW_UP_REQUIRED"

    predecessor_assessment_id = None if previous_assessment is None else previous_assessment.assessment_id
    core = {
        "policy_id": policy.policy_id,
        "assessed_at": _wire_time(assessed_at),
        "last_reconciliation_id": last_reconciliation_id,
        "last_reconciliation_completed_at": None if last_reconciliation_completed_at is None else _wire_time(last_reconciliation_completed_at),
        "next_due_at": _wire_time(next_due),
        "status": status.value,
        "commissioning_hold": hold,
        "recovery_phase": recovery_phase.value,
        "recovery_breach_status": None if breach_status is None else breach_status.value,
        "recovery_breach_at": None if breach_at is None else _wire_time(breach_at),
        "recovery_reconciliation_id": recovery_reconciliation_id,
    }
    assessment_id = "RCWD-" + _hash(core)[:48]
    return ReconciliationWatchdogAssessment(
        assessment_id=assessment_id,
        policy_id=policy.policy_id,
        venue_id=policy.venue_id,
        connection_id=policy.connection_id,
        assessed_at=assessed_at,
        last_reconciliation_id=last_reconciliation_id,
        last_reconciliation_completed_at=last_reconciliation_completed_at,
        next_due_at=next_due,
        age_seconds=age_seconds,
        missed_intervals=missed_intervals,
        status=status,
        commissioning_hold=hold,
        reason=reason,
        recovery_phase=recovery_phase,
        recovery_breach_status=breach_status,
        recovery_breach_at=breach_at,
        recovery_reconciliation_id=recovery_reconciliation_id,
        predecessor_assessment_id=predecessor_assessment_id,
    )


def hysteresis_v1_1_watchdog_payload(
    policy: ReconciliationCadencePolicy,
    *,
    assessed_at: datetime,
    last_reconciliation_id: str | None,
    last_reconciliation_completed_at: datetime | None,
    last_reconciliation_complete: bool = True,
    previous_assessment: ReconciliationWatchdogRecoveryContext | None = None,
) -> dict[str, Any]:
    """Render the historical v1.1 payload exactly, without v1.2 parent linkage."""
    current = assess_reconciliation_watchdog(
        policy, assessed_at=assessed_at, last_reconciliation_id=last_reconciliation_id,
        last_reconciliation_completed_at=last_reconciliation_completed_at,
        last_reconciliation_complete=last_reconciliation_complete, previous_assessment=previous_assessment,
    )
    payload = current._base_payload()
    payload.pop("predecessor_assessment_id", None)
    payload["schema_version"] = HYSTERESIS_WATCHDOG_SCHEMA_VERSION
    old_core = {
        "policy_id": policy.policy_id,
        "assessed_at": _wire_time(assessed_at),
        "last_reconciliation_id": last_reconciliation_id,
        "last_reconciliation_completed_at": None if last_reconciliation_completed_at is None else _wire_time(last_reconciliation_completed_at),
        "next_due_at": payload["next_due_at"],
        "status": payload["status"],
        "commissioning_hold": payload["commissioning_hold"],
        "recovery_phase": payload["recovery_phase"],
        "recovery_breach_status": payload["recovery_breach_status"],
        "recovery_breach_at": payload["recovery_breach_at"],
        "recovery_reconciliation_id": payload["recovery_reconciliation_id"],
    }
    payload["assessment_id"] = "RCWD-" + _hash(old_core)[:48]
    payload["payload_hash"] = _hash(payload)
    return payload


def legacy_reconciliation_watchdog_payload(
    policy: ReconciliationCadencePolicy,
    *,
    assessed_at: datetime,
    last_reconciliation_id: str | None,
    last_reconciliation_completed_at: datetime | None,
) -> dict[str, Any]:
    next_due, age_seconds, missed_intervals, status, hold, reason = _timing_state(
        policy, assessed_at=assessed_at, last_reconciliation_completed_at=last_reconciliation_completed_at
    )
    core = {
        "policy_id": policy.policy_id,
        "assessed_at": _wire_time(assessed_at),
        "last_reconciliation_id": last_reconciliation_id,
        "last_reconciliation_completed_at": None if last_reconciliation_completed_at is None else _wire_time(last_reconciliation_completed_at),
        "next_due_at": _wire_time(next_due),
        "status": status.value,
        "commissioning_hold": hold,
    }
    assessment_id = "RCWD-" + _hash(core)[:48]
    payload = {
        "schema_version": LEGACY_WATCHDOG_SCHEMA_VERSION,
        "assessment_id": assessment_id,
        "policy_id": policy.policy_id,
        "venue_id": policy.venue_id,
        "connection_id": policy.connection_id,
        "assessed_at": _wire_time(assessed_at),
        "last_reconciliation_id": last_reconciliation_id,
        "last_reconciliation_completed_at": None if last_reconciliation_completed_at is None else _wire_time(last_reconciliation_completed_at),
        "next_due_at": _wire_time(next_due),
        "age_seconds": age_seconds,
        "missed_intervals": missed_intervals,
        "status": status.value,
        "commissioning_hold": hold,
        "reason": reason,
    }
    payload["payload_hash"] = _hash(payload)
    return payload


def reconciliation_due(policy: ReconciliationCadencePolicy, *, at: datetime, last_reconciliation_completed_at: datetime | None) -> bool:
    _, _, _, status, _, _ = _timing_state(
        policy, assessed_at=at, last_reconciliation_completed_at=last_reconciliation_completed_at
    )
    return status in {
        ReconciliationWatchdogStatus.DUE,
        ReconciliationWatchdogStatus.MISSED_CADENCE,
        ReconciliationWatchdogStatus.MAX_AGE_EXCEEDED,
    }


__all__ = [
    "CURRENT_WATCHDOG_SCHEMA_VERSION", "HYSTERESIS_WATCHDOG_SCHEMA_VERSION", "LEGACY_WATCHDOG_SCHEMA_VERSION", "SUPPORTED_WATCHDOG_SCHEMA_VERSIONS",
    "ReconciliationCadencePolicy", "ReconciliationWatchdogAssessment", "ReconciliationWatchdogRecoveryContext",
    "ReconciliationWatchdogRecoveryPhase", "ReconciliationWatchdogStatus", "advance_recovery_context",
    "assess_reconciliation_watchdog", "deterministic_policy_id", "hysteresis_v1_1_watchdog_payload", "legacy_reconciliation_watchdog_payload",
    "make_reconciliation_policy", "reconciliation_due", "recovery_context_from_payload",
]

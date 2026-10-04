from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .models import ConnectionCapability, OrderIntent
from .venue_adapter import (
    AdapterResult,
    CanonicalVenueAdapter,
    ConstraintSnapshot,
    MarketSessionState,
    SessionState,
    VenueOperationalState,
    VenueState,
    validate_order_intent_against_constraints,
)


class V1PreDispatchError(PermissionError):
    """The canonical venue state does not safely permit this execution evaluation."""


@dataclass(frozen=True, slots=True)
class V1PreDispatchVerification:
    execution_intent_id: str
    connection_id: str
    venue_id: str
    instrument_id: str
    constraint_snapshot_id: str
    constraint_snapshot_hash: str
    constraint_source_ref: str
    constraint_source_version: str
    constraint_observed_at: datetime
    constraint_valid_until: datetime
    constraint_snapshot_payload: dict[str, Any]
    venue_state: VenueOperationalState
    venue_state_hash: str
    venue_observed_at: datetime
    venue_state_payload: dict[str, Any]
    session_state: SessionState
    session_state_hash: str
    session_observed_at: datetime
    session_label: str
    session_state_payload: dict[str, Any]
    verified_at: datetime
    state_max_age_seconds: int

    def to_payload(self) -> dict[str, Any]:
        return {
            "execution_intent_id": self.execution_intent_id,
            "connection_id": self.connection_id,
            "venue_id": self.venue_id,
            "instrument_id": self.instrument_id,
            "constraint_snapshot_id": self.constraint_snapshot_id,
            "constraint_snapshot_hash": self.constraint_snapshot_hash,
            "constraint_source_ref": self.constraint_source_ref,
            "constraint_source_version": self.constraint_source_version,
            "constraint_observed_at": self.constraint_observed_at.isoformat(),
            "constraint_valid_until": self.constraint_valid_until.isoformat(),
            "constraint_snapshot": self.constraint_snapshot_payload,
            "venue_state": self.venue_state.value,
            "venue_state_hash": self.venue_state_hash,
            "venue_observed_at": self.venue_observed_at.isoformat(),
            "venue_state_snapshot": self.venue_state_payload,
            "session_state": self.session_state.value,
            "session_state_hash": self.session_state_hash,
            "session_observed_at": self.session_observed_at.isoformat(),
            "session_label": self.session_label,
            "session_state_snapshot": self.session_state_payload,
            "verified_at": self.verified_at.isoformat(),
            "state_max_age_seconds": self.state_max_age_seconds,
        }


def _require_ok(name: str, result: AdapterResult[Any]) -> Any:
    if result.ok:
        return result.value
    assert result.error is not None
    raise V1PreDispatchError(
        f"{name} unavailable: {result.error.category.value}:{result.error.code}:{result.error.message}"
    )


def verify_v1_pre_dispatch(
    intent: OrderIntent,
    capability: ConnectionCapability,
    adapter: CanonicalVenueAdapter,
    *,
    at: datetime,
    state_max_age_seconds: int = 30,
) -> V1PreDispatchVerification:
    """Resolve and verify the canonical venue snapshots before engine dispatch.

    The adapter is the source of the three observations. Caller-supplied state flags are not
    trusted. This function authorizes only the existing non-live evaluation path; it does not
    grant venue submission authority.
    """
    if at.tzinfo is None or at.utcoffset() is None:
        raise V1PreDispatchError("pre-dispatch verification timestamp must be timezone-aware")
    if state_max_age_seconds <= 0:
        raise V1PreDispatchError("state_max_age_seconds must be positive")
    if adapter.connection_capability.to_payload() != capability.to_payload():
        raise V1PreDispatchError("venue adapter capability evidence does not match supplied connection capability")
    if capability.connection_id != intent.route.connection_id or capability.venue_id != intent.route.venue_id:
        raise V1PreDispatchError("venue adapter capability does not match intent route")

    constraint: ConstraintSnapshot = _require_ok(
        "instrument constraints",
        adapter.get_instrument_constraints(intent.instrument.instrument_id, at=at),
    )
    venue: VenueState = _require_ok("venue state", adapter.get_venue_state(at=at))
    session: MarketSessionState = _require_ok(
        "market session state",
        adapter.get_market_session_state(intent.instrument.instrument_id, at=at),
    )

    if constraint.venue_id != intent.route.venue_id or constraint.instrument_id != intent.instrument.instrument_id:
        raise V1PreDispatchError("constraint snapshot identity does not match intent route/instrument")
    validation = validate_order_intent_against_constraints(intent, constraint, checked_at=at)
    if not validation.accepted:
        raise V1PreDispatchError("constraint validation failed: " + ",".join(validation.violations))

    if venue.venue_id != intent.route.venue_id:
        raise V1PreDispatchError("venue state identity does not match intent venue")
    try:
        venue.assert_fresh(at, max_age_seconds=state_max_age_seconds)
    except ValueError as exc:
        raise V1PreDispatchError(str(exc)) from exc
    if venue.state != VenueOperationalState.TRADING:
        raise V1PreDispatchError(f"venue state is not dispatchable: {venue.state.value}:{venue.reason}")

    if session.venue_id != intent.route.venue_id or session.instrument_id != intent.instrument.instrument_id:
        raise V1PreDispatchError("market session identity does not match intent route/instrument")
    try:
        session.assert_fresh(at, max_age_seconds=state_max_age_seconds)
    except ValueError as exc:
        raise V1PreDispatchError(str(exc)) from exc
    if session.state != SessionState.OPEN:
        raise V1PreDispatchError(f"market session is not dispatchable: {session.state.value}")

    return V1PreDispatchVerification(
        execution_intent_id=intent.execution_intent_id,
        connection_id=capability.connection_id,
        venue_id=intent.route.venue_id,
        instrument_id=intent.instrument.instrument_id,
        constraint_snapshot_id=constraint.snapshot_id,
        constraint_snapshot_hash=constraint.payload_hash,
        constraint_source_ref=constraint.source_ref,
        constraint_source_version=constraint.source_version,
        constraint_observed_at=constraint.observed_at,
        constraint_valid_until=constraint.valid_until,
        constraint_snapshot_payload=constraint.to_payload(),
        venue_state=venue.state,
        venue_state_hash=venue.payload_hash,
        venue_observed_at=venue.observed_at,
        venue_state_payload=venue.to_payload(),
        session_state=session.state,
        session_state_hash=session.payload_hash,
        session_observed_at=session.observed_at,
        session_label=session.session_label,
        session_state_payload=session.to_payload(),
        verified_at=at,
        state_max_age_seconds=state_max_age_seconds,
    )


__all__ = ["V1PreDispatchError", "V1PreDispatchVerification", "verify_v1_pre_dispatch"]

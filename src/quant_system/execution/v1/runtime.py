from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from .models import (
    Capability,
    ConnectionCapability,
    ExecutionMode,
    InterlockState,
    OrderIntent,
    SubmissionInterlock,
)


class V1AuthorityError(PermissionError):
    """The supplied V1 authority objects do not authorize this non-live execution."""


class JournalTruthSource(StrEnum):
    SIMULATOR = "SIMULATOR"
    SHADOW_PREVIEW = "SHADOW_PREVIEW"
    VENUE = "VENUE"


@dataclass(frozen=True, slots=True)
class V1AuthorityVerification:
    execution_intent_id: str
    mode: ExecutionMode
    connection_id: str
    venue_id: str
    required_capability: Capability
    interlock_id: str
    interlock_revision: int
    interlock_state: InterlockState
    programme_state_ref: str
    interlock_seal_hash: str
    verified_at: datetime

    def to_payload(self) -> dict[str, Any]:
        return {
            "execution_intent_id": self.execution_intent_id,
            "mode": self.mode.value,
            "connection_id": self.connection_id,
            "venue_id": self.venue_id,
            "required_capability": self.required_capability.value,
            "interlock_id": self.interlock_id,
            "interlock_revision": self.interlock_revision,
            "interlock_state": self.interlock_state.value,
            "programme_state_ref": self.programme_state_ref,
            "interlock_seal_hash": self.interlock_seal_hash,
            "verified_at": self.verified_at.isoformat(),
        }


def verify_nonlive_execution_authority(
    intent: OrderIntent,
    capability: ConnectionCapability,
    interlock: SubmissionInterlock,
    *,
    runtime_mode: ExecutionMode,
    at: datetime,
    gateway_submission_enabled: bool,
) -> V1AuthorityVerification:
    """Verify the frozen V1 authority objects for PAPER/SHADOW without granting LIVE.

    This is deliberately stricter than SubmissionInterlock.assert_allows(): a PAPER
    runtime requires PAPER_ONLY and a SHADOW runtime requires SHADOW_ZERO_SUBMIT.
    Broader LIVE authority is not consumed by this non-live compatibility binding.
    """
    if at.tzinfo is None or at.utcoffset() is None:
        raise V1AuthorityError("authority verification timestamp must be timezone-aware")
    if runtime_mode == ExecutionMode.LIVE or intent.mode == ExecutionMode.LIVE:
        raise V1AuthorityError("non-live runtime binding cannot authorize LIVE")
    if intent.mode != runtime_mode:
        raise V1AuthorityError(f"intent mode {intent.mode.value} does not match runtime mode {runtime_mode.value}")
    if capability.connection_id != intent.route.connection_id:
        raise V1AuthorityError("connection capability does not match intent connection")
    if capability.venue_id != intent.route.venue_id:
        raise V1AuthorityError("connection capability does not match intent venue")
    if not capability.has(intent.route.required_capability):
        raise V1AuthorityError(f"connection lacks required capability {intent.route.required_capability.value}")
    if capability.verified_at > at:
        raise V1AuthorityError("connection capability verification timestamp is in the future")
    if capability.production_submission_enabled:
        raise V1AuthorityError("non-live runtime requires production submission disabled")
    if gateway_submission_enabled:
        raise V1AuthorityError("legacy gateway submission must remain disabled")
    if interlock.interlock_id != intent.authorisation.interlock_ref:
        raise V1AuthorityError("submission interlock does not match intent interlock_ref")
    if interlock.programme_state_ref != intent.authorisation.programme_state_ref:
        raise V1AuthorityError("submission interlock programme state does not match intent")

    expected_state = {
        ExecutionMode.PAPER: InterlockState.PAPER_ONLY,
        ExecutionMode.SHADOW: InterlockState.SHADOW_ZERO_SUBMIT,
    }[runtime_mode]
    if interlock.state != expected_state:
        raise V1AuthorityError(
            f"{runtime_mode.value} runtime requires exact interlock state {expected_state.value}, got {interlock.state.value}"
        )
    try:
        interlock.assert_allows(intent, at=at)
    except PermissionError as exc:
        raise V1AuthorityError(str(exc)) from exc

    return V1AuthorityVerification(
        execution_intent_id=intent.execution_intent_id,
        mode=runtime_mode,
        connection_id=capability.connection_id,
        venue_id=capability.venue_id,
        required_capability=intent.route.required_capability,
        interlock_id=interlock.interlock_id,
        interlock_revision=interlock.revision,
        interlock_state=interlock.state,
        programme_state_ref=interlock.programme_state_ref,
        interlock_seal_hash=interlock.seal_hash,
        verified_at=at,
    )

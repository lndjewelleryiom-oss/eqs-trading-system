from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid5

from quant_system.execution.models import OrderRequest
from quant_system.execution.reconciliation import ReconciliationAction
from quant_system.risk.authority import PreTradeRiskState
from quant_system.risk.contracts import CapitalMandate, RiskDirection, RiskDisposition, RiskReservation
from quant_system.risk.execution_bridge import (
    CapitalExecutionAuthorityV1,
    EQS07ExecutionTruthV2,
    ExecutionTruthConsumer,
)
from quant_system.risk.interfaces import ExecutionTruth
from quant_system.risk.nonlive_bridge import NonLiveCapitalAuthorisation, NonLiveCapitalBridge
from quant_system.runtime.orchestrator import PersistentPaperShadowRuntime, RuntimeMode


_SHADOW_EXEC_NS = UUID("d3c8bb1a-cc96-51c4-8ec9-7696e7d992d7")


@dataclass(frozen=True, slots=True)
class CapitalBoundRouteResult:
    authorisation: NonLiveCapitalAuthorisation
    authorised_order: OrderRequest | None
    runtime_result: object | None

class CapitalBoundNonLiveRouter:
    """EQS-06 -> durable PAPER/SHADOW runtime boundary. No LIVE route exists."""

    def __init__(
        self,
        *,
        runtime: PersistentPaperShadowRuntime,
        bridge: NonLiveCapitalBridge,
        truth_consumer: ExecutionTruthConsumer,
    ) -> None:
        if runtime.mode not in {RuntimeMode.PAPER, RuntimeMode.SHADOW}:
            raise ValueError("NONLIVE_RUNTIME_REQUIRED")
        self.runtime = runtime
        self.bridge = bridge
        self.truth = truth_consumer
        self._pending_shadow: dict[UUID, CapitalExecutionAuthorityV1] = {}

    @staticmethod
    def _lineage(auth: NonLiveCapitalAuthorisation) -> dict[str, object]:
        decision = auth.control.decision
        return {
            "capital_boundary": "EQS06_NONLIVE_V1",
            "risk_decision_id": str(decision.decision_id),
            "risk_disposition": decision.disposition.value,
            "risk_direction": decision.direction.value,
            "risk_state_ref": decision.input_state_ref,
            "policy_version": decision.policy_version,
        }

    def route(
        self,
        order: OrderRequest,
        mandate: CapitalMandate,
        state: PreTradeRiskState,
        *,
        strategy_version: str,
    ) -> CapitalBoundRouteResult:
        auth = self.bridge.authorise(
            order,
            mandate,
            state,
            strategy_version=strategy_version,
            execution_mode=self.runtime.mode.value,
        )
        decision = auth.control.decision
        if decision.disposition == RiskDisposition.REJECT:
            self.runtime.record_pipeline_event(
                "CAPITAL_AUTHORITY_REJECTED",
                self._lineage(auth) | {"order_id": str(order.order_id)},
            )
            return CapitalBoundRouteResult(auth, None, None)

        routed = auth.authorised_order
        if routed is None:
            raise RuntimeError("AUTHORISED_DECISION_WITHOUT_ORDER")
        lineage = self._lineage(auth)
        if auth.execution_authority is None:
            if decision.direction != RiskDirection.RISK_REDUCING:
                raise RuntimeError("NEW_RISK_WITHOUT_CAPITAL_AUTHORITY")
            result = self.runtime.submit_order(
                routed,
                lineage=lineage | {"capital_reservation": "NOT_REQUIRED_RISK_REDUCING"},
            )
            return CapitalBoundRouteResult(auth, routed, result)

        authority = auth.execution_authority
        result = self.runtime.submit_authorised_order(
            routed,
            authority,
            lineage=lineage,
        )
        if self.runtime.mode == RuntimeMode.SHADOW:
            self._pending_shadow[authority.reservation_id] = authority
        return CapitalBoundRouteResult(auth, routed, result)

    def recover_shadow_authority(self, authority: CapitalExecutionAuthorityV1) -> None:
        if self.runtime.mode != RuntimeMode.SHADOW:
            raise RuntimeError("SHADOW_RUNTIME_REQUIRED")
        if authority.execution_mode != RuntimeMode.SHADOW.value:
            raise ValueError("SHADOW_AUTHORITY_REQUIRED")
        cursor = self.truth.store.truth_cursor(authority.reservation_id)
        if cursor is not None and cursor["execution_state"] == ExecutionTruth.SUPPRESSED.value:
            return
        if self.runtime.shadow_engine is None or not self.runtime.shadow_engine.has_seen_order(authority.intent_id):
            raise RuntimeError("SHADOW_ORDER_NOT_PRESENT_AFTER_RECOVERY")
        self._pending_shadow[authority.reservation_id] = authority

    def settle_shadow_after_reconciliation(
        self,
        reservation_id: UUID,
        reconciliation: object,
        *,
        observed_at: datetime,
        evidence_ref: str,
    ) -> RiskReservation:

        if self.runtime.mode != RuntimeMode.SHADOW:
            raise RuntimeError("SHADOW_RUNTIME_REQUIRED")
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        if getattr(reconciliation, "action", None) != ReconciliationAction.OK:
            raise RuntimeError("SHADOW_RECONCILIATION_NOT_CLEAN")
        authority = self._pending_shadow.get(reservation_id)
        if authority is None:
            raise KeyError(str(reservation_id))
        if self.runtime.shadow_engine is None:
            raise RuntimeError("SHADOW_ENGINE_REQUIRED")
        if self.runtime.shadow_engine.gateway.venue_submission_enabled:
            raise RuntimeError("SHADOW_SUBMISSION_INVARIANT_BREACH")
        if not self.runtime.shadow_engine.has_seen_order(authority.intent_id):
            raise RuntimeError("SHADOW_ORDER_NOT_EVIDENCED")

        cursor = self.truth.store.truth_cursor(reservation_id)
        sequence = 1 if cursor is None else int(cursor["truth_sequence"]) + 1
        generation = 1 if cursor is None else int(cursor["reconciliation_generation"]) + 1
        truth = EQS07ExecutionTruthV2(
            reservation_id,
            uuid5(_SHADOW_EXEC_NS, str(authority.intent_id)),
            authority.intent_id,
            authority.venue,
            authority.instrument,
            ExecutionTruth.SUPPRESSED,
            Decimal("0"),
            sequence,
            generation,
            observed_at,
            evidence_ref,
        )

        settled = self.truth.apply(truth)
        self._pending_shadow.pop(reservation_id, None)
        self.runtime.record_pipeline_event(
            "SHADOW_CAPITAL_SETTLED",
            {
                "reservation_id": str(reservation_id),
                "execution_state": ExecutionTruth.SUPPRESSED.value,
                "truth_sequence": sequence,
                "reconciliation_generation": generation,
                "evidence_ref": evidence_ref,
                "venue_submission_enabled": False,
            },
        )
        return settled

from __future__ import annotations

from dataclasses import dataclass

from quant_system.execution.broker import BrokerGateway, SubmissionResult
from quant_system.execution.models import OrderRequest


@dataclass(frozen=True, slots=True)
class ShadowDecision:
    order: OrderRequest
    submission_result: SubmissionResult


@dataclass(frozen=True, slots=True)
class ShadowEngineState:
    decisions: tuple[ShadowDecision, ...]
    halted: bool
    halt_reason: str | None


class ShadowExecutionEngine:
    """Runs the broker-facing path while guaranteeing venue submission stays disabled."""

    def __init__(self, gateway: BrokerGateway):
        if gateway.venue_submission_enabled:
            raise ValueError("shadow engine requires venue submission to be disabled")
        self.gateway = gateway
        self._decisions: list[ShadowDecision] = []

    def evaluate_order(self, order: OrderRequest) -> ShadowDecision:
        if self.has_seen_order(order.order_id):
            raise ValueError("duplicate shadow order submission")
        if self.gateway.venue_submission_enabled:
            self.gateway.halt("SHADOW_SUBMISSION_INVARIANT_BREACH")
            raise RuntimeError("shadow engine detected venue submission enabled")
        result = self.gateway.submit(order)
        if result.sent:
            self.gateway.halt("SHADOW_SUBMISSION_INVARIANT_BREACH")
            raise RuntimeError("shadow engine observed an unexpected sent order")
        decision = ShadowDecision(order, result)
        self._decisions.append(decision)
        return decision

    def has_seen_order(self, order_id: object) -> bool:
        return any(item.order.order_id == order_id for item in self._decisions)

    @property
    def decisions(self) -> tuple[ShadowDecision, ...]:
        return tuple(self._decisions)

    def export_state(self) -> ShadowEngineState:
        return ShadowEngineState(tuple(self._decisions), self.gateway.halted, self.gateway.halt_reason)

    def restore_state(self, state: ShadowEngineState) -> None:
        if self.gateway.venue_submission_enabled:
            raise ValueError("cannot restore shadow state with venue submission enabled")
        order_ids = [item.order.order_id for item in state.decisions]
        if len(order_ids) != len(set(order_ids)):
            raise ValueError("shadow state contains duplicate order ids")
        self._decisions = list(state.decisions)
        if state.halted:
            self.gateway.halt(state.halt_reason or "RESTORED_HALTED_STATE")

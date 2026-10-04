from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
import json

from quant_system.core.enums import Side
from quant_system.execution.models import OrderRequest
from quant_system.risk.contracts import (
    CapitalMandate,
    LifecycleState,
    RiskDecisionV1,
    RiskDirection,
    RiskDisposition,
)

ENGINE_VERSION = "eqs-pretrade-authority-v1"
LIVE_STATES = {
    LifecycleState.LIVE_PROBATION,
    LifecycleState.LIVE_LIMITED,
    LifecycleState.LIVE_FULL,
}


@dataclass(frozen=True, slots=True)
class PreTradeRiskState:
    portfolio_id: str
    venue: str
    instrument: str
    position_quantity: Decimal | None
    gross_exposure: Decimal | None
    net_exposure: Decimal | None
    margin_usage: Decimal | None
    daily_loss: Decimal | None
    drawdown: Decimal | None
    turnover: Decimal | None
    open_order_notional: Decimal | None
    reserved_notional: Decimal | None
    state_time: datetime
    equity: Decimal | None = None
    available_capital: Decimal | None = None
    recent_order_count: int | None = None
    truth_valid_until: datetime | None = None
    account_truth_known: bool = True

    def __post_init__(self) -> None:
        if self.state_time.tzinfo is None or self.state_time.utcoffset() is None:
            raise ValueError("state_time must be timezone-aware")
        if self.truth_valid_until is not None and (
            self.truth_valid_until.tzinfo is None or self.truth_valid_until.utcoffset() is None
        ):
            raise ValueError("truth_valid_until must be timezone-aware")

    def fingerprint(self) -> str:
        payload = {}
        for name in self.__slots__:
            value = getattr(self, name)
            if isinstance(value, Decimal):
                value = str(value)
            elif isinstance(value, datetime):
                value = value.isoformat()
            payload[name] = value
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return sha256(raw).hexdigest()


class PreTradeAuthorityEngine:
    """External mandate gate. It has no exchange-submission capability."""

    def __init__(self, *, live_capital_enabled: bool = False) -> None:
        if live_capital_enabled:
            raise ValueError("LIVE capital authority is disabled")
        self.live_capital_enabled = False

    @staticmethod
    def classify_direction(
        order: OrderRequest, position: Decimal | None
    ) -> RiskDirection:
        if position is None:
            return RiskDirection.RISK_INCREASING
        signed_order = order.quantity if order.side == Side.BUY else -order.quantity
        projected = position + signed_order
        if abs(projected) < abs(position):
            return RiskDirection.RISK_REDUCING
        if abs(projected) == abs(position):
            return RiskDirection.RISK_NEUTRAL
        return RiskDirection.RISK_INCREASING

    def evaluate(
        self,
        order: OrderRequest,
        mandate: CapitalMandate,
        state: PreTradeRiskState,
    ) -> RiskDecisionV1:
        direction = self.classify_direction(order, state.position_quantity)
        now = order.decision_time
        reasons: list[str] = []
        evaluated = [
            "MANDATE_IDENTITY",
            "MANDATE_TIME",
            "EXECUTION_AUTHORITY",
            "ACCOUNT_TRUTH",
        ]
        if mandate.strategy_id != order.strategy_id:
            reasons.append("STRATEGY_NOT_AUTHORISED")
        if mandate.portfolio_id != state.portfolio_id:
            reasons.append("PORTFOLIO_MISMATCH")
        if state.venue not in mandate.permitted_venues:
            reasons.append("VENUE_NOT_AUTHORISED")
        if state.instrument not in mandate.permitted_instruments:
            reasons.append("INSTRUMENT_NOT_AUTHORISED")
        if not (mandate.valid_from <= now < mandate.valid_until):
            reasons.append("MANDATE_NOT_CURRENT")
        if now >= mandate.review_at:
            reasons.append("MANDATE_REVIEW_DUE")
        if mandate.lifecycle_state.value not in mandate.execution_authority:
            reasons.append("LIFECYCLE_NOT_AUTHORISED")
        if mandate.lifecycle_state in LIVE_STATES:
            reasons.append("LIVE_CAPITAL_DISABLED")
        if state.state_time > now:
            reasons.append("FUTURE_RISK_STATE")
        if state.truth_valid_until is None or now > state.truth_valid_until:
            reasons.append("RISK_STATE_STALE_OR_UNBOUNDED")
        if not state.account_truth_known:
            reasons.append("ACCOUNT_TRUTH_UNKNOWN")
        critical = (
            state.position_quantity,
            state.gross_exposure,
            state.net_exposure,
            state.margin_usage,
            state.daily_loss,
            state.drawdown,
            state.turnover,
            state.open_order_notional,
            state.reserved_notional,
            state.equity,
            state.available_capital,
            state.recent_order_count,
        )
        if any(value is None for value in critical):
            reasons.append("SAFETY_CRITICAL_STATE_UNKNOWN")
        if reasons:
            return self._decision(
                order, mandate, state, RiskDisposition.REJECT, direction,
                Decimal("0"), reasons, evaluated, (),
            )

        assert state.position_quantity is not None
        assert state.gross_exposure is not None
        assert state.margin_usage is not None
        assert state.daily_loss is not None
        assert state.drawdown is not None
        assert state.turnover is not None
        assert state.open_order_notional is not None
        assert state.reserved_notional is not None
        assert state.equity is not None
        assert state.available_capital is not None
        assert state.recent_order_count is not None

        if order.reduce_only and direction != RiskDirection.RISK_REDUCING:
            return self._decision(
                order, mandate, state, RiskDisposition.REJECT, direction,
                Decimal("0"), ["REDUCE_ONLY_WOULD_NOT_REDUCE"], evaluated, (),
            )
        if direction == RiskDirection.RISK_REDUCING:
            return self._decision(
                order, mandate, state, RiskDisposition.ALLOW, direction,
                order.quantity, ["RISK_REDUCING"], evaluated, (),
            )

        evaluated.extend(("DAILY_LOSS", "DRAWDOWN", "MARGIN", "TURNOVER",
                          "ORDER_RATE", "ORDER", "POSITION", "GROSS", "NET",
                          "LEVERAGE", "CONCENTRATION", "PROTECTED_RESERVE"))
        hard: list[str] = []
        if state.daily_loss >= mandate.daily_loss_limit:
            hard.append("DAILY_LOSS_LIMIT")
        if state.drawdown >= mandate.drawdown_limit:
            hard.append("DRAWDOWN_LIMIT")
        if state.margin_usage >= mandate.max_margin_usage:
            hard.append("MARGIN_USAGE_LIMIT")
        if state.turnover >= mandate.turnover_limit:
            hard.append("TURNOVER_LIMIT")
        if state.recent_order_count >= mandate.order_rate_limit:
            hard.append("ORDER_RATE_LIMIT")
        if state.equity <= 0:
            hard.append("NON_POSITIVE_EQUITY")
        if hard:
            return self._decision(
                order, mandate, state, RiskDisposition.REJECT, direction,
                Decimal("0"), hard, evaluated, (),
            )

        price = order.reference_price
        gross_headroom = max(
            Decimal("0"),
            mandate.max_gross_exposure - state.gross_exposure
            - state.open_order_notional - state.reserved_notional,
        )
        position_headroom = max(
            Decimal("0"),
            mandate.max_position_exposure - abs(state.position_quantity) * price,
        )
        net_headroom = max(Decimal("0"), mandate.max_net_exposure - abs(state.net_exposure))
        leverage_headroom = max(Decimal("0"), mandate.max_leverage * state.equity - state.gross_exposure)
        reserve_headroom = max(Decimal("0"), state.available_capital - mandate.protected_reserve)
        concentration_headroom = max(
            Decimal("0"), mandate.concentration_limit * mandate.capital_allocation
            - abs(state.position_quantity) * price,
        )
        capacity = min(mandate.max_order_notional, gross_headroom, position_headroom,
                       net_headroom, leverage_headroom, reserve_headroom, concentration_headroom)
        authorised = min(order.quantity, capacity / price)
        consumed = ("ORDER", "POSITION", "GROSS")

        if authorised <= 0:
            return self._decision(
                order, mandate, state, RiskDisposition.REJECT, direction,
                Decimal("0"), ["NO_RISK_CAPACITY"], evaluated, consumed,
            )
        if authorised < order.quantity:
            return self._decision(
                order, mandate, state, RiskDisposition.REDUCE, direction,
                authorised, ["RISK_CAPACITY_REDUCED"], evaluated, consumed,
            )
        return self._decision(
            order, mandate, state, RiskDisposition.ALLOW, direction,
            authorised, ["WITHIN_MANDATE"], evaluated, consumed,
        )

    @staticmethod
    def _decision(
        order: OrderRequest,
        mandate: CapitalMandate,
        state: PreTradeRiskState,
        disposition: RiskDisposition,
        direction: RiskDirection,
        quantity: Decimal,
        reasons: list[str],
        evaluated: list[str],
        consumed: tuple[str, ...],
    ) -> RiskDecisionV1:
        return RiskDecisionV1.create(
            intent_id=order.order_id,
            strategy_id=order.strategy_id,
            mandate_id=mandate.mandate_id,
            mandate_version=mandate.version,
            input_state_ref=state.fingerprint(),
            disposition=disposition,
            direction=direction,
            requested_quantity=order.quantity,
            authorised_quantity=quantity,
            reason_codes=tuple(reasons),
            limits_evaluated=tuple(evaluated),
            limits_consumed=consumed,
            decided_at=order.decision_time,
            risk_engine_version=ENGINE_VERSION,
            policy_version=mandate.policy_version,
        )

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from quant_system.risk.exposure import ExposureAccountant, ExposureLine, ExposureSnapshot


@dataclass(frozen=True, slots=True)
class PortfolioConstraints:
    max_committed_gross: Decimal | None
    max_abs_net: Decimal | None
    max_strategy_gross: Decimal | None
    max_instrument_gross: Decimal | None
    max_venue_gross: Decimal | None
    max_drawdown_fraction: Decimal | None

    def unset_reasons(self) -> tuple[str, ...]:
        pairs = (
            ("MAX_COMMITTED_GROSS_UNSET", self.max_committed_gross),
            ("MAX_ABS_NET_UNSET", self.max_abs_net),
            ("MAX_STRATEGY_GROSS_UNSET", self.max_strategy_gross),
            ("MAX_INSTRUMENT_GROSS_UNSET", self.max_instrument_gross),
            ("MAX_VENUE_GROSS_UNSET", self.max_venue_gross),
            ("MAX_DRAWDOWN_UNSET", self.max_drawdown_fraction),
        )
        return tuple(name for name, value in pairs if value is None)

@dataclass(frozen=True, slots=True)
class PortfolioSafetyState:
    global_kill: bool = False
    portfolio_kill: bool = False
    strategy_kills: frozenset[str] = frozenset()
    instrument_kills: frozenset[str] = frozenset()
    venue_kills: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class PortfolioState:
    lines: tuple[ExposureLine, ...]
    reserved_notional: Decimal
    open_order_notional: Decimal
    equity: Decimal
    peak_equity: Decimal


@dataclass(frozen=True, slots=True)
class CapitalAllocationRequest:
    request_id: UUID
    strategy_id: str
    venue: str
    instrument: str
    signed_notional: Decimal
    priority: int = 100

    @property
    def requested_notional(self) -> Decimal:
        return abs(self.signed_notional)

@dataclass(frozen=True, slots=True)
class PortfolioAllocationDecision:
    allowed: bool
    reasons: tuple[str, ...]
    projected: ExposureSnapshot | None


@dataclass(frozen=True, slots=True)
class ReservationArbitration:
    grants: dict[UUID, Decimal]
    reasons: dict[UUID, tuple[str, ...]]
    unused_capacity: Decimal | None


class PortfolioAllocationEngine:
    """Synthetic/PAPER portfolio authority. It never submits or creates LIVE authority."""

    @staticmethod
    def _gross(lines: tuple[ExposureLine, ...], attr: str) -> dict[str, Decimal]:
        result: dict[str, Decimal] = {}
        for line in lines:
            key = str(getattr(line, attr))
            result[key] = result.get(key, Decimal("0")) + abs(line.signed_notional)
        return result

    def evaluate(
        self,
        request: CapitalAllocationRequest,
        state: PortfolioState,
        constraints: PortfolioConstraints,
        safety: PortfolioSafetyState = PortfolioSafetyState(),
    ) -> PortfolioAllocationDecision:

        reasons = list(constraints.unset_reasons())
        if state.equity <= 0:
            reasons.append("NON_POSITIVE_EQUITY")
        if state.peak_equity <= 0:
            reasons.append("NON_POSITIVE_PEAK_EQUITY")
        if safety.global_kill:
            reasons.append("GLOBAL_KILL_ACTIVE")
        if safety.portfolio_kill:
            reasons.append("PORTFOLIO_KILL_ACTIVE")
        if request.strategy_id in safety.strategy_kills:
            reasons.append("STRATEGY_KILL_ACTIVE")
        if request.instrument in safety.instrument_kills:
            reasons.append("INSTRUMENT_KILL_ACTIVE")
        if request.venue in safety.venue_kills:
            reasons.append("VENUE_KILL_ACTIVE")
        if reasons:
            return PortfolioAllocationDecision(False, tuple(reasons), None)

        projected_lines = state.lines + (
            ExposureLine(
                request.strategy_id,
                request.venue,
                request.instrument,
                request.signed_notional,
            ),
        )
        projected = ExposureAccountant.compute(
            projected_lines,
            reserved=state.reserved_notional,
            open_order=state.open_order_notional,
        )

        strategy_gross = self._gross(projected_lines, "strategy_id")
        instrument_gross = self._gross(projected_lines, "instrument")
        venue_gross = self._gross(projected_lines, "venue")
        assert constraints.max_committed_gross is not None
        assert constraints.max_abs_net is not None
        assert constraints.max_strategy_gross is not None
        assert constraints.max_instrument_gross is not None
        assert constraints.max_venue_gross is not None
        assert constraints.max_drawdown_fraction is not None

        if projected.committed_gross > constraints.max_committed_gross:
            reasons.append("PORTFOLIO_GROSS_LIMIT")
        if abs(projected.net) > constraints.max_abs_net:
            reasons.append("PORTFOLIO_NET_LIMIT")
        if strategy_gross[request.strategy_id] > constraints.max_strategy_gross:
            reasons.append("STRATEGY_GROSS_LIMIT")
        if instrument_gross[request.instrument] > constraints.max_instrument_gross:
            reasons.append("INSTRUMENT_GROSS_LIMIT")
        if venue_gross[request.venue] > constraints.max_venue_gross:
            reasons.append("VENUE_GROSS_LIMIT")
        drawdown = (state.peak_equity - state.equity) / state.peak_equity
        if drawdown >= constraints.max_drawdown_fraction:
            reasons.append("PORTFOLIO_DRAWDOWN_LIMIT")
        return PortfolioAllocationDecision(
            not reasons, tuple(reasons) or ("WITHIN_PORTFOLIO_CONSTRAINTS",), projected
        )

    @staticmethod
    def arbitrate(
        requests: tuple[CapitalAllocationRequest, ...],
        *,
        available_capacity: Decimal | None,
    ) -> ReservationArbitration:
        if available_capacity is None:
            return ReservationArbitration(
                {},
                {request.request_id: ("PORTFOLIO_CAPACITY_UNSET",) for request in requests},
                None,
            )
        if available_capacity < 0:
            raise ValueError("available_capacity must be non-negative")

        remaining = available_capacity
        grants: dict[UUID, Decimal] = {}
        reasons: dict[UUID, tuple[str, ...]] = {}
        ordered = sorted(requests, key=lambda item: (item.priority, str(item.request_id)))
        for request in ordered:
            requested = request.requested_notional
            grant = min(requested, remaining)
            grants[request.request_id] = grant
            if grant == requested:
                reasons[request.request_id] = ("FULLY_GRANTED",)
            elif grant > 0:
                reasons[request.request_id] = ("PARTIALLY_GRANTED_CAPACITY_CONTENTION",)
            else:
                reasons[request.request_id] = ("REJECTED_CAPACITY_EXHAUSTED",)
            remaining -= grant
        return ReservationArbitration(grants, reasons, remaining)

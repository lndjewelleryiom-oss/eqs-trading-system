from decimal import Decimal
from uuid import UUID

from quant_system.risk.exposure import ExposureLine
from quant_system.risk.portfolio_control import (
    CapitalAllocationRequest,
    PortfolioAllocationEngine,
    PortfolioConstraints,
    PortfolioSafetyState,
    PortfolioState,
)


def constraints(**changes):
    base = dict(
        max_committed_gross=Decimal("1000"),
        max_abs_net=Decimal("500"),
        max_strategy_gross=Decimal("600"),
        max_instrument_gross=Decimal("500"),
        max_venue_gross=Decimal("800"),
        max_drawdown_fraction=Decimal("0.10"),
    )
    base.update(changes)
    return PortfolioConstraints(**base)


def state(**changes):
    base = dict(
        lines=(
            ExposureLine("s1", "V1", "X", Decimal("200")),
            ExposureLine("s2", "V2", "Y", Decimal("-100")),
        ),
        reserved_notional=Decimal("50"),
        open_order_notional=Decimal("25"),
        equity=Decimal("1000"),
        peak_equity=Decimal("1000"),
    )
    base.update(changes)
    return PortfolioState(**base)

def request(name="1", **changes):
    base = dict(
        request_id=UUID(f"00000000-0000-0000-0000-{int(name):012d}"),
        strategy_id="s1",
        venue="V1",
        instrument="X",
        signed_notional=Decimal("100"),
        priority=100,
    )
    base.update(changes)
    return CapitalAllocationRequest(**base)


def test_unset_constraints_fail_closed():
    c = constraints(max_venue_gross=None)
    decision = PortfolioAllocationEngine().evaluate(request(), state(), c)
    assert not decision.allowed
    assert decision.projected is None
    assert "MAX_VENUE_GROSS_UNSET" in decision.reasons


def test_cross_scope_limits_use_projected_portfolio_truth():
    decision = PortfolioAllocationEngine().evaluate(
        request(signed_notional=Decimal("350")),
        state(),
        constraints(max_instrument_gross=Decimal("500")),
    )
    assert not decision.allowed
    assert "INSTRUMENT_GROSS_LIMIT" in decision.reasons
    assert decision.projected is not None
    assert decision.projected.gross == Decimal("650")

def test_kills_block_without_mutating_exposure():
    decision = PortfolioAllocationEngine().evaluate(
        request(),
        state(),
        constraints(),
        PortfolioSafetyState(
            strategy_kills=frozenset({"s1"}),
            venue_kills=frozenset({"V1"}),
        ),
    )
    assert not decision.allowed
    assert set(decision.reasons) == {"STRATEGY_KILL_ACTIVE", "VENUE_KILL_ACTIVE"}
    assert decision.projected is None


def test_drawdown_blocks_even_when_notional_limits_pass():
    decision = PortfolioAllocationEngine().evaluate(
        request(signed_notional=Decimal("1")),
        state(equity=Decimal("899")),
        constraints(),
    )
    assert not decision.allowed
    assert "PORTFOLIO_DRAWDOWN_LIMIT" in decision.reasons


def test_valid_request_returns_deterministic_projected_exposure():
    decision = PortfolioAllocationEngine().evaluate(
        request(signed_notional=Decimal("50")),
        state(),
        constraints(),
    )
    assert decision.allowed
    assert decision.reasons == ("WITHIN_PORTFOLIO_CONSTRAINTS",)
    assert decision.projected.gross == Decimal("350")
    assert decision.projected.net == Decimal("150")
    assert decision.projected.committed_gross == Decimal("425")

def test_contention_arbitration_is_priority_then_id_deterministic():
    low = request("2", signed_notional=Decimal("80"), priority=20)
    high = request("1", signed_notional=Decimal("80"), priority=10)
    result = PortfolioAllocationEngine.arbitrate(
        (low, high), available_capacity=Decimal("100")
    )
    assert result.grants[high.request_id] == Decimal("80")
    assert result.grants[low.request_id] == Decimal("20")
    assert result.reasons[low.request_id] == ("PARTIALLY_GRANTED_CAPACITY_CONTENTION",)
    assert result.unused_capacity == 0


def test_unset_contention_capacity_blocks_all_requests():
    one = request("1")
    two = request("2")
    result = PortfolioAllocationEngine.arbitrate(
        (one, two), available_capacity=None
    )
    assert result.grants == {}
    assert result.unused_capacity is None
    assert result.reasons[one.request_id] == ("PORTFOLIO_CAPACITY_UNSET",)
    assert result.reasons[two.request_id] == ("PORTFOLIO_CAPACITY_UNSET",)

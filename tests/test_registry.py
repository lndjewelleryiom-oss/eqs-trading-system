import pytest

from quant_system.core.enums import StrategyState
from quant_system.registry.models import StrategyRecord, StrategySpec


def make_record():
    spec = StrategySpec.new(
        name="test", hypothesis="forced flow may mean revert", rationale="liquidity shock",
        asset_universe=("BTC-USD",), timeframe="1m", data_sources=("trades",),
        features=("imbalance",), entry_rules="x", exit_rules="y", position_sizing="z",
        expected_holding_period="5m", expected_transaction_cost_bps=5,
        expected_capacity_usd=100000, risks=("gap",), regime_dependencies=("high_vol",), code_version="abc123",
    )
    return StrategyRecord(spec)


def test_strategy_cannot_jump_from_proposed_to_live():
    record = make_record()
    with pytest.raises(ValueError):
        record.transition(StrategyState.LIVE_1)


def test_strategy_can_follow_research_path():
    record = make_record()
    for state in (StrategyState.EXPERIMENTAL, StrategyState.OOS_VALIDATED, StrategyState.PAPER, StrategyState.SHADOW, StrategyState.LIVE_1):
        record.transition(state)
    assert record.state == StrategyState.LIVE_1

from quant_system.registry.store import StrategyRegistry


def test_registry_records_transition_history_and_reason():
    record = make_record()
    registry = StrategyRegistry()
    registry.register(record.spec)
    registry.transition(record.spec.strategy_id, StrategyState.EXPERIMENTAL, reason="pre-registered experiment")
    history = registry.history(record.spec.strategy_id)
    assert len(history) == 2
    assert history[-1]["from_state"] == StrategyState.PROPOSED
    assert history[-1]["to_state"] == StrategyState.EXPERIMENTAL
    assert history[-1]["reason"] == "pre-registered experiment"


def test_registry_rejects_reasonless_transition():
    record = make_record()
    registry = StrategyRegistry()
    registry.register(record.spec)
    with pytest.raises(ValueError):
        registry.transition(record.spec.strategy_id, StrategyState.EXPERIMENTAL, reason="")

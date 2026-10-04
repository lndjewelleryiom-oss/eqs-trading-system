from __future__ import annotations
from datetime import timedelta
from decimal import Decimal
from quant_system.backtest.interfaces import ExecutionAssumptions
from quant_system.backtest.simulator import ConservativeBarExecutionSimulator
from quant_system.features import CryptoPerpetualFeatureEngine
from quant_system.nonlive import DeterministicInfrastructureTestStrategy,DeterministicTestStrategyConfig,NonLiveExecutionPipeline
from quant_system.paper import PaperTradingEngine
from quant_system.risk.policy import PortfolioRiskSnapshot,RiskEngine,RiskLimits

def execution_assumptions():
    return ExecutionAssumptions(commission_bps=Decimal("1"),spread_bps=Decimal("2"),slippage_bps=Decimal("1"),impact_bps=Decimal("1"),financing_bps_annual=Decimal("0"),borrow_bps_annual=Decimal("0"),latency_ms=0)

def paper_engine(initial_cash=Decimal("10000")):
    return PaperTradingEngine(ConservativeBarExecutionSimulator(execution_assumptions()),initial_cash=initial_cash)

def risk_engine(max_data_age=timedelta(seconds=30)):
    return RiskEngine(RiskLimits(max_order_notional=Decimal("1000000"),max_symbol_notional=Decimal("1000000"),max_strategy_notional=Decimal("1000000"),max_gross_notional=Decimal("1000000"),max_leverage=Decimal("100"),max_daily_loss=Decimal("5000"),max_drawdown_fraction=Decimal("0.50"),max_data_age=max_data_age))

def risk_snapshot(*,market_data_received_at,equity=Decimal("10000")):
    return PortfolioRiskSnapshot(equity=equity,peak_equity=equity,gross_notional=Decimal("0"),symbol_notional={},strategy_notional={},daily_pnl=Decimal("0"),market_data_received_at=market_data_received_at)

def pipeline(source,runtime,*,quantity=Decimal("0.01"),max_data_age=timedelta(seconds=30)):
    return NonLiveExecutionPipeline(source=source,feature_engine=CryptoPerpetualFeatureEngine(),strategy=DeterministicInfrastructureTestStrategy(DeterministicTestStrategyConfig(order_quantity=quantity)),risk_engine=risk_engine(max_data_age),runtime=runtime)

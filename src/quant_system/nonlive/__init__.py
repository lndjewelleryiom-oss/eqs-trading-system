"""Complete non-live R1.3 -> feature -> test signal -> risk -> PAPER/SHADOW path."""

from .capital_router import CapitalBoundNonLiveRouter, CapitalBoundRouteResult
from .pipeline import (
    DeterministicInfrastructureTestStrategy,
    DeterministicTestStrategyConfig,
    DuplicateMarketEventError,
    MarketDataAcceptanceGuard,
    MarketDataFault,
    MarketDataSequenceError,
    NonLiveExecutionPipeline,
    PipelineResult,
    RiskDecisionRecord,
    SignalAction,
    StrategyDecision,
    order_fingerprint,
    risk_snapshot_fingerprint,
)

__all__ = [
    "CapitalBoundNonLiveRouter",
    "CapitalBoundRouteResult",
    "DeterministicInfrastructureTestStrategy",
    "DeterministicTestStrategyConfig",
    "DuplicateMarketEventError",
    "MarketDataAcceptanceGuard",
    "MarketDataFault",
    "MarketDataSequenceError",
    "NonLiveExecutionPipeline",
    "PipelineResult",
    "RiskDecisionRecord",
    "SignalAction",
    "StrategyDecision",
    "order_fingerprint",
    "risk_snapshot_fingerprint",
]

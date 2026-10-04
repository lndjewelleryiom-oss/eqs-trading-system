import math

import pytest

from quant_system.research.report import ResearchReport
from quant_system.validation.scorecard import build_scorecard
from quant_system.validation.statistics import DeflatedSharpeResult, PBOResult, RealityCheckResult


def test_scorecard_computes_core_risk_and_trade_metrics():
    returns = [0.02, -0.01, 0.03, -0.005, 0.01, -0.02, 0.015, 0.01]
    card = build_scorecard(returns, periods_per_year=252, turnover=1.2, exposure=0.5, capacity=1_000_000)
    assert card.trade_count == len(returns)
    assert 0 < card.win_rate < 1
    assert card.max_drawdown > 0
    assert card.profit_factor > 1
    assert card.var_95 >= 0
    assert card.expected_shortfall_95 >= card.var_95
    assert card.turnover == 1.2


def test_scorecard_rejects_total_loss_return():
    with pytest.raises(ValueError, match="-100%"):
        build_scorecard([0.1, -1.0])


def test_research_report_explicitly_labels_stage_and_overfit_evidence():
    card = build_scorecard([0.01, -0.005, 0.012, 0.004])
    report = ResearchReport(
        strategy_id="SYNTH-1",
        hypothesis="Synthetic only",
        stage="SIMULATION",
        scorecard=card,
        deflated_sharpe=DeflatedSharpeResult(0.97, 0.2, 0.1, 0.03, 10, 100),
        pbo=PBOResult(0.1, (1.0,), 1),
        reality_check=RealityCheckResult(0.02, 0.001, 1000),
        weaknesses=("synthetic fixture",),
        failure_conditions=("edge disappears",),
    )
    text = report.to_markdown()
    assert "**Stage:** SIMULATION" in text
    assert "Deflated Sharpe probability" in text
    assert "synthetic fixture" in text

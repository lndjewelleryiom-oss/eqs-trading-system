from __future__ import annotations

import copy
import json
from pathlib import Path

from quant_system.research.feasibility_strategy_v2 import run_synthetic_shakedown
from quant_system.research.run_registry import StrategyRunRegistry
from quant_system.research.shakedown_gate_v2 import assess_shakedown, evaluate_registry_run


ROOT = Path(__file__).resolve().parents[1]
CAMPAIGN_PATH = ROOT / "research" / "preregistrations" / "v2" / "FEAS-BINANCE-BTC-MA-001.json"


def campaign() -> dict:
    return json.loads(CAMPAIGN_PATH.read_text(encoding="utf-8"))


def completed(tmp_path: Path):
    registry = StrategyRunRegistry(tmp_path / "runs.db")
    run_synthetic_shakedown(project_root=ROOT, registry=registry, run_id="gate-run")
    return registry


def test_completed_synthetic_run_is_only_nonqualifying_shakedown_eligible(tmp_path: Path) -> None:
    registry = completed(tmp_path)
    decision = evaluate_registry_run(registry, "gate-run", campaign_record=campaign())
    assert decision.status == "SHAKEDOWN_ELIGIBLE_NON_QUALIFYING"
    assert decision.eligible_non_qualifying_paper is True
    assert decision.completed_economic_trades >= 20
    assert decision.broker_submission_enabled is False
    assert decision.live_authority is False
    assert decision.counts_toward_168h_100_trade_gate is False
    assert decision.r13_certification_authority is False
    assert decision.j25_j26_candidate_authority is False


def test_gate_does_not_require_positive_pnl(tmp_path: Path) -> None:
    registry = completed(tmp_path)
    run = registry.get_run("gate-run")
    events = registry.events("gate-run", limit=5000)
    for event in events:
        if event["event_type"] == "RUN_FINISHED":
            event["payload"]["realized_pnl"] = "-999"
            event["payload"]["equity"] = "1"
    decision = assess_shakedown(
        run=run,
        events=events,
        campaign=campaign()["campaign"],
        campaign_fingerprint=campaign()["campaign_fingerprint"],
    )
    assert decision.eligible_non_qualifying_paper is True


def test_broker_submission_blocks_gate(tmp_path: Path) -> None:
    registry = completed(tmp_path)
    run = registry.get_run("gate-run")
    events = registry.events("gate-run", limit=5000)
    submitted = next(event for event in events if event["event_type"] == "ORDER_SUBMITTED")
    submitted["payload"]["broker_submitted"] = True
    decision = assess_shakedown(
        run=run,
        events=events,
        campaign=campaign()["campaign"],
        campaign_fingerprint=campaign()["campaign_fingerprint"],
    )
    assert decision.status == "BLOCKED"
    assert "BROKER_ORDER_SUBMISSION_OBSERVED" in decision.blockers


def test_forward_gate_credit_blocks_gate(tmp_path: Path) -> None:
    registry = completed(tmp_path)
    run = registry.get_run("gate-run")
    events = registry.events("gate-run", limit=5000)
    finish = next(event for event in events if event["event_type"] == "RUN_FINISHED")
    finish["payload"]["counts_toward_168h_100_trade_gate"] = True
    decision = assess_shakedown(
        run=run,
        events=events,
        campaign=campaign()["campaign"],
        campaign_fingerprint=campaign()["campaign_fingerprint"],
    )
    assert decision.status == "BLOCKED"
    assert "FINISH_FORWARD_CREDIT_INVALID" in decision.blockers


def test_campaign_fingerprint_mismatch_blocks_gate(tmp_path: Path) -> None:
    registry = completed(tmp_path)
    record = copy.deepcopy(campaign())
    record["campaign_fingerprint"] = "0" * 64
    decision = evaluate_registry_run(registry, "gate-run", campaign_record=record)
    assert decision.status == "BLOCKED"
    assert "CAMPAIGN_FINGERPRINT_MISMATCH" in decision.blockers

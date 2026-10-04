from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from typing import Any, Iterable

from quant_system.research.run_registry import StrategyRunRegistry


@dataclass(frozen=True, slots=True)
class ShakedownDecision:
    schema_id: str
    run_id: str
    campaign_id: str
    campaign_fingerprint: str
    status: str
    eligible_non_qualifying_paper: bool
    completed_economic_trades: int
    blockers: tuple[str, ...]
    broker_submission_enabled: bool
    live_authority: bool
    counts_toward_168h_100_trade_gate: bool
    r13_certification_authority: bool
    j25_j26_candidate_authority: bool

    def to_record(self) -> dict[str, object]:
        record = asdict(self)
        record["blockers"] = list(self.blockers)
        record["decision_sha256"] = sha256(
            json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        return record


def assess_shakedown(
    *,
    run: dict[str, Any],
    events: Iterable[dict[str, Any]],
    campaign: dict[str, Any],
    campaign_fingerprint: str,
) -> ShakedownDecision:
    blockers: list[str] = []
    event_list = list(events)
    campaign_id = str(campaign["campaign_id"])
    minimum_trades = int(campaign["shakedown_paper_gate"]["minimum_completed_validation_trades"])

    if run.get("strategy_id") != campaign_id:
        blockers.append("CAMPAIGN_ID_MISMATCH")
    if run.get("status") != "FINISHED" or run.get("result") != "SHAKEDOWN_MECHANICS_PASS":
        blockers.append("RUN_NOT_FINISHED_MECHANICS_PASS")
    if run.get("mode") != "RESEARCH_REPLAY":
        blockers.append("RUN_MODE_NOT_RESEARCH_REPLAY")
    if run.get("broker_submission_enabled") is not False:
        blockers.append("BROKER_SUBMISSION_NOT_FALSE")
    if run.get("live_authority") is not False:
        blockers.append("LIVE_AUTHORITY_NOT_FALSE")
    if run.get("fill_provenance") != "SIMULATED":
        blockers.append("FILL_PROVENANCE_NOT_SIMULATED")

    starts = [event for event in event_list if event.get("event_type") == "RUN_STARTED"]
    finishes = [event for event in event_list if event.get("event_type") == "RUN_FINISHED"]
    if len(starts) != 1:
        blockers.append("RUN_STARTED_EVENT_COUNT_INVALID")
    if len(finishes) != 1:
        blockers.append("RUN_FINISHED_EVENT_COUNT_INVALID")

    if starts:
        start = starts[0].get("payload", {})
        if start.get("classification") != "SHAKEDOWN_NON_QUALIFYING":
            blockers.append("START_CLASSIFICATION_INVALID")
        if start.get("campaign_fingerprint") != campaign_fingerprint:
            blockers.append("CAMPAIGN_FINGERPRINT_MISMATCH")
        if start.get("broker_submission_enabled") is not False:
            blockers.append("START_BROKER_BOUNDARY_INVALID")
        if start.get("live_authority") is not False:
            blockers.append("START_LIVE_BOUNDARY_INVALID")
        if start.get("counts_toward_168h_100_trade_gate") is not False:
            blockers.append("START_FORWARD_CREDIT_INVALID")

    completed_trades = sum(
        1
        for event in event_list
        if event.get("event_type") == "TRADE_CLOSED"
        and event.get("payload", {}).get("completed_economic_trade") is True
    )
    if completed_trades < minimum_trades:
        blockers.append("MINIMUM_SHAKEDOWN_TRADE_COUNT_NOT_MET")

    for event in event_list:
        payload = event.get("payload", {})
        if event.get("event_type") == "ORDER_SUBMITTED" and payload.get("broker_submitted") is not False:
            blockers.append("BROKER_ORDER_SUBMISSION_OBSERVED")
            break

    if finishes:
        finish = finishes[0].get("payload", {})
        if finish.get("classification") != "SHAKEDOWN_NON_QUALIFYING":
            blockers.append("FINISH_CLASSIFICATION_INVALID")
        if finish.get("journal_reconciled") is not True:
            blockers.append("ACCOUNTING_RECONCILIATION_NOT_PASS")
        if finish.get("broker_submission_enabled") is not False:
            blockers.append("FINISH_BROKER_BOUNDARY_INVALID")
        if finish.get("live_authority") is not False:
            blockers.append("FINISH_LIVE_BOUNDARY_INVALID")
        if finish.get("counts_toward_168h_100_trade_gate") is not False:
            blockers.append("FINISH_FORWARD_CREDIT_INVALID")
        if finish.get("eligible_for_r13_certification") is not False:
            blockers.append("R13_AUTHORITY_MUST_BE_FALSE")
        if finish.get("eligible_for_j25_j26") is not False:
            blockers.append("J25_J26_AUTHORITY_MUST_BE_FALSE")

    blockers = sorted(set(blockers))
    eligible = not blockers
    return ShakedownDecision(
        schema_id="EQS-SHAKEDOWN-PAPER-GATE-V2",
        run_id=str(run.get("run_id", "")),
        campaign_id=campaign_id,
        campaign_fingerprint=campaign_fingerprint,
        status="SHAKEDOWN_ELIGIBLE_NON_QUALIFYING" if eligible else "BLOCKED",
        eligible_non_qualifying_paper=eligible,
        completed_economic_trades=completed_trades,
        blockers=tuple(blockers),
        broker_submission_enabled=False,
        live_authority=False,
        counts_toward_168h_100_trade_gate=False,
        r13_certification_authority=False,
        j25_j26_candidate_authority=False,
    )


def evaluate_registry_run(
    registry: StrategyRunRegistry,
    run_id: str,
    *,
    campaign_record: dict[str, Any],
) -> ShakedownDecision:
    run = registry.get_run(run_id)
    events = registry.events(run_id, after=0, limit=5000)
    return assess_shakedown(
        run=run,
        events=events,
        campaign=dict(campaign_record["campaign"]),
        campaign_fingerprint=str(campaign_record["campaign_fingerprint"]),
    )

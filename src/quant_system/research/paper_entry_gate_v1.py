from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class PaperEntryDecision:
    schema_id: str
    campaign_id: str
    trial_id: str
    status: str
    eligible_managed_paper_candidate: bool
    blockers: tuple[str, ...]
    broker_submission_enabled: bool
    live_authority: bool
    r13_certification_authority: bool
    j25_j26_candidate_authority: bool
    counts_historical_trades_toward_168h_100_trade_gate: bool

    def to_record(self) -> dict[str, object]:
        record = asdict(self)
        record["blockers"] = list(self.blockers)
        record["decision_sha256"] = sha256(
            json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        return record


def load_policy(path: str | Path) -> dict[str, Any]:
    policy = json.loads(Path(path).read_text(encoding="utf-8"))
    if policy.get("schema_id") != "EQS-PAPER-ENTRY-RESEARCH-GATE-V1":
        raise ValueError("unsupported paper-entry policy schema")
    return policy


def assess_paper_entry(
    *,
    result: dict[str, Any],
    trial: dict[str, Any],
    methodology: dict[str, Any],
    policy: dict[str, Any],
) -> PaperEntryDecision:
    blockers: list[str] = []
    screen = dict(policy["minimum_research_screen"])
    scope = dict(policy["scope"])
    sizing = dict(policy["sizing"])
    authority = dict(policy["paper_authority"])

    if result.get("classification") != "EXPLORATORY_NON_EVIDENTIARY":
        blockers.append("RESULT_CLASSIFICATION_INVALID")
    if trial.get("source_class") != scope["allowed_source_class"]:
        blockers.append("SOURCE_CLASS_INVALID")
    if bool(trial.get("empirical_outcomes_consumed")) is not True:
        blockers.append("TRIAL_OUTCOME_CONSUMPTION_NOT_RECORDED")
    if trial.get("state") != "FINISHED":
        blockers.append("TRIAL_NOT_FINISHED")
    if trial.get("campaign_id") != result.get("campaign_id"):
        blockers.append("TRIAL_CAMPAIGN_MISMATCH")
    if trial.get("trial_id") != result.get("trial_id"):
        blockers.append("TRIAL_ID_MISMATCH")

    if result.get("locked_oos_opened") is not False:
        blockers.append("LOCKED_OOS_MUST_REMAIN_CLOSED")
    if result.get("broker_submission_enabled") is not False:
        blockers.append("BROKER_SUBMISSION_NOT_FALSE")
    if result.get("live_authority") is not False:
        blockers.append("LIVE_AUTHORITY_NOT_FALSE")
    if result.get("r13_certification_authority") is not False:
        blockers.append("R13_AUTHORITY_NOT_FALSE")
    if result.get("j25_j26_candidate_authority") is not False:
        blockers.append("J25_J26_AUTHORITY_NOT_FALSE")
    if result.get("counts_toward_168h_100_trade_gate") is not False:
        blockers.append("HISTORICAL_FORWARD_CREDIT_NOT_FALSE")

    if methodology.get("version") != sizing["required_methodology_version"]:
        blockers.append("SIZING_METHODOLOGY_VERSION_INVALID")
    if methodology.get("locked_oos_access") != "SEALED_NOT_PERMITTED":
        blockers.append("METHODOLOGY_LOCKED_OOS_BOUNDARY_INVALID")
    research_rules = dict(methodology.get("research_rules") or {})
    if research_rules.get("all_future_runs_counted") is not True:
        blockers.append("ALL_TRIALS_COUNTED_RULE_MISSING")
    if research_rules.get("may_claim_clean_validation") is not False:
        blockers.append("CLEAN_VALIDATION_CLAIM_NOT_BLOCKED")

    if int(result.get("training_completed_trades", 0)) < int(screen["minimum_training_completed_trades"]):
        blockers.append("MINIMUM_TRAINING_TRADES_NOT_MET")
    if int(result.get("validation_completed_trades", 0)) < int(screen["minimum_validation_completed_trades"]):
        blockers.append("MINIMUM_VALIDATION_TRADES_NOT_MET")

    training_net = Decimal(str(result.get("training_net_pnl_after_costs", "0")))
    validation_net = Decimal(str(result.get("validation_net_pnl_after_costs", "0")))
    whole_net = Decimal(str(result.get("whole_window_net_pnl_after_costs", "0")))
    if bool(screen["training_net_pnl_after_costs_must_be_positive"]) and training_net <= 0:
        blockers.append("TRAINING_NET_PNL_NOT_POSITIVE")
    if bool(screen["validation_net_pnl_after_costs_must_be_positive"]) and validation_net <= 0:
        blockers.append("VALIDATION_NET_PNL_NOT_POSITIVE")
    if bool(screen["whole_window_net_pnl_after_costs_must_be_positive"]) and whole_net <= 0:
        blockers.append("WHOLE_WINDOW_NET_PNL_NOT_POSITIVE")

    max_dd = Decimal(str(result.get("maximum_drawdown_fraction", "1")))
    if max_dd > Decimal(str(screen["maximum_drawdown_fraction"])):
        blockers.append("MAXIMUM_DRAWDOWN_EXCEEDED")
    cost_share = Decimal(str(result.get("commission_cost_share_of_gross_profit_fraction", "1")))
    if cost_share > Decimal(str(screen["maximum_commission_cost_share_of_gross_profit_fraction"])):
        blockers.append("COMMISSION_COST_SHARE_EXCEEDED")
    max_leverage = Decimal(str(result.get("maximum_observed_gross_leverage", "999")))
    if max_leverage > Decimal(str(sizing["maximum_gross_leverage"])):
        blockers.append("MAXIMUM_GROSS_LEVERAGE_EXCEEDED")
    entry_fraction = Decimal(str(result.get("maximum_entry_notional_fraction", "999")))
    if entry_fraction > Decimal(str(screen["maximum_entry_notional_fraction"])):
        blockers.append("ENTRY_NOTIONAL_FRACTION_EXCEEDED")

    if bool(screen["journal_reconciliation_required"]):
        if result.get("core_journal_reconciled") is not True or result.get("funding_journal_reconciled") is not True:
            blockers.append("JOURNAL_RECONCILIATION_NOT_PASS")
    if bool(screen["final_position_flat_required"]) and Decimal(str(result.get("final_position_quantity", "1"))) != 0:
        blockers.append("FINAL_POSITION_NOT_FLAT")
    if bool(screen["pending_orders_zero_required"]) and int(result.get("pending_order_count", 1)) != 0:
        blockers.append("PENDING_ORDERS_NOT_ZERO")
    if authority.get("requires_shakedown_mechanics_pass") is True and result.get("shakedown_eligible_non_qualifying") is not True:
        blockers.append("SHAKEDOWN_MECHANICS_NOT_PASS")

    blockers = sorted(set(blockers))
    eligible = not blockers
    return PaperEntryDecision(
        schema_id="EQS-PAPER-ENTRY-DECISION-V1",
        campaign_id=str(result.get("campaign_id", "")),
        trial_id=str(result.get("trial_id", "")),
        status="MANAGED_PAPER_CANDIDATE" if eligible else "BLOCKED",
        eligible_managed_paper_candidate=eligible,
        blockers=tuple(blockers),
        broker_submission_enabled=False,
        live_authority=False,
        r13_certification_authority=False,
        j25_j26_candidate_authority=False,
        counts_historical_trades_toward_168h_100_trade_gate=False,
    )

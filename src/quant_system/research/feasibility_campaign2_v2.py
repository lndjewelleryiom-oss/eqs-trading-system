from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from typing import Any

SCHEMA_ID = "EQS-FEASIBILITY-CRYPTO-CAMPAIGN-V2-2"
CAMPAIGN_ID = "FEAS-BINANCE-BTC-BREAKOUT-002"
VERSION = "2.2.0"
SOURCE_MANIFEST_SHA256 = "0a466c8ba272fddfcc1b92adbce3571818554f9b7af4122eef608a6deb71245f"


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def fingerprint(campaign: dict[str, Any]) -> str:
    return sha256(canonical(campaign)).hexdigest()


def validate_campaign2(record: dict[str, Any]) -> tuple[str, ...]:
    failures: list[str] = []
    if record.get("schema_id") != SCHEMA_ID:
        failures.append("SCHEMA_ID_INVALID")
    campaign = record.get("campaign")
    if not isinstance(campaign, dict):
        return tuple(failures + ["CAMPAIGN_REQUIRED"])
    if record.get("campaign_fingerprint") != fingerprint(campaign):
        failures.append("FINGERPRINT_MISMATCH")
    exact = {
        "campaign_id": CAMPAIGN_ID,
        "version": VERSION,
        "classification": "EXPLORATORY_NON_EVIDENTIARY",
    }
    for key, value in exact.items():
        if campaign.get(key) != value:
            failures.append(f"{key.upper()}_INVALID")
    scope = campaign.get("scope", {})
    if scope != {"asset_class":"CRYPTO_LINEAR_PERPETUAL","venue":"BINANCE_USDM","instrument":"BTCUSDT","bar_interval":"5m"}:
        failures.append("SCOPE_INVALID")
    source = campaign.get("source_contract", {})
    if source.get("required_series") != ["KLINES_5M", "FUNDING_RATE"]:
        failures.append("SOURCE_SERIES_INVALID")
    if source.get("shared_acquisition_manifest_file_sha256") != SOURCE_MANIFEST_SHA256:
        failures.append("SOURCE_MANIFEST_BINDING_INVALID")
    if source.get("historical_pit_metadata_claim") is not False:
        failures.append("PIT_METADATA_CLAIM_FORBIDDEN")
    strategy = campaign.get("strategy", {})
    expected_strategy = {
        "family":"CLOSE_CHANNEL_BREAKOUT",
        "entry_lookback_bars":288,
        "exit_lookback_bars":144,
        "max_holding_bars":2016,
        "direction":"LONG_SHORT",
        "quantity":"1",
        "parameter_tuning_allowed":False,
    }
    if strategy != expected_strategy:
        failures.append("STRATEGY_CONTRACT_INVALID")
    execution = campaign.get("execution_model", {})
    required_execution = {
        "fee_bps_per_side":5,
        "adverse_slippage_bps_per_side":2,
        "same_bar_fill_allowed":False,
        "fill_time":"NEXT_BAR_OPEN",
        "decision_time":"BAR_CLOSE",
        "entry_cutoff_bars_before_window_end":2017,
    }
    for key, value in required_execution.items():
        if execution.get(key) != value:
            failures.append(f"EXECUTION_{key.upper()}_INVALID")
    research = campaign.get("research_rules", {})
    for key in ("single_fixed_configuration", "all_runs_counted"):
        if research.get(key) is not True:
            failures.append(f"RESEARCH_{key.upper()}_INVALID")
    for key in ("empirical_outcomes_consumed", "may_write_r13_admission_ledger", "may_open_locked_oos", "campaign1_parameters_modified"):
        if research.get(key) is not False:
            failures.append(f"RESEARCH_{key.upper()}_INVALID")
    periods = campaign.get("periods", {})
    if periods.get("locked_oos_access") != "SEALED_NOT_PERMITTED":
        failures.append("LOCKED_OOS_ACCESS_INVALID")
    prohibitions = set(campaign.get("promotion_prohibitions", []))
    required_prohibitions = {"R13_CERTIFICATION","J25_LOCKED_OOS","J26_GENUINE_CANDIDATE","LIVE_PROMOTION","FORWARD_168H_100_TRADE_CREDIT"}
    if not required_prohibitions.issubset(prohibitions):
        failures.append("PROMOTION_PROHIBITIONS_INCOMPLETE")
    safety = campaign.get("safety", {})
    if safety != {"broker_submission_enabled":False,"live_authority":False,"options_included":False,"futures_dependency":False}:
        failures.append("SAFETY_CONTRACT_INVALID")
    exit_rule = campaign.get("programme_exit_rule", {})
    if exit_rule.get("campaign_number") != 2 or exit_rule.get("max_feasibility_campaigns") != 3 or exit_rule.get("max_elapsed_days") != 30:
        failures.append("EXIT_RULE_INVALID")
    return tuple(sorted(set(failures)))


def load_campaign2(project_root: str | Path) -> dict[str, Any]:
    path = Path(project_root) / "research" / "preregistrations" / "v2" / "FEAS-BINANCE-BTC-BREAKOUT-002.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    failures = validate_campaign2(record)
    if failures:
        raise ValueError(";".join(failures))
    return record

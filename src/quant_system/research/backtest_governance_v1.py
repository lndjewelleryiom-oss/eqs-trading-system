from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Mapping


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _seal(record: Mapping[str, Any]) -> dict[str, Any]:
    body = dict(record)
    body.pop("record_sha256", None)
    body["record_sha256"] = sha256(_canonical(body)).hexdigest()
    return body


def verify_record(record: Mapping[str, Any]) -> bool:
    expected = record.get("record_sha256")
    if not isinstance(expected, str):
        return False
    body = dict(record)
    body.pop("record_sha256", None)
    return sha256(_canonical(body)).hexdigest() == expected


def _load(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def _evidence_pass(path: Path) -> bool:
    doc = _load(path)
    return bool(doc and doc.get("result") == "PASS")


def build_backtest_research_policy(*, source_commit: str) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return _seal({
        "schema_id": "EQS-BACKTEST-RESEARCH-POLICY-V1",
        "policy_version": "1.0.0",
        "status": "ACTIVE",
        "created_at": now,
        "source_commit": source_commit,
        "purpose": "Govern all EQS empirical strategy research from preregistration through PAPER admission without granting LIVE authority.",
        "safety": {
            "broker_submission_enabled": False,
            "live_authority": False,
            "real_trades_permitted": False,
            "options_state": "FROZEN_EXCLUDED",
            "futures_state": "PAUSED_PENDING_ADMISSIBLE_LOW_COST_DATA",
            "failed_gates_fail_closed": True,
        },
        "canonical_stage_order": [
            "PREREGISTERED", "TRAINING", "PARAMETER_ROBUSTNESS", "VALIDATION",
            "OVERFIT_DEFENCE", "LOCKED_OOS", "ECONOMIC_STRESS", "REGIME_TEST",
            "PORTFOLIO_CONTRIBUTION", "CERTIFIED", "PAPER_CANDIDATE",
        ],
        "data_rules": {
            "empirical_run_requires_eligible_matrix_row": True,
            "point_in_time_lineage_required": True,
            "dataset_content_hash_required": True,
            "universe_fingerprint_required_when_universe_changes_over_time": True,
            "future_revisions_forbidden": True,
            "survivorship_only_universes_forbidden": True,
            "locked_oos_single_use": True,
            "historical_forward_credit_to_paper_gate": False,
            "synthetic_results_are_never_market_edge_evidence": True,
        },
        "strategy_identity_rules": {
            "hypothesis_and_parameter_space_frozen_before_results": True,
            "entry_exit_risk_and_cost_rules_frozen_before_results": True,
            "post_validation_change_requires_new_strategy_version": True,
            "post_oos_change_requires_new_campaign_version": True,
            "rejected_trials_retained": True,
            "refactor_does_not_reset_family_trial_count": True,
            "abandoned_trials_count_toward_budget": True,
        },
        "default_statistical_gate": {
            "oos_mean_after_costs_strictly_positive": True,
            "walk_forward_positive_fraction_min": 0.6666666666666666,
            "parameter_robust_profitable_fraction_min": 0.6666666666666666,
            "deflated_sharpe_probability_min": 0.95,
            "pbo_max": 0.25,
            "reality_check_p_max": 0.05,
            "family_wise_alpha": 0.05,
            "bootstrap_confidence": 0.95,
            "bootstrap_simulations_min": 5000,
            "monte_carlo_simulations_min": 5000,
            "cost_stress_multipliers": [1.0, 1.5, 2.0],
            "two_x_cost_stress_net_positive_required": True,
        },
        "robustness_rules": {
            "nearby_parameter_neighbourhood_required": True,
            "isolated_optimum_is_rejection_signal": True,
            "walk_forward_required": True,
            "purging_required_for_overlapping_information_windows": True,
            "embargo_required_where_information_overlap_can_leak": True,
            "regime_decomposition_required": True,
            "pnl_concentration_review_required": True,
            "transaction_cost_and_latency_stress_required": True,
        },
        "execution_rules": {
            "same_event_fill_forbidden": True,
            "market_impact_must_not_be_fabricated": True,
            "spread_slippage_and_fees_must_be_charged": True,
            "funding_and_financing_applied_when_economically_relevant": True,
            "missing_required_cost_or_funding_data_fails_closed": True,
            "deterministic_replay_required": True,
        },
        "promotion_rules": {
            "all_precommitted_gates_required": True,
            "positive_total_pnl_alone_is_insufficient": True,
            "certified_strategy_code_and_config_must_match_paper": True,
            "paper_requires_genuine_forward_market_data": True,
            "paper_performance_is_independent_evidence": True,
            "paper_to_live_transition_out_of_scope": True,
        },
        "paper_starting_floor": {
            "elapsed_genuine_hours": 168,
            "attributable_trades": 100,
            "note": "Frequency-aware evidence rules may supersede this floor only through a new sealed policy version; historical backtests never count toward it.",
        },
    })


def build_data_eligibility_matrix(*, evidence_dir: str | Path, source_commit: str) -> dict[str, Any]:
    ev = Path(evidence_dir)
    r13_contract = _evidence_pass(ev / "R1_3_ACCEPTANCE_2026-09-22.json")
    oos_seal = _load(ev / "EQS_R13_OOS_ACCESS_SEAL.json")
    oos_sealed = bool(oos_seal and oos_seal.get("status") == "SEALED" and oos_seal.get("outcome_viewed") is False)

    rows = [
        {
            "asset_class": "CRYPTO_PERPETUALS",
            "workstream": "R1.3",
            "mechanics_research_allowed": True,
            "genuine_empirical_research_allowed": False,
            "status": "BLOCKED_GENUINE_R13_ARCHIVE_NOT_ADMITTED",
            "training_allowed": False,
            "validation_allowed": False,
            "locked_oos_allowed": False,
            "locked_oos_state": "SEALED" if oos_sealed else "UNVERIFIED",
            "reason_codes": [
                "R13_CONTRACT_FIXTURES_ONLY" if r13_contract else "R13_CONTRACT_NOT_ACCEPTED",
                "GENUINE_LONG_HORIZON_ARCHIVE_NOT_ADMITTED",
                "EARLIER_FEASIBILITY_RESULTS_NOT_REUSABLE_AS_CLEAN_NEW_EVIDENCE",
            ],
            "required_next_evidence": ["genuine R1.3 admission", "verified PIT lifecycle/specification history", "complete source coverage and hashes"],
        },
        {
            "asset_class": "EQUITIES_ETFS",
            "workstream": "EQS-02",
            "mechanics_research_allowed": True,
            "genuine_empirical_research_allowed": False,
            "status": "BLOCKED_HISTORICAL_RESEARCH_ARCHIVE_NOT_CERTIFIED",
            "training_allowed": False, "validation_allowed": False, "locked_oos_allowed": False,
            "locked_oos_state": "NOT_DEFINED",
            "reason_codes": ["FORWARD_READ_ONLY_FEED_ACCEPTED", "ONE_DAY_FORWARD_SAMPLE_IS_NOT_A_RESEARCH_ARCHIVE", "PIT_CORPORATE_ACTION_AND_UNIVERSE_HISTORY_NOT_CERTIFIED_FOR_RESEARCH"],
            "required_next_evidence": ["long-horizon immutable historical bars/trades", "PIT corporate-action handling", "survivorship-safe universe", "sealed train/validation/OOS windows"],
        },
        {
            "asset_class": "FX",
            "workstream": "EQS-03",
            "mechanics_research_allowed": True,
            "genuine_empirical_research_allowed": False,
            "status": "BLOCKED_HISTORICAL_RESEARCH_ARCHIVE_NOT_CERTIFIED",
            "training_allowed": False, "validation_allowed": False, "locked_oos_allowed": False,
            "locked_oos_state": "NOT_DEFINED",
            "reason_codes": ["PAPER_ADAPTER_CERTIFIED", "FORWARD_SOURCE_NOT_EQUIVALENT_TO_LONG_HORIZON_RESEARCH_CERTIFICATION"],
            "required_next_evidence": ["long-horizon immutable PIT FX archive", "source gap policy", "sealed train/validation/OOS windows"],
        },
        {
            "asset_class": "GOLD_SPOT_XAUUSD",
            "workstream": "XAU-SPOT",
            "mechanics_research_allowed": True,
            "genuine_empirical_research_allowed": False,
            "status": "BLOCKED_LONG_HORIZON_RESEARCH_ARCHIVE_NOT_CERTIFIED",
            "training_allowed": False, "validation_allowed": False, "locked_oos_allowed": False,
            "locked_oos_state": "NOT_DEFINED",
            "reason_codes": ["GENUINE_ONE_DAY_DUKASCOPY_SOURCE_ACCEPTED", "SINGLE_DAY_SOURCE_ACCEPTANCE_IS_NOT_LONG_HORIZON_RESEARCH_AUTHORITY"],
            "required_next_evidence": ["long-horizon immutable XAUUSD tick/bar archive", "PIT availability contract", "sealed train/validation/OOS windows"],
        },
        {
            "asset_class": "RATES_FIXED_INCOME",
            "workstream": "EQS-05",
            "mechanics_research_allowed": True,
            "genuine_empirical_research_allowed": False,
            "status": "BLOCKED_HISTORICAL_PIT_AUTHORITY_FALSE",
            "training_allowed": False, "validation_allowed": False, "locked_oos_allowed": False,
            "locked_oos_state": "NOT_DEFINED",
            "reason_codes": ["OFFICIAL_TREASURY_CURVE_SOURCE_ACCEPTED", "CURRENT_ACCEPTANCE_EXPLICITLY_SETS_HISTORICAL_PIT_AUTHORITY_FALSE"],
            "required_next_evidence": ["revision-aware Treasury history", "historical known-at timestamps or defensible publication policy", "sealed train/validation/OOS windows"],
        },
        {
            "asset_class": "FUTURES_COMMODITIES",
            "workstream": "EQS-04",
            "mechanics_research_allowed": False,
            "genuine_empirical_research_allowed": False,
            "status": "PAUSED_COST",
            "training_allowed": False, "validation_allowed": False, "locked_oos_allowed": False,
            "locked_oos_state": "NOT_DEFINED",
            "reason_codes": ["PAUSED_BY_OWNER_DUE_DATA_COST"],
            "required_next_evidence": ["owner-approved admissible free/low-cost licensed data source"],
        },
        {
            "asset_class": "OPTIONS",
            "workstream": "EQS-06",
            "mechanics_research_allowed": False,
            "genuine_empirical_research_allowed": False,
            "status": "FROZEN_EXCLUDED",
            "training_allowed": False, "validation_allowed": False, "locked_oos_allowed": False,
            "locked_oos_state": "FROZEN",
            "reason_codes": ["OPTIONS_EXCLUDED_BY_OWNER"],
            "required_next_evidence": [],
        },
    ]
    return _seal({
        "schema_id": "EQS-RESEARCH-DATA-ELIGIBILITY-MATRIX-V1",
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source_commit": source_commit,
        "fail_closed": True,
        "rows": rows,
        "eligible_genuine_asset_classes": [row["asset_class"] for row in rows if row["genuine_empirical_research_allowed"]],
        "note": "Forward/PAPER feed certification is intentionally separate from historical research-data admission.",
    })

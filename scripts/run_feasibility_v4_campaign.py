from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from quant_system.research.feasibility_trial_ledger_v2 import FeasibilityTrialLedger
from quant_system.research.feasibility_v4_programme import load_campaign_v4, load_v4_months, programme_next_action, run_v4_campaign
from quant_system.research.paper_entry_gate_v1 import assess_paper_entry, load_policy

ROOT = Path(__file__).resolve().parents[1]
EV = ROOT / "artifacts" / "test-evidence"
V4 = ROOT / "research" / "preregistrations" / "v4"
DEFAULT_V2_ROOT = Path(r"H:\My Drive\EQS\feasibility_v2")
DEFAULT_V4_ROOT = Path(r"H:\My Drive\EQS\feasibility_v4_basis")
DEFAULT_LEDGER = DEFAULT_V2_ROOT / "control" / "feasibility_trials_v2.db"


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def file_sha(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def write_evidence(path: Path, record: dict[str, object]) -> str:
    body = dict(record)
    body.pop("record_sha256", None)
    digest = sha256(canonical(body)).hexdigest()
    body["record_sha256"] = digest
    path.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return digest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("campaign", choices=["FEAS-V4-PREMIUM-MR-001", "FEAS-V4-FUNDING-BASIS-002"])
    parser.add_argument("--v2-root", default=str(DEFAULT_V2_ROOT))
    parser.add_argument("--v4-root", default=str(DEFAULT_V4_ROOT))
    parser.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    args = parser.parse_args()

    campaign_path = V4 / f"{args.campaign}.json"
    campaign_record = load_campaign_v4(campaign_path)
    campaign = dict(campaign_record["campaign"])
    methodology_path = V4 / "METHODOLOGY-V4.json"
    methodology = json.loads(methodology_path.read_text(encoding="utf-8"))
    policy_path = V4 / "PAPER-ENTRY-V4.json"
    policy = load_policy(policy_path)
    data_scope_path = V4 / "DATA-SCOPE-V4.json"

    v2_manifest = Path(args.v2_root) / "FEAS-BINANCE-BTC-MA-001-acquisition-manifest.json"
    v4_manifest = Path(args.v4_root) / "FEAS-BINANCE-BTC-BASIS-V4-acquisition-manifest.json"
    source_bindings = dict(campaign["source_bindings"])
    observed = {
        "v2_price_funding_manifest_file_sha256": file_sha(v2_manifest),
        "v4_basis_manifest_file_sha256": file_sha(v4_manifest),
        "v4_data_scope_sha256": file_sha(data_scope_path),
    }
    for key, value in observed.items():
        if value != str(source_bindings[key]):
            raise SystemExit(f"V4_SOURCE_BINDING_MISMATCH:{key}")
    if file_sha(policy_path) != str(campaign["paper_entry_policy_sha256"]):
        raise SystemExit("V4_PAPER_ENTRY_POLICY_BINDING_MISMATCH")
    if methodology.get("methodology_fingerprint") != campaign.get("methodology_fingerprint"):
        raise SystemExit("V4_METHODOLOGY_BINDING_MISMATCH")
    methodology_body = dict(methodology)
    expected_methodology_fingerprint = methodology_body.pop("methodology_fingerprint", None)
    actual_methodology_fingerprint = sha256(canonical(methodology_body)).hexdigest()
    if actual_methodology_fingerprint != expected_methodology_fingerprint:
        raise SystemExit("V4_METHODOLOGY_FINGERPRINT_INVALID")

    configuration = {
        "campaign_fingerprint": campaign_record["campaign_fingerprint"],
        "methodology_fingerprint": methodology["methodology_fingerprint"],
        "paper_entry_policy_sha256": file_sha(policy_path),
        "data_scope_sha256": file_sha(data_scope_path),
        "v2_manifest_sha256": observed["v2_price_funding_manifest_file_sha256"],
        "v4_manifest_sha256": observed["v4_basis_manifest_file_sha256"],
        "runner_sha256": file_sha(Path(__file__)),
        "programme_sha256": file_sha(ROOT / "src" / "quant_system" / "research" / "feasibility_v4_programme.py"),
        "strategy_sha256": file_sha(ROOT / "src" / "quant_system" / "research" / "feasibility_v4_strategies.py"),
    }
    configuration_sha = sha256(canonical(configuration)).hexdigest()
    combined_source_sha = sha256(
        canonical({
            "v2_manifest_sha256": observed["v2_price_funding_manifest_file_sha256"],
            "v4_manifest_sha256": observed["v4_basis_manifest_file_sha256"],
        })
    ).hexdigest()
    trial_id = str(
        uuid5(
            NAMESPACE_URL,
            f"EQS-V4:{campaign_record['campaign_fingerprint']}:{combined_source_sha}:{configuration_sha}",
        )
    )

    ledger = FeasibilityTrialLedger(args.ledger)
    try:
        trial = ledger.register_trial(
            trial_id=trial_id,
            campaign_id=str(campaign["campaign_id"]),
            campaign_fingerprint=str(campaign_record["campaign_fingerprint"]),
            configuration_sha256=configuration_sha,
            acquisition_manifest_sha256=combined_source_sha,
        )
        if trial["state"] == "FINISHED":
            existing = EV / f"EQS_{args.campaign.replace('-', '_')}_ASSESSMENT.json"
            if not existing.is_file():
                raise SystemExit("V4_FINISHED_TRIAL_EVIDENCE_MISSING")
            print(existing.read_text(encoding="utf-8"))
            return 0
        if trial["state"] == "BLOCKED":
            raise SystemExit("V4_TRIAL_ALREADY_BLOCKED")
        if trial["state"] == "CREATED":
            ledger.transition(trial_id, "RUNNING", outcomes_consumed=True)
        elif trial["state"] != "RUNNING":
            raise SystemExit("V4_TRIAL_STATE_INVALID")

        try:
            observed_v2, observed_v4, months = load_v4_months(
                Path(args.v2_root), Path(args.v4_root), str(campaign["campaign_id"])
            )
            if observed_v2 != observed["v2_price_funding_manifest_file_sha256"]:
                raise RuntimeError("V2 manifest changed during V4 load")
            if observed_v4 != observed["v4_basis_manifest_file_sha256"]:
                raise RuntimeError("V4 manifest changed during V4 load")
            result = run_v4_campaign(
                campaign_record=campaign_record,
                months=months,
                trial_id=trial_id,
            )
            result_record = result.to_record()
            result_sha = ledger.persist_result(trial_id, result_record)
            ledger.transition(trial_id, "FINISHED", outcomes_consumed=True)
        except Exception as exc:
            current = ledger.trial(trial_id)
            if current["state"] == "RUNNING":
                ledger.transition(
                    trial_id,
                    "BLOCKED",
                    error_code=type(exc).__name__,
                    outcomes_consumed=True,
                )
            raise

        final_trial = ledger.trial(trial_id)
        decision = assess_paper_entry(
            result=result_record,
            trial=final_trial,
            methodology=methodology,
            policy=policy,
        )
        decision_record = decision.to_record()
        assessment = {
            "schema_id": "EQS-FEASIBILITY-V4-CAMPAIGN-ASSESSMENT-V1",
            "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "campaign_id": campaign["campaign_id"],
            "campaign_version": campaign["version"],
            "campaign_fingerprint": campaign_record["campaign_fingerprint"],
            "trial_id": trial_id,
            "trial_result_sha256": result_sha,
            "trial_ledger_hash_chain_valid": ledger.verify_hash_chain(),
            "trial_count_after_run": ledger.trial_count(),
            "classification": "EXPLORATORY_NON_EVIDENTIARY",
            "paper_entry_decision": decision_record,
            "source_bindings": observed,
            "coverage": campaign["coverage"],
            "economics": {
                "training_net_pnl_after_costs": result_record["training_net_pnl_after_costs"],
                "validation_net_pnl_after_costs": result_record["validation_net_pnl_after_costs"],
                "whole_window_net_pnl_after_costs": result_record["whole_window_net_pnl_after_costs"],
                "maximum_drawdown_fraction": result_record["maximum_drawdown_fraction"],
                "commission_cost_share_of_gross_profit_fraction": result_record["commission_cost_share_of_gross_profit_fraction"],
            },
            "activity": {
                "training_completed_trades": result_record["training_completed_trades"],
                "validation_completed_trades": result_record["validation_completed_trades"],
            },
            "safety": {
                "locked_oos_opened": False,
                "broker_submission_enabled": False,
                "live_authority": False,
                "r13_certification_authority": False,
                "j25_j26_candidate_authority": False,
                "counts_toward_168h_100_trade_gate": False,
            },
            "status": decision.status,
            "next_action": programme_next_action(
                campaign, eligible_managed_paper_candidate=decision.eligible_managed_paper_candidate
            ),
        }
        result_path = EV / f"EQS_{args.campaign.replace('-', '_')}_RESULT.json"
        assessment_path = EV / f"EQS_{args.campaign.replace('-', '_')}_ASSESSMENT.json"
        write_evidence(result_path, result_record)
        assessment_sha = write_evidence(assessment_path, assessment)
        print(
            json.dumps(
                {
                    "status": assessment["status"],
                    "campaign_id": campaign["campaign_id"],
                    "trial_id": trial_id,
                    "assessment_record_sha256": assessment_sha,
                    "blockers": decision_record["blockers"],
                    "training_net": result_record["training_net_pnl_after_costs"],
                    "validation_net": result_record["validation_net_pnl_after_costs"],
                    "training_trades": result_record["training_completed_trades"],
                    "validation_trades": result_record["validation_completed_trades"],
                },
                sort_keys=True,
            )
        )
        return 0
    finally:
        ledger.close()


if __name__ == "__main__":
    raise SystemExit(main())

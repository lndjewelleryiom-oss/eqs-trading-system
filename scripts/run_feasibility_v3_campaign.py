from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from quant_system.research.feasibility_trial_ledger_v2 import FeasibilityTrialLedger
from quant_system.research.feasibility_v3_programme import load_archive, load_campaign, run_v3_campaign
from quant_system.research.paper_entry_gate_v1 import assess_paper_entry, load_policy

ROOT = Path(__file__).resolve().parents[1]
EV = ROOT / "artifacts" / "test-evidence"
DEFAULT_ACQUISITION = Path(r"H:\My Drive\EQS\feasibility_v2")
DEFAULT_LEDGER = DEFAULT_ACQUISITION / "control" / "feasibility_trials_v2.db"
V3 = ROOT / "research" / "preregistrations" / "v3"


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha_file(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def write_evidence(path: Path, record: dict[str, object]) -> str:
    body = dict(record)
    body.pop("record_sha256", None)
    record_sha = sha256(canonical(body)).hexdigest()
    body["record_sha256"] = record_sha
    path.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return record_sha


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("campaign", choices=["FEAS-V3-MR-001", "FEAS-V3-VOLBREAK-002", "FEAS-V3-CARRY-003"])
    parser.add_argument("--acquisition-root", default=str(DEFAULT_ACQUISITION))
    parser.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    args = parser.parse_args()

    campaign_path = V3 / f"{args.campaign}.json"
    campaign_record = load_campaign(campaign_path)
    campaign = dict(campaign_record["campaign"])
    methodology_doc = json.loads((V3 / "FEASIBILITY-V3-METHODOLOGY.json").read_text(encoding="utf-8"))
    methodology = dict(methodology_doc["methodology"])
    policy_path = V3 / "PAPER-ENTRY-V1.json"
    policy = load_policy(policy_path)

    acquisition_root = Path(args.acquisition_root)
    acquisition_manifest = acquisition_root / "FEAS-BINANCE-BTC-MA-001-acquisition-manifest.json"
    acquisition_sha = sha_file(acquisition_manifest)
    required_acquisition_sha = str(dict(campaign["source_contract"])["shared_acquisition_manifest_file_sha256"])
    if acquisition_sha != required_acquisition_sha:
        raise SystemExit("V3_ACQUISITION_MANIFEST_BINDING_MISMATCH")
    if sha_file(policy_path) != str(campaign["paper_entry_policy_sha256"]):
        raise SystemExit("V3_PAPER_ENTRY_POLICY_BINDING_MISMATCH")
    if methodology_doc["methodology_fingerprint"] != campaign["methodology_fingerprint"]:
        raise SystemExit("V3_METHODOLOGY_BINDING_MISMATCH")

    configuration = {
        "campaign_fingerprint": campaign_record["campaign_fingerprint"],
        "methodology_fingerprint": methodology_doc["methodology_fingerprint"],
        "paper_entry_policy_sha256": sha_file(policy_path),
        "acquisition_manifest_sha256": acquisition_sha,
    }
    configuration_sha = sha256(canonical(configuration)).hexdigest()
    trial_id = str(uuid5(NAMESPACE_URL, f"EQS-V3:{campaign_record['campaign_fingerprint']}:{acquisition_sha}:{configuration_sha}"))

    ledger = FeasibilityTrialLedger(args.ledger)
    try:
        trial = ledger.register_trial(
            trial_id=trial_id,
            campaign_id=str(campaign["campaign_id"]),
            campaign_fingerprint=str(campaign_record["campaign_fingerprint"]),
            configuration_sha256=configuration_sha,
            acquisition_manifest_sha256=acquisition_sha,
        )
        if trial["state"] == "FINISHED":
            existing = EV / f"EQS_{args.campaign.replace('-', '_')}_ASSESSMENT.json"
            if not existing.is_file():
                raise SystemExit("V3_FINISHED_TRIAL_EVIDENCE_MISSING")
            print(existing.read_text(encoding="utf-8"))
            return 0
        if trial["state"] == "BLOCKED":
            raise SystemExit("V3_TRIAL_ALREADY_BLOCKED")
        if trial["state"] == "CREATED":
            ledger.transition(trial_id, "RUNNING", outcomes_consumed=True)
        elif trial["state"] != "RUNNING":
            raise SystemExit("V3_TRIAL_STATE_INVALID")

        try:
            observed_manifest_sha, bars, funding = load_archive(acquisition_root, str(campaign["campaign_id"]))
            if observed_manifest_sha != acquisition_sha:
                raise RuntimeError("acquisition manifest changed during load")
            result = run_v3_campaign(
                campaign_record=campaign_record,
                bars=bars,
                funding=funding,
                trial_id=trial_id,
            )
            result_record = result.to_record()
            result_sha = ledger.persist_result(trial_id, result_record)
            ledger.transition(trial_id, "FINISHED", outcomes_consumed=True)
        except Exception as exc:
            current = ledger.trial(trial_id)
            if current["state"] == "RUNNING":
                ledger.transition(trial_id, "BLOCKED", error_code=type(exc).__name__, outcomes_consumed=True)
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
            "schema_id": "EQS-FEASIBILITY-V3-CAMPAIGN-ASSESSMENT-V1",
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
            "next_action": (
                "ADMIT_TO_NON_SUBMITTING_MANAGED_PAPER_SHAKEDOWN"
                if decision.eligible_managed_paper_candidate
                else "RETAIN_REJECTED_COUNTED_TRIAL_AND_MOVE_TO_NEXT_PREFROZEN_V3_CAMPAIGN"
            ),
        }
        result_path = EV / f"EQS_{args.campaign.replace('-', '_')}_RESULT.json"
        assessment_path = EV / f"EQS_{args.campaign.replace('-', '_')}_ASSESSMENT.json"
        write_evidence(result_path, result_record)
        record_sha = write_evidence(assessment_path, assessment)
        print(json.dumps({
            "status": assessment["status"],
            "campaign_id": campaign["campaign_id"],
            "trial_id": trial_id,
            "assessment_record_sha256": record_sha,
            "blockers": decision_record["blockers"],
            "training_net": result_record["training_net_pnl_after_costs"],
            "validation_net": result_record["validation_net_pnl_after_costs"],
            "training_trades": result_record["training_completed_trades"],
            "validation_trades": result_record["validation_completed_trades"],
        }, sort_keys=True))
        return 0
    finally:
        ledger.close()


if __name__ == "__main__":
    raise SystemExit(main())

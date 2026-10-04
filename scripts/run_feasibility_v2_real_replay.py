from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from quant_system.research.feasibility_real_replay_v2 import (
    load_acquired_train_validation,
    run_fixed_campaign_replay,
)
from quant_system.research.feasibility_replay_job_v2 import prepare_replay_job
from quant_system.research.feasibility_trial_ledger_v2 import FeasibilityTrialLedger


ENGINE_VERSION = "EQS-FEASIBILITY-REAL-REPLAY-V2.1.1"


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def immutable_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(payload)
            handle.flush()
    except FileExistsError:
        if path.read_bytes() != payload:
            raise RuntimeError("real replay result path is immutable and existing content differs")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the counted EQS feasibility-v2 train/validation replay.")
    parser.add_argument(
        "--acquisition-root",
        default=str(ROOT / "artifacts" / "research" / "feasibility_v2_raw"),
    )
    parser.add_argument("--trial-id", default=None, help="Reuse only for crash/idempotent retry; new analysis should use a new ID.")
    args = parser.parse_args()
    acquisition_root = Path(args.acquisition_root).resolve()

    preflight = prepare_replay_job(project_root=ROOT, acquisition_root=acquisition_root)
    if preflight.status != "READY_FOR_REAL_TRAIN_VALIDATION_REPLAY":
        print(json.dumps({
            "status": "BLOCKED",
            "stage": "PREFLIGHT_BEFORE_OUTCOME_ACCESS",
            "blockers": list(preflight.blockers),
            "empirical_outcomes_consumed": False,
            "trial_registered": False,
            "broker_submission_enabled": False,
            "live_authority": False,
        }, sort_keys=True))
        return 2

    manifest_path = acquisition_root / "FEAS-BINANCE-BTC-MA-001-acquisition-manifest.json"
    manifest_sha = sha256(manifest_path.read_bytes()).hexdigest()
    campaign_path = ROOT / "research" / "preregistrations" / "v2" / "FEAS-BINANCE-BTC-MA-001.json"
    campaign_bytes = campaign_path.read_bytes()
    configuration_sha = sha256(
        canonical({"campaign_file_sha256": sha256(campaign_bytes).hexdigest(), "engine_version": ENGINE_VERSION})
    ).hexdigest()
    trial_id = args.trial_id or str(uuid4())
    ledger_path = acquisition_root / "control" / "feasibility_trials_v2.db"
    ledger = FeasibilityTrialLedger(ledger_path)
    try:
        ledger.register_trial(
            trial_id=trial_id,
            campaign_id=preflight.campaign_id,
            campaign_fingerprint=preflight.campaign_fingerprint,
            configuration_sha256=configuration_sha,
            acquisition_manifest_sha256=manifest_sha,
        )
        current = ledger.trial(trial_id)
        if current["state"] == "FINISHED":
            print(json.dumps({"status": "PASS_ALREADY", "trial_id": trial_id, "result_sha256": current["result_sha256"]}, sort_keys=True))
            return 0
        if current["state"] == "BLOCKED":
            print(json.dumps({"status": "BLOCKED_ALREADY", "trial_id": trial_id, "error_code": current["error_code"]}, sort_keys=True))
            return 2
        if current["state"] == "CREATED":
            # This transition is recorded before the first raw market-data body is opened.
            ledger.transition(trial_id, "RUNNING", outcomes_consumed=True)

        try:
            campaign_record, bars, funding = load_acquired_train_validation(
                project_root=ROOT,
                acquisition_root=acquisition_root,
            )
            result = run_fixed_campaign_replay(
                bars=bars,
                funding=funding,
                campaign_record=campaign_record,
            )
            result_record = result.to_record()
            envelope = {
                "schema_id": "EQS-FEASIBILITY-V2-COUNTED-TRIAL-RESULT-V1",
                "trial_id": trial_id,
                "engine_version": ENGINE_VERSION,
                "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "campaign_fingerprint": preflight.campaign_fingerprint,
                "configuration_sha256": configuration_sha,
                "acquisition_manifest_sha256": manifest_sha,
                "source_class": "PUBLIC_UNADMITTED_EXPLORATORY",
                "empirical_outcomes_consumed": True,
                "result": result_record,
                "r13_certification_authority": False,
                "j25_j26_candidate_authority": False,
                "counts_toward_168h_100_trade_gate": False,
                "broker_submission_enabled": False,
                "live_authority": False,
            }
            envelope["envelope_sha256"] = sha256(canonical(envelope)).hexdigest()
            ledger.persist_result(trial_id, envelope)
            ledger.transition(trial_id, "FINISHED", outcomes_consumed=True)
            result_path = acquisition_root / "results" / f"{trial_id}.json"
            immutable_write(result_path, json.dumps(envelope, indent=2, sort_keys=True).encode("utf-8") + b"\n")
            print(json.dumps({
                "status": "PASS",
                "trial_id": trial_id,
                "shakedown_eligible_non_qualifying": result.shakedown_eligible_non_qualifying,
                "validation_completed_trades": result.validation_completed_trades,
                "result_path": str(result_path),
                "result_sha256": envelope["envelope_sha256"],
                "empirical_outcomes_consumed": True,
                "r13_certification_authority": False,
                "live_authority": False,
            }, sort_keys=True))
            return 0
        except Exception as exc:
            row = ledger.trial(trial_id)
            if row["state"] == "RUNNING":
                ledger.transition(trial_id, "BLOCKED", error_code=type(exc).__name__, outcomes_consumed=True)
            raise
    finally:
        ledger.close()


if __name__ == "__main__":
    raise SystemExit(main())

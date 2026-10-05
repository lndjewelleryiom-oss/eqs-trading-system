from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
EV = ROOT / "artifacts" / "test-evidence"

GROUPS = {
    "governance_pipeline": [
        "tests/test_backtest_governance_v1.py",
        "tests/test_strategy_preregistration_v1.py",
        "tests/test_backtest_pipeline_v1.py",
    ],
    "deterministic_runner_visibility": [
        "tests/test_backtest_jobs.py",
        "tests/test_strategy_run_registry.py",
        "tests/test_strategy_run_api.py",
        "tests/test_backtest_http_api.py",
        "tests/test_executable_strategy_certification.py",
    ],
    "validation_robustness_portfolio": [
        "tests/test_validation_resampling.py",
        "tests/test_validation_scorecard.py",
        "tests/test_validation_sensitivity.py",
        "tests/test_validation_splits.py",
        "tests/test_validation_statistics.py",
        "tests/test_regime_models.py",
        "tests/test_portfolio_allocator.py",
        "tests/test_portfolio_control.py",
    ],
    "promotion_paper_degradation": [
        "tests/test_alpha_campaign_preregistration.py",
        "tests/test_alpha_data_binding_schema.py",
        "tests/test_r13_admission_evidence_runner.py",
        "tests/test_research_promotion_bridge.py",
        "tests/test_paper_entry_gate_v1.py",
        "tests/test_strategy_lifecycle.py",
        "tests/test_managed_paper_evidence.py",
        "tests/test_paper_attribution.py",
        "tests/test_autonomous_paper_allocator.py",
        "tests/test_degradation_monitor.py",
    ],
}


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def run_group(files: list[str]) -> dict[str, object]:
    result = subprocess.run(["python", "-m", "pytest", *files, "-q"], cwd=ROOT, capture_output=True, text=True)
    stdout = (result.stdout or "") + (result.stderr or "")
    return {
        "returncode": result.returncode,
        "status": "PASS" if result.returncode == 0 else "FAIL",
        "stdout_sha256": sha256(stdout.encode("utf-8")).hexdigest(),
        "summary_tail": "\n".join(stdout.strip().splitlines()[-4:]),
        "files": files,
    }


def main() -> int:
    evidence = {name: run_group(files) for name, files in GROUPS.items()}
    all_pass = all(row["status"] == "PASS" for row in evidence.values())
    record = {
        "schema_id": "EQS-BACKTESTING-FRAMEWORK-ACCEPTANCE-V1",
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "result": "PASS" if all_pass else "FAIL",
        "groups": evidence,
        "claims": {
            "bt02_preregistration_mechanics": all_pass,
            "bt03_deterministic_runner_and_visibility_mechanics": all_pass,
            "bt04_to_bt11_validation_mechanics": all_pass,
            "bt12_certification_and_promotion_mechanics": all_pass,
            "bt13_to_bt16_paper_lifecycle_mechanics": all_pass,
            "genuine_empirical_strategy_result_claimed": False,
            "profitability_claimed": False,
        },
        "broker_submission_enabled": False,
        "live_authority": False,
        "real_trades_permitted": False,
    }
    record["record_sha256"] = sha256(canonical(record)).hexdigest()
    out = EV / "EQS_BACKTESTING_FRAMEWORK_ACCEPTANCE_V1.json"
    out.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"result": record["result"], "record_sha256": record["record_sha256"], "groups": {k:v["status"] for k,v in evidence.items()}}, sort_keys=True))
    return 0 if all_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())

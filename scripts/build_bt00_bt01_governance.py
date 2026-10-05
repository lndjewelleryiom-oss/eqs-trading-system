from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import subprocess

from quant_system.research.backtest_governance_v1 import (
    build_backtest_research_policy,
    build_data_eligibility_matrix,
    verify_record,
)

ROOT = Path(__file__).resolve().parents[1]
EV = ROOT / "artifacts" / "test-evidence"


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def git_commit() -> str:
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    EV.mkdir(parents=True, exist_ok=True)
    commit = git_commit()
    policy = build_backtest_research_policy(source_commit=commit)
    matrix = build_data_eligibility_matrix(evidence_dir=EV, source_commit=commit)
    if not verify_record(policy) or not verify_record(matrix):
        raise RuntimeError("BT00/BT01 seal verification failed")

    policy_path = EV / "EQS_BACKTEST_RESEARCH_POLICY_V1.json"
    matrix_path = EV / "EQS_RESEARCH_DATA_ELIGIBILITY_MATRIX.json"
    write_json(policy_path, policy)
    write_json(matrix_path, matrix)

    record = {
        "schema_id": "EQS-BT00-BT01-ACCEPTANCE-V1",
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "result": "PASS",
        "bt00_status": "COMPLETE",
        "bt01_status": "COMPLETE",
        "policy_sha256": policy["record_sha256"],
        "eligibility_matrix_sha256": matrix["record_sha256"],
        "eligible_genuine_asset_classes": matrix["eligible_genuine_asset_classes"],
        "blocked_rows_are_expected_and_fail_closed": True,
        "broker_submission_enabled": False,
        "live_authority": False,
        "source_commit": commit,
    }
    record["record_sha256"] = sha256(canonical(record)).hexdigest()
    write_json(EV / "EQS_BT00_BT01_ACCEPTANCE.json", record)
    print(json.dumps(record, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

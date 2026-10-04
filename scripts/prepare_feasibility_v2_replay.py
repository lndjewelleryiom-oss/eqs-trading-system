from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from quant_system.research.feasibility_replay_job_v2 import prepare_replay_job


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare, but do not execute, EQS feasibility-v2 real replay.")
    parser.add_argument(
        "--acquisition-root",
        default=str(ROOT / "artifacts" / "research" / "feasibility_v2_raw"),
    )
    parser.add_argument(
        "--output",
        default=str(ROOT / "artifacts" / "test-evidence" / "EQS_FEASIBILITY_V2_REPLAY_JOB_STATUS.json"),
    )
    args = parser.parse_args()
    decision = prepare_replay_job(project_root=ROOT, acquisition_root=args.acquisition_root)
    record = decision.to_record()
    record["observed_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    record["execution_started"] = False
    record["empirical_outcomes_consumed"] = False
    record["record_sha256"] = sha256(
        json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(record, sort_keys=True))
    return 0 if decision.status.startswith("READY") else 2


if __name__ == "__main__":
    raise SystemExit(main())

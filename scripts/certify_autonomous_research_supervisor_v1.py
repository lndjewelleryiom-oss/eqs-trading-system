from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parents[1]
EV=ROOT/"artifacts"/"test-evidence"


def canonical(value: object) -> bytes:
    return json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()


def main() -> int:
    result=subprocess.run(["python","-m","pytest","tests/test_autonomous_research_supervisor_v1.py","-q"],cwd=ROOT,capture_output=True,text=True)
    output=(result.stdout or "")+(result.stderr or "")
    record={
        "schema_id":"EQS-AUTONOMOUS-RESEARCH-SUPERVISOR-ACCEPTANCE-V1",
        "created_at":datetime.now(timezone.utc).isoformat().replace("+00:00","Z"),
        "result":"PASS" if result.returncode==0 else "FAIL",
        "bt17_mechanics":"COMPLETE" if result.returncode==0 else "FAILED",
        "capabilities":[
            "fail-closed BT-01 eligibility admission",
            "durable idempotent strategy-lineage queue",
            "park external/data blockers without retry loop",
            "bounded transient retry budget",
            "automatic queueing when a newly preregistered lineage is eligible",
            "immutable hash-chained supervisor journal",
        ],
        "genuine_empirical_job_executed":False,
        "genuine_empirical_operation_status":"BLOCKED_NO_ELIGIBLE_GENUINE_DATASET",
        "broker_submission_enabled":False,"live_authority":False,"real_trades_permitted":False,
        "test_output_sha256":sha256(output.encode()).hexdigest(),
        "test_summary_tail":"\n".join(output.strip().splitlines()[-4:]),
    }
    record["record_sha256"]=sha256(canonical(record)).hexdigest()
    (EV/"EQS_AUTONOMOUS_RESEARCH_SUPERVISOR_ACCEPTANCE_V1.json").write_text(json.dumps(record,indent=2,sort_keys=True)+"\n",encoding="utf-8")
    print(json.dumps({"result":record["result"],"record_sha256":record["record_sha256"]},sort_keys=True))
    return result.returncode

if __name__=="__main__": raise SystemExit(main())

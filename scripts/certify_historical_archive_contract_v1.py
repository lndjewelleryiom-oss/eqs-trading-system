from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
EV = ROOT / "artifacts" / "test-evidence"


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def main() -> int:
    result = subprocess.run(
        ["python", "-m", "pytest", "tests/test_historical_archive_v1.py", "-q"],
        cwd=ROOT, capture_output=True, text=True,
    )
    output = (result.stdout or "") + (result.stderr or "")
    record = {
        "schema_id": "EQS-NONCRYPTO-HISTORICAL-ARCHIVE-CONTRACT-ACCEPTANCE-V1",
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "result": "PASS" if result.returncode == 0 else "FAIL",
        "supported_asset_classes": ["EQUITIES_ETFS", "FX", "GOLD_SPOT_XAUUSD", "RATES_FIXED_INCOME"],
        "locked_oos_default": "SEALED_NOT_OPENED",
        "requires_historical_pit_authority": True,
        "requires_content_and_retrieval_hashes": True,
        "requires_gap_policy": True,
        "requires_survivorship_safe_universe": True,
        "equities_require_corporate_action_policy": True,
        "fx_xau_forbid_fabricated_volume": True,
        "rates_require_revision_policy": True,
        "test_returncode": result.returncode,
        "test_output_sha256": sha256(output.encode("utf-8")).hexdigest(),
        "test_summary_tail": "\n".join(output.strip().splitlines()[-4:]),
        "genuine_archive_admitted": False,
        "broker_submission_enabled": False,
        "live_authority": False,
    }
    record["record_sha256"] = sha256(canonical(record)).hexdigest()
    out = EV / "EQS_NONCRYPTO_HISTORICAL_ARCHIVE_CONTRACT_ACCEPTANCE_V1.json"
    out.write_text(json.dumps(record, indent=2, sort_keys=True)+"\n", encoding="utf-8")
    print(json.dumps({"result":record["result"],"record_sha256":record["record_sha256"]}, sort_keys=True))
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())

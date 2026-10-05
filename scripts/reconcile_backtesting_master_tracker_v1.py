from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EV = ROOT / "artifacts" / "test-evidence"


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def load(name: str) -> dict:
    return json.loads((EV / name).read_text(encoding="utf-8"))


def main() -> int:
    bt01 = load("EQS_BT00_BT01_ACCEPTANCE.json")
    framework = load("EQS_BACKTESTING_FRAMEWORK_ACCEPTANCE_V1.json")
    matrix = load("EQS_RESEARCH_DATA_ELIGIBILITY_MATRIX.json")
    archive_contract = load("EQS_NONCRYPTO_HISTORICAL_ARCHIVE_CONTRACT_ACCEPTANCE_V1.json")
    supervisor = load("EQS_AUTONOMOUS_RESEARCH_SUPERVISOR_ACCEPTANCE_V1.json")
    if any(x.get("result") != "PASS" for x in (bt01, framework, archive_contract, supervisor)):
        raise SystemExit("BACKTESTING_PREREQUISITE_ACCEPTANCE_NOT_PASS")
    genuine = list(matrix.get("eligible_genuine_asset_classes", []))
    no_data_blocker = "NO_ELIGIBLE_GENUINE_RESEARCH_DATASET"
    no_candidate = "NO_EMPIRICALLY_CERTIFIED_STRATEGY_CANDIDATE"
    no_paper = "NO_CERTIFIED_STRATEGY_IN_GENUINE_PAPER"

    tasks = [
        {"id":"BT-00","name":"Freeze research rules","status":"COMPLETE","mechanics_certified":True,"blockers":[]},
        {"id":"BT-01","name":"Research data eligibility matrix","status":"COMPLETE","mechanics_certified":True,"blockers":[]},
        {"id":"BT-02","name":"Immutable strategy hypothesis/preregistration registry","status":"COMPLETE","mechanics_certified":True,"blockers":[]},
        {"id":"BT-03","name":"Deterministic backtest runner and strategy visibility","status":"COMPLETE","mechanics_certified":True,"blockers":[]},
    ]
    for task_id, name in (
        ("BT-04","Training-stage empirical test"),
        ("BT-05","Parameter robustness"),
        ("BT-06","Untouched validation period"),
        ("BT-07","Overfitting defence"),
        ("BT-08","Single-use locked OOS"),
        ("BT-09","Economic stress testing"),
        ("BT-10","Regime testing"),
        ("BT-11","Portfolio contribution"),
    ):
        tasks.append({"id":task_id,"name":name,"status":"BLOCKED","mechanics_certified":True,"blockers":[] if genuine else [no_data_blocker]})
    tasks.extend([
        {"id":"BT-12","name":"Backtest certification","status":"BLOCKED","mechanics_certified":True,"blockers":[no_candidate]},
        {"id":"BT-13","name":"PAPER candidate admission","status":"BLOCKED","mechanics_certified":True,"blockers":[no_candidate]},
        {"id":"BT-14","name":"Live PAPER strategy dashboard","status":"BLOCKED","mechanics_certified":True,"blockers":[no_paper]},
        {"id":"BT-15","name":"PAPER performance qualification","status":"BLOCKED","mechanics_certified":True,"blockers":[no_paper]},
        {"id":"BT-16","name":"Automatic rejection / retirement","status":"BLOCKED","mechanics_certified":True,"blockers":[no_paper]},
        {"id":"BT-17","name":"Continuous autonomous research supervisor","status":"COMPLETE","mechanics_certified":True,"operation_status":"RUNNABLE_WHEN_DATA_ELIGIBLE","blockers":[]},
    ])
    record = {
        "schema_id":"EQS-BACKTESTING-MASTER-TRACKER-V1",
        "updated_at":datetime.now(timezone.utc).isoformat().replace("+00:00","Z"),
        "tasks":tasks,
        "eligible_genuine_asset_classes":genuine,
        "parked_external_or_data_blockers":[
            {"id":"DATA-CRYPTO-R13","status":"BLOCKED","detail":"genuine long-horizon R1.3 archive not admitted; provider/PIT/storage inputs remain external"},
            {"id":"DATA-EQUITIES","status":"BLOCKED","detail":"long-horizon PIT research archive and survivorship/corporate-action history not certified"},
            {"id":"DATA-FX","status":"BLOCKED","detail":"long-horizon PIT FX research archive not certified"},
            {"id":"DATA-XAU","status":"BLOCKED","detail":"long-horizon XAUUSD research archive not certified"},
            {"id":"DATA-RATES","status":"BLOCKED","detail":"current official-curve acceptance explicitly has historical_pit_authority=false"},
            {"id":"DATA-FUTURES","status":"PAUSED","detail":"owner paused paid futures data"},
            {"id":"DATA-OPTIONS","status":"FROZEN","detail":"owner excluded options"},
        ],
        "next_dependency_safe_action":"BUILD_SOURCE_SPECIFIC_HISTORICAL_ACQUISITION_AND_PIT_ATTESTATION_PATHS_WITHOUT_OPENING_OOS",
        "research_framework_acceptance_sha256":framework["record_sha256"],
        "bt00_bt01_acceptance_sha256":bt01["record_sha256"],
        "noncrypto_archive_contract_acceptance_sha256":archive_contract["record_sha256"],
        "autonomous_supervisor_acceptance_sha256":supervisor["record_sha256"],
        "broker_submission_enabled":False,
        "live_authority":False,
        "real_trades_permitted":False,
    }
    record["record_sha256"] = sha256(canonical(record)).hexdigest()
    out = EV / "EQS_BACKTESTING_MASTER_TRACKER_V1.json"
    out.write_text(json.dumps(record, indent=2, sort_keys=True)+"\n", encoding="utf-8")
    print(json.dumps({"record_sha256":record["record_sha256"],"complete":[x["id"] for x in tasks if x["status"]=="COMPLETE"],"blocked":[x["id"] for x in tasks if x["status"]=="BLOCKED"],"next":record["next_dependency_safe_action"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

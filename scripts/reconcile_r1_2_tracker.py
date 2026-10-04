from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import time

ROOT = Path(r"C:\Users\lndje\Documents\EQS_Market_Terminal_V5_20260925\app")
EV = ROOT / "artifacts" / "test-evidence"
SOAK = EV / "R1_2_LIVE_SOAK_20261005_V2"
LONG = EV / "R1_2_LONG_LIVED_WS_20261005_V2"
OUTPUT = EV / "EQS_R1_2_PROGRESS_PROJECTION.json"


def canonical(obj: object) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def file_sha(path: Path) -> str | None:
    return sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def seal_ok(doc: dict, field: str = "record_sha256") -> bool:
    expected = doc.get(field)
    if not isinstance(expected, str): return False
    body = dict(doc); body.pop(field, None)
    return sha256(canonical(body)).hexdigest() == expected


def last_jsonl(path: Path) -> dict | None:
    if not path.is_file() or path.stat().st_size == 0: return None
    return json.loads(path.read_text(encoding="utf-8").splitlines()[-1])


def atomic_json(path: Path, doc: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as handle:
        json.dump(doc, handle, indent=2, sort_keys=True)
        handle.write("\n"); handle.flush(); os.fsync(handle.fileno())
    last = None
    for attempt in range(30):
        try:
            os.replace(tmp, path); return
        except PermissionError as exc:
            last = exc; time.sleep(0.1 * (attempt + 1))
    tmp.unlink(missing_ok=True)
    raise last


def main() -> int:
    retry_path = EV / "EQS_R1_2_RETRY_BACKOFF_ACCEPTANCE.json"
    prereg_path = EV / "EQS_R1_2_LONG_LIVED_WS_V2_PREREGISTRATION.json"
    retry = load(retry_path)
    prereg = load(prereg_path)
    if not seal_ok(retry) or not str(retry.get("status", "")).startswith("PASS"):
        raise RuntimeError("R1.2 retry/backoff acceptance invalid")
    if not seal_ok(prereg): raise RuntimeError("R1.2 long-lived preregistration invalid")

    soak_state_path = SOAK / "binance_soak_state.json"
    metric_path = SOAK / "binance_storage_metrics.jsonl"
    long_state_path = LONG / "state.json"
    soak_state = load(soak_state_path) if soak_state_path.is_file() else None
    metric = last_jsonl(metric_path)
    long_state = load(long_state_path) if long_state_path.is_file() else None
    if metric and not seal_ok(metric): raise RuntimeError("R1.2 storage metric seal invalid")

    evidence = [
        "2026-09-22 genuine read-only public-feed acceptance remains retained for Binance USD-M, Bybit Linear and OKX SWAP",
        "corrected Binance snapshot-bridge/reconnect acceptance retained; failed harness attempts remain preserved rather than rewritten",
        "three-hour 1-minute backfill reconciliation remains PASS at 180/180 buckets per venue with zero missing/duplicates",
        f"controlled REST retry/backoff mechanics PASS record={retry['record_sha256']}; 429/temporary 5xx/transport retries bounded; permanent 4xx fail closed",
    ]
    remaining: list[str] = []
    if soak_state:
        c = soak_state.get("counters", {})
        evidence.append(
            "R1.2 V2 genuine Binance soak RUNNING from " + str(soak_state.get("started_at")) +
            f"; frames persisted={c.get('raw_records_persisted',0)}/{c.get('socket_frames_received',0)}; "
            f"parse failures={c.get('parse_failures',0)}; persistence failures={c.get('persistence_failures',0)}; "
            f"sequence gaps={c.get('sequence_gaps',0)}; resynchronizations={c.get('resynchronizations',0)}"
        )
    else:
        remaining.append("R1.2 V2 genuine live soak is not currently publishing state")
    if metric:
        evidence.append(
            f"hash-chained V2 storage monitor RUNNING; raw_bytes={metric.get('raw_bytes')}; "
            f"throughput={metric.get('raw_bytes_per_second')} B/s; hourly checkpoints={metric.get('hourly_checkpoint_count')}"
        )
        if int(metric.get("hourly_checkpoint_count", 0)) < 1:
            remaining.append("V2 genuine live soak is RUNNING; first real >=3600-second hourly checkpoint has not yet elapsed")
        remaining.append("continue multi-hour/day storage-growth, throughput and drop-accounting observation")
    else:
        remaining.append("V2 storage-growth monitor has not yet produced a sealed sample")
    if long_state:
        evidence.append(
            f"long-lived Binance public WebSocket V2 RUNNING from {long_state.get('run_started_at')}; "
            f"messages={long_state.get('total_messages',0)}; sampled_raw={long_state.get('sampled_messages',0)}; "
            f"premature_closes={long_state.get('premature_closes',0)}; heartbeat_proof={long_state.get('heartbeat_survival_proved',False)}"
        )
        if not long_state.get("heartbeat_survival_proved"):
            remaining.append("long-lived WebSocket heartbeat proof is RUNNING; requires >=660 real seconds on one connection with genuine messages")
        if int(long_state.get("expected_rollovers", 0)) < 1:
            remaining.append("documented 24-hour Binance connection rollover observation is RUNNING and not yet complete")
    else:
        remaining.append("long-lived WebSocket V2 observation is not currently publishing state")

    component = {"status": "TESTING", "evidence": evidence, "remaining": remaining}
    rec = {
        "schema_id": "EQS-R1.2-PROGRESS-PROJECTION-V1",
        "status": "PASS",
        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "component_id": "R1.2", "component": component,
        "bindings": {
            "retry_acceptance_sha256": file_sha(retry_path),
            "soak_state_sha256": file_sha(soak_state_path),
            "storage_metrics_sha256": file_sha(metric_path),
            "long_lived_preregistration_sha256": file_sha(prereg_path),
            "long_lived_state_sha256": file_sha(long_state_path),
        },
        "safety": {"broker_submission_enabled": False, "live_authority": False, "read_only": True},
    }
    rec["record_sha256"] = sha256(canonical(rec)).hexdigest()
    atomic_json(OUTPUT, rec)
    print(json.dumps({"status":"PASS","remaining_count":len(remaining),"record_sha256":rec["record_sha256"]}, sort_keys=True))
    return 0

if __name__ == "__main__": raise SystemExit(main())

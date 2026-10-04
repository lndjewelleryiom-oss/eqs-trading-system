from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import shutil


def canonical(obj: object) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def parse_time(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return dt.astimezone(timezone.utc)


def last_metric(path: Path) -> dict | None:
    if not path.is_file() or path.stat().st_size == 0:
        return None
    line = path.read_text(encoding="utf-8").splitlines()[-1]
    rec = json.loads(line)
    expected = rec.get("record_sha256")
    body = dict(rec); body.pop("record_sha256", None)
    if not isinstance(expected, str) or sha256(canonical(body)).hexdigest() != expected:
        raise RuntimeError("prior soak metric hash invalid")
    return rec


def build_metric(soak_dir: Path, now: datetime) -> dict:
    state_path = soak_dir / "binance_soak_state.json"
    raw_path = soak_dir / "binance_raw.jsonl"
    checkpoint_path = soak_dir / "binance_hourly_checkpoints.jsonl"
    if not state_path.is_file():
        raise RuntimeError("soak state not yet available")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    started = parse_time(state["started_at"])
    raw_bytes = raw_path.stat().st_size if raw_path.is_file() else 0
    metrics_path = soak_dir / "binance_storage_metrics.jsonl"
    prev = last_metric(metrics_path)
    elapsed = max(0.0, (now - started).total_seconds())
    delta_bytes = raw_bytes - int(prev["raw_bytes"]) if prev else raw_bytes
    delta_seconds = max(0.0, (now - parse_time(prev["observed_at"])).total_seconds()) if prev else elapsed
    free = shutil.disk_usage(soak_dir).free
    checkpoints = 0
    if checkpoint_path.is_file():
        checkpoints = sum(1 for line in checkpoint_path.read_text(encoding="utf-8").splitlines() if line.strip())
    rec = {
        "schema_id": "EQS-R1.2-SOAK-STORAGE-METRIC-V1",
        "observed_at": now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "soak_started_at": started.isoformat().replace("+00:00", "Z"),
        "elapsed_seconds": round(elapsed, 3),
        "raw_bytes": raw_bytes,
        "raw_growth_bytes": delta_bytes,
        "sample_seconds": round(delta_seconds, 3),
        "raw_bytes_per_second": round(delta_bytes / delta_seconds, 6) if delta_seconds > 0 else 0.0,
        "disk_free_bytes": free,
        "hourly_checkpoint_count": checkpoints,
        "cycle_count": int(state.get("cycle_count", 0)),
        "counters": state.get("counters", {}),
        "previous_hash": prev.get("record_sha256") if prev else None,
        "read_only": True,
        "broker_submission_enabled": False,
        "live_authority": False,
    }
    rec["record_sha256"] = sha256(canonical(rec)).hexdigest()
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--soak-dir", required=True)
    ap.add_argument("--now")
    args = ap.parse_args()
    soak_dir = Path(args.soak_dir)
    now = parse_time(args.now) if args.now else datetime.now(timezone.utc)
    rec = build_metric(soak_dir, now)
    out = soak_dir / "binance_storage_metrics.jsonl"
    with out.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(rec, sort_keys=True, separators=(",", ":")) + "\n")
        handle.flush()
    print(json.dumps(rec, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

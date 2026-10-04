from __future__ import annotations
import argparse
import concurrent.futures
import datetime
import hashlib
import json
import os
import time
import urllib.request
from pathlib import Path

ROOT = Path(r"C:\Users\lndje\Documents\EQS_Market_Terminal_V5_20260925\app")
SOURCES = {
    "BYBIT_LINEAR_BTCUSDT": "https://api.bybit.com/v5/market/tickers?category=linear&symbol=BTCUSDT",
    "OKX_SWAP_BTC_USDT": "https://www.okx.com/api/v5/market/ticker?instId=BTC-USDT-SWAP",
}

def utc() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

def validate(source: str, raw: bytes) -> dict:
    if len(raw) > 1_000_000:
        raise ValueError("response too large")
    data = json.loads(raw)
    if source.startswith("BYBIT") and (data.get("retCode") != 0 or not data.get("result", {}).get("list")):
        raise ValueError("Bybit payload invalid")
    if source.startswith("OKX") and (data.get("code") != "0" or not data.get("data")):
        raise ValueError("OKX payload invalid")
    return data

def fetch(source: str, url: str, attempts: int, timeout: float) -> dict:
    errors = []
    for attempt in range(1, attempts + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "EQS-Operational-Commissioning-V3"})
            with urllib.request.urlopen(req, timeout=timeout) as response:
                raw = response.read(1_000_001)
            data = validate(source, raw)
            rec = {
                "received_at": utc(), "url_class": "PUBLIC_MARKET_TICKER",
                "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
                "status": "PASS", "payload": data, "attempts": attempt,
            }
            if errors:
                rec["recovered_attempt_errors"] = errors
            return rec
        except Exception as exc:
            errors.append({"attempt": attempt, "error": type(exc).__name__ + ":" + str(exc)})
            if attempt < attempts:
                time.sleep(min(2 ** (attempt - 1), 4))
    return {
        "received_at": utc(), "url_class": "PUBLIC_MARKET_TICKER",
        "status": "FAIL", "error": errors[-1]["error"], "attempts": attempts,
        "attempt_errors": errors,
    }

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration-seconds", type=float, default=24 * 3600)
    ap.add_argument("--interval-seconds", type=float, default=60.0)
    ap.add_argument("--attempts", type=int, default=4)
    ap.add_argument("--request-timeout", type=float, default=8.0)

    ap.add_argument("--prefix", default="operational_v3_soak_")
    args = ap.parse_args()
    if args.duration_seconds <= 0 or args.interval_seconds <= 0 or args.attempts < 1 or args.request_timeout <= 0:
        raise SystemExit("invalid collector arguments")

    run_id = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = ROOT / "artifacts" / "commissioning" / (args.prefix + run_id)
    out.mkdir(parents=True, exist_ok=False)
    start_wall = time.time()
    start_mono = time.monotonic()
    end_mono = start_mono + args.duration_seconds
    end_wall = start_wall + args.duration_seconds
    counts = {source: {"PASS": 0, "FAIL": 0} for source in SOURCES}

    def heartbeat(**extra) -> None:
        payload = {
            "run_id": run_id, "pid": os.getpid(),
            "started_at": datetime.datetime.fromtimestamp(start_wall, datetime.timezone.utc).isoformat(),
            "updated_at": utc(), "end_epoch": end_wall,
            "scheduled_end_at": datetime.datetime.fromtimestamp(end_wall, datetime.timezone.utc).isoformat(),
            "live": True, "complete": False, "collector_version": "v3",
            "retry_policy": {"attempts": args.attempts, "request_timeout_seconds": args.request_timeout},
        }
        payload.update(extra)
        tmp = out / "heartbeat.tmp"
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(out / "heartbeat.json")

    heartbeat(counts=counts)
    receipts = out / "feed_receipts.jsonl"

    next_cycle = start_mono
    with receipts.open("a", encoding="utf-8") as handle, concurrent.futures.ThreadPoolExecutor(max_workers=len(SOURCES)) as pool:
        while time.monotonic() < end_mono:
            cycle = utc()
            jobs = {
                source: pool.submit(fetch, source, url, args.attempts, args.request_timeout)
                for source, url in SOURCES.items()
            }
            results = {source: job.result() for source, job in jobs.items()}
            for source in SOURCES:
                rec = {"cycle": cycle, "source": source, **results[source]}
                counts[source][rec["status"]] += 1
                handle.write(json.dumps(rec, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
            heartbeat(counts=counts)
            next_cycle += args.interval_seconds
            delay = min(next_cycle, end_mono) - time.monotonic()
            if delay > 0:
                time.sleep(delay)

    completed_at = utc()
    heartbeat(live=False, complete=True, completed_at=completed_at, counts=counts)
    complete = {
        "run_id": run_id, "completed_at": completed_at, "counts": counts,
        "collector_version": "v3",
        "receipts_sha256": hashlib.sha256(receipts.read_bytes()).hexdigest(),
    }
    (out / "COMPLETE.json").write_text(json.dumps(complete, indent=2), encoding="utf-8")
    print(json.dumps({"status": "COMPLETE", "run_id": run_id, "out": str(out), "counts": counts}, indent=2))

if __name__ == "__main__":
    main()

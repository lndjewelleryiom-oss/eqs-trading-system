from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import urllib.request

from quant_system.research.feasibility_basis_archive_v4 import validate_basis_archive

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "research" / "preregistrations" / "v4" / "DATA-SCOPE-V4.json"
DEFAULT_DESTINATION = Path(r"H:\My Drive\EQS\feasibility_v4_basis")


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def months(start: str, end: str) -> list[str]:
    sy, sm = map(int, start.split("-")); ey, em = map(int, end.split("-"))
    out: list[str] = []
    y, m = sy, sm
    while (y, m) <= (ey, em):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            y += 1; m = 1
    return out


def immutable_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(payload)
            handle.flush()
    except FileExistsError:
        if path.read_bytes() != payload:
            raise RuntimeError(f"immutable path collision: {path}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--destination", default=str(DEFAULT_DESTINATION))
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    destination = Path(args.destination)
    contract_bytes = CONTRACT.read_bytes()
    contract_sha = sha256(contract_bytes).hexdigest()
    contract = json.loads(contract_bytes)
    if contract.get("schema_id") != "EQS-FEASIBILITY-DATA-SCOPE-V4":
        raise SystemExit("V4_DATA_SCOPE_SCHEMA_INVALID")
    window = dict(contract["window"])
    if window.get("locked_oos_access") != "SEALED_NOT_PERMITTED" or window.get("end_month") >= "2026-04":
        raise SystemExit("V4_LOCKED_OOS_BOUNDARY_INVALID")
    scope_id = str(contract["scope_id"])
    month_list = months(str(window["start_month"]), str(window["end_month"]))
    if len(month_list) != int(window["month_count"]):
        raise SystemExit("V4_MONTH_COUNT_INVALID")
    coverage = dict(contract["coverage_policy"])
    if coverage.get("mode") != "SYNCHRONIZED_COMPLETE_MONTH_INTERSECTION":
        raise SystemExit("V4_COVERAGE_POLICY_INVALID")
    if coverage.get("imputation_permitted") is not False or coverage.get("partial_month_use_permitted") is not False:
        raise SystemExit("V4_COVERAGE_POLICY_MUST_FAIL_CLOSED")
    excluded = set(str(value) for value in coverage.get("excluded_months", []))
    eligible_months = [month for month in month_list if month not in excluded]
    if len(eligible_months) != int(coverage["eligible_month_count"]):
        raise SystemExit("V4_ELIGIBLE_MONTH_COUNT_INVALID")

    jobs: list[dict[str, str]] = []
    for series in contract["series"]:
        if int(series["availability_probe_passed_months"]) != len(month_list):
            raise SystemExit("V4_AVAILABILITY_PROBE_INCOMPLETE")
        if int(series["synchronized_eligible_months"]) != len(eligible_months):
            raise SystemExit("V4_SERIES_ELIGIBLE_MONTH_COUNT_INVALID")
        pattern = str(series["url_pattern"])
        for month in eligible_months:
            jobs.append({"series": str(series["id"]), "month": month, "url": pattern.replace("{YYYY-MM}", month)})
    if len(jobs) != int(contract["availability_summary"]["synchronized_required_objects"]):
        raise SystemExit("V4_REQUIRED_OBJECT_COUNT_INVALID")

    def acquire(job: dict[str, str]) -> dict[str, object]:
        req = urllib.request.Request(job["url"], headers={"User-Agent": "EQS-feasibility-v4/1.0", "Accept": "*/*"})
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read()
            status = int(response.status)
        if status != 200 or not raw:
            raise RuntimeError(f"download failed: {job['series']} {job['month']} status={status}")
        receipt, _rows = validate_basis_archive(
            raw,
            scope_id=scope_id,
            series=job["series"],
            month=job["month"],
            source_url=job["url"],
        )
        raw_path = destination / "raw" / job["series"] / job["month"] / f"{receipt.archive_sha256}.zip"
        receipt_path = destination / "receipts" / job["series"] / f"{job['month']}-{receipt.archive_sha256[:16]}.json"
        immutable_write(raw_path, raw)
        receipt_record = {**receipt.to_record(), "receipt_sha256": receipt.receipt_sha256}
        immutable_write(receipt_path, json.dumps(receipt_record, indent=2, sort_keys=True).encode("utf-8") + b"\n")
        return {
            "series": job["series"],
            "month": job["month"],
            "source_url": job["url"],
            "archive_sha256": receipt.archive_sha256,
            "archive_size_bytes": receipt.archive_size_bytes,
            "receipt_sha256": receipt.receipt_sha256,
            "row_count": receipt.row_count,
            "first_time_utc": receipt.first_time_utc,
            "last_time_utc": receipt.last_time_utc,
            "raw_path": str(raw_path),
            "receipt_path": str(receipt_path),
        }

    workers = max(1, min(int(args.workers), 12))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        receipts = list(pool.map(acquire, jobs))
    receipts.sort(key=lambda item: (str(item["series"]), str(item["month"])))
    by_series: dict[str, dict[str, int]] = {}
    for series in {str(item["series"]) for item in receipts}:
        rows = [item for item in receipts if item["series"] == series]
        by_series[series] = {
            "object_count": len(rows),
            "archive_size_bytes": sum(int(item["archive_size_bytes"]) for item in rows),
            "row_count": sum(int(item["row_count"]) for item in rows),
        }
    manifest = {
        "schema_id": "EQS-FEASIBILITY-V4-BASIS-ACQUISITION-MANIFEST-V1",
        "scope_id": scope_id,
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source_contract_path": str(CONTRACT.relative_to(ROOT)),
        "source_contract_sha256": contract_sha,
        "destination": str(destination),
        "coverage_policy": coverage,
        "excluded_months": sorted(excluded),
        "eligible_months": eligible_months,
        "object_count": len(receipts),
        "series_summary": by_series,
        "locked_oos_touched": False,
        "broker_submission_enabled": False,
        "live_authority": False,
        "receipts": receipts,
    }
    manifest["manifest_sha256"] = sha256(canonical(manifest)).hexdigest()
    manifest_path = destination / "FEAS-BINANCE-BTC-BASIS-V4-acquisition-manifest.json"
    encoded = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    if manifest_path.exists():
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        # created_at is operational metadata; require all immutable acquisition content to match.
        comparable_existing = dict(existing); comparable_existing.pop("created_at", None); comparable_existing.pop("manifest_sha256", None)
        comparable_new = dict(manifest); comparable_new.pop("created_at", None); comparable_new.pop("manifest_sha256", None)
        if comparable_existing != comparable_new:
            raise RuntimeError("existing V4 acquisition manifest differs")
    else:
        immutable_write(manifest_path, encoded)
    print(json.dumps({
        "status": "PASS",
        "scope_id": scope_id,
        "object_count": len(receipts),
        "total_archive_bytes": sum(int(item["archive_size_bytes"]) for item in receipts),
        "manifest_path": str(manifest_path),
        "manifest_sha256": manifest["manifest_sha256"],
        "series_summary": by_series,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

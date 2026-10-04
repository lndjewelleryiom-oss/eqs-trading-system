from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sys
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from quant_system.operations.storage_guard import evaluate_bounded_offhost_write, evaluate_storage_guard
from quant_system.research.feasibility_archive_v2 import validate_archive, write_immutable_receipt
from quant_system.research.feasibility_v2 import archive_urls, load_and_validate


CAMPAIGN_PATH = ROOT / "research" / "preregistrations" / "v2" / "FEAS-BINANCE-BTC-MA-001.json"


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def immutable_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(payload)
            handle.flush()
    except FileExistsError:
        if sha256(path.read_bytes()).hexdigest() != sha256(payload).hexdigest():
            raise RuntimeError(f"immutable archive differs: {path}")


def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "EQS-feasibility-v2/1.0"})
    with urllib.request.urlopen(request, timeout=60) as response:
        if int(response.status) != 200:
            raise RuntimeError(f"unexpected HTTP status {response.status}")
        payload = response.read()
    if not payload:
        raise RuntimeError("downloaded archive is empty")
    return payload


def head_content_length(url: str) -> int:
    request = urllib.request.Request(url, headers={"User-Agent": "EQS-feasibility-v2/1.0"}, method="HEAD")
    with urllib.request.urlopen(request, timeout=30) as response:
        if int(response.status) != 200:
            raise RuntimeError(f"unexpected HEAD HTTP status {response.status}")
        value = response.headers.get("Content-Length")
    if value is None or int(value) <= 0:
        raise RuntimeError("bounded off-host acquisition requires positive Content-Length")
    return int(value)


def bounded_expected_bytes(plan: tuple[dict[str, str], ...]) -> tuple[int, str]:
    probe_path = ROOT / "artifacts" / "test-evidence" / "EQS_FEAS_V2_BINANCE_PUBLIC_ARCHIVE_PROBE.json"
    if probe_path.is_file():
        try:
            probe = json.loads(probe_path.read_text(encoding="utf-8"))
            expected = probe.get("record_sha256")
            core = dict(probe)
            core.pop("record_sha256", None)
            if isinstance(expected, str) and sha256(canonical(core)).hexdigest() == expected:
                rows = {
                    str(row.get("url")): int(row.get("content_length"))
                    for row in probe.get("rows", [])
                    if int(row.get("status", 0)) == 200 and int(row.get("content_length", 0)) > 0
                }
                urls = [item["url"] for item in plan]
                if all(url in rows for url in urls):
                    return sum(rows[url] for url in urls), "SEALED_HEAD_PROBE"
        except (ValueError, TypeError, json.JSONDecodeError):
            pass
    return sum(head_content_length(item["url"]) for item in plan), "LIVE_HEAD_FALLBACK"


def main() -> int:
    parser = argparse.ArgumentParser(description="Bounded Binance public-archive acquisition for EQS feasibility v2.")
    parser.add_argument("--destination", required=True, help="Raw destination root; use external/off-host storage while local guard is red.")
    parser.add_argument("--max-objects", type=int, default=78)
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--bounded-offhost", action="store_true", help="Permit only a small, size-capped write to a different mounted/off-host volume.")
    args = parser.parse_args()

    if args.max_objects <= 0 or args.max_objects > 78:
        raise SystemExit("max-objects must be in [1,78]")

    campaign = load_and_validate(CAMPAIGN_PATH, project_root=ROOT)
    plan = archive_urls(campaign)[: args.max_objects]
    destination = Path(args.destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    guard = evaluate_storage_guard(destination)
    bounded_offhost = None
    expected_bytes = None
    if args.bounded_offhost:
        expected_bytes, size_authority = bounded_expected_bytes(plan)
        bounded_offhost = evaluate_bounded_offhost_write(
            destination,
            expected_bytes=expected_bytes,
            local_safety_path=ROOT,
            max_expected_bytes=64 * 1024 ** 2,
            destination_floor_bytes=1024 ** 3,
            local_critical_bytes=4 * 1024 ** 3,
            cache_multiplier=4,
        )

    plan_record = {
        "schema_id": "EQS-FEASIBILITY-V2-ACQUISITION-PLAN-V1",
        "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "campaign_id": campaign["campaign"]["campaign_id"],
        "campaign_fingerprint": campaign["campaign_fingerprint"],
        "object_count": len(plan),
        "destination": str(destination),
        "storage_guard": asdict(guard),
        "bounded_offhost": None if bounded_offhost is None else asdict(bounded_offhost),
        "expected_compressed_bytes": expected_bytes,
        "expected_size_authority": None if not args.bounded_offhost else size_authority,
        "objects": list(plan),
        "locked_oos_opened": False,
        "broker_submission_enabled": False,
        "live_authority": False,
    }
    plan_record["record_sha256"] = sha256(canonical(plan_record)).hexdigest()
    print(json.dumps(plan_record, sort_keys=True))
    if args.plan_only:
        return 0
    storage_authorised = guard.allow_new_research or bool(bounded_offhost and bounded_offhost.allowed)
    if not storage_authorised:
        print(
            json.dumps(
                {
                    "status": "BLOCKED",
                    "blocker": "STORAGE_GUARD_BLOCKS_NEW_RESEARCH",
                    "storage_state": guard.state,
                    "free_bytes": guard.free_bytes,
                    "reserve_bytes": guard.reserve_bytes,
                    "bounded_offhost_state": None if bounded_offhost is None else bounded_offhost.state,
                },
                sort_keys=True,
            )
        )
        return 2

    receipts: list[dict[str, object]] = []
    for index, item in enumerate(plan, start=1):
        series = item["series"]
        month = item["month"]
        url = item["url"]
        archive = fetch(url)
        receipt, _ = validate_archive(
            archive,
            campaign_id=campaign["campaign"]["campaign_id"],
            series=series,
            month=month,
            source_url=url,
        )
        raw_path = destination / "raw" / series / month / f"{receipt.archive_sha256}.zip"
        receipt_path = destination / "receipts" / series / f"{month}-{receipt.archive_sha256[:16]}.json"
        immutable_write(raw_path, archive)
        write_immutable_receipt(receipt_path, receipt)
        receipts.append(
            {
                "index": index,
                "series": series,
                "month": month,
                "source_url": url,
                "archive_sha256": receipt.archive_sha256,
                "receipt_sha256": receipt.receipt_sha256,
                "raw_path": str(raw_path),
                "receipt_path": str(receipt_path),
                "row_count": receipt.row_count,
            }
        )

    manifest = {
        "schema_id": "EQS-FEASIBILITY-V2-ACQUISITION-MANIFEST-V1",
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "campaign_id": campaign["campaign"]["campaign_id"],
        "campaign_fingerprint": campaign["campaign_fingerprint"],
        "classification": "EXPLORATORY_NON_EVIDENTIARY",
        "storage_authority": "NORMAL_LOCAL_GUARD" if guard.allow_new_research else "BOUNDED_OFFHOST",
        "expected_compressed_bytes": expected_bytes,
        "receipts": receipts,
        "object_count": len(receipts),
        "locked_oos_opened": False,
        "r13_admission_authority": False,
        "broker_submission_enabled": False,
        "live_authority": False,
    }
    manifest["manifest_sha256"] = sha256(canonical(manifest)).hexdigest()
    manifest_path = destination / "FEAS-BINANCE-BTC-MA-001-acquisition-manifest.json"
    immutable_write(manifest_path, json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8") + b"\n")
    print(json.dumps({"status": "PASS", "object_count": len(receipts), "manifest_sha256": manifest["manifest_sha256"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

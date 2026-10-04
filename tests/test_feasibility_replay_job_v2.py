from __future__ import annotations

import json
from pathlib import Path

from quant_system.operations.storage_guard import StorageGuardState
from quant_system.research.feasibility_replay_job_v2 import prepare_replay_job
from quant_system.research.feasibility_v2 import archive_urls, load_and_validate


ROOT = Path(__file__).resolve().parents[1]
CAMPAIGN = ROOT / "research" / "preregistrations" / "v2" / "FEAS-BINANCE-BTC-MA-001.json"


def complete_manifest() -> dict:
    campaign = load_and_validate(CAMPAIGN, project_root=ROOT)
    receipts = []
    for index, item in enumerate(archive_urls(campaign), start=1):
        receipts.append(
            {
                "index": index,
                "series": item["series"],
                "month": item["month"],
                "source_url": item["url"],
                "archive_sha256": f"{index:064x}"[-64:],
                "receipt_sha256": f"{index + 100:064x}"[-64:],
                "raw_path": f"raw/{index}.zip",
                "receipt_path": f"receipts/{index}.json",
                "row_count": 1,
            }
        )
    return {
        "schema_id": "EQS-FEASIBILITY-V2-ACQUISITION-MANIFEST-V1",
        "campaign_id": campaign["campaign"]["campaign_id"],
        "campaign_fingerprint": campaign["campaign_fingerprint"],
        "classification": "EXPLORATORY_NON_EVIDENTIARY",
        "receipts": receipts,
        "object_count": len(receipts),
        "locked_oos_opened": False,
        "r13_admission_authority": False,
        "broker_submission_enabled": False,
        "live_authority": False,
    }


def normal_guard(path) -> StorageGuardState:
    return StorageGuardState(
        path=str(path),
        free_bytes=20 * 1024**3,
        free_gib=20.0,
        reserve_bytes=8 * 1024**3,
        critical_bytes=4 * 1024**3,
        allow_new_research=True,
        allow_large_downloads=True,
        state="NORMAL",
        reason="test",
    )


def test_missing_acquisition_manifest_blocks(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "quant_system.research.feasibility_replay_job_v2.evaluate_storage_guard",
        normal_guard,
    )
    decision = prepare_replay_job(project_root=ROOT, acquisition_root=tmp_path)
    assert decision.status == "BLOCKED"
    assert "ACQUISITION_MANIFEST_MISSING" in decision.blockers
    assert decision.locked_oos_opened is False


def test_complete_manifest_and_normal_storage_prepare_real_replay(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "FEAS-BINANCE-BTC-MA-001-acquisition-manifest.json").write_text(
        json.dumps(complete_manifest()), encoding="utf-8"
    )
    monkeypatch.setattr(
        "quant_system.research.feasibility_replay_job_v2.evaluate_storage_guard",
        normal_guard,
    )
    decision = prepare_replay_job(project_root=ROOT, acquisition_root=tmp_path)
    assert decision.status == "READY_FOR_REAL_TRAIN_VALIDATION_REPLAY"
    assert decision.expected_archive_objects == 78
    assert decision.admitted_archive_objects == 78
    assert decision.counts_toward_168h_100_trade_gate is False


def test_incomplete_coverage_blocks(tmp_path: Path, monkeypatch) -> None:
    document = complete_manifest()
    document["receipts"].pop()
    document["object_count"] -= 1
    (tmp_path / "FEAS-BINANCE-BTC-MA-001-acquisition-manifest.json").write_text(
        json.dumps(document), encoding="utf-8"
    )
    monkeypatch.setattr(
        "quant_system.research.feasibility_replay_job_v2.evaluate_storage_guard",
        normal_guard,
    )
    decision = prepare_replay_job(project_root=ROOT, acquisition_root=tmp_path)
    assert decision.status == "BLOCKED"
    assert "ACQUISITION_COVERAGE_INCOMPLETE" in decision.blockers


def test_storage_backpressure_blocks_even_complete_manifest(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "FEAS-BINANCE-BTC-MA-001-acquisition-manifest.json").write_text(
        json.dumps(complete_manifest()), encoding="utf-8"
    )
    monkeypatch.setattr(
        "quant_system.research.feasibility_replay_job_v2.evaluate_storage_guard",
        lambda path: StorageGuardState(
            path=str(path), free_bytes=5 * 1024**3, free_gib=5.0,
            reserve_bytes=8 * 1024**3, critical_bytes=4 * 1024**3,
            allow_new_research=False, allow_large_downloads=False,
            state="RESEARCH_BACKPRESSURE", reason="test",
        ),
    )
    decision = prepare_replay_job(project_root=ROOT, acquisition_root=tmp_path)
    assert decision.status == "BLOCKED"
    assert "STORAGE_GUARD_BLOCKS_NEW_RESEARCH" in decision.blockers

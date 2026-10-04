from __future__ import annotations

import copy
import json
from pathlib import Path

from quant_system.research.feasibility_v2 import (
    archive_urls,
    campaign_fingerprint,
    load_and_validate,
    validate_campaign,
)


ROOT = Path(__file__).resolve().parents[1]
CAMPAIGN = ROOT / "research" / "preregistrations" / "v2" / "FEAS-BINANCE-BTC-MA-001.json"


def load() -> dict:
    return json.loads(CAMPAIGN.read_text(encoding="utf-8"))


def test_feasibility_campaign_is_sealed_and_valid() -> None:
    record = load_and_validate(CAMPAIGN, project_root=ROOT)
    assert record["campaign_fingerprint"] == campaign_fingerprint(record["campaign"])
    assert record["campaign"]["classification"] == "EXPLORATORY_NON_EVIDENTIARY"
    assert record["campaign"]["safety"]["broker_submission_enabled"] is False
    assert record["campaign"]["safety"]["live_authority"] is False


def test_archive_plan_covers_train_validation_only() -> None:
    rows = archive_urls(load())
    assert len(rows) == 78
    assert rows[0]["month"] == "2023-01"
    assert rows[-1]["month"] == "2026-03"
    assert {row["series"] for row in rows} == {"KLINES_5M", "FUNDING_RATE"}
    assert all(row["month"] < "2026-04" for row in rows)


def test_campaign_cannot_claim_historical_pit_metadata() -> None:
    record = load()
    record["campaign"]["source_contract"]["historical_pit_metadata_claim"] = True
    record["campaign_fingerprint"] = campaign_fingerprint(record["campaign"])
    failures = validate_campaign(record, project_root=ROOT)
    assert any("HISTORICAL_PIT_METADATA" in failure for failure in failures)


def test_campaign_cannot_open_locked_oos() -> None:
    record = load()
    record["campaign"]["research_rules"]["may_open_locked_oos"] = True
    record["campaign_fingerprint"] = campaign_fingerprint(record["campaign"])
    failures = validate_campaign(record, project_root=ROOT)
    assert any("LOCKED_OOS" in failure for failure in failures)


def test_campaign_cannot_gain_r13_or_live_promotion_authority() -> None:
    record = load()
    record["campaign"]["promotion_prohibitions"] = ["LIVE_PROMOTION"]
    record["campaign_fingerprint"] = campaign_fingerprint(record["campaign"])
    failures = validate_campaign(record, project_root=ROOT)
    assert any("PROMOTION_PROHIBITIONS_INCOMPLETE" in failure for failure in failures)


def test_broker_and_live_safety_are_fail_closed() -> None:
    for key in ("broker_submission_enabled", "live_authority"):
        record = load()
        record["campaign"]["safety"][key] = True
        record["campaign_fingerprint"] = campaign_fingerprint(record["campaign"])
        failures = validate_campaign(record, project_root=ROOT)
        assert failures


def test_fingerprint_detects_unsealed_edit() -> None:
    record = load()
    edited = copy.deepcopy(record)
    edited["campaign"]["strategy"]["fast_bars"] = 13
    failures = validate_campaign(edited, project_root=ROOT)
    assert "SEMANTIC:FINGERPRINT_MISMATCH" in failures


def test_shakedown_is_explicitly_non_qualifying() -> None:
    gate = load()["campaign"]["shakedown_paper_gate"]
    assert gate["classification"] == "SHAKEDOWN_NON_QUALIFYING"
    assert gate["requires_positive_validation_pnl"] is False
    assert gate["counts_toward_168h_100_trade_gate"] is False

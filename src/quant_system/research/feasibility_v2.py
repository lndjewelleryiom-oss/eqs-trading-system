from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Iterable

from jsonschema import Draft202012Validator


SCHEMA_ID = "EQS-FEASIBILITY-CRYPTO-CAMPAIGN-V2"
CLASSIFICATION = "EXPLORATORY_NON_EVIDENTIARY"
PROHIBITED_PROMOTIONS = {
    "R13_CERTIFICATION",
    "J25_LOCKED_OOS",
    "J26_GENUINE_CANDIDATE",
    "LIVE_PROMOTION",
    "FORWARD_168H_100_TRADE_CREDIT",
}


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def campaign_fingerprint(campaign: dict[str, Any]) -> str:
    return sha256(canonical_bytes(campaign)).hexdigest()


def iter_months(start: str, end: str) -> tuple[str, ...]:
    start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
    end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
    if start_dt.tzinfo is None or end_dt.tzinfo is None:
        raise ValueError("periods must be timezone aware")
    if start_dt > end_dt:
        raise ValueError("start must not follow end")
    year, month = start_dt.year, start_dt.month
    out: list[str] = []
    while (year, month) <= (end_dt.year, end_dt.month):
        out.append(f"{year:04d}-{month:02d}")
        month += 1
        if month == 13:
            year += 1
            month = 1
    return tuple(out)


def archive_urls(record: dict[str, Any]) -> tuple[dict[str, str], ...]:
    campaign = record["campaign"]
    scope = campaign["scope"]
    periods = campaign["periods"]
    if scope["venue"] != "BINANCE_USDM" or scope["instrument"] != "BTCUSDT":
        raise ValueError("v2 feasibility archive planner only supports BINANCE_USDM BTCUSDT")
    months = iter_months(periods["training_start"], periods["validation_end"])
    rows: list[dict[str, str]] = []
    for month in months:
        rows.append(
            {
                "series": "KLINES_5M",
                "month": month,
                "url": (
                    "https://data.binance.vision/data/futures/um/monthly/klines/"
                    f"BTCUSDT/5m/BTCUSDT-5m-{month}.zip"
                ),
            }
        )
        rows.append(
            {
                "series": "FUNDING_RATE",
                "month": month,
                "url": (
                    "https://data.binance.vision/data/futures/um/monthly/fundingRate/"
                    f"BTCUSDT/BTCUSDT-fundingRate-{month}.zip"
                ),
            }
        )
    return tuple(rows)


def _load_json(path: str | Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("campaign record must be an object")
    return value


def load_schema(project_root: str | Path) -> dict[str, Any]:
    root = Path(project_root)
    return _load_json(root / "schemas" / "feasibility_crypto_campaign_v2.schema.json")


def validate_campaign(
    record: dict[str, Any],
    *,
    schema: dict[str, Any] | None = None,
    project_root: str | Path | None = None,
) -> tuple[str, ...]:
    failures: list[str] = []
    if schema is None:
        if project_root is None:
            raise ValueError("schema or project_root is required")
        schema = load_schema(project_root)
    validator = Draft202012Validator(schema)
    for error in sorted(validator.iter_errors(record), key=lambda item: list(item.absolute_path)):
        path = ".".join(str(part) for part in error.absolute_path) or "$"
        failures.append(f"SCHEMA:{path}:{error.message}")

    campaign = record.get("campaign")
    if not isinstance(campaign, dict):
        return tuple(failures or ["SEMANTIC:CAMPAIGN_REQUIRED"])

    expected_fingerprint = campaign_fingerprint(campaign)
    if record.get("campaign_fingerprint") != expected_fingerprint:
        failures.append("SEMANTIC:FINGERPRINT_MISMATCH")

    periods = campaign.get("periods", {})
    try:
        train_start = datetime.fromisoformat(str(periods["training_start"]).replace("Z", "+00:00"))
        train_end = datetime.fromisoformat(str(periods["training_end"]).replace("Z", "+00:00"))
        validation_start = datetime.fromisoformat(str(periods["validation_start"]).replace("Z", "+00:00"))
        validation_end = datetime.fromisoformat(str(periods["validation_end"]).replace("Z", "+00:00"))
        oos_start = datetime.fromisoformat(str(periods["locked_oos_start"]).replace("Z", "+00:00"))
        oos_end = datetime.fromisoformat(str(periods["locked_oos_end"]).replace("Z", "+00:00"))
        if not (train_start <= train_end < validation_start <= validation_end < oos_start <= oos_end):
            failures.append("SEMANTIC:PERIOD_ORDER_INVALID")
    except (KeyError, ValueError, TypeError):
        failures.append("SEMANTIC:PERIOD_PARSE_FAILED")

    source = campaign.get("source_contract", {})
    if source.get("required_series") != ["KLINES_5M", "FUNDING_RATE"]:
        failures.append("SEMANTIC:SOURCE_SERIES_NOT_FEASIBILITY_MINIMUM")
    if source.get("historical_pit_metadata_claim") is not False:
        failures.append("SEMANTIC:HISTORICAL_PIT_METADATA_MUST_NOT_BE_CLAIMED")

    rules = campaign.get("research_rules", {})
    if rules.get("empirical_outcomes_consumed") is not False:
        failures.append("SEMANTIC:EMPIRICAL_OUTCOMES_ALREADY_CONSUMED")
    if rules.get("may_open_locked_oos") is not False:
        failures.append("SEMANTIC:LOCKED_OOS_MUST_REMAIN_CLOSED")
    if rules.get("may_write_r13_admission_ledger") is not False:
        failures.append("SEMANTIC:R13_LEDGER_WRITE_FORBIDDEN")

    prohibitions = set(campaign.get("promotion_prohibitions", []))
    if not PROHIBITED_PROMOTIONS.issubset(prohibitions):
        failures.append("SEMANTIC:PROMOTION_PROHIBITIONS_INCOMPLETE")

    safety = campaign.get("safety", {})
    if safety.get("broker_submission_enabled") is not False:
        failures.append("SEMANTIC:BROKER_SUBMISSION_MUST_BE_FALSE")
    if safety.get("live_authority") is not False:
        failures.append("SEMANTIC:LIVE_AUTHORITY_MUST_BE_FALSE")
    if safety.get("options_included") is not False:
        failures.append("SEMANTIC:OPTIONS_MUST_REMAIN_EXCLUDED")
    if safety.get("futures_dependency") is not False:
        failures.append("SEMANTIC:FUTURES_DEPENDENCY_FORBIDDEN")

    planned = archive_urls(record)
    expected_months = int(source.get("monthly_archive_count", -1))
    if len(planned) != expected_months * 2:
        failures.append("SEMANTIC:MONTHLY_ARCHIVE_COUNT_MISMATCH")
    if any(row["month"] >= "2026-04" for row in planned):
        failures.append("SEMANTIC:ARCHIVE_PLAN_TOUCHES_LOCKED_OOS")
    return tuple(failures)


def load_and_validate(path: str | Path, *, project_root: str | Path) -> dict[str, Any]:
    record = _load_json(path)
    failures = validate_campaign(record, project_root=project_root)
    if failures:
        raise ValueError(";".join(failures))
    return record


def seal_campaign(campaign: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_id": SCHEMA_ID,
        "campaign": campaign,
        "campaign_fingerprint": campaign_fingerprint(campaign),
    }

from datetime import datetime, timedelta, timezone

from quant_system.data.failure_policy import (
    DataHealth,
    ScopedDataFailurePolicy,
    SeriesObservation,
    SeriesRequirement,
)


NOW = datetime(2026, 10, 4, 15, tzinfo=timezone.utc)


def policy():
    return ScopedDataFailurePolicy({
        "CRYPTO_ALPHA": (
            SeriesRequirement("BTC_TRADES", timedelta(seconds=5)),
            SeriesRequirement("BTC_BOOK", timedelta(seconds=2)),
        ),
        "EQUITY_FORWARD": (
            SeriesRequirement("SPY_BARS", timedelta(minutes=5)),
        ),
    })


def healthy(series_id, *, generation=10, seconds_ago=1):
    return SeriesObservation(
        series_id=series_id,
        observed_at=NOW - timedelta(seconds=seconds_ago),
        generation=generation,
    )


def test_gap_blocks_only_dependent_scope():
    p = policy()
    observations = {
        "BTC_TRADES": healthy("BTC_TRADES"),
        "BTC_BOOK": SeriesObservation(
            "BTC_BOOK", NOW - timedelta(seconds=1), 10, gap_detected=True
        ),
        "SPY_BARS": healthy("SPY_BARS"),
    }
    crypto = p.assess_scope("CRYPTO_ALPHA", observations, now=NOW)
    equity = p.assess_scope("EQUITY_FORWARD", observations, now=NOW)
    assert crypto.state == "BLOCKED"
    assert "BTC_BOOK:COVERAGE_GAP" in crypto.blockers
    assert equity.state == "PASS"
    assert p.blocked_scopes() == ("CRYPTO_ALPHA",)


def test_stale_missing_clock_skew_schema_and_correction_are_fail_closed():
    req = SeriesRequirement("X", timedelta(seconds=5))
    p = ScopedDataFailurePolicy({"S": (req,)})

    cases = [
        (None, DataHealth.MISSING),
        (SeriesObservation("X", NOW - timedelta(seconds=10), 1), DataHealth.STALE),
        (SeriesObservation("X", NOW + timedelta(seconds=10), 1), DataHealth.CLOCK_SKEW),
        (SeriesObservation("X", NOW, 1, schema_compatible=False), DataHealth.SCHEMA_CHANGE),
        (SeriesObservation("X", NOW, 1, correction_pending=True), DataHealth.CORRECTION_PENDING),
        (SeriesObservation("X", NOW, 1, quarantined=True), DataHealth.QUARANTINED),
    ]
    for obs, expected in cases:
        row = p.assess_series(req, obs, now=NOW)
        assert row.state is expected


def test_recovery_requires_strictly_newer_healthy_generation():
    p = policy()
    bad = {
        "BTC_TRADES": healthy("BTC_TRADES", generation=7),
        "BTC_BOOK": SeriesObservation(
            "BTC_BOOK", NOW, 7, gap_detected=True
        ),
    }
    blocked = p.assess_scope("CRYPTO_ALPHA", bad, now=NOW)
    assert blocked.required_revalidation_generation == 8

    same_generation = {
        "BTC_TRADES": healthy("BTC_TRADES", generation=7),
        "BTC_BOOK": healthy("BTC_BOOK", generation=7),
    }
    waiting = p.assess_scope("CRYPTO_ALPHA", same_generation, now=NOW)
    assert waiting.state == "REVALIDATION_REQUIRED"
    assert not waiting.can_resume

    fresh = {
        "BTC_TRADES": healthy("BTC_TRADES", generation=8),
        "BTC_BOOK": healthy("BTC_BOOK", generation=8),
    }
    passed = p.assess_scope("CRYPTO_ALPHA", fresh, now=NOW)
    assert passed.state == "PASS"
    assert passed.can_resume
    assert p.blocked_scopes() == ()


def test_no_silent_zero_or_holiday_substitution_for_missing_series():
    p = policy()
    result = p.assess_scope(
        "EQUITY_FORWARD",
        {},
        now=NOW,
    )
    assert result.state == "BLOCKED"
    assert result.series[0].state is DataHealth.MISSING
    assert result.series[0].age_seconds is None
    assert result.blockers == ("SPY_BARS:MISSING_SERIES",)

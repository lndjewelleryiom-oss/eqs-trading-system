from datetime import datetime, timedelta, timezone
from decimal import Decimal

from quant_system.data.contracts import DatasetManifest
from quant_system.data.instruments import Instrument, InstrumentMaster
from quant_system.data.models import MarketObservation
from quant_system.data.quality import DataQualityGate, QualityDisposition
from quant_system.data.raw_store import ImmutableRawStore
from quant_system.data.replay import DeterministicReplay


def obs(*, event_offset=0, available_offset=0, received_offset=None, value=1.0, confidence=1.0, corruption=False):
    base = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    received_offset = available_offset if received_offset is None else received_offset
    return MarketObservation(
        dataset_id="d1", symbol="XYZ",
        event_time=base + timedelta(seconds=event_offset),
        published_at=base + timedelta(seconds=available_offset),
        available_at=base + timedelta(seconds=available_offset),
        received_at=base + timedelta(seconds=received_offset),
        value=value, confidence=confidence, suspected_corruption=corruption,
    )


def test_raw_store_is_content_addressed_and_idempotent(tmp_path):
    store = ImmutableRawStore(tmp_path)
    a = store.put(b"same payload")
    b = store.put(b"same payload")
    assert a.content_hash == b.content_hash
    assert a.uri == b.uri
    assert store.get(a.content_hash) == b"same payload"


def test_manifest_fingerprint_is_stable_for_same_content():
    a = DatasetManifest.create(dataset_id="d", source="x", schema_version="1", content_hashes=("abc",))
    b = DatasetManifest.create(dataset_id="d", source="x", schema_version="1", content_hashes=("abc",))
    assert a.fingerprint() == b.fingerprint()


def test_quality_gate_quarantines_low_confidence_and_corruption():
    gate = DataQualityGate()
    assert gate.evaluate(obs(confidence=0.4)).disposition == QualityDisposition.QUARANTINE
    assert gate.evaluate(obs(corruption=True)).disposition == QualityDisposition.QUARANTINE


def test_quality_gate_rejects_impossible_receive_time():
    gate = DataQualityGate()
    result = gate.evaluate(obs(available_offset=10, received_offset=9))
    assert result.disposition == QualityDisposition.REJECT
    assert "RECEIVED_BEFORE_AVAILABLE" in result.reason_codes


def test_replay_is_deterministic_and_point_in_time():
    base = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    observations = [
        obs(event_offset=3, available_offset=3, value=3),
        obs(event_offset=1, available_offset=1, value=1),
        obs(event_offset=2, available_offset=2, value=2),
    ]
    r1 = [x.value for x in DeterministicReplay(observations).iter_until(base + timedelta(seconds=2))]
    r2 = [x.value for x in DeterministicReplay(reversed(observations)).iter_until(base + timedelta(seconds=2))]
    assert r1 == r2 == [1, 2]


def test_instrument_master_resolves_canonical_identity():
    instrument = Instrument("BTC-USD:EX", "BTC-USD", "crypto", "EX", "USD", Decimal("0.01"), Decimal("0.0001"))
    master = InstrumentMaster((instrument,))
    assert master.resolve("EX", "BTC-USD").instrument_id == "BTC-USD:EX"

from datetime import time
from quant_system.data.calendars import TradingCalendar, CONTINUOUS_24_7
from quant_system.data.views import latest_point_in_time_view


def test_trading_calendar_respects_weekdays_and_hours():
    cal = TradingCalendar(
        calendar_id="TEST", timezone_name="UTC",
        open_time=time(9, 30), close_time=time(16, 0),
        weekdays=frozenset({0, 1, 2, 3, 4}),
    )
    assert cal.is_open(datetime(2026, 1, 5, 10, 0, tzinfo=timezone.utc))  # Monday
    assert not cal.is_open(datetime(2026, 1, 5, 16, 0, tzinfo=timezone.utc))
    assert not cal.is_open(datetime(2026, 1, 4, 10, 0, tzinfo=timezone.utc))  # Sunday


def test_continuous_calendar_is_open_on_weekend():
    assert CONTINUOUS_24_7.is_open(datetime(2026, 1, 4, 12, 0, tzinfo=timezone.utc))


def test_point_in_time_revision_view_does_not_leak_future_revision():
    base = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    first = MarketObservation("fund", "XYZ", base, base, base, base, 10.0, revision=0)
    revision = MarketObservation(
        "fund", "XYZ", base, base + timedelta(hours=1),
        base + timedelta(hours=1), base + timedelta(hours=1), 12.0, revision=1,
    )
    early = latest_point_in_time_view([first, revision], base + timedelta(minutes=30))
    late = latest_point_in_time_view([first, revision], base + timedelta(hours=2))
    assert len(early) == 1 and early[0].value == 10.0 and early[0].revision == 0
    assert len(late) == 1 and late[0].value == 12.0 and late[0].revision == 1

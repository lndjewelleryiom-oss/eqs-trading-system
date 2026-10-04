from datetime import datetime, timedelta, timezone

import pytest

from quant_system.data.models import MarketObservation, TemporalLeakageError


def test_rejects_future_available_information():
    t = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    obs = MarketObservation("macro-cpi", "US_CPI", t, t, t + timedelta(minutes=1), t + timedelta(minutes=1), 2.4)
    with pytest.raises(TemporalLeakageError):
        obs.assert_usable_at(t)


def test_allows_information_only_after_availability_time():
    t = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    obs = MarketObservation("news", "ABC", t, t, t + timedelta(seconds=2), t + timedelta(seconds=2), 1.0)
    obs.assert_usable_at(t + timedelta(seconds=2))

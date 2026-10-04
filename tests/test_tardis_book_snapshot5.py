from datetime import timezone
from decimal import Decimal
import gzip
from hashlib import sha256

import pytest

from quant_system.data.crypto_perps.models import (
    BookLevel,
    BookUpdate,
    EventKind,
    MarketDataMeta,
)
from quant_system.features.engine import CryptoPerpetualFeatureEngine
from quant_system.features.models import FeatureInputBatch
from quant_system.research.tardis_provider import TardisProviderAdapter


def _raw():
    header = [
        "exchange","symbol","timestamp","local_timestamp",
    ]
    for i in range(5):
        header += [
            f"asks[{i}].price", f"asks[{i}].amount",
            f"bids[{i}].price", f"bids[{i}].amount",
        ]
    values = ["bybit","BTCUSDT","1672531200000000","1672531200000100"]
    asks = [("101","6"),("102","4"),("103","3"),("104","2"),("105","1")]
    bids = [("100","3"),("99","2"),("98","2"),("97","1"),("96","1")]
    for i in range(5):
        values += [asks[i][0],asks[i][1],bids[i][0],bids[i][1]]
    return gzip.compress((",".join(header)+"\n"+",".join(values)+"\n").encode())


def test_book_snapshot5_parser_yields_exact_five_levels():
    row = TardisProviderAdapter().parse_book_snapshot_5(_raw())[0]
    assert len(row.bids) == 5
    assert len(row.asks) == 5
    assert row.bids[0] == (Decimal("100"), Decimal("3"))
    assert row.asks[0] == (Decimal("101"), Decimal("6"))
    assert row.timestamp.tzinfo == timezone.utc
    assert row.local_timestamp >= row.timestamp


def test_book_snapshot5_is_sufficient_for_registered_l5_micro_features():
    raw = _raw()
    row = TardisProviderAdapter().parse_book_snapshot_5(raw)[0]
    raw_sha = sha256(raw).hexdigest()
    meta = MarketDataMeta(
        venue="BYBIT_LINEAR",
        instrument_id="BTC-USDT-PERP:BYBIT_LINEAR",
        venue_symbol="BTCUSDT",
        kind=EventKind.BOOK_SNAPSHOT,
        event_time=row.timestamp,
        published_at=row.timestamp,
        available_at=row.local_timestamp,
        received_at=row.local_timestamp,
        source_channel="tardis/book_snapshot_5",
        source_sequence=None,
        raw_sha256=raw_sha,
    )
    event = BookUpdate(
        meta=meta,
        bids=tuple(BookLevel(p,q) for p,q in row.bids),
        asks=tuple(BookLevel(p,q) for p,q in row.asks),
    )
    batch = FeatureInputBatch(
        instrument_id=meta.instrument_id,
        venue=meta.venue,
        decision_time=row.local_timestamp,
        events=(event,),
        dataset_fingerprints=(raw_sha,),
        universe_version="test-universe-v1",
    )
    values = {
        rec.definition.name: rec.value
        for rec in CryptoPerpetualFeatureEngine().compute(batch).records
    }
    assert values["micro.spread_bps"] == pytest.approx((1 / 100.5) * 10000)
    assert values["micro.book_imbalance_l5"] == pytest.approx((9 - 16) / (9 + 16))
    assert values["micro.microprice_deviation_bps"] == pytest.approx(
        (((101 * 3 + 100 * 6) / 9) - 100.5) / 100.5 * 10000
    )
    assert values["micro.top_depth_notional"] == pytest.approx(
        100*3 + 99*2 + 98*2 + 97*1 + 96*1
        + 101*6 + 102*4 + 103*3 + 104*2 + 105*1
    )


def test_crossed_snapshot_fails_closed():
    raw = _raw()
    text = gzip.decompress(raw).decode().replace("101,6,100,3", "99,6,100,3", 1)
    with pytest.raises(Exception, match="crossed"):
        TardisProviderAdapter().parse_book_snapshot_5(gzip.compress(text.encode()))

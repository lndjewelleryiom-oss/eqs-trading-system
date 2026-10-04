from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Mapping

from .models import (
    BookLevel,
    BookUpdate,
    EventKind,
    MarketDataMeta,
    PerpetualInstrumentDefinition,
    PerpetualStateEvent,
    TradeEvent,
)
from .research_datasets import ResearchEvent, event_to_record

UTC = timezone.utc


@dataclass(frozen=True, slots=True)
class RawCaptureReceipt:
    venue: str
    label: str
    url: str
    filename: str
    available_at: datetime
    received_at: datetime
    sha256: str
    byte_count: int
    status: int

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "RawCaptureReceipt":
        receipt = cls(
            venue=str(record["venue"]),
            label=str(record["label"]),
            url=str(record["url"]),
            filename=str(record["filename"]),
            available_at=_parse_dt(record["available_at"]),
            received_at=_parse_dt(record["received_at"]),
            sha256=str(record["sha256"]),
            byte_count=int(record["bytes"]),
            status=int(record["status"]),
        )
        if len(receipt.sha256) != 64:
            raise ValueError("raw capture receipt must contain a SHA-256 digest")
        if receipt.available_at > receipt.received_at:
            raise ValueError("raw capture receipt availability cannot follow receipt time")
        if receipt.status != 200:
            raise ValueError(f"raw capture was not successful: {receipt.venue}/{receipt.label}")
        return receipt


@dataclass(frozen=True, slots=True)
class ClockCorrection:
    venue: str
    label: str
    source_available_at: datetime
    source_received_at: datetime
    published_at: datetime
    effective_available_at: datetime
    effective_received_at: datetime

    def to_record(self) -> dict[str, str]:
        return {
            "venue": self.venue,
            "label": self.label,
            "source_available_at": self.source_available_at.isoformat(),
            "source_received_at": self.source_received_at.isoformat(),
            "published_at": self.published_at.isoformat(),
            "effective_available_at": self.effective_available_at.isoformat(),
            "effective_received_at": self.effective_received_at.isoformat(),
            "policy": "clamp-forward-only; never make venue data knowable before its venue timestamp",
        }


@dataclass(frozen=True, slots=True)
class RealMarketPopulation:
    receipts: tuple[RawCaptureReceipt, ...]
    definitions: tuple[PerpetualInstrumentDefinition, ...]
    events: tuple[ResearchEvent, ...]
    clock_corrections: tuple[ClockCorrection, ...]
    source_bundle_sha256: str

    @property
    def raw_hashes(self) -> frozenset[str]:
        return frozenset(receipt.sha256 for receipt in self.receipts)

    @property
    def decision_time(self) -> datetime:
        latest = max(
            [definition.available_at for definition in self.definitions]
            + [event.meta.available_at for event in self.events]
        )
        return latest + timedelta(microseconds=1)

    @property
    def start_time(self) -> datetime:
        return min(event.meta.event_time for event in self.events)

    def replay_fingerprint(self) -> str:
        payload = [event_to_record(event) for event in self.events]
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        return sha256(encoded).hexdigest()


def _parse_dt(value: Any) -> datetime:
    text = str(value).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return parsed.astimezone(UTC)


def _ms(value: int | str) -> datetime:
    return datetime.fromtimestamp(int(value) / 1000, UTC)


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    return Decimal(str(value))


def _load_bundle(path: str | Path) -> tuple[dict[str, Any], str]:
    raw = Path(path).read_bytes()
    payload = json.loads(raw)
    if payload.get("classification") != "REAL MARKET":
        raise ValueError("source bundle is not classified REAL MARKET")
    verification = payload.get("source_verification", {})
    if verification.get("all_15_raw_hashes_match") is not True:
        raise ValueError("source bundle does not attest verified raw hashes")
    return payload, sha256(raw).hexdigest()


def _canonical_status(venue: str, status: str) -> str:
    active = {
        "BINANCE_USDM": {"TRADING"},
        "BYBIT_LINEAR": {"TRADING"},
        "OKX_SWAP": {"LIVE"},
    }
    return "TRADING" if status.upper() in active.get(venue, set()) else status.upper()


def load_public_rest_capture(path: str | Path) -> RealMarketPopulation:
    bundle, bundle_hash = _load_bundle(path)
    receipts = tuple(RawCaptureReceipt.from_record(record) for record in bundle["receipts"])
    by_key = {(r.venue, r.label): r for r in receipts}
    if len(by_key) != len(receipts):
        raise ValueError("duplicate raw capture receipt")

    corrections: list[ClockCorrection] = []

    def timing(venue: str, label: str, published_at: datetime) -> tuple[datetime, datetime, RawCaptureReceipt]:
        receipt = by_key[(venue, label)]
        available = max(receipt.available_at, published_at)
        received = max(receipt.received_at, available)
        if available != receipt.available_at or received != receipt.received_at:
            corrections.append(
                ClockCorrection(
                    venue=venue,
                    label=label,
                    source_available_at=receipt.available_at,
                    source_received_at=receipt.received_at,
                    published_at=published_at,
                    effective_available_at=available,
                    effective_received_at=received,
                )
            )
        return available, received, receipt

    instruments = bundle["instruments"]
    definitions: list[PerpetualInstrumentDefinition] = []
    instrument_ids = {
        "BINANCE_USDM": "BTC-USDT-PERP:BINANCE_USDM",
        "BYBIT_LINEAR": "BTC-USDT-PERP:BYBIT_LINEAR",
        "OKX_SWAP": "BTC-USDT-PERP:OKX_SWAP",
    }
    instrument_receipt_labels = {
        "BINANCE_USDM": "exchange_info",
        "BYBIT_LINEAR": "instrument_info",
        "OKX_SWAP": "instrument_info",
    }
    listing_fields = {
        "BINANCE_USDM": "onboardDate",
        "BYBIT_LINEAR": "launchTime",
        "OKX_SWAP": "listTime",
    }
    status_fields = {
        "BINANCE_USDM": "status",
        "BYBIT_LINEAR": "status",
        "OKX_SWAP": "status",
    }
    for venue in ("BINANCE_USDM", "BYBIT_LINEAR", "OKX_SWAP"):
        item = instruments[venue]
        receipt = by_key[(venue, instrument_receipt_labels[venue])]
        # REST instrument snapshots do not expose a reliable per-definition publish timestamp.
        # Using the completed local HTTP-receipt timestamp is conservative and PIT-safe.
        published = receipt.available_at
        available = receipt.available_at
        received = receipt.received_at
        if venue == "BINANCE_USDM":
            symbol, base, quote, settle = item["symbol"], item["baseAsset"], item["quoteAsset"], item["marginAsset"]
            contract_value = None
        elif venue == "BYBIT_LINEAR":
            symbol, base, quote, settle = item["symbol"], item["baseCoin"], item["quoteCoin"], item["settleCoin"]
            contract_value = None
        else:
            symbol, base, quote, settle = item["symbol"], item["base"], item["quote"], item["settleCcy"]
            contract_value = Decimal(str(item["ctVal"]))
        definitions.append(
            PerpetualInstrumentDefinition(
                instrument_id=instrument_ids[venue],
                venue=venue,
                venue_symbol=str(symbol),
                base_asset=str(base),
                quote_asset=str(quote),
                settle_asset=str(settle),
                contract_style="LINEAR",
                tick_size=Decimal(str(item["tickSize"])),
                lot_size=Decimal(str(item["lotSize"])),
                contract_value=contract_value,
                status=_canonical_status(venue, str(item[status_fields[venue]])),
                effective_from=_ms(item[listing_fields[venue]]),
                published_at=published,
                available_at=available,
                received_at=received,
                raw_sha256=str(item["parent_sha256"]),
            )
        )

    events: list[ResearchEvent] = []
    payloads = bundle["payloads"]

    def meta(
        *, venue: str, label: str, kind: EventKind, event_time: datetime, published_at: datetime,
        channel: str, sequence: str | None, symbol: str,
    ) -> MarketDataMeta:
        available, received, receipt = timing(venue, label, published_at)
        return MarketDataMeta(
            venue=venue,
            instrument_id=instrument_ids[venue],
            venue_symbol=symbol,
            kind=kind,
            event_time=event_time,
            published_at=published_at,
            available_at=available,
            received_at=received,
            source_channel=channel,
            source_sequence=sequence,
            raw_sha256=receipt.sha256,
        )

    b = payloads["BINANCE_USDM"]
    for item in b["agg_trades"]:
        t = _ms(item["T"])
        m = meta(venue="BINANCE_USDM", label="agg_trades", kind=EventKind.TRADE, event_time=t,
                 published_at=t, channel="rest/aggTrades", sequence=str(item["a"]), symbol="BTCUSDT")
        events.append(TradeEvent(m, str(item["a"]), Decimal(item["p"]), Decimal(item["q"]), "SELL" if item["m"] else "BUY"))
    item = b["depth"]
    event_t, publish_t = _ms(item["T"]), _ms(item["E"])
    m = meta(venue="BINANCE_USDM", label="depth", kind=EventKind.BOOK_SNAPSHOT, event_time=event_t,
             published_at=publish_t, channel="rest/depth", sequence=str(item["lastUpdateId"]), symbol="BTCUSDT")
    events.append(BookUpdate(m, tuple(BookLevel(Decimal(p), Decimal(q)) for p, q in item["bids"]),
                             tuple(BookLevel(Decimal(p), Decimal(q)) for p, q in item["asks"]),
                             final_sequence=int(item["lastUpdateId"])))
    item = b["premium_index"]
    t = _ms(item["time"])
    m = meta(venue="BINANCE_USDM", label="premium_index", kind=EventKind.PERPETUAL_STATE, event_time=t,
             published_at=t, channel="rest/premiumIndex", sequence=None, symbol="BTCUSDT")
    events.append(PerpetualStateEvent(m, mark_price=_decimal(item.get("markPrice")), index_price=_decimal(item.get("indexPrice")),
                                      funding_rate=_decimal(item.get("lastFundingRate")),
                                      next_funding_time=_ms(item["nextFundingTime"]) if item.get("nextFundingTime") else None))
    item = b["open_interest"]
    t = _ms(item["time"])
    m = meta(venue="BINANCE_USDM", label="open_interest", kind=EventKind.PERPETUAL_STATE, event_time=t,
             published_at=t, channel="rest/openInterest", sequence=None, symbol="BTCUSDT")
    events.append(PerpetualStateEvent(m, open_interest=_decimal(item.get("openInterest"))))

    y = payloads["BYBIT_LINEAR"]
    publish_t = _ms(y["recent_trades"]["time"])
    for item in y["recent_trades"]["result"]["list"]:
        event_t = _ms(item["time"])
        m = meta(venue="BYBIT_LINEAR", label="recent_trades", kind=EventKind.TRADE, event_time=event_t,
                 published_at=publish_t, channel="rest/recent-trade", sequence=f"{item["seq"]}:{item["execId"]}", symbol="BTCUSDT")
        events.append(TradeEvent(m, str(item["execId"]), Decimal(item["price"]), Decimal(item["size"]), str(item["side"]).upper()))
    outer = y["orderbook"]
    item = outer["result"]
    event_t = _ms(item.get("cts") or item["ts"])
    publish_t = _ms(outer["time"])
    m = meta(venue="BYBIT_LINEAR", label="orderbook", kind=EventKind.BOOK_SNAPSHOT, event_time=event_t,
             published_at=publish_t, channel="rest/orderbook", sequence=f"{item.get('u')}:{item.get('seq')}", symbol="BTCUSDT")
    events.append(BookUpdate(m, tuple(BookLevel(Decimal(p), Decimal(q)) for p, q in item["b"]),
                             tuple(BookLevel(Decimal(p), Decimal(q)) for p, q in item["a"]),
                             final_sequence=int(item["u"]) if item.get("u") is not None else None))
    outer = y["ticker"]
    item = outer["result"]["list"][0]
    t = _ms(outer["time"])
    m = meta(venue="BYBIT_LINEAR", label="ticker", kind=EventKind.PERPETUAL_STATE, event_time=t,
             published_at=t, channel="rest/tickers", sequence=None, symbol="BTCUSDT")
    events.append(PerpetualStateEvent(m, mark_price=_decimal(item.get("markPrice")), index_price=_decimal(item.get("indexPrice")),
                                      funding_rate=_decimal(item.get("fundingRate")),
                                      next_funding_time=_ms(item["nextFundingTime"]) if item.get("nextFundingTime") else None,
                                      open_interest=_decimal(item.get("openInterest")), open_interest_value=_decimal(item.get("openInterestValue"))))

    o = payloads["OKX_SWAP"]
    for item in o["trades"]["data"]:
        t = _ms(item["ts"])
        m = meta(venue="OKX_SWAP", label="trades", kind=EventKind.TRADE, event_time=t,
                 published_at=t, channel="rest/trades", sequence=str(item["tradeId"]), symbol="BTC-USDT-SWAP")
        events.append(TradeEvent(m, str(item["tradeId"]), Decimal(item["px"]), Decimal(item["sz"]), str(item["side"]).upper()))
    for item in o["books"]["data"]:
        t = _ms(item["ts"])
        m = meta(venue="OKX_SWAP", label="books", kind=EventKind.BOOK_SNAPSHOT, event_time=t,
                 published_at=t, channel="rest/books", sequence=str(item.get("seqId")), symbol="BTC-USDT-SWAP")
        events.append(BookUpdate(m, tuple(BookLevel(Decimal(row[0]), Decimal(row[1])) for row in item["bids"]),
                                 tuple(BookLevel(Decimal(row[0]), Decimal(row[1])) for row in item["asks"]),
                                 final_sequence=int(item["seqId"]) if item.get("seqId") is not None else None))
    for label, channel in (("mark_price", "rest/mark-price"), ("funding_rate", "rest/funding-rate"), ("open_interest", "rest/open-interest")):
        for item in o[label]["data"]:
            t = _ms(item.get("ts") or item.get("fundingTime"))
            m = meta(venue="OKX_SWAP", label=label, kind=EventKind.PERPETUAL_STATE, event_time=t,
                     published_at=t, channel=channel, sequence=None, symbol="BTC-USDT-SWAP")
            events.append(PerpetualStateEvent(
                m,
                mark_price=_decimal(item.get("markPx")),
                funding_rate=_decimal(item.get("fundingRate")),
                next_funding_time=_ms(item["nextFundingTime"]) if item.get("nextFundingTime") else None,
                open_interest=_decimal(item.get("oi")),
                open_interest_value=_decimal(item.get("oiUsd")),
            ))

    ordered = tuple(sorted(events, key=lambda event: (
        event.meta.available_at,
        event.meta.event_time,
        event.meta.venue,
        event.meta.instrument_id,
        event.meta.kind.value,
        "" if event.meta.source_sequence is None else event.meta.source_sequence,
        event.meta.canonical_identity(),
    )))
    population = RealMarketPopulation(
        receipts=tuple(sorted(receipts, key=lambda r: (r.venue, r.label))),
        definitions=tuple(sorted(definitions, key=lambda d: d.instrument_id)),
        events=ordered,
        clock_corrections=tuple(sorted(corrections, key=lambda c: (c.venue, c.label, c.published_at))),
        source_bundle_sha256=bundle_hash,
    )
    if not population.events:
        raise ValueError("real-market source bundle produced no events")
    if any(event.meta.raw_sha256 not in population.raw_hashes for event in population.events):
        raise ValueError("normalized event is missing raw-capture SHA lineage")
    if any(definition.raw_sha256 not in population.raw_hashes for definition in population.definitions):
        raise ValueError("instrument definition is missing raw-capture SHA lineage")
    return population

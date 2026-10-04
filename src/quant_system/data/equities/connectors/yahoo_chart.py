from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from .base import EquityInstrumentMappedConnector, EquityStreamSubscription, UnsupportedEquityMessageError
from ..models import EquityBarEvent, EquityEventKind, EquityMarketDataMeta


class YahooChartConnector(EquityInstrumentMappedConnector):
    """Credential-free read-only Yahoo chart normalizer.

    This connector deliberately treats Yahoo as a data source, not as an execution venue.
    It admits only fully closed bars and does not infer exchange tick/lot rules.
    """

    venue = "YAHOO_CHART"

    def subscriptions(self, venue_symbol: str) -> tuple[EquityStreamSubscription, ...]:
        self.instrument_id(venue_symbol)
        return ()

    @staticmethod
    def _interval_seconds(interval: str) -> int:
        mapping = {
            "1m": 60,
            "2m": 120,
            "5m": 300,
            "15m": 900,
            "30m": 1800,
            "60m": 3600,
            "90m": 5400,
            "1h": 3600,
            "1d": 86400,
        }
        try:
            return mapping[interval]
        except KeyError as exc:
            raise UnsupportedEquityMessageError(f"unsupported chart interval {interval!r}") from exc

    def normalize(
        self,
        payload: dict[str, Any],
        *,
        available_at: datetime,
        received_at: datetime,
        raw_sha256: str | None = None,
    ) -> tuple[EquityBarEvent, ...]:
        self.assert_ingestion_clock(available_at, received_at)
        if raw_sha256 is None:
            raise ValueError("raw_sha256 is required for genuine chart normalization")

        chart = payload.get("chart")
        if not isinstance(chart, dict) or chart.get("error") is not None:
            raise UnsupportedEquityMessageError("chart payload missing or contains an error")
        result = chart.get("result")
        if not isinstance(result, list) or len(result) != 1 or not isinstance(result[0], dict):
            raise UnsupportedEquityMessageError("chart payload must contain exactly one result")

        row = result[0]
        meta = row.get("meta")
        timestamps = row.get("timestamp")
        indicators = row.get("indicators")
        if not isinstance(meta, dict) or not isinstance(timestamps, list) or not isinstance(indicators, dict):
            raise UnsupportedEquityMessageError("chart result missing meta/timestamp/indicators")

        symbol = str(meta.get("symbol") or "")
        if not symbol:
            raise UnsupportedEquityMessageError("chart symbol missing")
        instrument_id = self.instrument_id(symbol)
        interval = str(meta.get("dataGranularity") or "")
        step = self._interval_seconds(interval)

        quotes = indicators.get("quote")
        if not isinstance(quotes, list) or len(quotes) != 1 or not isinstance(quotes[0], dict):
            raise UnsupportedEquityMessageError("chart quote block missing")
        q = quotes[0]
        fields = {name: q.get(name) for name in ("open", "high", "low", "close", "volume")}
        if any(not isinstance(values, list) for values in fields.values()):
            raise UnsupportedEquityMessageError("chart quote arrays missing")
        if any(len(values) != len(timestamps) for values in fields.values()):
            raise UnsupportedEquityMessageError("chart timestamp/quote array length mismatch")

        events: list[EquityBarEvent] = []
        for idx, ts in enumerate(timestamps):
            if not isinstance(ts, (int, float)):
                raise UnsupportedEquityMessageError("chart timestamp must be numeric")
            values = {name: fields[name][idx] for name in fields}
            if any(values[name] is None for name in ("open", "high", "low", "close")):
                continue
            bar_start = datetime.fromtimestamp(int(ts), tz=timezone.utc)
            bar_end = bar_start + timedelta(seconds=step)
            if bar_end > available_at:
                # Current/incomplete bar is not point-in-time safe yet.
                continue
            event_meta = EquityMarketDataMeta(
                venue=self.venue,
                instrument_id=instrument_id,
                venue_symbol=symbol,
                kind=EquityEventKind.BAR,
                event_time=bar_end,
                published_at=available_at,
                available_at=available_at,
                received_at=received_at,
                source_channel=f"chart:{interval}",
                source_sequence=str(int(ts)),
                raw_sha256=raw_sha256,
            )
            events.append(
                EquityBarEvent(
                    event_meta,
                    bar_start,
                    bar_end,
                    Decimal(str(values["open"])),
                    Decimal(str(values["high"])),
                    Decimal(str(values["low"])),
                    Decimal(str(values["close"])),
                    Decimal(str(values["volume"] or 0)),
                )
            )
        if not events:
            raise UnsupportedEquityMessageError("chart payload produced no closed usable bars")
        return tuple(events)

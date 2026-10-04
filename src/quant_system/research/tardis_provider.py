from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256
import csv
import gzip
import io
import json
from pathlib import Path
from typing import Iterable, Mapping


class TardisProviderError(RuntimeError):
    pass


VENUE_IDS = {
    "BINANCE_USDM": "binance-futures",
    "BYBIT_LINEAR": "bybit",
    "OKX_SWAP": "okex-swap",
}

REQUIRED_EXTERNAL_SERIES = {
    "BINANCE_USDM": {"LIQUIDATIONS", "INSTRUMENT_METADATA"},
    "BYBIT_LINEAR": {"LIQUIDATIONS", "INSTRUMENT_METADATA"},
    "OKX_SWAP": {"OPEN_INTEREST", "LIQUIDATIONS", "INSTRUMENT_METADATA"},
}

# These facts only describe documented source capability. They are NOT data
# admission and do not establish uninterrupted coverage or PIT completeness.
DOCUMENTED_CAPABILITY = {
    "BINANCE_USDM": {
        "exchange_available_since": "2019-11-17",
        "open_interest_available_since": "2020-05-13",
        "liquidations_documented_sample_on_or_before": "2021-09-01",
    },
    "BYBIT_LINEAR": {
        "exchange_available_since": "2019-11-07",
        "linear_available_since": "2020-05-28",
        "liquidations_available_since": "2020-12-18",
    },
    "OKX_SWAP": {
        "exchange_available_since": "2019-03-30",
        "liquidations_available_since": "2020-12-18",
    },
}


def canonical_json(payload: object) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")


def payload_sha256(payload: object) -> str:
    return sha256(canonical_json(payload)).hexdigest()


@dataclass(frozen=True, slots=True)
class ProviderReceipt:
    provider: str
    venue: str
    provider_exchange: str
    series: str
    source_url: str
    requested_date: str
    symbol: str
    raw_sha256: str
    byte_count: int
    classification: str = "REAL_MARKET"

    @property
    def receipt_sha256(self) -> str:
        return payload_sha256(
            {
                "provider": self.provider,
                "venue": self.venue,
                "provider_exchange": self.provider_exchange,
                "series": self.series,
                "source_url": self.source_url,
                "requested_date": self.requested_date,
                "symbol": self.symbol,
                "raw_sha256": self.raw_sha256,
                "byte_count": self.byte_count,
                "classification": self.classification,
            }
        )


@dataclass(frozen=True, slots=True)
class LiquidationRow:
    exchange: str
    symbol: str
    timestamp: datetime
    local_timestamp: datetime
    side: str
    price: Decimal
    amount: Decimal
    liquidation_id: str | None


@dataclass(frozen=True, slots=True)
class DerivativeTickerRow:
    exchange: str
    symbol: str
    timestamp: datetime
    local_timestamp: datetime
    open_interest: Decimal | None
    funding_rate: Decimal | None
    index_price: Decimal | None
    mark_price: Decimal | None


@dataclass(frozen=True, slots=True)
class BookSnapshot5Row:
    exchange: str
    symbol: str
    timestamp: datetime
    local_timestamp: datetime
    bids: tuple[tuple[Decimal, Decimal], ...]
    asks: tuple[tuple[Decimal, Decimal], ...]


@dataclass(frozen=True, slots=True)
class TardisCapabilityAssessment:
    frozen_start: str
    required_scope: Mapping[str, tuple[str, ...]]
    documented_supported_scope: Mapping[str, tuple[str, ...]]
    full_window_access_requires_api_key: bool
    normalized_data_can_cover_missing_market_series: bool
    historical_instrument_metadata_certifiable: bool
    metadata_blocker: str | None
    admission_ready: bool


class TardisProviderAdapter:
    BASE = "https://datasets.tardis.dev/v1"

    def __init__(self, *, frozen_start: date = date(2023, 1, 1)):
        self.frozen_start = frozen_start

    @staticmethod
    def provider_exchange(venue: str) -> str:
        try:
            return VENUE_IDS[venue]
        except KeyError as exc:
            raise TardisProviderError(f"unsupported venue: {venue}") from exc

    def dataset_url(
        self,
        *,
        venue: str,
        data_type: str,
        day: date,
        symbol: str,
    ) -> str:
        if data_type not in {"liquidations", "derivative_ticker"}:
            raise TardisProviderError(f"unsupported normalized data type: {data_type}")
        exchange = self.provider_exchange(venue)
        return (
            f"{self.BASE}/{exchange}/{data_type}/"
            f"{day:%Y/%m/%d}/{symbol}.csv.gz"
        )

    def metadata_url(self, *, venue: str, symbol: str) -> str:
        exchange = self.provider_exchange(venue)
        return f"https://api.tardis.dev/v1/instruments/{exchange}/{symbol}"

    @staticmethod
    def receipt(
        raw: bytes,
        *,
        venue: str,
        series: str,
        source_url: str,
        requested_date: date,
        symbol: str,
    ) -> ProviderReceipt:
        if not raw:
            raise TardisProviderError("empty provider response")
        return ProviderReceipt(
            provider="TARDIS.DEV",
            venue=venue,
            provider_exchange=TardisProviderAdapter.provider_exchange(venue),
            series=series,
            source_url=source_url,
            requested_date=requested_date.isoformat(),
            symbol=symbol,
            raw_sha256=sha256(raw).hexdigest(),
            byte_count=len(raw),
        )

    @staticmethod
    def _csv_bytes(raw: bytes) -> bytes:
        if raw[:2] == b"\x1f\x8b":
            try:
                return gzip.decompress(raw)
            except OSError as exc:
                raise TardisProviderError("invalid gzip payload") from exc
        return raw

    @staticmethod
    def _rows(raw: bytes) -> Iterable[dict[str, str]]:
        try:
            text = TardisProviderAdapter._csv_bytes(raw).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise TardisProviderError("provider CSV is not UTF-8") from exc
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames:
            raise TardisProviderError("provider CSV header missing")
        for row in reader:
            yield {str(k): "" if v is None else str(v) for k, v in row.items()}

    @staticmethod
    def _time_us(value: str, name: str) -> datetime:
        try:
            micros = int(value)
        except (TypeError, ValueError) as exc:
            raise TardisProviderError(f"{name} must be microsecond epoch") from exc
        return datetime.fromtimestamp(micros / 1_000_000, tz=timezone.utc)

    @staticmethod
    def _decimal(value: str, name: str, *, optional: bool = False) -> Decimal | None:
        if value == "" and optional:
            return None
        try:
            result = Decimal(value)
        except Exception as exc:
            raise TardisProviderError(f"{name} must be decimal") from exc
        if not result.is_finite():
            raise TardisProviderError(f"{name} must be finite")
        return result

    def parse_liquidations(self, raw: bytes) -> tuple[LiquidationRow, ...]:
        required = {
            "exchange",
            "symbol",
            "timestamp",
            "local_timestamp",
            "side",
            "price",
            "amount",
        }
        result: list[LiquidationRow] = []
        for row in self._rows(raw):
            missing = required - set(row)
            if missing:
                raise TardisProviderError(
                    "liquidation columns missing: " + ",".join(sorted(missing))
                )
            if row["side"] not in {"buy", "sell"}:
                raise TardisProviderError("invalid liquidation side")
            price = self._decimal(row["price"], "price")
            amount = self._decimal(row["amount"], "amount")
            assert price is not None and amount is not None
            if price <= 0 or amount <= 0:
                raise TardisProviderError("liquidation price/amount must be positive")
            result.append(
                LiquidationRow(
                    exchange=row["exchange"],
                    symbol=row["symbol"],
                    timestamp=self._time_us(row["timestamp"], "timestamp"),
                    local_timestamp=self._time_us(
                        row["local_timestamp"], "local_timestamp"
                    ),
                    side=row["side"],
                    price=price,
                    amount=amount,
                    liquidation_id=(row.get("id") or None),
                )
            )
        return tuple(result)

    def parse_derivative_ticker(
        self, raw: bytes
    ) -> tuple[DerivativeTickerRow, ...]:
        required = {
            "exchange",
            "symbol",
            "timestamp",
            "local_timestamp",
            "open_interest",
            "funding_rate",
            "index_price",
            "mark_price",
        }
        result: list[DerivativeTickerRow] = []
        for row in self._rows(raw):
            missing = required - set(row)
            if missing:
                raise TardisProviderError(
                    "derivative_ticker columns missing: " + ",".join(sorted(missing))
                )
            result.append(
                DerivativeTickerRow(
                    exchange=row["exchange"],
                    symbol=row["symbol"],
                    timestamp=self._time_us(row["timestamp"], "timestamp"),
                    local_timestamp=self._time_us(
                        row["local_timestamp"], "local_timestamp"
                    ),
                    open_interest=self._decimal(
                        row["open_interest"], "open_interest", optional=True
                    ),
                    funding_rate=self._decimal(
                        row["funding_rate"], "funding_rate", optional=True
                    ),
                    index_price=self._decimal(
                        row["index_price"], "index_price", optional=True
                    ),
                    mark_price=self._decimal(
                        row["mark_price"], "mark_price", optional=True
                    ),
                )
            )
        return tuple(result)

    def parse_book_snapshot_5(self, raw: bytes) -> tuple[BookSnapshot5Row, ...]:
        base_required = {
            "exchange",
            "symbol",
            "timestamp",
            "local_timestamp",
        }
        level_fields = {
            f"{side}[{level}].{field}"
            for side in ("bids", "asks")
            for level in range(5)
            for field in ("price", "amount")
        }
        required = base_required | level_fields
        result: list[BookSnapshot5Row] = []
        for row in self._rows(raw):
            missing = required - set(row)
            if missing:
                raise TardisProviderError(
                    "book_snapshot_5 columns missing: " + ",".join(sorted(missing))
                )
            bids: list[tuple[Decimal, Decimal]] = []
            asks: list[tuple[Decimal, Decimal]] = []
            for level in range(5):
                bp = self._decimal(row[f"bids[{level}].price"], f"bids[{level}].price")
                bq = self._decimal(row[f"bids[{level}].amount"], f"bids[{level}].amount")
                ap = self._decimal(row[f"asks[{level}].price"], f"asks[{level}].price")
                aq = self._decimal(row[f"asks[{level}].amount"], f"asks[{level}].amount")
                assert bp is not None and bq is not None and ap is not None and aq is not None
                if bp <= 0 or ap <= 0 or bq < 0 or aq < 0:
                    raise TardisProviderError("book_snapshot_5 prices must be positive and amounts non-negative")
                bids.append((bp, bq))
                asks.append((ap, aq))
            if any(bids[i][0] <= bids[i + 1][0] for i in range(4)):
                raise TardisProviderError("book_snapshot_5 bids must be strictly descending")
            if any(asks[i][0] >= asks[i + 1][0] for i in range(4)):
                raise TardisProviderError("book_snapshot_5 asks must be strictly ascending")
            if bids[0][0] >= asks[0][0]:
                raise TardisProviderError("book_snapshot_5 is crossed")
            result.append(
                BookSnapshot5Row(
                    exchange=row["exchange"],
                    symbol=row["symbol"],
                    timestamp=self._time_us(row["timestamp"], "timestamp"),
                    local_timestamp=self._time_us(row["local_timestamp"], "local_timestamp"),
                    bids=tuple(bids),
                    asks=tuple(asks),
                )
            )
        return tuple(result)

    def assess_documented_capability(self) -> TardisCapabilityAssessment:
        required = {
            venue: tuple(sorted(series))
            for venue, series in REQUIRED_EXTERNAL_SERIES.items()
        }
        # Normalized Tardis datasets explicitly support liquidations and
        # derivative_ticker (including OI). Historical instrument metadata is
        # available through the Instruments Metadata API, but Tardis documents
        # non-multiplier field changes as best-effort rather than guaranteed.
        supported = {
            "BINANCE_USDM": ("LIQUIDATIONS",),
            "BYBIT_LINEAR": ("LIQUIDATIONS",),
            "OKX_SWAP": ("LIQUIDATIONS", "OPEN_INTEREST"),
        }
        return TardisCapabilityAssessment(
            frozen_start=self.frozen_start.isoformat(),
            required_scope=required,
            documented_supported_scope=supported,
            full_window_access_requires_api_key=True,
            normalized_data_can_cover_missing_market_series=True,
            historical_instrument_metadata_certifiable=False,
            metadata_blocker=(
                "TARDIS_METADATA_CHANGES_NOT_FULLY_GUARANTEED_FOR_"
                "PRICE_AMOUNT_INCREMENT_AND_SOURCE_REVISIONS"
            ),
            admission_ready=False,
        )

    def write_capability_record(self, path: str | Path) -> dict[str, object]:
        assessment = self.assess_documented_capability()
        record: dict[str, object] = {
            "schema_id": "EQS-R13-TARDIS-CANDIDATE-CAPABILITY-V1",
            "provider": "TARDIS.DEV",
            "source_class": "EXTERNAL_PROVIDER_CANDIDATE",
            "frozen_start": assessment.frozen_start,
            "required_scope": {
                k: list(v) for k, v in assessment.required_scope.items()
            },
            "documented_supported_scope": {
                k: list(v) for k, v in assessment.documented_supported_scope.items()
            },
            "full_window_access_requires_api_key": (
                assessment.full_window_access_requires_api_key
            ),
            "normalized_data_can_cover_missing_market_series": (
                assessment.normalized_data_can_cover_missing_market_series
            ),
            "historical_instrument_metadata_certifiable": (
                assessment.historical_instrument_metadata_certifiable
            ),
            "metadata_blocker": assessment.metadata_blocker,
            "admission_ready": assessment.admission_ready,
            "broker_submission_enabled": False,
            "live_authority": False,
            "notes": [
                "Capability record is documentation/contract evidence only.",
                "No R1.3 credit is granted without downloaded raw data and immutable receipts.",
                "Full PIT lifecycle/spec/source revision history remains fail-closed.",
            ],
        }
        record["record_sha256"] = payload_sha256(record)
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return record

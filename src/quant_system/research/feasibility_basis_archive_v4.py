from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import calendar
import csv
import io
import json
from pathlib import Path
import zipfile

from quant_system.research.feasibility_archive_v2 import FeasibilityBar

ALLOWED_SERIES = {"MARK_PRICE_5M", "INDEX_PRICE_5M", "PREMIUM_INDEX_5M"}
OOS_START = datetime(2026, 4, 1, tzinfo=timezone.utc)
MAX_UNCOMPRESSED_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class BasisArchiveReceipt:
    schema_id: str
    scope_id: str
    venue: str
    instrument: str
    series: str
    month: str
    source_url: str
    archive_sha256: str
    archive_size_bytes: int
    csv_member: str
    csv_sha256: str
    csv_size_bytes: int
    row_count: int
    first_time_utc: str
    last_time_utc: str
    signed_series: bool
    locked_oos_touched: bool

    def to_record(self) -> dict[str, object]:
        return asdict(self)

    @property
    def receipt_sha256(self) -> str:
        return sha256(json.dumps(self.to_record(), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _single_csv(archive: bytes) -> tuple[str, bytes]:
    if not archive:
        raise ValueError("archive is empty")
    with zipfile.ZipFile(io.BytesIO(archive), "r") as bundle:
        members = [item for item in bundle.infolist() if not item.is_dir()]
        if len(members) != 1 or not members[0].filename.lower().endswith(".csv"):
            raise ValueError("archive must contain exactly one CSV")
        member = members[0]
        if Path(member.filename).name != member.filename:
            raise ValueError("nested ZIP member rejected")
        if not 0 < member.file_size <= MAX_UNCOMPRESSED_BYTES:
            raise ValueError("archive member outside size bound")
        payload = bundle.read(member)
        if len(payload) != member.file_size:
            raise ValueError("archive member size mismatch")
        return member.filename, payload


def _epoch(value: str) -> datetime:
    raw = int(value)
    if abs(raw) >= 10**14:
        seconds = raw / 1_000_000
    elif abs(raw) >= 10**11:
        seconds = raw / 1_000
    else:
        seconds = raw
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


def _decimal(value: str) -> Decimal:
    parsed = Decimal(value)
    if not parsed.is_finite():
        raise ValueError("non-finite decimal")
    return parsed


def parse_basis_kline_archive(archive: bytes, *, series: str, month: str) -> tuple[FeasibilityBar, ...]:
    if series not in ALLOWED_SERIES:
        raise ValueError("unsupported V4 basis series")
    year, number = map(int, month.split("-"))
    member, payload = _single_csv(archive)
    rows = list(csv.reader(io.StringIO(payload.decode("utf-8-sig"))))
    if not rows:
        raise ValueError("CSV is empty")
    if rows[0] and not rows[0][0].strip().isdigit():
        rows = rows[1:]
    signed = series == "PREMIUM_INDEX_5M"
    bars: list[FeasibilityBar] = []
    previous: datetime | None = None
    for row in rows:
        if not row or all(not cell.strip() for cell in row):
            continue
        if len(row) < 6:
            raise ValueError("kline row has fewer than six columns")
        timestamp = _epoch(row[0].strip())
        if timestamp.year != year or timestamp.month != number:
            raise ValueError("row outside expected month")
        if timestamp >= OOS_START:
            raise ValueError("LOCKED_OOS_ACCESS_FORBIDDEN")
        if previous is not None and (timestamp - previous).total_seconds() != 300:
            raise ValueError("series has duplicate, non-monotonic or non-5-minute gap")
        open_, high, low, close, volume = (_decimal(row[i].strip()) for i in range(1, 6))
        if not signed and min(open_, high, low, close) <= 0:
            raise ValueError("price series must be positive")
        if volume < 0:
            raise ValueError("volume cannot be negative")
        if high < max(open_, close) or low > min(open_, close) or high < low:
            raise ValueError("invalid OHLC envelope")
        bars.append(FeasibilityBar(timestamp, open_, high, low, close, volume))
        previous = timestamp
    days = calendar.monthrange(year, number)[1]
    expected = days * 24 * 12
    expected_first = datetime(year, number, 1, tzinfo=timezone.utc)
    if number == 12:
        next_month = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        next_month = datetime(year, number + 1, 1, tzinfo=timezone.utc)
    expected_last = next_month - timedelta(minutes=5)
    if len(bars) != expected or bars[0].open_time != expected_first or bars[-1].open_time != expected_last:
        raise ValueError("monthly 5-minute coverage incomplete")
    return tuple(bars)


def validate_basis_archive(archive: bytes, *, scope_id: str, series: str, month: str, source_url: str) -> tuple[BasisArchiveReceipt, tuple[FeasibilityBar, ...]]:
    member, csv_payload = _single_csv(archive)
    bars = parse_basis_kline_archive(archive, series=series, month=month)
    receipt = BasisArchiveReceipt(
        schema_id="EQS-FEASIBILITY-BASIS-ARCHIVE-RECEIPT-V4",
        scope_id=scope_id,
        venue="BINANCE_USDM",
        instrument="BTCUSDT",
        series=series,
        month=month,
        source_url=source_url,
        archive_sha256=sha256(archive).hexdigest(),
        archive_size_bytes=len(archive),
        csv_member=member,
        csv_sha256=sha256(csv_payload).hexdigest(),
        csv_size_bytes=len(csv_payload),
        row_count=len(bars),
        first_time_utc=bars[0].open_time.isoformat(),
        last_time_utc=bars[-1].open_time.isoformat(),
        signed_series=(series == "PREMIUM_INDEX_5M"),
        locked_oos_touched=False,
    )
    return receipt, bars

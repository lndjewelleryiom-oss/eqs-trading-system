from __future__ import annotations

import calendar
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import csv
import io
import json
from pathlib import Path
import re
import zipfile


OOS_START = datetime(2026, 4, 1, tzinfo=timezone.utc)
MAX_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
_MONTH_RE = re.compile(r"^(20\d{2})-(0[1-9]|1[0-2])$")


@dataclass(frozen=True, slots=True)
class FeasibilityBar:
    open_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


@dataclass(frozen=True, slots=True)
class FeasibilityFunding:
    funding_time: datetime
    funding_rate: Decimal


@dataclass(frozen=True, slots=True)
class ArchiveReceipt:
    schema_id: str
    campaign_id: str
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
    locked_oos_touched: bool

    def to_record(self) -> dict[str, object]:
        return asdict(self)

    @property
    def receipt_sha256(self) -> str:
        return sha256(_canonical(self.to_record())).hexdigest()


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _parse_epoch(value: str) -> datetime:
    raw = int(value)
    magnitude = abs(raw)
    if magnitude >= 10**17:
        seconds = raw / 1_000_000_000
    elif magnitude >= 10**14:
        seconds = raw / 1_000_000
    elif magnitude >= 10**11:
        seconds = raw / 1_000
    else:
        seconds = raw
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


def _assert_month(value: str) -> tuple[int, int]:
    match = _MONTH_RE.fullmatch(value)
    if not match:
        raise ValueError("month must be YYYY-MM")
    return int(match.group(1)), int(match.group(2))


def _assert_time_in_month(value: datetime, month: str) -> None:
    year, number = _assert_month(month)
    if value.year != year or value.month != number:
        raise ValueError("archive row lies outside expected month")
    if value >= OOS_START:
        raise ValueError("LOCKED_OOS_ACCESS_FORBIDDEN")


def _single_csv(archive: bytes) -> tuple[str, bytes]:
    if not archive:
        raise ValueError("archive is empty")
    with zipfile.ZipFile(io.BytesIO(archive), "r") as bundle:
        members = [item for item in bundle.infolist() if not item.is_dir()]
        csv_members = [item for item in members if item.filename.lower().endswith(".csv")]
        if len(csv_members) != 1 or len(members) != 1:
            raise ValueError("archive must contain exactly one CSV member")
        member = csv_members[0]
        if Path(member.filename).name != member.filename:
            raise ValueError("nested or unsafe ZIP member path rejected")
        if member.file_size <= 0 or member.file_size > MAX_UNCOMPRESSED_BYTES:
            raise ValueError("archive CSV size outside bounded limit")
        payload = bundle.read(member)
        if len(payload) != member.file_size:
            raise ValueError("archive member size mismatch")
        return member.filename, payload


def _decimal(value: str, name: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except Exception as exc:
        raise ValueError(f"invalid decimal {name}") from exc
    if not parsed.is_finite():
        raise ValueError(f"non-finite decimal {name}")
    return parsed


def parse_kline_archive(archive: bytes, *, month: str) -> tuple[FeasibilityBar, ...]:
    _assert_month(month)
    _, csv_payload = _single_csv(archive)
    text = csv_payload.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        raise ValueError("kline CSV is empty")
    if rows[0] and not rows[0][0].strip().lstrip("-").isdigit():
        rows = rows[1:]
    bars: list[FeasibilityBar] = []
    previous: datetime | None = None
    for row in rows:
        if not row or all(not cell.strip() for cell in row):
            continue
        if len(row) < 6:
            raise ValueError("kline row has fewer than six columns")
        timestamp = _parse_epoch(row[0].strip())
        _assert_time_in_month(timestamp, month)
        if previous is not None:
            delta = (timestamp - previous).total_seconds()
            if delta <= 0:
                raise ValueError("kline timestamps are duplicate or non-monotonic")
            if delta != 300:
                raise ValueError("kline sequence has a non-5-minute gap")
        open_ = _decimal(row[1].strip(), "open")
        high = _decimal(row[2].strip(), "high")
        low = _decimal(row[3].strip(), "low")
        close = _decimal(row[4].strip(), "close")
        volume = _decimal(row[5].strip(), "volume")
        if min(open_, high, low, close) <= 0 or volume < 0:
            raise ValueError("invalid kline prices or volume")
        if high < max(open_, close) or low > min(open_, close) or high < low:
            raise ValueError("invalid OHLC envelope")
        bars.append(FeasibilityBar(timestamp, open_, high, low, close, volume))
        previous = timestamp
    if not bars:
        raise ValueError("kline CSV has no data rows")
    return tuple(bars)


def assert_complete_kline_month(bars: tuple[FeasibilityBar, ...], *, month: str) -> None:
    year, number = _assert_month(month)
    days = calendar.monthrange(year, number)[1]
    expected_count = days * 24 * 12
    expected_first = datetime(year, number, 1, tzinfo=timezone.utc)
    if number == 12:
        next_month = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        next_month = datetime(year, number + 1, 1, tzinfo=timezone.utc)
    expected_last = next_month - timedelta(minutes=5)
    if len(bars) != expected_count:
        raise ValueError(f"kline month coverage incomplete: {len(bars)} != {expected_count}")
    if bars[0].open_time != expected_first or bars[-1].open_time != expected_last:
        raise ValueError("kline month boundary coverage incomplete")


def _funding_columns(header: list[str]) -> tuple[int, int] | None:
    normalized = [re.sub(r"[^a-z0-9]", "", value.lower()) for value in header]
    time_names = {"calctime", "fundingtime", "fundingtimestamp", "time", "timestamp"}
    rate_names = {"lastfundingrate", "fundingrate", "rate"}
    time_index = next((i for i, value in enumerate(normalized) if value in time_names), None)
    rate_index = next((i for i, value in enumerate(normalized) if value in rate_names), None)
    if time_index is None or rate_index is None:
        return None
    return time_index, rate_index


def parse_funding_archive(archive: bytes, *, month: str) -> tuple[FeasibilityFunding, ...]:
    _assert_month(month)
    _, csv_payload = _single_csv(archive)
    rows = list(csv.reader(io.StringIO(csv_payload.decode("utf-8-sig"))))
    if not rows:
        raise ValueError("funding CSV is empty")
    columns = _funding_columns(rows[0])
    if columns is not None:
        time_index, rate_index = columns
        rows = rows[1:]
    else:
        # Binance Data Vision legacy funding rows are calc_time, interval_hours, last_funding_rate.
        time_index, rate_index = 0, 2
    values: list[FeasibilityFunding] = []
    previous: datetime | None = None
    for row in rows:
        if not row or all(not cell.strip() for cell in row):
            continue
        if len(row) <= max(time_index, rate_index):
            raise ValueError("funding row missing required columns")
        timestamp = _parse_epoch(row[time_index].strip())
        _assert_time_in_month(timestamp, month)
        if previous is not None and timestamp <= previous:
            raise ValueError("funding timestamps are duplicate or non-monotonic")
        rate = _decimal(row[rate_index].strip(), "funding_rate")
        values.append(FeasibilityFunding(timestamp, rate))
        previous = timestamp
    if not values:
        raise ValueError("funding CSV has no data rows")
    return tuple(values)


def validate_archive(
    archive: bytes,
    *,
    campaign_id: str,
    series: str,
    month: str,
    source_url: str,
) -> tuple[ArchiveReceipt, tuple[FeasibilityBar | FeasibilityFunding, ...]]:
    member, csv_payload = _single_csv(archive)
    if series == "KLINES_5M":
        bar_rows = parse_kline_archive(archive, month=month)
        assert_complete_kline_month(bar_rows, month=month)
        rows: tuple[FeasibilityBar | FeasibilityFunding, ...] = bar_rows
        times = [item.open_time for item in bar_rows]
    elif series == "FUNDING_RATE":
        rows = parse_funding_archive(archive, month=month)
        times = [item.funding_time for item in rows if isinstance(item, FeasibilityFunding)]
    else:
        raise ValueError("unsupported feasibility series")
    if not times:
        raise ValueError("archive produced no timestamps")
    receipt = ArchiveReceipt(
        schema_id="EQS-FEASIBILITY-ARCHIVE-RECEIPT-V2",
        campaign_id=campaign_id,
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
        row_count=len(rows),
        first_time_utc=min(times).isoformat(),
        last_time_utc=max(times).isoformat(),
        locked_oos_touched=False,
    )
    return receipt, rows


def write_immutable_receipt(path: str | Path, receipt: ArchiveReceipt) -> str:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    record = {**receipt.to_record(), "receipt_sha256": receipt.receipt_sha256}
    encoded = json.dumps(record, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    try:
        with destination.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
    except FileExistsError:
        if destination.read_bytes() != encoded:
            raise RuntimeError("archive receipt is immutable and existing content differs")
    return receipt.receipt_sha256

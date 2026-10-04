from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time
from hashlib import sha256
import json
from zoneinfo import ZoneInfo

from .calendars import TradingCalendar


@dataclass(frozen=True, slots=True)
class CalendarSourceRecord:
    calendar_id: str
    timezone_name: str
    open_time: str
    close_time: str
    weekdays: tuple[int, ...]
    holidays: tuple[str, ...]
    source: str
    version: str

    def fingerprint(self) -> str:
        payload = json.dumps(
            {
                "calendar_id": self.calendar_id,
                "timezone_name": self.timezone_name,
                "open_time": self.open_time,
                "close_time": self.close_time,
                "weekdays": self.weekdays,
                "holidays": self.holidays,
                "source": self.source,
                "version": self.version,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return sha256(payload.encode()).hexdigest()

    def build(self) -> TradingCalendar:
        ZoneInfo(self.timezone_name)  # validate timezone eagerly
        weekdays = frozenset(self.weekdays)
        if not weekdays or any(day < 0 or day > 6 for day in weekdays):
            raise ValueError("weekdays must be integers in [0, 6]")
        holiday_dates = tuple(date.fromisoformat(value) for value in self.holidays)
        if len(set(holiday_dates)) != len(holiday_dates):
            raise ValueError("duplicate holidays are not allowed")
        open_time = time.fromisoformat(self.open_time)
        close_time = time.fromisoformat(self.close_time)
        if open_time >= close_time:
            raise ValueError("open_time must be before close_time")
        return TradingCalendar(
            calendar_id=self.calendar_id,
            timezone_name=self.timezone_name,
            open_time=open_time,
            close_time=close_time,
            weekdays=weekdays,
            holidays=frozenset(holiday_dates),
        )


def load_calendar_record(payload: bytes) -> CalendarSourceRecord:
    raw = json.loads(payload.decode("utf-8"))
    required = {"calendar_id", "timezone_name", "open_time", "close_time", "weekdays", "holidays", "source", "version"}
    missing = required - raw.keys()
    if missing:
        raise ValueError(f"missing calendar fields: {sorted(missing)}")
    return CalendarSourceRecord(
        calendar_id=str(raw["calendar_id"]),
        timezone_name=str(raw["timezone_name"]),
        open_time=str(raw["open_time"]),
        close_time=str(raw["close_time"]),
        weekdays=tuple(int(value) for value in raw["weekdays"]),
        holidays=tuple(str(value) for value in raw["holidays"]),
        source=str(raw["source"]),
        version=str(raw["version"]),
    )

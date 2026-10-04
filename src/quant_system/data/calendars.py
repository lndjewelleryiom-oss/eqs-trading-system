from dataclasses import dataclass
from datetime import date, datetime, time
from zoneinfo import ZoneInfo


@dataclass(frozen=True, slots=True)
class TradingCalendar:
    calendar_id: str
    timezone_name: str
    open_time: time
    close_time: time
    weekdays: frozenset[int]
    holidays: frozenset[date] = frozenset()

    def is_open(self, ts: datetime) -> bool:
        if ts.tzinfo is None or ts.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        local = ts.astimezone(ZoneInfo(self.timezone_name))
        if local.weekday() not in self.weekdays or local.date() in self.holidays:
            return False
        t = local.timetz().replace(tzinfo=None)
        return self.open_time <= t < self.close_time


CONTINUOUS_24_7 = TradingCalendar(
    calendar_id="24_7_UTC",
    timezone_name="UTC",
    open_time=time(0, 0),
    close_time=time(23, 59, 59, 999999),
    weekdays=frozenset(range(7)),
)

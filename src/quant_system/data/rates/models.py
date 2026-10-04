from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from hashlib import sha256
import json
from typing import Mapping


def _aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class TreasuryCurveSnapshot:
    as_of_date: date
    known_from: datetime
    curve_bps: Mapping[str, Decimal]
    source: str
    raw_sha256: str

    def __post_init__(self) -> None:
        _aware(self.known_from, "known_from")
        if not self.source:
            raise ValueError("source is required")
        if len(self.raw_sha256) != 64:
            raise ValueError("raw_sha256 must be SHA-256 hex")
        try:
            int(self.raw_sha256, 16)
        except ValueError as exc:
            raise ValueError("raw_sha256 must be SHA-256 hex") from exc
        if not self.curve_bps:
            raise ValueError("curve_bps cannot be empty")
        for tenor, value in self.curve_bps.items():
            if not tenor:
                raise ValueError("curve tenor cannot be empty")
            if value < Decimal("-500") or value > Decimal("2500"):
                raise ValueError("curve yield is outside supported basis-point bounds")

    def assert_usable_at(self, decision_time: datetime) -> None:
        _aware(decision_time, "decision_time")
        if self.known_from > decision_time:
            raise ValueError("Treasury curve snapshot was not known at decision time")

    def canonical_identity(self) -> str:
        payload={
            "as_of_date":self.as_of_date.isoformat(),
            "known_from":self.known_from.isoformat(),
            "curve_bps":{k:str(v) for k,v in sorted(self.curve_bps.items())},
            "source":self.source,
            "raw_sha256":self.raw_sha256,
        }
        return sha256(json.dumps(payload,sort_keys=True,separators=(",",":")).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class KeyRateDv01Exposure:
    instrument_id: str
    currency: str
    key_rate_dv01: Mapping[str, Decimal]
    duration: Decimal
    dv01: Decimal
    pv01: Decimal
    convexity: Decimal

    def __post_init__(self) -> None:
        if not self.instrument_id or not self.currency:
            raise ValueError("instrument_id and currency are required")
        if not self.key_rate_dv01:
            raise ValueError("key_rate_dv01 cannot be empty")
        if any(not tenor for tenor in self.key_rate_dv01):
            raise ValueError("key-rate tenor cannot be empty")

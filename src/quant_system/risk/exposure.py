from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

@dataclass(frozen=True, slots=True)
class ExposureLine:
    strategy_id: str
    venue: str
    instrument: str
    signed_notional: Decimal

@dataclass(frozen=True, slots=True)
class ExposureSnapshot:
    gross: Decimal
    net: Decimal
    by_strategy: dict[str, Decimal]
    by_instrument: dict[str, Decimal]
    by_venue: dict[str, Decimal]
    reserved: Decimal
    open_order: Decimal

    @property
    def committed_gross(self) -> Decimal:
        return self.gross + self.reserved + self.open_order

class ExposureAccountant:
    @staticmethod
    def compute(lines: Iterable[ExposureLine], *, reserved: Decimal,
                open_order: Decimal) -> ExposureSnapshot:
        gross = Decimal("0"); net = Decimal("0")
        by_strategy = {}; by_instrument = {}; by_venue = {}
        for line in lines:
            gross += abs(line.signed_notional); net += line.signed_notional
            by_strategy[line.strategy_id] = by_strategy.get(line.strategy_id, Decimal("0")) + abs(line.signed_notional)
            by_instrument[line.instrument] = by_instrument.get(line.instrument, Decimal("0")) + line.signed_notional
            by_venue[line.venue] = by_venue.get(line.venue, Decimal("0")) + line.signed_notional
        return ExposureSnapshot(gross, net, by_strategy, by_instrument, by_venue, reserved, open_order)

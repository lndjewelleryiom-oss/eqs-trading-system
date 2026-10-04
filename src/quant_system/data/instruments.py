from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class Instrument:
    instrument_id: str
    symbol: str
    asset_class: str
    venue: str
    quote_currency: str
    tick_size: Decimal
    lot_size: Decimal
    active: bool = True

    def __post_init__(self) -> None:
        if self.tick_size <= 0 or self.lot_size <= 0:
            raise ValueError("tick_size and lot_size must be positive")


class InstrumentMaster:
    def __init__(self, instruments: tuple[Instrument, ...] = ()):
        self._by_id: dict[str, Instrument] = {}
        self._by_venue_symbol: dict[tuple[str, str], Instrument] = {}
        for instrument in instruments:
            self.add(instrument)

    def add(self, instrument: Instrument) -> None:
        venue_key = (instrument.venue, instrument.symbol)
        if instrument.instrument_id in self._by_id:
            raise ValueError("duplicate instrument_id")
        if venue_key in self._by_venue_symbol:
            raise ValueError("duplicate venue/symbol")
        self._by_id[instrument.instrument_id] = instrument
        self._by_venue_symbol[venue_key] = instrument

    def get(self, instrument_id: str) -> Instrument:
        return self._by_id[instrument_id]

    def resolve(self, venue: str, symbol: str) -> Instrument:
        return self._by_venue_symbol[(venue, symbol)]

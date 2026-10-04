from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json

from quant_system.data.raw_store import ImmutableRawStore, StoredBlob

from .connectors.base import EquityMarketConnector, NormalizedEquityEvent


@dataclass(frozen=True, slots=True)
class EquityCollectionReceipt:
    raw_blob: StoredBlob
    events: tuple[NormalizedEquityEvent, ...]


class ReadOnlyEquityMarketCollector:
    """Raw-first, read-only Stocks/ETFs market-data collector boundary."""

    def __init__(self, connector: EquityMarketConnector, raw_store: ImmutableRawStore):
        self.connector = connector
        self.raw_store = raw_store

    def collect(
        self,
        raw_payload: bytes,
        *,
        available_at: datetime,
        received_at: datetime,
    ) -> EquityCollectionReceipt:
        blob = self.raw_store.put(raw_payload)
        decoded = json.loads(raw_payload)
        if not isinstance(decoded, dict):
            raise TypeError("equity market-data payload root must be a JSON object")
        events = self.connector.normalize(
            decoded,
            available_at=available_at,
            received_at=received_at,
            raw_sha256=blob.content_hash,
        )
        if any(event.meta.raw_sha256 != blob.content_hash for event in events):
            raise RuntimeError("normalized equity event lost immutable raw-payload lineage")
        return EquityCollectionReceipt(blob, events)

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json

from quant_system.data.raw_store import ImmutableRawStore, StoredBlob

from .connectors.base import CryptoPerpetualConnector, NormalizedEvent


@dataclass(frozen=True, slots=True)
class CollectionReceipt:
    raw_blob: StoredBlob
    events: tuple[NormalizedEvent, ...]


class ReadOnlyMarketCollector:
    """Raw-first market-data collector boundary.

    The socket/network layer must supply `available_at` at byte receipt. Raw bytes are
    durably content-addressed *before* JSON decoding or normalization, preserving malformed
    messages for incident analysis. This class contains no authenticated/trading methods.
    """

    def __init__(self, connector: CryptoPerpetualConnector, raw_store: ImmutableRawStore):
        self.connector = connector
        self.raw_store = raw_store

    def collect(
        self,
        raw_payload: bytes,
        *,
        available_at: datetime,
        received_at: datetime,
    ) -> CollectionReceipt:
        blob = self.raw_store.put(raw_payload)
        decoded = json.loads(raw_payload)
        if not isinstance(decoded, dict):
            raise TypeError("market-data payload root must be a JSON object")
        events = self.connector.normalize(
            decoded,
            available_at=available_at,
            received_at=received_at,
            raw_sha256=blob.content_hash,
        )
        if any(event.meta.raw_sha256 != blob.content_hash for event in events):
            raise RuntimeError("normalized event lost immutable raw-payload lineage")
        return CollectionReceipt(blob, events)

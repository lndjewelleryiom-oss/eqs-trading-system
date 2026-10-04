from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
import os
from pathlib import Path
from typing import Iterable, Iterator, Sequence
from urllib.parse import quote

from .models import (
    BookLevel,
    BookUpdate,
    EventKind,
    LiquidatedSide,
    LiquidationEvent,
    MarketDataMeta,
    PerpetualInstrumentDefinition,
    PerpetualStateEvent,
    TradeEvent,
)

ResearchEvent = TradeEvent | BookUpdate | PerpetualStateEvent | LiquidationEvent

_ACTIVE_STATUSES = frozenset({"TRADING", "ACTIVE", "OPEN"})
_DATASET_FORMAT_VERSION = "crypto-perps-research-v1"


def _aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _canonical_json(payload: object) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _write_immutable(path: Path, payload: bytes, *, label: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        if path.read_bytes() != payload:
            raise RuntimeError(f"{label} is immutable and existing content differs")


def _meta_to_record(meta: MarketDataMeta) -> dict[str, object]:
    return {
        "venue": meta.venue,
        "instrument_id": meta.instrument_id,
        "venue_symbol": meta.venue_symbol,
        "kind": meta.kind.value,
        "event_time": meta.event_time.isoformat(),
        "published_at": meta.published_at.isoformat(),
        "available_at": meta.available_at.isoformat(),
        "received_at": meta.received_at.isoformat(),
        "source_channel": meta.source_channel,
        "source_sequence": meta.source_sequence,
        "raw_sha256": meta.raw_sha256,
        "schema_version": meta.schema_version,
    }


def _meta_from_record(record: dict[str, object]) -> MarketDataMeta:
    return MarketDataMeta(
        venue=str(record["venue"]),
        instrument_id=str(record["instrument_id"]),
        venue_symbol=str(record["venue_symbol"]),
        kind=EventKind(str(record["kind"])),
        event_time=datetime.fromisoformat(str(record["event_time"])),
        published_at=datetime.fromisoformat(str(record["published_at"])),
        available_at=datetime.fromisoformat(str(record["available_at"])),
        received_at=datetime.fromisoformat(str(record["received_at"])),
        source_channel=str(record["source_channel"]),
        source_sequence=None if record.get("source_sequence") is None else str(record["source_sequence"]),
        raw_sha256=str(record["raw_sha256"]),
        schema_version=str(record["schema_version"]),
    )


def event_to_record(event: ResearchEvent) -> dict[str, object]:
    base: dict[str, object] = {"meta": _meta_to_record(event.meta)}
    if isinstance(event, TradeEvent):
        return {
            **base,
            "record_type": "TRADE",
            "trade_id": event.trade_id,
            "price": str(event.price),
            "quantity": str(event.quantity),
            "aggressor_side": event.aggressor_side,
        }
    if isinstance(event, BookUpdate):
        return {
            **base,
            "record_type": "BOOK",
            "bids": [[str(level.price), str(level.quantity)] for level in event.bids],
            "asks": [[str(level.price), str(level.quantity)] for level in event.asks],
            "first_sequence": event.first_sequence,
            "final_sequence": event.final_sequence,
            "previous_sequence": event.previous_sequence,
            "checksum": event.checksum,
        }
    if isinstance(event, PerpetualStateEvent):
        return {
            **base,
            "record_type": "PERPETUAL_STATE",
            "mark_price": None if event.mark_price is None else str(event.mark_price),
            "index_price": None if event.index_price is None else str(event.index_price),
            "funding_rate": None if event.funding_rate is None else str(event.funding_rate),
            "next_funding_time": None if event.next_funding_time is None else event.next_funding_time.isoformat(),
            "open_interest": None if event.open_interest is None else str(event.open_interest),
            "open_interest_value": None if event.open_interest_value is None else str(event.open_interest_value),
        }
    if isinstance(event, LiquidationEvent):
        return {
            **base,
            "record_type": "LIQUIDATION",
            "liquidation_id": event.liquidation_id,
            "liquidated_side": event.liquidated_side.value,
            "price": str(event.price),
            "quantity": str(event.quantity),
        }
    raise TypeError(f"unsupported research event: {type(event)!r}")


def event_from_record(record: dict[str, object]) -> ResearchEvent:
    meta = _meta_from_record(dict(record["meta"]))
    kind = str(record["record_type"])
    if kind == "TRADE":
        return TradeEvent(
            meta=meta,
            trade_id=str(record["trade_id"]),
            price=Decimal(str(record["price"])),
            quantity=Decimal(str(record["quantity"])),
            aggressor_side=None if record.get("aggressor_side") is None else str(record["aggressor_side"]),
        )
    if kind == "BOOK":
        return BookUpdate(
            meta=meta,
            bids=tuple(BookLevel(Decimal(str(p)), Decimal(str(q))) for p, q in record.get("bids", [])),
            asks=tuple(BookLevel(Decimal(str(p)), Decimal(str(q))) for p, q in record.get("asks", [])),
            first_sequence=None if record.get("first_sequence") is None else int(record["first_sequence"]),
            final_sequence=None if record.get("final_sequence") is None else int(record["final_sequence"]),
            previous_sequence=None if record.get("previous_sequence") is None else int(record["previous_sequence"]),
            checksum=None if record.get("checksum") is None else int(record["checksum"]),
        )
    if kind == "PERPETUAL_STATE":
        def decimal_or_none(name: str) -> Decimal | None:
            value = record.get(name)
            return None if value is None else Decimal(str(value))

        next_funding = record.get("next_funding_time")
        return PerpetualStateEvent(
            meta=meta,
            mark_price=decimal_or_none("mark_price"),
            index_price=decimal_or_none("index_price"),
            funding_rate=decimal_or_none("funding_rate"),
            next_funding_time=None if next_funding is None else datetime.fromisoformat(str(next_funding)),
            open_interest=decimal_or_none("open_interest"),
            open_interest_value=decimal_or_none("open_interest_value"),
        )
    if kind == "LIQUIDATION":
        return LiquidationEvent(
            meta=meta,
            liquidation_id=None if record.get("liquidation_id") is None else str(record["liquidation_id"]),
            liquidated_side=LiquidatedSide(str(record["liquidated_side"])),
            price=Decimal(str(record["price"])),
            quantity=Decimal(str(record["quantity"])),
        )
    raise ValueError(f"unsupported research record_type: {kind}")


def instrument_to_record(definition: PerpetualInstrumentDefinition) -> dict[str, object]:
    return {
        "instrument_id": definition.instrument_id,
        "venue": definition.venue,
        "venue_symbol": definition.venue_symbol,
        "base_asset": definition.base_asset,
        "quote_asset": definition.quote_asset,
        "settle_asset": definition.settle_asset,
        "contract_style": definition.contract_style,
        "tick_size": str(definition.tick_size),
        "lot_size": str(definition.lot_size),
        "contract_value": None if definition.contract_value is None else str(definition.contract_value),
        "status": definition.status,
        "effective_from": definition.effective_from.isoformat(),
        "published_at": definition.published_at.isoformat(),
        "available_at": definition.available_at.isoformat(),
        "received_at": definition.received_at.isoformat(),
        "raw_sha256": definition.raw_sha256,
        "schema_version": definition.schema_version,
    }


def instrument_from_record(record: dict[str, object]) -> PerpetualInstrumentDefinition:
    contract_value = record.get("contract_value")
    return PerpetualInstrumentDefinition(
        instrument_id=str(record["instrument_id"]),
        venue=str(record["venue"]),
        venue_symbol=str(record["venue_symbol"]),
        base_asset=str(record["base_asset"]),
        quote_asset=str(record["quote_asset"]),
        settle_asset=str(record["settle_asset"]),
        contract_style=str(record["contract_style"]),
        tick_size=Decimal(str(record["tick_size"])),
        lot_size=Decimal(str(record["lot_size"])),
        contract_value=None if contract_value is None else Decimal(str(contract_value)),
        status=str(record["status"]),
        effective_from=datetime.fromisoformat(str(record["effective_from"])),
        published_at=datetime.fromisoformat(str(record["published_at"])),
        available_at=datetime.fromisoformat(str(record["available_at"])),
        received_at=datetime.fromisoformat(str(record["received_at"])),
        raw_sha256=str(record["raw_sha256"]),
        schema_version=str(record["schema_version"]),
    )


def event_sort_key(event: ResearchEvent) -> tuple[object, ...]:
    meta = event.meta
    sequence = "" if meta.source_sequence is None else meta.source_sequence
    return (
        meta.available_at,
        meta.event_time,
        meta.venue,
        meta.instrument_id,
        meta.kind.value,
        sequence,
        meta.canonical_identity(),
    )


@dataclass(frozen=True, slots=True, order=True)
class PartitionKey:
    dataset_id: str
    venue: str
    instrument_id: str
    event_date: date
    kind: EventKind


@dataclass(frozen=True, slots=True)
class PartitionDescriptor:
    key: PartitionKey
    schema_version: str
    row_count: int
    content_hash: str
    relative_path: str
    min_event_time: datetime
    max_event_time: datetime
    min_available_at: datetime
    max_available_at: datetime

    def to_record(self) -> dict[str, object]:
        return {
            "dataset_id": self.key.dataset_id,
            "venue": self.key.venue,
            "instrument_id": self.key.instrument_id,
            "event_date": self.key.event_date.isoformat(),
            "kind": self.key.kind.value,
            "schema_version": self.schema_version,
            "row_count": self.row_count,
            "content_hash": self.content_hash,
            "relative_path": self.relative_path,
            "min_event_time": self.min_event_time.isoformat(),
            "max_event_time": self.max_event_time.isoformat(),
            "min_available_at": self.min_available_at.isoformat(),
            "max_available_at": self.max_available_at.isoformat(),
        }

    @staticmethod
    def from_record(record: dict[str, object]) -> "PartitionDescriptor":
        return PartitionDescriptor(
            key=PartitionKey(
                dataset_id=str(record["dataset_id"]),
                venue=str(record["venue"]),
                instrument_id=str(record["instrument_id"]),
                event_date=date.fromisoformat(str(record["event_date"])),
                kind=EventKind(str(record["kind"])),
            ),
            schema_version=str(record["schema_version"]),
            row_count=int(record["row_count"]),
            content_hash=str(record["content_hash"]),
            relative_path=str(record["relative_path"]),
            min_event_time=datetime.fromisoformat(str(record["min_event_time"])),
            max_event_time=datetime.fromisoformat(str(record["max_event_time"])),
            min_available_at=datetime.fromisoformat(str(record["min_available_at"])),
            max_available_at=datetime.fromisoformat(str(record["max_available_at"])),
        )


class PartitionIntegrityError(RuntimeError):
    pass


class HistoricalPartitionStore:
    """Immutable, content-addressed JSONL partition storage for normalized research events."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def partition_key(dataset_id: str, event: ResearchEvent) -> PartitionKey:
        meta = event.meta
        return PartitionKey(dataset_id, meta.venue, meta.instrument_id, meta.event_time.astimezone(timezone.utc).date(), meta.kind)

    def _relative_path(self, key: PartitionKey, content_hash: str) -> Path:
        return Path(
            f"dataset={quote(key.dataset_id, safe='')}"
        ) / f"venue={quote(key.venue, safe='')}" / f"instrument={quote(key.instrument_id, safe='')}" / (
            f"date={key.event_date.isoformat()}"
        ) / f"kind={key.kind.value}" / f"part-{content_hash}.jsonl"

    def write_partition(self, dataset_id: str, events: Sequence[ResearchEvent]) -> PartitionDescriptor:
        if not events:
            raise ValueError("partition cannot be empty")
        ordered = tuple(sorted(events, key=event_sort_key))
        key = self.partition_key(dataset_id, ordered[0])
        if any(self.partition_key(dataset_id, event) != key for event in ordered):
            raise ValueError("all events in a partition must share dataset/venue/instrument/date/kind")
        schema_versions = {event.meta.schema_version for event in ordered}
        if len(schema_versions) != 1:
            raise ValueError("partition cannot mix schema versions")
        identities = [event.meta.canonical_identity() for event in ordered]
        if len(identities) != len(set(identities)):
            raise ValueError("partition cannot contain duplicate canonical event identities")
        payload = b"".join(_canonical_json(event_to_record(event)) + b"\n" for event in ordered)
        digest = sha256(payload).hexdigest()
        relative = self._relative_path(key, digest)
        path = self.root / relative
        try:
            _write_immutable(path, payload, label="partition")
        except RuntimeError as exc:
            raise PartitionIntegrityError("partition content-address collision or tampering") from exc
        if sha256(path.read_bytes()).hexdigest() != digest:
            raise PartitionIntegrityError("partition content-address collision or tampering")
        return PartitionDescriptor(
            key=key,
            schema_version=next(iter(schema_versions)),
            row_count=len(ordered),
            content_hash=digest,
            relative_path=relative.as_posix(),
            min_event_time=min(event.meta.event_time for event in ordered),
            max_event_time=max(event.meta.event_time for event in ordered),
            min_available_at=min(event.meta.available_at for event in ordered),
            max_available_at=max(event.meta.available_at for event in ordered),
        )

    def write_events(self, dataset_id: str, events: Iterable[ResearchEvent]) -> tuple[PartitionDescriptor, ...]:
        groups: dict[PartitionKey, list[ResearchEvent]] = {}
        for event in events:
            groups.setdefault(self.partition_key(dataset_id, event), []).append(event)
        descriptors = [self.write_partition(dataset_id, groups[key]) for key in sorted(groups)]
        return tuple(sorted(descriptors, key=lambda descriptor: descriptor.relative_path))

    def read_partition(self, descriptor: PartitionDescriptor) -> tuple[ResearchEvent, ...]:
        path = self.root / descriptor.relative_path
        payload = path.read_bytes()
        if sha256(payload).hexdigest() != descriptor.content_hash:
            raise PartitionIntegrityError("partition hash verification failed")
        events = tuple(event_from_record(json.loads(line)) for line in payload.splitlines() if line)
        if len(events) != descriptor.row_count:
            raise PartitionIntegrityError("partition row count mismatch")
        if events and any(self.partition_key(descriptor.key.dataset_id, event) != descriptor.key for event in events):
            raise PartitionIntegrityError("partition key mismatch")
        if events and any(event.meta.schema_version != descriptor.schema_version for event in events):
            raise PartitionIntegrityError("partition schema version mismatch")
        if tuple(sorted(events, key=event_sort_key)) != events:
            raise PartitionIntegrityError("partition row ordering is not canonical")
        if events:
            observed_bounds = (
                min(event.meta.event_time for event in events),
                max(event.meta.event_time for event in events),
                min(event.meta.available_at for event in events),
                max(event.meta.available_at for event in events),
            )
            expected_bounds = (
                descriptor.min_event_time,
                descriptor.max_event_time,
                descriptor.min_available_at,
                descriptor.max_available_at,
            )
            if observed_bounds != expected_bounds:
                raise PartitionIntegrityError("partition timestamp bounds mismatch")
        return events


class InstrumentUniverseHistory:
    """Point-in-time instrument membership history using only definitions available at each decision time."""

    def __init__(
        self,
        definitions: Iterable[PerpetualInstrumentDefinition],
        active_statuses: Iterable[str] = _ACTIVE_STATUSES,
    ):
        self.active_statuses = frozenset(status.upper() for status in active_statuses)
        self._definitions = tuple(sorted(
            definitions,
            key=lambda d: (d.instrument_id, d.available_at, d.effective_from, d.received_at, d.raw_sha256),
        ))
        seen: set[tuple[str, datetime, datetime, str]] = set()
        for definition in self._definitions:
            identity = (
                definition.instrument_id,
                definition.available_at,
                definition.effective_from,
                definition.raw_sha256,
            )
            if identity in seen:
                raise ValueError("duplicate instrument definition")
            seen.add(identity)

    @property
    def definitions(self) -> tuple[PerpetualInstrumentDefinition, ...]:
        return self._definitions

    def definition_as_of(self, instrument_id: str, decision_time: datetime) -> PerpetualInstrumentDefinition | None:
        _aware(decision_time, "decision_time")
        candidates = [
            definition
            for definition in self._definitions
            if definition.instrument_id == instrument_id
            and definition.available_at <= decision_time
            and definition.effective_from <= decision_time
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda d: (d.available_at, d.effective_from, d.received_at, d.raw_sha256))

    def active_instruments_as_of(self, decision_time: datetime) -> tuple[str, ...]:
        ids = {definition.instrument_id for definition in self._definitions}
        active = []
        for instrument_id in sorted(ids):
            definition = self.definition_as_of(instrument_id, decision_time)
            if definition is not None and definition.status.upper() in self.active_statuses:
                active.append(instrument_id)
        return tuple(active)

    def event_was_in_active_universe(self, event: ResearchEvent) -> bool:
        definition = self.definition_as_of(event.meta.instrument_id, event.meta.available_at)
        return (
            definition is not None
            and definition.effective_from <= event.meta.event_time
            and definition.status.upper() in self.active_statuses
        )

    def fingerprint(self) -> str:
        payload = {
            "active_statuses": sorted(self.active_statuses),
            "definitions": [instrument_to_record(definition) for definition in self._definitions],
        }
        return sha256(_canonical_json(payload)).hexdigest()

    def write(self, path: str | Path) -> str:
        payload = {
            "format_version": _DATASET_FORMAT_VERSION,
            "active_statuses": sorted(self.active_statuses),
            "definitions": [instrument_to_record(definition) for definition in self._definitions],
            "fingerprint": self.fingerprint(),
        }
        encoded = _canonical_json(payload)
        destination = Path(path)
        _write_immutable(destination, encoded, label="universe history path")
        return payload["fingerprint"]

    @staticmethod
    def read(path: str | Path) -> "InstrumentUniverseHistory":
        payload = json.loads(Path(path).read_bytes())
        history = InstrumentUniverseHistory(
            (instrument_from_record(item) for item in payload["definitions"]),
            active_statuses=payload["active_statuses"],
        )
        if payload.get("fingerprint") != history.fingerprint():
            raise RuntimeError("universe history integrity verification failed")
        return history


@dataclass(frozen=True, slots=True)
class ResearchDatasetManifest:
    dataset_id: str
    format_version: str
    decision_time: datetime
    start_time: datetime | None
    partition_descriptors: tuple[PartitionDescriptor, ...]
    universe_fingerprint: str
    event_count: int
    event_identity_hash: str

    def __post_init__(self) -> None:
        _aware(self.decision_time, "decision_time")
        if self.start_time is not None:
            _aware(self.start_time, "start_time")
            if self.start_time > self.decision_time:
                raise ValueError("start_time cannot be after decision_time")
        if self.event_count < 0:
            raise ValueError("event_count cannot be negative")

    def deterministic_payload(self) -> dict[str, object]:
        return {
            "dataset_id": self.dataset_id,
            "format_version": self.format_version,
            "decision_time": self.decision_time.isoformat(),
            "start_time": None if self.start_time is None else self.start_time.isoformat(),
            "partitions": [descriptor.to_record() for descriptor in sorted(
                self.partition_descriptors, key=lambda item: item.relative_path
            )],
            "universe_fingerprint": self.universe_fingerprint,
            "event_count": self.event_count,
            "event_identity_hash": self.event_identity_hash,
        }

    def fingerprint(self) -> str:
        return sha256(_canonical_json(self.deterministic_payload())).hexdigest()

    def to_record(self) -> dict[str, object]:
        payload = self.deterministic_payload()
        payload["manifest_fingerprint"] = self.fingerprint()
        return payload

    @staticmethod
    def from_record(record: dict[str, object]) -> "ResearchDatasetManifest":
        manifest = ResearchDatasetManifest(
            dataset_id=str(record["dataset_id"]),
            format_version=str(record["format_version"]),
            decision_time=datetime.fromisoformat(str(record["decision_time"])),
            start_time=None if record.get("start_time") is None else datetime.fromisoformat(str(record["start_time"])),
            partition_descriptors=tuple(
                PartitionDescriptor.from_record(dict(item)) for item in record.get("partitions", [])
            ),
            universe_fingerprint=str(record["universe_fingerprint"]),
            event_count=int(record["event_count"]),
            event_identity_hash=str(record["event_identity_hash"]),
        )
        expected = record.get("manifest_fingerprint")
        if expected is not None and str(expected) != manifest.fingerprint():
            raise RuntimeError("dataset manifest integrity verification failed")
        return manifest


class ResearchManifestCatalog:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, manifest: ResearchDatasetManifest) -> str:
        fingerprint = manifest.fingerprint()
        path = self.root / f"{fingerprint}.json"
        encoded = _canonical_json(manifest.to_record())
        _write_immutable(path, encoded, label="manifest fingerprint")
        return fingerprint

    def get(self, fingerprint: str) -> ResearchDatasetManifest:
        path = self.root / f"{fingerprint}.json"
        manifest = ResearchDatasetManifest.from_record(json.loads(path.read_bytes()))
        if manifest.fingerprint() != fingerprint:
            raise RuntimeError("dataset manifest filename/content mismatch")
        return manifest


@dataclass(frozen=True, slots=True)
class AssembledResearchDataset:
    events: tuple[ResearchEvent, ...]
    manifest: ResearchDatasetManifest


class PointInTimeDatasetAssembler:
    def __init__(self, store: HistoricalPartitionStore, universe: InstrumentUniverseHistory):
        self.store = store
        self.universe = universe

    def assemble(
        self,
        *,
        dataset_id: str,
        partitions: Iterable[PartitionDescriptor],
        decision_time: datetime,
        start_time: datetime | None = None,
    ) -> AssembledResearchDataset:
        _aware(decision_time, "decision_time")
        if start_time is not None:
            _aware(start_time, "start_time")
            if start_time > decision_time:
                raise ValueError("start_time cannot be after decision_time")
        selected = tuple(sorted(
            (p for p in partitions if p.key.dataset_id == dataset_id and p.min_available_at <= decision_time),
            key=lambda p: p.relative_path,
        ))
        by_identity: dict[str, ResearchEvent] = {}
        for descriptor in selected:
            for event in self.store.read_partition(descriptor):
                if event.meta.available_at > decision_time:
                    continue
                if start_time is not None and event.meta.event_time < start_time:
                    continue
                event.meta.assert_usable_at(decision_time)
                if not self.universe.event_was_in_active_universe(event):
                    continue
                identity = event.meta.canonical_identity()
                prior = by_identity.get(identity)
                if prior is not None and event_to_record(prior) != event_to_record(event):
                    raise PartitionIntegrityError("canonical event identity collision across partitions")
                by_identity[identity] = event
        events = tuple(sorted(by_identity.values(), key=event_sort_key))
        identity_hash = sha256(_canonical_json([event.meta.canonical_identity() for event in events])).hexdigest()
        manifest = ResearchDatasetManifest(
            dataset_id=dataset_id,
            format_version=_DATASET_FORMAT_VERSION,
            decision_time=decision_time,
            start_time=start_time,
            partition_descriptors=selected,
            universe_fingerprint=self.universe.fingerprint(),
            event_count=len(events),
            event_identity_hash=identity_hash,
        )
        return AssembledResearchDataset(events=events, manifest=manifest)


class DeterministicCryptoPerpReplay:
    """Deterministic PIT replay for a manifest-bound assembled crypto-perpetual dataset."""

    def __init__(self, dataset: AssembledResearchDataset):
        identities = [event.meta.canonical_identity() for event in dataset.events]
        actual_hash = sha256(_canonical_json(identities)).hexdigest()
        if len(dataset.events) != dataset.manifest.event_count or actual_hash != dataset.manifest.event_identity_hash:
            raise ValueError("assembled dataset does not match its manifest")
        self.dataset = dataset

    def iter_until(self, decision_time: datetime) -> Iterator[ResearchEvent]:
        _aware(decision_time, "decision_time")
        if decision_time > self.dataset.manifest.decision_time:
            raise ValueError("replay cannot move beyond the dataset manifest decision_time")
        for event in self.dataset.events:
            if event.meta.available_at > decision_time:
                break
            event.meta.assert_usable_at(decision_time)
            yield event

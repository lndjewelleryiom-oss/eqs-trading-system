"""Canonical crypto perpetual-futures market-data models and connector contracts."""

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

from .real_market_population import (
    ClockCorrection,
    RawCaptureReceipt,
    RealMarketPopulation,
    load_public_rest_capture,
)

from .research_datasets import (
    AssembledResearchDataset,
    DeterministicCryptoPerpReplay,
    HistoricalPartitionStore,
    InstrumentUniverseHistory,
    PartitionDescriptor,
    PartitionIntegrityError,
    PartitionKey,
    PointInTimeDatasetAssembler,
    ResearchDatasetManifest,
    ResearchManifestCatalog,
    event_from_record,
    event_to_record,
)

__all__ = [
    "BookLevel",
    "BookUpdate",
    "EventKind",
    "LiquidatedSide",
    "LiquidationEvent",
    "MarketDataMeta",
    "PerpetualInstrumentDefinition",
    "PerpetualStateEvent",
    "TradeEvent",
    "ClockCorrection",
    "RawCaptureReceipt",
    "RealMarketPopulation",
    "load_public_rest_capture",
    "AssembledResearchDataset",
    "DeterministicCryptoPerpReplay",
    "HistoricalPartitionStore",
    "InstrumentUniverseHistory",
    "PartitionDescriptor",
    "PartitionIntegrityError",
    "PartitionKey",
    "PointInTimeDatasetAssembler",
    "ResearchDatasetManifest",
    "ResearchManifestCatalog",
    "event_from_record",
    "event_to_record",
]

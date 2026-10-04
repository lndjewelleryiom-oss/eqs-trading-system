from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import time
from urllib.request import Request, urlopen

from quant_system.backtest.events import BarEvent
from quant_system.backtest.interfaces import ExecutionAssumptions
from quant_system.backtest.simulator import ConservativeBarExecutionSimulator
from quant_system.data.crypto_perps.feature_source import R13FeatureDatasetSource
from quant_system.data.crypto_perps.models import EventKind, MarketDataMeta, PerpetualInstrumentDefinition, PerpetualStateEvent
from quant_system.data.crypto_perps.research_datasets import HistoricalPartitionStore, InstrumentUniverseHistory, PointInTimeDatasetAssembler
from quant_system.execution.broker import BrokerGateway, InMemoryBrokerAdapter
from quant_system.features import CryptoPerpetualFeatureEngine
from quant_system.nonlive import DeterministicInfrastructureTestStrategy, NonLiveExecutionPipeline
from quant_system.paper import PaperTradingEngine
from quant_system.risk.policy import PortfolioRiskSnapshot, RiskEngine, RiskLimits
from quant_system.runtime import PersistentPaperShadowRuntime, PersistentRuntimeStore, RuntimeMode
from quant_system.shadow import ShadowExecutionEngine

UTC = timezone.utc
VENUE = "BINANCE_USDM"
INSTRUMENT = "BTC-USDT-PERP:BINANCE_USDM"
DATASET_ID = "public-readonly-soak-v1"
PUBLIC_URL = "https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT"


def _fetch(timeout: float) -> tuple[bytes, datetime]:
    request = Request(PUBLIC_URL, headers={"User-Agent": "EQS-readonly-nonlive-soak/1.0"})
    with urlopen(request, timeout=timeout) as response:  # nosec B310 - fixed HTTPS public market endpoint
        payload = response.read()
    return payload, datetime.now(UTC)


def _event(raw: bytes, received: datetime) -> PerpetualStateEvent:
    payload = json.loads(raw)
    venue_ms = int(payload.get("time") or int(received.timestamp() * 1000))
    venue_time = datetime.fromtimestamp(venue_ms / 1000, tz=UTC)
    available_at = max(received, venue_time)
    digest = sha256(raw).hexdigest()
    meta = MarketDataMeta(
        venue=VENUE,
        instrument_id=INSTRUMENT,
        venue_symbol="BTCUSDT",
        kind=EventKind.PERPETUAL_STATE,
        event_time=venue_time,
        published_at=venue_time,
        available_at=available_at,
        received_at=available_at,
        source_channel="public-rest-premiumIndex",
        source_sequence=str(venue_ms),
        raw_sha256=digest,
    )
    return PerpetualStateEvent(
        meta=meta,
        mark_price=Decimal(str(payload["markPrice"])),
        index_price=Decimal(str(payload["indexPrice"])),
        funding_rate=Decimal(str(payload["lastFundingRate"])) if payload.get("lastFundingRate") not in (None, "") else None,
    )


def _definition(first_event: PerpetualStateEvent) -> PerpetualInstrumentDefinition:
    available = first_event.meta.available_at - timedelta(seconds=1)
    return PerpetualInstrumentDefinition(
        instrument_id=INSTRUMENT,
        venue=VENUE,
        venue_symbol="BTCUSDT",
        base_asset="BTC",
        quote_asset="USDT",
        settle_asset="USDT",
        contract_style="LINEAR",
        tick_size=Decimal("0.1"),
        lot_size=Decimal("0.001"),
        contract_value=None,
        status="TRADING",
        effective_from=available - timedelta(days=1),
        published_at=available,
        available_at=available,
        received_at=available,
        raw_sha256=sha256(b"public-soak-harness-instrument-definition").hexdigest(),
    )


def _assumptions() -> ExecutionAssumptions:
    return ExecutionAssumptions(
        commission_bps=Decimal("1"), spread_bps=Decimal("2"), slippage_bps=Decimal("1"), impact_bps=Decimal("1"),
        financing_bps_annual=Decimal("0"), borrow_bps_annual=Decimal("0"), latency_ms=0,
    )


def _risk() -> RiskEngine:
    return RiskEngine(RiskLimits(
        max_order_notional=Decimal("1000000"), max_symbol_notional=Decimal("1000000"),
        max_strategy_notional=Decimal("1000000"), max_gross_notional=Decimal("1000000"),
        max_leverage=Decimal("100"), max_daily_loss=Decimal("5000"),
        max_drawdown_fraction=Decimal("0.50"), max_data_age=timedelta(seconds=10),
    ))


def _snapshot(at: datetime) -> PortfolioRiskSnapshot:
    return PortfolioRiskSnapshot(Decimal("10000"), Decimal("10000"), Decimal("0"), {}, {}, Decimal("0"), at)


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only public market -> R1.3 -> feature -> PAPER/SHADOW soak")
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--interval", type=float, default=0.25)
    parser.add_argument("--timeout", type=float, default=3.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.samples < 2:
        raise SystemExit("--samples must be >= 2")

    observed: dict[str, PerpetualStateEvent] = {}
    try:
        for index in range(args.samples):
            raw, received = _fetch(args.timeout)
            event = _event(raw, received)
            observed[event.meta.canonical_identity()] = event
            if index + 1 < args.samples:
                time.sleep(args.interval)
    except Exception as exc:
        evidence = {
            "status": "BLOCKED_NETWORK",
            "endpoint": PUBLIC_URL,
            "exception_type": type(exc).__name__,
            "exception": str(exc),
            "credentials_accessed": False,
            "authenticated_endpoint_used": False,
            "venue_submission_enabled": False,
        }
        encoded = json.dumps(evidence, sort_keys=True, indent=2)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(encoded + "\n")
        print(encoded)
        return 2

    events = tuple(sorted(observed.values(), key=lambda item: item.meta.available_at))
    decision_time = max(item.meta.available_at for item in events) + timedelta(milliseconds=1)
    with tempfile.TemporaryDirectory(prefix="eqs-public-soak-") as temp:
        root = Path(temp)
        partition_store = HistoricalPartitionStore(root / "partitions")
        partitions = partition_store.write_events(DATASET_ID, events)
        source = R13FeatureDatasetSource(
            PointInTimeDatasetAssembler(partition_store, InstrumentUniverseHistory((_definition(events[0]),))),
            dataset_id=DATASET_ID,
            partitions=partitions,
        )

        paper_store = PersistentRuntimeStore(root / "paper.db")
        paper_engine = PaperTradingEngine(ConservativeBarExecutionSimulator(_assumptions()), initial_cash=Decimal("10000"))
        paper_runtime = PersistentPaperShadowRuntime(runtime_id="public-paper", mode=RuntimeMode.PAPER, store=paper_store, paper_engine=paper_engine)
        paper_runtime.start()
        paper_result = NonLiveExecutionPipeline(
            source=source, feature_engine=CryptoPerpetualFeatureEngine(), strategy=DeterministicInfrastructureTestStrategy(),
            risk_engine=_risk(), runtime=paper_runtime,
        ).run_once(instrument_id=INSTRUMENT, venue=VENUE, decision_time=decision_time, risk_snapshot=_snapshot(decision_time))
        mark = Decimal(str(events[-1].mark_price))
        paper_runtime.on_bar(BarEvent(INSTRUMENT, decision_time + timedelta(milliseconds=1), mark, mark, mark, mark, Decimal("100000")))
        paper_reconciled = paper_runtime.paper_engine.ledger.reconcile().ok

        adapter = InMemoryBrokerAdapter()
        shadow_store = PersistentRuntimeStore(root / "shadow.db")
        shadow_runtime = PersistentPaperShadowRuntime(
            runtime_id="public-shadow", mode=RuntimeMode.SHADOW, store=shadow_store,
            shadow_engine=ShadowExecutionEngine(BrokerGateway(adapter, venue_submission_enabled=False)),
        )
        shadow_runtime.start()
        shadow_result = NonLiveExecutionPipeline(
            source=source, feature_engine=CryptoPerpetualFeatureEngine(), strategy=DeterministicInfrastructureTestStrategy(),
            risk_engine=_risk(), runtime=shadow_runtime,
        ).run_once(instrument_id=INSTRUMENT, venue=VENUE, decision_time=decision_time, risk_snapshot=_snapshot(decision_time))

        evidence = {
            "status": "PASS",
            "endpoint": PUBLIC_URL,
            "samples_requested": args.samples,
            "unique_samples": len(events),
            "raw_sha256s": sorted({event.meta.raw_sha256 for event in events}),
            "dataset_fingerprint": paper_result.batch.dataset_fingerprints[0],
            "feature_manifest_fingerprint": paper_result.feature_run.manifest.fingerprint,
            "strategy_decision_fingerprint": paper_result.strategy_decision.fingerprint,
            "risk_decision_fingerprint": paper_result.risk_decision.fingerprint,
            "paper_ledger_reconciled": paper_reconciled,
            "shadow_sent": shadow_result.runtime_result.submission_result.sent if shadow_result.runtime_result else None,
            "shadow_adapter_submitted_count": len(adapter.submitted),
            "credentials_accessed": False,
            "authenticated_endpoint_used": False,
            "venue_submission_enabled": False,
            "profitability_or_edge_claimed": False,
        }
    encoded = json.dumps(evidence, sort_keys=True, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n")
    print(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

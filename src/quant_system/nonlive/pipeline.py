from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
import json
from typing import Any
from uuid import UUID, uuid5

from quant_system.core.enums import OrderType, RiskAction, Side
from quant_system.data.crypto_perps.feature_source import R13FeatureDatasetSource
from quant_system.data.crypto_perps.models import BookUpdate, PerpetualStateEvent, TradeEvent
from quant_system.execution.models import OrderRequest
from quant_system.features import CryptoPerpetualFeatureEngine, FeatureInputBatch, FeatureRun
from quant_system.risk.policy import PortfolioRiskSnapshot, RiskDecision, RiskEngine
from quant_system.risk.shared03 import (
    SCHEMA_VERSION as SHARED03_SCHEMA_VERSION,
    Shared03Context,
    Shared03GateDisposition,
    Shared03NonLiveGate,
)
from quant_system.runtime import PersistentPaperShadowRuntime


_ORDER_NAMESPACE = UUID("7c260b0e-2f7f-5d87-aeba-992fc93df589")
_STRATEGY_NAMESPACE = UUID("9db19e47-243b-57ad-a2bf-48da165e38e1")


def _canonical_json(payload: object) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str).encode("utf-8")


def _fingerprint(payload: object) -> str:
    return sha256(_canonical_json(payload)).hexdigest()


class SignalAction(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class MarketDataFault(RuntimeError):
    reason_code = "MARKET_DATA_FAULT"


class MarketDataSequenceError(MarketDataFault):
    reason_code = "BOOK_SEQUENCE_FAULT"


class DuplicateMarketEventError(MarketDataFault):
    reason_code = "DUPLICATE_MARKET_EVENT"


@dataclass(frozen=True, slots=True)
class DeterministicTestStrategyConfig:
    feature_name: str = "basis.mark_index_bps"
    threshold: Decimal = Decimal("0")
    order_quantity: Decimal = Decimal("0.01")
    strategy_version: str = "deterministic-infrastructure-test-v1"

    def __post_init__(self) -> None:
        if not self.feature_name.strip() or not self.strategy_version.strip():
            raise ValueError("feature_name and strategy_version are required")
        if self.order_quantity <= 0:
            raise ValueError("order_quantity must be positive")

    @property
    def strategy_id(self) -> UUID:
        payload = f"{self.strategy_version}|{self.feature_name}|{self.threshold}|{self.order_quantity}"
        return uuid5(_STRATEGY_NAMESPACE, payload)


@dataclass(frozen=True, slots=True)
class StrategyDecision:
    strategy_id: UUID
    strategy_version: str
    signal: SignalAction
    reason_codes: tuple[str, ...]
    instrument_id: str
    venue: str
    symbol: str
    decision_time: datetime
    quantity: Decimal
    reference_price: Decimal | None
    selected_feature_name: str
    selected_feature_value: str | None
    selected_feature_fingerprint: str | None
    feature_manifest_fingerprint: str
    feature_input_batch_fingerprint: str
    dataset_fingerprints: tuple[str, ...]
    universe_version: str
    source_event_ids: tuple[str, ...]
    source_raw_sha256s: tuple[str, ...]
    infrastructure_boundary_fingerprint: str | None = None

    def to_payload(self) -> dict[str, object]:
        return {
            "strategy_id": str(self.strategy_id),
            "strategy_version": self.strategy_version,
            "signal": self.signal.value,
            "reason_codes": self.reason_codes,
            "instrument_id": self.instrument_id,
            "venue": self.venue,
            "symbol": self.symbol,
            "decision_time": self.decision_time.isoformat(),
            "quantity": str(self.quantity),
            "reference_price": None if self.reference_price is None else str(self.reference_price),
            "selected_feature_name": self.selected_feature_name,
            "selected_feature_value": self.selected_feature_value,
            "selected_feature_fingerprint": self.selected_feature_fingerprint,
            "feature_manifest_fingerprint": self.feature_manifest_fingerprint,
            "feature_input_batch_fingerprint": self.feature_input_batch_fingerprint,
            "dataset_fingerprints": self.dataset_fingerprints,
            "universe_version": self.universe_version,
            "source_event_ids": self.source_event_ids,
            "source_raw_sha256s": self.source_raw_sha256s,
            "infrastructure_boundary_fingerprint": self.infrastructure_boundary_fingerprint,
        }

    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.to_payload())


@dataclass(frozen=True, slots=True)
class RiskDecisionRecord:
    action: RiskAction
    reason_codes: tuple[str, ...]
    decision_time: datetime
    strategy_decision_fingerprint: str
    risk_snapshot_fingerprint: str
    order_id: UUID | None
    order_fingerprint: str | None
    feature_manifest_fingerprint: str
    dataset_fingerprints: tuple[str, ...]
    universe_version: str
    infrastructure_boundary_fingerprint: str | None = None
    shared03_schema_version: str | None = None
    capital_state_fingerprint: str | None = None
    reservation_fingerprint: str | None = None
    exposure_fingerprints: tuple[str, ...] = ()
    shared03_context_fingerprint: str | None = None
    shared03_reason_codes: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, object]:
        payload = {
            "action": self.action.value,
            "reason_codes": self.reason_codes,
            "decision_time": self.decision_time.isoformat(),
            "strategy_decision_fingerprint": self.strategy_decision_fingerprint,
            "risk_snapshot_fingerprint": self.risk_snapshot_fingerprint,
            "order_id": None if self.order_id is None else str(self.order_id),
            "order_fingerprint": self.order_fingerprint,
            "feature_manifest_fingerprint": self.feature_manifest_fingerprint,
            "dataset_fingerprints": self.dataset_fingerprints,
            "universe_version": self.universe_version,
            "infrastructure_boundary_fingerprint": self.infrastructure_boundary_fingerprint,
        }
        if self.shared03_schema_version is not None:
            payload.update({
                "shared03_schema_version": self.shared03_schema_version,
                "capital_state_fingerprint": self.capital_state_fingerprint,
                "reservation_fingerprint": self.reservation_fingerprint,
                "exposure_fingerprints": self.exposure_fingerprints,
                "shared03_context_fingerprint": self.shared03_context_fingerprint,
                "shared03_reason_codes": self.shared03_reason_codes,
            })
        return payload

    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.to_payload())


@dataclass(frozen=True, slots=True)
class PipelineResult:
    batch: FeatureInputBatch
    feature_run: FeatureRun
    strategy_decision: StrategyDecision
    risk_decision: RiskDecisionRecord
    order: OrderRequest | None
    runtime_result: object | None


class MarketDataAcceptanceGuard:
    """Fail-closed structural checks before feature generation.

    Staleness is deliberately handled by the authoritative RiskEngine so it becomes an
    explicit risk decision rather than an implicit preprocessing drop.
    """

    def validate(self, batch: FeatureInputBatch) -> None:
        identities: set[str] = set()
        books: list[BookUpdate] = []
        for event in batch.events:
            identity = event.meta.canonical_identity()
            if identity in identities:
                raise DuplicateMarketEventError(identity)
            identities.add(identity)
            if isinstance(event, BookUpdate):
                books.append(event)

        previous_final: int | None = None
        for book in sorted(books, key=lambda item: (item.meta.available_at, item.meta.event_time, item.meta.canonical_identity())):
            if book.previous_sequence is not None and previous_final is not None and book.previous_sequence != previous_final:
                raise MarketDataSequenceError(
                    f"book previous_sequence={book.previous_sequence} expected={previous_final}"
                )
            if book.first_sequence is not None and previous_final is not None and book.first_sequence > previous_final + 1:
                raise MarketDataSequenceError(
                    f"book first_sequence={book.first_sequence} expected <= {previous_final + 1}"
                )
            if book.final_sequence is not None:
                if previous_final is not None and book.final_sequence <= previous_final:
                    raise MarketDataSequenceError("book final sequence did not advance")
                previous_final = book.final_sequence


class DeterministicInfrastructureTestStrategy:
    """A deliberately non-alpha deterministic signal used only for infrastructure acceptance."""

    def __init__(self, config: DeterministicTestStrategyConfig = DeterministicTestStrategyConfig()):
        self.config = config

    @staticmethod
    def _reference_price(batch: FeatureInputBatch) -> Decimal | None:
        ordered = sorted(batch.events, key=lambda event: (event.meta.available_at, event.meta.event_time, event.meta.canonical_identity()), reverse=True)
        for event in ordered:
            if isinstance(event, PerpetualStateEvent) and event.mark_price is not None and event.mark_price > 0:
                return event.mark_price
            if isinstance(event, TradeEvent) and event.price > 0:
                return event.price
            if isinstance(event, BookUpdate) and event.bids and event.asks:
                return (event.bids[0].price + event.asks[0].price) / Decimal("2")
        return None

    def decide(self, batch: FeatureInputBatch, run: FeatureRun, *, symbol: str | None = None) -> StrategyDecision:
        record = run.by_name().get(self.config.feature_name)
        reference_price = self._reference_price(batch)
        if record is None:
            signal = SignalAction.HOLD
            value = None
            feature_fp = None
            source_ids: tuple[str, ...] = ()
            source_hashes: tuple[str, ...] = ()
            reasons = ("TEST_FEATURE_UNAVAILABLE",)
        else:
            try:
                numeric = Decimal(str(record.value))
            except Exception as exc:
                raise ValueError("selected deterministic test feature must be numeric") from exc
            signal = SignalAction.BUY if numeric >= self.config.threshold else SignalAction.SELL
            value = str(record.value)
            feature_fp = record.fingerprint
            source_ids = tuple(ref.event_id for ref in record.source_events)
            source_hashes = tuple(sorted({ref.raw_sha256 for ref in record.source_events}))
            reasons = ("DETERMINISTIC_TEST_SIGNAL_ONLY",)
        if reference_price is None:
            signal = SignalAction.HOLD
            reasons = tuple(sorted(set(reasons + ("REFERENCE_PRICE_UNAVAILABLE",))))
        return StrategyDecision(
            strategy_id=self.config.strategy_id,
            strategy_version=self.config.strategy_version,
            signal=signal,
            reason_codes=reasons,
            instrument_id=batch.instrument_id,
            venue=batch.venue,
            symbol=symbol or batch.instrument_id,
            decision_time=batch.decision_time,
            quantity=self.config.order_quantity,
            reference_price=reference_price,
            selected_feature_name=self.config.feature_name,
            selected_feature_value=value,
            selected_feature_fingerprint=feature_fp,
            feature_manifest_fingerprint=run.manifest.fingerprint,
            feature_input_batch_fingerprint=batch.fingerprint,
            dataset_fingerprints=batch.dataset_fingerprints,
            universe_version=batch.universe_version,
            source_event_ids=source_ids,
            source_raw_sha256s=source_hashes,
            infrastructure_boundary_fingerprint=batch.infrastructure_boundary_fingerprint,
        )

    def order_for(self, decision: StrategyDecision) -> OrderRequest | None:
        if decision.signal == SignalAction.HOLD or decision.reference_price is None:
            return None
        side = Side.BUY if decision.signal == SignalAction.BUY else Side.SELL
        order_id = uuid5(_ORDER_NAMESPACE, decision.fingerprint)
        return OrderRequest(
            strategy_id=decision.strategy_id,
            symbol=decision.symbol,
            side=side,
            quantity=decision.quantity,
            order_type=OrderType.MARKET,
            decision_time=decision.decision_time,
            reference_price=decision.reference_price,
            order_id=order_id,
        )


def risk_snapshot_fingerprint(snapshot: PortfolioRiskSnapshot) -> str:
    return _fingerprint({
        "equity": str(snapshot.equity),
        "peak_equity": str(snapshot.peak_equity),
        "gross_notional": str(snapshot.gross_notional),
        "symbol_notional": {key: str(value) for key, value in sorted(snapshot.symbol_notional.items())},
        "strategy_notional": {key: str(value) for key, value in sorted(snapshot.strategy_notional.items())},
        "daily_pnl": str(snapshot.daily_pnl),
        "market_data_received_at": snapshot.market_data_received_at.isoformat(),
        "global_kill_switch": snapshot.global_kill_switch,
    })


def order_fingerprint(order: OrderRequest) -> str:
    return _fingerprint({
        "strategy_id": str(order.strategy_id),
        "symbol": order.symbol,
        "side": order.side.value,
        "quantity": str(order.quantity),
        "order_type": order.order_type.value,
        "decision_time": order.decision_time.isoformat(),
        "reference_price": str(order.reference_price),
        "limit_price": None if order.limit_price is None else str(order.limit_price),
        "reduce_only": order.reduce_only,
        "order_id": str(order.order_id),
    })


class NonLiveExecutionPipeline:
    """R1.3 PIT -> feature -> deterministic test signal -> risk -> PAPER/SHADOW.

    No live broker enablement, credential loading, authenticated endpoint, or capital path is
    present here. The strategy is a deterministic plumbing fixture and makes no edge claim.
    """

    def __init__(
        self,
        *,
        source: R13FeatureDatasetSource,
        feature_engine: CryptoPerpetualFeatureEngine,
        strategy: DeterministicInfrastructureTestStrategy,
        risk_engine: RiskEngine,
        runtime: PersistentPaperShadowRuntime,
        guard: MarketDataAcceptanceGuard | None = None,
        shared03_gate: Shared03NonLiveGate | None = None,
    ) -> None:
        self.source = source
        self.feature_engine = feature_engine
        self.strategy = strategy
        self.risk_engine = risk_engine
        self.runtime = runtime
        self.guard = guard or MarketDataAcceptanceGuard()
        self.shared03_gate = shared03_gate

    def run_once(
        self,
        *,
        instrument_id: str,
        venue: str,
        decision_time: datetime,
        risk_snapshot: PortfolioRiskSnapshot,
        symbol: str | None = None,
        shared03_context: Shared03Context | None = None,
    ) -> PipelineResult:
        batch = self.source.load_feature_batch(instrument_id=instrument_id, venue=venue, decision_time=decision_time)
        try:
            self.guard.validate(batch)
        except MarketDataFault as exc:
            self.runtime.record_pipeline_event(
                "MARKET_DATA_FAULT",
                {"reason_code": exc.reason_code, "detail": str(exc), "input_batch_fingerprint": batch.fingerprint},
            )
            self.runtime.halt(f"MARKET_DATA_FAULT:{exc.reason_code}")
            raise

        run = self.feature_engine.compute(batch)
        strategy_decision = self.strategy.decide(batch, run, symbol=symbol)
        newest_available = max(event.meta.available_at for event in batch.events)
        effective_snapshot = replace(risk_snapshot, market_data_received_at=newest_available)
        order = self.strategy.order_for(strategy_decision)

        if order is None:
            raw_risk = RiskDecision(RiskAction.BLOCK, ("NO_ORDER_FOR_HOLD",))
            order_fp = None
        else:
            raw_risk = self.risk_engine.evaluate(order, effective_snapshot)
            order_fp = order_fingerprint(order)

        effective_action = raw_risk.action
        effective_reasons = raw_risk.reason_codes
        shared03_reasons: tuple[str, ...] = ()
        capital_state_fp = None
        reservation_fp = None
        exposure_fps: tuple[str, ...] = ()
        context_fp = None

        if order is not None and self.shared03_gate is not None:
            if shared03_context is None:
                shared03_reasons = ("SHARED03_CONTEXT_MISSING",)
                if raw_risk.action == RiskAction.ALLOW:
                    effective_action = RiskAction.HALT
                    effective_reasons = tuple(sorted(set(raw_risk.reason_codes + shared03_reasons)))
            else:
                capital_state_fp = shared03_context.capital_state.state_fingerprint
                reservation_fp = None if shared03_context.reservation is None else shared03_context.reservation.reservation_fingerprint
                exposure_fps = tuple(item.exposure_fingerprint for item in shared03_context.exposures)
                context_fp = shared03_context.context_fingerprint
                if shared03_context.execution_mode != self.runtime.mode.value:
                    shared03_reasons = ("SHARED03_EXECUTION_MODE_MISMATCH",)
                    shared03_disposition = Shared03GateDisposition.HALT
                else:
                    gate_result = self.shared03_gate.evaluate(order, shared03_context)
                    shared03_reasons = gate_result.reason_codes
                    shared03_disposition = gate_result.disposition
                if raw_risk.action == RiskAction.ALLOW:
                    if shared03_disposition == Shared03GateDisposition.HALT:
                        effective_action = RiskAction.HALT
                    elif shared03_disposition == Shared03GateDisposition.BLOCK:
                        effective_action = RiskAction.BLOCK
                    if shared03_disposition != Shared03GateDisposition.ALLOW:
                        effective_reasons = tuple(sorted(set(raw_risk.reason_codes + shared03_reasons)))

        risk_record = RiskDecisionRecord(
            action=effective_action,
            reason_codes=effective_reasons,
            decision_time=decision_time,
            strategy_decision_fingerprint=strategy_decision.fingerprint,
            risk_snapshot_fingerprint=risk_snapshot_fingerprint(effective_snapshot),
            order_id=None if order is None else order.order_id,
            order_fingerprint=order_fp,
            feature_manifest_fingerprint=run.manifest.fingerprint,
            dataset_fingerprints=batch.dataset_fingerprints,
            universe_version=batch.universe_version,
            infrastructure_boundary_fingerprint=batch.infrastructure_boundary_fingerprint,
            shared03_schema_version=None if self.shared03_gate is None else SHARED03_SCHEMA_VERSION,
            capital_state_fingerprint=capital_state_fp,
            reservation_fingerprint=reservation_fp,
            exposure_fingerprints=exposure_fps,
            shared03_context_fingerprint=context_fp,
            shared03_reason_codes=shared03_reasons,
        )
        self.runtime.record_pipeline_event("STRATEGY_DECISION", strategy_decision.to_payload() | {"fingerprint": strategy_decision.fingerprint})
        self.runtime.record_pipeline_event("RISK_DECISION", risk_record.to_payload() | {"fingerprint": risk_record.fingerprint})

        runtime_result: object | None = None
        if risk_record.action == RiskAction.HALT:
            self.runtime.halt("RISK_HALT:" + ",".join(risk_record.reason_codes))
        elif risk_record.action == RiskAction.ALLOW and order is not None:
            lineage: dict[str, Any] = {
                "dataset_fingerprints": list(batch.dataset_fingerprints),
                "infrastructure_boundary_fingerprint": batch.infrastructure_boundary_fingerprint,
                "universe_version": batch.universe_version,
                "feature_input_batch_fingerprint": batch.fingerprint,
                "feature_manifest_fingerprint": run.manifest.fingerprint,
                "feature_record_fingerprint": strategy_decision.selected_feature_fingerprint,
                "strategy_decision_fingerprint": strategy_decision.fingerprint,
                "risk_decision_fingerprint": risk_record.fingerprint,
                "order_fingerprint": order_fp,
                "source_event_ids": list(strategy_decision.source_event_ids),
                "source_raw_sha256s": list(strategy_decision.source_raw_sha256s),
            }
            if risk_record.shared03_schema_version is not None:
                lineage.update({
                    "shared03_schema_version": risk_record.shared03_schema_version,
                    "capital_state_fingerprint": risk_record.capital_state_fingerprint,
                    "reservation_fingerprint": risk_record.reservation_fingerprint,
                    "exposure_fingerprints": list(risk_record.exposure_fingerprints),
                    "shared03_context_fingerprint": risk_record.shared03_context_fingerprint,
                    "shared03_reason_codes": list(risk_record.shared03_reason_codes),
                })
            runtime_result = self.runtime.submit_order(order, lineage=lineage)

        return PipelineResult(batch, run, strategy_decision, risk_record, order, runtime_result)

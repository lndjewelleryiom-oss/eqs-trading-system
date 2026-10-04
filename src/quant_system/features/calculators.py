from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
import math
from statistics import fmean, pstdev
from typing import Iterable, Mapping, Protocol

from quant_system.data.crypto_perps.acceptance import BookSequenceGuard
from quant_system.data.crypto_perps.models import (
    BookUpdate,
    EventKind,
    LiquidatedSide,
    LiquidationEvent,
    PerpetualStateEvent,
    TradeEvent,
)

from .config import FeatureEngineConfig
from .models import FeatureDefinition, FeatureFamily, FeatureInputError, FeatureScalar, MarketEvent


@dataclass(frozen=True, slots=True)
class ComputedFeature:
    definition: FeatureDefinition
    value: FeatureScalar
    source_events: tuple[MarketEvent, ...]


@dataclass(slots=True)
class FeatureContext:
    batch_events: tuple[MarketEvent, ...]
    decision_time: datetime
    config: FeatureEngineConfig
    prior: dict[str, ComputedFeature]

    def window(self, seconds: int, event_type: type | tuple[type, ...] | None = None) -> tuple[MarketEvent, ...]:
        start = self.decision_time - timedelta(seconds=seconds)
        events = self.batch_events
        if event_type is not None:
            events = tuple(event for event in events if isinstance(event, event_type))
        return tuple(event for event in events if event.meta.event_time >= start)


class FeatureCalculator(Protocol):
    family: FeatureFamily

    def definitions(self, config: FeatureEngineConfig) -> tuple[FeatureDefinition, ...]: ...

    def compute(self, context: FeatureContext) -> tuple[ComputedFeature, ...]: ...


def _definition(
    *, name: str, family: FeatureFamily, config: FeatureEngineConfig, unit: str,
    description: str, parameters: Mapping[str, object] | None = None,
) -> FeatureDefinition:
    items = tuple(sorted((key, str(value)) for key, value in (parameters or {}).items()))
    return FeatureDefinition(
        name=name,
        family=family,
        version=config.feature_version,
        unit=unit,
        description=description,
        parameters=items,
    )


def _event_key(event: MarketEvent) -> tuple[object, ...]:
    return (
        event.meta.event_time,
        event.meta.published_at,
        event.meta.available_at,
        event.meta.canonical_identity(),
    )


def _latest_state(events: Iterable[PerpetualStateEvent], field: str) -> PerpetualStateEvent | None:
    eligible = [event for event in events if getattr(event, field) is not None]
    return max(eligible, key=_event_key) if eligible else None


def _price_points(context: FeatureContext, seconds: int) -> tuple[tuple[float, MarketEvent], ...]:
    start = context.decision_time - timedelta(seconds=seconds)
    states = [
        event for event in context.batch_events
        if isinstance(event, PerpetualStateEvent)
        and event.mark_price is not None
        and event.meta.event_time >= start
    ]
    if len(states) >= 2:
        selected: list[tuple[float, MarketEvent]] = [(float(event.mark_price), event) for event in states]
    else:
        trades = [
            event for event in context.batch_events
            if isinstance(event, TradeEvent) and event.meta.event_time >= start
        ]
        selected = [(float(event.price), event) for event in trades]
    selected.sort(key=lambda item: _event_key(item[1]))
    # Collapse identical timestamps deterministically to the last published/available observation.
    by_time: dict[object, tuple[float, MarketEvent]] = {}
    for item in selected:
        by_time[item[1].meta.event_time] = item
    return tuple(by_time[key] for key in sorted(by_time))


def _returns(points: tuple[tuple[float, MarketEvent], ...]) -> tuple[float, ...]:
    if len(points) < 2:
        return ()
    return tuple(math.log(current[0] / previous[0]) for previous, current in zip(points, points[1:]))


def _canonical_sources(events: Iterable[MarketEvent]) -> tuple[MarketEvent, ...]:
    unique = {event.meta.canonical_identity(): event for event in events}
    return tuple(sorted(unique.values(), key=_event_key))


class MicrostructureCalculator:
    family = FeatureFamily.MICROSTRUCTURE

    def definitions(self, config: FeatureEngineConfig) -> tuple[FeatureDefinition, ...]:
        depth = config.book_depth_levels
        window = config.microstructure_window_seconds
        return (
            _definition(name="micro.spread_bps", family=self.family, config=config, unit="bps",
                        description="Top-of-book bid/ask spread relative to midpoint."),
            _definition(name=f"micro.book_imbalance_l{depth}", family=self.family, config=config, unit="ratio",
                        description="Bid-minus-ask quantity imbalance across configured book depth.",
                        parameters={"depth_levels": depth}),
            _definition(name="micro.microprice_deviation_bps", family=self.family, config=config, unit="bps",
                        description="Top-level size-weighted microprice deviation from midpoint."),
            _definition(name="micro.top_depth_notional", family=self.family, config=config, unit="quote_notional",
                        description="Combined bid and ask price-times-quantity across configured book depth.",
                        parameters={"depth_levels": depth}),
            _definition(name=f"micro.trade_imbalance_{window}s", family=self.family, config=config, unit="ratio",
                        description="Aggressor buy-minus-sell quantity imbalance over the configured window.",
                        parameters={"window_seconds": window}),
        )

    def compute(self, context: FeatureContext) -> tuple[ComputedFeature, ...]:
        defs = {definition.name: definition for definition in self.definitions(context.config)}
        books = [event for event in context.batch_events if isinstance(event, BookUpdate)]
        outputs: list[ComputedFeature] = []
        if books:
            snapshot_indexes = [index for index, event in enumerate(books) if event.meta.kind == EventKind.BOOK_SNAPSHOT]
            if snapshot_indexes:
                start_index = snapshot_indexes[-1]
                relevant = books[start_index:]
                if all(update.final_sequence is not None for update in relevant):
                    guard = BookSequenceGuard()
                    for update in relevant:
                        guard.accept(update)
                bids: dict[Decimal, Decimal] = {}
                asks: dict[Decimal, Decimal] = {}
                for index, update in enumerate(relevant):
                    if index == 0:
                        bids = {level.price: level.quantity for level in update.bids if level.quantity > 0}
                        asks = {level.price: level.quantity for level in update.asks if level.quantity > 0}
                    else:
                        for level in update.bids:
                            if level.quantity == 0:
                                bids.pop(level.price, None)
                            else:
                                bids[level.price] = level.quantity
                        for level in update.asks:
                            if level.quantity == 0:
                                asks.pop(level.price, None)
                            else:
                                asks[level.price] = level.quantity
                if bids and asks:
                    best_bid = max(bids)
                    best_ask = min(asks)
                    if best_bid >= best_ask:
                        raise FeatureInputError("reconstructed order book is crossed")
                    midpoint = (best_bid + best_ask) / Decimal(2)
                    spread_bps = float((best_ask - best_bid) / midpoint * Decimal(10_000))
                    levels = context.config.book_depth_levels
                    bid_levels = sorted(bids.items(), reverse=True)[:levels]
                    ask_levels = sorted(asks.items())[:levels]
                    best_bid_qty = bids[best_bid]
                    best_ask_qty = asks[best_ask]
                    top_qty = best_bid_qty + best_ask_qty
                    microprice = (
                        best_ask * best_bid_qty + best_bid * best_ask_qty
                    ) / top_qty if top_qty > 0 else midpoint
                    microprice_deviation = float((microprice - midpoint) / midpoint * Decimal(10_000))
                    sources = _canonical_sources(relevant)
                    outputs.extend((
                        ComputedFeature(defs["micro.spread_bps"], spread_bps, sources),
                        ComputedFeature(defs["micro.microprice_deviation_bps"], microprice_deviation, sources),
                    ))
                    # Depth-labelled features must fail closed when the source
                    # does not actually contain the configured number of levels.
                    if len(bid_levels) == levels and len(ask_levels) == levels:
                        bid_qty = sum((quantity for _, quantity in bid_levels), Decimal(0))
                        ask_qty = sum((quantity for _, quantity in ask_levels), Decimal(0))
                        total_qty = bid_qty + ask_qty
                        imbalance = float((bid_qty - ask_qty) / total_qty) if total_qty > 0 else 0.0
                        depth_notional = float(sum(
                            (price * quantity for price, quantity in (*bid_levels, *ask_levels)), Decimal(0)
                        ))
                        outputs.extend((
                            ComputedFeature(defs[f"micro.book_imbalance_l{levels}"], imbalance, sources),
                            ComputedFeature(defs["micro.top_depth_notional"], depth_notional, sources),
                        ))

        trades = [
            event for event in context.window(context.config.microstructure_window_seconds, TradeEvent)
            if event.aggressor_side in {"BUY", "SELL"}
        ]
        if trades:
            buy = sum((event.quantity for event in trades if event.aggressor_side == "BUY"), Decimal(0))
            sell = sum((event.quantity for event in trades if event.aggressor_side == "SELL"), Decimal(0))
            total = buy + sell
            imbalance = float((buy - sell) / total) if total > 0 else 0.0
            name = f"micro.trade_imbalance_{context.config.microstructure_window_seconds}s"
            outputs.append(ComputedFeature(defs[name], imbalance, _canonical_sources(trades)))
        return tuple(outputs)


class FundingCalculator:
    family = FeatureFamily.FUNDING

    def definitions(self, config: FeatureEngineConfig) -> tuple[FeatureDefinition, ...]:
        window = config.funding_window_seconds
        return (
            _definition(name="funding.current_rate", family=self.family, config=config, unit="rate",
                        description="Latest funding rate known by decision time."),
            _definition(name=f"funding.mean_rate_{window}s", family=self.family, config=config, unit="rate",
                        description="Mean known funding rate over the configured event-time window.",
                        parameters={"window_seconds": window}),
            _definition(name=f"funding.change_{window}s", family=self.family, config=config, unit="rate",
                        description="Latest minus earliest known funding rate over the configured window.",
                        parameters={"window_seconds": window}),
        )

    def compute(self, context: FeatureContext) -> tuple[ComputedFeature, ...]:
        defs = {definition.name: definition for definition in self.definitions(context.config)}
        all_states = [event for event in context.batch_events if isinstance(event, PerpetualStateEvent)]
        latest = _latest_state(all_states, "funding_rate")
        if latest is None:
            return ()
        window = context.config.funding_window_seconds
        history = [
            event for event in context.window(window, PerpetualStateEvent)
            if event.funding_rate is not None
        ]
        history.sort(key=_event_key)
        name_mean = f"funding.mean_rate_{window}s"
        name_change = f"funding.change_{window}s"
        outputs = [ComputedFeature(defs["funding.current_rate"], float(latest.funding_rate), (latest,))]
        if history:
            values = [float(event.funding_rate) for event in history]
            sources = _canonical_sources(history)
            outputs.append(ComputedFeature(defs[name_mean], fmean(values), sources))
            if len(values) >= 2:
                outputs.append(ComputedFeature(defs[name_change], values[-1] - values[0], sources))
        return tuple(outputs)


class BasisCalculator:
    family = FeatureFamily.BASIS

    def definitions(self, config: FeatureEngineConfig) -> tuple[FeatureDefinition, ...]:
        window = config.basis_window_seconds
        return (
            _definition(name="basis.mark_index_bps", family=self.family, config=config, unit="bps",
                        description="Current mark-minus-index basis relative to index price."),
            _definition(name=f"basis.change_{window}s_bps", family=self.family, config=config, unit="bps",
                        description="Change in mark/index basis over the configured window.",
                        parameters={"window_seconds": window}),
        )

    @staticmethod
    def _basis(event: PerpetualStateEvent) -> float:
        if event.mark_price is None or event.index_price is None or event.index_price <= 0:
            raise FeatureInputError("basis requires positive mark and index prices")
        return float((event.mark_price - event.index_price) / event.index_price * Decimal(10_000))

    def compute(self, context: FeatureContext) -> tuple[ComputedFeature, ...]:
        defs = {definition.name: definition for definition in self.definitions(context.config)}
        states = [
            event for event in context.batch_events if isinstance(event, PerpetualStateEvent)
            and event.mark_price is not None and event.index_price is not None
        ]
        if not states:
            return ()
        states.sort(key=_event_key)
        latest = states[-1]
        outputs = [ComputedFeature(defs["basis.mark_index_bps"], self._basis(latest), (latest,))]
        window = context.config.basis_window_seconds
        history = [event for event in states if event.meta.event_time >= context.decision_time - timedelta(seconds=window)]
        if len(history) >= 2:
            outputs.append(ComputedFeature(
                defs[f"basis.change_{window}s_bps"],
                self._basis(history[-1]) - self._basis(history[0]),
                _canonical_sources(history),
            ))
        return tuple(outputs)


class OpenInterestCalculator:
    family = FeatureFamily.OPEN_INTEREST

    def definitions(self, config: FeatureEngineConfig) -> tuple[FeatureDefinition, ...]:
        window = config.open_interest_window_seconds
        return (
            _definition(name="open_interest.current", family=self.family, config=config, unit="contracts",
                        description="Latest open-interest quantity known by decision time."),
            _definition(name="open_interest.value_current", family=self.family, config=config, unit="quote_notional",
                        description="Latest venue-reported open-interest notional/value."),
            _definition(name=f"open_interest.change_{window}s_pct", family=self.family, config=config, unit="percent",
                        description="Percentage change in open-interest quantity over the configured window.",
                        parameters={"window_seconds": window}),
        )

    def compute(self, context: FeatureContext) -> tuple[ComputedFeature, ...]:
        defs = {definition.name: definition for definition in self.definitions(context.config)}
        states = [event for event in context.batch_events if isinstance(event, PerpetualStateEvent)]
        outputs: list[ComputedFeature] = []
        latest_oi = _latest_state(states, "open_interest")
        if latest_oi is not None:
            outputs.append(ComputedFeature(defs["open_interest.current"], float(latest_oi.open_interest), (latest_oi,)))
            window = context.config.open_interest_window_seconds
            history = [
                event for event in context.window(window, PerpetualStateEvent)
                if event.open_interest is not None
            ]
            history.sort(key=_event_key)
            if len(history) >= 2 and history[0].open_interest and history[0].open_interest > 0:
                change = float((history[-1].open_interest / history[0].open_interest - Decimal(1)) * Decimal(100))
                outputs.append(ComputedFeature(
                    defs[f"open_interest.change_{window}s_pct"], change, _canonical_sources(history)
                ))
        latest_value = _latest_state(states, "open_interest_value")
        if latest_value is not None:
            outputs.append(ComputedFeature(
                defs["open_interest.value_current"], float(latest_value.open_interest_value), (latest_value,)
            ))
        return tuple(outputs)


class LiquidationCalculator:
    family = FeatureFamily.LIQUIDATION

    def definitions(self, config: FeatureEngineConfig) -> tuple[FeatureDefinition, ...]:
        window = config.liquidation_window_seconds
        params = {"window_seconds": window}
        return (
            _definition(name=f"liquidation.notional_{window}s", family=self.family, config=config, unit="quote_notional",
                        description="Total forced-liquidation price-times-quantity over the configured window.", parameters=params),
            _definition(name=f"liquidation.long_notional_{window}s", family=self.family, config=config, unit="quote_notional",
                        description="Long-side forced-liquidation notional over the configured window.", parameters=params),
            _definition(name=f"liquidation.short_notional_{window}s", family=self.family, config=config, unit="quote_notional",
                        description="Short-side forced-liquidation notional over the configured window.", parameters=params),
            _definition(name=f"liquidation.imbalance_{window}s", family=self.family, config=config, unit="ratio",
                        description="Long-minus-short liquidation notional divided by total liquidation notional.", parameters=params),
        )

    def compute(self, context: FeatureContext) -> tuple[ComputedFeature, ...]:
        defs = {definition.name: definition for definition in self.definitions(context.config)}
        window = context.config.liquidation_window_seconds
        events = list(context.window(window, LiquidationEvent))
        if not events:
            return ()
        long_notional = sum(
            (event.price * event.quantity for event in events if event.liquidated_side == LiquidatedSide.LONG), Decimal(0)
        )
        short_notional = sum(
            (event.price * event.quantity for event in events if event.liquidated_side == LiquidatedSide.SHORT), Decimal(0)
        )
        total = long_notional + short_notional
        imbalance = float((long_notional - short_notional) / total) if total > 0 else 0.0
        sources = _canonical_sources(events)
        return (
            ComputedFeature(defs[f"liquidation.notional_{window}s"], float(total), sources),
            ComputedFeature(defs[f"liquidation.long_notional_{window}s"], float(long_notional), sources),
            ComputedFeature(defs[f"liquidation.short_notional_{window}s"], float(short_notional), sources),
            ComputedFeature(defs[f"liquidation.imbalance_{window}s"], imbalance, sources),
        )


class VolatilityCalculator:
    family = FeatureFamily.VOLATILITY

    def definitions(self, config: FeatureEngineConfig) -> tuple[FeatureDefinition, ...]:
        window = config.volatility_window_seconds
        params = {"window_seconds": window, "price_source": "mark_else_trade"}
        return (
            _definition(name=f"volatility.realized_{window}s_bps", family=self.family, config=config, unit="bps",
                        description="Square-root sum of squared log returns over the configured window.", parameters=params),
            _definition(name=f"volatility.return_std_{window}s_bps", family=self.family, config=config, unit="bps",
                        description="Population standard deviation of log returns over the configured window.", parameters=params),
            _definition(name=f"volatility.range_{window}s_bps", family=self.family, config=config, unit="bps",
                        description="Observed high-to-low price range relative to the low over the configured window.", parameters=params),
        )

    def compute(self, context: FeatureContext) -> tuple[ComputedFeature, ...]:
        window = context.config.volatility_window_seconds
        points = _price_points(context, window)
        rets = _returns(points)
        if len(points) < 2 or not rets:
            return ()
        defs = {definition.name: definition for definition in self.definitions(context.config)}
        realized = math.sqrt(sum(value * value for value in rets)) * 10_000
        std = pstdev(rets) * 10_000 if len(rets) >= 2 else 0.0
        prices = [price for price, _ in points]
        range_bps = (max(prices) / min(prices) - 1.0) * 10_000
        sources = _canonical_sources(event for _, event in points)
        return (
            ComputedFeature(defs[f"volatility.realized_{window}s_bps"], realized, sources),
            ComputedFeature(defs[f"volatility.return_std_{window}s_bps"], std, sources),
            ComputedFeature(defs[f"volatility.range_{window}s_bps"], range_bps, sources),
        )


class MomentumCalculator:
    family = FeatureFamily.MOMENTUM

    def definitions(self, config: FeatureEngineConfig) -> tuple[FeatureDefinition, ...]:
        short = config.momentum_short_window_seconds
        long = config.momentum_long_window_seconds
        return (
            _definition(name=f"momentum.return_{short}s_bps", family=self.family, config=config, unit="bps",
                        description="Log price return over the configured short window.",
                        parameters={"window_seconds": short, "price_source": "mark_else_trade"}),
            _definition(name=f"momentum.return_{long}s_bps", family=self.family, config=config, unit="bps",
                        description="Log price return over the configured long window.",
                        parameters={"window_seconds": long, "price_source": "mark_else_trade"}),
            _definition(name=f"momentum.efficiency_{long}s", family=self.family, config=config, unit="ratio",
                        description="Absolute net log return divided by total absolute log-return path length.",
                        parameters={"window_seconds": long, "price_source": "mark_else_trade"}),
        )

    @staticmethod
    def _return_bps(points: tuple[tuple[float, MarketEvent], ...]) -> float:
        return math.log(points[-1][0] / points[0][0]) * 10_000

    def compute(self, context: FeatureContext) -> tuple[ComputedFeature, ...]:
        defs = {definition.name: definition for definition in self.definitions(context.config)}
        short = context.config.momentum_short_window_seconds
        long = context.config.momentum_long_window_seconds
        outputs: list[ComputedFeature] = []
        short_points = _price_points(context, short)
        if len(short_points) >= 2:
            outputs.append(ComputedFeature(
                defs[f"momentum.return_{short}s_bps"], self._return_bps(short_points),
                _canonical_sources(event for _, event in short_points),
            ))
        long_points = _price_points(context, long)
        if len(long_points) >= 2:
            sources = _canonical_sources(event for _, event in long_points)
            outputs.append(ComputedFeature(
                defs[f"momentum.return_{long}s_bps"], self._return_bps(long_points), sources
            ))
            path = sum(abs(value) for value in _returns(long_points))
            net = abs(math.log(long_points[-1][0] / long_points[0][0]))
            efficiency = net / path if path > 0 else 0.0
            outputs.append(ComputedFeature(defs[f"momentum.efficiency_{long}s"], efficiency, sources))
        return tuple(outputs)


class RegimeStateCalculator:
    family = FeatureFamily.REGIME

    def definitions(self, config: FeatureEngineConfig) -> tuple[FeatureDefinition, ...]:
        return (
            _definition(name="regime.state", family=self.family, config=config, unit="category",
                        description="Deterministic descriptive market state; not an alpha-selection signal.",
                        parameters={
                            "wide_spread_bps": config.regime_wide_spread_bps,
                            "high_volatility_bps": config.regime_high_volatility_bps,
                            "low_volatility_bps": config.regime_low_volatility_bps,
                            "trend_return_bps": config.regime_trend_return_bps,
                            "trend_efficiency": config.regime_trend_efficiency,
                        }),
            _definition(name="regime.liquidity_score", family=self.family, config=config, unit="score_0_1",
                        description="Deterministic liquidity score derived from spread relative to the configured stress threshold."),
            _definition(name="regime.stress_score", family=self.family, config=config, unit="score_0_1",
                        description="Deterministic stress score combining spread and realized-volatility threshold utilization."),
        )

    def compute(self, context: FeatureContext) -> tuple[ComputedFeature, ...]:
        cfg = context.config
        spread = context.prior.get("micro.spread_bps")
        vol = context.prior.get(f"volatility.realized_{cfg.volatility_window_seconds}s_bps")
        ret = context.prior.get(f"momentum.return_{cfg.momentum_long_window_seconds}s_bps")
        efficiency = context.prior.get(f"momentum.efficiency_{cfg.momentum_long_window_seconds}s")
        if spread is None and vol is None and ret is None:
            return ()
        spread_value = float(spread.value) if spread is not None else 0.0
        vol_value = float(vol.value) if vol is not None else 0.0
        ret_value = float(ret.value) if ret is not None else 0.0
        efficiency_value = float(efficiency.value) if efficiency is not None else 0.0
        spread_util = spread_value / cfg.regime_wide_spread_bps if cfg.regime_wide_spread_bps > 0 else 0.0
        vol_util = vol_value / cfg.regime_high_volatility_bps if cfg.regime_high_volatility_bps > 0 else 0.0
        liquidity_score = max(0.0, min(1.0, 1.0 - spread_util))
        stress_score = max(0.0, min(1.0, max(spread_util, vol_util)))
        if spread_value >= cfg.regime_wide_spread_bps:
            state = "LIQUIDITY_STRESS"
        elif vol_value >= cfg.regime_high_volatility_bps:
            state = "HIGH_VOLATILITY"
        elif efficiency_value >= cfg.regime_trend_efficiency and abs(ret_value) >= cfg.regime_trend_return_bps:
            state = "TRENDING_UP" if ret_value > 0 else "TRENDING_DOWN"
        elif vol is not None and vol_value <= cfg.regime_low_volatility_bps:
            state = "LOW_VOLATILITY_RANGE"
        else:
            state = "BALANCED"
        dependencies = tuple(feature for feature in (spread, vol, ret, efficiency) if feature is not None)
        sources = _canonical_sources(event for feature in dependencies for event in feature.source_events)
        defs = {definition.name: definition for definition in self.definitions(cfg)}
        return (
            ComputedFeature(defs["regime.state"], state, sources),
            ComputedFeature(defs["regime.liquidity_score"], liquidity_score, sources),
            ComputedFeature(defs["regime.stress_score"], stress_score, sources),
        )

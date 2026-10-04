from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from quant_system.backtest.accounting import Ledger
from quant_system.backtest.events import BarEvent
from quant_system.backtest.interfaces import ExecutionAssumptions
from quant_system.backtest.simulator import ConservativeBarExecutionSimulator
from quant_system.core.enums import OrderType, Side
from quant_system.execution.models import OrderRequest
from quant_system.research.exploratory_sizing_v3 import ExploratorySizingPolicyV3
from quant_system.research.feasibility_archive_v2 import FeasibilityBar, FeasibilityFunding, validate_archive
from quant_system.research.feasibility_basis_archive_v4 import validate_basis_archive
from quant_system.research.feasibility_v3_programme import PhaseResult, ProgrammeResult
from quant_system.research.feasibility_v4_strategies import (
    BasisFeature,
    FundingBasisConfirmedCarry,
    PremiumBasisMeanReversion,
)

ZERO = Decimal("0")
ONE = Decimal("1")
BAR_SIZE = timedelta(minutes=5)
DECISION_EPSILON = timedelta(microseconds=1)
INITIAL_EQUITY = Decimal("10000")


@dataclass(frozen=True, slots=True)
class V4Bar:
    trade: FeasibilityBar
    feature: BasisFeature


@dataclass(frozen=True, slots=True)
class V4Month:
    month: str
    bars: tuple[V4Bar, ...]
    funding: tuple[FeasibilityFunding, ...]


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")


def _utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def load_campaign_v4(path: str | Path) -> dict[str, object]:
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if document.get("schema_id") != "EQS-FEASIBILITY-CRYPTO-CAMPAIGN-V4":
        raise ValueError("unsupported V4 campaign schema")
    campaign = dict(document["campaign"])
    expected = sha256(_canonical(campaign)).hexdigest()
    if expected != document.get("campaign_fingerprint"):
        raise ValueError("campaign fingerprint mismatch")
    if campaign.get("classification") != "EXPLORATORY_NON_EVIDENTIARY":
        raise ValueError("campaign classification invalid")
    if dict(campaign["periods"]).get("locked_oos_access") != "SEALED_NOT_PERMITTED":
        raise PermissionError("locked OOS access is forbidden")
    return document


def _read_bound_raw(entry: dict[str, object]) -> bytes:
    path = Path(str(entry["raw_path"]))
    payload = path.read_bytes()
    if sha256(payload).hexdigest() != str(entry["archive_sha256"]):
        raise RuntimeError(f"raw hash mismatch: {entry.get('series')} {entry.get('month')}")
    return payload


def load_v4_months(v2_root: str | Path, v4_root: str | Path, campaign_id: str) -> tuple[str, str, tuple[V4Month, ...]]:
    if not campaign_id.startswith("FEAS-V4-"):
        raise ValueError("V4 campaign identity invalid")
    v2_manifest_path = Path(v2_root) / "FEAS-BINANCE-BTC-MA-001-acquisition-manifest.json"
    v4_manifest_path = Path(v4_root) / "FEAS-BINANCE-BTC-BASIS-V4-acquisition-manifest.json"
    v2_bytes = v2_manifest_path.read_bytes(); v4_bytes = v4_manifest_path.read_bytes()
    v2_sha = sha256(v2_bytes).hexdigest(); v4_sha = sha256(v4_bytes).hexdigest()
    v2_manifest = json.loads(v2_bytes); v4_manifest = json.loads(v4_bytes)
    if v2_manifest.get("campaign_id") != "FEAS-BINANCE-BTC-MA-001":
        raise RuntimeError("V2 acquisition identity invalid")
    if v4_manifest.get("scope_id") != "FEAS-BINANCE-BTC-BASIS-V4":
        raise RuntimeError("V4 acquisition identity invalid")
    if v4_manifest.get("locked_oos_touched") is not False:
        raise RuntimeError("V4 acquisition touched locked OOS")
    eligible = tuple(str(value) for value in v4_manifest.get("eligible_months", []))
    if len(eligible) != 36:
        raise RuntimeError("V4 eligible month count invalid")

    v2_map = {(str(row["series"]), str(row["month"])): row for row in v2_manifest["receipts"]}
    v4_map = {(str(row["series"]), str(row["month"])): row for row in v4_manifest["receipts"]}
    months: list[V4Month] = []
    for month in eligible:
        kline_entry = v2_map.get(("KLINES_5M", month))
        funding_entry = v2_map.get(("FUNDING_RATE", month))
        if kline_entry is None or funding_entry is None:
            raise RuntimeError(f"V2 base series missing for {month}")
        _, kline_rows = validate_archive(
            _read_bound_raw(kline_entry),
            campaign_id="FEAS-BINANCE-BTC-MA-001",
            series="KLINES_5M",
            month=month,
            source_url=str(kline_entry["source_url"]),
        )
        _, funding_rows = validate_archive(
            _read_bound_raw(funding_entry),
            campaign_id="FEAS-BINANCE-BTC-MA-001",
            series="FUNDING_RATE",
            month=month,
            source_url=str(funding_entry["source_url"]),
        )
        trade_bars = tuple(row for row in kline_rows if isinstance(row, FeasibilityBar))
        funding = tuple(row for row in funding_rows if isinstance(row, FeasibilityFunding))
        basis_rows: dict[str, tuple[FeasibilityBar, ...]] = {}
        for series in ("MARK_PRICE_5M", "INDEX_PRICE_5M", "PREMIUM_INDEX_5M"):
            entry = v4_map.get((series, month))
            if entry is None:
                raise RuntimeError(f"V4 basis series missing: {series} {month}")
            receipt, rows = validate_basis_archive(
                _read_bound_raw(entry),
                scope_id="FEAS-BINANCE-BTC-BASIS-V4",
                series=series,
                month=month,
                source_url=str(entry["source_url"]),
            )
            if receipt.receipt_sha256 != str(entry["receipt_sha256"]):
                raise RuntimeError(f"V4 receipt binding mismatch: {series} {month}")
            basis_rows[series] = rows
        if not (
            len(trade_bars)
            == len(basis_rows["MARK_PRICE_5M"])
            == len(basis_rows["INDEX_PRICE_5M"])
            == len(basis_rows["PREMIUM_INDEX_5M"])
        ):
            raise RuntimeError(f"V4 aligned row count mismatch: {month}")
        combined: list[V4Bar] = []
        for trade, mark, index, premium in zip(
            trade_bars,
            basis_rows["MARK_PRICE_5M"],
            basis_rows["INDEX_PRICE_5M"],
            basis_rows["PREMIUM_INDEX_5M"],
        ):
            if not (trade.open_time == mark.open_time == index.open_time == premium.open_time):
                raise RuntimeError(f"V4 timestamp alignment mismatch: {month}")
            combined.append(
                V4Bar(
                    trade=trade,
                    feature=BasisFeature(
                        premium_close=premium.close,
                        mark_close=mark.close,
                        index_close=index.close,
                    ),
                )
            )
        months.append(V4Month(month=month, bars=tuple(combined), funding=funding))
    return v2_sha, v4_sha, tuple(months)


def _strategy(campaign: dict[str, object]):
    config = dict(campaign["strategy"])
    family = str(config["family"])
    if family == "PREMIUM_BASIS_MEAN_REVERSION":
        return PremiumBasisMeanReversion(
            lookback_bars=int(config["premium_lookback_bars"]),
            entry_zscore=Decimal(str(config["entry_zscore"])),
            exit_zscore=Decimal(str(config["exit_zscore"])),
            max_holding_bars=int(config["max_holding_bars"]),
        )
    if family == "FUNDING_BASIS_CONFIRMED_CARRY":
        return FundingBasisConfirmedCarry(
            minimum_same_sign_funding_prints=int(config["minimum_same_sign_funding_prints"]),
            max_holding_bars=int(config["max_holding_bars"]),
        )
    raise ValueError(f"unsupported V4 family: {family}")


def _phase_segments(months: tuple[V4Month, ...], start: datetime, end: datetime) -> tuple[tuple[V4Month, ...], ...]:
    selected = tuple(month for month in months if month.bars and start <= month.bars[0].trade.open_time <= end)
    if not selected:
        raise RuntimeError("phase contains no eligible V4 months")
    segments: list[list[V4Month]] = []
    current: list[V4Month] = []
    previous_last: datetime | None = None
    for month in selected:
        first = month.bars[0].trade.open_time
        if previous_last is not None and first - previous_last != BAR_SIZE:
            if current:
                segments.append(current)
            current = []
        current.append(month)
        previous_last = month.bars[-1].trade.open_time
    if current:
        segments.append(current)
    return tuple(tuple(segment) for segment in segments)


def _run_phase(
    *,
    phase: str,
    segments: tuple[tuple[V4Month, ...], ...],
    campaign: dict[str, object],
    sizing: ExploratorySizingPolicyV3,
) -> PhaseResult:
    execution = dict(campaign["execution_model"])
    assumptions = ExecutionAssumptions(
        commission_bps=Decimal(str(execution["fee_bps_per_side"])),
        spread_bps=ZERO,
        slippage_bps=Decimal(str(execution["adverse_slippage_bps_per_side"])),
        impact_bps=ZERO,
        financing_bps_annual=ZERO,
        borrow_bps_annual=ZERO,
        latency_ms=0,
        partial_fill_fraction=ONE,
    )
    simulator = ConservativeBarExecutionSimulator(assumptions, max_volume_participation=Decimal("0.01"))
    ledger = Ledger.with_cash(INITIAL_EQUITY)
    strategy_id = uuid5(NAMESPACE_URL, f"{campaign['campaign_id']}:{phase}")
    funding_total = ZERO
    funding_sum_check = ZERO
    prior_realized = ZERO
    completed_trades = 0
    gross_positive_trade_pnl = ZERO
    peak_equity = INITIAL_EQUITY
    maximum_drawdown = ZERO
    maximum_leverage = ZERO
    maximum_entry_fraction = ZERO
    blockers: list[str] = []
    cutoff = int(execution["entry_cutoff_bars_before_segment_end"])
    total_bars = 0
    total_funding = 0

    for segment_index, segment in enumerate(segments):
        strategy = _strategy(campaign)
        segment_bars = tuple(bar for month in segment for bar in month.bars)
        segment_funding = tuple(sorted((row for month in segment for row in month.funding), key=lambda item: item.funding_time))
        total_bars += len(segment_bars); total_funding += len(segment_funding)
        funding_index = 0
        previous_close: Decimal | None = None
        open_order: OrderRequest | None = None
        if ledger.position_state("BTCUSDT").quantity != ZERO:
            blockers.append(f"POSITION_NOT_FLAT_AT_SEGMENT_START:{segment_index}")

        for index, row in enumerate(segment_bars):
            bar = row.trade
            while funding_index < len(segment_funding) and segment_funding[funding_index].funding_time <= bar.open_time:
                item = segment_funding[funding_index]
                if hasattr(strategy, "on_funding"):
                    strategy.on_funding(item.funding_rate)
                if previous_close is not None:
                    position = ledger.position_state("BTCUSDT")
                    if position.quantity != ZERO:
                        cashflow = -(position.quantity * previous_close * item.funding_rate)
                        funding_total += cashflow; funding_sum_check += cashflow
                funding_index += 1

            market_bar = BarEvent(symbol="BTCUSDT", timestamp=bar.open_time, open=bar.open, high=bar.high, low=bar.low, close=bar.close, volume=bar.volume)
            for order, fill in simulator.on_bar(market_bar):
                ledger.apply(fill, order.side)
                position = ledger.position_state("BTCUSDT")
                if position.quantity == ZERO:
                    increment = position.realized_pnl - prior_realized
                    prior_realized = position.realized_pnl
                    completed_trades += 1
                    if increment > ZERO:
                        gross_positive_trade_pnl += increment
                open_order = None

            snapshot = ledger.snapshot({"BTCUSDT": bar.close})
            adjusted_equity = snapshot.equity + funding_total
            peak_equity = max(peak_equity, adjusted_equity)
            if peak_equity > ZERO:
                maximum_drawdown = max(maximum_drawdown, max(ZERO, (peak_equity - adjusted_equity) / peak_equity))
            maximum_leverage = max(maximum_leverage, snapshot.gross_notional / INITIAL_EQUITY)

            position = ledger.position_state("BTCUSDT")
            remaining = len(segment_bars) - index - 1
            target = None; reason = None
            if open_order is None:
                if remaining < cutoff:
                    if position.quantity != ZERO:
                        target, reason = 0, "SEGMENT_END_FLATTEN"
                else:
                    signal = strategy.observe(row.feature, position.quantity)
                    if signal is not None:
                        target, reason, _metrics = signal
            if target is not None:
                current = 1 if position.quantity > ZERO else -1 if position.quantity < ZERO else 0
                if target != current:
                    if current == 0 and target != 0:
                        side = Side.BUY if target > 0 else Side.SELL
                        quantity = sizing.entry_quantity(bar.close)
                        sizing.assert_entry_within_policy(quantity=quantity, reference_price=bar.close)
                        maximum_entry_fraction = max(maximum_entry_fraction, (quantity * bar.close) / INITIAL_EQUITY)
                        reduce_only = False
                    else:
                        side = Side.SELL if current > 0 else Side.BUY
                        quantity = abs(position.quantity); reduce_only = True
                    order = OrderRequest(
                        strategy_id=strategy_id,
                        symbol="BTCUSDT",
                        side=side,
                        quantity=quantity,
                        order_type=OrderType.MARKET,
                        decision_time=bar.open_time + BAR_SIZE - DECISION_EPSILON,
                        reference_price=bar.close,
                        limit_price=None,
                        reduce_only=reduce_only,
                        order_id=uuid5(NAMESPACE_URL, f"{campaign['campaign_id']}:{phase}:{segment_index}:{index}:{side.value}:{reason}"),
                    )
                    simulator.submit(order); open_order = order
            previous_close = bar.close

        if ledger.position_state("BTCUSDT").quantity != ZERO:
            blockers.append(f"FINAL_POSITION_NOT_FLAT_SEGMENT:{segment_index}")
        if simulator.pending_count:
            blockers.append(f"PENDING_ORDER_AT_SEGMENT_END:{segment_index}")

    final_mark = segments[-1][-1].bars[-1].trade.close
    final_position = ledger.position_state("BTCUSDT")
    final = ledger.snapshot({"BTCUSDT": final_mark})
    adjusted_final = final.equity + funding_total
    if not ledger.reconcile().ok:
        blockers.append("CORE_JOURNAL_RECONCILIATION_FAILED")
    funding_reconciled = funding_sum_check == funding_total
    if not funding_reconciled:
        blockers.append("FUNDING_JOURNAL_RECONCILIATION_FAILED")
    return PhaseResult(
        phase=phase,
        bar_count=total_bars,
        funding_record_count=total_funding,
        completed_trades=completed_trades,
        net_pnl_after_costs=adjusted_final - INITIAL_EQUITY,
        final_core_equity=final.equity,
        final_adjusted_equity=adjusted_final,
        realized_pnl=final.realized_pnl,
        commissions=final.commissions,
        funding_cashflow=funding_total,
        gross_positive_trade_pnl=gross_positive_trade_pnl,
        maximum_drawdown_fraction=maximum_drawdown,
        maximum_observed_gross_leverage=maximum_leverage,
        maximum_entry_notional_fraction=maximum_entry_fraction,
        journal_reconciled=ledger.reconcile().ok,
        funding_journal_reconciled=funding_reconciled,
        final_position_quantity=final_position.quantity,
        pending_order_count=simulator.pending_count,
        blockers=tuple(sorted(set(blockers))),
    )


def run_v4_campaign(*, campaign_record: dict[str, object], months: tuple[V4Month, ...], trial_id: str) -> ProgrammeResult:
    campaign = dict(campaign_record["campaign"])
    periods = dict(campaign["periods"])
    if periods.get("locked_oos_access") != "SEALED_NOT_PERMITTED":
        raise PermissionError("locked OOS access is forbidden")
    coverage = dict(campaign["coverage"])
    if coverage.get("imputation_permitted") is not False or coverage.get("partial_month_use_permitted") is not False:
        raise PermissionError("V4 coverage policy must remain fail-closed")
    sizing_cfg = dict(campaign["sizing"])
    sizing = ExploratorySizingPolicyV3(
        starting_equity=Decimal(str(sizing_cfg["starting_equity"])),
        target_position_notional_fraction=Decimal(str(sizing_cfg["target_position_notional_fraction"])),
        max_gross_leverage=Decimal(str(sizing_cfg["max_gross_leverage"])),
        quantity_step=Decimal(str(sizing_cfg["quantity_step"])),
    )
    training_segments = _phase_segments(months, _utc(str(periods["training_start"])), _utc(str(periods["training_end"])))
    validation_segments = _phase_segments(months, _utc(str(periods["validation_start"])), _utc(str(periods["validation_end"])))
    training = _run_phase(phase="TRAINING", segments=training_segments, campaign=campaign, sizing=sizing)
    validation = _run_phase(phase="VALIDATION", segments=validation_segments, campaign=campaign, sizing=sizing)
    gross_profit = training.gross_positive_trade_pnl + validation.gross_positive_trade_pnl
    commissions = training.commissions + validation.commissions
    commission_share = commissions / gross_profit if gross_profit > ZERO else Decimal("999")
    blockers = tuple(sorted(set(training.blockers + validation.blockers)))
    return ProgrammeResult(
        schema_id="EQS-FEASIBILITY-V4-PROGRAMME-RESULT-V1",
        campaign_id=str(campaign["campaign_id"]),
        campaign_fingerprint=str(campaign_record["campaign_fingerprint"]),
        campaign_version=str(campaign["version"]),
        trial_id=trial_id,
        classification="EXPLORATORY_NON_EVIDENTIARY",
        training_completed_trades=training.completed_trades,
        validation_completed_trades=validation.completed_trades,
        training_net_pnl_after_costs=training.net_pnl_after_costs,
        validation_net_pnl_after_costs=validation.net_pnl_after_costs,
        whole_window_net_pnl_after_costs=training.net_pnl_after_costs + validation.net_pnl_after_costs,
        maximum_drawdown_fraction=max(training.maximum_drawdown_fraction, validation.maximum_drawdown_fraction),
        commission_cost_share_of_gross_profit_fraction=commission_share,
        maximum_observed_gross_leverage=max(training.maximum_observed_gross_leverage, validation.maximum_observed_gross_leverage),
        maximum_entry_notional_fraction=max(training.maximum_entry_notional_fraction, validation.maximum_entry_notional_fraction),
        core_journal_reconciled=training.journal_reconciled and validation.journal_reconciled,
        funding_journal_reconciled=training.funding_journal_reconciled and validation.funding_journal_reconciled,
        final_position_quantity=validation.final_position_quantity,
        pending_order_count=validation.pending_order_count,
        shakedown_eligible_non_qualifying=not blockers,
        blockers=blockers,
        locked_oos_opened=False,
        broker_submission_enabled=False,
        live_authority=False,
        r13_certification_authority=False,
        j25_j26_candidate_authority=False,
        counts_toward_168h_100_trade_gate=False,
        training=training,
        validation=validation,
    )

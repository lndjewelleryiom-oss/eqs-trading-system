from __future__ import annotations

from dataclasses import asdict, dataclass
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
from quant_system.research.feasibility_v3_strategies import (
    FundingSignCarry,
    VolatilityCompressionBreakout,
    VolatilityNormalizedMeanReversion,
)

ZERO = Decimal("0")
ONE = Decimal("1")
BAR_SIZE = timedelta(minutes=5)
DECISION_EPSILON = timedelta(microseconds=1)
INITIAL_EQUITY = Decimal("10000")


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")


def _utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@dataclass(frozen=True, slots=True)
class PhaseResult:
    phase: str
    bar_count: int
    funding_record_count: int
    completed_trades: int
    net_pnl_after_costs: Decimal
    final_core_equity: Decimal
    final_adjusted_equity: Decimal
    realized_pnl: Decimal
    commissions: Decimal
    funding_cashflow: Decimal
    gross_positive_trade_pnl: Decimal
    maximum_drawdown_fraction: Decimal
    maximum_observed_gross_leverage: Decimal
    maximum_entry_notional_fraction: Decimal
    journal_reconciled: bool
    funding_journal_reconciled: bool
    final_position_quantity: Decimal
    pending_order_count: int
    blockers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProgrammeResult:
    schema_id: str
    campaign_id: str
    campaign_fingerprint: str
    campaign_version: str
    trial_id: str
    classification: str
    training_completed_trades: int
    validation_completed_trades: int
    training_net_pnl_after_costs: Decimal
    validation_net_pnl_after_costs: Decimal
    whole_window_net_pnl_after_costs: Decimal
    maximum_drawdown_fraction: Decimal
    commission_cost_share_of_gross_profit_fraction: Decimal
    maximum_observed_gross_leverage: Decimal
    maximum_entry_notional_fraction: Decimal
    core_journal_reconciled: bool
    funding_journal_reconciled: bool
    final_position_quantity: Decimal
    pending_order_count: int
    shakedown_eligible_non_qualifying: bool
    blockers: tuple[str, ...]
    locked_oos_opened: bool
    broker_submission_enabled: bool
    live_authority: bool
    r13_certification_authority: bool
    j25_j26_candidate_authority: bool
    counts_toward_168h_100_trade_gate: bool
    training: PhaseResult
    validation: PhaseResult

    def to_record(self) -> dict[str, object]:
        def convert(value):
            if isinstance(value, Decimal):
                return str(value)
            if isinstance(value, tuple):
                return [convert(item) for item in value]
            if hasattr(value, "__dataclass_fields__"):
                return {key: convert(item) for key, item in asdict(value).items()}
            if isinstance(value, dict):
                return {key: convert(item) for key, item in value.items()}
            return value

        record = {key: convert(value) for key, value in asdict(self).items()}
        record["result_sha256"] = sha256(_canonical(record)).hexdigest()
        return record


def load_campaign(path: str | Path) -> dict[str, object]:
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if document.get("schema_id") != "EQS-FEASIBILITY-CRYPTO-CAMPAIGN-V3":
        raise ValueError("unsupported V3 campaign schema")
    campaign = dict(document["campaign"])
    expected = sha256(_canonical(campaign)).hexdigest()
    if expected != document.get("campaign_fingerprint"):
        raise ValueError("campaign fingerprint mismatch")
    if campaign.get("classification") != "EXPLORATORY_NON_EVIDENTIARY":
        raise ValueError("campaign classification invalid")
    if dict(campaign["periods"]).get("locked_oos_access") != "SEALED_NOT_PERMITTED":
        raise ValueError("locked OOS boundary invalid")
    return document


def load_archive(acquisition_root: str | Path, campaign_id: str) -> tuple[str, tuple[FeasibilityBar, ...], tuple[FeasibilityFunding, ...]]:
    root = Path(acquisition_root)
    manifest_path = root / "FEAS-BINANCE-BTC-MA-001-acquisition-manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest_sha = sha256(manifest_bytes).hexdigest()
    manifest = json.loads(manifest_bytes)
    receipts = list(manifest.get("receipts", []))
    if len(receipts) != 78:
        raise RuntimeError("expected exactly 78 admitted feasibility archive objects")
    bars: list[FeasibilityBar] = []
    funding: list[FeasibilityFunding] = []
    for entry in receipts:
        raw_path = Path(str(entry["raw_path"]))
        payload = raw_path.read_bytes()
        if sha256(payload).hexdigest() != str(entry["archive_sha256"]):
            raise RuntimeError("archive hash mismatch")
        receipt, rows = validate_archive(
            payload,
            campaign_id=campaign_id,
            series=str(entry["series"]),
            month=str(entry["month"]),
            source_url=str(entry["source_url"]),
        )
        if receipt.archive_sha256 != str(entry["archive_sha256"]):
            raise RuntimeError("revalidated archive hash mismatch")
        if receipt.receipt_sha256 != str(entry["receipt_sha256"]):
            raise RuntimeError("revalidated receipt hash mismatch")
        if entry["series"] == "KLINES_5M":
            bars.extend(item for item in rows if isinstance(item, FeasibilityBar))
        elif entry["series"] == "FUNDING_RATE":
            funding.extend(item for item in rows if isinstance(item, FeasibilityFunding))
    ordered_bars = tuple(sorted(bars, key=lambda item: item.open_time))
    ordered_funding = tuple(sorted(funding, key=lambda item: item.funding_time))
    return manifest_sha, ordered_bars, ordered_funding


def _strategy(campaign: dict[str, object]):
    cfg = dict(campaign["strategy"])
    family = str(cfg["family"])
    if family == "VOLATILITY_NORMALIZED_MEAN_REVERSION":
        return VolatilityNormalizedMeanReversion(
            lookback_bars=int(cfg["lookback_bars"]),
            entry_zscore=Decimal(str(cfg["entry_zscore"])),
            exit_zscore=Decimal(str(cfg["exit_zscore"])),
            max_holding_bars=int(cfg["max_holding_bars"]),
        )
    if family == "VOLATILITY_COMPRESSION_BREAKOUT":
        return VolatilityCompressionBreakout(
            fast_vol_bars=int(cfg["fast_vol_bars"]),
            slow_vol_bars=int(cfg["slow_vol_bars"]),
            compression_ratio_max=Decimal(str(cfg["compression_ratio_max"])),
            breakout_lookback_bars=int(cfg["breakout_lookback_bars"]),
            exit_lookback_bars=int(cfg["exit_lookback_bars"]),
            max_holding_bars=int(cfg["max_holding_bars"]),
        )
    if family == "FUNDING_SIGN_CARRY":
        return FundingSignCarry(
            minimum_same_sign_prints=int(cfg["minimum_same_sign_prints"]),
            max_holding_bars=int(cfg["max_holding_bars"]),
        )
    raise ValueError(f"unsupported V3 strategy family: {family}")


def _slice_phase(
    bars: tuple[FeasibilityBar, ...],
    funding: tuple[FeasibilityFunding, ...],
    start: datetime,
    end: datetime,
) -> tuple[tuple[FeasibilityBar, ...], tuple[FeasibilityFunding, ...]]:
    phase_bars = tuple(item for item in bars if start <= item.open_time <= end)
    phase_funding = tuple(item for item in funding if start <= item.funding_time <= end)
    if not phase_bars:
        raise RuntimeError("phase contains no bars")
    previous = None
    for item in phase_bars:
        if previous is not None and item.open_time - previous != BAR_SIZE:
            raise RuntimeError("phase bar series is not contiguous")
        previous = item.open_time
    return phase_bars, phase_funding


def _run_phase(
    *,
    phase: str,
    bars: tuple[FeasibilityBar, ...],
    funding: tuple[FeasibilityFunding, ...],
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
    strategy = _strategy(campaign)
    strategy_id = uuid5(NAMESPACE_URL, f"{campaign['campaign_id']}:{phase}")
    funding_index = 0
    funding_total = ZERO
    funding_sum_check = ZERO
    open_order: OrderRequest | None = None
    previous_close: Decimal | None = None
    prior_realized = ZERO
    completed_trades = 0
    gross_positive_trade_pnl = ZERO
    peak_equity = INITIAL_EQUITY
    maximum_drawdown = ZERO
    maximum_leverage = ZERO
    maximum_entry_fraction = ZERO
    cutoff = int(execution["entry_cutoff_bars_before_window_end"])

    for index, bar in enumerate(bars):
        while funding_index < len(funding) and funding[funding_index].funding_time <= bar.open_time:
            item = funding[funding_index]
            if hasattr(strategy, "on_funding"):
                strategy.on_funding(item.funding_rate)
            if previous_close is not None:
                position = ledger.position_state("BTCUSDT")
                if position.quantity != ZERO:
                    cashflow = -(position.quantity * previous_close * item.funding_rate)
                    funding_total += cashflow
                    funding_sum_check += cashflow
            funding_index += 1

        market_bar = BarEvent(
            symbol="BTCUSDT", timestamp=bar.open_time, open=bar.open, high=bar.high,
            low=bar.low, close=bar.close, volume=bar.volume,
        )
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
            drawdown = max(ZERO, (peak_equity - adjusted_equity) / peak_equity)
            maximum_drawdown = max(maximum_drawdown, drawdown)
        leverage = snapshot.gross_notional / INITIAL_EQUITY
        maximum_leverage = max(maximum_leverage, leverage)

        position = ledger.position_state("BTCUSDT")
        remaining = len(bars) - index - 1
        target = None
        signal_reason = None
        if open_order is None:
            if remaining < cutoff:
                if position.quantity != ZERO:
                    target = 0
                    signal_reason = "PHASE_END_FLATTEN"
            else:
                signal = strategy.observe(bar.close, position.quantity)
                if signal is not None:
                    target, signal_reason, _metrics = signal

        if target is not None:
            current = 1 if position.quantity > ZERO else -1 if position.quantity < ZERO else 0
            if target != current:
                if current == 0 and target != 0:
                    side = Side.BUY if target > 0 else Side.SELL
                    quantity = sizing.entry_quantity(bar.close)
                    sizing.assert_entry_within_policy(quantity=quantity, reference_price=bar.close)
                    entry_fraction = (quantity * bar.close) / INITIAL_EQUITY
                    maximum_entry_fraction = max(maximum_entry_fraction, entry_fraction)
                    reduce_only = False
                else:
                    side = Side.SELL if current > 0 else Side.BUY
                    quantity = abs(position.quantity)
                    reduce_only = True
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
                    order_id=uuid5(NAMESPACE_URL, f"{campaign['campaign_id']}:{phase}:{index}:{side.value}:{signal_reason}"),
                )
                simulator.submit(order)
                open_order = order
        previous_close = bar.close

    final_position = ledger.position_state("BTCUSDT")
    final = ledger.snapshot({"BTCUSDT": bars[-1].close})
    adjusted_final = final.equity + funding_total
    blockers: list[str] = []
    if final_position.quantity != ZERO:
        blockers.append("FINAL_POSITION_NOT_FLAT")
    if simulator.pending_count:
        blockers.append("PENDING_ORDER_AT_END")
    if not ledger.reconcile().ok:
        blockers.append("CORE_JOURNAL_RECONCILIATION_FAILED")
    funding_reconciled = funding_sum_check == funding_total
    if not funding_reconciled:
        blockers.append("FUNDING_JOURNAL_RECONCILIATION_FAILED")

    return PhaseResult(
        phase=phase,
        bar_count=len(bars),
        funding_record_count=len(funding),
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


def run_v3_campaign(
    *,
    campaign_record: dict[str, object],
    bars: tuple[FeasibilityBar, ...],
    funding: tuple[FeasibilityFunding, ...],
    trial_id: str,
) -> ProgrammeResult:
    campaign = dict(campaign_record["campaign"])
    periods = dict(campaign["periods"])
    if periods.get("locked_oos_access") != "SEALED_NOT_PERMITTED":
        raise PermissionError("locked OOS access is forbidden")
    sizing_cfg = dict(campaign["sizing"])
    sizing = ExploratorySizingPolicyV3(
        starting_equity=Decimal(str(sizing_cfg["starting_equity"])),
        target_position_notional_fraction=Decimal(str(sizing_cfg["target_position_notional_fraction"])),
        max_gross_leverage=Decimal(str(sizing_cfg["max_gross_leverage"])),
        quantity_step=Decimal(str(sizing_cfg["quantity_step"])),
    )
    training_bars, training_funding = _slice_phase(
        bars, funding, _utc(str(periods["training_start"])), _utc(str(periods["training_end"])),
    )
    validation_bars, validation_funding = _slice_phase(
        bars, funding, _utc(str(periods["validation_start"])), _utc(str(periods["validation_end"])),
    )
    training = _run_phase(
        phase="TRAINING", bars=training_bars, funding=training_funding, campaign=campaign, sizing=sizing,
    )
    validation = _run_phase(
        phase="VALIDATION", bars=validation_bars, funding=validation_funding, campaign=campaign, sizing=sizing,
    )
    total_gross_profit = training.gross_positive_trade_pnl + validation.gross_positive_trade_pnl
    total_commissions = training.commissions + validation.commissions
    commission_share = total_commissions / total_gross_profit if total_gross_profit > ZERO else Decimal("999")
    blockers = tuple(sorted(set(training.blockers + validation.blockers)))
    mechanics_ok = not blockers
    return ProgrammeResult(
        schema_id="EQS-FEASIBILITY-V3-PROGRAMME-RESULT-V1",
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
        shakedown_eligible_non_qualifying=mechanics_ok,
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

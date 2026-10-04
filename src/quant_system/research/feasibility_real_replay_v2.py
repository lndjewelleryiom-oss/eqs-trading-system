from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
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
from quant_system.research.feasibility_archive_v2 import (
    FeasibilityBar,
    FeasibilityFunding,
    validate_archive,
)
from quant_system.research.feasibility_strategy_v2 import FixedCloseChannelBreakout, FixedFundingPersistenceContrarian, FixedSmaCrossover
from quant_system.research.feasibility_v2 import load_and_validate


ZERO = Decimal("0")
ONE = Decimal("1")
BAR_SIZE = timedelta(minutes=5)
DECISION_EPSILON = timedelta(microseconds=1)


@dataclass(frozen=True, slots=True)
class FundingCashflow:
    funding_time: datetime
    funding_rate: Decimal
    proxy_price: Decimal
    position_quantity: Decimal
    cashflow: Decimal


@dataclass(frozen=True, slots=True)
class ClosedTrade:
    closed_at: datetime
    phase: str
    realized_pnl_increment: Decimal
    cumulative_realized_pnl: Decimal


@dataclass(frozen=True, slots=True)
class FeasibilityReplayResult:
    schema_id: str
    campaign_id: str
    campaign_fingerprint: str
    campaign_version: str
    classification: str
    bar_count: int
    funding_record_count: int
    training_completed_trades: int
    validation_completed_trades: int
    total_completed_trades: int
    final_core_equity: Decimal
    final_adjusted_equity: Decimal
    core_realized_pnl: Decimal
    commissions: Decimal
    funding_cashflow: Decimal
    core_journal_reconciled: bool
    funding_journal_reconciled: bool
    final_position_quantity: Decimal
    pending_order_count: int
    shakedown_eligible_non_qualifying: bool
    blockers: tuple[str, ...]
    broker_submission_enabled: bool
    live_authority: bool
    locked_oos_opened: bool
    counts_toward_168h_100_trade_gate: bool
    r13_certification_authority: bool
    j25_j26_candidate_authority: bool
    trades: tuple[ClosedTrade, ...]
    funding_journal: tuple[FundingCashflow, ...]

    def to_record(self) -> dict[str, object]:
        def convert(value):
            if isinstance(value, Decimal):
                return str(value)
            if isinstance(value, datetime):
                return value.isoformat()
            if isinstance(value, tuple):
                return [convert(item) for item in value]
            if hasattr(value, "__dataclass_fields__"):
                return {key: convert(item) for key, item in asdict(value).items()}
            if isinstance(value, dict):
                return {key: convert(item) for key, item in value.items()}
            return value

        record = {key: convert(value) for key, value in asdict(self).items()}
        record["result_sha256"] = sha256(
            json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        return record


def _phase(timestamp: datetime, campaign: dict[str, object]) -> str:
    periods = dict(campaign["periods"])
    training_start = datetime.fromisoformat(str(periods["training_start"]).replace("Z", "+00:00"))
    training_end = datetime.fromisoformat(str(periods["training_end"]).replace("Z", "+00:00"))
    validation_start = datetime.fromisoformat(str(periods["validation_start"]).replace("Z", "+00:00"))
    validation_end = datetime.fromisoformat(str(periods["validation_end"]).replace("Z", "+00:00"))
    if validation_start <= timestamp <= validation_end + BAR_SIZE:
        return "VALIDATION"
    if training_start <= timestamp < validation_start:
        return "TRAINING"
    return "OUT_OF_SCOPE"


def _validate_series(
    bars: tuple[FeasibilityBar, ...],
    funding: tuple[FeasibilityFunding, ...],
    campaign: dict[str, object],
) -> None:
    if not bars:
        raise ValueError("replay requires bars")
    periods = dict(campaign["periods"])
    start = datetime.fromisoformat(str(periods["training_start"]).replace("Z", "+00:00"))
    validation_end = datetime.fromisoformat(str(periods["validation_end"]).replace("Z", "+00:00"))
    previous = None
    for bar in bars:
        if bar.open_time < start or bar.open_time > validation_end:
            raise ValueError("bar lies outside train/validation scope")
        if previous is not None and bar.open_time - previous != BAR_SIZE:
            raise ValueError("bar series must be strictly contiguous at 5-minute cadence")
        previous = bar.open_time
    previous_funding = None
    for item in funding:
        if item.funding_time < start or item.funding_time > validation_end:
            raise ValueError("funding record lies outside train/validation scope")
        if previous_funding is not None and item.funding_time <= previous_funding:
            raise ValueError("funding records must be strictly increasing")
        previous_funding = item.funding_time


def run_fixed_campaign_replay(
    *,
    bars: tuple[FeasibilityBar, ...],
    funding: tuple[FeasibilityFunding, ...],
    campaign_record: dict[str, object],
) -> FeasibilityReplayResult:
    campaign = dict(campaign_record["campaign"])
    _validate_series(bars, funding, campaign)
    strategy_cfg = dict(campaign["strategy"])
    execution = dict(campaign["execution_model"])
    if execution["decision_timestamp_convention"] != "BAR_END_MINUS_1_MICROSECOND_FOR_NEXT_BAR_ELIGIBILITY":
        raise ValueError("unsupported decision timestamp convention")
    if execution["funding_event_ordering"] != "SETTLE_BEFORE_SAME_TIMESTAMP_NEXT_BAR_FILL":
        raise ValueError("unsupported funding event ordering")
    if execution["funding_price_proxy"] != "MOST_RECENT_CLOSED_5M_CLOSE":
        raise ValueError("unsupported funding proxy")

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
    ledger = Ledger.with_cash(Decimal("10000"))
    family = str(strategy_cfg["family"])
    if family == "SIMPLE_MOVING_AVERAGE_CROSSOVER":
        strategy = FixedSmaCrossover(
            fast_bars=int(strategy_cfg["fast_bars"]),
            slow_bars=int(strategy_cfg["slow_bars"]),
            max_holding_bars=int(strategy_cfg["max_holding_bars"]),
        )
    elif family == "CLOSE_CHANNEL_BREAKOUT":
        strategy = FixedCloseChannelBreakout(
            entry_lookback_bars=int(strategy_cfg["entry_lookback_bars"]),
            exit_lookback_bars=int(strategy_cfg["exit_lookback_bars"]),
            max_holding_bars=int(strategy_cfg["max_holding_bars"]),
        )
    elif family == "FUNDING_PERSISTENCE_CONTRARIAN":
        strategy = FixedFundingPersistenceContrarian(
            absolute_funding_entry_threshold=Decimal(str(strategy_cfg["absolute_funding_entry_threshold"])),
            holding_bars=int(strategy_cfg["holding_bars"]),
        )
    else:
        raise ValueError(f"unsupported feasibility strategy family: {family}")
    strategy_id = uuid5(NAMESPACE_URL, str(campaign["campaign_id"]))
    cutoff = int(execution["entry_cutoff_bars_before_window_end"])
    funding_index = 0
    funding_total = ZERO
    funding_journal: list[FundingCashflow] = []
    trades: list[ClosedTrade] = []
    open_order: OrderRequest | None = None
    previous_close: Decimal | None = None
    trade_entry_realized = ZERO

    for index, bar in enumerate(bars):
        # Funding settles on the position that existed before a same-timestamp next-bar fill.
        while funding_index < len(funding) and funding[funding_index].funding_time <= bar.open_time:
            item = funding[funding_index]
            if hasattr(strategy, "on_funding"):
                strategy.on_funding(item.funding_rate)
            if previous_close is not None:
                position = ledger.position_state("BTCUSDT")
                if position.quantity != ZERO:
                    cashflow = -(position.quantity * previous_close * item.funding_rate)
                    funding_total += cashflow
                    funding_journal.append(
                        FundingCashflow(
                            funding_time=item.funding_time,
                            funding_rate=item.funding_rate,
                            proxy_price=previous_close,
                            position_quantity=position.quantity,
                            cashflow=cashflow,
                        )
                    )
            funding_index += 1

        market_bar = BarEvent(
            symbol="BTCUSDT",
            timestamp=bar.open_time,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
        )
        for order, fill in simulator.on_bar(market_bar):
            ledger.apply(fill, order.side)
            position = ledger.position_state("BTCUSDT")
            if position.quantity == ZERO:
                increment = position.realized_pnl - trade_entry_realized
                trade_entry_realized = position.realized_pnl
                trades.append(
                    ClosedTrade(
                        closed_at=fill.timestamp,
                        phase=_phase(fill.timestamp, campaign),
                        realized_pnl_increment=increment,
                        cumulative_realized_pnl=position.realized_pnl,
                    )
                )
            open_order = None

        position = ledger.position_state("BTCUSDT")
        remaining_after_this_bar = len(bars) - index - 1
        target: int | None = None
        if open_order is None:
            if remaining_after_this_bar < cutoff:
                if position.quantity != ZERO:
                    target = 0
            else:
                signal = strategy.observe(bar.close, position.quantity)
                if signal is not None:
                    target = int(signal[0])

        if target is not None:
            current = 1 if position.quantity > ZERO else -1 if position.quantity < ZERO else 0
            if target != current:
                if current == 0 and target != 0:
                    side = Side.BUY if target > 0 else Side.SELL
                    quantity = ONE
                    reduce_only = False
                elif current != 0 and target == 0:
                    side = Side.SELL if current > 0 else Side.BUY
                    quantity = abs(position.quantity)
                    reduce_only = True
                else:
                    side = Side.SELL if current > 0 else Side.BUY
                    quantity = abs(position.quantity)
                    reduce_only = True
                decision_time = bar.open_time + BAR_SIZE - DECISION_EPSILON
                order = OrderRequest(
                    strategy_id=strategy_id,
                    symbol="BTCUSDT",
                    side=side,
                    quantity=quantity,
                    order_type=OrderType.MARKET,
                    decision_time=decision_time,
                    reference_price=bar.close,
                    limit_price=None,
                    reduce_only=reduce_only,
                    order_id=uuid5(NAMESPACE_URL, f"{campaign['campaign_id']}:{index}:{side.value}:{target}"),
                )
                simulator.submit(order)
                open_order = order
        previous_close = bar.close

    final_position = ledger.position_state("BTCUSDT")
    core = ledger.snapshot({"BTCUSDT": bars[-1].close})
    funding_reconciled = sum((item.cashflow for item in funding_journal), ZERO) == funding_total
    validation_trades = sum(item.phase == "VALIDATION" for item in trades)
    training_trades = sum(item.phase == "TRAINING" for item in trades)
    blockers: list[str] = []
    if final_position.quantity != ZERO:
        blockers.append("FINAL_POSITION_NOT_FLAT")
    if simulator.pending_count:
        blockers.append("PENDING_ORDER_AT_END")
    if not ledger.reconcile().ok:
        blockers.append("CORE_LEDGER_RECONCILIATION_FAILED")
    if not funding_reconciled:
        blockers.append("FUNDING_LEDGER_RECONCILIATION_FAILED")
    required = int(dict(campaign["shakedown_paper_gate"])["minimum_completed_validation_trades"])
    if validation_trades < required:
        blockers.append("MINIMUM_VALIDATION_SHAKEDOWN_TRADES_NOT_MET")
    blockers = sorted(set(blockers))

    return FeasibilityReplayResult(
        schema_id="EQS-FEASIBILITY-V2-REAL-REPLAY-RESULT-V1",
        campaign_id=str(campaign["campaign_id"]),
        campaign_fingerprint=str(campaign_record["campaign_fingerprint"]),
        campaign_version=str(campaign["version"]),
        classification="EXPLORATORY_NON_EVIDENTIARY",
        bar_count=len(bars),
        funding_record_count=len(funding),
        training_completed_trades=training_trades,
        validation_completed_trades=validation_trades,
        total_completed_trades=len(trades),
        final_core_equity=core.equity,
        final_adjusted_equity=core.equity + funding_total,
        core_realized_pnl=core.realized_pnl,
        commissions=core.commissions,
        funding_cashflow=funding_total,
        core_journal_reconciled=ledger.reconcile().ok,
        funding_journal_reconciled=funding_reconciled,
        final_position_quantity=final_position.quantity,
        pending_order_count=simulator.pending_count,
        shakedown_eligible_non_qualifying=not blockers,
        blockers=tuple(blockers),
        broker_submission_enabled=False,
        live_authority=False,
        locked_oos_opened=False,
        counts_toward_168h_100_trade_gate=False,
        r13_certification_authority=False,
        j25_j26_candidate_authority=False,
        trades=tuple(trades),
        funding_journal=tuple(funding_journal),
    )


def load_acquired_train_validation(
    *,
    project_root: str | Path,
    acquisition_root: str | Path,
) -> tuple[dict[str, object], tuple[FeasibilityBar, ...], tuple[FeasibilityFunding, ...]]:
    root = Path(project_root)
    acquisition = Path(acquisition_root)
    campaign_record = load_and_validate(
        root / "research" / "preregistrations" / "v2" / "FEAS-BINANCE-BTC-MA-001.json",
        project_root=root,
    )
    manifest_path = acquisition / "FEAS-BINANCE-BTC-MA-001-acquisition-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    bars: list[FeasibilityBar] = []
    funding: list[FeasibilityFunding] = []
    for entry in manifest.get("receipts", []):
        raw_path = Path(str(entry["raw_path"]))
        payload = raw_path.read_bytes()
        if sha256(payload).hexdigest() != str(entry["archive_sha256"]):
            raise RuntimeError("acquisition raw archive hash mismatch")
        receipt, rows = validate_archive(
            payload,
            campaign_id=str(campaign_record["campaign"]["campaign_id"]),
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
    return campaign_record, ordered_bars, ordered_funding

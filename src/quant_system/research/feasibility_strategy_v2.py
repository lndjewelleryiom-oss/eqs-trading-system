from __future__ import annotations

from collections import deque
from dataclasses import asdict
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
from quant_system.research.feasibility_v2 import campaign_fingerprint, load_and_validate
from quant_system.research.run_registry import StrategyRunRegistry


ZERO = Decimal("0")
ONE = Decimal("1")


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


class FixedSmaCrossover:
    def __init__(self, *, fast_bars: int, slow_bars: int, max_holding_bars: int) -> None:
        if not 1 < fast_bars < slow_bars:
            raise ValueError("require 1 < fast_bars < slow_bars")
        if max_holding_bars <= 0:
            raise ValueError("max_holding_bars must be positive")
        self.fast_bars = int(fast_bars)
        self.slow_bars = int(slow_bars)
        self.max_holding_bars = int(max_holding_bars)
        self._closes: deque[Decimal] = deque(maxlen=self.slow_bars)
        self._bars_in_position = 0

    def observe(self, close: Decimal, position_quantity: Decimal) -> tuple[int, str, dict[str, str]] | None:
        self._closes.append(Decimal(close))
        if position_quantity != ZERO:
            self._bars_in_position += 1
        else:
            self._bars_in_position = 0
        if len(self._closes) < self.slow_bars:
            return None

        values = tuple(self._closes)
        fast = sum(values[-self.fast_bars :], ZERO) / Decimal(self.fast_bars)
        slow = sum(values, ZERO) / Decimal(self.slow_bars)
        target = 1 if fast > slow else -1 if fast < slow else 0
        current = 1 if position_quantity > ZERO else -1 if position_quantity < ZERO else 0
        metrics = {"fast_sma": str(fast), "slow_sma": str(slow)}

        if current == 0 and target != 0:
            return target, "SMA_ENTRY", metrics
        if current != 0 and target != current:
            return 0, "SMA_EXIT_OR_REVERSAL", metrics
        if current != 0 and self._bars_in_position >= self.max_holding_bars:
            return 0, "MAX_HOLD_EXIT", metrics
        return None


def synthetic_shakedown_bars() -> tuple[BarEvent, ...]:
    """Deterministic oscillating bars for mechanics only; never empirical evidence."""
    start = datetime(2026, 1, 5, 0, 0, tzinfo=timezone.utc)
    price = Decimal("100")
    closes: list[Decimal] = []
    # 24 alternating 20-bar regimes create repeated entries/exits after warm-up.
    for regime in range(24):
        step = Decimal("0.35") if regime % 2 == 0 else Decimal("-0.35")
        for _ in range(20):
            price += step
            closes.append(price)
    # Finish flat so the fixed strategy naturally returns to flat.
    closes.extend([price] * 60)

    bars: list[BarEvent] = []
    previous = closes[0]
    for index, close in enumerate(closes):
        open_ = previous if index else close
        high = max(open_, close) + Decimal("0.20")
        low = min(open_, close) - Decimal("0.20")
        bars.append(
            BarEvent(
                symbol="BTCUSDT",
                timestamp=start + timedelta(minutes=5 * index),
                open=open_,
                high=high,
                low=low,
                close=close,
                volume=Decimal("1000"),
            )
        )
        previous = close
    return tuple(bars)


def run_synthetic_shakedown(
    *,
    project_root: str | Path,
    registry: StrategyRunRegistry,
    run_id: str,
) -> dict[str, object]:
    root = Path(project_root)
    contract_path = root / "research" / "preregistrations" / "v2" / "FEAS-BINANCE-BTC-MA-001.json"
    contract = load_and_validate(contract_path, project_root=root)
    campaign = contract["campaign"]
    strategy_cfg = campaign["strategy"]
    execution_cfg = campaign["execution_model"]
    bars = synthetic_shakedown_bars()

    assumptions = ExecutionAssumptions(
        commission_bps=Decimal(str(execution_cfg["fee_bps_per_side"])),
        spread_bps=ZERO,
        slippage_bps=Decimal(str(execution_cfg["adverse_slippage_bps_per_side"])),
        impact_bps=ZERO,
        financing_bps_annual=ZERO,
        borrow_bps_annual=ZERO,
        latency_ms=0,
        partial_fill_fraction=ONE,
    )
    dataset_hash = sha256(_canonical([asdict(bar) for bar in bars])).hexdigest()
    config_hash = sha256(_canonical({"campaign": campaign, "assumptions": asdict(assumptions)})).hexdigest()
    parameters = {
        "fast_bars": strategy_cfg["fast_bars"],
        "slow_bars": strategy_cfg["slow_bars"],
        "max_holding_bars": strategy_cfg["max_holding_bars"],
        "quantity": "1",
        "classification": "SHAKEDOWN_NON_QUALIFYING",
    }

    registry.create_run(
        run_id=run_id,
        strategy_id=campaign["campaign_id"],
        strategy_version=campaign["version"],
        mode="RESEARCH_REPLAY",
        source_id="SYNTHETIC_FEASIBILITY_V2_FIXTURE",
        instrument_id="BTCUSDT",
        parameters=parameters,
        dataset_hash=dataset_hash,
        code_hash="FEASIBILITY_STRATEGY_V2_SYNTHETIC_ACCEPTANCE",
        config_hash=config_hash,
        rationale="Mechanics-only v2 feasibility shakedown. Not R1.3 evidence and not forward-gate credit.",
        fill_provenance="SIMULATED",
    )
    registry.set_status(run_id, "RUNNING")
    registry.append_event(
        run_id,
        "RUN_STARTED",
        market_time_utc=bars[0].timestamp.isoformat(),
        payload={
            "classification": "SHAKEDOWN_NON_QUALIFYING",
            "campaign_fingerprint": contract["campaign_fingerprint"],
            "dataset_hash": dataset_hash,
            "config_hash": config_hash,
            "historical": True,
            "synthetic": True,
            "broker_submission_enabled": False,
            "live_authority": False,
            "counts_toward_168h_100_trade_gate": False,
        },
    )

    strategy = FixedSmaCrossover(
        fast_bars=int(strategy_cfg["fast_bars"]),
        slow_bars=int(strategy_cfg["slow_bars"]),
        max_holding_bars=int(strategy_cfg["max_holding_bars"]),
    )
    simulator = ConservativeBarExecutionSimulator(assumptions, max_volume_participation=Decimal("0.01"))
    ledger = Ledger.with_cash(Decimal("10000"))
    open_order: OrderRequest | None = None
    completed_trades = 0
    trade_entry_realized = ZERO

    for index, bar in enumerate(bars):
        market_time = bar.timestamp.isoformat()
        registry.append_event(
            run_id,
            "MARKET_BAR",
            market_time_utc=market_time,
            payload={
                "symbol": bar.symbol,
                "open": str(bar.open),
                "high": str(bar.high),
                "low": str(bar.low),
                "close": str(bar.close),
                "volume": str(bar.volume),
                "source": "SYNTHETIC_FEASIBILITY_V2_FIXTURE",
            },
        )

        for order, fill in simulator.on_bar(bar):
            ledger.apply(fill, order.side)
            position = ledger.position_state(bar.symbol)
            registry.append_event(
                run_id,
                "FILL",
                market_time_utc=market_time,
                payload={
                    "order_id": str(order.order_id),
                    "side": order.side.value,
                    "quantity": str(fill.quantity),
                    "price": str(fill.price),
                    "commission": str(fill.commission),
                    "slippage_bps": str(fill.slippage_bps_applied),
                    "fill_provenance": "SIMULATED",
                    "broker_submitted": False,
                },
            )
            registry.append_event(
                run_id,
                "POSITION_CHANGE",
                market_time_utc=market_time,
                payload={
                    "symbol": bar.symbol,
                    "quantity": str(position.quantity),
                    "average_cost": str(position.average_cost),
                    "realized_pnl": str(position.realized_pnl),
                },
            )
            if position.quantity == ZERO:
                completed_trades += 1
                trade_pnl = position.realized_pnl - trade_entry_realized
                trade_entry_realized = position.realized_pnl
                registry.append_event(
                    run_id,
                    "TRADE_CLOSED",
                    market_time_utc=market_time,
                    payload={
                        "symbol": bar.symbol,
                        "completed_economic_trade": True,
                        "trade_realized_pnl": str(trade_pnl),
                        "cumulative_realized_pnl": str(position.realized_pnl),
                        "historical_replay_trade": True,
                        "synthetic": True,
                        "counts_toward_forward_gate": False,
                    },
                )
            open_order = None

        accounting = ledger.snapshot({bar.symbol: bar.close})
        registry.append_event(
            run_id,
            "EQUITY_UPDATE",
            market_time_utc=market_time,
            payload={
                "cash": str(accounting.cash),
                "equity": str(accounting.equity),
                "realized_pnl": str(accounting.realized_pnl),
                "unrealized_pnl": str(accounting.unrealized_pnl),
                "commissions": str(accounting.commissions),
                "gross_notional": str(accounting.gross_notional),
                "completed_economic_trades": completed_trades,
            },
        )

        if open_order is None:
            position = ledger.position_state(bar.symbol)
            signal = strategy.observe(bar.close, position.quantity)
            if signal is not None:
                target, reason, metrics = signal
                current = 1 if position.quantity > ZERO else -1 if position.quantity < ZERO else 0
                if target == current:
                    signal = None
                else:
                    if current == 0:
                        side = Side.BUY if target > 0 else Side.SELL
                        quantity = ONE
                        reduce_only = False
                    else:
                        side = Side.SELL if current > 0 else Side.BUY
                        quantity = abs(position.quantity)
                        reduce_only = True
                    registry.append_event(
                        run_id,
                        "SIGNAL",
                        market_time_utc=market_time,
                        payload={
                            "target_position_sign": target,
                            "current_position_sign": current,
                            "reason": reason,
                            **metrics,
                            "decision_uses_only_bars_through_index": index,
                        },
                    )
                    registry.append_event(
                        run_id,
                        "RISK_DECISION",
                        market_time_utc=market_time,
                        payload={
                            "decision": "ALLOW",
                            "reason": "FIXED_SHAKEDOWN_MAX_ONE_UNIT",
                            "max_quantity": "1",
                            "broker_submission_enabled": False,
                        },
                    )
                    order_id = uuid5(NAMESPACE_URL, f"{run_id}:{index}:{side.value}:{reason}")
                    order = OrderRequest(
                        strategy_id=uuid5(NAMESPACE_URL, campaign["campaign_id"]),
                        symbol=bar.symbol,
                        side=side,
                        quantity=quantity,
                        order_type=OrderType.MARKET,
                        decision_time=bar.timestamp,
                        reference_price=bar.close,
                        limit_price=None,
                        reduce_only=reduce_only,
                        order_id=order_id,
                    )
                    simulator.submit(order)
                    open_order = order
                    registry.append_event(
                        run_id,
                        "ORDER_SUBMITTED",
                        market_time_utc=market_time,
                        payload={
                            "order_id": str(order_id),
                            "side": side.value,
                            "quantity": str(quantity),
                            "reduce_only": reduce_only,
                            "submission_mode": "SIMULATOR_ONLY",
                            "broker_submitted": False,
                        },
                    )

        registry.append_event(
            run_id,
            "RUN_PROGRESS",
            market_time_utc=market_time,
            payload={
                "completed_events": index + 1,
                "total_events": len(bars),
                "progress_fraction": (index + 1) / len(bars),
                "current_historical_market_time": market_time,
                "classification": "SHAKEDOWN_NON_QUALIFYING",
            },
        )

    final_position = ledger.position_state("BTCUSDT")
    final = ledger.snapshot({"BTCUSDT": bars[-1].close})
    blockers: list[str] = []
    if simulator.pending_count:
        blockers.append("PENDING_SIMULATED_ORDER_AT_END")
    if final_position.quantity != ZERO:
        blockers.append("NON_FLAT_POSITION_AT_END")
    if completed_trades < int(campaign["shakedown_paper_gate"]["minimum_completed_validation_trades"]):
        blockers.append("MINIMUM_SHAKEDOWN_TRADE_COUNT_NOT_MET")
    if not ledger.reconcile().ok:
        blockers.append("ACCOUNTING_RECONCILIATION_FAILED")

    if blockers:
        registry.append_event(
            run_id,
            "RUN_BLOCKED",
            market_time_utc=bars[-1].timestamp.isoformat(),
            payload={"blockers": blockers, "completed_economic_trades": completed_trades},
        )
        registry.set_status(run_id, "BLOCKED", result="SHAKEDOWN_MECHANICS_BLOCKED")
    else:
        registry.append_event(
            run_id,
            "RUN_FINISHED",
            market_time_utc=bars[-1].timestamp.isoformat(),
            payload={
                "status": "FINISHED",
                "classification": "SHAKEDOWN_NON_QUALIFYING",
                "completed_economic_trades": completed_trades,
                "equity": str(final.equity),
                "realized_pnl": str(final.realized_pnl),
                "commissions": str(final.commissions),
                "journal_reconciled": True,
                "broker_submission_enabled": False,
                "live_authority": False,
                "counts_toward_168h_100_trade_gate": False,
                "eligible_for_r13_certification": False,
                "eligible_for_j25_j26": False,
            },
        )
        registry.set_status(run_id, "FINISHED", result="SHAKEDOWN_MECHANICS_PASS")
    return registry.get_run(run_id)

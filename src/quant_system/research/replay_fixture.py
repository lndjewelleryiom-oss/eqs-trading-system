from __future__ import annotations

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
from quant_system.research.run_registry import StrategyRunRegistry


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()


def fixture_bars() -> tuple[BarEvent, ...]:
    start = datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)
    closes = ("100", "101", "102", "103", "102", "104", "105", "103")
    bars: list[BarEvent] = []
    prior = Decimal(closes[0])
    for index, close_text in enumerate(closes):
        close = Decimal(close_text)
        open_ = prior if index else close
        high = max(open_, close) + Decimal("1")
        low = min(open_, close) - Decimal("1")
        bars.append(
            BarEvent(
                symbol="FIXTURE-USD",
                timestamp=start + timedelta(minutes=index),
                open=open_,
                high=high,
                low=low,
                close=close,
                volume=Decimal("1000"),
            )
        )
        prior = close
    return tuple(bars)


def run_fixture_replay(registry: StrategyRunRegistry, run_id: str) -> dict[str, object]:
    """Run a deterministic non-broker historical fixture through the canonical EQS simulator."""

    bars = fixture_bars()
    params = {"entry_event_index": 1, "exit_event_index": 5, "quantity": "2"}
    assumptions = ExecutionAssumptions(
        commission_bps=Decimal("5"),
        spread_bps=Decimal("2"),
        slippage_bps=Decimal("2"),
        impact_bps=Decimal("1"),
        financing_bps_annual=Decimal("0"),
        borrow_bps_annual=Decimal("0"),
        latency_ms=0,
        partial_fill_fraction=Decimal("1"),
    )
    dataset_hash = sha256(_canonical([asdict(bar) for bar in bars])).hexdigest()
    config_hash = sha256(_canonical({"parameters": params, "assumptions": asdict(assumptions)})).hexdigest()
    try:
        registry.create_run(
            run_id=run_id,
            strategy_id="fixture.deterministic.v1",
            strategy_version="1",
            mode="RESEARCH_REPLAY",
            source_id="DETERMINISTIC_FIXTURE",
            instrument_id="FIXTURE-USD",
            parameters=params,
            dataset_hash=dataset_hash,
            code_hash="FIXTURE_CODE_V1",
            config_hash=config_hash,
            rationale="J45-J51 deterministic plumbing fixture; not empirical strategy evidence.",
            fill_provenance="SIMULATED",
        )
    except Exception:
        existing = registry.get_run(run_id)
        if existing["status"] in {"FINISHED", "FAILED", "BLOCKED", "CANCELLED"}:
            return existing
        if existing["status"] not in {"QUEUED", "WAITING_RESOURCE"}:
            raise

    registry.set_status(run_id, "RUNNING")
    registry.append_event(
        run_id,
        "RUN_STARTED",
        market_time_utc=bars[0].timestamp.isoformat(),
        payload={
            "mode": "RESEARCH_REPLAY",
            "historical": True,
            "broker_submission_enabled": False,
            "live_authority": False,
            "dataset_hash": dataset_hash,
            "config_hash": config_hash,
            "fill_provenance": "SIMULATED",
        },
    )

    simulator = ConservativeBarExecutionSimulator(assumptions, max_volume_participation=Decimal("0.01"))
    ledger = Ledger.with_cash(Decimal("10000"))
    open_order: OrderRequest | None = None
    completed_trades = 0
    entry_equity: Decimal | None = None

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
                "source": "DETERMINISTIC_FIXTURE",
            },
        )

        for order, fill in simulator.on_bar(bar):
            ledger.apply(fill, order.side)
            event_type = "PARTIAL_FILL" if fill.quantity < order.quantity else "FILL"
            registry.append_event(
                run_id,
                event_type,
                market_time_utc=market_time,
                payload={
                    "order_id": str(order.order_id),
                    "strategy_id": str(order.strategy_id),
                    "symbol": fill.symbol,
                    "side": order.side.value,
                    "quantity": str(fill.quantity),
                    "price": str(fill.price),
                    "commission": str(fill.commission),
                    "spread_bps": str(fill.spread_bps_applied),
                    "slippage_bps": str(fill.slippage_bps_applied),
                    "impact_bps": str(fill.impact_bps_applied),
                    "fill_provenance": "SIMULATED",
                },
            )
            position = ledger.position_state(bar.symbol)
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
            if position.quantity == 0 and order.side == Side.SELL:
                completed_trades += 1
                registry.append_event(
                    run_id,
                    "TRADE_CLOSED",
                    market_time_utc=market_time,
                    payload={
                        "symbol": bar.symbol,
                        "completed_economic_trade": True,
                        "realized_pnl": str(position.realized_pnl),
                        "historical_replay_trade": True,
                        "counts_toward_forward_gate": False,
                    },
                )
            open_order = None

        marks = {bar.symbol: bar.close}
        accounting = ledger.snapshot(marks)
        if entry_equity is None and accounting.positions:
            entry_equity = accounting.equity
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

        side: Side | None = None
        reason: str | None = None
        if index == params["entry_event_index"] and not accounting.positions and open_order is None:
            side, reason = Side.BUY, "FIXTURE_ENTRY_INDEX"
        elif index == params["exit_event_index"] and accounting.positions and open_order is None:
            side, reason = Side.SELL, "FIXTURE_EXIT_INDEX"

        if side is not None:
            quantity = Decimal(params["quantity"])
            registry.append_event(
                run_id,
                "SIGNAL",
                market_time_utc=market_time,
                payload={
                    "side": side.value,
                    "quantity": str(quantity),
                    "reason": reason,
                    "decision_uses_only_events_through_sequence_index": index,
                },
            )
            registry.append_event(
                run_id,
                "RISK_DECISION",
                market_time_utc=market_time,
                payload={
                    "decision": "ALLOW",
                    "reason": "FIXTURE_BOUNDED_RISK",
                    "max_fixture_quantity": "2",
                    "broker_submission_enabled": False,
                },
            )
            order_id = uuid5(NAMESPACE_URL, f"{run_id}:{index}:{side.value}")
            order = OrderRequest(
                strategy_id=uuid5(NAMESPACE_URL, "fixture.deterministic.v1"),
                symbol=bar.symbol,
                side=side,
                quantity=quantity,
                order_type=OrderType.MARKET,
                decision_time=bar.timestamp,
                reference_price=bar.close,
                limit_price=None,
                reduce_only=(side == Side.SELL),
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
                    "order_type": order.order_type.value,
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
                "historical": True,
            },
        )

    final = ledger.snapshot({bars[-1].symbol: bars[-1].close})
    if simulator.pending_count:
        registry.append_event(
            run_id,
            "RUN_BLOCKED",
            market_time_utc=bars[-1].timestamp.isoformat(),
            payload={"reason": "PENDING_SIMULATED_ORDER_AT_END_OF_FIXTURE"},
        )
        registry.set_status(run_id, "BLOCKED", result="PENDING_SIMULATED_ORDER")
    else:
        registry.append_event(
            run_id,
            "RUN_FINISHED",
            market_time_utc=bars[-1].timestamp.isoformat(),
            payload={
                "status": "FINISHED",
                "historical": True,
                "simulated": True,
                "completed_economic_trades": completed_trades,
                "cash": str(final.cash),
                "equity": str(final.equity),
                "realized_pnl": str(final.realized_pnl),
                "unrealized_pnl": str(final.unrealized_pnl),
                "commissions": str(final.commissions),
                "journal_reconciled": ledger.reconcile().ok,
                "counts_toward_168h_100_trade_gate": False,
            },
        )
        registry.set_status(run_id, "FINISHED", result="FIXTURE_PASS")
    return registry.get_run(run_id)


def default_registry(root: str | Path) -> StrategyRunRegistry:
    return StrategyRunRegistry(
        Path(root) / "artifacts" / "research" / "strategy_runs" / "research_runs.db"
    )

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from quant_system.backtest.accounting import JournalEntry, JournalType, LedgerPositionSnapshot, LedgerState
from quant_system.backtest.simulator import SimulatedFill, SimulatorState
from quant_system.core.enums import OrderType, Side
from quant_system.execution.broker import SubmissionResult
from quant_system.execution.models import OrderRequest
from quant_system.paper.engine import PaperEngineState, PaperFillRecord
from quant_system.shadow.engine import ShadowDecision, ShadowEngineState


def _dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("persisted timestamp must be timezone-aware")
    return parsed


def encode_order(order: OrderRequest) -> dict[str, object]:
    return {
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
    }


def decode_order(value: dict[str, object]) -> OrderRequest:
    return OrderRequest(
        strategy_id=UUID(str(value["strategy_id"])),
        symbol=str(value["symbol"]),
        side=Side(str(value["side"])),
        quantity=Decimal(str(value["quantity"])),
        order_type=OrderType(str(value["order_type"])),
        decision_time=_dt(str(value["decision_time"])),
        reference_price=Decimal(str(value["reference_price"])),
        limit_price=None if value.get("limit_price") is None else Decimal(str(value["limit_price"])),
        reduce_only=bool(value.get("reduce_only", False)),
        order_id=UUID(str(value["order_id"])),
    )


def encode_fill(fill: SimulatedFill) -> dict[str, object]:
    return {
        "order_id": str(fill.order_id),
        "symbol": fill.symbol,
        "quantity": str(fill.quantity),
        "price": str(fill.price),
        "commission": str(fill.commission),
        "timestamp": fill.timestamp.isoformat(),
        "spread_bps_applied": str(fill.spread_bps_applied),
        "slippage_bps_applied": str(fill.slippage_bps_applied),
        "impact_bps_applied": str(fill.impact_bps_applied),
    }


def decode_fill(value: dict[str, object]) -> SimulatedFill:
    return SimulatedFill(
        order_id=UUID(str(value["order_id"])),
        symbol=str(value["symbol"]),
        quantity=Decimal(str(value["quantity"])),
        price=Decimal(str(value["price"])),
        commission=Decimal(str(value["commission"])),
        timestamp=_dt(str(value["timestamp"])),
        spread_bps_applied=Decimal(str(value["spread_bps_applied"])),
        slippage_bps_applied=Decimal(str(value["slippage_bps_applied"])),
        impact_bps_applied=Decimal(str(value["impact_bps_applied"])),
    )


def encode_ledger_state(state: LedgerState) -> dict[str, object]:
    return {
        "initial_cash": str(state.initial_cash),
        "cash": str(state.cash),
        "positions": {
            symbol: {
                "quantity": str(position.quantity),
                "average_cost": str(position.average_cost),
                "realized_pnl": str(position.realized_pnl),
            }
            for symbol, position in sorted(state.positions.items())
        },
        "commissions": str(state.commissions),
        "financing_costs": str(state.financing_costs),
        "borrow_costs": str(state.borrow_costs),
        "dividend_cashflow": str(state.dividend_cashflow),
        "journal": [
            {
                "sequence": entry.sequence,
                "timestamp": entry.timestamp.isoformat(),
                "entry_type": entry.entry_type.value,
                "symbol": entry.symbol,
                "cash_delta": str(entry.cash_delta),
                "quantity_delta": str(entry.quantity_delta),
                "detail": entry.detail,
            }
            for entry in state.journal
        ],
        "sequence": state.sequence,
    }


def decode_ledger_state(value: dict[str, object]) -> LedgerState:
    positions_raw = dict(value["positions"])
    journal_raw = list(value["journal"])
    return LedgerState(
        initial_cash=Decimal(str(value["initial_cash"])),
        cash=Decimal(str(value["cash"])),
        positions={
            symbol: LedgerPositionSnapshot(
                Decimal(str(position["quantity"])),
                Decimal(str(position["average_cost"])),
                Decimal(str(position["realized_pnl"])),
            )
            for symbol, position in positions_raw.items()
        },
        commissions=Decimal(str(value["commissions"])),
        financing_costs=Decimal(str(value["financing_costs"])),
        borrow_costs=Decimal(str(value["borrow_costs"])),
        dividend_cashflow=Decimal(str(value["dividend_cashflow"])),
        journal=tuple(
            JournalEntry(
                sequence=int(entry["sequence"]),
                timestamp=_dt(str(entry["timestamp"])),
                entry_type=JournalType(str(entry["entry_type"])),
                symbol=None if entry.get("symbol") is None else str(entry["symbol"]),
                cash_delta=Decimal(str(entry["cash_delta"])),
                quantity_delta=Decimal(str(entry["quantity_delta"])),
                detail=str(entry.get("detail", "")),
            )
            for entry in journal_raw
        ),
        sequence=int(value["sequence"]),
    )


def encode_simulator_state(state: SimulatorState) -> dict[str, object]:
    return {
        "pending_orders": [encode_order(order) for order in state.pending_orders],
        "seen_order_ids": [str(value) for value in state.seen_order_ids],
        "last_bar_times": {symbol: timestamp.isoformat() for symbol, timestamp in sorted(state.last_bar_times.items())},
    }


def decode_simulator_state(value: dict[str, object]) -> SimulatorState:
    return SimulatorState(
        pending_orders=tuple(decode_order(item) for item in list(value["pending_orders"])),
        seen_order_ids=tuple(UUID(str(item)) for item in list(value["seen_order_ids"])),
        last_bar_times={symbol: _dt(str(timestamp)) for symbol, timestamp in dict(value["last_bar_times"]).items()},
    )


def encode_paper_state(state: PaperEngineState) -> dict[str, object]:
    return {
        "ledger": encode_ledger_state(state.ledger),
        "simulator": encode_simulator_state(state.simulator),
        "fills": [{"order": encode_order(record.order), "fill": encode_fill(record.fill)} for record in state.fills],
    }


def decode_paper_state(value: dict[str, object]) -> PaperEngineState:
    return PaperEngineState(
        ledger=decode_ledger_state(dict(value["ledger"])),
        simulator=decode_simulator_state(dict(value["simulator"])),
        fills=tuple(
            PaperFillRecord(decode_order(dict(item["order"])), decode_fill(dict(item["fill"])))
            for item in list(value["fills"])
        ),
    )


def encode_submission_result(result: SubmissionResult) -> dict[str, object]:
    return {
        "sent": result.sent,
        "client_order_id": str(result.client_order_id),
        "venue_order_id": result.venue_order_id,
        "reason": result.reason,
    }


def decode_submission_result(value: dict[str, object]) -> SubmissionResult:
    return SubmissionResult(
        sent=bool(value["sent"]),
        client_order_id=UUID(str(value["client_order_id"])),
        venue_order_id=None if value.get("venue_order_id") is None else str(value["venue_order_id"]),
        reason=str(value["reason"]),
    )


def encode_shadow_state(state: ShadowEngineState) -> dict[str, object]:
    return {
        "decisions": [
            {"order": encode_order(item.order), "submission_result": encode_submission_result(item.submission_result)}
            for item in state.decisions
        ],
        "halted": state.halted,
        "halt_reason": state.halt_reason,
    }


def decode_shadow_state(value: dict[str, object]) -> ShadowEngineState:
    return ShadowEngineState(
        decisions=tuple(
            ShadowDecision(decode_order(dict(item["order"])), decode_submission_result(dict(item["submission_result"])))
            for item in list(value["decisions"])
        ),
        halted=bool(value["halted"]),
        halt_reason=None if value.get("halt_reason") is None else str(value["halt_reason"]),
    )

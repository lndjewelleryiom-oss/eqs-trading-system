from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .mock_adapter import MockReadOnlyEqsAdapter
from .read_model import EqsInterfaceSnapshot, InterfaceMeta
from .runtime_reader import RuntimeReadError, RuntimeStoreReadOnlyReader


def _decimal(value: object | None) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _number(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


class LiveReadOnlyEqsAdapter:
    """Read-only interface adapter backed by the live F5.6 durable runtime store.

    Research/data/evidence fields continue to come from immutable packaged artifacts.
    Runtime fields are replaced by verified persisted state when a runtime database is
    configured. Missing values remain unavailable rather than falling back to demo data.
    """

    def __init__(self, runtime_db: str | Path, repository_root: str | Path | None = None) -> None:
        self.repository_root = Path(repository_root) if repository_root else Path(__file__).resolve().parents[3]
        self.runtime_db = Path(runtime_db)
        self.evidence_adapter = MockReadOnlyEqsAdapter(self.repository_root)
        self.reader = RuntimeStoreReadOnlyReader(self.runtime_db)

    def snapshot(self, *, now: datetime | None = None) -> EqsInterfaceSnapshot:
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        base = self.evidence_adapter.snapshot(now=current).to_payload()
        try:
            live = self.reader.snapshot(now=current)
        except RuntimeReadError as exc:
            return self._runtime_read_failure(base, current, str(exc))

        runtimes = list(live["runtimes"])
        paper = self._latest_mode(runtimes, "PAPER")
        shadow = self._latest_mode(runtimes, "SHADOW")
        base["meta"] = {
            "schema_version": "eqs-interface-read-model-v1",
            "generated_at": current.isoformat(),
            "access_mode": "READ_ONLY",
            "adapter": "F5.6_RUNTIME_READ_ONLY_ADAPTER",
            "data_classification": "REAL_EVIDENCE_PLUS_LIVE_NONLIVE_RUNTIME",
            "mutations_enabled": False,
        }

        base["command"]["active_runs"] = [
            row for row in base["command"]["active_runs"] if row.get("kind") != "EXECUTION"
        ] + [self._command_runtime_row(item) for item in runtimes]
        base["execution"] = self._execution_payload(base["execution"]["verified_acceptance"], paper, shadow, runtimes)
        base["risk"] = self._risk_payload(runtimes)
        base["system_health"] = self._health_payload(base["system_health"], runtimes, paper, shadow)
        return EqsInterfaceSnapshot(
            meta=InterfaceMeta(**base["meta"]),
            command=base["command"],
            data=base["data"],
            alpha_research=base["alpha_research"],
            strategies=base["strategies"],
            risk=base["risk"],
            execution=base["execution"],
            evidence=base["evidence"],
            system_health=base["system_health"],
        )

    @staticmethod
    def _latest_mode(runtimes: list[dict[str, Any]], mode: str) -> dict[str, Any] | None:
        matches = [item for item in runtimes if item["mode"] == mode]
        if not matches:
            return None
        # PAPER summary must prefer a verified checkpoint carrying actual execution/accounting
        # state over a newer flat/HOLD runtime. This prevents an active position being hidden.
        if mode == "PAPER":
            def activity(item):
                cp = item.get("checkpoint") or {}
                payload = cp.get("payload") or {}
                paper = payload.get("paper") or {}
                ledger = paper.get("ledger") or {}
                sim = paper.get("simulator") or {}
                positions = ledger.get("positions") or {}
                fills = paper.get("fills") or []
                pending = sim.get("pending_orders") or []
                nonflat = sum(1 for v in positions.values() if str((v or {}).get("quantity", "0")) not in {"0", "0.0", "0.00"})
                return (bool(item.get("checkpoint_verified")), nonflat, len(fills), len(pending), item.get("updated_at") or "")
            return max(matches, key=activity)
        return max(matches, key=lambda item: item.get("updated_at") or "")

    @staticmethod
    def _command_runtime_row(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "name": f'{item["mode"]} runtime · {item["runtime_id"]}',
            "kind": "EXECUTION",
            "state": item["status"],
            "progress": None,
        }

    def _execution_payload(
        self,
        verified_acceptance: dict[str, Any],
        paper: dict[str, Any] | None,
        shadow: dict[str, Any] | None,
        runtimes: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "source": "LIVE_F5.6_RUNTIME_STORE",
            "read_only": True,
            "runtime_count": len(runtimes),
            "runtimes": [self._runtime_summary(item) for item in runtimes],
            "paper": self._paper_payload(paper),
            "paper_by_venue": {venue: self._paper_payload(self._latest_mode([r for r in runtimes if venue.lower() in r["runtime_id"].lower()], "PAPER")) for venue in ("BYBIT_LINEAR", "OKX_SWAP")},
            "shadow": self._shadow_payload(shadow),
            "verified_acceptance": verified_acceptance,
        }

    @staticmethod
    def _runtime_summary(item: dict[str, Any]) -> dict[str, Any]:
        checkpoint = item.get("checkpoint")
        payload = {} if checkpoint is None else dict(checkpoint.get("payload", {}))
        return {
            "runtime_id": item["runtime_id"],
            "mode": item["mode"],
            "status": item["status"],
            "halt_reason": item["halt_reason"],
            "generation": item["generation"],
            "created_at": item["created_at"],
            "updated_at": item["updated_at"],
            "recent_events": item.get("recent_events", []),
            "latest_reconciliation": item.get("latest_reconciliation"),
            "latest_degradation": item.get("latest_degradation"),
            "lease_state": item["lease_state"],
            "lease_expires_at": item["lease_expires_at"],
            "last_heartbeat_at": item["last_heartbeat_at"],
            "event_count": item["event_count"],
            "events_verified": item["events_verified"],
            "checkpoint_verified": checkpoint is not None,
            "checkpoint_at": None if checkpoint is None else checkpoint["created_at"],
            "metrics": dict(payload.get("metrics", {})),
            "last_market_event_at": payload.get("last_market_event_at"),
            "last_reconcile_at": payload.get("last_reconcile_at"),
            "last_degradation_at": payload.get("last_degradation_at"),
            "consecutive_failures": int(payload.get("consecutive_failures", 0)),
        }

    def _paper_payload(self, runtime: dict[str, Any] | None) -> dict[str, Any]:
        if runtime is None:
            return self._missing_mode("PAPER")
        checkpoint = runtime.get("checkpoint")
        payload = {} if checkpoint is None else dict(checkpoint.get("payload", {}))
        paper = dict(payload.get("paper", {}))
        ledger = dict(paper.get("ledger", {}))
        simulator = dict(paper.get("simulator", {}))
        positions_raw = dict(ledger.get("positions", {}))
        positions = []
        realized_total = Decimal("0")
        for symbol, raw in sorted(positions_raw.items()):
            position = dict(raw)
            qty = _decimal(position.get("quantity")) or Decimal("0")
            realized = _decimal(position.get("realized_pnl")) or Decimal("0")
            realized_total += realized
            positions.append({
                "symbol": symbol,
                "venue": (position.get("valuation_contract") or {}).get("venue", "NOT_PERSISTED"),
                "valuation_contract": position.get("valuation_contract"),
                "side": "LONG" if qty > 0 else "SHORT" if qty < 0 else "FLAT",
                "qty": _number(qty),
                "avg_price": _number(_decimal(position.get("average_cost"))),
                "mark": None,
                "pnl": None,
                "realized_pnl": _number(realized),
            })
        pending_orders = [self._order_row(dict(item), "PENDING") for item in list(simulator.get("pending_orders", []))]
        fills = []
        for item in list(paper.get("fills", [])):
            entry = dict(item)
            fill = dict(entry.get("fill", {}))
            fills.append({
                "id": str(fill.get("order_id", "")),
                "order_id": str(fill.get("order_id", "")),
                "symbol": fill.get("symbol"),
                "price": _number(_decimal(fill.get("price"))),
                "qty": _number(_decimal(fill.get("quantity"))),
                "fee": _number(_decimal(fill.get("commission"))),
                "timestamp": fill.get("timestamp"),
                "slippage_bps": fill.get("slippage_bps_applied"),
            })
        valuations = list(payload.get("paper_valuations", []))
        latest_valuation = dict(valuations[-1]) if valuations else {}
        latest_marks = dict(latest_valuation.get("marks", {}))
        for position in positions:
            mark = _decimal(latest_marks.get(position["symbol"]))
            position["mark"] = _number(mark)
            avg = _decimal(position.get("avg_price")); qty = _decimal(position.get("qty"))
            position["pnl"] = _number((mark-avg)*qty) if mark is not None and avg is not None and qty is not None else None
        reconciliation = self._reconciliation_label(runtime)
        return {
            "available": True,
            "runtime_id": runtime["runtime_id"],
            "status": runtime["status"],
            "halt_reason": runtime["halt_reason"],
            "lease_state": runtime["lease_state"],
            "cash": _number(_decimal(ledger.get("cash"))),
            "equity": _number(_decimal(latest_valuation.get("equity"))),
            "realized_pnl": _number(realized_total),
            "unrealized_pnl": _number(_decimal(latest_valuation.get("unrealized_pnl"))),
            "positions": positions,
            "orders": pending_orders,
            "fills": fills,
            "reconciliation": reconciliation,
            "metrics": dict(payload.get("metrics", {})),
            "marks_persisted": bool(valuations),
            "valuation_history": valuations,
            "commissions": _number(_decimal(ledger.get("commissions"))),
            "financing_costs": _number(_decimal(ledger.get("financing_costs"))),
            "currency": ledger.get("currency"),
            "valuation_at": latest_valuation.get("observed_at") or (None if checkpoint is None else checkpoint["created_at"]),
            "cost_basis": "Realized position P&L before separately recorded commissions/financing; not net account return",
        }

    def _shadow_payload(self, runtime: dict[str, Any] | None) -> dict[str, Any]:
        if runtime is None:
            missing = self._missing_mode("SHADOW")
            missing.update({"decisions": 0, "venue_submission_enabled": None, "venue_submission_state": "NOT_PRESENT", "submitted_orders": 0, "zero_submit_invariant": "NO_RUNTIME"})
            return missing
        checkpoint = runtime.get("checkpoint")
        payload = {} if checkpoint is None else dict(checkpoint.get("payload", {}))
        shadow = dict(payload.get("shadow", {}))
        decisions = list(shadow.get("decisions", []))
        sent_count = 0
        for item in decisions:
            result = dict(dict(item).get("submission_result", {}))
            if bool(result.get("sent", False)):
                sent_count += 1
        invariant = "PASS" if sent_count == 0 else "BREACH_OBSERVED"
        return {
            "available": True,
            "runtime_id": runtime["runtime_id"],
            "status": runtime["status"],
            "halt_reason": runtime["halt_reason"],
            "lease_state": runtime["lease_state"],
            "decisions": len(decisions),
            "venue_submission_enabled": None,
            "venue_submission_state": "NOT_PERSISTED",
            "submitted_orders": sent_count,
            "zero_submit_invariant": "PASS_PERSISTED" if invariant == "PASS" else invariant,
            "reconciliation": self._reconciliation_label(runtime),
            "gateway_halted": bool(shadow.get("halted", False)),
            "gateway_halt_reason": shadow.get("halt_reason"),
            "metrics": dict(payload.get("metrics", {})),
        }

    @staticmethod
    def _order_row(order: dict[str, Any], state: str) -> dict[str, Any]:
        return {
            "id": str(order.get("order_id", "")),
            "symbol": order.get("symbol"),
            "side": order.get("side"),
            "qty": _number(_decimal(order.get("quantity"))),
            "state": state,
            "strategy_id": order.get("strategy_id"),
            "decision_time": order.get("decision_time"),
            "reference_price": _number(_decimal(order.get("reference_price"))),
        }

    @staticmethod
    def _missing_mode(mode: str) -> dict[str, Any]:
        return {
            "available": False,
            "runtime_id": None,
            "status": "NOT_PRESENT",
            "halt_reason": None,
            "lease_state": "NONE",
            "equity": None,
            "realized_pnl": None,
            "unrealized_pnl": None,
            "positions": [],
            "orders": [],
            "fills": [],
            "reconciliation": "NOT_AVAILABLE",
            "metrics": {},
            "message": f"No persisted {mode} runtime exists in the configured read-only store.",
        }

    @staticmethod
    def _reconciliation_label(runtime: dict[str, Any]) -> str:
        event = runtime.get("latest_reconciliation")
        if event is None:
            return "NOT_RECORDED"
        payload = dict(event.get("payload", {}))
        if "action" in payload:
            return str(payload["action"])
        if "ok" in payload:
            return "PASS" if bool(payload["ok"]) else "FAIL"
        if "ledger_reconciled" in payload:
            return "PASS" if bool(payload["ledger_reconciled"]) else "FAIL"
        return str(event.get("event_type", "RECORDED"))

    def _risk_payload(self, runtimes: list[dict[str, Any]]) -> dict[str, Any]:
        halted = any(item["status"] == "HALTED" for item in runtimes)
        degradation_events = [item["latest_degradation"] for item in runtimes if item.get("latest_degradation")]
        latest_degradation = degradation_events[0] if degradation_events else None
        degradation_state = "NOT_RECORDED"
        if latest_degradation is not None:
            degradation_state = str(dict(latest_degradation.get("payload", {})).get("state", "RECORDED"))
        paused = degradation_state == "PAUSED" or any(
            str(item.get("halt_reason") or "").startswith("DEGRADATION_PAUSE:") for item in runtimes
        )
        conditions = [
            {"condition": "Runtime HALT", "state": "ACTIVE" if halted else "CLEAR"},
            {"condition": "Degradation PAUSE", "state": "ACTIVE" if paused else degradation_state},
            {"condition": "Persisted event integrity", "state": "VERIFIED" if all(item["events_verified"] for item in runtimes) else "FAIL"},
        ]
        for item in runtimes:
            if item.get("halt_reason"):
                conditions.append({"condition": f'{item["mode"]} halt reason', "state": str(item["halt_reason"])})
        return {
            "source": "LIVE_F5.6_RUNTIME_STORE",
            "state": "HALTED" if halted else "OBSERVED",
            "degradation_state": degradation_state,
            "halted": halted,
            "paused": paused,
            "limits": [],
            "limits_state": "NOT_PERSISTED_IN_RUNTIME_STORE",
            "conditions": conditions,
        }

    @staticmethod
    def _health_payload(
        base_health: dict[str, Any],
        runtimes: list[dict[str, Any]],
        paper: dict[str, Any] | None,
        shadow: dict[str, Any] | None,
    ) -> dict[str, Any]:
        active_leases = [item for item in runtimes if item["lease_state"] == "ACTIVE"]
        halted = [item for item in runtimes if item["status"] == "HALTED"]
        expired_live = [item for item in runtimes if item["lease_state"] == "EXPIRED" and item["status"] in {"RUNNING", "DEGRADED", "STARTING"}]
        overall = "HALTED_NON_LIVE" if halted else "DEGRADED_NON_LIVE" if expired_live else "HEALTHY_NON_LIVE"
        if active_leases:
            lease_state = "ACTIVE"
            lease_detail = f"{len(active_leases)} active runtime lease(s) observed read-only."
        elif runtimes:
            lease_state = "NONE_ACTIVE"
            lease_detail = "Persisted runtimes exist, but no current active lease was observed."
        else:
            lease_state = "NONE"
            lease_detail = "No persisted runtime records exist in the configured store."
        alerts = list(base_health["alerts"])
        alerts = [item for item in alerts if "mock" not in item["message"].lower()]
        if expired_live:
            alerts.append({"severity": "BLOCK", "message": "A runtime is marked operational but its persisted lease is expired."})
        for item in halted:
            alerts.append({"severity": "BLOCK", "message": f'{item["mode"]} runtime {item["runtime_id"]} is HALTED: {item.get("halt_reason") or "reason not recorded"}.'})
        shadow_state = "NOT_PRESENT"
        shadow_detail = "No SHADOW runtime is present."
        if shadow is not None:
            shadow_payload = {} if shadow.get("checkpoint") is None else dict(shadow["checkpoint"].get("payload", {})).get("shadow", {})
            decisions = list(dict(shadow_payload).get("decisions", [])) if isinstance(shadow_payload, dict) else []
            sent = sum(bool(dict(dict(x).get("submission_result", {})).get("sent", False)) for x in decisions)
            shadow_state = "DISABLED" if sent == 0 else "BREACH"
            shadow_detail = f"Persisted SHADOW decisions observed: {len(decisions)}; sent=true results: {sent}."
            if sent:
                alerts.append({"severity": "BLOCK", "message": "SHADOW zero-submit invariant breach is present in persisted runtime state."})
        reconcile_events = [item.get("latest_reconciliation") for item in runtimes if item.get("latest_reconciliation")]
        return {
            "overall": overall,
            "feed": {"state": "OBSERVED_VIA_CHECKPOINT", "detail": "Last market-event timestamps are read from persisted runtime checkpoints; feed transport itself is not controlled by this interface."},
            "lease": {"state": lease_state, "detail": lease_detail},
            "restart_recovery": base_health["restart_recovery"],
            "persistence": {"state": "VERIFIED_READ_ONLY", "detail": "Runtime events/checkpoints were read without acquiring a lease and their persisted SHA-256 values were verified."},
            "reconciliation": {"state": "RECORDED" if reconcile_events else "NOT_RECORDED", "detail": "Latest persisted reconciliation event is surfaced when available."},
            "shadow_submission": {"state": shadow_state, "detail": shadow_detail},
            "alerts": alerts,
        }

    def _runtime_read_failure(self, base: dict[str, Any], current: datetime, message: str) -> EqsInterfaceSnapshot:
        base["meta"] = {
            "schema_version": "eqs-interface-read-model-v1",
            "generated_at": current.isoformat(),
            "access_mode": "READ_ONLY",
            "adapter": "F5.6_RUNTIME_READ_ONLY_ADAPTER",
            "data_classification": "REAL_EVIDENCE_RUNTIME_STATUS_UNAVAILABLE",
            "mutations_enabled": False,
        }
        base["risk"] = {
            "source": "RUNTIME_READ_FAILURE",
            "state": "UNKNOWN_FAIL_CLOSED",
            "degradation_state": "UNKNOWN",
            "halted": False,
            "paused": False,
            "limits": [],
            "limits_state": "UNAVAILABLE",
            "conditions": [{"condition": "Runtime status read", "state": "FAILED"}],
        }
        base["execution"] = {
            "source": "RUNTIME_READ_FAILURE",
            "read_only": True,
            "runtime_count": 0,
            "runtimes": [],
            "paper": self._missing_mode("PAPER"),
            "shadow": {**self._missing_mode("SHADOW"), "decisions": 0, "venue_submission_enabled": None, "venue_submission_state": "UNKNOWN", "submitted_orders": 0, "zero_submit_invariant": "UNKNOWN"},
            "verified_acceptance": base["execution"]["verified_acceptance"],
        }
        base["command"]["active_runs"] = [
            row for row in base["command"]["active_runs"] if row.get("kind") != "EXECUTION"
        ] + [{"name": "F5.6 runtime status", "kind": "EXECUTION", "state": "READ_FAILURE", "progress": None}]
        base["system_health"] = {
            "overall": "RUNTIME_STATUS_UNAVAILABLE",
            "feed": {"state": "UNKNOWN", "detail": "Runtime store could not be trusted/read."},
            "lease": {"state": "UNKNOWN", "detail": "Runtime lease state unavailable."},
            "restart_recovery": base["system_health"]["restart_recovery"],
            "persistence": {"state": "READ_FAILURE", "detail": message},
            "reconciliation": {"state": "UNKNOWN", "detail": "Runtime status read failed."},
            "shadow_submission": {"state": "UNKNOWN", "detail": "Persisted SHADOW state unavailable."},
            "alerts": [item for item in base["system_health"]["alerts"] if "mock" not in item["message"].lower()] + [{"severity": "BLOCK", "message": "Runtime status unavailable: " + message}],
        }
        return EqsInterfaceSnapshot(
            meta=InterfaceMeta(**base["meta"]), command=base["command"], data=base["data"],
            alpha_research=base["alpha_research"], strategies=base["strategies"], risk=base["risk"],
            execution=base["execution"], evidence=base["evidence"], system_health=base["system_health"],
        )

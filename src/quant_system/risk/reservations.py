from __future__ import annotations
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
import sqlite3
import time
from uuid import UUID, uuid5

from quant_system.risk.contracts import RiskDecisionV1, RiskDisposition, RiskReservation, ReservationState

_NS = UUID("ff923657-5168-5b5f-8ed8-45ed9fb6b086")
_ACTIVE = ("RESERVED", "PARTIALLY_CONSUMED", "UNKNOWN_EXECUTION")

class ReservationConflict(RuntimeError):
    pass

class RiskReservationStore:
    """Durable EQS-06 capacity ledger. No exchange operations exist here."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(20):
            try:
                with self._connect() as c:
                    c.execute("PRAGMA journal_mode=WAL")
                    c.executescript("""
            CREATE TABLE IF NOT EXISTS risk_reservations(
              reservation_id TEXT PRIMARY KEY, decision_id TEXT UNIQUE NOT NULL,
              intent_id TEXT NOT NULL, portfolio_id TEXT NOT NULL, strategy_id TEXT NOT NULL,
              instrument TEXT NOT NULL, venue TEXT NOT NULL, reserved_notional TEXT NOT NULL,
              consumed_notional TEXT NOT NULL, state TEXT NOT NULL, created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL, expires_at TEXT, version INTEGER NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_rr_portfolio_state
              ON risk_reservations(portfolio_id,state);
            CREATE TABLE IF NOT EXISTS execution_truth_cursor(
              reservation_id TEXT PRIMARY KEY, execution_id TEXT NOT NULL,
              truth_sequence INTEGER NOT NULL, reconciliation_generation INTEGER NOT NULL,
              cumulative_filled_notional TEXT NOT NULL, execution_state TEXT NOT NULL,
              intent_id TEXT NOT NULL, venue TEXT NOT NULL, instrument TEXT NOT NULL,
              observed_at TEXT NOT NULL, evidence_ref TEXT NOT NULL);
            """)
                break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == 19:
                    raise
                time.sleep(0.01 * (attempt + 1))

    def _connect(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA busy_timeout=10000")
        c.execute("PRAGMA synchronous=FULL")
        return c

    @staticmethod
    def _aware(now: datetime):
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")

    def active_notional(self, portfolio_id: str) -> Decimal:
        with self._connect() as c:
            rows = c.execute(
                "SELECT reserved_notional,consumed_notional FROM risk_reservations "
                "WHERE portfolio_id=? AND state IN (?,?,?)",
                (portfolio_id, *_ACTIVE)).fetchall()
        return sum((Decimal(r["reserved_notional"]) - Decimal(r["consumed_notional"]) for r in rows), Decimal("0"))

    def reserve(self, decision: RiskDecisionV1, *, portfolio_id: str, instrument: str,
                venue: str, reference_price: Decimal, max_total_reserved: Decimal,
                now: datetime, expires_at: datetime | None = None) -> RiskReservation:
        self._aware(now)
        if decision.disposition == RiskDisposition.REJECT or decision.authorised_quantity <= 0:
            raise ReservationConflict("decision has no reservable authority")
        amount = decision.authorised_quantity * reference_price
        rid = uuid5(_NS, str(decision.decision_id))
        with self._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            existing = c.execute("SELECT * FROM risk_reservations WHERE decision_id=?",
                                 (str(decision.decision_id),)).fetchone()
            if existing is not None:
                c.commit()
                return self._row(existing)
            rows = c.execute(
                "SELECT reserved_notional,consumed_notional FROM risk_reservations "
                "WHERE portfolio_id=? AND state IN (?,?,?)",
                (portfolio_id, *_ACTIVE)).fetchall()
            active = sum((Decimal(r[0])-Decimal(r[1]) for r in rows), Decimal("0"))
            if active + amount > max_total_reserved:
                raise ReservationConflict("portfolio reservation capacity exceeded")
            c.execute("""INSERT INTO risk_reservations VALUES
                (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                str(rid), str(decision.decision_id), str(decision.intent_id), portfolio_id,
                str(decision.strategy_id), instrument, venue, str(amount), "0",
                ReservationState.RESERVED.value, now.isoformat(), now.isoformat(),
                None if expires_at is None else expires_at.isoformat(), 1))
            c.commit()
        return self.get(rid)

    def transition(self, reservation_id: UUID, *, state: ReservationState,
                   now: datetime, consumed_notional: Decimal | None = None) -> RiskReservation:
        self._aware(now)
        with self._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM risk_reservations WHERE reservation_id=?",
                            (str(reservation_id),)).fetchone()
            if row is None:
                raise KeyError(str(reservation_id))
            old = ReservationState(row["state"])
            allowed = {
                ReservationState.RESERVED: {ReservationState.PARTIALLY_CONSUMED, ReservationState.CONSUMED, ReservationState.RELEASED, ReservationState.UNKNOWN_EXECUTION},
                ReservationState.PARTIALLY_CONSUMED: {ReservationState.CONSUMED, ReservationState.RELEASED, ReservationState.UNKNOWN_EXECUTION},
                ReservationState.UNKNOWN_EXECUTION: {ReservationState.RESERVED, ReservationState.PARTIALLY_CONSUMED, ReservationState.CONSUMED, ReservationState.RELEASED},
            }
            if state != old and state not in allowed.get(old, set()):
                raise ReservationConflict(f"invalid transition {old}->{state}")
            consumed = Decimal(row["consumed_notional"]) if consumed_notional is None else consumed_notional
            reserved = Decimal(row["reserved_notional"])
            if consumed < 0 or consumed > reserved:
                raise ReservationConflict("invalid consumed notional")
            if state == ReservationState.CONSUMED:
                consumed = reserved
            c.execute("""UPDATE risk_reservations SET state=?,consumed_notional=?,
                      updated_at=?,version=version+1 WHERE reservation_id=?""",
                      (state.value, str(consumed), now.isoformat(), str(reservation_id)))
            c.commit()
        return self.get(reservation_id)

    def truth_cursor(self, reservation_id: UUID):
        with self._connect() as c:
            return c.execute("SELECT * FROM execution_truth_cursor WHERE reservation_id=?",
                             (str(reservation_id),)).fetchone()

    def save_truth_cursor(self, truth) -> None:
        with self._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute("""INSERT INTO execution_truth_cursor VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(reservation_id) DO UPDATE SET
                execution_id=excluded.execution_id,truth_sequence=excluded.truth_sequence,
                reconciliation_generation=excluded.reconciliation_generation,
                cumulative_filled_notional=excluded.cumulative_filled_notional,
                execution_state=excluded.execution_state,intent_id=excluded.intent_id,
                venue=excluded.venue,instrument=excluded.instrument,
                observed_at=excluded.observed_at,evidence_ref=excluded.evidence_ref""",(
                str(truth.reservation_id),str(truth.execution_id),truth.truth_sequence,
                truth.reconciliation_generation,str(truth.cumulative_filled_notional),
                truth.execution_state.value,str(truth.intent_id),truth.venue,truth.instrument,
                truth.observed_at.isoformat(),truth.evidence_ref))
            c.commit()

    def apply_execution_truth(self, truth) -> RiskReservation:
        """Settle only from explicit EQS-07 evidence; UNKNOWN keeps capacity."""
        from quant_system.risk.interfaces import ExecutionTruth
        mapping = {
            ExecutionTruth.OPEN: ReservationState.RESERVED,
            ExecutionTruth.PARTIAL_FILL: ReservationState.PARTIALLY_CONSUMED,
            ExecutionTruth.FILLED: ReservationState.CONSUMED,
            ExecutionTruth.CANCELLED: ReservationState.RELEASED,
            ExecutionTruth.REJECTED: ReservationState.RELEASED,
            ExecutionTruth.SUPPRESSED: ReservationState.RELEASED,
            ExecutionTruth.UNKNOWN: ReservationState.UNKNOWN_EXECUTION,
        }
        return self.transition(
            truth.reservation_id, state=mapping[truth.execution_state],
            now=truth.observed_at, consumed_notional=truth.cumulative_filled_notional,
        )

    def get(self, reservation_id: UUID) -> RiskReservation:
        with self._connect() as c:
            row = c.execute("SELECT * FROM risk_reservations WHERE reservation_id=?",
                            (str(reservation_id),)).fetchone()
        if row is None:
            raise KeyError(str(reservation_id))
        return self._row(row)

    @staticmethod
    def _row(r) -> RiskReservation:
        parse = lambda x: None if x is None else datetime.fromisoformat(x)
        return RiskReservation(
            UUID(r["reservation_id"]), UUID(r["decision_id"]), UUID(r["intent_id"]),
            r["portfolio_id"], UUID(r["strategy_id"]), r["instrument"], r["venue"],
            Decimal(r["reserved_notional"]), Decimal(r["consumed_notional"]),
            ReservationState(r["state"]), datetime.fromisoformat(r["created_at"]),
            datetime.fromisoformat(r["updated_at"]), parse(r["expires_at"]), int(r["version"]))

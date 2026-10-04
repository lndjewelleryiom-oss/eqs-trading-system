from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from typing import Any

from .ledger import INFRASTRUCTURE_TEST_STRATEGY_VERSION


def _canonical(payload: object) -> bytes:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def _sha(payload: object) -> str:
    return sha256(_canonical(payload)).hexdigest()


class AttributionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AttributedPaperFill:
    runtime_id: str
    event_id: str
    execution_intent_id: str
    client_order_id: str
    occurred_at: str
    strategy_id: str
    strategy_version: str
    venue_id: str
    fill_id: str
    quantity: Decimal
    price: Decimal
    fee_amount: Decimal
    fee_currency: str | None
    performance_claim_eligible: bool
    exclusion_reason: str | None


class PersistentPaperAttributionLedger:
    """Immutable projection of canonical PAPER simulator fills to strategies."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=FULL")
        con.execute("PRAGMA busy_timeout=10000")
        return con

    def _init_schema(self) -> None:
        con = self._connect()
        try:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS attribution_ingest_state(
                    source_db TEXT PRIMARY KEY,
                    last_execution_row_id INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS paper_fill_attribution(
                    source_db TEXT NOT NULL,
                    source_execution_row_id INTEGER NOT NULL,
                    runtime_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    execution_intent_id TEXT NOT NULL,
                    client_order_id TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    strategy_id TEXT NOT NULL,
                    strategy_version TEXT NOT NULL,
                    venue_id TEXT NOT NULL,
                    fill_id TEXT NOT NULL,
                    quantity TEXT NOT NULL,
                    price TEXT NOT NULL,
                    fee_amount TEXT NOT NULL,
                    fee_currency TEXT,
                    event_sha256 TEXT NOT NULL,
                    intent_sha256 TEXT NOT NULL,
                    performance_claim_eligible INTEGER NOT NULL CHECK(performance_claim_eligible IN (0,1)),
                    exclusion_reason TEXT,
                    PRIMARY KEY(source_db, source_execution_row_id),
                    UNIQUE(source_db, event_id)
                );

                CREATE TABLE IF NOT EXISTS attribution_journal(
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_db TEXT NOT NULL,
                    source_execution_row_id INTEGER NOT NULL,
                    event_id TEXT NOT NULL,
                    strategy_id TEXT NOT NULL,
                    strategy_version TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    previous_hash TEXT,
                    journal_hash TEXT NOT NULL UNIQUE
                );

                CREATE TRIGGER IF NOT EXISTS attribution_rows_no_update
                BEFORE UPDATE ON paper_fill_attribution
                BEGIN SELECT RAISE(ABORT,'immutable attribution row'); END;
                CREATE TRIGGER IF NOT EXISTS attribution_rows_no_delete
                BEFORE DELETE ON paper_fill_attribution
                BEGIN SELECT RAISE(ABORT,'immutable attribution row'); END;
                CREATE TRIGGER IF NOT EXISTS attribution_journal_no_update
                BEFORE UPDATE ON attribution_journal
                BEGIN SELECT RAISE(ABORT,'immutable attribution journal'); END;
                CREATE TRIGGER IF NOT EXISTS attribution_journal_no_delete
                BEFORE DELETE ON attribution_journal
                BEGIN SELECT RAISE(ABORT,'immutable attribution journal'); END;
                """
            )
            con.commit()
        finally:
            con.close()

    @staticmethod
    def _eligibility(strategy_id: str, strategy_version: str) -> tuple[bool, str | None]:
        if not strategy_id.strip() or not strategy_version.strip():
            return False, "STRATEGY_IDENTITY_MISSING"
        if strategy_version == INFRASTRUCTURE_TEST_STRATEGY_VERSION:
            return False, "INFRASTRUCTURE_TEST_STRATEGY"
        return True, None

    def ingest_runtime_execution(self, runtime_db: str | Path) -> dict[str, int]:
        runtime_db = Path(runtime_db).resolve()
        source_db = str(runtime_db)
        src = sqlite3.connect(runtime_db, timeout=10)
        src.row_factory = sqlite3.Row
        dst = self._connect()
        counters = {
            "execution_rows_seen": 0,
            "paper_fill_rows_added": 0,
            "eligible_fill_rows_added": 0,
        }
        try:
            state = dst.execute(
                "SELECT last_execution_row_id FROM attribution_ingest_state WHERE source_db=?",
                (source_db,),
            ).fetchone()
            last_id = int(state["last_execution_row_id"]) if state else 0

            rows = src.execute(
                """SELECT id,runtime_id,event_id,execution_intent_id,client_order_id,
                          runtime_mode,truth_source,authoritative_external_truth,
                          occurred_at,event_json,event_sha256
                   FROM execution_v1_events
                   WHERE id>? ORDER BY id""",
                (last_id,),
            ).fetchall()
            highest = last_id

            with dst:
                for row in rows:
                    counters["execution_rows_seen"] += 1
                    highest = max(highest, int(row["id"]))
                    event = json.loads(row["event_json"])
                    if _sha(event) != row["event_sha256"]:
                        raise AttributionError(
                            f"EXECUTION_EVENT_HASH_INVALID:{row['runtime_id']}:{row['event_id']}"
                        )

                    if row["runtime_mode"] != "PAPER":
                        continue
                    if row["truth_source"] != "SIMULATOR":
                        continue
                    if int(row["authoritative_external_truth"]) != 0:
                        raise AttributionError("PAPER_SIMULATOR_EVENT_MARKED_EXTERNAL_TRUTH")
                    if str(event.get("event_type")) not in {"PARTIAL_FILL", "FILL"}:
                        continue

                    binding = src.execute(
                        """SELECT intent_json,intent_sha256 FROM execution_v1_bindings
                           WHERE runtime_id=? AND client_order_id=?""",
                        (row["runtime_id"], row["client_order_id"]),
                    ).fetchone()
                    if binding is None:
                        raise AttributionError(
                            f"FILL_INTENT_BINDING_MISSING:{row['runtime_id']}:{row['client_order_id']}"
                        )
                    intent = json.loads(binding["intent_json"])
                    if _sha(intent) != binding["intent_sha256"]:
                        raise AttributionError("INTENT_HASH_INVALID")

                    strategy = intent.get("strategy") or {}
                    strategy_id = str(strategy.get("strategy_id") or "")
                    strategy_version = str(strategy.get("strategy_version") or "")
                    eligible, exclusion = self._eligibility(strategy_id, strategy_version)

                    fill = event.get("fill") or {}
                    fill_id = str(fill.get("fill_id") or "")
                    quantity = Decimal(str(fill.get("quantity")))
                    price = Decimal(str(fill.get("price")))
                    if not fill_id or quantity <= 0 or price <= 0:
                        raise AttributionError("CANONICAL_FILL_DETAIL_INVALID")

                    fees = event.get("fees") or []
                    fee_amount = Decimal("0")
                    fee_currency: str | None = None
                    for fee in fees:
                        amount = Decimal(str(fee.get("amount", "0")))
                        currency = str(fee.get("currency") or "")
                        if fee_currency is None and currency:
                            fee_currency = currency
                        elif currency and fee_currency != currency:
                            raise AttributionError("MULTI_CURRENCY_FILL_FEES_UNSUPPORTED")
                        fee_amount += amount

                    dst.execute(
                        """INSERT OR IGNORE INTO paper_fill_attribution(
                           source_db,source_execution_row_id,runtime_id,event_id,
                           execution_intent_id,client_order_id,occurred_at,
                           strategy_id,strategy_version,venue_id,fill_id,quantity,price,
                           fee_amount,fee_currency,event_sha256,intent_sha256,
                           performance_claim_eligible,exclusion_reason
                           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            source_db,
                            int(row["id"]),
                            row["runtime_id"],
                            row["event_id"],
                            row["execution_intent_id"],
                            row["client_order_id"],
                            row["occurred_at"],
                            strategy_id,
                            strategy_version,
                            str(event.get("venue_id") or ""),
                            fill_id,
                            str(quantity),
                            str(price),
                            str(fee_amount),
                            fee_currency,
                            row["event_sha256"],
                            binding["intent_sha256"],
                            1 if eligible else 0,
                            exclusion,
                        ),
                    )
                    if dst.execute("SELECT changes()").fetchone()[0]:
                        counters["paper_fill_rows_added"] += 1
                        if eligible:
                            counters["eligible_fill_rows_added"] += 1
                        prev = dst.execute(
                            "SELECT journal_hash FROM attribution_journal ORDER BY sequence DESC LIMIT 1"
                        ).fetchone()
                        previous_hash = prev["journal_hash"] if prev else None
                        body = {
                            "source_db": source_db,
                            "source_execution_row_id": int(row["id"]),
                            "event_id": row["event_id"],
                            "strategy_id": strategy_id,
                            "strategy_version": strategy_version,
                            "occurred_at": row["occurred_at"],
                            "previous_hash": previous_hash,
                        }
                        dst.execute(
                            """INSERT INTO attribution_journal(
                               source_db,source_execution_row_id,event_id,
                               strategy_id,strategy_version,occurred_at,
                               previous_hash,journal_hash
                               ) VALUES(?,?,?,?,?,?,?,?)""",
                            (
                                source_db,
                                int(row["id"]),
                                row["event_id"],
                                strategy_id,
                                strategy_version,
                                row["occurred_at"],
                                previous_hash,
                                _sha(body),
                            ),
                        )

                dst.execute(
                    """INSERT INTO attribution_ingest_state(source_db,last_execution_row_id)
                       VALUES(?,?)
                       ON CONFLICT(source_db) DO UPDATE SET
                       last_execution_row_id=excluded.last_execution_row_id""",
                    (source_db, highest),
                )
        finally:
            src.close()
            dst.close()
        return counters

    def fills(
        self,
        *,
        eligible_only: bool = False,
        strategy_id: str | None = None,
        strategy_version: str | None = None,
    ) -> tuple[AttributedPaperFill, ...]:
        clauses = []
        args: list[object] = []
        if eligible_only:
            clauses.append("performance_claim_eligible=1")
        if strategy_id is not None:
            clauses.append("strategy_id=?")
            args.append(strategy_id)
        if strategy_version is not None:
            clauses.append("strategy_version=?")
            args.append(strategy_version)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        con = self._connect()
        try:
            rows = con.execute(
                "SELECT * FROM paper_fill_attribution"
                + where
                + " ORDER BY occurred_at,source_execution_row_id",
                args,
            ).fetchall()
            return tuple(
                AttributedPaperFill(
                    runtime_id=row["runtime_id"],
                    event_id=row["event_id"],
                    execution_intent_id=row["execution_intent_id"],
                    client_order_id=row["client_order_id"],
                    occurred_at=row["occurred_at"],
                    strategy_id=row["strategy_id"],
                    strategy_version=row["strategy_version"],
                    venue_id=row["venue_id"],
                    fill_id=row["fill_id"],
                    quantity=Decimal(row["quantity"]),
                    price=Decimal(row["price"]),
                    fee_amount=Decimal(row["fee_amount"]),
                    fee_currency=row["fee_currency"],
                    performance_claim_eligible=bool(row["performance_claim_eligible"]),
                    exclusion_reason=row["exclusion_reason"],
                )
                for row in rows
            )
        finally:
            con.close()

    def strategy_counts(self) -> dict[tuple[str, str], int]:
        con = self._connect()
        try:
            rows = con.execute(
                """SELECT strategy_id,strategy_version,COUNT(*) n
                   FROM paper_fill_attribution
                   WHERE performance_claim_eligible=1
                   GROUP BY strategy_id,strategy_version
                   ORDER BY strategy_id,strategy_version"""
            ).fetchall()
            return {
                (row["strategy_id"], row["strategy_version"]): int(row["n"])
                for row in rows
            }
        finally:
            con.close()

    def verify_hash_chain(self) -> bool:
        con = self._connect()
        try:
            previous = None
            for row in con.execute("SELECT * FROM attribution_journal ORDER BY sequence"):
                body = {
                    "source_db": row["source_db"],
                    "source_execution_row_id": int(row["source_execution_row_id"]),
                    "event_id": row["event_id"],
                    "strategy_id": row["strategy_id"],
                    "strategy_version": row["strategy_version"],
                    "occurred_at": row["occurred_at"],
                    "previous_hash": row["previous_hash"],
                }
                if row["previous_hash"] != previous or row["journal_hash"] != _sha(body):
                    return False
                previous = row["journal_hash"]
            return True
        finally:
            con.close()

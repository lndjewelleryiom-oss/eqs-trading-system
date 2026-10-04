from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import time
from typing import Any

from .ledger import PerformanceLedger
from .lifecycle import PersistentStrategyLifecycle, StrategyLifecycleState
from .promotion import ResearchPromotionError, promote_passed_genuine_trial
from .publication import publish_json_generation
from .replacement import ReplacementResearchError, ReplacementResearchQueue


def _canonical(obj: object) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")


class PerformanceAutonomyCycle:
    """One fail-closed iteration of the autonomous PAPER performance programme."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.ev = self.root / "artifacts" / "test-evidence"
        self.runtime_db = self.root / "artifacts" / "commissioning" / "current_runtime_v1" / "runtime.db"
        self.performance_db = self.ev / "EQS_PAPER_PERFORMANCE_LEDGER.db"
        self.lifecycle_db = self.ev / "EQS_STRATEGY_LIFECYCLE.db"
        self.alpha_db = self.ev / "EQS_ALPHA_TRIAL_EVIDENCE_LEDGER.db"
        self.replacement_db = self.ev / "EQS_REPLACEMENT_RESEARCH.db"
        self.canary_db = self.ev / "EQS_PAPER_CANARY_ADMISSIONS.db"
        self.output = self.ev / "EQS_PERFORMANCE_AUTONOMY_STATE.json"

    @staticmethod
    def _strategy_key(strategy_id: str, version: str) -> str:
        return strategy_id + "@" + version

    def _passed_genuine_trials(self) -> tuple[str, ...]:
        if not self.alpha_db.is_file():
            return ()
        con = sqlite3.connect(self.alpha_db)
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute(
                """SELECT trial_id FROM alpha_trials
                   WHERE source_class='GENUINE' AND state='PASSED'
                   ORDER BY created_at,trial_id"""
            ).fetchall()
            return tuple(row["trial_id"] for row in rows)
        finally:
            con.close()

    def _canary_admissions(self) -> dict[str, dict[str, Any]]:
        if not self.canary_db.is_file():
            return {}
        con = sqlite3.connect(self.canary_db)
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute(
                "SELECT * FROM paper_canary_admissions ORDER BY admitted_at,admission_id"
            ).fetchall()
            return {
                self._strategy_key(row["strategy_id"], row["strategy_version"]): dict(row)
                for row in rows
            }
        finally:
            con.close()

    def run(self) -> dict[str, Any]:
        self.ev.mkdir(parents=True, exist_ok=True)
        ledger = PerformanceLedger(self.performance_db)
        ingest = ledger.ingest_runtime_events(self.runtime_db)
        lifecycle = PersistentStrategyLifecycle(self.lifecycle_db)
        replacements = ReplacementResearchQueue(self.replacement_db)

        promotion_results: list[dict[str, Any]] = []
        blockers: list[dict[str, str]] = []
        for trial_id in self._passed_genuine_trials():
            try:
                result = promote_passed_genuine_trial(
                    alpha_ledger_path=self.alpha_db,
                    trial_id=trial_id,
                    lifecycle=lifecycle,
                )
                promotion_results.append(
                    {
                        "trial_id": trial_id,
                        "strategy_id": result.strategy_id,
                        "strategy_version": result.strategy_version,
                        "state": result.lifecycle_state.value,
                    }
                )
            except ResearchPromotionError as exc:
                blockers.append(
                    {
                        "scope": "RESEARCH_PROMOTION",
                        "item": trial_id,
                        "reason": str(exc),
                    }
                )

        replacement_results: list[dict[str, Any]] = []
        records = lifecycle.records()
        for record in records:
            if record.state not in {StrategyLifecycleState.PAUSED, StrategyLifecycleState.RETIRED}:
                continue
            try:
                request = replacements.enqueue_for_strategy(
                    lifecycle=lifecycle,
                    strategy_id=record.strategy_id,
                    strategy_version=record.strategy_version,
                    reason="LIFECYCLE_" + record.state.value,
                )
                replacement_results.append(
                    {
                        "request_id": request.request_id,
                        "strategy_id": record.strategy_id,
                        "strategy_version": record.strategy_version,
                        "state": request.state.value,
                    }
                )
            except ReplacementResearchError as exc:
                blockers.append(
                    {
                        "scope": "REPLACEMENT_RESEARCH",
                        "item": self._strategy_key(record.strategy_id, record.strategy_version),
                        "reason": str(exc),
                    }
                )

        canary_admissions = self._canary_admissions()
        records = lifecycle.records()
        strategy_rows = []
        for record in records:
            key = self._strategy_key(record.strategy_id, record.strategy_version)
            strategy_rows.append(
                {
                    "strategy_id": record.strategy_id,
                    "strategy_version": record.strategy_version,
                    "state": record.state.value,
                    "source_class": record.source_class,
                    "asset_class": record.asset_class,
                    "family": record.family,
                    "horizon": record.horizon,
                    "research_trial_id": record.research_trial_id,
                    "research_evidence_sha256": record.research_evidence_sha256,
                    "canary_admitted": key in canary_admissions,
                }
            )
            if record.state is StrategyLifecycleState.VALIDATED and key not in canary_admissions:
                blockers.append(
                    {
                        "scope": "PAPER_CANARY",
                        "item": key,
                        "reason": "EXECUTABLE_STRATEGY_AND_INTERFACE_CERTIFICATION_REQUIRED",
                    }
                )

        runtime_snapshots: dict[str, Any] = {}
        equity_series: dict[str, list[dict[str, str]]] = {}
        for runtime_id in ("current-bybit_linear-paper", "current-okx_swap-paper"):
            try:
                runtime_snapshots[runtime_id] = asdict(ledger.runtime_snapshot(runtime_id))
                equity_series[runtime_id] = list(ledger.equity_series(runtime_id, limit=1000))
            except ValueError:
                runtime_snapshots[runtime_id] = {"state": "NO_VALUATION"}
                equity_series[runtime_id] = []

        activity = [asdict(row) for row in ledger.strategy_activity()]
        eligible_activity = [row for row in activity if row["performance_claim_eligible"]]
        genuine_lifecycle = [row for row in strategy_rows if row["source_class"] == "GENUINE"]
        managed = [
            row for row in strategy_rows
            if row["state"] in {"PAPER_CANARY", "PAPER_ACTIVE", "REDUCED", "PAUSED"}
        ]

        if not genuine_lifecycle:
            blockers.append(
                {
                    "scope": "AUTONOMOUS_ALPHA",
                    "item": "GENUINE_RESEARCH",
                    "reason": "NO_GENUINE_VALIDATED_STRATEGY_AVAILABLE",
                }
            )

        state = {
            "schema_id": "EQS-PERFORMANCE-AUTONOMY-STATE-V1",
            "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "mode": "PAPER_ONLY",
            "status": "RUNNING" if managed else "READY_WAITING_FOR_GENUINE_STRATEGY",
            "live_authority": False,
            "broker_submission_enabled": False,
            "infrastructure_test_performance_claim_allowed": False,
            "performance_ledger": {
                "hash_chain_valid": ledger.verify_hash_chain(),
                "ingest": ingest,
                "runtime_snapshots": runtime_snapshots,
                "equity_series": equity_series,
                "eligible_strategy_activity_count": len(eligible_activity),
            },
            "lifecycle": {
                "hash_chain_valid": lifecycle.verify_hash_chain(),
                "strategy_count": len(strategy_rows),
                "strategies": strategy_rows,
            },
            "research_promotion": {
                "canonical_alpha_ledger_present": self.alpha_db.is_file(),
                "promotions_this_cycle": promotion_results,
            },
            "paper_canary": {
                "admission_count": len(canary_admissions),
                "admissions": list(canary_admissions.values()),
            },
            "replacement_research": {
                "hash_chain_valid": replacements.verify_hash_chain(),
                "pending_count": len(replacements.pending()),
                "queued_this_cycle": replacement_results,
            },
            "blockers": sorted(blockers, key=lambda row: (row["scope"], row["item"], row["reason"])),
            "next_valid_action": (
                "RUN_MANAGED_PAPER_PERFORMANCE_LOOP"
                if managed
                else "WAIT_FOR_GENUINE_R13_ALPHA_AND_EXECUTABLE_STRATEGY_ARTIFACT"
            ),
            "performance_autonomy_ready": False,
        }
        state["record_sha256"] = sha256(_canonical(state)).hexdigest()
        publish_json_generation(self.output, state)
        return state

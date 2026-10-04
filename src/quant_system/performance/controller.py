from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from typing import Mapping

from quant_system.monitoring.degradation import (
    DegradationAssessment,
    DegradationMonitor,
    DegradationState,
    ExpectedBehavior,
    ObservedBehavior,
)
from quant_system.portfolio.allocator import (
    AllocationResult,
    ConstrainedPortfolioAllocator,
    PortfolioConstraints,
    StrategyAllocationInput,
)
from quant_system.regime.models import MarketRegime

from .lifecycle import (
    PersistentStrategyLifecycle,
    StrategyLifecycleState,
)


class PerformanceControllerError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class StrategyPerformanceInput:
    strategy_id: str
    strategy_version: str
    expected: ExpectedBehavior
    observed: ObservedBehavior
    base_weight: float
    confidence: float
    expected_volatility: float
    liquidity_score: float
    capacity_notional: Decimal
    regime_fit: Mapping[MarketRegime, float]
    forward_paper_acceptance_passed: bool = False
    feature_psi: float = 0.0
    prediction_psi: float = 0.0
    execution_error: bool = False
    data_integrity_error: bool = False


@dataclass(frozen=True, slots=True)
class StrategyControlDecision:
    strategy_id: str
    strategy_version: str
    lifecycle_before: StrategyLifecycleState
    lifecycle_after: StrategyLifecycleState
    degradation_state: DegradationState
    allocation_multiplier: float
    reasons: tuple[str, ...]
    replacement_research_requested: bool


@dataclass(frozen=True, slots=True)
class PerformanceControlCycle:
    cycle_id: str
    observed_at: str
    decisions: tuple[StrategyControlDecision, ...]
    allocation: AllocationResult
    journal_sha256: str


def _canonical(obj) -> bytes:
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")


class PersistentPerformanceController:
    """Closed-loop PAPER performance controller.

    It can promote a successful PAPER canary to PAPER_ACTIVE, reduce/pause
    degraded strategies, enqueue replacement research and calculate constrained
    PAPER allocations. It cannot create a LIVE state or enable broker submission.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        lifecycle: PersistentStrategyLifecycle,
        degradation_monitor: DegradationMonitor | None = None,
        allocator: ConstrainedPortfolioAllocator | None = None,
    ):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lifecycle = lifecycle
        self.degradation_monitor = degradation_monitor or DegradationMonitor()
        self.allocator = allocator or ConstrainedPortfolioAllocator()
        self._init_schema()

    def _connect(self):
        con = sqlite3.connect(self.path, timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=FULL")
        con.execute("PRAGMA busy_timeout=10000")
        return con

    def _init_schema(self):
        con = self._connect()
        try:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS performance_control_cycles(
                    cycle_id TEXT PRIMARY KEY,
                    observed_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    previous_hash TEXT,
                    cycle_hash TEXT NOT NULL UNIQUE
                );

                CREATE TABLE IF NOT EXISTS replacement_research_queue(
                    request_id TEXT PRIMARY KEY,
                    strategy_id TEXT NOT NULL,
                    strategy_version TEXT NOT NULL,
                    requested_at TEXT NOT NULL,
                    reason_codes_json TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('QUEUED','CLAIMED','COMPLETED','CANCELLED')),
                    request_sha256 TEXT NOT NULL UNIQUE
                );

                CREATE TRIGGER IF NOT EXISTS control_cycles_no_update
                BEFORE UPDATE ON performance_control_cycles
                BEGIN SELECT RAISE(ABORT,'immutable performance control cycle'); END;
                CREATE TRIGGER IF NOT EXISTS control_cycles_no_delete
                BEFORE DELETE ON performance_control_cycles
                BEGIN SELECT RAISE(ABORT,'immutable performance control cycle'); END;
                """
            )
            con.commit()
        finally:
            con.close()

    def _queue_replacement(
        self,
        con: sqlite3.Connection,
        *,
        strategy_id: str,
        strategy_version: str,
        reasons: tuple[str, ...],
        requested_at: str,
    ) -> bool:
        existing = con.execute(
            """SELECT 1 FROM replacement_research_queue
               WHERE strategy_id=? AND strategy_version=? AND state IN ('QUEUED','CLAIMED')""",
            (strategy_id, strategy_version),
        ).fetchone()
        if existing:
            return False
        body = {
            "strategy_id": strategy_id,
            "strategy_version": strategy_version,
            "requested_at": requested_at,
            "reason_codes": reasons,
            "state": "QUEUED",
        }
        request_sha = sha256(_canonical(body)).hexdigest()
        request_id = "replacement-" + request_sha[:20]
        con.execute(
            """INSERT INTO replacement_research_queue(
               request_id,strategy_id,strategy_version,requested_at,
               reason_codes_json,state,request_sha256
               ) VALUES(?,?,?,?,?,?,?)""",
            (
                request_id,
                strategy_id,
                strategy_version,
                requested_at,
                json.dumps(reasons, sort_keys=True),
                "QUEUED",
                request_sha,
            ),
        )
        return True

    def _target_lifecycle(
        self,
        *,
        before: StrategyLifecycleState,
        assessment: DegradationAssessment,
        forward_passed: bool,
    ) -> StrategyLifecycleState:
        if before not in {
            StrategyLifecycleState.PAPER_CANARY,
            StrategyLifecycleState.PAPER_ACTIVE,
            StrategyLifecycleState.REDUCED,
        }:
            raise PerformanceControllerError(
                f"strategy state is not autonomously controllable: {before.value}"
            )

        if assessment.state == DegradationState.PAUSED:
            return StrategyLifecycleState.PAUSED
        if assessment.state == DegradationState.REDUCED:
            return StrategyLifecycleState.REDUCED
        if before == StrategyLifecycleState.PAPER_CANARY:
            if assessment.state == DegradationState.ACTIVE and forward_passed:
                return StrategyLifecycleState.PAPER_ACTIVE
            return StrategyLifecycleState.PAPER_CANARY
        if before == StrategyLifecycleState.REDUCED:
            if assessment.state == DegradationState.ACTIVE:
                return StrategyLifecycleState.PAPER_ACTIVE
            return StrategyLifecycleState.REDUCED
        return StrategyLifecycleState.PAPER_ACTIVE

    def run_cycle(
        self,
        inputs: list[StrategyPerformanceInput],
        *,
        regime: MarketRegime,
        regime_confidence: float,
        nav: Decimal,
        correlations: dict[tuple[str, str], float] | None = None,
        broker_submission_enabled: bool,
        live_authority: bool,
        paper_authorised: bool,
    ) -> PerformanceControlCycle:
        if broker_submission_enabled:
            raise PerformanceControllerError("BROKER_SUBMISSION_MUST_REMAIN_DISABLED")
        if live_authority:
            raise PerformanceControllerError("LIVE_AUTHORITY_MUST_REMAIN_FALSE")
        if not paper_authorised:
            raise PerformanceControllerError("PAPER_AUTHORITY_REQUIRED")
        if nav <= 0:
            raise PerformanceControllerError("NAV_MUST_BE_POSITIVE")
        identities = [(x.strategy_id, x.strategy_version) for x in inputs]
        if len(set(identities)) != len(identities):
            raise PerformanceControllerError("DUPLICATE_STRATEGY_INPUT")

        observed_at = datetime.now(timezone.utc).isoformat()
        decisions: list[StrategyControlDecision] = []
        allocation_inputs: list[StrategyAllocationInput] = []
        replacement_requests: list[tuple[str, str, tuple[str, ...]]] = []

        for item in sorted(inputs, key=lambda x: (x.strategy_id, x.strategy_version)):
            current = self.lifecycle.get(item.strategy_id, item.strategy_version)
            assessment = self.degradation_monitor.assess(
                item.expected,
                item.observed,
                feature_psi=item.feature_psi,
                prediction_psi=item.prediction_psi,
                execution_error=item.execution_error,
                data_integrity_error=item.data_integrity_error,
            )
            target = self._target_lifecycle(
                before=current.state,
                assessment=assessment,
                forward_passed=item.forward_paper_acceptance_passed,
            )

            transition_reasons = tuple(assessment.reasons)
            after = current.state
            if target != current.state:
                updated = self.lifecycle.transition(
                    item.strategy_id,
                    item.strategy_version,
                    target,
                    reasons=transition_reasons,
                    evidence_sha256=sha256(
                        _canonical(
                            {
                                "expected": asdict(item.expected),
                                "observed": asdict(item.observed),
                                "assessment": asdict(assessment),
                                "observed_at": observed_at,
                            }
                        )
                    ).hexdigest(),
                )
                after = updated.state

            replacement = after in {
                StrategyLifecycleState.PAUSED,
                StrategyLifecycleState.RETIRED,
            }
            if replacement:
                replacement_requests.append(
                    (item.strategy_id, item.strategy_version, transition_reasons)
                )

            decisions.append(
                StrategyControlDecision(
                    strategy_id=item.strategy_id,
                    strategy_version=item.strategy_version,
                    lifecycle_before=current.state,
                    lifecycle_after=after,
                    degradation_state=assessment.state,
                    allocation_multiplier=assessment.allocation_multiplier,
                    reasons=transition_reasons,
                    replacement_research_requested=replacement,
                )
            )

            if after in {
                StrategyLifecycleState.PAPER_CANARY,
                StrategyLifecycleState.PAPER_ACTIVE,
                StrategyLifecycleState.REDUCED,
            }:
                drawdown_fraction = min(
                    1.0,
                    max(
                        0.0,
                        (
                            item.observed.drawdown / item.expected.max_drawdown
                            if item.expected.max_drawdown > 0
                            else (1.0 if item.observed.drawdown > 0 else 0.0)
                        ),
                    ),
                )
                allocation_inputs.append(
                    StrategyAllocationInput(
                        strategy_id=item.strategy_id,
                        base_weight=item.base_weight,
                        confidence=item.confidence,
                        expected_volatility=item.expected_volatility,
                        drawdown_fraction=drawdown_fraction,
                        liquidity_score=item.liquidity_score,
                        capacity_notional=item.capacity_notional,
                        regime_fit=dict(item.regime_fit),
                        degradation_multiplier=assessment.allocation_multiplier,
                    )
                )

        allocation = self.allocator.allocate(
            allocation_inputs,
            regime=regime,
            regime_confidence=regime_confidence,
            nav=nav,
            correlations=correlations or {},
        )

        payload = {
            "observed_at": observed_at,
            "decisions": [
                {
                    "strategy_id": d.strategy_id,
                    "strategy_version": d.strategy_version,
                    "lifecycle_before": d.lifecycle_before.value,
                    "lifecycle_after": d.lifecycle_after.value,
                    "degradation_state": d.degradation_state.value,
                    "allocation_multiplier": d.allocation_multiplier,
                    "reasons": d.reasons,
                    "replacement_research_requested": d.replacement_research_requested,
                }
                for d in decisions
            ],
            "allocation": {
                "weights": allocation.weights,
                "gross_weight": allocation.gross_weight,
                "cash_weight": allocation.cash_weight,
                "reasons": allocation.reasons,
            },
            "regime": str(regime.value if hasattr(regime, "value") else regime),
            "regime_confidence": regime_confidence,
            "nav": str(nav),
            "mode": "PAPER",
            "broker_submission_enabled": False,
            "live_authority": False,
        }

        con = self._connect()
        try:
            with con:
                prev = con.execute(
                    "SELECT cycle_hash FROM performance_control_cycles ORDER BY rowid DESC LIMIT 1"
                ).fetchone()
                previous_hash = prev["cycle_hash"] if prev else None
                cycle_body = {**payload, "previous_hash": previous_hash}
                cycle_hash = sha256(_canonical(cycle_body)).hexdigest()
                cycle_id = "performance-cycle-" + cycle_hash[:20]
                con.execute(
                    """INSERT INTO performance_control_cycles(
                       cycle_id,observed_at,payload_json,previous_hash,cycle_hash
                       ) VALUES(?,?,?,?,?)""",
                    (
                        cycle_id,
                        observed_at,
                        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str),
                        previous_hash,
                        cycle_hash,
                    ),
                )
                for sid, version, reasons in replacement_requests:
                    self._queue_replacement(
                        con,
                        strategy_id=sid,
                        strategy_version=version,
                        reasons=reasons,
                        requested_at=observed_at,
                    )
        finally:
            con.close()

        return PerformanceControlCycle(
            cycle_id=cycle_id,
            observed_at=observed_at,
            decisions=tuple(decisions),
            allocation=allocation,
            journal_sha256=cycle_hash,
        )

    def queued_replacements(self) -> tuple[dict[str, object], ...]:
        con = self._connect()
        try:
            rows = con.execute(
                """SELECT request_id,strategy_id,strategy_version,requested_at,
                          reason_codes_json,state,request_sha256
                   FROM replacement_research_queue
                   WHERE state='QUEUED' ORDER BY requested_at,request_id"""
            ).fetchall()
            return tuple(
                {
                    "request_id": row["request_id"],
                    "strategy_id": row["strategy_id"],
                    "strategy_version": row["strategy_version"],
                    "requested_at": row["requested_at"],
                    "reason_codes": tuple(json.loads(row["reason_codes_json"])),
                    "state": row["state"],
                    "request_sha256": row["request_sha256"],
                }
                for row in rows
            )
        finally:
            con.close()

    def verify_hash_chain(self) -> bool:
        con = self._connect()
        try:
            previous = None
            for row in con.execute(
                "SELECT payload_json,previous_hash,cycle_hash FROM performance_control_cycles ORDER BY rowid"
            ):
                payload = json.loads(row["payload_json"])
                body = {**payload, "previous_hash": row["previous_hash"]}
                if row["previous_hash"] != previous or row["cycle_hash"] != sha256(_canonical(body)).hexdigest():
                    return False
                previous = row["cycle_hash"]
            return True
        finally:
            con.close()


# Compatibility facade retained for the earlier closed-loop acceptance contract.
@dataclass(frozen=True, slots=True)
class ForwardPaperEvidence:
    decision_count: int
    fill_count: int
    elapsed_hours: float
    evidence_sha256: str

    def __post_init__(self) -> None:
        if self.decision_count < 0 or self.fill_count < 0 or self.elapsed_hours < 0:
            raise ValueError("forward PAPER evidence counts/duration must be non-negative")
        if len(self.evidence_sha256) != 64 or any(c not in "0123456789abcdef" for c in self.evidence_sha256):
            raise ValueError("evidence_sha256 must be lowercase SHA-256")


class PerformanceControlError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PerformanceControlDecision:
    strategy_id: str
    strategy_version: str
    prior_state: StrategyLifecycleState
    resulting_state: StrategyLifecycleState
    degradation_state: DegradationState
    allocation_multiplier: float
    reason_codes: tuple[str, ...]
    forward_evidence_sufficient: bool
    decision_sha256: str


class PaperPerformanceController:
    """Single-strategy compatibility controller used by the original closed-loop test.

    It remains PAPER-only. Canary promotion requires both healthy observed
    behaviour and a minimum forward PAPER evidence window. Severe degradation
    can always reduce/pause immediately.
    """

    MIN_DECISIONS = 50
    MIN_FILLS = 20
    MIN_HOURS = 24.0

    def __init__(self, path: str | Path, *, monitor: DegradationMonitor | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.monitor = monitor or DegradationMonitor()
        self._init_schema()

    def _connect(self):
        con = sqlite3.connect(self.path, timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=FULL")
        con.execute("PRAGMA busy_timeout=10000")
        return con

    def _init_schema(self):
        con = self._connect()
        try:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS paper_performance_control_journal(
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy_id TEXT NOT NULL,
                    strategy_version TEXT NOT NULL,
                    prior_state TEXT NOT NULL,
                    resulting_state TEXT NOT NULL,
                    degradation_state TEXT NOT NULL,
                    allocation_multiplier REAL NOT NULL,
                    reason_codes_json TEXT NOT NULL,
                    forward_evidence_json TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    previous_hash TEXT,
                    decision_sha256 TEXT NOT NULL UNIQUE
                );
                CREATE TRIGGER IF NOT EXISTS paper_performance_control_no_update
                BEFORE UPDATE ON paper_performance_control_journal
                BEGIN SELECT RAISE(ABORT,'immutable paper performance control journal'); END;
                CREATE TRIGGER IF NOT EXISTS paper_performance_control_no_delete
                BEFORE DELETE ON paper_performance_control_journal
                BEGIN SELECT RAISE(ABORT,'immutable paper performance control journal'); END;
                """
            )
            con.commit()
        finally:
            con.close()

    def evaluate(
        self,
        *,
        lifecycle: PersistentStrategyLifecycle,
        strategy_id: str,
        strategy_version: str,
        expected: ExpectedBehavior,
        observed: ObservedBehavior,
        evidence: ForwardPaperEvidence,
        feature_psi: float = 0.0,
        prediction_psi: float = 0.0,
        execution_error: bool = False,
        data_integrity_error: bool = False,
    ) -> PerformanceControlDecision:
        current = lifecycle.get(strategy_id, strategy_version)
        if current.state not in {
            StrategyLifecycleState.PAPER_CANARY,
            StrategyLifecycleState.PAPER_ACTIVE,
            StrategyLifecycleState.REDUCED,
        }:
            raise PerformanceControlError("STRATEGY_NOT_PAPER_CONTROLLABLE")

        assessment = self.monitor.assess(
            expected,
            observed,
            feature_psi=feature_psi,
            prediction_psi=prediction_psi,
            execution_error=execution_error,
            data_integrity_error=data_integrity_error,
        )
        sufficient = (
            evidence.decision_count >= self.MIN_DECISIONS
            and evidence.fill_count >= self.MIN_FILLS
            and evidence.elapsed_hours >= self.MIN_HOURS
        )

        target = current.state
        reasons = list(assessment.reasons)
        if assessment.state is DegradationState.PAUSED:
            target = StrategyLifecycleState.PAUSED
        elif assessment.state is DegradationState.REDUCED:
            target = StrategyLifecycleState.REDUCED
        elif current.state is StrategyLifecycleState.PAPER_CANARY:
            if assessment.state is DegradationState.ACTIVE and sufficient:
                target = StrategyLifecycleState.PAPER_ACTIVE
                reasons.append("FORWARD_PAPER_ACCEPTANCE_PASS")
            else:
                reasons.append("FORWARD_PAPER_ACCEPTANCE_PENDING")
        elif current.state is StrategyLifecycleState.REDUCED:
            if assessment.state is DegradationState.ACTIVE:
                target = StrategyLifecycleState.PAPER_ACTIVE
                reasons.append("PERFORMANCE_RECOVERED")
        elif current.state is StrategyLifecycleState.PAPER_ACTIVE:
            target = StrategyLifecycleState.PAPER_ACTIVE

        if target != current.state:
            lifecycle.transition(
                strategy_id,
                strategy_version,
                target,
                reasons=tuple(reasons),
                evidence_sha256=evidence.evidence_sha256,
            )

        occurred_at = datetime.now(timezone.utc).isoformat()
        forward_payload = {
            "decision_count": evidence.decision_count,
            "fill_count": evidence.fill_count,
            "elapsed_hours": evidence.elapsed_hours,
            "evidence_sha256": evidence.evidence_sha256,
            "sufficient": sufficient,
        }
        con = self._connect()
        try:
            with con:
                prev = con.execute(
                    "SELECT decision_sha256 FROM paper_performance_control_journal ORDER BY sequence DESC LIMIT 1"
                ).fetchone()
                previous_hash = prev["decision_sha256"] if prev else None
                body = {
                    "strategy_id": strategy_id,
                    "strategy_version": strategy_version,
                    "prior_state": current.state.value,
                    "resulting_state": target.value,
                    "degradation_state": assessment.state.value,
                    "allocation_multiplier": assessment.allocation_multiplier,
                    "reason_codes": tuple(reasons),
                    "forward_evidence": forward_payload,
                    "occurred_at": occurred_at,
                    "previous_hash": previous_hash,
                }
                decision_sha = sha256(_canonical(body)).hexdigest()
                con.execute(
                    """INSERT INTO paper_performance_control_journal(
                       strategy_id,strategy_version,prior_state,resulting_state,
                       degradation_state,allocation_multiplier,reason_codes_json,
                       forward_evidence_json,occurred_at,previous_hash,decision_sha256
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        strategy_id,
                        strategy_version,
                        current.state.value,
                        target.value,
                        assessment.state.value,
                        assessment.allocation_multiplier,
                        json.dumps(tuple(reasons), sort_keys=True),
                        json.dumps(forward_payload, sort_keys=True, separators=(",", ":")),
                        occurred_at,
                        previous_hash,
                        decision_sha,
                    ),
                )
        finally:
            con.close()

        return PerformanceControlDecision(
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            prior_state=current.state,
            resulting_state=target,
            degradation_state=assessment.state,
            allocation_multiplier=assessment.allocation_multiplier,
            reason_codes=tuple(reasons),
            forward_evidence_sufficient=sufficient,
            decision_sha256=decision_sha,
        )

    def verify_hash_chain(self) -> bool:
        con = self._connect()
        try:
            previous = None
            for row in con.execute(
                "SELECT * FROM paper_performance_control_journal ORDER BY sequence"
            ):
                body = {
                    "strategy_id": row["strategy_id"],
                    "strategy_version": row["strategy_version"],
                    "prior_state": row["prior_state"],
                    "resulting_state": row["resulting_state"],
                    "degradation_state": row["degradation_state"],
                    "allocation_multiplier": row["allocation_multiplier"],
                    "reason_codes": tuple(json.loads(row["reason_codes_json"])),
                    "forward_evidence": json.loads(row["forward_evidence_json"]),
                    "occurred_at": row["occurred_at"],
                    "previous_hash": row["previous_hash"],
                }
                if row["previous_hash"] != previous or row["decision_sha256"] != sha256(_canonical(body)).hexdigest():
                    return False
                previous = row["decision_sha256"]
            return True
        finally:
            con.close()

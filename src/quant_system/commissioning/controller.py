from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


class LiveLevel(IntEnum):
    LIVE_0 = 0
    LIVE_1 = 1
    LIVE_2 = 2
    LIVE_3 = 3
    LIVE_4 = 4


@dataclass(frozen=True, slots=True)
class CommissioningEvidence:
    all_required_components_passed: bool = False
    postgres_verified: bool = False
    trade_only_credentials_present: bool = False
    withdrawals_disabled: bool = False
    regulatory_approved: bool = False
    operational_approved: bool = False
    human_live_authorized: bool = False
    shadow_acceptance_passed: bool = False
    risk_acceptance_passed: bool = False


@dataclass(frozen=True, slots=True)
class ReadinessDecision:
    ready: bool
    reasons: tuple[str, ...]


def assess_live_1_readiness(evidence: CommissioningEvidence) -> ReadinessDecision:
    checks = {
        "COMPONENT_GATES_INCOMPLETE": evidence.all_required_components_passed,
        "POSTGRES_NOT_VERIFIED": evidence.postgres_verified,
        "TRADE_ONLY_CREDENTIALS_MISSING": evidence.trade_only_credentials_present,
        "WITHDRAWALS_NOT_CONFIRMED_DISABLED": evidence.withdrawals_disabled,
        "REGULATORY_APPROVAL_MISSING": evidence.regulatory_approved,
        "OPERATIONAL_APPROVAL_MISSING": evidence.operational_approved,
        "HUMAN_LIVE_AUTHORIZATION_MISSING": evidence.human_live_authorized,
        "SHADOW_ACCEPTANCE_INCOMPLETE": evidence.shadow_acceptance_passed,
        "RISK_ACCEPTANCE_INCOMPLETE": evidence.risk_acceptance_passed,
    }
    reasons = tuple(reason for reason, passed in checks.items() if not passed)
    return ReadinessDecision(not reasons, reasons or ("LIVE_1_READY",))


@dataclass(frozen=True, slots=True)
class ScaleEvidence:
    minimum_observation_count: int
    actual_observation_count: int
    realized_slippage_within_limit: bool
    drawdown_within_limit: bool
    reconciliation_clean: bool
    degradation_clear: bool

    @property
    def passed(self) -> bool:
        return (
            self.minimum_observation_count > 0
            and self.actual_observation_count >= self.minimum_observation_count
            and self.realized_slippage_within_limit
            and self.drawdown_within_limit
            and self.reconciliation_clean
            and self.degradation_clear
        )


@dataclass(frozen=True, slots=True)
class RuntimeSafetyEvidence:
    global_kill_switch_clear: bool = True
    market_data_fresh: bool = True
    broker_healthy: bool = True
    reconciliation_clean: bool = True
    daily_loss_within_limit: bool = True
    drawdown_within_limit: bool = True
    execution_quality_within_limit: bool = True
    degradation_clear: bool = True


@dataclass(frozen=True, slots=True)
class RuntimeSafetyDecision:
    safe: bool
    target: LiveLevel | None
    reasons: tuple[str, ...]


def assess_runtime_safety(current: LiveLevel, evidence: RuntimeSafetyEvidence) -> RuntimeSafetyDecision:
    critical_checks = {
        "GLOBAL_KILL_SWITCH": evidence.global_kill_switch_clear,
        "STALE_MARKET_DATA": evidence.market_data_fresh,
        "BROKER_UNHEALTHY": evidence.broker_healthy,
        "RECONCILIATION_FAILURE": evidence.reconciliation_clean,
        "DAILY_LOSS_LIMIT": evidence.daily_loss_within_limit,
        "MAX_DRAWDOWN": evidence.drawdown_within_limit,
    }
    soft_checks = {
        "EXECUTION_QUALITY_DEGRADED": evidence.execution_quality_within_limit,
        "STRATEGY_DEGRADATION": evidence.degradation_clear,
    }
    critical = tuple(reason for reason, passed in critical_checks.items() if not passed)
    soft = tuple(reason for reason, passed in soft_checks.items() if not passed)
    if critical:
        return RuntimeSafetyDecision(False, LiveLevel.LIVE_0, critical + soft)
    if soft:
        target = LiveLevel(max(LiveLevel.LIVE_0, int(current) - 1))
        return RuntimeSafetyDecision(False, target, soft)
    return RuntimeSafetyDecision(True, None, ("RUNTIME_SAFETY_CLEAR",))


class CommissioningController:
    """Evidence-gated live-state controller. It does not submit orders itself."""

    def __init__(self):
        self.state = LiveLevel.LIVE_0
        self.history: list[tuple[LiveLevel, str]] = [(LiveLevel.LIVE_0, "INITIAL_DISABLED_STATE")]

    def enter_live_1(self, evidence: CommissioningEvidence) -> LiveLevel:
        if self.state != LiveLevel.LIVE_0:
            raise ValueError("LIVE_1 can only be entered from LIVE_0")
        decision = assess_live_1_readiness(evidence)
        if not decision.ready:
            raise PermissionError(";".join(decision.reasons))
        self.state = LiveLevel.LIVE_1
        self.history.append((self.state, "LIVE_1_READINESS_PASSED"))
        return self.state

    def scale_one_level(self, evidence: ScaleEvidence) -> LiveLevel:
        if self.state < LiveLevel.LIVE_1 or self.state >= LiveLevel.LIVE_4:
            raise ValueError("scaling requires LIVE_1..LIVE_3")
        if not evidence.passed:
            raise PermissionError("SCALE_EVIDENCE_FAILED")
        self.state = LiveLevel(self.state + 1)
        self.history.append((self.state, "SCALE_EVIDENCE_PASSED"))
        return self.state

    def apply_runtime_safety(self, evidence: RuntimeSafetyEvidence) -> RuntimeSafetyDecision:
        decision = assess_runtime_safety(self.state, evidence)
        if decision.safe:
            return decision
        if self.state == LiveLevel.LIVE_0:
            self.history.append((self.state, "SAFETY_BLOCK:" + ",".join(decision.reasons)))
            return decision
        assert decision.target is not None
        self.state = decision.target
        self.history.append((self.state, "AUTO_DERISK:" + ",".join(decision.reasons)))
        return decision

    def rollback(self, reason: str, *, target: LiveLevel = LiveLevel.LIVE_0) -> LiveLevel:
        if not reason.strip():
            raise ValueError("rollback reason is required")
        if target > self.state:
            raise ValueError("rollback target cannot increase live level")
        self.state = target
        self.history.append((self.state, f"ROLLBACK:{reason}"))
        return self.state

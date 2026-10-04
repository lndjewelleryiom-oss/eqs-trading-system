"""Frozen EQS Cross-Asset Execution V1 compatibility surface.

Additive only: BrokerGateway, PersistentRuntimeStore and SHADOW behaviour remain
unchanged.  Adapters translate current Crypto objects into canonical V1 records
and fail closed when legacy state lacks required evidence.
"""

from .adapters import (
    CompatibilityMappingError,
    CryptoOrderIntentContext,
    connection_capability_from_gateway,
    execution_reconciliation_to_v1,
    order_request_to_order_intent,
    order_intent_to_submission_attempt_event,
    paper_fill_to_execution_event,
    reconciliation_observation_to_execution_event,
    paper_fill_to_reservation_settlement,
    submission_interlock_from_gateway,
    submission_result_to_execution_event,
    venue_order_to_execution_event,
)
from .models import (
    AssetClass,
    AuthorisationRef,
    Capability,
    ConnectionCapability,
    ExecutionEvent,
    ExecutionEventType,
    ExecutionMode,
    FeeDetail,
    FillDetail,
    InstrumentRef,
    InterlockIssuer,
    InterlockState,
    OrderIntent,
    OrderLeg,
    OrderSpec,
    OrderTypeV1,
    ReconciliationCounts,
    ReconciliationResult,
    ReconciliationStatus,
    ReservationRef,
    ReservationSettlement,
    Route,
    SettlementEvent,
    StrategyRef,
    SubmissionInterlock,
    Target,
    TimeInForce,
    UnresolvedItem,
    UnresolvedKind,
)

__all__ = [
    "AssetClass", "AuthorisationRef", "Capability", "ConnectionCapability",
    "ExecutionEvent", "ExecutionEventType", "ExecutionMode", "FeeDetail",
    "FillDetail", "InstrumentRef", "InterlockIssuer", "InterlockState",
    "OrderIntent", "OrderLeg", "OrderSpec", "OrderTypeV1",
    "ReconciliationCounts", "ReconciliationResult", "ReconciliationStatus",
    "ReservationRef", "ReservationSettlement", "Route", "SettlementEvent",
    "StrategyRef", "SubmissionInterlock", "Target", "TimeInForce",
    "UnresolvedItem", "UnresolvedKind", "CompatibilityMappingError",
    "CryptoOrderIntentContext", "connection_capability_from_gateway",
    "execution_reconciliation_to_v1", "order_request_to_order_intent",
    "order_intent_to_submission_attempt_event", "paper_fill_to_execution_event",
    "reconciliation_observation_to_execution_event",
    "paper_fill_to_reservation_settlement", "submission_interlock_from_gateway",
    "submission_result_to_execution_event", "venue_order_to_execution_event",
    "JournalTruthSource", "V1AuthorityError", "V1AuthorityVerification",
    "verify_nonlive_execution_authority", "ReservationSettlementError",
    "execution_event_to_reservation_settlement",
    "BlindExternalActionRetryError", "ExternalActionKind", "ExternalActionObservation",
    "ExternalActionProtocolError", "ExternalActionState", "ExternalObservationStatus",
    "SyntheticUnknownOutcomeProtocol", "deterministic_external_action_id",
]

from .runtime import JournalTruthSource, V1AuthorityError, V1AuthorityVerification, verify_nonlive_execution_authority

from .settlement import ReservationSettlementError, execution_event_to_reservation_settlement

from .external_action import (
    BlindExternalActionRetryError,
    ExternalActionKind,
    ExternalActionObservation,
    ExternalActionProtocolError,
    ExternalActionState,
    ExternalObservationStatus,
    SyntheticUnknownOutcomeProtocol,
    deterministic_external_action_id,
)

from .venue_adapter import (
    AccountStateSnapshot,
    AdapterError,
    AdapterErrorCategory,
    AdapterResult,
    BalanceSnapshot,
    CanonicalVenueAdapter,
    CanonicalVenueFee,
    CanonicalVenueFill,
    CanonicalVenueOrder,
    ConstraintSnapshot,
    ConstraintValidation,
    CryptoBrokerAdapterBridge,
    MarketSessionState,
    PositionSnapshot,
    SessionState,
    VenueOperationalState,
    VenueState,
    crypto_perpetual_definition_to_constraint_snapshot,
    validate_order_intent_against_constraints,
    normalize_adapter_exception,
)

__all__.extend([
    "AccountStateSnapshot", "AdapterError", "AdapterErrorCategory", "AdapterResult",
    "BalanceSnapshot", "CanonicalVenueAdapter", "CanonicalVenueFee", "CanonicalVenueFill",
    "CanonicalVenueOrder", "ConstraintSnapshot", "ConstraintValidation",
    "CryptoBrokerAdapterBridge", "MarketSessionState", "PositionSnapshot", "SessionState",
    "VenueOperationalState", "VenueState", "crypto_perpetual_definition_to_constraint_snapshot",
    "validate_order_intent_against_constraints", "normalize_adapter_exception",
])
from .pre_dispatch import V1PreDispatchError, V1PreDispatchVerification, verify_v1_pre_dispatch

__all__.extend(["V1PreDispatchError", "V1PreDispatchVerification", "verify_v1_pre_dispatch"])

from .private_reconciliation import (
    PrivateReadExpectation,
    PrivateReadReconciliationReport,
    PrivateTruthDomain,
    PrivateTruthObservation,
    PrivateTruthState,
    TradingCommissioningBlocked,
    assert_trading_capable_commissioning_ready,
    reconcile_private_read,
)

__all__.extend([
    "PrivateReadExpectation", "PrivateReadReconciliationReport", "PrivateTruthDomain",
    "PrivateTruthObservation", "PrivateTruthState", "TradingCommissioningBlocked",
    "assert_trading_capable_commissioning_ready", "reconcile_private_read",
])

from .drift import DriftStatus, DomainDrift, PrivateReadDriftReport, assess_private_read_drift, compare_private_read_domains

__all__.extend([
    "DriftStatus", "DomainDrift", "PrivateReadDriftReport", "assess_private_read_drift",
    "compare_private_read_domains",
])

from .watchdog import (
    ReconciliationCadencePolicy,
    ReconciliationWatchdogAssessment,
    ReconciliationWatchdogRecoveryContext,
    ReconciliationWatchdogRecoveryPhase,
    ReconciliationWatchdogStatus,
    advance_recovery_context,
    assess_reconciliation_watchdog,
    deterministic_policy_id,
    make_reconciliation_policy,
    reconciliation_due,
    recovery_context_from_payload,
)

__all__.extend([
    "ReconciliationCadencePolicy", "ReconciliationWatchdogAssessment",
    "ReconciliationWatchdogRecoveryContext", "ReconciliationWatchdogRecoveryPhase",
    "ReconciliationWatchdogStatus", "advance_recovery_context", "assess_reconciliation_watchdog",
    "deterministic_policy_id", "make_reconciliation_policy", "reconciliation_due",
    "recovery_context_from_payload",
])

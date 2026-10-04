# EQS-07 EXECUTION BASELINE V1

Classification: VERIFIED NON-LIVE BASELINE
Canonical parent tree SHA-256: f66e564c8aa24019f4bddfda5c7773258a25a2b823a674e8c38e0ef2605f4eab
Repository: C:\Users\lndje\Documents\EQS_Market_Terminal_V5_20260925\app
Git state: not a Git checkout; release identity is deterministic tree hash.
Safety state: broker submission CLOSED/REJECTED; live execution disabled.

## Existing architecture
1. Public venue data: real Bybit/OKX infrastructure feeds and replay exist; Binance public acceptance artifacts also exist.
2. PAPER: PersistentPaperShadowRuntime routes allowed OrderRequest objects to the paper engine with durable events/checkpoints, leasing, duplicate protection and crash recovery.
3. SHADOW: ShadowExecutionEngine uses BrokerGateway with venue_submission_enabled=False and rejects construction if submission is enabled.
4. Order schema: execution.models.OrderRequest contains strategy_id, symbol, side, quantity, order_type, decision_time, reference_price, optional limit_price/reduce_only and UUID order_id.
5. Persistence/idempotency: durable runtime store, deterministic order UUIDs in nonlive pipeline, event-chain verification, runtime leasing, duplicate order/bar protection, atomic recovery evidence.
6. Fill/order/position models: paper accounting plus VenueOrder/VenueAccountSnapshot; venue model is intentionally minimal.
7. Reconciliation: ExecutionReconciler compares expected open order IDs and positions against venue snapshot and HALTs on unknown/missing orders or position mismatch.
8. Venue abstraction: BrokerAdapter protocol is health/submit/cancel/account_snapshot only; no production venue-specific private adapter exists.
9. Terminal APIs: read-only runtime/market/accounting surfaces exist; no production trading control API is accepted.
10. Credentials/config: no production private/trading credential path is accepted in this baseline.
11. Submission-capable locations: BrokerGateway.submit -> BrokerAdapter.submit_order; runtime orchestrator calls its configured PAPER/SHADOW engine; nonlive pipeline calls runtime.submit_order.
12. Production submission: sealed acceptance says CLOSED/REJECTED and live_execution_enabled=false. No production exchange adapter is present.
13. Main gaps: canonical OrderIntent V1, external authority contract, deterministic order state machine, richer adapter contract, private-read adapters, venue validation, ambiguous-submit reconciliation, rate-limit/health model, execution telemetry/quality, testnet adapters, production interlock.
14. Integration dependencies: EQS-01 persistence/commissioning; EQS-05 PAPER/SHADOW qualification; EQS-06 capital/risk authority; EQS-08 lifecycle; EQS-09 global safety/kills; EQS-10 terminal schemas.

## Safety finding
Existing reconciliation can call adapter.cancel_order for unknown orders during containment. That behavior is safe only for a non-live double today. EQS-07 must make cancellation authority explicit before a real adapter is connected.

## Baseline verification
Targeted execution suite: 23 passed in 16.81s.
Canonical acceptance: 55 tests, deterministic two-run Bybit/OKX PAPER/SHADOW replay, all live gates closed.

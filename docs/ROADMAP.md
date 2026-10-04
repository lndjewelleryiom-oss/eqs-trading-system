# Dependency-Aware Implementation Roadmap

## Phase F0 — Governance and safety foundation
F0.1 repository, configuration and CI
F0.2 canonical database and audit schema
F0.3 strategy/deployment state machines
F0.4 authoritative risk policy interface
F0.5 immutable/tamper-evident audit path

Exit: live disabled by default; illegal strategy promotion blocked; fail-closed risk tests pass; audit integrity test passes.

## Phase F1 — Point-in-time data plane
Depends on F0.

F1.1 instrument master and calendars
F1.2 dataset contracts/lineage
F1.3 immutable raw capture
F1.4 normalized event schema with event/publish/available/receive timestamps
F1.5 revision handling and quality gates
F1.6 deterministic historical replay

Exit: deliberately leaked fixture is rejected and replay is deterministic.

## Phase F2 — Event-driven backtest and execution simulation
Depends on F1 + F0 risk interfaces.

F2.1 market clock/event bus
F2.2 order lifecycle and idempotency
F2.3 spread/commission/slippage model
F2.4 latency/partial-fill/reject model
F2.5 funding/borrow/financing/corporate actions
F2.6 accounting/P&L/reconciliation

Exit: synthetic scenarios have exact expected fills/cash/P&L and impossible fills are rejected.

## Phase F3 — Research/validation platform
Depends on F2.

F3.1 experiment manifests and reproducibility
F3.2 walk-forward/OOS framework
F3.3 purged CV/embargo utilities
F3.4 parameter and cost surfaces
F3.5 bootstrap/Monte Carlo
F3.6 Deflated Sharpe/PBO/reality checks
F3.7 scorecards/research reports

Exit: a deliberately overfit toy strategy is rejected while a controlled synthetic edge is recovered within expected confidence.

## Phase F4 — Regime, portfolio and degradation engines
Depends on F3.

F4.1 transparent regime baselines
F4.2 probabilistic regime classifier
F4.3 constrained portfolio allocator
F4.4 correlation/capacity/liquidity controls
F4.5 live-vs-expected monitoring
F4.6 drift/degradation state transitions

Exit: simulated regime change causes intended allocation reduction without breaching limits.

## Phase F5 — Paper, shadow and broker abstraction
Depends on F2-F4.

F5.1 normalized broker/exchange adapter interface
F5.2 paper engine
F5.3 shadow engine
F5.4 reconciliation and unknown-order containment
F5.5 fault injection and kill-switch acceptance

Exit: production path can run end-to-end with venue submission disabled and survives injected failures safely.

## Phase F6 — Alpha discovery and evolutionary research
Depends on F3-F5.

F6.1 hypothesis registry/queue
F6.2 anomaly detectors
F6.3 research-family trial budgets
F6.4 strategy factory templates across asset classes
F6.5 autonomous falsification/stress jobs
F6.6 candidate prioritization by evidence/novelty/diversification

Exit: system autonomously creates, tests, rejects/documents and queues new hypotheses without altering acceptance criteria post hoc.

## Phase F7 — Controlled live commissioning
Depends on all above plus external credentials/legal/venue setup.

F7.1 isolated trade-only credentials
F7.2 live authorization gate
F7.3 LIVE_1 minimum allocation
F7.4 automatic rollback/de-risk
F7.5 progressive LIVE_2/3/4 evidence gates

Exit: only after human-controlled authorization and objective staging evidence. No live activation is part of the current scaffold.

## Research Commissioning R1 — Real-market crypto perpetual data
Depends on F1-F6 research infrastructure. This phase does not alter F7 live-trading authorization.

R1.1 canonical crypto-perpetual schemas, public connector normalization, raw-first lineage and point-in-time acceptance
R1.2 live public-feed connectivity, venue-specific order-book bootstrap/resync, reconnect/rate-limit soak and historical backfill validation
R1.3 canonical research dataset build and deterministic replay against captured real-market data
R1.4 first bounded alpha-research campaign using only datasets that pass R1.2/R1.3

R1.1 exit: docs-backed connector fixtures and raw/PIT/sequence safeguards pass without credentials or live-order capability.

R1.2 exit: sustained live read-only feeds survive disconnect/gap/reconnect fault injection, historical backfills reconcile to documented venue semantics, and latency/completeness reports are retained as evidence.

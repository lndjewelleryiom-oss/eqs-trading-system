# Evolutionary Autonomous Quant Trading System

Institutional-style research and trading platform scaffold focused on survival, evidence quality, robustness, and auditable staged deployment.

## Safety posture

- Live trading is **disabled by default**.
- A strategy cannot jump directly from research/backtest to live capital.
- Risk decisions are authoritative and fail closed.
- Data records distinguish event time, publication time, and market-availability time.
- Every strategy transition is recorded.
- Every order decision is designed to be auditable.

## Foundation implemented in this scaffold

1. Canonical component architecture and dependency-aware roadmap.
2. PostgreSQL database schema for strategy registry, experiments, datasets, validation, deployment, risk, orders, fills, positions, and audit events.
3. Versioned Python domain models for strategy lifecycle, datasets, orders, and risk decisions.
4. Strategy lifecycle transition guardrails.
5. Fail-closed pre-trade risk engine with stale-data, kill-switch, order-size, gross-exposure, leverage, daily-loss, and drawdown checks.
6. Temporal data guard that rejects market observations unavailable at the simulated decision time.
7. Append-only audit writer with hash chaining.
8. Initial automated test suite.
9. Canonical implementation tracker with objective acceptance evidence.
10. Canonical F7 signed external-gate approval template with LIVE_1-only scope, validity, evidence and automatic-revocation controls.
11. Completed F7 TEST DATA example that is explicitly non-authorizing and regression-tested to remain outside objective gate evidence.
12. CI guard that rejects TEST DATA from objective evidence, signed approval references, and live-authorization fields before the repository test suite runs.
13. Versioned Git pre-commit hook that runs the same TEST DATA contamination guard locally before commits are created.
14. Versioned Git pre-push hook that reruns the same guard before refs are sent to a remote, protecting against `--no-verify` commit bypasses.
15. R1.1 crypto-perpetual read-only data foundation with canonical point-in-time schemas and Binance USD-M, Bybit Linear, and OKX SWAP normalizers.
16. Raw-first public market-data collector that stores exact bytes before parsing and links canonical events to immutable SHA-256 lineage.
17. R1.2 genuine live-feed acceptance evidence for Binance USD-M, Bybit Linear, and OKX SWAP, including reconnect/book recovery, REST reconciliation, latency measurement, persistent raw-capture accounting, deterministic hourly checkpoint mechanics, and a complete three-hour 1-minute backfill window. R1.2 remains TESTING because actual multi-hour wall-clock socket soak/hourly checkpoints, long-lived heartbeat/24h rollover, rate-limit/backoff, and a corrected sustained Binance bootstrap run remain open.

## Quick start

```bash
python -m pytest -q
```

## Activate the local Git safety guards

Run once in a Git checkout:

```bash
python scripts/install_git_hooks.py
python scripts/install_git_hooks.py --check
```

This configures `core.hooksPath=.githooks`. Both the versioned `pre-commit` and `pre-push` hooks invoke the same `quant_system.ci.test_data_evidence_guard` module used by CI. `pre-commit` rejects contaminated commits before creation; `pre-push` reruns the guard before refs are sent to a remote, including when a local commit was created with `--no-verify`. Both hooks fail closed if no Python interpreter is available, and the installer repairs executable bits for both hooks after archive extraction.

Live brokerage/exchange credentials are intentionally not part of this repository.

# Complete System Architecture

## Design principles

The platform is separated into four trust zones so that research code cannot directly command unrestricted capital:

1. **Research plane** — ingestion, feature engineering, hypothesis generation, backtesting, validation.
2. **Decision plane** — approved strategy packages, regime classification, portfolio construction, signal generation.
3. **Control plane** — authoritative risk engine, deployment state machine, secrets, approvals, kill switches.
4. **Execution plane** — broker/exchange adapters, smart order handling, reconciliation, positions, fills.

The control plane is deliberately independent from alpha models. A model can request an order; only risk can authorize it.

## Major components and dependencies

```text
Raw Sources
  -> Data Connectors
  -> Immutable Raw Store
  -> Normalization / Point-in-Time Layer
  -> Data Quality & Lineage
  -> Feature Store
        -> Regime Engine
        -> Hypothesis / Alpha Discovery
        -> Strategy Factory
        -> Backtest Engine
        -> Validation / Overfit Defence
        -> Strategy Registry
        -> Paper Trading
        -> Shadow Execution
        -> Deployment Controller
                         -> Portfolio Engine
                         -> Risk Engine (authoritative)
                         -> Execution Engine
                         -> Venue Adapters
                         -> Reconciliation
All components -> Metrics / Monitoring / Audit / Alerting
Performance + drift -> Evolution Engine -> Research Queue
```

## Production technology shape

- Python for research, data, portfolio logic and first execution implementations.
- Rust/C++ only where profiling proves latency needs it.
- PostgreSQL for canonical metadata, registry, controls and audit references.
- Columnar object storage (Parquet) for historical market/fundamental/alternative data.
- Kafka/Redpanda-class event transport once scale requires streaming decoupling.
- Redis-class ephemeral cache only for non-canonical low-latency state.
- Containerized services; one service identity per trust boundary.
- OpenTelemetry-compatible metrics/traces/logs.
- Secrets manager/KMS; never `.env` secrets committed to source control.

## Canonical services

### Data Engine
Connectors, raw immutable capture, normalization, symbology, calendars, corporate actions, point-in-time availability, revisions, quality scoring, lineage, replay.

### Feature Engine
Versioned feature definitions with strict event-time semantics. Every feature records source datasets, code version, lag policy and valid-from/valid-to times.

### Market Regime Engine
Probabilistic state classification. Initial models should include transparent baselines before HMM/clustering/change-point/ML approaches. Regime output includes probability and uncertainty, not only a label.

### Research Engine / Alpha Discovery
Maintains hypothesis queue. New work begins from a causal/economic mechanism, falsification criteria and expected failure modes. Search budget is tracked to quantify multiple testing.

### Strategy Factory
Compiles a research hypothesis into a versioned strategy specification and executable package. A strategy ID never changes; material modifications create a descendant/version.

### Backtest Engine
Event-driven simulation with a point-in-time data clock, execution simulator, fees, spreads, latency, impact, partial fills, borrow/funding/financing and order rejects. No same-bar fills unless the data and execution model make them possible.

### Validation Engine
OOS, walk-forward, purged CV/embargo where appropriate, bootstraps, Monte Carlo, parameter/cost/latency sensitivity, crisis/regime/universe tests, Deflated Sharpe and PBO/reality checks where statistically appropriate.

### Strategy Registry
Canonical lifecycle, lineage, test ledger, evidence, state changes and rejection reasons. Rejected strategies are not silently recycled.

### Paper / Shadow Engines
Paper uses actual observed market conditions; shadow produces real orders internally but blocks them before venue submission. Execution differences versus simulation are measured.

### Portfolio Engine
Combines validated alphas with uncertainty, covariance, liquidity, capacity and regime fitness. Uses constrained allocation; recent profits alone cannot increase weight.

### Risk Engine
Independent authoritative pre-trade and continuous risk. Fails closed. It can block orders, reduce allocations, pause strategies and halt the portfolio.

### Execution Engine
Idempotent order submission, smart order policy, venue routing, slicing, cancellation/retry, reconciliation and explicit handling of unknown states. Unknown order state triggers containment rather than blind retry.

### Monitoring / Evolution
Monitors realized edge, costs, fill quality, drift, regime, correlation, exposure and system integrity. Generates research tasks when degradation or new anomalies are detected.

## Trust boundaries

- Research workers: no trading API credentials.
- Strategy decision service: can create order intents only.
- Risk service: signs approved order intents.
- Execution service: accepts only risk-authorized intents.
- Venue keys: trade-only where possible, withdrawals disabled, scoped per venue/account.
- Human approval required before first live-capital activation and material limit expansion.

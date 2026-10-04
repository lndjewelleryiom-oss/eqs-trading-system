# Repository Structure

```text
evolutionary-quant-system/
├── config/                 # non-secret example configuration
├── docs/                   # architecture, risk, research, deployment and roadmap
├── migrations/             # canonical PostgreSQL schema migrations
├── src/quant_system/
│   ├── audit/              # tamper-evident decision/audit trail
│   ├── backtest/           # simulation contracts and later event engine
│   ├── core/               # enums, IDs, common domain rules
│   ├── data/               # point-in-time data contracts/quality/replay
│   ├── execution/          # orders, venue-neutral execution models/adapters
│   ├── portfolio/          # allocation/covariance/capacity controls
│   ├── registry/           # strategy lifecycle and evidence registry
│   ├── risk/               # authoritative pre-trade/continuous risk controls
│   └── research/           # experiment manifests, validation and research queue
└── tests/                  # unit, invariants, leakage, failure and acceptance tests
```

Planned top-level additions when the corresponding phases begin: `services/` for independently deployable control/execution services, `infra/` for container/IaC definitions, `notebooks/` for disposable exploration only, and `artifacts/` references for reproducible reports. Production logic must migrate out of notebooks before validation.

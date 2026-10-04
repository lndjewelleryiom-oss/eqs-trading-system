# Staged Deployment Plan

## Environments

- `research`: offline historical and synthetic data; no venue credentials.
- `paper`: real-time data; simulated orders/fills.
- `shadow`: production signal/risk/execution path; venue submission physically blocked.
- `staging`: broker/exchange sandbox or testnet where available.
- `live`: separate credentials, network policy and database controls.

## Strategy stages

### RESEARCH
Hypothesis and prototype only. No capital.

### BACKTEST / ROBUSTNESS / OOS
Acceptance criteria fixed before final holdout. No capital.

### PAPER
Real-time observations and execution simulation. Required minimum sample depends on horizon and trade count, not a fixed number of calendar days.

### SHADOW
Exercises production orchestration, risk and order construction. Orders are stopped before external submission.

### LIVE_1
Minimum allocation; strict limits; manual activation required. Automated reductions/halts enabled.

### LIVE_2
Limited allocation after execution and P&L behaviour remain within tolerance.

### LIVE_3
Validated allocation after sufficient live sample across relevant conditions.

### LIVE_4
Scaled within capacity and portfolio constraints. Not permanent: can degrade automatically.

## Advancement evidence

Each stage change must reference validation IDs, paper/shadow execution metrics, current risk policy version, current code version, observed slippage/cost confidence intervals, drift checks and explicit rollback conditions.

## Commissioning safety boundary

The broker-facing live path is bound to the commissioning controller. `LIVE_0` blocks the call before it reaches the broker adapter, even if a gateway is technically configured as submission-capable. No component in the commissioning layer provisions credentials or grants human authorization.

Runtime safety is asymmetric: critical integrity/risk failures immediately return the controller to `LIVE_0`; execution-quality or degradation warnings reduce one level. Any later scale-up must satisfy fresh evidence again.

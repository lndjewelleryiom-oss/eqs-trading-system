# Testing Framework

## Test layers

1. Unit tests — mathematics, state machines, timestamps, sizing, fee models.
2. Property/invariant tests — no negative quantity, conservation of cash/positions, idempotency.
3. Data tests — monotonic timestamps, point-in-time availability, duplicate detection, revisions, missing/corrupt flags.
4. Simulation tests — known synthetic market paths with expected fills/P&L.
5. Reconciliation tests — internal ledger versus broker/exchange statements.
6. Failure-injection tests — stale feeds, dropped acknowledgements, duplicate responses, venue outage, clock skew.
7. Backtest-bias tests — explicit look-ahead and survivorship fixtures that must fail.
8. Security tests — secret scanning, authorization boundaries, withdrawal-disabled policy evidence.
9. Staging acceptance — shadow order path, kill switch, circuit breakers, recovery.
10. Live commissioning checks — minimum notional, manual authorization, automatic rollback/reduction conditions.

## Definition of PASSED

A component can move to `PASSED` only when its acceptance criteria are machine-checkable where possible, test output/artifact is retained, no critical test is skipped, and evidence identifies the exact code/data version.

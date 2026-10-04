# EQS Read-Only Interface v2 — F5.6 Runtime Status

## Purpose

Interface v2 keeps the v1 command surface read-only and adds the first genuine runtime-status integration. It observes the durable F5.6 PAPER/SHADOW SQLite store without acquiring a runtime lease and without exposing any execution command.

## Runtime connection

Run against packaged/mock state:

```bash
python scripts/serve_readonly_interface.py
```

Run against a real F5.6 runtime store:

```bash
python scripts/serve_readonly_interface.py --runtime-db /path/to/runtime.db
```

The same path can be provided through `EQS_RUNTIME_DB`.

## Read-only enforcement

`RuntimeStoreReadOnlyReader` opens SQLite with `mode=ro` and `PRAGMA query_only=ON`. It does not instantiate `PersistentRuntimeStore`, claim/renew/release leases, append events, save checkpoints, alter journal settings, or create missing databases.

The HTTP surface remains GET-only. POST, PUT, PATCH and DELETE return HTTP 405 with `Allow: GET`.

## Status now sourced from the durable runtime

- PAPER / SHADOW runtime status and halt reason;
- generation and lease state/expiry;
- heartbeat/update timestamps;
- event count, contiguous event sequence and payload SHA-256 verification;
- latest checkpoint identity and SHA-256 verification;
- persisted runtime metrics and last market/reconciliation/degradation timestamps;
- PAPER cash, positions, realized P&L, pending orders and fills from the checkpoint;
- SHADOW decisions and persisted `sent` results;
- latest degradation assessment and reconciliation evidence;
- runtime integrity/read failures surfaced as fail-closed interface alerts.

## Values intentionally not inferred

The current F5.6 checkpoint does not persist live marks, therefore the interface does not fabricate PAPER equity, marks or unrealized P&L. It also does not persist an authoritative current risk-limit utilisation snapshot or the gateway's current `venue_submission_enabled` flag. Those values are displayed as unavailable/not persisted rather than replaced by demo values.

For SHADOW, the interface reports the persisted zero-submit observation (`sent=true` count) separately from the non-persisted gateway flag.

## Still prohibited

No route or UI control can place orders, enable LIVE, edit F7, change risk limits, access credentials, modify preregistered Alpha-v1 campaigns, or mutate immutable evidence.

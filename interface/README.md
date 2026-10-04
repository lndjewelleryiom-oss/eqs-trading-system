# EQS Read-Only Command Interface

This interface is observability-only. It exposes Command, Data, Alpha Research, Strategies, Risk, PAPER/SHADOW, Evidence and System Health without any mutation authority.

## Bootstrap / packaged evidence mode

```bash
python scripts/serve_readonly_interface.py
```

Open `http://127.0.0.1:8765`.

## F5.6 live runtime-status mode

```bash
python scripts/serve_readonly_interface.py --runtime-db /path/to/runtime.db
```

Or set `EQS_RUNTIME_DB` to the durable F5.6 runtime SQLite path. The database is opened with SQLite `mode=ro` and `PRAGMA query_only=ON`; no lease is acquired and no row is modified.

GET endpoints:

- `/api/status` — full interface snapshot;
- `/api/runtime/status` — runtime/risk/health subset;
- `/api/healthz` — interface adapter health.

POST, PUT, PATCH and DELETE are rejected with HTTP 405. There are no controls for broker submission, LIVE enablement, F7, risk-limit edits, credentials, preregistration edits or evidence mutation.

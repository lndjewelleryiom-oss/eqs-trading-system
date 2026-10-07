# EQS Read-Only Command Interface

This interface is observability-only. It exposes Command, Data, Alpha Research, Strategies, Risk, PAPER/SHADOW, Evidence and System Health without any mutation authority.

## Desktop and mobile surfaces

The full operator terminal is served at `/index.html`. A dedicated phone-first observer is always shipped with the interface at `/mobile.html` and reads the same `/api/dashboard` source. The full terminal also retains responsive breakpoints for tablet and mobile widths.

The mobile observer auto-refreshes while visible and shows system health, runtime connectivity, strategy lifecycle state, the supervisor-selected next action, managed-paper progress, alerts/blockers and runtime heartbeat state. It deliberately exposes no mutation controls.

`/mobile.webmanifest` makes the mobile observer installable when the interface is delivered over a secure origin.

Mobile support is part of the interface contract. Changes to the operator interface must keep the dedicated mobile surface, the responsive viewport declaration and the read-only authority boundary intact.

## Bootstrap / packaged evidence mode

```bash
python scripts/serve_readonly_interface.py
```

Open `http://127.0.0.1:8765` for the full terminal or `http://127.0.0.1:8765/mobile.html` for the mobile observer.

## F5.6 live runtime-status mode

```bash
python scripts/serve_readonly_interface.py --runtime-db /path/to/runtime.db
```

Or set `EQS_RUNTIME_DB` to the durable F5.6 runtime SQLite path. The database is opened with SQLite `mode=ro` and `PRAGMA query_only=ON`; no lease is acquired and no row is modified.

## Private phone access

The server defaults to loopback and must not be published directly to the open internet. For access away from the host computer, expose the observer only through a private authenticated network or HTTPS reverse proxy/VPN. Bind `--host` only to the private interface used by that access layer. The mobile page does not require any write-capable API.

GET endpoints:

- `/api/dashboard` — current operator dashboard snapshot used by the desktop and mobile surfaces;
- `/api/status` — full interface snapshot;
- `/api/runtime/status` — runtime/risk/health subset;
- `/api/healthz` — interface adapter health.

POST, PUT, PATCH and DELETE are rejected with HTTP 405. There are no controls for broker submission, LIVE enablement, F7, risk-limit edits, credentials, preregistration edits or evidence mutation.

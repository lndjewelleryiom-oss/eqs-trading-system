# EQS Operator Dashboard v3 — internal build

Built 23 September 2026. **Internal review build; not deployed to production.**

## Open it

Open **EQS-Interactive-Preview.html** in a desktop or mobile browser. This is a self-contained, interactive saved snapshot with no external assets and no runtime connection. Navigation, inspection drawers, chart controls, search, sort and keyboard palette work. Its persistent SAVED PREVIEW / SAVED PACKAGE SNAPSHOT labels distinguish it from a running observer. Refresh reopens the saved snapshot; it does not update market data.

For the actual observer, extract **EQS-Operator-Dashboard-v3.zip**, enter `evolutionary-quant-system`, and run with Python 3.12 or newer:

```bash
python scripts/serve_readonly_interface.py
```

Open `http://127.0.0.1:8765` on that machine. No runtime database is required to inspect packaged evidence.

To read the real F5.6 runtime later:

```bash
python scripts/serve_readonly_interface.py --runtime-db "/absolute/path/to/runtime.db"
```

On Windows, use the actual Windows path inside quotes. `EQS_RUNTIME_DB` remains supported. No database is created if the file is missing. No runtime lease is taken. This is a local review server, not a public production host.

## Implemented

- Graphite/navy terminal, compact navigation, persistent source/mode/status strip, mobile monitoring layout, focus states and reduced-motion support.
- Overview, Markets & Data, Research, Strategies, PAPER/SHADOW Runtime, Risk, Jobs & Activity, Evidence & Diagnostics.
- Real archived trade chart with venue selection, capture-range selection, keyboard-accessible point inspection and source links. The capture is labelled 22 September 2026; no invented price or equity history.
- Priority exceptions and evidence drawers; preregistration/gate drill-down; table search, filters, sort and density; Control/Command K palette; R refresh; Escape dismiss.
- Bounded F5.6 observation of cash, positions, pending orders, fills, commissions, simulated slippage, runtime events, heartbeats, leases, degradation and reconciliation. Scope is the most recently updated persisted runtime for each mode, not a combined portfolio.
- Missing/incompatible/corrupt/oversized source states remain unavailable. Network failure preserves last-known values and their response time. Browser fetch time does not change upstream source timestamps.
- Critical status updates remain visible in the source strip. Open investigations are pinned to their captured evidence rather than being replaced underneath the operator.

## Source inventory

| Panel | Actual source in this delivery | Truthful limits |
|---|---|---|
| Market chart / partitions | Saved R1.3 real-market acceptance capture: 39 observations, nine partitions, 30 trades across Binance, Bybit and OKX | Acceptance slice, not current prices or continuous historical coverage |
| Integrity / raw lineage | Partition bytes rehashed; row counts checked; packaged acceptance report | Raw lineage is reported capture-host evidence. No fresh raw-file audit; gap, duplicate and exchange-sequence audits unavailable |
| Research | Six code-defined preregistration contracts and packaged A01–A28 baseline statuses | No current remote results, empirical sample counts, OOS returns or robustness results |
| Strategies | Explicit disconnected current-registry state | The saved baseline has no selected strategies; not a claim about the latest workstation |
| PAPER / SHADOW / risk / activity | Compatible F5.6 SQLite adapter | Actual workstation database absent here; populated views verified with explicitly labelled TEST DATA |
| Equity / drawdown / exposure | Explicit unavailable state | Runtime lacks current market marks, timestamped equity curve, valuation currency and current limit utilisation |
| Agents / worker tasks / schedules | Explicit unavailable state | Runtime records are not relabelled as live agents; creation time is not claimed as current-task start time |
| Preview | Embedded real archival snapshot | No demo balances, fabricated live activity or automatic production connection |

The UI consumes `GET /api/dashboard`, a new `eqs-operator-v3` presentation contract. Legacy `/api/status` and `/api/runtime/status` remain for compatibility with Interface v2. Their original bootstrap mode is explicitly marked MOCK when no database is configured; v3 never consumes those bootstrap values. Route only the v3 endpoint for a future production frontend, or retire the compatibility routes during commissioning.

## Verification evidence

- Full Python regression: **279 passed; 1 existing intentional cross-process skip**.
- Browser checks: all eight workspaces, source drawers, A18 history blocker, chart controls, keyboard point selection, command palette, Escape, disconnect/recovery and no JavaScript exceptions.
- Layout checks: 390, 768, 1440 and 2560px widths; no whole-page horizontal overflow. Dense tables scroll within their containers.
- Populated runtime browser checks: cash, positions, pending orders, fills, costs, SHADOW invariant, runtime activity, stale heartbeat, reconciliation and phone-width overflow. Synthetic data carried a persistent DEMO / TEST DATA banner.
- Read-only test: runtime file SHA-256 unchanged before/after inspection. Missing database not created; incompatible schema and excessive log size fail closed.
- Baseline archive verified: `0a3aad3df5d8c28e83d107603a79c06872d144ad50f44ae6037fe1804a132b33`.
- **342 existing non-cache files byte-identical**; seven existing interface/test files changed; no unexpected source changes. Added presentation adapter and its tests. Execution, Alpha-v1 and F7 source/evidence remain unchanged. ZIP-extracted safety-hook executable permissions restored.

The desktop/mobile screenshots show the actual packaged-evidence view. `EQS-Runtime-TEST-DATA.png` is explicitly a populated-state test screenshot, not operational evidence.

## Read limits and failure behaviour

Database connections use SQLite `mode=ro`, `PRAGMA query_only=ON` and a read transaction. There is a one-second SQLite instruction budget, maximum 64 runtimes, maximum 10,000 fully checked events per runtime, and a four-million-character payload ceiling. Event histories are not silently truncated and then declared verified: exceeding the verification budget makes runtime status unavailable. The UI stream is limited to the latest 100 observations. Table views display at most 200 matching rows. API polling is non-overlapping at 15 seconds, with an eight-second browser timeout. The displayed heartbeat-stale threshold is 60 seconds and is not a trading risk limit.

Large production stores need a dedicated read replica/summary service with incremental integrity verification. This review implementation does not claim load qualification for an unbounded production history.

## Before production

1. Reconcile this interface branch with the latest authoritative EQS master. This build uses the verified saved Interface v2 archive; it does not include newer workstation changes from other chats.
2. Supply the canonical F5.6 store read-only and verify every live field against its source, including multi-runtime selection and stale/disconnected states.
3. Connect versioned current dataset, ingestion, gap/duplicate/sequence/lineage reports and research/strategy registries. The existing archived panel must remain clearly historical until then.
4. Provide explicit account/portfolio identity, currency, market marks, cost policy and a timestamped equity history before enabling performance, drawdown, benchmark or marked-exposure charts.
5. Supply actual worker/task/schedule/heartbeat contracts for agent and job monitoring.
6. Add authenticated HTTPS hosting, access control, read-only service permissions and an appropriately bounded read service. The included Python local server is not the production hosting layer.
7. Run commissioning against production-shaped data, including permissions, oversized stores, corruption, stale clocks, missing source fields, recovery, and monitoring. Roll out the observer separately from any execution authorisation.

No production deployment, live trading enablement, strategy promotion, order placement, risk change or F7 approval was performed.

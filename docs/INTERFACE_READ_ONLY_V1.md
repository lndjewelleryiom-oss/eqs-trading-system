# EQS Read-Only Command Interface v1

## Purpose

The interface is an observational command plane for the current EQS master. It intentionally exposes no mutation authority.

Initial areas:

- Command dashboard — overall state, blockers, runs, health and authority boundary.
- Data — venues, genuine R1.3 manifest metrics, PIT universe, lineage, historical-population readiness and throughput.
- Alpha Research — six frozen Alpha-v1 campaigns, A01–A28 status, sample/fold readiness and research stage.
- Strategies — candidate/survivor registry, intentionally empty until legitimate empirical results exist.
- Risk — read-only limits, utilisation, degradation and HALT/PAUSE conditions.
- PAPER / SHADOW — demo PAPER observability and SHADOW hard zero-submit state plus verified infrastructure acceptance.
- Evidence — immutable fingerprints and packaged acceptance trail.
- System health — feeds, leases, restart recovery, persistence, reconciliation and alerts.

## Safety boundary

The browser has no controls for order placement, LIVE enablement, F7 changes, campaign edits, risk-limit edits, credential access or evidence mutation. The HTTP server accepts GET only. POST, PUT, PATCH and DELETE return 405.

## Data classification

The bootstrap adapter is `MOCK_READ_ONLY_ADAPTER`. It uses genuine packaged R1.3/Alpha/non-live evidence for canonical identities and acceptance facts, plus clearly labelled mock runtime/risk/PAPER values to make the interface useful while backend status contracts stabilize.

No mock runtime value is research evidence, and no profitability claim is made.

## Run locally

```bash
python scripts/serve_readonly_interface.py
```

Then open `http://127.0.0.1:8765`.

## Adapter contract

The frontend consumes `eqs-interface-read-model-v1` via `GET /api/status`. The Python seam is `ReadOnlyEqsAdapter.snapshot()`. A future real adapter can replace `MockReadOnlyEqsAdapter` without changing the command surface.

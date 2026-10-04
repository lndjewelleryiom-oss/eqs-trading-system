# R1.2 Crypto Perpetual Live-Feed Acceptance

## Status

`TESTING` as of 2026-09-22. The short live acceptance passed on Binance USD-M, Bybit Linear, and OKX SWAP. Full R1.2 remains open because the canonical gate also requires multi-hour/day soak, heartbeat/rate-limit behavior, sustained raw-capture throughput, and longer-horizon historical backfill completeness.

## Live run

The acceptance ran from an internet-connected, temporary read-only execution harness. No API key, account credential, trade endpoint, private websocket, or capital was used. The runner was decommissioned after evidence capture.

Instrument set: BTCUSDT on Binance USD-M and Bybit Linear; BTC-USDT-SWAP on OKX.

Window: `2026-09-22T18:39:32.296Z` to `2026-09-22T18:39:46.377Z`. Venue jobs ran concurrently, so this is a short connection/recovery test rather than a sustained soak.

## Acceptance results

| Venue | Reconnect / book recovery | Required live channels | WS→REST trade reconciliation | Adjusted message age |
|---|---|---|---|---|
| Binance USD-M | PASS — REST snapshot bridge found twice; `pu` continuity held; uncrossed book | PASS — 40 aggTrade pushes/trades and 4 mark-price pushes | PASS — 40/40 observed WS aggregate-trade IDs found in 1000-row REST window | n=44; p50 110 ms; p95 147 ms; max 156 ms |
| Bybit Linear | PASS — fresh snapshot after both connections; update IDs monotonic; uncrossed book | PASS — 27 publicTrade pushes containing 69 trades; 44 ticker pushes | PASS — 69/69 observed WS trade IDs found in 1000-row REST window | n=113; p50 87.5 ms; p95 854.5 ms; max 2055.5 ms |
| OKX SWAP | PASS — fresh snapshot after both connections; `seqId`/`prevSeqId` continuity held; uncrossed book | PASS — trades 21, mark-price 24, funding-rate 2, open-interest 3 | PASS — 19/20 observed WS trade IDs found in 500-row REST window | n=46; p50 165 ms; p95 171 ms; max 25,735 ms |

The OKX maximum is not interpreted as pure network transit latency. The measurement is venue message timestamp to local receipt after a midpoint clock-offset estimate; slower-changing state channels can therefore include publication/state staleness. The p50/p95 values are more representative of the short sampled window.

Sparse forced-liquidation/liquidation feeds were subscribed where part of the R1.1 plan, but no liquidation event was required to occur during the short acceptance window. Absence of a liquidation print is not a completeness failure for this test.

## Book recovery procedure

Binance follows the venue-specific bootstrap sequence: begin buffering depth updates, fetch `/fapi/v1/depth`, discard obsolete events, find the bridging event around `lastUpdateId`, then require `pu` to equal the preceding `u`. The procedure was repeated after a deliberate socket close/reconnect.

Bybit and OKX send order-book snapshots on subscription. The test deliberately disconnected and reconnected, required a fresh snapshot, rebuilt the local book, checked update-sequence behavior, and required best bid < best ask after reconstruction.

## Backfill reconciliation

This acceptance validates near-real-time reconciliation, not full historical research backfill. For each venue, live trade identifiers were captured from websocket traffic, then compared against the venue's public REST recent-trade/aggregate-trade endpoint. All Binance and Bybit captured IDs overlapped; OKX overlapped 19 of 20, consistent with one websocket trade arriving outside the REST snapshot timing boundary.

## Remaining R1.2 exit work

R1.2 must remain `TESTING` until a longer-running collector proves multi-hour/day stability, venue heartbeat and forced-rollover/reconnect behavior, controlled rate-limit/backoff behavior, sustained raw-first capture with measurable drop/storage throughput, and longer-horizon historical backfill completeness.

Raw captured metrics: `artifacts/test-evidence/R1_2_LIVE_ACCEPTANCE_2026-09-22.json`.
Reproducible temporary runner source: `scripts/r1_2_live_acceptance_edge.ts`.

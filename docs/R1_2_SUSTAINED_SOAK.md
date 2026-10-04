# R1.2 Sustained Soak Collector — 2026-09-22

## Scope

This checkpoint exercised genuine public Binance USD-M, Bybit Linear and OKX SWAP data without credentials or trading capability. It adds persistent raw-capture, explicit drop accounting, controlled disconnect/reconnect injection and a three-hour historical backfill check to the earlier short-live acceptance.

It does **not** claim that several hours of live WebSocket wall-clock time elapsed. The runtime available to this synchronous acceptance cannot manufacture elapsed time; the canonical R1.2 gate therefore remains `TESTING`.

## Persistent raw capture

An isolated staging table captured every raw WebSocket frame from the sustained mechanics run before evidence extraction. Results:

- received frames: 1,366
- persisted rows: 1,366
- persistence failures: 0
- persisted bytes: 797,828
- rows whose stored raw payload independently re-hashed to the recorded SHA-256: 1,366/1,366
- Binance: 416 rows / 294,435 bytes
- Bybit: 483 rows / 199,156 bytes
- OKX: 467 rows / 304,237 bytes

The temporary database capture is an acceptance fixture, not the production research datastore. The repository's durable contract is implemented in `quant_system.data.crypto_perps.soak.PersistentRawCapture`.

## Failure injection and resynchronization

The live runner deliberately closed public sockets and opened fresh sessions. Bybit and OKX each produced fresh book snapshots on both sessions and maintained valid local-book state without detected sequence gaps or crossed books.

The original Binance sustained subtest is retained as a failed harness result. Its REST depth snapshot was requested after its buffered depth socket session had closed, so it could not establish a valid sequence bridge and reported two artificial gaps. Those two gaps are **not** attributed to Binance data loss.

A corrected genuine Binance probe was then executed from the connected workstation using the proper ordering: open depth socket, buffer updates, request the REST snapshot while the socket remains open, bridge the snapshot to buffered updates, force a disconnect, reconnect, and repeat. Both cycles found the bridge, applied 25 updates, recorded zero sequence gaps, remained uncrossed, and persisted 71/71 received frames with zero parse or persistence failures. Evidence: `artifacts/test-evidence/R1_2_BINANCE_CORRECTED_RECONNECT_2026-09-22.json`.

Failed acceptance work remains retained alongside the corrected result rather than rewritten to appear green.

## Drop accounting

The collector distinguishes:

- socket frames received;
- raw records successfully persisted;
- parsing failures;
- persistence failures;
- sequence-integrity gaps;
- deliberate disconnects;
- reconnect transitions; and
- order-book resynchronizations.

A zero count is only claimed for observable categories. Network loss on channels without complete venue sequence coverage is explicitly `unknown`, not silently assumed to be zero.

## Three-hour historical backfill reconciliation

The most recent three hours of completed one-minute bars were reconciled for BTC perpetuals:

| Venue | Expected | Observed | Missing | Duplicates | Result |
|---|---:|---:|---:|---:|---|
| Binance USD-M | 180 | 180 | 0 | 0 | PASS |
| Bybit Linear | 180 | 180 | 0 | 0 | PASS |
| OKX SWAP | 180 | 180 | 0 | 0 | PASS |

This closes the previous near-real-time-only backfill limitation for a three-hour interval. It is not yet a multi-day/month research-history audit.

## Hourly checkpoints

The production checkpoint scheduler defaults to one-hour intervals. Deterministic tests advance a timezone-aware clock across four hours and prove checkpoints are emitted exactly at +1h, +2h, +3h and +4h, with unhealthy states when persistence or sequence gaps exist.

No actual one-hour wall-clock checkpoint had completed at this update. A real hourly condition watch is configured to inspect the remote read-only collector, restart it if absent when the workstation is reachable, and accept a checkpoint only when its recorded elapsed time is at least 3,600 real seconds. The collector was subsequently confirmed running from `2026-09-22T19:49:38.321Z`; at a pre-hour observation it had processed 2,125/2,125 persisted frames, 0 parse failures, 0 persistence failures, 0 sequence gaps, 1 forced disconnect/reconnect and 3 successful resynchronizations. The raw capture file had grown to 14,468,964 bytes. Because less than one real hour had elapsed, the accepted live-hour count remains **0** and the requirement remains open.

## Remaining R1.2 blockers

1. Actual multi-hour/day uninterrupted live WebSocket runtime with real hourly checkpoints.
2. Long-lived heartbeat handling and Binance's documented 24-hour connection rollover.
3. Controlled rate-limit/backoff testing.
4. Long-duration storage growth/throughput/drop-rate measurements.

R1.2 therefore remains `TESTING`.

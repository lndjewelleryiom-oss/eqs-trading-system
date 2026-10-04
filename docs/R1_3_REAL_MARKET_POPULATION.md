# R1.3 Genuine Real-Market Population and Replay Acceptance

## Scope

This workstream populates the existing R1.3 immutable research-dataset layer from genuine read-only public market captures for Binance USD-M, Bybit Linear and OKX SWAP. It does not perform strategy selection, profitability research, broker submission, authenticated exchange access, F7 changes or live-capital activity.

## Genuine source evidence

The preserved capture bundle is `artifacts/real-market/r1_3_2026-09-22/public_capture_source_bundle.json`, SHA-256 `5c08b7bb196f4b82df7c113bf9ae61799e7e478a90b1c05aa3540f7accbb734d`. It was derived from 15 successful public HTTP responses captured on 2026-09-22. Each receipt records venue, public URL, raw filename, local availability/receipt timestamps, exact raw byte count and raw SHA-256.

The exact raw response files remain preserved read-only on the capture host under the path recorded inside the bundle. `raw_integrity_acceptance.json` records an independent re-hash of all 15 originals and a copy-only tamper probe. The original raw files were not changed by the tamper test.

The R1.2 artifacts available to this workstream contained acceptance/soak summaries and checkpoints, but no complete raw response/backfill payload bundle suitable for R1.3 ingestion. Those R1.2 artifacts remain read-only and were not substituted for missing raw bytes.

## Normalization and clock-skew policy

`real_market_population.py` converts the captured public REST observations into the existing R1.3 `TradeEvent`, `BookUpdate`, `PerpetualStateEvent` and `PerpetualInstrumentDefinition` contracts. Every normalized event/definition carries the SHA-256 of its exact source HTTP response.

The capture host clock was behind several venue timestamps by up to a few seconds. R1.3 never shifts venue data backward in time to make it appear knowable earlier. Where a venue publication timestamp is later than the local capture timestamp, `available_at` and `received_at` are conservatively clamped forward to the venue publication boundary. Every correction is emitted into `clock_corrections.json` and the acceptance report.

## Materialized R1.3 evidence

Running `scripts/r1_3_real_market_acceptance.py` creates `artifacts/real-market/r1_3_2026-09-22/materialized/` containing:

- nine immutable content-addressed JSONL partitions spanning the three venues and the TRADE, BOOK_SNAPSHOT and PERPETUAL_STATE event families;
- a genuine point-in-time instrument-universe artifact;
- a genuine `ResearchDatasetManifest` stored by deterministic fingerprint;
- a replay/manifest reproducibility comparison from an independently reordered second build;
- partition-tamper detection evidence;
- raw-integrity linkage to the independent capture-host re-hash/tamper probe;
- measured partition write/storage throughput.

## Listing, delisting and revision evidence boundary

The three genuine instrument snapshots include real listing/launch timestamps and current active states. Acceptance proves they are unknown to R1.3 before the snapshot itself became available, become active once both effective time and availability permit, and are retained with raw source lineage.

No genuine historical delisting response or genuine historical instrument-definition revision sequence for these three BTC perpetual instruments was present in the supplied evidence. Those cases are therefore explicitly recorded as missing genuine raw evidence. Existing deterministic unit fixtures continue to test the R1.3 delisting/revision contract, but they remain TEST DATA and are not represented as REAL MARKET evidence.

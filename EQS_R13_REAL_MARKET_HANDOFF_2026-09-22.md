# EQS R1.3 Genuine Real-Market Population + Replay Handoff — 2026-09-22

## Baseline

Source master: `evolutionary-quant-system-master-r1.3-feature-f5.6-adapter.zip`

Verified source SHA-256: `51d1a3cf2eb6d970f5963e1b11bc5e6e96cb1d20a7fedfcd0ccc272fbeafa146`

The source master archive was not overwritten. All work in this handoff is a separate derived R1.3 real-market integration artifact.

## Workstream delivered

Added a non-live R1.3 genuine-market population path for preserved, read-only public REST captures from Binance USD-M, Bybit Linear and OKX SWAP. The path normalizes genuine observations into the existing R1.3 crypto-perpetual contracts, preserves raw SHA-256/source lineage, writes immutable content-addressed partitions, builds a genuine `ResearchDatasetManifest`, assembles PIT datasets, and replays them deterministically.

No broker submission, credentials, authenticated exchange APIs, F7 changes, live capital, or alpha selection were used.

## Genuine source evidence

- Public read-only responses: **15/15 HTTP 200**.
- Compact source bundle SHA-256: `5c08b7bb196f4b82df7c113bf9ae61799e7e478a90b1c05aa3540f7accbb734d`.
- Raw-integrity evidence SHA-256: `6f6c57dbec5567bd762f07131bd6674f1ba98ee319d2708ad53be575b3bb2595`.
- All 15 exact preserved raw responses re-hashed successfully on the capture host.
- A copy-only raw tamper probe changed the digest and was detected; preserved originals remained unchanged.
- Each normalized definition/event retains the corresponding raw source SHA-256.

The available preserved R1.2 artifacts were acceptance/soak summaries and checkpoints, not a complete raw-response/backfill bundle suitable for R1.3 ingestion. They were kept read-only and were **not** substituted for missing raw data.

## R1.3 materialized dataset

- Dataset ID: `crypto-perps-normalized-v1`.
- Genuine normalized events: **39**.
- Immutable partitions: **9**.
- Partition bytes: **25,392**.
- Active PIT instruments at the dataset decision boundary:
  - `BTC-USDT-PERP:BINANCE_USDM`
  - `BTC-USDT-PERP:BYBIT_LINEAR`
  - `BTC-USDT-PERP:OKX_SWAP`
- Universe fingerprint: `236009152ebffc20a6c59ef65662298c593f716d0d642a3aa8d7cfddb57066e3`.
- ResearchDatasetManifest fingerprint: `1db543b2881321a4e453915258f3d08926d0b8b066f4fde2c09d82e8a5d4016c`.
- Replay fingerprint: `fba9ee4f70e0e5d8c937a0ac4ede37227e6bd0beb6d67720509504845c50e0ee`.

A second build with reversed source-event and partition enumeration produced the **same manifest fingerprint** and the **same replay fingerprint**.

## PIT membership / listing / revision semantics

The genuine instrument snapshots carry exchange listing/launch timestamps and raw-source lineage. R1.3 correctly treats each definition as unknown immediately before its capture availability and active once both effective listing time and captured availability permit it.

The capture host clock lagged some venue publication timestamps. To preserve no-lookahead semantics, the population adapter applies a conservative **forward-only** availability clamp and records every correction. **21** corrections are materialized in `clock_corrections.json`; no observation is moved backward to manufacture earlier availability.

No genuine historical delisting response or historical instrument-definition revision sequence was present for the three current BTC perpetual instruments. Those missing genuine captures are explicitly documented. Existing fixture tests continue to verify the semantic code paths, but they remain **TEST DATA** and are not represented as REAL MARKET evidence.

## Tamper / throughput acceptance

- Partition tamper detection: **PASS**, fail-closed on hash mismatch.
- Raw tamper detection: **PASS**, copy-only mutation detected and originals unchanged.
- Raw-SHA lineage completeness: **PASS**.
- Latest repeated partition-write measurement: **~3,383 events/s** and **~2.20 MB/s** over 25 iterations / 975 event writes / 634,800 partition bytes. This is an acceptance-sample infrastructure measurement, not a production capacity claim.

## Validation gates

- Targeted R1.3 real-market + R1.3 core + R1.3→Feature integration: **30 passed, 1 intentional worker skip**.
- Full repository regression with external pytest plugin autoload disabled: **227 passed, 1 intentional worker skip**.
- Protected commissioning/F7/broker-boundary subset: **21 passed**.
- `compileall`: **PASS**.
- TEST DATA live-evidence guard: **PASS**.
- Protected-boundary hash comparison: **35/35 unchanged — PASS_IDENTICAL**.

## Code/data delta

Implementation changes are limited to:

- `src/quant_system/data/crypto_perps/real_market_population.py`
- `src/quant_system/data/crypto_perps/__init__.py`
- `scripts/r1_3_real_market_acceptance.py`
- `tests/test_r1_3_real_market_population.py`
- `docs/R1_3_REAL_MARKET_POPULATION.md`
- separate `artifacts/real-market/r1_3_2026-09-22/` genuine evidence/materialization
- separate `artifacts/test-evidence/r1_3-real-market-*` acceptance evidence
- this handoff document

Protected R1.2, F7, commissioning and tracker files remained byte-identical to the verified master baseline.

## Safety / protected boundaries

- F7 modified: **NO**
- R1.2 evidence modified: **NO**
- `docs/TRACKER.md` modified: **NO**
- `tracker.json` modified: **NO**
- authenticated trading endpoints used: **NO**
- credentials accessed: **NO**
- broker submission enabled: **NO**
- live capital touched: **NO**
- alpha selection run: **NO**

## Acceptance status

**R1.3 genuine current-market population and deterministic replay acceptance: PASS for the captured scope.**

The remaining evidence gaps are genuine historical delisting/revision captures and a complete preserved R1.2 raw-response/backfill bundle. They are documented as missing rather than replaced with fixtures.

This handoff is infrastructure evidence only. It makes no claim of alpha, profitability, live-trading readiness, or authorization to submit orders.

EQS HANDOFF READY

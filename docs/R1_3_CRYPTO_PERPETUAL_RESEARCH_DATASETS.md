# R1.3 — Canonical Crypto-Perpetual Research Datasets

## Scope

R1.3 adds the deterministic historical research-data layer above the R1.1 normalized crypto-perpetual contracts. It does not modify R1.2 live-soak evidence, F7 commissioning controls, broker submission, credentials, or live capital paths.

The implementation is in `src/quant_system/data/crypto_perps/research_datasets.py` and provides:

- canonical serialization/deserialization for trade, book, perpetual-state and liquidation events;
- immutable content-addressed historical partitions;
- deterministic partition descriptors and research dataset manifests;
- point-in-time instrument-universe history;
- point-in-time dataset assembly that excludes unavailable/future observations;
- survivorship-safe historical membership decisions based on what was known when each observation became available;
- deterministic manifest-bound historical replay;
- integrity checks for partitions, universe histories and manifests.

## Historical storage contract

The first production-neutral storage format is deterministic UTF-8 JSONL. It is dependency-free and content-addressed, and its directory layout is compatible with local filesystems or object-storage key spaces:

```text
dataset=<dataset_id>/
  venue=<venue>/
    instrument=<instrument_id>/
      date=<UTC YYYY-MM-DD>/
        kind=<event kind>/
          part-<sha256>.jsonl
```

Rows inside each partition are sorted by the canonical replay key before serialization. Decimal values are serialized as exact decimal strings rather than binary floats. A partition path is derived from the SHA-256 of its exact canonical bytes; an existing path with different bytes fails closed.

Partition descriptors bind the dataset/venue/instrument/date/kind key, schema version, row count, content hash and event/availability timestamp bounds. Reads re-hash content and revalidate row count, partition key, schema, canonical ordering and timestamp bounds.

## Instrument-universe history

`InstrumentUniverseHistory` stores all point-in-time instrument definitions and deterministically fingerprints their canonical records. Queries select only definitions whose metadata was available and whose effective time had begun at the requested decision time.

Historical event membership is evaluated using the event's own `available_at`, not the final dataset assembly time. This deliberately retains an instrument that was legitimately active at the time even if it is known to be delisted later, preventing survivorship bias. An event that occurred before a future listing's `effective_from` is excluded even if it was received after that effective time.

## Point-in-time assembly

`PointInTimeDatasetAssembler` receives immutable partition descriptors, a universe history and a decision time. It:

1. ignores partitions that could not contain any observation available by the decision time;
2. verifies each selected partition before use;
3. excludes individual rows with `available_at > decision_time`;
4. applies the optional historical start bound;
5. applies point-in-time instrument membership using only information available with each event;
6. de-duplicates identical canonical source-event identities across partition inventories;
7. sorts the final event set using the deterministic replay key;
8. produces a manifest containing source partition descriptors, universe fingerprint, event count and event-identity hash.

No event is made historically usable merely because it was retrieved later. The R1.1 `available_at` contract remains the first-known boundary.

## Deterministic manifests

`ResearchDatasetManifest.fingerprint()` hashes only deterministic inputs:

- dataset identifier;
- R1.3 format version;
- point-in-time decision/start bounds;
- sorted immutable partition descriptors;
- instrument-universe fingerprint;
- final event count;
- final ordered event-identity hash.

Wall-clock manifest creation time is intentionally excluded, so equivalent inputs produce byte-identical records and the same manifest fingerprint. `ResearchManifestCatalog` stores manifests by fingerprint and verifies the filename/content identity on read.

## Deterministic replay

`DeterministicCryptoPerpReplay` accepts only an assembled event set whose event count and ordered identity hash match its manifest. Replay cannot advance beyond the manifest's decision-time boundary. Every yielded event reasserts the R1.1 point-in-time availability rule.

## Acceptance evidence

R1.3 targeted acceptance covers:

- all four normalized event families round-trip without loss of decimal or raw-lineage information;
- partition bytes/descriptors are invariant to input ordering;
- partition grouping uses venue, instrument, UTC date and event kind;
- content tampering fails closed;
- listing/delisting history is point-in-time correct;
- universe fingerprints are invariant to source enumeration order;
- future-availability observations are excluded;
- historical delisted instruments are retained when they were active at the event's availability time;
- pre-effective-listing events are excluded;
- manifests are deterministic and decision-boundary sensitive;
- manifest tampering fails closed;
- replay is deterministic and cannot cross the manifest decision time;
- manifest/event-set mismatch fails closed;
- identical partition/manifest writes are idempotent.

Test evidence is stored under `artifacts/test-evidence/r1_3-*` and consolidated in `artifacts/test-evidence/R1_3_EVIDENCE.md`.

## Evidence classification and limitation

The R1.3 acceptance suite uses deterministic TEST FIXTURE data. It proves the dataset contracts, storage semantics, point-in-time controls and deterministic replay implementation. It does **not** claim that a long-horizon real-market research archive has already been populated, nor does it claim alpha, profitability, paper-trading performance or live-trading readiness.

The current JSONL partition format is intentionally dependency-free. If production scale later requires Parquet/Arrow or remote object storage, that adapter should preserve the same immutable partition descriptor, manifest fingerprint, universe and PIT assembly semantics rather than changing the research contract.

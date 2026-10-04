# Crypto Perpetual Production Feature Engine

## Scope

`quant_system.features` constructs deterministic, versioned, point-in-time (PIT) research features from the canonical R1.1 crypto-perpetual event contracts. It is deliberately isolated from alpha discovery, strategy ranking, portfolio allocation, order generation, broker submission, paper/shadow execution and F7 commissioning.

The engine does not persist canonical research datasets itself. R1.3 remains the dataset layer. The integration seam is `FeatureDatasetSource.load_feature_batch(...) -> FeatureInputBatch`; a later R1.3 adapter can map partitioned/revisioned research data into that batch without changing feature calculators.

## Point-in-time contract

Every `FeatureInputBatch` has a decision time, instrument, venue, dataset fingerprint(s) and universe version. Every canonical input event must match the batch identity and have `available_at <= decision_time`. Any future-available event causes a fail-closed `FeatureLeakageError`; it is not silently filtered.

Every emitted `FeatureRecord` includes:

- immutable feature definition and semantic version;
- decision time;
- engine/config fingerprints;
- source dataset fingerprints;
- universe version;
- canonically sorted source-event IDs;
- raw SHA-256 lineage for those source events;
- each source availability timestamp; and
- a deterministic record fingerprint.

The run manifest binds the engine version, config, registry, input batch and every output fingerprint. Identical inputs/configuration reproduce the same manifest even when input events are shuffled or Python hash randomization differs.

## Feature families

### Microstructure

- `micro.spread_bps`
- `micro.book_imbalance_l<N>`
- `micro.microprice_deviation_bps`
- `micro.top_depth_notional`
- `micro.trade_imbalance_<window>s`

Order-book features reconstruct the latest snapshot plus subsequent deltas. When sequence IDs are supplied, the canonical R1.1 `BookSequenceGuard` is applied and gaps fail closed. Crossed reconstructed books are rejected.

### Funding

- `funding.current_rate`
- `funding.mean_rate_<window>s`
- `funding.change_<window>s`

### Basis

- `basis.mark_index_bps`
- `basis.change_<window>s_bps`

Basis uses venue mark versus index prices already known by the decision time.

### Open interest

- `open_interest.current`
- `open_interest.value_current`
- `open_interest.change_<window>s_pct`

### Liquidations

- `liquidation.notional_<window>s`
- `liquidation.long_notional_<window>s`
- `liquidation.short_notional_<window>s`
- `liquidation.imbalance_<window>s`

### Volatility

- `volatility.realized_<window>s_bps`
- `volatility.return_std_<window>s_bps`
- `volatility.range_<window>s_bps`

Mark-price observations are preferred when at least two are present in-window; otherwise canonical trade prices are used.

### Momentum

- `momentum.return_<short>s_bps`
- `momentum.return_<long>s_bps`
- `momentum.efficiency_<long>s`

### Regime state

- `regime.state`
- `regime.liquidity_score`
- `regime.stress_score`

The regime output is descriptive only. It deterministically labels observed conditions such as liquidity stress, high volatility, directional trending, low-volatility range or balanced state. It does not score, rank, retain or select alpha hypotheses.

## Versioning and reproducibility

`FeatureEngineConfig` is fingerprinted and contains all feature windows/depth/regime thresholds. Each `FeatureDefinition` has a semantic feature version and a fingerprint covering its family, unit, description and parameters. Changing the feature version, configuration, dataset fingerprint, event availability or source lineage changes the resulting run identity.

## R1.3 integration

R1.3 should implement `FeatureDatasetSource` rather than make calculators query storage directly. The adapter should:

1. resolve the canonical dataset/universe version;
2. apply R1.3 revision/partition selection rules;
3. return only events that are available as of the requested decision time;
4. preserve canonical R1.1 event metadata and raw hashes; and
5. supply deterministic dataset fingerprint(s) and universe version.

`FeatureInputBatch` then independently re-checks PIT availability and identity before any calculation.

## Explicit non-capabilities

The package contains no alpha-selection workflow, strategy optimization, expected-return ranking, portfolio allocation, broker adapter, order submission, credential handling, capital movement or F7 commissioning path.

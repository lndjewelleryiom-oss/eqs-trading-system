# Research Workflow

## Canonical experiment lifecycle

1. **Observation** — anomaly, structural premise, event or literature-inspired mechanism.
2. **Hypothesis** — precise statement with economic rationale and falsification criteria.
3. **Data contract** — exact datasets, point-in-time rules, revisions, known gaps and latency.
4. **Pre-registration** — primary metric, acceptance gate, train/OOS ranges and research budget recorded before results.
5. **Prototype** — simplest credible baseline.
6. **Backtest** — conservative executable assumptions.
7. **Falsification** — deliberately seek conditions that destroy the effect.
8. **Robustness** — parameter perturbation, costs, latency, universe, regime, bootstrap/Monte Carlo.
9. **Multiple-testing adjustment** — research-family trial count retained.
10. **Locked OOS** — no tuning against the final holdout.
11. **Research decision** — reject, revise, or advance.
12. **Paper** — execution-quality evidence.
13. **Shadow** — production path without venue release.
14. **Controlled live commissioning** — LIVE_1 upward only with evidence.
15. **Continuous degradation monitoring**.

## Non-negotiable experiment metadata

Every run records code commit, environment lock, dataset manifest/hashes, feature versions, random seeds, execution assumptions, parameter set, strategy lineage, research family/trial count, acceptance criteria, output artifact URIs and failures.

## Overfit defence

- Search-space size is part of the result.
- Holdouts remain locked until the candidate reaches the designated gate.
- Narrow optimum islands are a rejection signal.
- Parameter robustness is evaluated as a surface, not at a single optimum.
- Costs are stressed above the expected base case.
- Performance is decomposed by time, asset, regime and source of P&L.
- Exceptional results receive more scrutiny, not less.

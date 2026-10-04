# Alpha v1 R1.3 Dataset Binding Gates

`schemas/alpha_data_binding_gates_v1.schema.json` is the machine-readable structural contract for the R1.3 → Alpha-v1 dataset handoff.

The authoritative deterministic structural validator and final roll-up live in `quant_system.research.alpha_binding`.

Key rules:

- Gate IDs are exactly A01 through A28.
- Gate states are only `PASS`, `FAIL`, or `BLOCKED`; there is no waiver state.
- `FAIL` is an observed violation; `BLOCKED` is insufficient/missing evidence. Neither is accepted as pass.
- A01–A18, A24, A25, A27 and A28 are global hard gates.
- A19/A20 are fail-closed PIT instrument/time filters. Legitimate exclusions do not themselves fail the gate; admitting an ineligible row does.
- A21/A22/A23/A26 are evaluated per campaign and deterministically aggregated to their top-level gate records.
- Candidate trade-count minima are recorded but remain `DEFERRED_UNTIL_CANDIDATE_EXECUTION`; observations are never re-labelled as trades.
- All six frozen Alpha-v1 campaigns must be binding-ready before `ALPHA_DATA_BINDING_READY` or `locked_oos_may_be_opened` can be true.
- The final decision and decision fingerprint are recomputed by the validator; a caller cannot assert the boolean by hand.
- `generated_at`, human notes and filesystem paths are excluded from the decision fingerprint; evidence SHA-256 values and all material gate/campaign decisions are included.
- A26 OOS contamination is represented as a campaign invalidation failure, never a warning.

This contract authorizes research-dataset binding only. It does not prove strategy edge, profitability, PAPER/SHADOW readiness, broker readiness or LIVE authorization.

## Empirical binding enforcement

`CampaignLineageBinding.bind()` is fail-closed and requires the complete validated report. It rechecks the concrete campaign fingerprint, R1.3 manifest/dataset/universe identity and requested FeatureRun fingerprints before returning a verified binding. `to_experiment_manifest()` rejects any lineage object that does not retain an accepted Alpha data-binding ID and decision fingerprint.

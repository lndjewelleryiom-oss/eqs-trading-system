# EQS Strategy Visibility Contract V1

## Purpose

Every permissible strategy run is inspectable before profitability approval. Visibility is read-only and independent from permission to submit broker orders or use live capital.

## Modes

- RESEARCH_REPLAY: historical data, simulated fills only.
- INTERNAL_PAPER: current admitted market data, simulated fills only.
- BROKER_DEMO: broker demo execution only after a separate scoped approval.
- LIVE: real-capital execution only after explicit F7 authority.

The current implementation work in J45-J51 only admits RESEARCH_REPLAY and INTERNAL_PAPER. BROKER_DEMO and LIVE remain unavailable.

## Catalogue states

The catalogue can display discovery, queued, running, validating, internal paper, paused, rejected, blocked, failed and finished runs. Operational status and research/evidence result are separate fields. A successful checker process cannot convert a blocked research result into a passed gate.

## Identity

Each run has an immutable run_id, strategy_id, strategy_version, source_id, instrument_id, parameter snapshot, dataset hash, code hash and config hash. Parameter changes create a new run identity.

## Required event envelope

The canonical event schema is schemas/strategy_run_event_v1.schema.json. Events are append-only and per-run sequence numbers are strictly increasing. Each event links to the previous event hash. Corrections are new events; historical events are not silently rewritten.

## Required visible panels

The strategy detail view must expose:

1. Identity and execution mode.
2. Historical/current price observations with signal/order/fill markers.
3. Equity, drawdown, realised/unrealised P&L and costs.
4. Orders, fills, positions and completed economic trades.
5. Test progress, historical market time, wall time, heartbeat and last event.
6. Frozen parameters and rationale.
7. Data/risk/execution diagnostics and failure reasons.
8. Run comparison using aligned periods, cost model and source scope.

A rejected or failed run remains visible in the catalogue and keeps its evidence.

## Locked OOS rule

Locked out-of-sample performance remains hidden until the precommitted release condition authorises access. Operational progress may remain visible without exposing sealed performance/trade outcomes.

## Safety and authority

Read access never grants execution permission. All broker-connected actions remain outside this contract. Simulated orders and fills are labelled SIMULATED. Historical replay trades never count toward the 168-hour / 100 qualifying forward-trade autonomy floor.

## API mapping

The strategy viewer consumes bounded run-registry reads:

- GET /strategy-runs
- GET /strategy-runs/{run_id}
- GET /strategy-runs/{run_id}/events?after={cursor}
- GET /strategy-runs/{run_id}/trades
- GET /strategy-runs/{run_id}/equity

J51 adds research-job admission separately. The existing production read-only interface remains mutation-free.

## Acceptance

J45 preparation passes when fixtures cover visible running, blocked, failed, rejected and completed states; mode and source labels are explicit; and viewing does not require profitability approval.

J46 preparation passes when deterministic replay proves contiguous event sequence, hash-chain integrity, exact fill/order linkage, no duplicate economic totals on cursor replay and explicit simulated provenance.

J47 preparation passes when the existing EQS UI can open a visible unapproved research run, render price/progress/equity/trades/failures from the registry, and reconnect without losing or duplicating events.

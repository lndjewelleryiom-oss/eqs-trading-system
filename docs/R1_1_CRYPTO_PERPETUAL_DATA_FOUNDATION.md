# R1.1 — Crypto Perpetual Real-Market Data Foundation

## Scope

R1.1 establishes the read-only, point-in-time-safe normalization boundary for perpetual-futures research. It does **not** place orders, require API credentials, claim that a live feed has been soak-tested, or establish any trading edge.

The first venue adapters are:

1. **Binance USDⓈ-M perpetual futures**
2. **Bybit Linear (USDT/USDC perpetuals)**
3. **OKX SWAP**

All connector network surfaces in R1.1 are public market-data surfaces. Authenticated account/trading APIs are outside this phase.

## Design invariants

1. Raw bytes are stored content-addressed before parsing or normalization.
2. Every normalized record links to the SHA-256 of the exact raw payload that produced it.
3. Venue symbols are never guessed into canonical instruments. An explicit point-in-time instrument map is required; an unknown symbol fails closed.
4. Monetary/quantity values use `Decimal`, not binary floating point.
5. Every event carries four point-in-time timestamps with the invariant:

   `event_time <= published_at <= available_at <= received_at`

6. Research visibility is controlled by `available_at`, not by the exchange's event timestamp.
7. Order-book deltas cannot be applied before initialization; sequence regressions/gaps fail closed when the venue provides sufficient sequence metadata.
8. Connector normalization is deterministic for identical raw payload + timestamp inputs.
9. R1.1 contains no API key, authentication, order-entry, withdrawal, transfer, or capital-control method.

## Point-in-time timestamp semantics

| Field | Meaning | Allowed source |
|---|---|---|
| `event_time` | Time of the economic/matching-engine event | Venue trade/matching-engine timestamp where available; otherwise venue data timestamp |
| `published_at` | Time the venue/system generated or published the message | Venue message-generation timestamp where available; otherwise the venue data timestamp |
| `available_at` | Earliest instant this collector could know the payload | Local wall-clock timestamp captured at socket byte receipt |
| `received_at` | Time normalization/ingestion completed | Local wall-clock timestamp after raw persistence/receipt |

`available_at` is the historical anti-leakage boundary. A venue timestamp is never substituted for local availability merely because it is earlier.

Clock-skew that produces `published_at > available_at` is rejected/quarantined rather than silently rewritten. A later production collector may add an explicit calibrated-clock/skew policy, but it must preserve the original source and receive timestamps.

## Canonical instrument schema

`PerpetualInstrumentDefinition` contains:

- canonical `instrument_id`;
- venue and venue symbol;
- base, quote and settlement assets;
- linear/inverse contract style;
- tick size and lot size;
- contract value where applicable;
- venue status;
- effective time;
- publication, availability and receipt timestamps;
- exact raw payload hash;
- schema version.

Instrument metadata is point-in-time data. Current tick size, status or contract metadata must not be projected backward into a period before that metadata was available.

## Canonical event schemas

### TradeEvent

- provenance metadata;
- venue trade ID;
- price;
- quantity;
- aggressor side where the venue makes it inferable/explicit.

### BookUpdate

- provenance metadata;
- snapshot or delta classification;
- bid and ask levels;
- first/final/previous sequence when provided;
- checksum when provided.

Zero-quantity levels are retained in delta events because they carry deletion semantics.

### PerpetualStateEvent

Sparse state event containing one or more of:

- mark price;
- index price;
- funding rate;
- next funding time;
- open interest;
- open-interest value.

A sparse schema is deliberate: venues publish these fields through different channels and at different frequencies. Missing fields mean "not supplied in this source event", not zero.

### LiquidationEvent

- provenance metadata;
- liquidation identifier if supplied;
- liquidated position side;
- execution/bankruptcy/average price as defined by the venue source;
- quantity.

Venue-specific side semantics are normalized explicitly in adapters and must be covered by fixture tests.

## First connector surfaces

### Binance USDⓈ-M

The connector uses the current routed WebSocket layout rather than the legacy unrouted endpoint:

- high-frequency order-book depth → `wss://fstream.binance.com/public`
- aggregate trades → `wss://fstream.binance.com/market`
- mark price/funding → `wss://fstream.binance.com/market`
- liquidation/force-order → `wss://fstream.binance.com/market`

Initial channels:

- `<symbol>@depth@100ms`
- `<symbol>@aggTrade`
- `<symbol>@markPrice@1s`
- `<symbol>@forceOrder`

### Bybit Linear

Public endpoint:

- `wss://stream.bybit.com/v5/public/linear`

Initial channels:

- `orderbook.50.<symbol>`
- `publicTrade.<symbol>`
- `tickers.<symbol>`
- `allLiquidation.<symbol>`

The order-book adapter distinguishes snapshot/delta messages and captures update/cross-sequence data. Ticker state supplies mark/index price, funding information and open interest when present.

### OKX SWAP

Default global public endpoint:

- `wss://ws.okx.com:8443/ws/v5/public`

Initial channels:

- `books`
- `trades`
- `mark-price`
- `funding-rate`
- `open-interest`

OKX API domains are region-dependent. The production transport must resolve the endpoint from the actual account/registration region instead of assuming the global domain. R1.1's normalizer itself remains transport-agnostic.

## Raw-first collection path

The required order is:

`socket bytes` → timestamp `available_at` → immutable raw store → JSON decode → normalize → timestamp `received_at` → canonical event

Malformed payloads are still stored before parse failure. This allows incident reconstruction and prevents parser failure from destroying the source evidence.

## Order-book initialization and continuity

R1.1 provides a generic `BookSequenceGuard`:

- a snapshot initializes/resets state;
- a delta before initialization is rejected;
- previous-sequence mismatch is rejected when supplied;
- first-sequence gaps are rejected when sufficient bounds are supplied;
- non-increasing final sequence IDs are rejected.

Venue-specific production book builders remain an R1.2 concern because each venue has its own REST snapshot/bootstrap and resynchronization algorithm.

## R1.1 acceptance tests

R1.1 may be marked `PASSED` only if all of the following are objectively green:

1. Current endpoint/stream plans for Binance USDⓈ-M, Bybit Linear and OKX SWAP are encoded.
2. Unknown instrument symbols fail closed.
3. Canonical point-in-time timestamp ordering is enforced.
4. A record cannot be consumed before `available_at`.
5. Exact raw bytes are stored before parsing and linked into normalized-event lineage.
6. Malformed raw payloads remain recoverable after parse failure.
7. Binance trade/depth/mark-funding/liquidation fixtures normalize correctly.
8. Bybit snapshot/trade/ticker/liquidation fixtures normalize correctly.
9. OKX book/trade/funding/open-interest fixtures normalize correctly.
10. Order-book initialization/sequence-gap protection is tested.
11. Decimal-valued prices/quantities remain decimal.
12. Existing repository regressions stay green.

## Explicit non-claims / next gate

R1.1 does **not** prove:

- live public WebSocket connectivity from the deployment runtime;
- reconnect behavior over long sessions;
- rate-limit handling;
- live gap/resnapshot recovery;
- sustained raw-data throughput;
- historical REST backfill correctness;
- multi-day clock drift/latency distributions;
- dataset completeness;
- any profitable strategy.

Those are R1.2 live-feed/backfill acceptance requirements.

## Official source register (verified 2026-09-22)

- Binance USDⓈ-M WebSocket market streams: `https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/websocket-market-streams/Connect`
- Binance USDⓈ-M split-route migration/mapping: `https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/websocket-market-streams/Important-WebSocket-Change-Notice`
- Bybit V5 public WebSocket connection: `https://bybit-exchange.github.io/docs/v5/ws/connect`
- Bybit V5 order book: `https://bybit-exchange.github.io/docs/v5/websocket/public/orderbook`
- Bybit V5 ticker: `https://bybit-exchange.github.io/docs/v5/websocket/public/ticker`
- Bybit V5 all-liquidation: `https://bybit-exchange.github.io/docs/v5/websocket/public/all-liquidation`
- OKX V5 API guide: `https://www.okx.com/docs-v5/en/`
- OKX V5 market-data guidance: `https://www.okx.com/docs-v5/trick_en/`

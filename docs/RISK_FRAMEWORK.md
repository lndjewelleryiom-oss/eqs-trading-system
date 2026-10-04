# Risk Framework

## Authority model

Risk is a separate control service and has final authority. Alpha components never modify their own risk limits.

## Hierarchy of limits

- Order: size, notional, price deviation, spread, liquidity, order rate.
- Instrument: gross/net exposure, concentration, liquidity and venue limits.
- Strategy: capital, gross/net, leverage, VaR/ES proxy, drawdown, daily loss, turnover.
- Asset/sector/currency/venue: concentration and correlated failure domains.
- Portfolio: gross/net, leverage, factor exposure, correlation clusters, tail loss, daily loss and drawdown.

## Fail-closed triggers

- stale or corrupt critical data;
- impossible/future timestamps;
- broker/exchange order state uncertain beyond timeout;
- reconciliation mismatch above tolerance;
- portfolio/account balance uncertainty;
- global or venue kill switch;
- daily loss/drawdown hard limits;
- uncontrolled order loop or idempotency failure;
- execution prices materially outside tolerance;
- credentials/authorization anomaly.

## Dynamic sizing

Position size is bounded by the minimum of strategy confidence sizing, volatility target, liquidity/capacity, concentration limits, drawdown throttle and portfolio risk budget. Uncertainty reduces sizing.

## Commissioning limits

LIVE_1 begins at economically meaningful but intentionally small capital. Advancement requires sufficient real execution observations and no material divergence from the approved research envelope. Any material divergence can move a strategy to WATCH/REDUCED/PAUSED without model permission.

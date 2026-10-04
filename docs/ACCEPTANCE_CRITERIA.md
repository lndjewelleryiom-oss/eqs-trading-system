# Foundation Acceptance Criteria

## F0.1 Repository/config
- tests discover and run from a clean checkout;
- live trading default is false;
- fail-closed default is true;
- withdrawal API permission policy is false;
- secrets are ignored by source control.

## F0.2 Database schema
- schema includes dataset point-in-time metadata, strategy registry/history, experiments, validation, multiple-testing ledger, deployment, risk, orders/fills/positions and audit events;
- every forward migration has an explicit rollback path;
- the complete forward migration executes successfully on real PostgreSQL;
- database-backed structural and behavioral assertions verify core enums, tables, indexes, defaults, checks, uniqueness and foreign keys;
- rollback removes all migration-owned tables/types while preserving shared extensions;
- the migration can be reapplied cleanly after rollback and the acceptance suite passes again;
- the temporary acceptance schema is removed after verification.

## F0.3 Strategy lifecycle
- direct PROPOSED -> LIVE transition is rejected;
- staged PROPOSED -> EXPERIMENTAL -> OOS_VALIDATED -> PAPER -> SHADOW -> LIVE_1 works;
- transition reason is mandatory and retained.

## F0.4 Risk foundation
- valid small order can be allowed;
- oversized order is blocked;
- stale data causes HALT;
- global kill switch causes HALT;
- non-positive equity, daily loss and drawdown hard stops are supported.

## F0.5 Audit foundation
- valid hash chain verifies;
- modified historical content invalidates verification.

## F1.4 Point-in-time guard
- observations unavailable at decision time are rejected;
- timezone-naive timestamps are rejected;
- suspected-corrupt observations are rejected.

## F2 Event-driven backtest/execution simulator
- market orders never fill on the decision timestamp and latency is enforced before eligibility;
- per-symbol bars must be strictly time-ordered;
- buy/sell limit orders require a bar-range touch and fill at the limit rather than receiving optimistic price improvement;
- commissions, half-spread, slippage and participation-scaled market impact are applied deterministically and adversely;
- volume participation and partial-fill constraints limit executable quantity;
- debit-cash financing and short borrow costs accrue under an explicit ACT/365 convention;
- splits adjust signed quantity/cost basis without creating cash and dividends credit longs/debit shorts;
- accounting reports cash, positions, average cost, realized/unrealized P&L, costs, market value, gross/net notional and equity;
- reconciliation reconstructs cash and signed positions from primitive journal deltas and detects drift;
- exact synthetic acceptance fixtures and the complete repository test suite pass with zero failures.

## F3 Research/validation and overfitting defence
- experiment manifests produce a canonical content fingerprint from strategy, hypothesis, data fingerprints, code version, parameters, costs, split specification, seed and software versions;
- identical research inputs reproduce the same experiment ID, while changed code/seed/parameters change it and persisted-manifest tampering is detected;
- chronological holdout and rolling/expanding walk-forward splits keep training observations strictly before test observations and support an explicit gap;
- purged K-fold removes training label windows that overlap the test span and applies an explicit post-test embargo;
- parameter grids and cost multipliers are evaluated deterministically and expose broad-region profitability rather than only an optimum;
- seeded block bootstrap confidence intervals and seeded Monte Carlo trade-path resampling are reproducible;
- Deflated Sharpe penalises selection across multiple trials; CSCV/PBO measures whether in-sample winners collapse out of sample; a White-style max-mean bootstrap reality check tests the selected family against a zero benchmark; Holm-Bonferroni correction is available for explicit p-value families;
- scorecards report return, risk, drawdown, trade-distribution and tail metrics and research reports explicitly label research stage plus weaknesses/failure conditions;
- a deliberately slice-mined synthetic strategy is rejected while a controlled synthetic edge is recovered by the validation gate;
- the complete repository test suite passes with zero failures.

F3 statistical implementations are research controls, not guarantees. PBO assumes an aligned candidate-return matrix, the Deflated Sharpe implementation uses an approximate independent-trial benchmark, and the reality check uses a deterministic block-bootstrap max-mean statistic. Production research reports must disclose these assumptions and may require stronger methods for dependent trial families or specialized datasets.

## F7 Controlled live commissioning — credential-free acceptance
- controller initializes in `LIVE_0` and cannot leave it unless every LIVE_1 readiness input is affirmative;
- with F0-F6, PostgreSQL, shadow and risk acceptance marked complete, readiness must remain blocked only by external venue/human authorization gates;
- scale-up is strictly one level at a time and requires a positive precommitted observation floor plus slippage, drawdown, reconciliation and degradation evidence;
- rollback requires a recorded reason and cannot be used to increase live level;
- critical runtime faults (kill switch, stale data, broker health, reconciliation, daily-loss or drawdown breach) automatically de-risk to `LIVE_0`;
- execution-quality or strategy-degradation breaches automatically reduce one live level;
- a commissioned submission boundary refuses to call even a submission-capable broker gateway while the controller is `LIVE_0`;
- credential-free tests may use only the deterministic in-memory broker double and synthetic evidence fixtures; they do not satisfy external authorization or venue-specific evidence;
- actual `LIVE_1` remains blocked until trade-only credentials, withdrawals-disabled proof, regulatory/operational approval and explicit human live-capital authorization exist.

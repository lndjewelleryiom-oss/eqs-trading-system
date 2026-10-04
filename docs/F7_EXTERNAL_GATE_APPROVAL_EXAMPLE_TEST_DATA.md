# F7 External Gate Approval Record — TEST DATA EXAMPLE

**TEST DATA — NON-AUTHORIZING EXAMPLE — DO NOT USE FOR LIVE TRADING**

```text
TEST_DATA: true
AUTHORIZATION_EFFECT: NONE
MAY_AUTHORIZE_LIVE_TRADING: false
RUNTIME_ELIGIBILITY: REJECT
OVERALL_DECISION: NOT_APPROVED
```

This document is a completed training/test example of `docs/F7_EXTERNAL_GATE_APPROVAL_TEMPLATE.md`. All names, venues, account identifiers, evidence references, hashes, signatures, limits and timestamps below are fictional placeholders. It is structurally non-authorizing and must never be recorded as objective evidence for `F7.E1`–`F7.E5`.

## 1. Approval identity

| Field | Completed TEST value |
|---|---|
| Approval record ID | `TEST-F7-20260922-0001` |
| System | `Evolutionary Autonomous Quant Trading System` |
| Repository commit / release | `1111111111111111111111111111111111111111` (TEST PLACEHOLDER) |
| Environment | `TEST-EXAMPLE-NOT-A-RUNTIME` |
| Venue | `Example Exchange Ltd — FICTIONAL` |
| Account identifier | `TEST-ACCT-FP-7F3A91C2` |
| Approver full name | `Alex Example` |
| Approver role / authority | `Test Commissioning Officer — NO REAL AUTHORITY` |
| Approver organization | `Example Quant Systems Ltd — FICTIONAL` |
| Secondary reviewer, if required | `Morgan Sample — Test Risk Reviewer` |
| Signed at (UTC) | `2026-09-22T08:45:00Z` |
| Signature method | `TEST DIGITAL SIGNATURE — INVALID FOR AUTHORIZATION` |
| Signed document hash | `TEST-SHA256-NOT-VALID-9f57b1d949c08a680ca5b4f975c568c7` |

## 2. Authorized scope — TEST ONLY

| Scope control | Completed TEST value |
|---|---|
| Maximum deployment stage | `LIVE_0 ONLY — TEST DOCUMENT CANNOT AUTHORIZE LIVE_1` |
| Approved strategy IDs | `TEST-STRAT-MOM-001`, `TEST-STRAT-RV-002` |
| Approved instruments / markets | `TEST: BTC-USD`, `TEST: ES` |
| Prohibited instruments / markets | `ALL REAL INSTRUMENTS AND MARKETS` |
| Maximum live capital | `£0.00` |
| Maximum gross exposure | `£0.00 / 0%` |
| Maximum net exposure | `£0.00 / 0%` |
| Maximum leverage | `0.00x` |
| Maximum per-position exposure | `£0.00 / 0%` |
| Maximum daily loss | `£0.00 / 0%` |
| Maximum drawdown | `£0.00 / 0%` |
| Maximum order notional | `£0.00` |
| Maximum order rate | `0 orders / any interval` |
| Permitted trading hours | `NONE` |
| Approved risk-policy version | `TEST-RISK-v0` |
| Approved broker-adapter version | `TEST-ADAPTER-v0` |
| Approved configuration hash | `TEST-CONFIG-HASH-NOT-VALID` |

Anything not explicitly listed above remains unauthorized. This example authorizes no real venue, account, strategy, instrument, capital, order, or broker submission.

## 3. Validity period

| Field | Completed TEST value |
|---|---|
| Valid from (UTC) | `2026-09-22T08:45:00Z` |
| Expires at (UTC) | `2026-09-29T08:45:00Z` |
| Maximum validity permitted by policy | `7 days — TEST EXAMPLE ONLY` |
| Mandatory review date | `2026-09-29` |
| Renewal requires new signature | `YES` |

The dates above demonstrate form completion only. They do not create an authorization window.

## 4. External gate decisions and evidence

### F7.E1 — Trade-only credentials

**Decision:** `BLOCKED — TEST DATA`  
**Credential ID/fingerprint:** `TEST-KEY-FP-83A0C4D7`  
**Secret-manager record reference:** `test-evidence://vault/eqs/f7/e1/credential`  
**Permission export/screenshot evidence:** `test-evidence://f7/e1/permissions.json#TEST-HASH-E1`  
**Safe trading-permission test evidence:** `test-evidence://f7/e1/trade-permission-test.txt#TEST-HASH-E1B`  
**Credential owner:** `Alex Example / Test Operator`  
**Rotation/review date:** `2026-09-29`  
**Gate approver:** `Alex Example / Test Commissioning Officer`  
**Gate approved at (UTC):** `NOT APPROVED — TEST DATA`

**Blocker:** No real production trading credential exists or has been verified.

### F7.E2 — Withdrawals disabled

**Decision:** `BLOCKED — TEST DATA`  
**Redacted permission evidence:** `test-evidence://f7/e2/withdrawal-disabled.png#TEST-HASH-E2`  
**Negative withdrawal/fund-movement authorization test:** `test-evidence://f7/e2/negative-withdrawal-test.txt#TEST-HASH-E2B`  
**Separately approved transfer exception, if any:** `NONE`  
**Gate approver:** `Morgan Sample / Test Risk Reviewer`  
**Gate approved at (UTC):** `NOT APPROVED — TEST DATA`

**Blocker:** No real venue credential exists from which fund-movement restrictions can be verified.

### F7.E3 — Operational approval

**Decision:** `BLOCKED — TEST DATA`  
**Operational runbook reference:** `test-evidence://f7/e3/runbook-v1.pdf#TEST-HASH-E3`  
**Monitoring/alerting evidence:** `test-evidence://f7/e3/monitoring.json#TEST-HASH-E3A`  
**Kill-switch test evidence:** `test-evidence://f7/e3/kill-switch.txt#TEST-HASH-E3B`  
**Reconciliation test evidence:** `test-evidence://f7/e3/reconciliation.txt#TEST-HASH-E3C`  
**Fault-recovery rehearsal evidence:** `test-evidence://f7/e3/rehearsal.txt#TEST-HASH-E3D`  
**Incident owner:** `Taylor Demo / Test Incident Owner`  
**Operations approver:** `Alex Example / Test Commissioning Officer`  
**Gate approved at (UTC):** `NOT APPROVED — TEST DATA`

**Blocker:** No accountable operator has approved a real production operating configuration.

### F7.E4 — Regulatory approval

**Decision:** `BLOCKED — TEST DATA`  
**Legal/compliance assessment reference:** `test-evidence://f7/e4/legal-assessment.pdf#TEST-HASH-E4`  
**Jurisdiction(s):** `TEST JURISDICTION — FICTIONAL`  
**Venue/account type reviewed:** `Example Exchange / fictional proprietary account`  
**Instruments/activity reviewed:** `Synthetic spot, futures and ETF examples only`  
**Reviewer name/organization:** `Jordan Placeholder / Example Compliance LLP — FICTIONAL`  
**Binding restrictions / conditions:** `NO LIVE TRADING; TEST DATA ONLY`  
**Unresolved regulatory matters:** `REAL JURISDICTION, VENUE, ACCOUNT AND ACTIVITY NOT REVIEWED`  
**Gate approved at (UTC):** `NOT APPROVED — TEST DATA`

**Blocker:** No real legal/compliance determination has been obtained for a live configuration.

### F7.E5 — Explicit live-capital authorization

**Decision:** `BLOCKED — TEST DATA`  
**Authorized person:** `Alex Example / Test Commissioning Officer / NO REAL AUTHORITY`  
**Venue/account fingerprint:** `TEST-ACCT-FP-7F3A91C2`  
**Strategy IDs:** `TEST-STRAT-MOM-001`, `TEST-STRAT-RV-002`  
**System commit/release:** `1111111111111111111111111111111111111111`  
**Approved capital/risk envelope:** `£0.00; LIVE_0 only`  
**Capital-source/account ownership evidence reference:** `test-evidence://f7/e5/ownership.txt#TEST-HASH-E5`  
**Authorization approved at (UTC):** `NOT APPROVED — TEST DATA`  
**Authorization expiry (UTC):** `2026-09-29T08:45:00Z — TEST FIELD ONLY`

**Blocker:** No authorized human has granted permission to expose real capital.

## 5. Consolidated evidence register — TEST REFERENCES ONLY

| Evidence ID | Gate | Description | Immutable URI / record ID | SHA-256 / integrity hash | Captured at UTC | Custodian |
|---|---|---|---|---|---|---|
| `TEST-EVID-001` | `F7.E1` | Example credential permission export | `test-evidence://f7/e1/permissions.json` | `TEST-HASH-E1` | `2026-09-22T08:30:00Z` | `Alex Example` |
| `TEST-EVID-002` | `F7.E2` | Example negative withdrawal test | `test-evidence://f7/e2/negative-withdrawal-test.txt` | `TEST-HASH-E2B` | `2026-09-22T08:32:00Z` | `Morgan Sample` |
| `TEST-EVID-003` | `F7.E3` | Example fault-recovery rehearsal | `test-evidence://f7/e3/rehearsal.txt` | `TEST-HASH-E3D` | `2026-09-22T08:35:00Z` | `Taylor Demo` |
| `TEST-EVID-004` | `F7.E4` | Example legal assessment | `test-evidence://f7/e4/legal-assessment.pdf` | `TEST-HASH-E4` | `2026-09-22T08:38:00Z` | `Jordan Placeholder` |
| `TEST-EVID-005` | `F7.E5` | Example ownership record | `test-evidence://f7/e5/ownership.txt` | `TEST-HASH-E5` | `2026-09-22T08:40:00Z` | `Alex Example` |

All `test-evidence://` references are fictional and intentionally non-resolving outside test fixtures.

## 6. Automatic revocation conditions

For this example the authorization is already invalid. If it were a real record, the following conditions would revoke it immediately and require `LIVE_0`: expiry; any gate becoming false or unverifiable; credential rotation or permission change; credential compromise; system, adapter, risk-policy or configuration drift; out-of-scope strategy/venue/instrument/capital; unavailable safety controls; stale/corrupt data; broker/API failure; reconciliation mismatch; unknown orders/positions; risk-limit breach; withdrawn legal/compliance approval; withdrawn operational approval; human revocation; or evidence found stale, altered, incomplete or false.

## 7. Final approval declaration — TEST DATA

The test signer confirms only that this example demonstrates how a completed record may look. It does **not** confirm that any external gate has passed and does **not** grant live-capital authority.

**Overall decision:** `NOT APPROVED`  
**Approver name:** `Alex Example`  
**Approver role/authority:** `Test Commissioning Officer — NO REAL AUTHORITY`  
**Signature:** `TEST-SIGNATURE-INVALID-NON-AUTHORIZING`  
**Signed at (UTC):** `2026-09-22T08:45:00Z`  
**Signed document hash:** `TEST-SHA256-NOT-VALID-9f57b1d949c08a680ca5b4f975c568c7`  
**Independent reviewer:** `Morgan Sample / TEST-SIGNATURE-INVALID / 2026-09-22T08:46:00Z`

## 8. Post-signature system record — MUST REMAIN REJECTED

- Approval record ID: `TEST-F7-20260922-0001`
- Signed artifact URI: `test-evidence://f7/example/TEST-F7-20260922-0001.md`
- Signed artifact SHA-256: `TEST-HASH-NOT-VALID`
- Tracker evidence references added to: `NONE`
- Deployment stage after verification: `LIVE_0`
- Verification service/operator: `TEST VALIDATOR`
- Verification timestamp (UTC): `2026-09-22T08:47:00Z`
- Runtime eligibility: `REJECT`

**Fail-closed result:** this example must never satisfy `F7.E1`, `F7.E2`, `F7.E3`, `F7.E4`, or `F7.E5`; it must never unlock `LIVE_1`; and it must never be accepted as objective evidence in `tracker.json` or `docs/TRACKER.md`.

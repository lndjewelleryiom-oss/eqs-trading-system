# F7 External Gate Approval Record

**Document status:** TEMPLATE — NOT AN APPROVAL UNTIL COMPLETED AND SIGNED  
**Purpose:** Human-controlled evidence record for the five external commissioning gates required before any transition from `LIVE_0` to `LIVE_1`.

> This document never authorizes `LIVE_2`, `LIVE_3`, or `LIVE_4`. If any required field, gate decision, evidence reference, signature, validity field, or prerequisite is absent, the authorization is invalid and the deployment stage remains `LIVE_0`.

## 1. Approval identity

| Field | Required value |
|---|---|
| Approval record ID | `[unique immutable ID]` |
| System | `Evolutionary Autonomous Quant Trading System` |
| Repository commit / release | `[full commit SHA / immutable release ID]` |
| Environment | `[production environment identifier]` |
| Venue | `[venue legal name]` |
| Account identifier | `[redacted account ID / fingerprint]` |
| Approver full name | `[name]` |
| Approver role / authority | `[role and basis of authority]` |
| Approver organization | `[organization]` |
| Secondary reviewer, if required | `[name / role or N/A]` |
| Signed at (UTC) | `[YYYY-MM-DDTHH:MM:SSZ]` |
| Signature method | `[wet signature / qualified e-signature / approved digital signature]` |
| Signed document hash | `[SHA-256 or approved immutable evidence hash]` |

## 2. Authorized scope

| Scope control | Approved value |
|---|---|
| Maximum deployment stage | `LIVE_1 ONLY` |
| Approved strategy IDs | `[explicit list; no wildcard]` |
| Approved instruments / markets | `[explicit list]` |
| Prohibited instruments / markets | `[explicit list]` |
| Maximum live capital | `[currency and amount]` |
| Maximum gross exposure | `[amount / %]` |
| Maximum net exposure | `[amount / %]` |
| Maximum leverage | `[x]` |
| Maximum per-position exposure | `[amount / %]` |
| Maximum daily loss | `[amount / %]` |
| Maximum drawdown | `[amount / %]` |
| Maximum order notional | `[amount]` |
| Maximum order rate | `[orders / interval]` |
| Permitted trading hours | `[UTC window / venue session]` |
| Approved risk-policy version | `[immutable version/hash]` |
| Approved broker-adapter version | `[immutable version/hash]` |
| Approved configuration hash | `[hash]` |

Anything not explicitly listed above is outside scope and is not authorized.

## 3. Validity period

| Field | Required value |
|---|---|
| Valid from (UTC) | `[YYYY-MM-DDTHH:MM:SSZ]` |
| Expires at (UTC) | `[YYYY-MM-DDTHH:MM:SSZ]` |
| Maximum validity permitted by policy | `[duration]` |
| Mandatory review date | `[YYYY-MM-DD]` |
| Renewal requires new signature | `YES` |

Expiration is automatic. An expired approval cannot be extended merely by editing dates; a new approval record and signature are required.

## 4. External gate decisions and evidence

A gate is `PASSED` only when every required evidence reference is present and the named approver/reviewer confirms the pass condition. Secrets must never be embedded in this record.

### F7.E1 — Trade-only credentials

**Pass condition:** A dedicated production trading credential exists with only minimum required read/trade/order-management permissions, no unrelated administrative or fund-movement privileges, and is stored in approved secret management.

- Decision: `[PASSED / BLOCKED]`
- Credential ID/fingerprint (never secret value): `[reference]`
- Secret-manager record reference: `[URI / immutable ID]`
- Permission export/screenshot evidence: `[URI + hash]`
- Safe trading-permission test evidence: `[URI + hash]`
- Credential owner: `[name/role]`
- Rotation/review date: `[date]`
- Gate approver: `[name/role]`
- Gate approved at (UTC): `[timestamp]`

### F7.E2 — Withdrawals disabled

**Pass condition:** The production trading credential cannot withdraw assets, manage withdrawal addresses, change bank/security settings, create privileged credentials, or move funds except any narrowly required transfer permission separately approved in writing.

- Decision: `[PASSED / BLOCKED]`
- Redacted permission evidence: `[URI + hash]`
- Negative withdrawal/fund-movement authorization test: `[URI + hash]`
- Separately approved transfer exception, if any: `[approval ID or NONE]`
- Gate approver: `[name/role]`
- Gate approved at (UTC): `[timestamp]`

### F7.E3 — Operational approval

**Pass condition:** An accountable operator approves the exact system/adapter version, strategy scope, capital/risk limits, monitoring, reconciliation, kill-switch, incident response, credential-revocation, manual-flattening and restart procedures, and a fault-recovery commissioning rehearsal is completed.

- Decision: `[PASSED / BLOCKED]`
- Operational runbook reference: `[URI + hash]`
- Monitoring/alerting configuration evidence: `[URI + hash]`
- Kill-switch test evidence: `[URI + hash]`
- Reconciliation test evidence: `[URI + hash]`
- Fault-recovery rehearsal evidence (`normal → fault → HALT → reconcile → human review → controlled restart`): `[URI + hash]`
- Incident owner: `[name/role]`
- Operations approver: `[name/role]`
- Gate approved at (UTC): `[timestamp]`

### F7.E4 — Regulatory approval

**Pass condition:** A written legal/compliance determination for the actual operator, jurisdiction, venue, instruments, ownership/funding model and intended activity concludes that the proposed live operation may proceed and identifies all binding restrictions.

- Decision: `[PASSED / BLOCKED]`
- Legal/compliance assessment reference: `[URI / document ID + hash]`
- Jurisdiction(s): `[list]`
- Venue/account type reviewed: `[details]`
- Instruments/activity reviewed: `[details]`
- Reviewer name/organization: `[details]`
- Binding restrictions / conditions: `[list or NONE]`
- Unresolved regulatory matters: `[list; must be NONE to pass unless explicitly accepted by competent authority]`
- Gate approved at (UTC): `[timestamp]`

### F7.E5 — Explicit live-capital authorization

**Pass condition:** A named authorized human explicitly approves `LIVE_1` only for the specified venue/account, strategy IDs, system version, maximum capital, exposure, leverage, position, daily-loss and drawdown limits, with revocation conditions and an expiry/review point.

- Decision: `[PASSED / BLOCKED]`
- Authorized person: `[name/role/basis of authority]`
- Venue/account fingerprint: `[reference]`
- Strategy IDs: `[explicit list]`
- System commit/release: `[immutable ID]`
- Approved capital/risk envelope: `[reference to Section 2 and any stricter limits]`
- Capital-source/account ownership evidence reference: `[URI + hash where required]`
- Authorization approved at (UTC): `[timestamp]`
- Authorization expiry (UTC): `[timestamp]`

## 5. Consolidated evidence register

| Evidence ID | Gate | Description | Immutable URI / record ID | SHA-256 / integrity hash | Captured at UTC | Custodian |
|---|---|---|---|---|---|---|
| `[EVID-001]` | `[F7.Ex]` | `[description]` | `[reference]` | `[hash]` | `[timestamp]` | `[name/role]` |

Evidence references must resolve to immutable or access-controlled records. Redactions must not hide the permission state needed to establish the gate.

## 6. Automatic revocation conditions

Any condition below immediately invalidates this authorization and requires the controller to return to `LIVE_0` and block new broker submissions until a fresh approval is granted where required:

- approval validity period expires;
- any `F7.E1`–`F7.E5` gate becomes false, unverifiable, withdrawn, or materially changes;
- credential is rotated, revoked, leaked, suspected compromised, or its permission scopes change;
- withdrawal, transfer, administrative, or other prohibited credential capability is detected;
- repository commit/release, broker adapter, risk-policy version, or approved configuration changes outside the signed scope;
- strategy ID, venue/account, instrument universe, or capital/risk limit falls outside the approved scope;
- risk engine, kill switch, monitoring, alerting, audit logging, clock synchronization, market-data integrity, or reconciliation is unavailable or untrusted;
- stale/corrupt market data, broker/API health failure, unknown order/position, reconciliation mismatch, duplicate/runaway order condition, or execution-integrity failure occurs;
- maximum daily loss, drawdown, leverage, exposure, concentration, liquidity, slippage, order-rate, or other approved risk limit is breached;
- legal/compliance approval is withdrawn, expires, becomes inapplicable, or a new restriction conflicts with the approved activity;
- operational approval is withdrawn or the designated incident/control ownership is no longer in force;
- the authorized human revokes approval for any reason;
- evidence used for approval is later found inaccurate, stale, incomplete, altered, or unverifiable.

A revocation event must be audit-logged with timestamp, reason code, prior deployment stage, resulting deployment stage, and operator notification status.

## 7. Final approval declaration

By signing below, the approver confirms that:

1. all five gates are individually `PASSED` with objective evidence;
2. every evidence reference has been reviewed and is applicable to this exact scope;
3. this approval authorizes **`LIVE_1` only** and does not authorize automatic progression to a higher live stage;
4. any missing, expired, contradictory, or revoked prerequisite makes the authorization invalid;
5. automatic revocation conditions in Section 6 are accepted and take precedence over strategy/model outputs; and
6. no secret credential material is contained in this document.

**Overall decision:** `[APPROVED FOR LIVE_1 / NOT APPROVED]`  
**Approver name:** `[name]`  
**Approver role/authority:** `[role]`  
**Signature:** `[signature / digital-signature reference]`  
**Signed at (UTC):** `[timestamp]`  
**Signed document hash:** `[hash]`  
**Independent reviewer (if policy requires):** `[name / signature / timestamp or N/A]`

## 8. Post-signature system record

The signed artifact should be registered without copying secrets into the tracker:

- Approval record ID: `[ID]`
- Signed artifact URI: `[immutable/access-controlled URI]`
- Signed artifact SHA-256: `[hash]`
- Tracker evidence references added to: `[F7.E1, F7.E2, F7.E3, F7.E4, F7.E5 as applicable]`
- Deployment stage after verification: `[LIVE_0 or LIVE_1]`
- Verification service/operator: `[identity]`
- Verification timestamp (UTC): `[timestamp]`

**Fail-closed rule:** the signed template is necessary evidence but is not, by itself, sufficient to submit an order. The commissioning controller must independently verify all required gates and runtime safety conditions before permitting `LIVE_1` submission.

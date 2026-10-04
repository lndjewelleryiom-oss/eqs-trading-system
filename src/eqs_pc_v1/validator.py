from __future__ import annotations
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
import hashlib, json, math
import jsonschema
from jsonschema import Draft202012Validator, RefResolver
from .reservation_policy_binding import validate_capital_risk_policy_binding
from .reservation_lease import ImmutableReservationLeaseLifecycle

VALIDATOR_VERSION = "1.0.0"
AUTHORIZED_QUALIFICATION_STATES = {"PAPER_AUTHORISED", "SHADOW_AUTHORISED", "LIVE_AUTHORISED"}

@dataclass(frozen=True)
class RuleResult:
    rule_id: str
    status: str
    severity: str
    message: str
    evidence: List[str]

def canonical_json(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")

def sha256_obj(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj)).hexdigest()

def hash_without(obj: Dict[str, Any], field: str) -> str:
    c = dict(obj); c.pop(field, None); return sha256_obj(c)

def target_set_hash(targets: List[Dict[str, Any]]) -> str:
    return sha256_obj(sorted(targets, key=lambda x: x["target_id"]))

def parse_ts(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None: raise ValueError("timestamp must be timezone-aware")
    return dt.astimezone(timezone.utc)

def aggregate_rule_results(results: List[RuleResult]) -> str:
    """Fail-closed aggregation used by the validator and fuzz certification.

    Any FAIL or UNKNOWN at ERROR/CRITICAL severity blocks the chain.
    INFO/WARN UNKNOWN values remain observable but do not independently block.
    """
    return "FAIL" if any(
        r.severity in {"ERROR", "CRITICAL"} and r.status in {"FAIL", "UNKNOWN"}
        for r in results
    ) else "PASS"

class SchemaRegistry:
    def __init__(self, schema_dir: str | Path):
        self.schema_dir = Path(schema_dir)
        self.schemas = {p.name: json.loads(p.read_text()) for p in self.schema_dir.glob("*.schema.json")}
        self.store = {s["$id"]: s for s in self.schemas.values() if "$id" in s}
        self.store.update(self.schemas)
    def validate(self, schema_name: str, obj: Dict[str, Any]) -> List[str]:
        schema = self.schemas[schema_name]
        resolver = RefResolver.from_schema(schema, store=self.store)
        validator = Draft202012Validator(schema, resolver=resolver, format_checker=jsonschema.FormatChecker())
        return sorted(f"{list(e.absolute_path)}: {e.message}" for e in validator.iter_errors(obj))

class SemanticValidator:
    """Deterministic, fail-closed semantic validator for EQS-PC-V1.0.0."""
    def __init__(self, schema_dir: str | Path): self.schemas = SchemaRegistry(schema_dir)
    def _schema(self, rid, name, obj, label):
        errs = self.schemas.validate(name, obj)
        return RuleResult(rid, "FAIL" if errs else "PASS", "CRITICAL", f"{label} schema {'invalid' if errs else 'valid'}", errs)
    def validate_chain(self, *, decision_time: str, intents: List[Dict[str, Any]], targets: List[Dict[str, Any]], reservation_request: Dict[str, Any], capital_risk_response: Dict[str, Any], execution_handoff: Optional[Dict[str, Any]]=None, qualification_evidence: Optional[Dict[str, Dict[str, Any]]]=None, certification_context: Optional[Dict[str, Any]]=None, validation_time: Optional[str]=None) -> Dict[str, Any]:
        qev = qualification_evidence or {}; rr: List[RuleResult] = []
        # Canonical JSON is the trust boundary for all content-addressed identities.
        # Python's stdlib can represent NaN/Infinity even though canonical JSON cannot;
        # reject such input explicitly so malformed numeric values fail closed instead of crashing hashing.
        try:
            canonical_json({
                "decision_time": decision_time, "intents": intents, "targets": targets,
                "reservation_request": reservation_request, "capital_risk_response": capital_risk_response,
                "execution_handoff": execution_handoff, "qualification_evidence": qev,
                "certification_context": certification_context,
            })
            rr.append(RuleResult("PCV-000","PASS","CRITICAL","Input is canonical-JSON serialisable and finite",[]))
        except Exception as e:
            rr.append(RuleResult("PCV-000","FAIL","CRITICAL","Input is not canonical JSON or contains non-finite numeric data",[str(e)]))
            core={"validator_version":VALIDATOR_VERSION,"validated_at":validation_time or datetime.now(timezone.utc).isoformat().replace("+00:00","Z"),"overall_status":"FAIL","rule_results":[asdict(r) for r in rr]}
            core["report_hash"]=sha256_obj(core); return core
        decision_dt = parse_ts(decision_time)
        for i,x in enumerate(intents): rr.append(self._schema(f"PCV-S001[{i}]","strategy_intent.schema.json",x,"strategy_intent"))
        for i,x in enumerate(targets): rr.append(self._schema(f"PCV-S002[{i}]","target_exposure.schema.json",x,"target_exposure"))
        rr.append(self._schema("PCV-S003","risk_reservation_request.schema.json",reservation_request,"risk_reservation_request"))
        rr.append(self._schema("PCV-S004","capital_risk_response.schema.json",capital_risk_response,"capital_risk_response"))
        if execution_handoff is not None: rr.append(self._schema("PCV-S005","execution_handoff.schema.json",execution_handoff,"execution_handoff"))
        seen=set()
        for i,x in enumerate(intents):
            iid=x.get("intent_id"); dup=iid in seen; seen.add(iid)
            rr.append(RuleResult(f"PCV-001[{i}]","FAIL" if dup else "PASS","CRITICAL","Duplicate intent_id" if dup else "intent_id unique",[str(iid)]))
            state=x.get("qualification_state"); ok=state in AUTHORIZED_QUALIFICATION_STATES
            rr.append(RuleResult(f"PCV-002[{i}]","PASS" if ok else "FAIL","CRITICAL","Strategy qualification state authorised" if ok else "Strategy qualification state not authorised",[str(state)]))
            evid=x.get("qualification_evidence_id")
            if evid not in qev:
                rr.append(RuleResult(f"PCV-003[{i}]","UNKNOWN","CRITICAL","Qualification evidence unresolved",[str(evid)]))
            else:
                rr.append(RuleResult(f"PCV-003[{i}]","PASS","CRITICAL","Qualification evidence resolved",[str(evid)]))
                got=sha256_obj(qev[evid]); expected=x.get("qualification_snapshot_hash")
                rr.append(RuleResult(f"PCV-004[{i}]","PASS" if got==expected else "FAIL","CRITICAL","Qualification snapshot hash matches" if got==expected else "Qualification snapshot hash mismatch",[expected or "",got]))
                same=qev[evid].get("strategy_id")==x.get("strategy_id") and qev[evid].get("strategy_version")==x.get("strategy_version")
                rr.append(RuleResult(f"PCV-005[{i}]","PASS" if same else "FAIL","CRITICAL","Qualification evidence strategy/version matches" if same else "Qualification evidence strategy/version mismatch",[str(evid)]))
            try:
                safe=parse_ts(x["knowledge_time"])<=decision_dt
                rr.append(RuleResult(f"PCV-006[{i}]","PASS" if safe else "FAIL","CRITICAL","Intent PIT-safe" if safe else "Intent knowledge_time after decision_time",[x["knowledge_time"],decision_time]))
            except Exception as e: rr.append(RuleResult(f"PCV-006[{i}]","FAIL","CRITICAL","Invalid PIT timestamp",[str(e)]))
            if x.get("direction")=="FLAT":
                ok=all(v in (None,0,0.0) for v in [x.get("desired_quantity"),x.get("desired_notional")])
                rr.append(RuleResult(f"PCV-007[{i}]","PASS" if ok else "FAIL","ERROR","FLAT exposure valid" if ok else "FLAT intent carries non-zero exposure",[]))
            else: rr.append(RuleResult(f"PCV-007[{i}]","NOT_APPLICABLE","ERROR","Direction not FLAT",[]))
        admitted={(x.get("strategy_id"),x.get("strategy_version")) for x in intents}; seen=set()
        for i,t in enumerate(targets):
            tid=t.get("target_id"); dup=tid in seen; seen.add(tid)
            rr.append(RuleResult(f"PCV-010[{i}]","FAIL" if dup else "PASS","CRITICAL","Duplicate target_id" if dup else "target_id unique",[str(tid)]))
            delta=t.get("target_quantity",0)-t.get("current_quantity",0); ok=math.isclose(delta,t.get("trade_delta_quantity",float('nan')),rel_tol=0,abs_tol=1e-12)
            rr.append(RuleResult(f"PCV-011[{i}]","PASS" if ok else "FAIL","CRITICAL","Trade delta arithmetic valid" if ok else "Trade delta mismatch",[str(delta),str(t.get("trade_delta_quantity"))]))
            ok=t.get("gross_strategy_exposure",-1)+1e-12>=abs(t.get("net_external_exposure",0))
            rr.append(RuleResult(f"PCV-012[{i}]","PASS" if ok else "FAIL","CRITICAL","Gross/net exposure relation valid" if ok else "Gross exposure below absolute net exposure",[]))
            bad=[f"{a.get('strategy_id')}@{a.get('strategy_version')}" for a in t.get("source_attribution",[]) if (a.get("strategy_id"),a.get("strategy_version")) not in admitted]
            rr.append(RuleResult(f"PCV-013[{i}]","FAIL" if bad else "PASS","CRITICAL","Attribution references non-admitted strategies" if bad else "Attribution references admitted strategies",bad))
            accepted=sum(float(a.get("accepted_exposure",0)) for a in t.get("source_attribution",[])); net=float(t.get("net_external_exposure",0)); ok=math.isclose(accepted,net,rel_tol=0,abs_tol=1e-9)
            rr.append(RuleResult(f"PCV-014[{i}]","PASS" if ok else "FAIL","ERROR","Attribution reconciles to net exposure" if ok else "Attribution net mismatch",[str(accepted),str(net)]))
            gross=sum(abs(float(a.get("accepted_exposure",0))) for a in t.get("source_attribution",[])); ok=gross<=float(t.get("gross_strategy_exposure",0))+1e-9
            rr.append(RuleResult(f"PCV-015[{i}]","PASS" if ok else "FAIL","ERROR","Attribution gross within declared gross" if ok else "Attribution gross exceeds declared gross",[str(gross)]))
            calc=hash_without(t,"target_hash"); ok=t.get("target_hash")==calc
            rr.append(RuleResult(f"PCV-016[{i}]","PASS" if ok else "FAIL","CRITICAL","target_hash valid" if ok else "target_hash mismatch",[t.get("target_hash",""),calc]))
            badf=[v.get("factor_id","?") for v in t.get("factor_exposures",[]) if v.get("quality_state") in {"FAIL","UNKNOWN"}]; increase=abs(t.get("target_quantity",0))>abs(t.get("current_quantity",0))+1e-12; ok=not(badf and increase)
            rr.append(RuleResult(f"PCV-017[{i}]","PASS" if ok else "FAIL","CRITICAL","No unsafe increase from unknown factor state" if ok else "Risk increase with unknown/failed factor state",badf))
        ids1=sorted(t.get("target_id") for t in targets); ids2=sorted(t.get("target_id") for t in reservation_request.get("targets",[])); ok=ids1==ids2
        rr.append(RuleResult("PCV-020","PASS" if ok else "FAIL","CRITICAL","Reservation target identities match" if ok else "Reservation target identities mismatch",[str(ids1),str(ids2)]))
        tsh=target_set_hash(targets); ok=reservation_request.get("proposed_target_set_hash")==tsh
        rr.append(RuleResult("PCV-021","PASS" if ok else "FAIL","CRITICAL","Proposed target-set hash valid" if ok else "Proposed target-set hash mismatch",[reservation_request.get("proposed_target_set_hash",""),tsh]))
        runids={t.get("portfolio_run_id") for t in targets}; ok=not targets or runids=={reservation_request.get("portfolio_run_id")}
        rr.append(RuleResult("PCV-022","PASS" if ok else "FAIL","CRITICAL","portfolio_run_id consistent" if ok else "portfolio_run_id mismatch",list(map(str,runids))))
        reqq={x.get("qualification_evidence_id") for x in intents}; have=set(reservation_request.get("qualification_evidence_refs",[])); miss=sorted(x for x in reqq if x not in have); ok=not miss
        rr.append(RuleResult("PCV-023","PASS" if ok else "FAIL","CRITICAL","Qualification evidence refs complete" if ok else "Qualification evidence refs incomplete",miss))
        ok=capital_risk_response.get("reservation_request_id")==reservation_request.get("reservation_request_id")
        rr.append(RuleResult("PCV-030","PASS" if ok else "FAIL","CRITICAL","Risk response references request" if ok else "Risk response request mismatch",[]))
        decision=capital_risk_response.get("decision")
        if decision=="APPROVED": ok=capital_risk_response.get("approved_target_set_hash")==reservation_request.get("proposed_target_set_hash")
        elif decision=="APPROVED_WITH_REDUCTIONS":
            ats=capital_risk_response.get("approved_targets") or []; ok=bool(ats) and capital_risk_response.get("approved_target_set_hash")==target_set_hash(ats)
        else: ok=None
        rr.append(RuleResult("PCV-031","NOT_APPLICABLE" if ok is None else ("PASS" if ok else "FAIL"),"CRITICAL","No approved target set" if ok is None else ("Approved target-set hash valid" if ok else "Approved target-set hash invalid"),[str(decision)]))
        if decision in {"APPROVED","APPROVED_WITH_REDUCTIONS"}:
            ok=bool(capital_risk_response.get("reservation_id")); rr.append(RuleResult("PCV-032","PASS" if ok else "FAIL","CRITICAL","Reservation present" if ok else "Approval missing reservation_id",[]))
            try: ok=parse_ts(capital_risk_response["reservation_expiry"])>parse_ts(capital_risk_response["decided_at"])
            except Exception: ok=False
            rr.append(RuleResult("PCV-033","PASS" if ok else "FAIL","CRITICAL","Reservation expiry valid" if ok else "Reservation expiry invalid",[]))
        else:
            rr += [RuleResult("PCV-032","NOT_APPLICABLE","CRITICAL","No approval",[]),RuleResult("PCV-033","NOT_APPLICABLE","CRITICAL","No approval",[])]

        request_policy_pin = reservation_request.get("policy_generation_pin_sha256")
        response_policy_pin = capital_risk_response.get("policy_generation_pin_sha256")
        if request_policy_pin:
            pin_ok = response_policy_pin == request_policy_pin
            rr.append(RuleResult("PCV-034","PASS" if pin_ok else "FAIL","CRITICAL","Capital & Risk response attests reservation policy pin" if pin_ok else "Capital & Risk response policy pin mismatch",[str(request_policy_pin),str(response_policy_pin)]))
            binding_errors = validate_capital_risk_policy_binding(reservation_request, capital_risk_response)
            seal_errors = [e for e in binding_errors if "SEAL" in e or "NONCANONICAL" in e]
            cross_errors = [e for e in binding_errors if e not in seal_errors and e != "CAPITAL_RISK_RESPONSE_POLICY_PIN_MISMATCH"]
            rr.append(RuleResult("PCV-035","FAIL" if seal_errors else "PASS","CRITICAL","Capital & Risk reservation-policy attestation seal valid" if not seal_errors else "Capital & Risk reservation-policy attestation seal invalid",seal_errors))
            rr.append(RuleResult("PCV-036","FAIL" if cross_errors else "PASS","CRITICAL","Capital & Risk reservation-policy attestation fields bind exactly" if not cross_errors else "Capital & Risk reservation-policy attestation cross-field mismatch",cross_errors))
        else:
            unexpected = bool(response_policy_pin or capital_risk_response.get("reservation_policy_attestation"))
            rr.append(RuleResult("PCV-034","FAIL" if unexpected else "NOT_APPLICABLE","CRITICAL","Unpinned reservation cannot acquire a Capital & Risk policy pin" if unexpected else "Reservation is not policy-generation pinned",[]))
            rr.append(RuleResult("PCV-035","NOT_APPLICABLE","CRITICAL","No authoritative reservation-policy attestation required",[]))
            rr.append(RuleResult("PCV-036","NOT_APPLICABLE","CRITICAL","No authoritative reservation-policy attestation required",[]))

        if execution_handoff is None:
            rr.append(RuleResult("PCV-040","NOT_APPLICABLE","CRITICAL","No execution handoff supplied",[]))
        else:
            approved=decision in {"APPROVED","APPROVED_WITH_REDUCTIONS"}; rr.append(RuleResult("PCV-040","PASS" if approved else "FAIL","CRITICAL","Execution follows approval" if approved else "Execution without approval",[str(decision)]))
            lease_ctx_for_expiry=(certification_context or {}).get("reservation_lease_state")
            authoritative_expiry=(lease_ctx_for_expiry or {}).get("expires_at") or capital_risk_response.get("reservation_expiry")
            checks=[("PCV-041","reservation_request_id",reservation_request.get("reservation_request_id")),("PCV-042","capital_reservation_id",capital_risk_response.get("reservation_id")),("PCV-043","approved_target_set_hash",capital_risk_response.get("approved_target_set_hash")),("PCV-044","reservation_expiry",authoritative_expiry)]
            for rid,field,expected in checks:
                got=execution_handoff.get(field); ok=got==expected; rr.append(RuleResult(rid,"PASS" if ok else "FAIL","CRITICAL",f"{field} matches authoritative value" if ok else f"{field} mismatch",[str(got),str(expected)]))
            try: ok=parse_ts(execution_handoff["created_at"])<parse_ts(execution_handoff["reservation_expiry"])
            except Exception: ok=False
            rr.append(RuleResult("PCV-045","PASS" if ok else "FAIL","CRITICAL","Handoff before expiry" if ok else "Reservation expired at handoff",[]))
            approved_targets=targets if decision=="APPROVED" else (capital_risk_response.get("approved_targets") or []); ok=target_set_hash(execution_handoff.get("instrument_targets",[]))==target_set_hash(approved_targets)
            rr.append(RuleResult("PCV-046","PASS" if ok else "FAIL","CRITICAL","Execution targets match approved set" if ok else "Execution targets differ from approved set",[]))
            calc=hash_without(execution_handoff,"handoff_hash"); ok=execution_handoff.get("handoff_hash")==calc
            rr.append(RuleResult("PCV-047","PASS" if ok else "FAIL","CRITICAL","handoff_hash valid" if ok else "handoff_hash mismatch",[execution_handoff.get("handoff_hash",""),calc]))
            if request_policy_pin:
                hp = execution_handoff.get("policy_generation_pin_sha256")
                ok = hp == request_policy_pin == response_policy_pin
                rr.append(RuleResult("PCV-048","PASS" if ok else "FAIL","CRITICAL","Execution handoff preserves exact reservation policy pin" if ok else "Execution handoff policy pin mismatch",[str(hp),str(request_policy_pin),str(response_policy_pin)]))
            else:
                unexpected = bool(execution_handoff.get("policy_generation_pin_sha256"))
                rr.append(RuleResult("PCV-048","FAIL" if unexpected else "NOT_APPLICABLE","CRITICAL","Unpinned execution handoff unexpectedly claims policy pin" if unexpected else "No authoritative policy pin required",[]))
        # Adversarial certification context: cross-asset/state invariants.
        ctx = certification_context or {}
        if certification_context is not None:
            rr.append(self._schema("PCV-S006","certification_context.schema.json",ctx,"certification_context"))

        # PCV-050/051 Economic factor aggregation and duplicate-component protection.
        for j,a in enumerate(ctx.get("economic_exposure_assertions", [])):
            comps=a.get("components",[]); ids=[c.get("component_id") for c in comps]
            dup=len(ids)!=len(set(ids))
            rr.append(RuleResult(f"PCV-050[{j}]","FAIL" if dup else "PASS","CRITICAL","Economic exposure assertion contains duplicate components" if dup else "Economic exposure components unique",[str(x) for x in ids]))
            summed=sum(float(c.get("signed_exposure",0)) for c in comps); declared=float(a.get("declared_net_exposure",0)); ok=math.isclose(summed,declared,rel_tol=0,abs_tol=1e-9)
            rr.append(RuleResult(f"PCV-051[{j}]","PASS" if ok else "FAIL","CRITICAL","Cross-asset economic netting reconciles" if ok else "Cross-asset economic exposure double-counted or mis-netted",[a.get("factor_id",""),str(summed),str(declared)]))

        # PCV-052 Hidden FX exposure and PCV-053 option Greek completeness.
        t_by_inst={t.get("instrument_id"):t for t in targets}
        for j,m in enumerate(ctx.get("instrument_metadata", [])):
            t=t_by_inst.get(m.get("instrument_id"))
            if t is None:
                rr.append(RuleResult(f"PCV-052[{j}]","NOT_APPLICABLE","CRITICAL","Instrument not in target set",[str(m.get("instrument_id"))]))
                rr.append(RuleResult(f"PCV-053[{j}]","NOT_APPLICABLE","CRITICAL","Instrument not in target set",[str(m.get("instrument_id"))]))
                continue
            factors=t.get("factor_exposures",[])
            if m.get("requires_currency_factor"):
                base=m.get("base_currency"); report=m.get("reporting_currency")
                has_fx=any(v.get("factor_class")=="CURRENCY" and (base is None or base in str(v.get("factor_id",""))) for v in factors)
                rr.append(RuleResult(f"PCV-052[{j}]","PASS" if has_fx else "FAIL","CRITICAL","Required currency exposure represented" if has_fx else "Hidden FX exposure: required currency factor missing",[str(base),str(report)]))
            else:
                rr.append(RuleResult(f"PCV-052[{j}]","NOT_APPLICABLE","CRITICAL","Currency factor not required",[]))
            req=set(m.get("required_greeks",[]))
            if m.get("instrument_type")=="OPTION" and req:
                have={v.get("factor_class") for v in factors}; missing=sorted(req-have); ok=not missing
                rr.append(RuleResult(f"PCV-053[{j}]","PASS" if ok else "FAIL","CRITICAL","Required option Greeks represented" if ok else "Option Greek exposure incomplete",missing))
            else:
                rr.append(RuleResult(f"PCV-053[{j}]","NOT_APPLICABLE","CRITICAL","Option Greek completeness not required",[]))

        # PCV-054 Derivative economic exposure must not be replaced by margin.
        for j,d in enumerate(ctx.get("derivative_exposures", [])):
            t=t_by_inst.get(d.get("instrument_id")); ok=False; observed=None
            if t is not None:
                vals=[float(v.get("exposure_value",0)) for v in t.get("factor_exposures",[]) if v.get("factor_id")==d.get("factor_id")]
                if vals: observed=sum(vals); ok=math.isclose(observed,float(d.get("economic_exposure",0)),rel_tol=0,abs_tol=1e-9)
            ev=[str(observed),str(d.get("economic_exposure")),str(d.get("margin_requirement"))]
            rr.append(RuleResult(f"PCV-054[{j}]","PASS" if ok else "FAIL","CRITICAL","Derivative economic exposure represented independently of margin" if ok else "Derivative exposure missing or confused with margin requirement",ev))

        # PCV-055 Reserved cash cannot be allocated twice.
        cs=ctx.get("capital_state")
        if cs is not None:
            deployable=float(cs.get("available_cash",0))-float(cs.get("reserved_cash",0)); proposed=float(cs.get("proposed_cash_use",0)); ok=proposed<=deployable+1e-9
            rr.append(RuleResult("PCV-055","PASS" if ok else "FAIL","CRITICAL","Proposed cash use fits unreserved cash" if ok else "Reserved cash double allocation detected",[str(deployable),str(proposed)]))

        # PCV-056/057 PIT and freshness of covariance/correlation snapshots.
        for j,e in enumerate(ctx.get("risk_estimates", [])):
            try:
                kt=parse_ts(e["knowledge_time"]); age=(decision_dt-kt).total_seconds(); pit=kt<=decision_dt; fresh=pit and age<=float(e.get("max_age_seconds",0))+1e-9
            except Exception:
                pit=False; fresh=False; age=float("inf")
            rid="PCV-056" if e.get("estimate_type")=="COVARIANCE" else "PCV-057"
            rr.append(RuleResult(f"{rid}[{j}]","PASS" if fresh else "FAIL","CRITICAL",f"{e.get('estimate_type')} estimate fresh and PIT-safe" if fresh else f"{e.get('estimate_type')} estimate stale or non-PIT",[str(e.get("estimate_id")),str(age),str(e.get("max_age_seconds"))]))

        # PCV-058 Capacity limits: no unknown capacity and no absolute target quantity above certified capacity.
        for j,c in enumerate(ctx.get("capacities", [])):
            t=t_by_inst.get(c.get("instrument_id")); qstate=c.get("quality_state")
            if t is None:
                rr.append(RuleResult(f"PCV-058[{j}]","NOT_APPLICABLE","CRITICAL","Capacity instrument not targeted",[str(c.get("instrument_id"))])); continue
            qty=abs(float(t.get("target_quantity",0))); lim=float(c.get("max_abs_target_quantity",0)); ok=qstate=="PASS" and qty<=lim+1e-12
            rr.append(RuleResult(f"PCV-058[{j}]","PASS" if ok else ("UNKNOWN" if qstate=="UNKNOWN" else "FAIL"),"CRITICAL","Target within certified capacity" if ok else "Capacity unavailable or target exceeds capacity",[str(qty),str(lim),str(qstate)]))

        # PCV-059 Demotion/unwind handling. Demoted strategy can hold, reduce or exit according to authoritative action, never increase.
        accepted_by_strategy={}
        for t in targets:
            for a in t.get("source_attribution",[]):
                sid=a.get("strategy_id"); accepted_by_strategy[sid]=accepted_by_strategy.get(sid,0.0)+float(a.get("accepted_exposure",0))
        for j,st in enumerate(ctx.get("strategy_states", [])):
            if st.get("state")!="DEMOTED":
                rr.append(RuleResult(f"PCV-059[{j}]","NOT_APPLICABLE","CRITICAL","Strategy not demoted",[str(st.get("strategy_id"))])); continue
            cur=float(st.get("current_exposure",0)); new=float(accepted_by_strategy.get(st.get("strategy_id"),0)); action=st.get("allowed_action")
            if action=="HOLD_NO_INCREASE": ok=abs(new)<=abs(cur)+1e-9
            elif action=="REDUCE": ok=abs(new)<abs(cur)-1e-9 or math.isclose(new,0.0,abs_tol=1e-9)
            elif action=="EXIT": ok=math.isclose(new,0.0,abs_tol=1e-9)
            else: ok=False
            rr.append(RuleResult(f"PCV-059[{j}]","PASS" if ok else "FAIL","CRITICAL","Demotion/unwind action respected" if ok else "Demoted strategy target violates unwind authority",[str(st.get("strategy_id")),str(cur),str(new),str(action)]))

        # PCV-060 One-time capital reservation consumption / replay protection.
        if execution_handoff is not None:
            consumed=set(ctx.get("consumed_reservation_ids",[])); rid=execution_handoff.get("capital_reservation_id"); replay=rid in consumed
            rr.append(RuleResult("PCV-060","FAIL" if replay else "PASS","CRITICAL","Capital reservation replay detected" if replay else "Capital reservation not previously consumed",[str(rid)]))

        # PCV-061/062 Reservation lease must be active, identity-bound, and unexpired when a handoff is validated.
        if execution_handoff is not None:
            lease=ctx.get("reservation_lease_state")
            if lease is None:
                rr.append(RuleResult("PCV-061","NOT_APPLICABLE","CRITICAL","No reservation lease context supplied (legacy/static fixture)",[]))
                rr.append(RuleResult("PCV-062","NOT_APPLICABLE","CRITICAL","No reservation lease context supplied (legacy/static fixture)",[]))
            else:
                identity_ok=(
                    lease.get("status")=="ACTIVE" and
                    lease.get("reservation_id")==execution_handoff.get("capital_reservation_id") and
                    lease.get("reservation_request_id")==execution_handoff.get("reservation_request_id") and
                    lease.get("portfolio_run_id")==execution_handoff.get("portfolio_run_id") and
                    lease.get("lease_id")==execution_handoff.get("reservation_lease_id") and
                    lease.get("issue_event_sha256")==execution_handoff.get("reservation_lease_issue_sha256") and
                    lease.get("approved_target_set_hash")==execution_handoff.get("approved_target_set_hash") and
                    lease.get("expires_at")==execution_handoff.get("reservation_expiry") and
                    (execution_handoff.get("reservation_lease_head_sha256") in (None, lease.get("head_sha256"))) and
                    (execution_handoff.get("reservation_lease_renewal_count") in (None, lease.get("renewal_count"))) and
                    (execution_handoff.get("reservation_lease_last_renewal_approval_sha256") in (None, lease.get("last_renewal_approval_sha256")))
                )
                rr.append(RuleResult("PCV-061","PASS" if identity_ok else "FAIL","CRITICAL","Execution handoff is bound to the active reservation lease" if identity_ok else "Execution handoff reservation lease identity/state mismatch",[str(lease.get("lease_id")),str(execution_handoff.get("reservation_lease_id"))]))
                try:
                    created=parse_ts(execution_handoff["created_at"]); issued=parse_ts(lease["issued_at"]); expires=parse_ts(lease["expires_at"]); temporal_ok=issued<=created<expires
                except Exception:
                    temporal_ok=False
                rr.append(RuleResult("PCV-062","PASS" if temporal_ok else "FAIL","CRITICAL","Execution handoff falls inside reservation lease validity window" if temporal_ok else "Execution handoff is outside reservation lease validity window",[str(lease.get("issued_at")),str(execution_handoff.get("created_at")),str(lease.get("expires_at"))]))

                lifecycle_obj=ctx.get("reservation_lease_lifecycle")
                renewal_count=int(lease.get("renewal_count",0) or 0)
                if lifecycle_obj is None:
                    rr.append(RuleResult("PCV-063","FAIL" if renewal_count>0 else "NOT_APPLICABLE","CRITICAL","Renewed lease lacks immutable lifecycle proof" if renewal_count>0 else "Lease has no renewal requiring lifecycle proof",[]))
                else:
                    try:
                        parsed=ImmutableReservationLeaseLifecycle.from_dict(lifecycle_obj)
                        lifecycle_ok=(parsed.state().to_dict()==lease)
                        if renewal_count>0:
                            renewed=[e for e in parsed.events if e.event_type=="RENEWED"]
                            lifecycle_ok=lifecycle_ok and len(renewed)==renewal_count and execution_handoff.get("reservation_lease_head_sha256")==parsed.head_sha256 and execution_handoff.get("reservation_lease_last_renewal_approval_sha256")==lease.get("last_renewal_approval_sha256")
                    except Exception as exc:
                        lifecycle_ok=False
                        lifecycle_error=str(exc)
                    else:
                        lifecycle_error=""
                    rr.append(RuleResult("PCV-063","PASS" if lifecycle_ok else "FAIL","CRITICAL","Reservation renewal is backed by a valid immutable Capital & Risk re-approval lifecycle" if lifecycle_ok else "Reservation renewal lifecycle proof invalid",[lifecycle_error,str(renewal_count)]))

        overall_status=aggregate_rule_results(rr)
        core={"validator_version":VALIDATOR_VERSION,"validated_at":validation_time or datetime.now(timezone.utc).isoformat().replace("+00:00","Z"),"overall_status":overall_status,"rule_results":[asdict(r) for r in rr]}
        core["report_hash"]=sha256_obj(core); return core

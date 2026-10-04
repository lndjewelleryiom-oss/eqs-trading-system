from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import jsonschema
from referencing import Registry, Resource

from .validator import semantic_validate

ENGINE_ID = "EQS-SHARED-06-REFERENCE-AGGREGATOR-V1"
ENGINE_VERSION = "1.0.0"
GATES = [
    "OPS_G01_SERVICE_HEALTH", "OPS_G02_DATA_HEALTH", "OPS_G03_RISK_HEALTH",
    "OPS_G04_EXECUTION_HEALTH", "OPS_G05_RECONCILIATION", "OPS_G06_MODE_INTEGRITY",
    "OPS_G07_COMMISSIONING", "OPS_G08_RECOVERY", "OPS_G09_SECRET_HYGIENE",
    "OPS_G10_SAFETY_INTERFACE"
]

SCHEMA_BY_RECORD_TYPE = {
    "service_health":"service_health_v1.json",
    "heartbeat":"heartbeat_v1.json",
    "data_health":"data_health_v1.json",
    "risk_health":"risk_health_v1.json",
    "execution_health":"execution_health_v1.json",
    "portfolio_health":"portfolio_health_v1.json",
    "reconciliation":"reconciliation_v1.json",
    "incident":"incident_v1.json",
    "alert_event":"alert_event_v1.json",
    "commissioning_run":"commissioning_run_v1.json",
    "restart_recovery_test":"restart_recovery_test_v1.json",
    "safety_action_request":"safety_action_request_v1.json",
    "mode_awareness":"mode_awareness_v1.json",
    "change_event":"change_event_v1.json",
    "evidence_seal":"evidence_seal_v1.json",
}

EXPECTED_DOMAIN = {
    "service_health":"SERVICE_RUNTIME_HEALTH",
    "heartbeat":"SERVICE_RUNTIME_HEALTH",
    "data_health":"MARKET_DATA_HEALTH",
    "risk_health":"CAPITAL_RISK_STATE",
    "execution_health":"EXECUTION_INTERNAL_STATE",
    "portfolio_health":"PORTFOLIO_STATE",
    "reconciliation":"EXTERNAL_ORDER_POSITION_FILL_TRUTH",
    "commissioning_run":"COMMISSIONING_RESULT",
    "restart_recovery_test":"COMMISSIONING_RESULT",
    "mode_awareness":"PROGRAMME_MODE",
    "evidence_seal":"COMMISSIONING_RESULT",
    "safety_action_request":"CAPITAL_RISK_STATE",
    "incident":"SERVICE_RUNTIME_HEALTH",
    "alert_event":"SERVICE_RUNTIME_HEALTH",
    "change_event":"SERVICE_RUNTIME_HEALTH",
}

GATE_RECORD_TYPES = {
    "OPS_G01_SERVICE_HEALTH":{"service_health","heartbeat","portfolio_health"},
    "OPS_G02_DATA_HEALTH":{"data_health"},
    "OPS_G03_RISK_HEALTH":{"risk_health"},
    "OPS_G04_EXECUTION_HEALTH":{"execution_health"},
    "OPS_G05_RECONCILIATION":{"reconciliation","restart_recovery_test"},
    "OPS_G06_MODE_INTEGRITY":{"mode_awareness","service_health"},
    "OPS_G07_COMMISSIONING":{"commissioning_run","evidence_seal"},
    "OPS_G08_RECOVERY":{"restart_recovery_test"},
    "OPS_G09_SECRET_HYGIENE":{"evidence_seal"},
    "OPS_G10_SAFETY_INTERFACE":{"safety_action_request"},
}

RULE_GATE_OUTCOME = {
    "OPS-R001":"BLOCKED", "OPS-R002":"FAIL", "OPS-R003":"FAIL", "OPS-R004":"BLOCKED",
    "OPS-R005":"FAIL", "OPS-R006":"FAIL", "OPS-R007":"BLOCKED", "OPS-R008":"FAIL",
    "OPS-R009":"FAIL", "OPS-R010":"UNKNOWN", "OPS-R011":"FAIL", "OPS-R012":"UNKNOWN",
    "OPS-R013":"FAIL", "OPS-R014":"FAIL", "OPS-R015":"FAIL", "OPS-R016":"BLOCKED",
    "OPS-R017":"UNKNOWN", "OPS-R018":"FAIL", "OPS-R019":"FAIL", "OPS-R020":"FAIL",
}

OUTCOME_PRIORITY = {"FAIL":5,"BLOCKED":4,"UNKNOWN":3,"PASS":2,"NOT_APPLICABLE":1}


def canonical_bytes(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",",":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def sha256_obj(obj: Any, omit: Sequence[str] = ()) -> str:
    if isinstance(obj, dict) and omit:
        obj = {k:v for k,v in obj.items() if k not in set(omit)}
    return hashlib.sha256(canonical_bytes(obj)).hexdigest()


def parse_ts(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return dt.astimezone(timezone.utc)


def _get_path(obj: Mapping[str, Any], path: str) -> Any:
    cur: Any = obj
    for part in path.split("."):
        if not isinstance(cur, Mapping) or part not in cur:
            return None
        cur = cur[part]
    return cur


def selector_matches(record_type: str, payload: Mapping[str, Any], selector: Mapping[str, Any]) -> bool:
    if selector.get("record_type") and selector["record_type"] != record_type:
        return False
    for path, expected in selector.get("match", {}).items():
        actual = _get_path(payload, path)
        if isinstance(expected, list) and isinstance(actual, list):
            if sorted(actual) != sorted(expected):
                return False
        elif actual != expected:
            return False
    return True


def record_scope(record_type: str, payload: Mapping[str, Any]) -> str:
    if record_type == "service_health": return str(payload.get("service_id"))
    if record_type == "heartbeat": return str(payload.get("service_id"))
    if record_type == "data_health": return f"{payload.get('source_id')}|{'|'.join(sorted(payload.get('instrument_scope', [])))}"
    if record_type == "risk_health": return str(payload.get("risk_service_id"))
    if record_type == "execution_health": return str(payload.get("execution_service_id"))
    if record_type == "portfolio_health": return str(payload.get("portfolio_service_id"))
    if record_type == "reconciliation": return f"{payload.get('scope')}|{payload.get('materiality_policy_id')}"
    if record_type == "commissioning_run": return str(payload.get("run_id"))
    if record_type == "restart_recovery_test": return f"{payload.get('component_id')}|{payload.get('fault_type')}"
    if record_type == "mode_awareness": return "PROGRAMME_MODE"
    if record_type == "evidence_seal": return f"{payload.get('artifact_type')}|{payload.get('artifact_id')}"
    if record_type == "safety_action_request": return str(payload.get("request_id"))
    if record_type == "incident": return str(payload.get("incident_id"))
    if record_type == "alert_event": return str(payload.get("alert_id"))
    if record_type == "change_event": return str(payload.get("component_id"))
    return record_type


def fact_name(record_type: str) -> str:
    return record_type.upper()


def _health_negative(record_type: str, p: Mapping[str, Any]) -> Optional[str]:
    if record_type == "service_health":
        s=p.get("health_state")
        if s in {"BLOCKED","UNKNOWN"}: return s
    elif record_type == "heartbeat":
        s=p.get("health_state")
        if s in {"BLOCKED","UNKNOWN"}: return s
    elif record_type == "portfolio_health":
        if p.get("health_state") in {"BLOCKED","UNKNOWN"} or p.get("allocation_state") in {"FAILED","BLOCKED","UNKNOWN"}: return "BLOCKED"
    elif record_type == "data_health":
        bad = [p.get("freshness_state"),p.get("sequence_state"),p.get("coverage_state"),p.get("clock_state"),p.get("replay_live_divergence")]
        if any(v in {"STALE","GAP","MISSING","REGRESSED","DIVERGED","UNKNOWN"} for v in bad): return "BLOCKED"
    elif record_type == "risk_health":
        vals=[p.get("reservation_state"),p.get("account_freshness_state"),p.get("margin_state"),p.get("kill_switch_state"),p.get("reconciliation_state"),p.get("health_state")]
        if any(v in {"INCONSISTENT","STALE","BREACH","UNKNOWN","MISMATCH","BLOCKED"} for v in vals): return "BLOCKED"
    elif record_type == "execution_health":
        if p.get("unknown_order_outcome_count",0)>0 or p.get("reconciliation_state") in {"MISMATCH","UNKNOWN","STALE"} or p.get("health_state") in {"BLOCKED","UNKNOWN"}: return "BLOCKED"
    elif record_type == "reconciliation":
        if p.get("material_unresolved") or p.get("state") in {"MISMATCH","UNKNOWN","STALE"}: return "FAIL"
    elif record_type == "mode_awareness":
        if not p.get("match") or p.get("authorised_mode") != p.get("actual_mode"): return "FAIL"
    elif record_type == "commissioning_run":
        if p.get("status") == "FAIL": return "FAIL"
        if p.get("status") == "BLOCKED": return "BLOCKED"
        if p.get("status") in {"PLANNED","RUNNING","INCOMPLETE","UNKNOWN"}: return "UNKNOWN"
    elif record_type == "restart_recovery_test":
        if p.get("result") == "FAIL": return "FAIL"
        if p.get("result") in {"INCOMPLETE","UNKNOWN"}: return "UNKNOWN"
        if p.get("result") == "PASS" and (not p.get("authoritative_state_restored") or p.get("reconciliation_state") != "MATCHED"): return "FAIL"
    elif record_type == "evidence_seal":
        if p.get("verification_status") == "FAILED": return "FAIL"
        if p.get("verification_status") == "UNKNOWN": return "UNKNOWN"
    elif record_type == "safety_action_request":
        if p.get("status") == "FAILED": return "FAIL"
    return None


@dataclass
class ValidationRecord:
    envelope: Dict[str, Any]
    status: str
    reasons: List[str] = field(default_factory=list)
    schema_errors: List[str] = field(default_factory=list)
    semantic_failures: List[Dict[str, Any]] = field(default_factory=list)
    authority_rank: Optional[int] = None

    @property
    def record_id(self) -> str: return self.envelope.get("record_id", "")
    @property
    def record_type(self) -> str: return self.envelope.get("record_type", "")
    @property
    def payload(self) -> Mapping[str, Any]: return self.envelope.get("payload", {})


class SchemaRegistry:
    def __init__(self, schema_dir: Path):
        self.schema_dir = Path(schema_dir)
        self.docs = {}
        self.registry = Registry()
        for p in sorted(self.schema_dir.glob("*.json")):
            doc=json.loads(p.read_text(encoding="utf-8"))
            self.docs[p.name]=doc
            if "$id" in doc:
                self.registry=self.registry.with_resource(doc["$id"], Resource.from_contents(doc))

    def validate(self, schema_name: str, instance: Any) -> List[str]:
        schema=self.docs[schema_name]
        v=jsonschema.Draft202012Validator(schema, registry=self.registry)
        return [e.message for e in sorted(v.iter_errors(instance), key=lambda x:list(x.path))]


class ReferenceAggregator:
    def __init__(self, package_root: Optional[Path]=None):
        self.root = Path(package_root or Path(__file__).resolve().parent)
        self.schemas = SchemaRegistry(self.root/"schemas")
        self.rule_catalog = json.loads((self.root/"rule_catalog_v1.json").read_text(encoding="utf-8"))
        self.engine_definition = json.loads((self.root/"aggregation_engine_definition_v1.json").read_text(encoding="utf-8"))
        self.engine_build_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

    def _verify_self_hashed_policy(self, policy: Mapping[str, Any], schema_name: str) -> List[str]:
        errors=self.schemas.validate(schema_name, policy)
        expected=policy.get("policy_sha256")
        actual=sha256_obj(policy, omit=("policy_sha256",))
        if expected != actual:
            errors.append("policy_sha256 mismatch")
        if policy.get("verified") is not True:
            errors.append("policy not verified")
        return errors

    def _authority_rank(self, env: Mapping[str,Any], policy: Mapping[str,Any]) -> Optional[int]:
        domain=env.get("authority_domain")
        bindings=policy.get("authority_bindings",{}).get(domain,[])
        for b in bindings:
            if b.get("producer_service_id") == env.get("producer_service_id") and env.get("evidence_class") in b.get("allowed_evidence_classes",[]):
                return int(b.get("rank",0))
        return None

    def _validate_envelope(self, env: Dict[str,Any], as_of: datetime, environment: str, policy: Mapping[str,Any]) -> ValidationRecord:
        reasons=[]; schema_errors=[]; semantic=[]
        schema_errors.extend(self.schemas.validate("input_envelope_v1.json", env))
        if schema_errors:
            return ValidationRecord(env,"INVALID",["SCHEMA_INVALID"],schema_errors,semantic,None)
        rt=env["record_type"]
        payload_errors=self.schemas.validate(SCHEMA_BY_RECORD_TYPE[rt], env["payload"])
        if payload_errors:
            return ValidationRecord(env,"INVALID",["PAYLOAD_SCHEMA_INVALID"],payload_errors,semantic,None)
        if sha256_obj(env["payload"]) != env["payload_sha256"].lower():
            return ValidationRecord(env,"INVALID",["HASH_MISMATCH"],[],semantic,None)
        if env["environment"] != environment:
            return ValidationRecord(env,"REJECTED",["ENVIRONMENT_MISMATCH"],[],semantic,None)
        try:
            effective=parse_ts(env["effective_at"]); observed=parse_ts(env["observed_at"]); received=parse_ts(env["received_at"])
            expires=parse_ts(env["expires_at"]) if env.get("expires_at") else None
        except Exception as e:
            return ValidationRecord(env,"INVALID",["TIME_INVALID",str(e)],[],semantic,None)
        if received > as_of or effective > as_of:
            return ValidationRecord(env,"REJECTED",["FUTURE_DATED_INPUT"],[],semantic,None)
        if expires is not None and expires <= as_of:
            return ValidationRecord(env,"STALE",["EVIDENCE_STALE"],[],semantic,None)
        if env["source_status"] in {"REVOKED","QUARANTINED","SUPERSEDED"}:
            return ValidationRecord(env,"REJECTED",[f"SOURCE_{env['source_status']}"],[],semantic,None)
        expected_domain=EXPECTED_DOMAIN.get(rt)
        if expected_domain and env["authority_domain"] != expected_domain:
            return ValidationRecord(env,"INVALID",["AUTHORITY_DOMAIN_MISMATCH"],[],semantic,None)
        if env["evidence_class"] not in policy.get("accepted_evidence_classes", ["CANONICAL_CONTROL","DOMAIN_AUTHORITY","EXTERNAL_TRUTH","PRIMARY_OBSERVATION","SEALED_COMMISSIONING"]):
            return ValidationRecord(env,"REJECTED",["EVIDENCE_CLASS_NOT_ACCEPTED"],[],semantic,None)
        rank=self._authority_rank(env, policy)
        if rank is None:
            return ValidationRecord(env,"UNKNOWN_AUTHORITY",["AUTHORITY_BINDING_MISSING"],[],semantic,None)
        semantic=semantic_validate(rt, dict(env["payload"]))
        return ValidationRecord(env,"ELIGIBLE",reasons,[],semantic,rank)

    def _verify_source_seal_references(self, records: List[ValidationRecord]) -> None:
        verified_seals=set()
        for r in records:
            if r.status=="ELIGIBLE" and r.record_type=="evidence_seal" and r.payload.get("verification_status")=="VERIFIED":
                verified_seals.add(r.payload.get("seal_id"))
        for r in records:
            sid=r.envelope.get("source_seal_id")
            if sid and r.record_type != "evidence_seal" and r.status=="ELIGIBLE" and sid not in verified_seals:
                r.status="INVALID"
                r.reasons.append("SOURCE_SEAL_UNVERIFIED")

    def _apply_explicit_relations(self, records: List[ValidationRecord]) -> None:
        by_id={r.record_id:r for r in records}
        for r in records:
            if r.status != "ELIGIBLE":
                continue
            revoked=r.envelope.get("revokes_record_id")
            if revoked and revoked in by_id and by_id[revoked] is not r:
                target=by_id[revoked]
                target.status="REJECTED"
                target.reasons.append(f"EXPLICITLY_REVOKED_BY:{r.record_id}")
            superseded=r.envelope.get("supersedes_record_id")
            if superseded and superseded in by_id and by_id[superseded] is not r:
                target=by_id[superseded]
                target.status="REJECTED"
                target.reasons.append(f"EXPLICITLY_SUPERSEDED_BY:{r.record_id}")

    def _detect_heartbeat_clock_regression(self, records: List[ValidationRecord]) -> None:
        groups={}
        for r in records:
            if r.status=="ELIGIBLE" and r.record_type=="heartbeat":
                key=(r.payload.get("service_id"),r.payload.get("restart_generation"))
                groups.setdefault(key,[]).append(r)
        for _, rs in groups.items():
            ordered=sorted(rs,key=lambda r:(r.payload.get("sequence",0),r.record_id))
            prev=None
            for r in ordered:
                cur=parse_ts(r.payload["emitted_at"])
                if prev is not None and cur < prev:
                    r.status="INVALID"
                    r.reasons.append("CLOCK_REGRESSION")
                prev=max(prev,cur) if prev is not None else cur

    def _dedupe(self, records: List[ValidationRecord]) -> Tuple[List[ValidationRecord], List[Dict[str,Any]]]:
        by_id={}; conflicts=[]; out=[]
        for r in records:
            rid=r.record_id
            fingerprint=sha256_obj(r.envelope)
            if rid not in by_id:
                by_id[rid]=(fingerprint,r); out.append(r)
            else:
                prior_fp, prior=by_id[rid]
                if fingerprint == prior_fp:
                    r.status="DUPLICATE"; r.reasons.append("IDEMPOTENT_DUPLICATE")
                else:
                    prior.status="CORRUPT"; r.status="CORRUPT"
                    prior.reasons.append("DUPLICATE_ID_CONFLICT"); r.reasons.append("DUPLICATE_ID_CONFLICT")
                    conflicts.append({"record_id":rid,"reason":"DUPLICATE_ID_CONFLICT"})
        return out, conflicts

    def _fact_group_key(self, r: ValidationRecord) -> Tuple[str,str,str,str]:
        e=r.envelope
        return (e["authority_domain"], e["environment"], record_scope(r.record_type,r.payload), fact_name(r.record_type))

    def _resolve_facts(self, records: List[ValidationRecord]) -> List[Dict[str,Any]]:
        groups={}
        for r in records:
            if r.status != "ELIGIBLE": continue
            groups.setdefault(self._fact_group_key(r),[]).append(r)
        facts=[]
        for key, rs in sorted(groups.items(), key=lambda kv:kv[0]):
            # Newest per producer + authority rank. Equal-time conflicting records
            # are deliberately retained so they resolve to CONFLICT rather than
            # being arbitrarily tie-broken by record ID.
            producer_groups={}
            for r in rs:
                pk=(r.envelope["producer_service_id"], r.authority_rank)
                producer_groups.setdefault(pk,[]).append(r)
            current=[]
            for pk, prs in producer_groups.items():
                newest=max(parse_ts(r.envelope["observed_at"]) for r in prs)
                current.extend(r for r in prs if parse_ts(r.envelope["observed_at"])==newest)
            best_rank=min(r.authority_rank for r in current if r.authority_rank is not None)
            top=[r for r in current if r.authority_rank==best_rank]
            latest=max(parse_ts(r.envelope["observed_at"]) for r in top)
            top_latest=[r for r in top if parse_ts(r.envelope["observed_at"])==latest]
            payload_hashes={r.envelope["payload_sha256"] for r in top_latest}
            rejected=[r.record_id for r in current if r not in top_latest]
            reason_codes=[]; conflict="NONE"; state="RESOLVED"; value=None
            winners=[r.record_id for r in sorted(top_latest,key=lambda x:x.record_id)]
            if len(payload_hashes)>1:
                state="CONFLICT"; conflict="SAME_AUTHORITY_SAME_TIME"; reason_codes.append("AUTHORITY_CONFLICT")
            else:
                value=copy.deepcopy(top_latest[0].payload)
            # Negative evidence from current active, non-presentation observations survives authority selection.
            hazards=[]
            for r in current:
                neg=_health_negative(r.record_type,r.payload)
                if neg:
                    hazards.append({"record_id":r.record_id,"signal":neg,"authority_rank":r.authority_rank})
            fact={
                "fact_key":{"authority_domain":key[0],"environment":key[1],"scope":key[2],"fact_name":key[3]},
                "record_type":rs[0].record_type,
                "resolved_value":value,
                "resolution_state":state,
                "winning_record_ids":winners,
                "rejected_record_ids":sorted(rejected),
                "authority_used":{"rank":best_rank,"producer_service_ids":sorted({r.envelope['producer_service_id'] for r in top_latest})},
                "freshness_state":"CURRENT",
                "conflict_state":conflict,
                "reason_codes":reason_codes,
                "negative_evidence":sorted(hazards,key=lambda x:(x["authority_rank"],x["record_id"])),
                "candidate_payloads":[copy.deepcopy(r.payload) for r in sorted(top_latest,key=lambda x:x.record_id)],
                "evidence_refs":sorted(r.record_id for r in current),
            }
            fact["fact_sha256"]=sha256_obj(fact,omit=("fact_sha256",))
            facts.append(fact)
        return facts

    def _select_facts(self, facts: List[Dict[str,Any]], policy: Mapping[str,Any], gate_id: str) -> Tuple[List[Dict[str,Any]], List[Dict[str,Any]]]:
        gp=policy.get("gate_policies",{}).get(gate_id,{})
        selectors=gp.get("selectors",[])
        matched=[]; missing=[]
        if selectors:
            for s in selectors:
                candidates=[]
                for f in facts:
                    if f.get("resolved_value") is not None:
                        if selector_matches(f["record_type"], f.get("resolved_value") or {}, s): candidates.append(f)
                    else:
                        cps=f.get("candidate_payloads",[])
                        if cps and all(selector_matches(f["record_type"], cp, s) for cp in cps): candidates.append(f)
                if candidates: matched.extend(candidates)
                else: missing.append(s)
        else:
            types=GATE_RECORD_TYPES.get(gate_id,set())
            matched=[f for f in facts if f["record_type"] in types]
        # dedupe by fact hash
        uniq={f["fact_sha256"]:f for f in matched}
        return [uniq[k] for k in sorted(uniq)], missing

    def _worst(self, outcomes: Iterable[str]) -> str:
        vals=list(outcomes)
        return max(vals,key=lambda x:OUTCOME_PRIORITY[x]) if vals else "PASS"

    def _fact_gate_outcome(self, gate_id: str, f: Mapping[str,Any], gp: Mapping[str,Any]) -> Tuple[str,List[str]]:
        reasons=[]
        if f["resolution_state"] == "CONFLICT": return "UNKNOWN",["AUTHORITY_CONFLICT"]
        p=f.get("resolved_value") or {}; rt=f["record_type"]
        # Negative semantic evidence of the winning record(s)
        for h in f.get("negative_evidence",[]):
            signal=h["signal"]
            if signal == "FAIL": return "FAIL",["NEGATIVE_EVIDENCE_FAIL"]
            if signal == "BLOCKED": return "BLOCKED",["NEGATIVE_EVIDENCE_BLOCK"]
            if signal == "UNKNOWN": return "UNKNOWN",["NEGATIVE_EVIDENCE_UNKNOWN"]
        if gate_id == "OPS_G01_SERVICE_HEALTH":
            if rt in {"service_health","heartbeat","portfolio_health"}:
                s=p.get("health_state")
                if s=="HEALTHY": return "PASS",[]
                if s=="DEGRADED": return ("PASS" if gp.get("allow_degraded") else "BLOCKED"),(["DEGRADED_POLICY_ACCEPTED"] if gp.get("allow_degraded") else ["DEGRADED_NOT_ACCEPTED"])
                if s=="BLOCKED": return "BLOCKED",["SERVICE_BLOCKED"]
                return "UNKNOWN",["SERVICE_HEALTH_UNKNOWN"]
        if gate_id == "OPS_G02_DATA_HEALTH":
            good=(p.get("freshness_state")=="FRESH" and p.get("sequence_state") in {"CONTIGUOUS","NOT_APPLICABLE"} and p.get("coverage_state")=="COMPLETE" and p.get("clock_state")=="SYNCHRONIZED" and p.get("replay_live_divergence") in {None,"MATCH","NOT_APPLICABLE"} and p.get("health_state")=="HEALTHY")
            return ("PASS",[]) if good else ("BLOCKED",["DATA_HEALTH_NOT_ACCEPTABLE"])
        if gate_id == "OPS_G03_RISK_HEALTH":
            good=(p.get("reservation_state")=="CONSISTENT" and p.get("account_freshness_state")=="FRESH" and p.get("margin_state") in {"NORMAL","NOT_APPLICABLE"} and p.get("kill_switch_state")=="CLEAR" and p.get("reconciliation_state")=="MATCHED" and p.get("health_state")=="HEALTHY")
            return ("PASS",[]) if good else ("BLOCKED",["RISK_HEALTH_NOT_ACCEPTABLE"])
        if gate_id == "OPS_G04_EXECUTION_HEALTH":
            good=(p.get("unknown_order_outcome_count")==0 and p.get("reconciliation_state")=="MATCHED" and p.get("health_state")=="HEALTHY" and p.get("submission_state") in {"NORMAL","DISABLED"})
            return ("PASS",[]) if good else ("BLOCKED",["EXECUTION_HEALTH_NOT_ACCEPTABLE"])
        if gate_id == "OPS_G05_RECONCILIATION":
            if rt=="reconciliation": return ("PASS",[]) if p.get("state")=="MATCHED" and not p.get("material_unresolved") else ("FAIL",["MATERIAL_RECONCILIATION_MISMATCH"])
            if rt=="restart_recovery_test": return ("PASS",[]) if p.get("reconciliation_state")=="MATCHED" else ("FAIL",["RECOVERY_RECONCILIATION_NOT_MATCHED"])
        if gate_id == "OPS_G06_MODE_INTEGRITY":
            if rt=="mode_awareness": return ("PASS",[]) if p.get("match") and p.get("authorised_mode")==p.get("actual_mode") else ("FAIL",["MODE_MISMATCH"])
            if rt=="service_health" and p.get("actual_mode") is not None: return ("PASS",[]) if p.get("authorised_mode")==p.get("actual_mode") else ("FAIL",["MODE_MISMATCH"])
        if gate_id == "OPS_G07_COMMISSIONING":
            if rt=="commissioning_run":
                if p.get("status")=="PASS" and p.get("terminal_completion_verified") and p.get("seal_verified") and all((not c.get("required")) or c.get("result")=="PASS" for c in p.get("checks",[])):
                    if p.get("commissioning_type")=="SOAK" and p.get("ended_at"):
                        dur=(parse_ts(p["ended_at"])-parse_ts(p["started_at"])).total_seconds()
                        if dur < p.get("required_duration_seconds",0): return "FAIL",["DURATION_SHORTFALL"]
                    return "PASS",[]
                if p.get("status")=="FAIL": return "FAIL",["COMMISSIONING_FAILED"]
                if p.get("status")=="BLOCKED": return "BLOCKED",["COMMISSIONING_BLOCKED"]
                return "UNKNOWN",["COMMISSIONING_INCOMPLETE_OR_UNVERIFIED"]
            if rt=="evidence_seal": return ("PASS",[]) if p.get("verification_status")=="VERIFIED" else ("FAIL",["SEAL_UNVERIFIED"])
        if gate_id == "OPS_G08_RECOVERY":
            good=(p.get("result")=="PASS" and p.get("authoritative_state_restored") is True and p.get("reconciliation_state")=="MATCHED")
            return ("PASS",[]) if good else ("FAIL",["RECOVERY_NOT_AUTHORITATIVE"])
        if gate_id == "OPS_G09_SECRET_HYGIENE":
            allowed=set(gp.get("required_seal_artifact_types",["SECRET_HYGIENE_SCAN_PASS"]))
            good=(rt=="evidence_seal" and p.get("artifact_type") in allowed and p.get("verification_status")=="VERIFIED")
            return ("PASS",[]) if good else ("UNKNOWN",["SECRET_HYGIENE_EVIDENCE_MISSING_OR_INVALID"])
        if gate_id == "OPS_G10_SAFETY_INTERFACE":
            if rt=="safety_action_request":
                allowed=set(gp.get("authorising_policy_ids",[]))
                good=p.get("authorising_policy_id") in allowed and p.get("requested_action") in {"BLOCK_NEW_RISK","DISABLE_VENUE","DEMOTE_MODE","ACTIVATE_SCOPED_KILL_SWITCH","ACTIVATE_PORTFOLIO_KILL_SWITCH"} and p.get("status")!="FAILED"
                return ("PASS",[]) if good else ("FAIL",["SAFETY_INTERFACE_UNAUTHORISED"])
        return "PASS",[]

    def _gate_result(self, gate_id: str, facts: List[Dict[str,Any]], validations: List[ValidationRecord], policy: Mapping[str,Any]) -> Dict[str,Any]:
        gp=policy.get("gate_policies",{}).get(gate_id)
        required=bool(gp and gp.get("required",False))
        gate_rule_results=[]
        if gp is None:
            outcome="UNKNOWN" if required else "NOT_APPLICABLE"
            reasons=["GATE_POLICY_MISSING"] if required else ["GATE_NOT_CONFIGURED"]
            selected=[]; missing=[]
        elif gp.get("not_applicable") is True:
            outcome="NOT_APPLICABLE"; reasons=["POLICY_AUTHORISED_NOT_APPLICABLE"]
            selected=[]; missing=[]
        else:
            selected,missing=self._select_facts(facts,policy,gate_id)
            outcomes=[]; reasons=[]
            if missing:
                outcomes.append("UNKNOWN"); reasons.append("REQUIRED_SCOPE_MISSING")
            if required and not selected and not missing and gate_id not in {"OPS_G10_SAFETY_INTERFACE"}:
                outcomes.append("UNKNOWN"); reasons.append("REQUIRED_EVIDENCE_MISSING")
            # Hard invalid evidence targeting selectors/gate is fail-closed.
            relevant_types=GATE_RECORD_TYPES.get(gate_id,set())
            for v in validations:
                if v.record_type not in relevant_types: continue
                if v.status in {"INVALID","CORRUPT"}:
                    outcomes.append("FAIL"); reasons.extend(v.reasons)
                elif v.status in {"UNKNOWN_AUTHORITY","STALE"}:
                    # only poison gate when no selected replacement for same scope
                    sc=record_scope(v.record_type,v.payload) if v.payload else ""
                    if not any(f["fact_key"]["scope"]==sc and f["record_type"]==v.record_type for f in selected):
                        outcomes.append("UNKNOWN"); reasons.extend(v.reasons)
            for f in selected:
                o,rs=self._fact_gate_outcome(gate_id,f,gp)
                outcomes.append(o); reasons.extend(rs)
                # Apply V1 semantic rule failures from winning records and retain them
                # in the gate artifact for auditability.
                win=set(f.get("winning_record_ids",[]))
                for v in validations:
                    if v.record_id in win:
                        for sf in v.semantic_failures:
                            hooks=next((r.get("gate_hooks",[]) for r in self.rule_catalog.get("rules",[]) if r.get("rule_id")==sf.get("rule_id")),[])
                            if gate_id in hooks:
                                mapped=RULE_GATE_OUTCOME.get(sf["rule_id"],"UNKNOWN")
                                outcomes.append(mapped); reasons.append(sf["failure_code"])
                                gate_rule_results.append({"record_id":v.record_id,"rule_id":sf["rule_id"],"failure_code":sf["failure_code"],"severity":sf["severity"],"mapped_gate_outcome":mapped})
            # G10 can pass on verified policy bindings even with no request record.
            if gate_id=="OPS_G10_SAFETY_INTERFACE" and not selected and not missing:
                required_ids=set(gp.get("authorising_policy_ids",[]))
                bound=set(policy.get("safety_action_policy_bindings",[]))
                if required_ids and required_ids.issubset(bound): outcomes.append("PASS")
                else: outcomes.append("UNKNOWN"); reasons.append("SAFETY_POLICY_BINDING_MISSING")
            outcome=self._worst(outcomes) if outcomes else ("PASS" if not required else "UNKNOWN")
        result={
            "gate_id":gate_id,"outcome":outcome,"required":required,
            "policy_id":policy.get("policy_id"),
            "input_fact_hashes":sorted(f["fact_sha256"] for f in selected),
            "rule_results":sorted(gate_rule_results, key=lambda x:(x["rule_id"],x["record_id"])),"reason_codes":sorted(set(reasons)),
            "missing_selectors":missing,
        }
        result["gate_result_sha256"]=sha256_obj(result,omit=("gate_result_sha256",))
        return result

    def _promotion_candidate(self, gate_results: List[Dict[str,Any]], certificate_hash: str, certificate_outcome: str, promotion_policy: Optional[Mapping[str,Any]], external_gate_evidence: Optional[Sequence[Mapping[str,Any]]]) -> Optional[Dict[str,Any]]:
        if promotion_policy is None: return None
        errors=self._verify_self_hashed_policy(promotion_policy,"promotion_policy_v1.json")
        reasons=[]
        if errors:
            outcome="UNKNOWN"; reasons.extend(["PROMOTION_POLICY_INVALID"]+errors)
        elif certificate_outcome != "PASS":
            outcome={"FAIL":"NOT_ELIGIBLE","BLOCKED":"BLOCKED","UNKNOWN":"UNKNOWN"}.get(certificate_outcome,"UNKNOWN")
            reasons.append(f"OPERATIONAL_CERTIFICATE_{certificate_outcome}")
        else:
            by_gate={g["gate_id"]:g for g in gate_results}
            allowed_na=set(promotion_policy.get("allowed_not_applicable_gates",[]))
            ops=[]
            for gid in promotion_policy.get("required_operational_gates",[]):
                g=by_gate.get(gid)
                if not g: ops.append("UNKNOWN"); reasons.append(f"MISSING_OPERATIONAL_GATE:{gid}")
                elif g["outcome"]=="PASS": ops.append("PASS")
                elif g["outcome"]=="NOT_APPLICABLE" and gid in allowed_na: ops.append("PASS")
                else: ops.append(g["outcome"]); reasons.append(f"OPERATIONAL_GATE_{g['outcome']}:{gid}")
            ext_map={e.get("ref"):e for e in (external_gate_evidence or [])}
            ext=[]
            for ref in promotion_policy.get("required_external_gate_refs",[]):
                e=ext_map.get(ref)
                if not e: ext.append("UNKNOWN"); reasons.append(f"EXTERNAL_GATE_MISSING:{ref}")
                elif e.get("status")=="VERIFIED_PASS": ext.append("PASS")
                elif e.get("status") in {"VERIFIED_FAIL","FAIL"}: ext.append("FAIL"); reasons.append(f"EXTERNAL_GATE_FAIL:{ref}")
                elif e.get("status") in {"BLOCKED","VERIFIED_BLOCKED"}: ext.append("BLOCKED"); reasons.append(f"EXTERNAL_GATE_BLOCKED:{ref}")
                else: ext.append("UNKNOWN"); reasons.append(f"EXTERNAL_GATE_UNKNOWN:{ref}")
            worst=self._worst(ops+ext) if (ops or ext) else "PASS"
            outcome={"PASS":"ELIGIBLE_FOR_EQS00_REVIEW","FAIL":"NOT_ELIGIBLE","BLOCKED":"BLOCKED","UNKNOWN":"UNKNOWN","NOT_APPLICABLE":"UNKNOWN"}[worst]
        candidate={
            "candidate_id":"PENDING_HASH","target_mode":promotion_policy.get("target_mode"),"candidate_outcome":outcome,
            "eqs00_promotion_policy_id":promotion_policy.get("policy_id"),
            "operational_certificate_sha256":certificate_hash,
            "external_gate_refs":sorted(promotion_policy.get("required_external_gate_refs",[])),
            "reason_codes":sorted(set(reasons)),
            "canonical_programme_mutation":False,
        }
        ch=sha256_obj(candidate,omit=("candidate_id","candidate_sha256")); candidate["candidate_id"]="pc-"+ch[:24]; candidate["candidate_sha256"]=sha256_obj(candidate,omit=("candidate_sha256",))
        return candidate

    def certify(self, envelopes: Sequence[Dict[str,Any]], certification_policy: Mapping[str,Any], certification_as_of: str, environment: str, promotion_policy: Optional[Mapping[str,Any]]=None, external_gate_evidence: Optional[Sequence[Mapping[str,Any]]]=None, previous_seal_sha256: Optional[str]=None) -> Dict[str,Any]:
        as_of=parse_ts(certification_as_of)
        policy_errors=self._verify_self_hashed_policy(certification_policy,"certification_policy_bundle_v1.json")
        if certification_policy.get("environment") != environment: policy_errors.append("policy environment mismatch")
        if policy_errors:
            raise ValueError("invalid certification policy: "+"; ".join(policy_errors))
        validations=[self._validate_envelope(copy.deepcopy(e),as_of,environment,certification_policy) for e in envelopes]
        self._verify_source_seal_references(validations)
        self._apply_explicit_relations(validations)
        self._detect_heartbeat_clock_regression(validations)
        validations,dup_conflicts=self._dedupe(validations)
        facts=self._resolve_facts(validations)
        # Freeze input manifest including invalid/rejected evidence; deterministically sorted.
        records_manifest=[]
        for v in sorted(validations,key=lambda x:(x.record_id,sha256_obj(x.envelope))):
            records_manifest.append({"record_id":v.record_id,"record_type":v.record_type,"status":v.status,"envelope_sha256":sha256_obj(v.envelope),"reasons":sorted(set(v.reasons))})
        input_manifest={
            "manifest_id":"PENDING_HASH","certification_as_of":certification_as_of,"environment":environment,
            "record_count":len(records_manifest),"records":records_manifest,
            "duplicate_conflicts":sorted(dup_conflicts,key=lambda x:x["record_id"]),
            "policy_bundle_sha256":certification_policy["policy_sha256"],"engine_version":ENGINE_VERSION,
        }
        mh=sha256_obj(input_manifest,omit=("manifest_id","manifest_sha256")); input_manifest["manifest_id"]="im-"+mh[:24]; input_manifest["manifest_sha256"]=sha256_obj(input_manifest,omit=("manifest_sha256",))
        fact_set_hash=hashlib.sha256(canonical_bytes([f["fact_sha256"] for f in facts])).hexdigest()
        gate_results=[self._gate_result(g,facts,validations,certification_policy) for g in GATES]
        gate_results_hash=hashlib.sha256(canonical_bytes([g["gate_result_sha256"] for g in gate_results])).hexdigest()
        required=[g for g in gate_results if g["required"]]
        overall=self._worst([g["outcome"] for g in required]) if required else "UNKNOWN"
        if overall=="NOT_APPLICABLE": overall="UNKNOWN"
        cert={
            "certification_id":"PENDING_HASH","certification_as_of":certification_as_of,"environment":environment,
            "overall_outcome":overall,
            "required_gate_results":[{"gate_id":g["gate_id"],"outcome":g["outcome"],"gate_result_sha256":g["gate_result_sha256"]} for g in required],
            "warnings":[],"input_manifest_sha256":input_manifest["manifest_sha256"],
            "resolved_fact_set_sha256":fact_set_hash,"gate_results_sha256":gate_results_hash,
            "policy_bundle_sha256":certification_policy["policy_sha256"],"engine_build_sha256":self.engine_build_sha256,
            "canonical_programme_mutation":False,"capital_promotion_performed":False,
        }
        ch=sha256_obj(cert,omit=("certification_id","certificate_sha256")); cert["certification_id"]="oc-"+ch[:24]; cert["certificate_sha256"]=sha256_obj(cert,omit=("certificate_sha256",))
        candidate=self._promotion_candidate(gate_results,cert["certificate_sha256"],overall,promotion_policy,external_gate_evidence)
        status_map={"PASS":"VERIFIED_PASS","FAIL":"VERIFIED_FAIL","BLOCKED":"VERIFIED_BLOCKED","UNKNOWN":"VERIFIED_UNKNOWN"}
        final_seal={
            "seal_id":"PENDING_HASH","certification_id":cert["certification_id"],"created_at":certification_as_of,
            "input_manifest_sha256":input_manifest["manifest_sha256"],"resolved_fact_set_sha256":fact_set_hash,
            "gate_results_sha256":gate_results_hash,"operational_certificate_sha256":cert["certificate_sha256"],
            "policy_bundle_sha256":certification_policy["policy_sha256"],"engine_build_sha256":self.engine_build_sha256,
            "previous_seal_sha256":previous_seal_sha256,"seal_status":status_map[overall],
        }
        sh=sha256_obj(final_seal,omit=("seal_id","seal_sha256")); final_seal["seal_id"]="seal-"+sh[:24]; final_seal["seal_sha256"]=sha256_obj(final_seal,omit=("seal_sha256",))
        result={
            "engine_id":ENGINE_ID,"engine_version":ENGINE_VERSION,
            "input_manifest":input_manifest,"resolved_facts":facts,"gate_results":gate_results,
            "operational_certificate":cert,"promotion_candidate":candidate,"final_seal":final_seal,
            "validation_summary":{
                "eligible":sum(v.status=="ELIGIBLE" for v in validations),
                "invalid":sum(v.status in {"INVALID","CORRUPT"} for v in validations),
                "unknown_authority":sum(v.status=="UNKNOWN_AUTHORITY" for v in validations),
                "stale":sum(v.status=="STALE" for v in validations),
                "rejected":sum(v.status=="REJECTED" for v in validations),
            }
        }
        return result

    def verify_output(self, result: Mapping[str,Any]) -> Dict[str,Any]:
        errors=[]
        im=result["input_manifest"]
        if sha256_obj(im,omit=("manifest_sha256",)) != im.get("manifest_sha256"): errors.append("input_manifest hash mismatch")
        im_base=sha256_obj(im,omit=("manifest_id","manifest_sha256"))
        if im.get("manifest_id") != "im-"+im_base[:24]: errors.append("input_manifest id mismatch")
        facts=result.get("resolved_facts",[])
        for f in facts:
            if sha256_obj(f,omit=("fact_sha256",)) != f.get("fact_sha256"): errors.append(f"fact hash mismatch:{f.get('fact_key')}")
        fs=hashlib.sha256(canonical_bytes([f["fact_sha256"] for f in facts])).hexdigest()
        gates=result.get("gate_results",[])
        for g in gates:
            if sha256_obj(g,omit=("gate_result_sha256",)) != g.get("gate_result_sha256"): errors.append(f"gate hash mismatch:{g.get('gate_id')}")
        gs=hashlib.sha256(canonical_bytes([g["gate_result_sha256"] for g in gates])).hexdigest()
        cert=result["operational_certificate"]
        if sha256_obj(cert,omit=("certificate_sha256",)) != cert.get("certificate_sha256"): errors.append("certificate hash mismatch")
        cert_base=sha256_obj(cert,omit=("certification_id","certificate_sha256"))
        if cert.get("certification_id") != "oc-"+cert_base[:24]: errors.append("certificate id mismatch")
        if cert.get("resolved_fact_set_sha256") != fs: errors.append("certificate fact-set hash mismatch")
        if cert.get("gate_results_sha256") != gs: errors.append("certificate gate-set hash mismatch")
        seal=result["final_seal"]
        if sha256_obj(seal,omit=("seal_sha256",)) != seal.get("seal_sha256"): errors.append("final seal hash mismatch")
        seal_base=sha256_obj(seal,omit=("seal_id","seal_sha256"))
        if seal.get("seal_id") != "seal-"+seal_base[:24]: errors.append("final seal id mismatch")
        if seal.get("operational_certificate_sha256") != cert.get("certificate_sha256"): errors.append("seal certificate binding mismatch")
        if seal.get("input_manifest_sha256") != im.get("manifest_sha256"): errors.append("seal input manifest binding mismatch")
        expected_seal_status={"PASS":"VERIFIED_PASS","FAIL":"VERIFIED_FAIL","BLOCKED":"VERIFIED_BLOCKED","UNKNOWN":"VERIFIED_UNKNOWN"}.get(cert.get("overall_outcome"))
        if seal.get("seal_status") != expected_seal_status: errors.append("seal status/certificate outcome mismatch")
        if cert.get("engine_build_sha256") != self.engine_build_sha256: errors.append("certificate engine build mismatch")
        if result.get("promotion_candidate"):
            pc=result["promotion_candidate"]
            if sha256_obj(pc,omit=("candidate_sha256",)) != pc.get("candidate_sha256"): errors.append("promotion candidate hash mismatch")
            pc_base=sha256_obj(pc,omit=("candidate_id","candidate_sha256"))
            if pc.get("candidate_id") != "pc-"+pc_base[:24]: errors.append("promotion candidate id mismatch")
            if pc.get("operational_certificate_sha256") != cert.get("certificate_sha256"): errors.append("promotion candidate certificate binding mismatch")
        return {"verified":not errors,"errors":errors}

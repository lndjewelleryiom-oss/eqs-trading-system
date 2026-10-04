from datetime import datetime
from typing import Any, Dict, List
import re

BLOCKING_REASON_PREFIXES = ("BLOCK_", "CRITICAL_", "UNKNOWN_")
ALLOWED_ACTIONS = {
    "BLOCK_NEW_RISK","DISABLE_VENUE","DEMOTE_MODE",
    "ACTIVATE_SCOPED_KILL_SWITCH","ACTIVATE_PORTFOLIO_KILL_SWITCH"
}

def _dt(s: str):
    return datetime.fromisoformat(s.replace("Z","+00:00"))

def semantic_validate(record_type: str, r: Dict[str, Any]) -> List[Dict[str, Any]]:
    failures = []
    def fail(rule_id, code, severity, detail):
        failures.append({"rule_id":rule_id,"failure_code":code,"severity":severity,"detail":detail})

    if record_type == "service_health":
        for d in r.get("dependency_states", []):
            if d.get("safety_critical") and d.get("health_state") == "UNKNOWN":
                fail("OPS-R001","OPS_UNKNOWN_CRITICAL_DEPENDENCY","CRITICAL",d.get("dependency_id"))
        if r.get("actual_mode") is not None and r.get("authorised_mode") != r.get("actual_mode"):
            fail("OPS-R002","OPS_MODE_MISMATCH","CRITICAL","authorised_mode != actual_mode")
        if r.get("health_state") == "HEALTHY":
            if any(str(x).startswith(BLOCKING_REASON_PREFIXES) for x in r.get("reason_codes", [])):
                fail("OPS-R017","OPS_HEALTHY_WITH_BLOCKING_REASON","HIGH","blocking reason present")

    elif record_type == "mode_awareness":
        if r.get("authorised_mode") != r.get("actual_mode") or r.get("match") is False:
            fail("OPS-R002","OPS_MODE_MISMATCH","CRITICAL","authorised/actual mode mismatch")

    elif record_type == "reconciliation":
        if r.get("material_unresolved") is True or r.get("state") == "UNKNOWN":
            fail("OPS-R003","OPS_MATERIAL_RECON_MISMATCH","CRITICAL","material or unknown reconciliation")
        if not r.get("materiality_policy_id"):
            fail("OPS-R012","OPS_POLICY_REFERENCE_MISSING","HIGH","materiality_policy_id missing")

    elif record_type == "execution_health":
        if r.get("unknown_order_outcome_count",0) > 0 and r.get("health_state") == "HEALTHY":
            fail("OPS-R016","OPS_UNKNOWN_ORDER_OUTCOME","CRITICAL","unknown order outcome while HEALTHY")

    elif record_type == "commissioning_run":
        if r.get("status") == "PASS":
            required_bad = [c for c in r.get("checks",[]) if c.get("required") and c.get("result") != "PASS"]
            if required_bad or not r.get("terminal_completion_verified") or not r.get("seal_verified"):
                fail("OPS-R005","OPS_INVALID_COMMISSIONING_PASS","CRITICAL","PASS lacks required evidence")
            if required_bad:
                fail("OPS-R018","OPS_FAILING_REQUIRED_CHECK","CRITICAL","required check not PASS")
            if r.get("commissioning_type") == "SOAK" and r.get("ended_at"):
                dur = (_dt(r["ended_at"]) - _dt(r["started_at"])).total_seconds()
                if dur < r.get("required_duration_seconds",0):
                    fail("OPS-R019","OPS_PASS_WITHOUT_DURATION","CRITICAL",f"{dur} < {r.get('required_duration_seconds',0)}")
        if r.get("ended_at") and _dt(r["ended_at"]) < _dt(r["started_at"]):
            fail("OPS-R011","OPS_TIME_REGRESSION","HIGH","ended_at < started_at")

    elif record_type == "restart_recovery_test":
        if r.get("result") == "PASS" and not r.get("authoritative_state_restored"):
            fail("OPS-R014","OPS_RECOVERY_NOT_AUTHORITATIVE","CRITICAL","PASS without authoritative restoration")
        if r.get("result") == "PASS" and r.get("reconciliation_state") in {"UNKNOWN","STALE","MISMATCH"}:
            fail("OPS-R015","OPS_RECON_UNKNOWN_ON_RECOVERY","CRITICAL","PASS with non-matched reconciliation")

    elif record_type == "safety_action_request":
        if r.get("requested_action") not in ALLOWED_ACTIONS:
            fail("OPS-R009","OPS_PROMOTION_AUTHORITY_BREACH","CRITICAL","requested action not allowed")

    elif record_type == "incident":
        st = _dt(r["started_at"])
        if _dt(r["detected_at"]) < st:
            fail("OPS-R013","OPS_INCIDENT_TIME_INVALID","HIGH","detected_at < started_at")
        for k in ("recovered_at","resolved_at"):
            if r.get(k) and _dt(r[k]) < st:
                fail("OPS-R013","OPS_INCIDENT_TIME_INVALID","HIGH",f"{k} < started_at")

    elif record_type == "evidence_seal":
        if r.get("verification_status") != "VERIFIED":
            fail("OPS-R020","OPS_UNVERIFIED_SEAL","CRITICAL","seal not VERIFIED")

    return failures

def aggregate(failures: List[Dict[str, Any]]) -> Dict[str, Any]:
    sev_order = {"INFO":0,"WARNING":1,"HIGH":2,"CRITICAL":3}
    max_sev = max((sev_order[f["severity"]] for f in failures), default=-1)
    return {
        "pass": len(failures) == 0,
        "failure_count": len(failures),
        "max_severity": None if max_sev < 0 else [k for k,v in sev_order.items() if v == max_sev][0],
        "fail_closed_block": any(f["severity"] == "CRITICAL" for f in failures)
    }

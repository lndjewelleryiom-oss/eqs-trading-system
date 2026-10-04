from datetime import datetime,timezone
from uuid import uuid4
import pytest
from quant_system.execution.audit import ExecutionAuditTrail
from quant_system.execution.telemetry import ExecutionTelemetry,PrivateReadHealth
NOW=datetime(2026,9,26,12,tzinfo=timezone.utc)
def test_audit_is_append_only_hash_evidence(tmp_path):
 a=ExecutionAuditTrail(tmp_path/"a.db");i=uuid4();a.append(i,"RECOVERY_ATTEMPT",NOW,{"venue":"OKX","order_id":"9"})
 r=a.records(i);assert len(r)==1 and len(r[0].sha256)==64 and r[0].evidence["venue"]=="OKX"
def test_audit_rejects_credential_fields(tmp_path):
 a=ExecutionAuditTrail(tmp_path/"a.db")
 with pytest.raises(ValueError):a.append(uuid4(),"BAD",NOW,{"api_key":"never"})
def test_telemetry_exposes_blocked_health_without_control_authority():
 t=ExecutionTelemetry("OKX_SWAP",PrivateReadHealth.RECONCILIATION_DEGRADED,1,True,("UNRESOLVED_EXECUTION_STATE",))
 assert t.reconciliation_blocked and t.unresolved_executions==1

from __future__ import annotations
from dataclasses import dataclass
from enum import StrEnum
class PrivateReadHealth(StrEnum):
    HEALTHY="HEALTHY";STALE="STALE";RATE_LIMITED="RATE_LIMITED";AUTH_UNAVAILABLE="AUTH_UNAVAILABLE";HISTORY_UNAVAILABLE="HISTORY_UNAVAILABLE";RECONCILIATION_DEGRADED="RECONCILIATION_DEGRADED"
@dataclass(frozen=True,slots=True)
class ExecutionTelemetry:
    venue:str
    private_read_health:PrivateReadHealth
    unresolved_executions:int
    reconciliation_blocked:bool
    reason_codes:tuple[str,...]

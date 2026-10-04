from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil


GIB = 1024 ** 3


@dataclass(frozen=True, slots=True)
class StorageGuardState:
    path: str
    free_bytes: int
    free_gib: float
    reserve_bytes: int
    critical_bytes: int
    allow_new_research: bool
    allow_large_downloads: bool
    state: str
    reason: str


def evaluate_storage_guard(
    path: str | Path,
    *,
    reserve_bytes: int = 8 * GIB,
    critical_bytes: int = 4 * GIB,
) -> StorageGuardState:
    if reserve_bytes <= critical_bytes or critical_bytes <= 0:
        raise ValueError("storage thresholds must satisfy reserve > critical > 0")
    probe = Path(path)
    usage = shutil.disk_usage(probe)
    free = int(usage.free)
    if free <= critical_bytes:
        state = "CRITICAL_RESERVE"
        reason = "Free space is at or below the critical reserve; block all new research/download growth."
    elif free <= reserve_bytes:
        state = "RESEARCH_BACKPRESSURE"
        reason = "Free space is below the research reserve; block new research/download growth."
    else:
        state = "NORMAL"
        reason = "Free space is above the configured research reserve."
    return StorageGuardState(
        path=str(probe.resolve()),
        free_bytes=free,
        free_gib=round(free / GIB, 3),
        reserve_bytes=reserve_bytes,
        critical_bytes=critical_bytes,
        allow_new_research=state == "NORMAL",
        allow_large_downloads=state == "NORMAL",
        state=state,
        reason=reason,
    )

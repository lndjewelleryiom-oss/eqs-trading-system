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


@dataclass(frozen=True, slots=True)
class BoundedOffhostWriteState:
    destination: str
    local_safety_path: str
    expected_bytes: int
    destination_free_bytes: int
    local_free_bytes: int
    destination_floor_bytes: int
    local_critical_bytes: int
    local_cache_headroom_bytes: int
    different_volume: bool
    allowed: bool
    state: str
    reason: str


def evaluate_bounded_offhost_write(
    destination: str | Path,
    *,
    expected_bytes: int,
    local_safety_path: str | Path,
    max_expected_bytes: int = 64 * 1024 ** 2,
    destination_floor_bytes: int = 1024 ** 3,
    local_critical_bytes: int = 4 * GIB,
    cache_multiplier: int = 4,
) -> BoundedOffhostWriteState:
    """Allow only small, explicitly bounded writes to a different mounted volume.

    This is not a relaxation of ``evaluate_storage_guard``.  It is a separate
    policy for off-host/cloud-mounted destinations where the project drive is
    already under research backpressure.  The local drive must retain its hard
    critical reserve plus conservative cache headroom, and the destination must
    retain its own floor after the entire bounded write.
    """
    if expected_bytes <= 0:
        raise ValueError("expected_bytes must be positive")
    if expected_bytes > max_expected_bytes:
        return BoundedOffhostWriteState(
            str(Path(destination).resolve()), str(Path(local_safety_path).resolve()), expected_bytes,
            0, 0, destination_floor_bytes, local_critical_bytes, 0, False, False,
            "BLOCKED_SIZE_CAP", "Expected write exceeds bounded off-host size cap."
        )
    if cache_multiplier < 1:
        raise ValueError("cache_multiplier must be >= 1")
    dest = Path(destination).resolve()
    local = Path(local_safety_path).resolve()
    dest_usage = shutil.disk_usage(dest)
    local_usage = shutil.disk_usage(local)
    different_volume = dest.drive.lower() != local.drive.lower()
    cache_headroom = expected_bytes * cache_multiplier
    dest_ok = int(dest_usage.free) - expected_bytes >= destination_floor_bytes
    local_ok = int(local_usage.free) - cache_headroom > local_critical_bytes
    allowed = different_volume and dest_ok and local_ok
    if not different_volume:
        state, reason = "BLOCKED_SAME_VOLUME", "Destination must be on a different mounted volume."
    elif not dest_ok:
        state, reason = "BLOCKED_DESTINATION_FLOOR", "Destination would fall below its bounded-write reserve."
    elif not local_ok:
        state, reason = "BLOCKED_LOCAL_CACHE_HEADROOM", "Local project drive lacks critical reserve plus cache headroom."
    else:
        state, reason = "BOUNDED_OFFHOST_ALLOWED", "Small off-host write fits both destination and local hard safety floors."
    return BoundedOffhostWriteState(
        destination=str(dest), local_safety_path=str(local), expected_bytes=expected_bytes,
        destination_free_bytes=int(dest_usage.free), local_free_bytes=int(local_usage.free),
        destination_floor_bytes=destination_floor_bytes, local_critical_bytes=local_critical_bytes,
        local_cache_headroom_bytes=cache_headroom, different_volume=different_volume,
        allowed=allowed, state=state, reason=reason,
    )

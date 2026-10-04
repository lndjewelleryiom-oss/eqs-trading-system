from collections import namedtuple
from pathlib import Path

import pytest

from quant_system.operations.storage_guard import GIB, evaluate_bounded_offhost_write, evaluate_storage_guard


Usage = namedtuple("Usage", "total used free")


@pytest.mark.parametrize(
    ("free", "state", "allowed"),
    [
        (12 * GIB, "NORMAL", True),
        (8 * GIB, "RESEARCH_BACKPRESSURE", False),
        (6 * GIB, "RESEARCH_BACKPRESSURE", False),
        (4 * GIB, "CRITICAL_RESERVE", False),
        (2 * GIB, "CRITICAL_RESERVE", False),
    ],
)
def test_storage_guard_states(monkeypatch, tmp_path: Path, free: int, state: str, allowed: bool) -> None:
    monkeypatch.setattr(
        "quant_system.operations.storage_guard.shutil.disk_usage",
        lambda path: Usage(100 * GIB, 100 * GIB - free, free),
    )
    result = evaluate_storage_guard(tmp_path)
    assert result.state == state
    assert result.allow_new_research is allowed
    assert result.allow_large_downloads is allowed


def test_storage_guard_rejects_invalid_thresholds(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        evaluate_storage_guard(tmp_path, reserve_bytes=4 * GIB, critical_bytes=4 * GIB)


def _offhost_usage(path):
    drive = Path(path).drive.upper()
    if drive.startswith("H:"):
        return Usage(15 * GIB, 10 * GIB, 5 * GIB)
    return Usage(500 * GIB, 495 * GIB, 5 * GIB)


def test_bounded_offhost_allows_small_different_volume_write(monkeypatch) -> None:
    monkeypatch.setattr("quant_system.operations.storage_guard.shutil.disk_usage", _offhost_usage)
    result = evaluate_bounded_offhost_write(
        Path("H:/offhost"), expected_bytes=16 * 1024 ** 2, local_safety_path=Path("C:/project")
    )
    assert result.allowed is True
    assert result.state == "BOUNDED_OFFHOST_ALLOWED"
    assert result.different_volume is True


def test_bounded_offhost_rejects_same_volume(monkeypatch) -> None:
    monkeypatch.setattr("quant_system.operations.storage_guard.shutil.disk_usage", _offhost_usage)
    result = evaluate_bounded_offhost_write(
        Path("C:/other"), expected_bytes=16 * 1024 ** 2, local_safety_path=Path("C:/project")
    )
    assert result.allowed is False
    assert result.state == "BLOCKED_SAME_VOLUME"


def test_bounded_offhost_rejects_oversized_write(monkeypatch) -> None:
    monkeypatch.setattr("quant_system.operations.storage_guard.shutil.disk_usage", _offhost_usage)
    result = evaluate_bounded_offhost_write(
        Path("H:/offhost"), expected_bytes=65 * 1024 ** 2, local_safety_path=Path("C:/project")
    )
    assert result.allowed is False
    assert result.state == "BLOCKED_SIZE_CAP"


def test_bounded_offhost_rejects_when_local_cache_headroom_crosses_critical(monkeypatch) -> None:
    def usage(path):
        drive = Path(path).drive.upper()
        if drive.startswith("H:"):
            return Usage(15 * GIB, 10 * GIB, 5 * GIB)
        return Usage(500 * GIB, 496 * GIB, 4 * GIB + 32 * 1024 ** 2)
    monkeypatch.setattr("quant_system.operations.storage_guard.shutil.disk_usage", usage)
    result = evaluate_bounded_offhost_write(
        Path("H:/offhost"), expected_bytes=16 * 1024 ** 2, local_safety_path=Path("C:/project"), cache_multiplier=4
    )
    assert result.allowed is False
    assert result.state == "BLOCKED_LOCAL_CACHE_HEADROOM"

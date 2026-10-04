from collections import namedtuple
from pathlib import Path

import pytest

from quant_system.operations.storage_guard import GIB, evaluate_storage_guard


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

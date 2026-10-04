from datetime import datetime, timezone
import json

import pytest

from quant_system.research.manifest import ExperimentManifest


def _manifest(**overrides):
    kwargs = dict(
        strategy_id="STRAT-001",
        hypothesis="A stable synthetic edge survives costs.",
        data_fingerprints={"prices": "sha256:abc", "fundamentals": "sha256:def"},
        code_version="deadbeef",
        parameters={"lookback": 20, "threshold": 1.5},
        cost_assumptions={"commission_bps": 1.0, "slippage_bps": 2.0},
        split_spec={"type": "walk_forward", "train": 100, "test": 20},
        random_seed=42,
        software_versions={"python": "3.13"},
        created_at=datetime(2026, 9, 21, tzinfo=timezone.utc),
    )
    kwargs.update(overrides)
    return ExperimentManifest(**kwargs)


def test_manifest_hash_is_canonical_and_reproducible():
    first = _manifest(parameters={"threshold": 1.5, "lookback": 20})
    second = _manifest(
        parameters={"lookback": 20, "threshold": 1.5},
        created_at=datetime(2026, 9, 22, tzinfo=timezone.utc),
    )
    assert first.fingerprint == second.fingerprint
    assert first.experiment_id == second.experiment_id


def test_manifest_changes_when_research_inputs_change():
    assert _manifest(random_seed=42).fingerprint != _manifest(random_seed=43).fingerprint
    assert _manifest(code_version="a").fingerprint != _manifest(code_version="b").fingerprint


def test_manifest_round_trip_and_tamper_detection(tmp_path):
    path = tmp_path / "experiment.json"
    manifest = _manifest()
    manifest.write(path)
    restored = ExperimentManifest.read(path)
    assert restored.fingerprint == manifest.fingerprint

    record = json.loads(path.read_text())
    record["manifest"]["parameters"]["lookback"] = 999
    path.write_text(json.dumps(record))
    with pytest.raises(ValueError, match="fingerprint mismatch"):
        ExperimentManifest.read(path)


def test_manifest_rejects_non_finite_values():
    with pytest.raises(ValueError, match="finite"):
        _manifest(parameters={"bad": float("nan")})

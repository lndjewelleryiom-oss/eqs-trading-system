from hashlib import sha256
import json

import pytest

from quant_system.operations.canonical_snapshot import load_snapshot, pointer_path, publish_snapshot


def _canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _snapshot(generation: int, stage: str, marker: str):
    doc = {
        "schema_id": "EQS-CANONICAL-TRACKER-SNAPSHOT-V4",
        "projection_generation": generation,
        "projection_stage": stage,
        "updated_at": "2026-10-05T00:00:00Z",
        "tracker": {"marker": marker},
    }
    doc["record_sha256"] = sha256(_canonical(doc)).hexdigest()
    return doc


def test_pointer_publication_leaves_legacy_file_untouched_and_loads_generation(tmp_path):
    target = tmp_path / "snapshot.json"
    legacy = _snapshot(1, "V4_FORWARD", "legacy")
    target.write_text(json.dumps(legacy))
    fresh = _snapshot(2, "V4_BASE", "fresh")
    pointer = publish_snapshot(target, fresh)
    assert json.loads(target.read_text())["tracker"]["marker"] == "legacy"
    assert pointer["projection_generation"] == 2
    assert load_snapshot(target)["tracker"]["marker"] == "fresh"


def test_forward_stage_can_publish_same_generation_as_second_immutable_generation(tmp_path):
    target = tmp_path / "snapshot.json"
    publish_snapshot(target, _snapshot(7, "V4_BASE", "base"))
    publish_snapshot(target, _snapshot(7, "V4_FORWARD", "forward"))
    loaded = load_snapshot(target)
    assert loaded["projection_generation"] == 7
    assert loaded["projection_stage"] == "V4_FORWARD"
    assert loaded["tracker"]["marker"] == "forward"
    generations = list((tmp_path / "snapshot.generations").glob("*.json"))
    assert len(generations) == 2


def test_pointer_tamper_fails_closed(tmp_path):
    target = tmp_path / "snapshot.json"
    publish_snapshot(target, _snapshot(3, "V4_BASE", "ok"))
    pointer = pointer_path(target)
    doc = json.loads(pointer.read_text())
    doc["projection_generation"] = 999
    pointer.write_text(json.dumps(doc))
    with pytest.raises(ValueError, match="record_sha256"):
        load_snapshot(target)


def test_generation_tamper_fails_closed(tmp_path):
    target = tmp_path / "snapshot.json"
    pointer = publish_snapshot(target, _snapshot(4, "V4_BASE", "ok"))
    generation = tmp_path / "snapshot.generations" / pointer["generation_file"]
    generation.write_text(generation.read_text().replace('"ok"', '"bad"'))
    with pytest.raises(ValueError, match="file hash"):
        load_snapshot(target)

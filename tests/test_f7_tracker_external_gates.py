import json
from pathlib import Path


def test_f7_external_gates_are_explicit_and_evidence_gated():
    tracker = json.loads((Path(__file__).parents[1] / "tracker.json").read_text())
    f7 = next(component for component in tracker["components"] if component["id"] == "F7")
    gates = f7["external_gates"]

    assert [gate["id"] for gate in gates] == ["F7.E1", "F7.E2", "F7.E3", "F7.E4", "F7.E5"]
    assert f7["status"] == "BLOCKED"
    for gate in gates:
        assert gate["status"] == "BLOCKED"
        assert gate["pass_condition"].strip()
        assert gate["objective_evidence_required"]
        assert gate["objective_evidence_recorded"] == []
        assert gate["blockers"]

    assert any("venue-specific LIVE_1" in item for item in f7["remaining"])

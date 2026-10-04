from pathlib import Path


ROOT = Path(__file__).parents[1]
TEMPLATE = ROOT / "docs" / "F7_EXTERNAL_GATE_APPROVAL_TEMPLATE.md"


def test_f7_signed_approval_template_contains_required_controls():
    text = TEMPLATE.read_text()

    for gate_id in ("F7.E1", "F7.E2", "F7.E3", "F7.E4", "F7.E5"):
        assert gate_id in text
        assert text.count(gate_id) >= 2

    for required in (
        "Approver full name",
        "Approver role / authority",
        "Authorized scope",
        "Validity period",
        "Objective",
        "Evidence",
        "Automatic revocation conditions",
        "Signed document hash",
        "LIVE_1 ONLY",
        "Overall decision",
        "Fail-closed rule",
    ):
        assert required.lower() in text.lower()


def test_f7_template_revocation_covers_critical_external_and_runtime_changes():
    text = TEMPLATE.read_text().lower()
    for trigger in (
        "expires",
        "credential",
        "permission scopes change",
        "repository commit",
        "risk-policy version",
        "reconciliation mismatch",
        "maximum daily loss",
        "legal/compliance approval is withdrawn",
        "authorized human revokes approval",
    ):
        assert trigger in text

    assert "return to `live_0`" in text
    assert "block new broker submissions" in text


EXAMPLE = ROOT / "docs" / "F7_EXTERNAL_GATE_APPROVAL_EXAMPLE_TEST_DATA.md"


def test_f7_completed_example_is_explicitly_non_authorizing_test_data():
    text = EXAMPLE.read_text()
    lower = text.lower()

    assert "test_data: true" in lower
    assert "authorization_effect: none" in lower
    assert "may_authorize_live_trading: false" in lower
    assert "runtime_eligibility: reject" in lower
    assert "overall_decision: not_approved" in lower
    assert "**overall decision:** `not approved`" in lower
    assert "deployment stage after verification: `live_0`" in lower
    assert "tracker evidence references added to: `none`" in lower
    assert "test-signature-invalid-non-authorizing" in lower

    for gate_id in ("F7.E1", "F7.E2", "F7.E3", "F7.E4", "F7.E5"):
        assert gate_id in text

    # A completed test example may demonstrate fields, but it must never contain
    # a positive gate decision that a permissive parser could mistake for approval.
    assert "**Decision:** `PASSED" not in text
    assert "**Overall decision:** `APPROVED FOR LIVE_1`" not in text


def test_f7_test_example_is_not_recorded_as_objective_gate_evidence():
    import json

    tracker = json.loads((ROOT / "tracker.json").read_text())
    f7 = next(component for component in tracker["components"] if component["id"] == "F7")
    example_name = EXAMPLE.name

    for gate in f7["external_gates"]:
        assert gate["objective_evidence_recorded"] == []
        assert all(example_name not in str(item) for item in gate["objective_evidence_recorded"])

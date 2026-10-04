import json
from pathlib import Path

from quant_system.ci.test_data_evidence_guard import scan_repository


ROOT = Path(__file__).parents[1]
EXAMPLE_REL = "docs/F7_EXTERNAL_GATE_APPROVAL_EXAMPLE_TEST_DATA.md"


def _write_test_artifact(root: Path) -> None:
    path = root / EXAMPLE_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# Example\n\nTEST_DATA: true\nAUTHORIZATION_EFFECT: NONE\n",
        encoding="utf-8",
    )


def _write_tracker(root: Path, payload: dict) -> None:
    (root / "tracker.json").write_text(json.dumps(payload), encoding="utf-8")


def test_current_repository_has_no_test_data_in_live_evidence_fields():
    assert scan_repository(ROOT) == []


def test_guard_rejects_test_data_in_objective_evidence_recorded(tmp_path: Path):
    _write_test_artifact(tmp_path)
    _write_tracker(
        tmp_path,
        {
            "components": [
                {
                    "id": "F7",
                    "external_gates": [
                        {
                            "id": "F7.E1",
                            "objective_evidence_recorded": [EXAMPLE_REL],
                        }
                    ],
                }
            ]
        },
    )
    violations = scan_repository(tmp_path)
    assert any("objective_evidence_recorded" in item.field_path for item in violations)


def test_guard_rejects_test_data_in_signed_approval_references(tmp_path: Path):
    _write_test_artifact(tmp_path)
    _write_tracker(
        tmp_path,
        {
            "signed_approval_references": [
                {"record": EXAMPLE_REL, "sha256": "TEST-HASH-NOT-VALID"}
            ]
        },
    )
    violations = scan_repository(tmp_path)
    assert any("signed_approval_references" in item.field_path for item in violations)


def test_guard_rejects_inline_test_data_in_live_authorization_field(tmp_path: Path):
    _write_tracker(
        tmp_path,
        {
            "live_capital_authorization": {
                "TEST_DATA": True,
                "record_reference": "approval-record-001",
                "authorized": False,
            }
        },
    )
    violations = scan_repository(tmp_path)
    assert any("live_capital_authorization" in item.field_path for item in violations)


def test_guard_rejects_test_evidence_uri_in_live_authorization_child_field(tmp_path: Path):
    _write_tracker(
        tmp_path,
        {
            "live_authorization": {
                "record_reference": "test-evidence://f7/example/record.md",
                "authorized": False,
            }
        },
    )
    violations = scan_repository(tmp_path)
    assert any("live_authorization.record_reference" in item.field_path for item in violations)


def test_ci_workflow_runs_guard_before_test_suite():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    guard_command = "python -m quant_system.ci.test_data_evidence_guard --root ."
    pytest_command = "pytest -q"
    assert guard_command in workflow
    assert pytest_command in workflow
    assert workflow.index(guard_command) < workflow.index(pytest_command)

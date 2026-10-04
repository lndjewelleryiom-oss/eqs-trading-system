import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_v4_scope_uses_synchronised_complete_month_intersection():
    doc = json.loads((ROOT / "research" / "preregistrations" / "v4" / "DATA-SCOPE-V4.json").read_text())
    assert doc["version"] == "4.0.1"
    policy = doc["coverage_policy"]
    assert policy["mode"] == "SYNCHRONIZED_COMPLETE_MONTH_INTERSECTION"
    assert policy["excluded_months"] == ["2023-02", "2023-04", "2023-11"]
    assert policy["eligible_month_count"] == 36
    assert policy["imputation_permitted"] is False
    assert policy["partial_month_use_permitted"] is False
    assert policy["strategy_state_must_reset_after_excluded_interval"] is True
    assert doc["availability_summary"]["synchronized_required_objects"] == 108
    assert doc["window"]["locked_oos_access"] == "SEALED_NOT_PERMITTED"
    assert doc["research_authority"]["strategy_outcomes_permitted_by_this_scope"] is False
    assert doc["research_authority"]["managed_paper_authority"] is False
    assert doc["research_authority"]["live_authority"] is False

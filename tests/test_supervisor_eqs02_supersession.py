from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def module():
    spec=importlib.util.spec_from_file_location("eqs_supervisor_test",ROOT/"scripts"/"eqs_supervisor.py")
    assert spec and spec.loader
    mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); return mod


def acceptance():
    return json.loads((ROOT/"artifacts"/"test-evidence"/"EQS02_ALPACA_AUTHENTICATED_ACCEPTANCE.json").read_text(encoding="utf-8"))


def test_later_sealed_alpaca_acceptance_satisfies_old_source_authority_blocker():
    mod=module(); doc=acceptance()
    assert mod.eqs02_source_authority_satisfied(doc) is True


def test_source_authority_supersession_fails_closed_on_boundary_change():
    mod=module(); original=acceptance()
    for key,value in (
        ("broker_submission_enabled",True),
        ("live_authority",True),
        ("trading_endpoint_used",True),
        ("production_paper_market_data_authority",False),
    ):
        doc=dict(original); doc[key]=value
        assert mod.eqs02_source_authority_satisfied(doc) is False


def test_current_blocker_queue_no_longer_reports_superseded_eqs02_source_authority():
    mod=module(); queue=mod.build_blocker_queue()
    items=[row.get("item") for row in queue.get("items",[])]
    assert "APPROVED_PRODUCTION_STOCKS_ETFS_MARKET_DATA_SOURCE_REQUIRED" not in items
    assert "GENUINE_FORWARD_PAPER_BAR_SEQUENCE_REQUIRED_AFTER_SOURCE_APPROVAL" in [row.get("item") for row in queue.get("deferred_jobs",[])]

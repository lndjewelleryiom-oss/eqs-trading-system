from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from quant_system.research.feasibility_trial_ledger_v2 import FeasibilityTrialLedger


def ledger(tmp_path: Path) -> FeasibilityTrialLedger:
    return FeasibilityTrialLedger(tmp_path / "trials.db")


def test_trial_is_registered_before_outcomes_and_hash_chain_verifies(tmp_path: Path) -> None:
    store = ledger(tmp_path)
    try:
        row = store.register_trial(
            trial_id="trial-1", campaign_id="c", campaign_fingerprint="a" * 64,
            configuration_sha256="b" * 64, acquisition_manifest_sha256="c" * 64,
        )
        assert row["state"] == "CREATED"
        assert row["empirical_outcomes_consumed"] == 0
        assert row["r13_authority"] == 0
        assert row["live_authority"] == 0
        assert store.verify_hash_chain()
    finally:
        store.close()


def test_idempotent_retry_does_not_double_count_trial(tmp_path: Path) -> None:
    store = ledger(tmp_path)
    try:
        kwargs = dict(
            trial_id="same", campaign_id="c", campaign_fingerprint="a" * 64,
            configuration_sha256="b" * 64, acquisition_manifest_sha256="c" * 64,
        )
        store.register_trial(**kwargs)
        store.register_trial(**kwargs)
        assert store.trial_count() == 1
        with pytest.raises(ValueError, match="conflicting"):
            store.register_trial(**{**kwargs, "configuration_sha256": "d" * 64})
    finally:
        store.close()


def test_outcome_consumption_is_one_way_and_result_immutable(tmp_path: Path) -> None:
    store = ledger(tmp_path)
    try:
        store.register_trial(
            trial_id="trial", campaign_id="c", campaign_fingerprint="a" * 64,
            configuration_sha256="b" * 64, acquisition_manifest_sha256="c" * 64,
        )
        store.transition("trial", "RUNNING", outcomes_consumed=True)
        digest = store.persist_result("trial", {"status": "DONE", "pnl": "-1"})
        assert len(digest) == 64
        store.transition("trial", "FINISHED", outcomes_consumed=True)
        assert store.trial("trial")["empirical_outcomes_consumed"] == 1
        with pytest.raises(ValueError, match="illegal"):
            store.transition("trial", "RUNNING")
        with sqlite3.connect(store.path) as raw:
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                raw.execute("UPDATE feasibility_trial_results SET result_json='{}' WHERE trial_id='trial'")
    finally:
        store.close()


def test_blocked_pre_outcome_trial_remains_nonempirical(tmp_path: Path) -> None:
    store = ledger(tmp_path)
    try:
        store.register_trial(
            trial_id="blocked", campaign_id="c", campaign_fingerprint="a" * 64,
            configuration_sha256="b" * 64, acquisition_manifest_sha256="c" * 64,
        )
        store.transition("blocked", "BLOCKED", error_code="SOURCE_MISSING", outcomes_consumed=False)
        row = store.trial("blocked")
        assert row["state"] == "BLOCKED"
        assert row["empirical_outcomes_consumed"] == 0
        assert store.verify_hash_chain()
    finally:
        store.close()

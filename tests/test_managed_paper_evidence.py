import json
import sqlite3
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path

from quant_system.performance.lifecycle import (
    PersistentStrategyLifecycle,
    StrategyLifecycleState,
)
from quant_system.performance.managed_evidence import ManagedPaperEvidenceRecorder


def _canonical(obj):
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _sealed(path: Path, body: dict):
    doc = dict(body)
    doc["record_sha256"] = sha256(_canonical(doc)).hexdigest()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")


def _runtime(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE runtime_events(
            id INTEGER PRIMARY KEY,
            runtime_id TEXT,
            sequence INTEGER,
            event_type TEXT,
            occurred_at TEXT,
            payload_json TEXT,
            payload_sha256 TEXT
        );
        CREATE TABLE execution_v1_events(
            id INTEGER PRIMARY KEY,
            runtime_id TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            event_id TEXT NOT NULL,
            execution_intent_id TEXT NOT NULL,
            client_order_id TEXT NOT NULL,
            runtime_mode TEXT NOT NULL,
            truth_source TEXT NOT NULL,
            authoritative_external_truth INTEGER NOT NULL,
            occurred_at TEXT NOT NULL,
            event_json TEXT NOT NULL,
            event_sha256 TEXT NOT NULL
        );
        CREATE TABLE execution_v1_bindings(
            runtime_id TEXT NOT NULL,
            client_order_id TEXT NOT NULL,
            execution_intent_id TEXT NOT NULL,
            mode TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            intent_json TEXT NOT NULL,
            intent_sha256 TEXT NOT NULL,
            capability_json TEXT NOT NULL,
            capability_sha256 TEXT NOT NULL,
            interlock_json TEXT NOT NULL,
            interlock_sha256 TEXT NOT NULL,
            authority_json TEXT NOT NULL,
            authority_sha256 TEXT NOT NULL,
            PRIMARY KEY(runtime_id,client_order_id)
        );
        """
    )
    intent = {
        "strategy": {
            "strategy_id": "g1",
            "strategy_version": "v1",
            "campaign_id": "c1",
        },
        "route": {"venue_id": "BYBIT_LINEAR"},
    }
    event = {
        "event_id": "fill-event-1",
        "execution_id": "exec-intent-1",
        "client_order_id": "client-order-1",
        "venue_id": "BYBIT_LINEAR",
        "event_type": "FILL",
        "receive_timestamp": "2026-10-04T00:00:00+00:00",
        "payload_hash": "1" * 64,
        "fill": {
            "fill_id": "fill-1",
            "quantity": "1",
            "price": "100",
            "liquidity_role": "TAKER",
        },
        "fees": [],
        "schema_version": "EQS-EXEC-TRUTH-EVENT-v1.0",
    }
    def canon_ascii(x):
        return json.dumps(x, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    con.execute(
        """INSERT INTO execution_v1_bindings VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "paper-runtime","client-order-1","exec-intent-1","PAPER",
            "2026-10-03T23:59:59+00:00",
            canon_ascii(intent),sha256(canon_ascii(intent).encode()).hexdigest(),
            "{}","0"*64,"{}","0"*64,"{}","0"*64,
        ),
    )
    con.execute(
        """INSERT INTO execution_v1_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            1,"paper-runtime",1,"fill-event-1","exec-intent-1","client-order-1",
            "PAPER","SIMULATOR",0,"2026-10-04T00:00:00+00:00",
            canon_ascii(event),sha256(canon_ascii(event).encode()).hexdigest(),
        ),
    )
    con.commit()
    con.close()


def _base(root: Path):
    ev = root / "artifacts" / "test-evidence"
    _runtime(root / "artifacts" / "commissioning" / "current_runtime_v1" / "runtime.db")

    life = PersistentStrategyLifecycle(ev / "EQS_STRATEGY_LIFECYCLE.db")
    life.register(
        strategy_id="g1",
        strategy_version="v1",
        source_class="GENUINE",
        research_trial_id="trial",
        research_evidence_sha256="a"*64,
        research_manifest_sha256="b"*64,
    )
    life.transition(
        "g1","v1",StrategyLifecycleState.VALIDATED,
        reasons=("PASS",),genuine_research_gate_passed=True,
    )
    life.transition(
        "g1","v1",StrategyLifecycleState.PAPER_CANARY,
        reasons=("CANARY",),evidence_sha256="c"*64,
    )

    _sealed(
        ev / "EQS00_PROGRAMME_STATE.json",
        {
            "hard_boundaries": {
                "live_authority": False,
                "broker_submission": "DISABLED",
                "eqs06_options": "FROZEN_EXCLUDED",
                "real_trades_permitted": False,
            }
        },
    )
    _sealed(
        ev / "EQS_LIVE_ADMISSION_POLICY.json",
        {
            "live_authority_permitted": False,
            "broker_submission_permitted": False,
        },
    )
    _sealed(
        ev / "EQS_SUPERVISOR_STATE.json",
        {
            "status": "HEALTHY",
            "runtime": {"verified": True},
            "dashboard": {"verified": True},
        },
    )
    _sealed(
        ev / "EQS_SUSTAINED_PAPER_HEALTH_LATEST.json",
        {"status": "PASS"},
    )
    return ev


def test_continuous_qualifying_window_reaches_pass(tmp_path):
    _base(tmp_path)
    recorder = ManagedPaperEvidenceRecorder(
        tmp_path,
        max_observation_gap_seconds=300,
        required_hours=0.01,
        required_attributed_trades=1,
    )
    t0 = datetime(2026,10,4,0,0,tzinfo=timezone.utc)
    first = recorder.record(now=t0)
    assert first.status == "RUNNING"
    second = recorder.record(now=t0 + timedelta(seconds=60))
    assert second.status == "PASS"
    assert second.total_attributed_trades == 1
    assert second.genuine_strategy_count == 1
    assert second.broker_send_count == 0
    assert second.live_authority is False
    assert recorder.verify_observation_hash_chain()


def test_observation_gap_resets_continuity_credit(tmp_path):
    _base(tmp_path)
    recorder = ManagedPaperEvidenceRecorder(
        tmp_path,
        max_observation_gap_seconds=120,
        required_hours=0.01,
        required_attributed_trades=1,
    )
    t0 = datetime(2026,10,4,0,0,tzinfo=timezone.utc)
    recorder.record(now=t0)
    later = recorder.record(now=t0 + timedelta(minutes=10))
    assert later.status == "RUNNING"
    assert later.elapsed_hours == 0.0


def test_live_authority_fails_closed(tmp_path):
    ev = _base(tmp_path)
    _sealed(
        ev / "EQS_LIVE_ADMISSION_POLICY.json",
        {
            "live_authority_permitted": True,
            "broker_submission_permitted": False,
        },
    )
    recorder = ManagedPaperEvidenceRecorder(
        tmp_path,
        required_hours=0.01,
        required_attributed_trades=1,
    )
    result = recorder.record(now=datetime(2026,10,4,0,0,tzinfo=timezone.utc))
    assert result.status == "BLOCKED"
    assert "LIVE_AUTHORITY_NOT_FALSE" in result.blocker_codes


def test_verified_source_outage_pauses_but_does_not_reset_clock(tmp_path):
    _base(tmp_path)
    recorder = ManagedPaperEvidenceRecorder(
        tmp_path,
        max_observation_gap_seconds=120,
        required_hours=0.02,
        required_attributed_trades=1,
    )
    t0 = datetime(2026,10,4,0,0,tzinfo=timezone.utc)
    recorder.record(now=t0)
    recorder.record_continuity_gap(
        gap_id="source-1",
        started_at=t0 + timedelta(minutes=1),
        ended_at=t0 + timedelta(minutes=9),
        gap_class="SOURCE_OUTAGE_VERIFIED",
        evidence_ref="provider-status-evidence-001",
        created_at=t0 + timedelta(minutes=9),
    )
    later = recorder.record(now=t0 + timedelta(minutes=10))
    assert later.status == "PASS"
    assert abs(later.elapsed_hours - (2/60)) < 1e-9
    assert abs(later.paused_gap_hours - (8/60)) < 1e-9


def test_host_runtime_outage_resets_even_when_gap_is_registered(tmp_path):
    _base(tmp_path)
    recorder = ManagedPaperEvidenceRecorder(
        tmp_path,
        max_observation_gap_seconds=120,
        required_hours=0.01,
        required_attributed_trades=1,
    )
    t0 = datetime(2026,10,4,0,0,tzinfo=timezone.utc)
    recorder.record(now=t0)
    recorder.record_continuity_gap(
        gap_id="host-1",
        started_at=t0 + timedelta(minutes=1),
        ended_at=t0 + timedelta(minutes=9),
        gap_class="HOST_RUNTIME_OUTAGE",
        evidence_ref="host-watchdog-001",
        created_at=t0 + timedelta(minutes=9),
    )
    later = recorder.record(now=t0 + timedelta(minutes=10))
    assert later.status == "RUNNING"
    assert later.elapsed_hours == 0.0
    assert later.paused_gap_hours == 0.0


def test_pause_clock_class_requires_substantive_evidence_reference(tmp_path):
    _base(tmp_path)
    recorder = ManagedPaperEvidenceRecorder(tmp_path)
    t0 = datetime(2026,10,4,0,0,tzinfo=timezone.utc)
    import pytest
    with pytest.raises(ValueError, match="substantive evidence"):
        recorder.record_continuity_gap(
            gap_id="source-weak",
            started_at=t0,
            ended_at=t0 + timedelta(minutes=10),
            gap_class="SOURCE_OUTAGE_VERIFIED",
            evidence_ref="x",
        )

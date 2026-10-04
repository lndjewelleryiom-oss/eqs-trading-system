from __future__ import annotations
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4
import pytest

from quant_system.data.infrastructure_gate import DatasetConsumer, InfrastructureDatasetBoundary, InfrastructureDatasetGateError
from quant_system.execution.broker import BrokerGateway, InMemoryBrokerAdapter, VenueOrder, VenueOrderStatus
from quant_system.nonlive import MarketDataSequenceError
from quant_system.monitoring import ExpectedBehavior, ObservedBehavior
from quant_system.runtime import PersistentPaperShadowRuntime, PersistentRuntimeStore, RuntimeMode, RuntimeStatus
from quant_system.shadow import ShadowExecutionEngine
from test_nonlive_execution_path import (
    _source, _pipeline, _paper_engine, _risk_snapshot, _bar,
    DECISION, INSTRUMENT, VENUE, MutableClock, RuntimeConfig, UTC,
)

MANIFEST=r"C:\Temp\EQS\infrastructure_only_dataset_manifest_v1_20260925\INFRASTRUCTURE_ONLY_DATASET_MANIFEST_V1.json"

def bound_source(path,boundary,**kwargs):
    base=_source(path,**kwargs)
    from quant_system.data.crypto_perps.feature_source import R13FeatureDatasetSource
    return R13FeatureDatasetSource(base._assembler,dataset_id=base._dataset_id,partitions=base._partitions,infrastructure_boundary=boundary,consumer=DatasetConsumer.FEATURE_ENGINE)

def assert_lineage(store,runtime_id,boundary):
    events=store.load_events(runtime_id)
    rows=[e for e in events if ((e.payload or {}).get("lineage") or {}).get("infrastructure_boundary_fingerprint")]
    assert rows
    assert all(e.payload["lineage"]["infrastructure_boundary_fingerprint"]==boundary.fingerprint for e in rows)
def test_infrastructure_resilience_fail_closed(tmp_path):
    boundary=InfrastructureDatasetBoundary.load(MANIFEST)
    # stale feed
    store=PersistentRuntimeStore(tmp_path/"stale.db"); rt=PersistentPaperShadowRuntime(runtime_id="stale",mode=RuntimeMode.PAPER,store=store,paper_engine=_paper_engine()); rt.start()
    r=_pipeline(bound_source(tmp_path/"stale-src",boundary),rt,max_data_age=timedelta(seconds=15)).run_once(instrument_id=INSTRUMENT,venue=VENUE,decision_time=DECISION+timedelta(minutes=2),risk_snapshot=_risk_snapshot())
    assert r.risk_decision.action.value=="HALT" and rt.status==RuntimeStatus.HALTED and rt.paper_engine.simulator.pending_count==0
    assert r.risk_decision.infrastructure_boundary_fingerprint==boundary.fingerprint
    # sequence fault
    store2=PersistentRuntimeStore(tmp_path/"seq.db"); rt2=PersistentPaperShadowRuntime(runtime_id="seq",mode=RuntimeMode.PAPER,store=store2,paper_engine=_paper_engine()); rt2.start()
    with pytest.raises(MarketDataSequenceError): _pipeline(bound_source(tmp_path/"seq-src",boundary,sequence_fault=True),rt2).run_once(instrument_id=INSTRUMENT,venue=VENUE,decision_time=DECISION,risk_snapshot=_risk_snapshot())
    assert rt2.status==RuntimeStatus.HALTED and rt2.paper_engine.simulator.pending_count==0
    # shadow reconciliation mismatch
    store3=PersistentRuntimeStore(tmp_path/"recon.db"); adapter=InMemoryBrokerAdapter(); rt3=PersistentPaperShadowRuntime(runtime_id="recon",mode=RuntimeMode.SHADOW,store=store3,shadow_engine=ShadowExecutionEngine(BrokerGateway(adapter,venue_submission_enabled=False))); rt3.start()
    _pipeline(bound_source(tmp_path/"recon-src",boundary),rt3).run_once(instrument_id=INSTRUMENT,venue=VENUE,decision_time=DECISION,risk_snapshot=_risk_snapshot()); assert_lineage(store3,"recon",boundary)
    unknown=uuid4(); adapter.open_orders[unknown]=VenueOrder(unknown,"UNKNOWN",INSTRUMENT,VenueOrderStatus.ACKNOWLEDGED,Decimal("1")); rec=rt3.reconcile_shadow(expected_open_order_ids=set(),expected_positions={})
    assert rec.action.value=="HALT" and rt3.status==RuntimeStatus.HALTED and adapter.submitted==[] and adapter.canceled==[]
    # degradation PAUSE/HALT
    store4=PersistentRuntimeStore(tmp_path/"deg.db"); rt4=PersistentPaperShadowRuntime(runtime_id="deg",mode=RuntimeMode.PAPER,store=store4,paper_engine=_paper_engine()); rt4.start()
    _pipeline(bound_source(tmp_path/"deg-src",boundary),rt4).run_once(instrument_id=INSTRUMENT,venue=VENUE,decision_time=DECISION,risk_snapshot=_risk_snapshot()); assert_lineage(store4,"deg",boundary)
    ass=rt4.assess_degradation(ExpectedBehavior(0.55,0.002,1.5,0.10,5.0,100),ObservedBehavior(0.30,-0.001,0.2,0.15,12.0,30))
    assert ass.state.value=="PAUSED" and rt4.status==RuntimeStatus.HALTED
    # all prohibited consumers remain closed
    for consumer in (DatasetConsumer.ALPHA,DatasetConsumer.OOS,DatasetConsumer.LIVE,DatasetConsumer.BROKER):
        with pytest.raises(InfrastructureDatasetGateError): boundary.authorize(consumer)
def test_infrastructure_restart_pending_and_partial_fill(tmp_path):
    boundary=InfrastructureDatasetBoundary.load(MANIFEST)
    clock=MutableClock(__import__("datetime").datetime(2026,9,22,22,0,tzinfo=UTC)); store=PersistentRuntimeStore(tmp_path/"pending.db")
    first=PersistentPaperShadowRuntime(runtime_id="pending",mode=RuntimeMode.PAPER,store=store,paper_engine=_paper_engine(),clock=clock,config=RuntimeConfig(lease_seconds=5)); first.start()
    result=_pipeline(bound_source(tmp_path/"pending-src",boundary),first).run_once(instrument_id=INSTRUMENT,venue=VENUE,decision_time=DECISION,risk_snapshot=_risk_snapshot()); assert first.paper_engine.simulator.pending_count==1; assert_lineage(store,"pending",boundary)
    clock.advance(seconds=6); resumed=PersistentPaperShadowRuntime(runtime_id="pending",mode=RuntimeMode.PAPER,store=store,paper_engine=_paper_engine(),clock=clock,config=RuntimeConfig(lease_seconds=5)); resumed.start()
    assert resumed.paper_engine.simulator.pending_count==1 and resumed.health().metrics["restarts"]==1
    # partial-fill restart
    clock2=MutableClock(__import__("datetime").datetime(2026,9,22,22,30,tzinfo=UTC)); store2=PersistentRuntimeStore(tmp_path/"partial.db")
    a=PersistentPaperShadowRuntime(runtime_id="partial",mode=RuntimeMode.PAPER,store=store2,paper_engine=_paper_engine(),clock=clock2,config=RuntimeConfig(lease_seconds=5)); a.start()
    _pipeline(bound_source(tmp_path/"partial-src",boundary),a,quantity="10").run_once(instrument_id=INSTRUMENT,venue=VENUE,decision_time=DECISION,risk_snapshot=_risk_snapshot()); a.on_bar(_bar(DECISION+timedelta(seconds=1),volume="100"))
    before=a.paper_engine.snapshot({INSTRUMENT:Decimal("102")}); assert before.positions[INSTRUMENT]==Decimal("5"); assert_lineage(store2,"partial",boundary)
    clock2.advance(seconds=6); b=PersistentPaperShadowRuntime(runtime_id="partial",mode=RuntimeMode.PAPER,store=store2,paper_engine=_paper_engine(),clock=clock2,config=RuntimeConfig(lease_seconds=5)); b.start(); assert b.paper_engine.snapshot({INSTRUMENT:Decimal("102")})==before
    b.on_bar(_bar(DECISION+timedelta(seconds=2),volume="1000")); assert b.paper_engine.snapshot({INSTRUMENT:Decimal("102")}).positions[INSTRUMENT]==Decimal("10"); assert b.paper_engine.ledger.reconcile().ok
    live=InMemoryBrokerAdapter(); rejected=BrokerGateway(live,venue_submission_enabled=True).submit(result.order,lineage={"infrastructure_boundary_fingerprint":boundary.fingerprint})
    assert not rejected.sent and live.submitted==[]

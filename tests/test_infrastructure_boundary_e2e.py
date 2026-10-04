from test_nonlive_execution_path import (
    _source, _pipeline, _paper_engine, _risk_snapshot,
    INSTRUMENT, VENUE, DECISION,
)
from quant_system.data.infrastructure_gate import (
    DatasetConsumer, InfrastructureDatasetBoundary, InfrastructureDatasetGateError,
)
from quant_system.data.crypto_perps.feature_source import R13FeatureDatasetSource
from quant_system.execution.broker import BrokerGateway, InMemoryBrokerAdapter
from quant_system.paper import PaperTradingEngine
from quant_system.runtime import PersistentPaperShadowRuntime, PersistentRuntimeStore, RuntimeMode
from quant_system.shadow import ShadowExecutionEngine

MANIFEST = r"C:\Temp\EQS\infrastructure_only_dataset_manifest_v1_20260925\INFRASTRUCTURE_ONLY_DATASET_MANIFEST_V1.json"


def bound_source(path, boundary):
    base = _source(path)
    return R13FeatureDatasetSource(
        base._assembler, dataset_id=base._dataset_id, partitions=base._partitions,
        infrastructure_boundary=boundary, consumer=DatasetConsumer.FEATURE_ENGINE,
    )
def test_infrastructure_lineage_e2e_and_live_broker_rejection(tmp_path):
    boundary = InfrastructureDatasetBoundary.load(MANIFEST)
    ps = PersistentRuntimeStore(tmp_path / "paper.db")
    paper = PersistentPaperShadowRuntime(
        runtime_id="e2e-paper", mode=RuntimeMode.PAPER, store=ps, paper_engine=_paper_engine()
    )
    paper.start()
    pr = _pipeline(bound_source(tmp_path / "paper-src", boundary), paper).run_once(
        instrument_id=INSTRUMENT, venue=VENUE, decision_time=DECISION, risk_snapshot=_risk_snapshot()
    )
    assert pr.batch.infrastructure_boundary_fingerprint == boundary.fingerprint
    assert pr.strategy_decision.infrastructure_boundary_fingerprint == boundary.fingerprint
    assert pr.risk_decision.infrastructure_boundary_fingerprint == boundary.fingerprint
    pe = next(e for e in ps.load_events("e2e-paper") if e.event_type == "ORDER_EVALUATED")
    assert pe.payload["lineage"]["infrastructure_boundary_fingerprint"] == boundary.fingerprint
    ss = PersistentRuntimeStore(tmp_path / "shadow.db")
    adapter = InMemoryBrokerAdapter()
    shadow = PersistentPaperShadowRuntime(
        runtime_id="e2e-shadow", mode=RuntimeMode.SHADOW, store=ss,
        shadow_engine=ShadowExecutionEngine(BrokerGateway(adapter, venue_submission_enabled=False)),
    )
    shadow.start()
    sr = _pipeline(bound_source(tmp_path / "shadow-src", boundary), shadow).run_once(
        instrument_id=INSTRUMENT, venue=VENUE, decision_time=DECISION, risk_snapshot=_risk_snapshot()
    )
    assert sr.strategy_decision.infrastructure_boundary_fingerprint == boundary.fingerprint
    se = next(e for e in ss.load_events("e2e-shadow") if e.event_type == "ORDER_EVALUATED")
    assert se.payload["lineage"]["infrastructure_boundary_fingerprint"] == boundary.fingerprint
    assert adapter.submitted == []

    base = _source(tmp_path / "live-src")
    import pytest
    with pytest.raises(InfrastructureDatasetGateError):
        R13FeatureDatasetSource(
            base._assembler, dataset_id=base._dataset_id, partitions=base._partitions,
            infrastructure_boundary=boundary, consumer=DatasetConsumer.LIVE,
        )
    live_adapter = InMemoryBrokerAdapter()
    live_gateway = BrokerGateway(live_adapter, venue_submission_enabled=True)
    result = live_gateway.submit(
        pr.order, lineage={"infrastructure_boundary_fingerprint": boundary.fingerprint}
    )
    assert result.sent is False
    assert result.reason == "INFRASTRUCTURE_ONLY_DATASET_BLOCKED"
    assert live_adapter.submitted == []

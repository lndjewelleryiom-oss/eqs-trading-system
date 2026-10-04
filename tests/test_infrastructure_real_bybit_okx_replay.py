from __future__ import annotations
from hashlib import sha256
import json
from pathlib import Path

import pytest

from quant_system.data.crypto_perps.real_market_population import load_public_rest_capture
from quant_system.data.crypto_perps.research_datasets import HistoricalPartitionStore, InstrumentUniverseHistory, PointInTimeDatasetAssembler, DeterministicCryptoPerpReplay
from quant_system.data.crypto_perps.feature_source import R13FeatureDatasetSource
from quant_system.data.infrastructure_gate import DatasetConsumer, InfrastructureDatasetBoundary, InfrastructureDatasetGateError
from quant_system.features import CryptoPerpetualFeatureEngine
from quant_system.execution.broker import BrokerGateway, InMemoryBrokerAdapter
from quant_system.paper import PaperTradingEngine
from quant_system.runtime import PersistentPaperShadowRuntime, PersistentRuntimeStore, RuntimeMode
from quant_system.shadow import ShadowExecutionEngine
from test_nonlive_execution_path import _pipeline, _paper_engine, _risk_snapshot

ROOT=Path(__file__).resolve().parents[1]
SOURCE=ROOT/"artifacts/real-market/r1_3_2026-09-22/public_capture_source_bundle.json"
BOUNDARY_PATH=Path(r"C:\Temp\EQS\infrastructure_only_dataset_manifest_v1_20260925\INFRASTRUCTURE_ONLY_DATASET_MANIFEST_V1.json")
DATASET="infrastructure-only-bybit-okx-v1"

def fp(obj):
    return sha256(json.dumps(obj,sort_keys=True,separators=(",",":"),default=str).encode()).hexdigest()

def build_source(root, boundary, venue):
    pop=load_public_rest_capture(SOURCE)
    defs=tuple(d for d in pop.definitions if d.venue==venue)
    events=tuple(e for e in pop.events if e.meta.venue==venue)
    store=HistoricalPartitionStore(root/"partitions")
    parts=store.write_events(DATASET,events)
    assembler=PointInTimeDatasetAssembler(store,InstrumentUniverseHistory(defs))
    source=R13FeatureDatasetSource(assembler,dataset_id=DATASET,partitions=parts,infrastructure_boundary=boundary,consumer=DatasetConsumer.FEATURE_ENGINE)
    return pop,source,defs[0],events,parts,assembler
def capture(root, mode, venue):
    boundary=InfrastructureDatasetBoundary.load(BOUNDARY_PATH)
    pop,source,definition,events,parts,assembler=build_source(root,boundary,venue)
    decision=max(e.meta.available_at for e in events)
    dataset=assembler.assemble(dataset_id=DATASET,partitions=parts,decision_time=decision,start_time=min(e.meta.event_time for e in events))
    replay=tuple(DeterministicCryptoPerpReplay(dataset).iter_until(decision))
    store=PersistentRuntimeStore(root/"runtime.db")
    adapter=InMemoryBrokerAdapter()
    if mode==RuntimeMode.PAPER:
        runtime=PersistentPaperShadowRuntime(runtime_id=f"{venue}-paper",mode=mode,store=store,paper_engine=_paper_engine())
    else:
        runtime=PersistentPaperShadowRuntime(runtime_id=f"{venue}-shadow",mode=mode,store=store,shadow_engine=ShadowExecutionEngine(BrokerGateway(adapter,venue_submission_enabled=False)))
    runtime.start()
    result=_pipeline(source,runtime,max_data_age=__import__("datetime").timedelta(days=3650)).run_once(instrument_id=definition.instrument_id,venue=venue,decision_time=decision,risk_snapshot=_risk_snapshot())
    lineage=[e.payload for e in store.load_events(runtime.runtime_id) if e.event_type in ("STRATEGY_DECISION","RISK_DECISION","ORDER_EVALUATED")]
    return {
      "dataset":dataset.manifest.fingerprint(),
      "replay":fp([e.meta.canonical_identity() for e in replay]),
      "feature":result.feature_run.manifest.fingerprint,
      "decision":result.strategy_decision.fingerprint,
      "risk":result.risk_decision.fingerprint,
      "paper_shadow":fp(lineage),
      "runtime":fp([(e.sequence,e.event_type,e.payload) for e in store.load_events(runtime.runtime_id) if e.event_type in ("STRATEGY_DECISION","RISK_DECISION","ORDER_EVALUATED")]),
      "boundary":result.batch.infrastructure_boundary_fingerprint,
      "order":result.order,
      "adapter_submitted":len(adapter.submitted),
    }
@pytest.mark.parametrize("venue",["BYBIT_LINEAR","OKX_SWAP"])
@pytest.mark.parametrize("mode",[RuntimeMode.PAPER,RuntimeMode.SHADOW])
def test_real_bybit_okx_infrastructure_replay_is_deterministic_and_contained(tmp_path,venue,mode):
    one=capture(tmp_path/"one",mode,venue)
    two=capture(tmp_path/"two",mode,venue)
    for key in ("dataset","replay","feature","decision","risk","paper_shadow","runtime","boundary"):
        assert one[key]==two[key], key
    assert one["boundary"]==InfrastructureDatasetBoundary.load(BOUNDARY_PATH).fingerprint
    if mode==RuntimeMode.SHADOW:
        assert one["adapter_submitted"]==two["adapter_submitted"]==0
    boundary=InfrastructureDatasetBoundary.load(BOUNDARY_PATH)
    for consumer in (DatasetConsumer.ALPHA,DatasetConsumer.OOS,DatasetConsumer.LIVE,DatasetConsumer.BROKER):
        with pytest.raises(InfrastructureDatasetGateError):
            boundary.authorize(consumer)
    if one["order"] is not None:
        adapter=InMemoryBrokerAdapter(); gateway=BrokerGateway(adapter,venue_submission_enabled=True)
        rejected=gateway.submit(one["order"],lineage={"infrastructure_boundary_fingerprint":boundary.fingerprint})
        assert not rejected.sent and rejected.reason=="INFRASTRUCTURE_ONLY_DATASET_BLOCKED"
        assert adapter.submitted==[]

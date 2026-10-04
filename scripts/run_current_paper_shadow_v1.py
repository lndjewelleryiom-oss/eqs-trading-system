from pathlib import Path
from datetime import datetime,timezone,timedelta
import json,time
from hashlib import sha256
from quant_system.data.crypto_perps.current_market import fetch_current_snapshot,fetch_current_instrument_definition
from quant_system.data.crypto_perps.research_datasets import HistoricalPartitionStore,InstrumentUniverseHistory,PointInTimeDatasetAssembler
from quant_system.data.crypto_perps.feature_source import R13FeatureDatasetSource
from quant_system.data.crypto_perps.models import PerpetualStateEvent,TradeEvent
from quant_system.data.infrastructure_gate import InfrastructureDatasetBoundary,DatasetConsumer
from quant_system.runtime import PersistentRuntimeStore,PersistentPaperShadowRuntime,RuntimeMode
from quant_system.runtime.idempotency import DurableCycleEventLedger
from quant_system.shadow import ShadowExecutionEngine
from quant_system.execution.broker import BrokerGateway,InMemoryBrokerAdapter
from quant_system.commissioning.runtime_factory import pipeline,paper_engine,risk_snapshot
ROOT=Path(r"C:\Users\lndje\Documents\EQS_Market_Terminal_V5_20260925\app"); B=InfrastructureDatasetBoundary.load(r"C:\Temp\EQS\infrastructure_only_dataset_manifest_v1_20260925\INFRASTRUCTURE_ONLY_DATASET_MANIFEST_V1.json")
OUT=ROOT/"artifacts"/"commissioning"/"current_runtime_v1"; OUT.mkdir(parents=True,exist_ok=True); DB=OUT/"runtime.db"; DATA="current-commissioning-v1"
store=PersistentRuntimeStore(DB)
runtimes={}
adapters={}
for venue in ("BYBIT_LINEAR","OKX_SWAP"):
 for mode in (RuntimeMode.PAPER,RuntimeMode.SHADOW):
  rid=f"current-{venue.lower()}-{mode.value.lower()}"; adapters[rid]=InMemoryBrokerAdapter()
  rt=PersistentPaperShadowRuntime(runtime_id=rid,mode=mode,store=store,paper_engine=paper_engine() if mode==RuntimeMode.PAPER else None,shadow_engine=ShadowExecutionEngine(BrokerGateway(adapters[rid],venue_submission_enabled=False)) if mode==RuntimeMode.SHADOW else None)
  rt.start(); runtimes[(venue,mode)]=rt
dedupe=DurableCycleEventLedger(OUT/"cycle_event_ledger.db")
while True:
 cycle={"at":datetime.now(timezone.utc).isoformat(),"boundary":B.fingerprint,"venues":{},"closures":{"alpha":"REJECTED","oos":"REJECTED","live":"LIVE_0/BLOCKED","broker":"DISABLED"}}
 for venue in ("BYBIT_LINEAR","OKX_SWAP"):
  try:
   instrument_definition=fetch_current_instrument_definition(venue); snap=fetch_current_snapshot(venue); event_ids=tuple(e.meta.canonical_identity() for e in snap.events); admission=dedupe.admit(venue,event_ids); cycle_id=admission.cycle_id; duplicates=admission.duplicate_event_ids; fresh_ids=set(admission.fresh_event_ids); fresh=tuple(e for e in snap.events if e.meta.canonical_identity() in fresh_ids);
   if not fresh:
    cycle["venues"][venue]={"cycle_id":cycle_id,"event_count_received":len(snap.events),"event_count_fresh":0,"duplicate_event_count":len(duplicates),"dedupe_action":"DUPLICATE_CYCLE_SKIPPED","received_at":snap.received_at.isoformat(),"raw_sha256s":snap.raw_sha256s,"runtime":{m.value:{"dedupe_action":"SKIPPED_BEFORE_PIPELINE","broker_submitted":len(adapters[runtimes[(venue,m)].runtime_id].submitted)} for m in (RuntimeMode.PAPER,RuntimeMode.SHADOW)}}; continue
   ps=HistoricalPartitionStore(OUT/"partitions"/venue); parts=ps.write_events(DATA,fresh); ass=PointInTimeDatasetAssembler(ps,InstrumentUniverseHistory((instrument_definition,),active_statuses=("TRADING","ACTIVE","OPEN","LIVE")))
   source=R13FeatureDatasetSource(ass,dataset_id=DATA,partitions=parts,infrastructure_boundary=B,consumer=DatasetConsumer.FEATURE_ENGINE)
   dec=max(max(e.meta.available_at for e in snap.events),instrument_definition.available_at)+timedelta(microseconds=1); vr={}
   for mode in (RuntimeMode.PAPER,RuntimeMode.SHADOW):
    rt=runtimes[(venue,mode)]
    try:
     res=pipeline(source,rt,max_data_age=timedelta(seconds=30)).run_once(instrument_id=snap.instrument_id,venue=venue,decision_time=dec,risk_snapshot=risk_snapshot(market_data_received_at=snap.received_at))
     valuation=None
     if mode==RuntimeMode.PAPER:
      mark=next((e.mark_price for e in reversed(snap.events) if isinstance(e,PerpetualStateEvent) and e.mark_price is not None),None)
      if mark is None: mark=next(e.price for e in reversed(snap.events) if isinstance(e,TradeEvent))
      valuation=rt.record_paper_valuation({snap.instrument_id:mark},observed_at=snap.received_at,source={"venue":venue,"classification":"GENUINE_PUBLIC_MARKET","raw_sha256s":list(snap.raw_sha256s),"boundary":B.fingerprint})
     vr[mode.value]={"decision":res.strategy_decision.fingerprint,"risk":res.risk_decision.fingerprint,"feature":res.feature_run.manifest.fingerprint,"order":None if res.order is None else str(res.order.order_id),"boundary":res.batch.infrastructure_boundary_fingerprint,"broker_submitted":len(adapters[rt.runtime_id].submitted),"valuation":valuation}
    except Exception as e: vr[mode.value]={"error":type(e).__name__+":"+str(e),"broker_submitted":len(adapters[rt.runtime_id].submitted)}
   cycle["venues"][venue]={"cycle_id":cycle_id,"event_count_received":len(snap.events),"event_count_fresh":len(fresh),"duplicate_event_count":len(duplicates),"received_at":snap.received_at.isoformat(),"raw_sha256s":snap.raw_sha256s,"instrument_definition":{"tick_size":str(instrument_definition.tick_size),"lot_size":str(instrument_definition.lot_size),"contract_value":None if instrument_definition.contract_value is None else str(instrument_definition.contract_value),"status":instrument_definition.status,"effective_from":instrument_definition.effective_from.isoformat(),"received_at":instrument_definition.received_at.isoformat(),"raw_sha256":instrument_definition.raw_sha256},"events":len(snap.events),"runtime":vr}
  except Exception as e: cycle["venues"][venue]={"error":type(e).__name__+":"+str(e)}
 with (OUT/"current_runtime_cycles.jsonl").open("a") as f: f.write(json.dumps(cycle,separators=(",",":"))+"\n")
 (OUT/"status.json").write_text(json.dumps(cycle,indent=2))
 # Keep every PAPER/SHADOW lease fresh independently of whether the strategy emits an order.
 # The default lease is 30s, so a 60s market cycle must renew more frequently than the cycle cadence.
 for _ in range(4):
  for rt in runtimes.values():
   rt.heartbeat()
  time.sleep(15)

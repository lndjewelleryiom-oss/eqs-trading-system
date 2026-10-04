from pathlib import Path
from datetime import timedelta
from decimal import Decimal
import json,sys
from quant_system.data.crypto_perps.current_market import fetch_current_snapshot,fetch_current_instrument_definition
from quant_system.data.crypto_perps.research_datasets import HistoricalPartitionStore,InstrumentUniverseHistory,PointInTimeDatasetAssembler
from quant_system.data.crypto_perps.feature_source import R13FeatureDatasetSource
from quant_system.data.infrastructure_gate import InfrastructureDatasetBoundary,DatasetConsumer
from quant_system.runtime import PersistentRuntimeStore,PersistentPaperShadowRuntime,RuntimeMode
from quant_system.commissioning.runtime_factory import pipeline,paper_engine,risk_snapshot
ROOT=Path(r"C:\Users\lndje\Documents\EQS_Market_Terminal_V5_20260925\app"); OUT=ROOT/"artifacts"/"commissioning"/"cross_process_idempotency_v1"; OUT.mkdir(parents=True,exist_ok=True)
B=InfrastructureDatasetBoundary.load(r"C:\Temp\EQS\infrastructure_only_dataset_manifest_v1_20260925\INFRASTRUCTURE_ONLY_DATASET_MANIFEST_V1.json")
phase=sys.argv[1]; venue=sys.argv[2]; cap=OUT/(venue+"_capture.json")
if phase=="capture":
 d=fetch_current_instrument_definition(venue); s=fetch_current_snapshot(venue)
 import pickle
 (OUT/(venue+".pkl")).write_bytes(pickle.dumps((s,d)))
elif phase in ("first","firstkill","replay","firstfillkill","replayfilled"):
 import pickle
 s,d=pickle.loads((OUT/(venue+".pkl")).read_bytes()); store=PersistentRuntimeStore(OUT/(venue+".db")); rt=PersistentPaperShadowRuntime(runtime_id="restart-"+venue.lower()+("-filled-paper" if "fill" in phase else "-paper"),mode=RuntimeMode.PAPER,store=store,paper_engine=paper_engine()); rt.start()
 ps=HistoricalPartitionStore(OUT/"parts"/venue); parts=ps.write_events("restart-idem-v1",s.events); src=R13FeatureDatasetSource(PointInTimeDatasetAssembler(ps,InstrumentUniverseHistory((d,),active_statuses=("TRADING","ACTIVE","OPEN","LIVE"))),dataset_id="restart-idem-v1",partitions=parts,infrastructure_boundary=B,consumer=DatasetConsumer.FEATURE_ENGINE)
 dec=max(max(e.meta.available_at for e in s.events),d.available_at)+timedelta(microseconds=1)
 before=rt.paper_engine.export_state(); result=None; error=None
 try: result=pipeline(src,rt).run_once(instrument_id=s.instrument_id,venue=venue,decision_time=dec,risk_snapshot=risk_snapshot(market_data_received_at=s.received_at))
 except Exception as e: error=type(e).__name__+":"+str(e)
 
 if phase=="firstfillkill" and result is not None and result.order is not None:
  from quant_system.backtest.events import BarEvent
  px=result.order.reference_price; rt.on_bar(BarEvent(symbol=result.order.symbol,timestamp=dec+timedelta(seconds=1),open=px,high=px,low=px,close=px,volume=Decimal("100")))
 after=rt.paper_engine.export_state()
 def counts(x): return {"orders":len(x.simulator.seen_order_ids),"fills":len(x.fills)}
 out={"phase":phase,"generation":rt.generation,"before":counts(before),"after":counts(after),"order":None if result is None or result.order is None else str(result.order.order_id),"error":error}
 (OUT/(venue+"_"+phase+".json")).write_text(json.dumps(out,indent=2)); print(json.dumps(out),flush=True);
 if phase in ("firstkill","firstfillkill"):
  import time; time.sleep(300)
 rt.stop()

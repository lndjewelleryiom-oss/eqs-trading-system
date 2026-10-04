from pathlib import Path
from datetime import datetime,timezone,timedelta
from decimal import Decimal
import json,subprocess,time,hashlib,urllib.request,sys
ROOT=Path(r"C:\Users\lndje\Documents\EQS_Market_Terminal_V5_20260925\app"); sys.path.insert(0,str(ROOT/"tests"))
from quant_system.data.crypto_perps.current_market import fetch_current_snapshot
from quant_system.data.crypto_perps.research_datasets import HistoricalPartitionStore,InstrumentUniverseHistory,PointInTimeDatasetAssembler
from quant_system.data.crypto_perps.feature_source import R13FeatureDatasetSource
from quant_system.data.crypto_perps.models import PerpetualInstrumentDefinition,TradeEvent
from quant_system.data.infrastructure_gate import InfrastructureDatasetBoundary,DatasetConsumer
from quant_system.runtime import PersistentRuntimeStore,PersistentPaperShadowRuntime,RuntimeMode
from quant_system.shadow import ShadowExecutionEngine
from quant_system.execution.broker import BrokerGateway,InMemoryBrokerAdapter
from quant_system.backtest.events import BarEvent
from test_nonlive_execution_path import _pipeline,_paper_engine,_risk_snapshot
B=InfrastructureDatasetBoundary.load(r"C:\Temp\EQS\infrastructure_only_dataset_manifest_v1_20260925\INFRASTRUCTURE_ONLY_DATASET_MANIFEST_V1.json")
OUT=ROOT/"artifacts"/"commissioning"/"paper_accounting_acceptance_v1"; OUT.mkdir(parents=True,exist_ok=True); DB=OUT/"runtime.db"; DATA="current-paper-accounting-v1"
def definition(s):
 z=datetime(2020,1,1,tzinfo=timezone.utc); return PerpetualInstrumentDefinition(s.instrument_id,s.venue,s.symbol,"BTC","USDT","USDT","LINEAR",Decimal("0.1"),Decimal("0.001"),None,"TRADING",z,z,z,z,s.raw_sha256s[0])
def bar(s,after):
 trades=[e for e in s.events if isinstance(e,TradeEvent)]; prices=[e.price for e in trades]; qty=sum((e.quantity for e in trades),Decimal("0")); last=trades[-1].price
 return BarEvent(s.instrument_id,after,max(prices[0],last) if False else prices[0],max(prices),min(prices),last,max(qty,Decimal("1")))
store=PersistentRuntimeStore(DB); evidence={"schema":"eqs-current-paper-accounting-acceptance-v1","boundary":B.fingerprint,"venues":{}}
for venue in ("BYBIT_LINEAR","OKX_SWAP"):
 s=fetch_current_snapshot(venue); ps=HistoricalPartitionStore(OUT/"partitions"/venue); parts=ps.write_events(DATA,s.events); ass=PointInTimeDatasetAssembler(ps,InstrumentUniverseHistory((definition(s),))); source=R13FeatureDatasetSource(ass,dataset_id=DATA,partitions=parts,infrastructure_boundary=B,consumer=DatasetConsumer.FEATURE_ENGINE)
 rid=f"accounting-{venue.lower()}-paper"; rt=PersistentPaperShadowRuntime(runtime_id=rid,mode=RuntimeMode.PAPER,store=store,paper_engine=_paper_engine()); rt.start(); dec=max(e.meta.available_at for e in s.events)+timedelta(microseconds=1); res=_pipeline(source,rt).run_once(instrument_id=s.instrument_id,venue=venue,decision_time=dec,risk_snapshot=_risk_snapshot())
 pre=rt.paper_engine.snapshot({s.instrument_id:next(e.mark_price for e in s.events if getattr(e,"mark_price",None) is not None)})
 fills=rt.on_bar(bar(s,dec+timedelta(seconds=1))) if res.order else ()
 mark=next((e.mark_price for e in reversed(s.events) if getattr(e,"mark_price",None) is not None),None) or next(e.price for e in reversed(s.events) if isinstance(e,TradeEvent))
 acct=rt.paper_engine.snapshot({s.instrument_id:mark}); rec=rt.paper_engine.ledger.reconcile(); rt.record_paper_valuation({s.instrument_id:mark}, observed_at=s.received_at, source={"venue":venue,"raw_sha256s":list(s.raw_sha256s),"classification":"GENUINE_PUBLIC_MARKET"}); rt.stop()
 before={"cash":str(acct.cash),"equity":str(acct.equity),"realized":str(acct.realized_pnl),"unrealized":str(acct.unrealized_pnl),"commissions":str(acct.commissions),"positions":{k:str(v) for k,v in acct.positions.items()},"fills":len(rt.paper_engine.fills)}
 rt2=PersistentPaperShadowRuntime(runtime_id=rid,mode=RuntimeMode.PAPER,store=store,paper_engine=_paper_engine()); rt2.start(); afteracct=rt2.paper_engine.snapshot({s.instrument_id:mark}); after={"cash":str(afteracct.cash),"equity":str(afteracct.equity),"realized":str(afteracct.realized_pnl),"unrealized":str(afteracct.unrealized_pnl),"commissions":str(afteracct.commissions),"positions":{k:str(v) for k,v in afteracct.positions.items()},"fills":len(rt2.paper_engine.fills)}
 evidence["venues"][venue]={"received_at":s.received_at.isoformat(),"raw_sha256s":s.raw_sha256s,"signal":res.strategy_decision.signal.value,"risk":res.risk_decision.action.value,"order":None if res.order is None else str(res.order.order_id),"fill_count":len(fills),"mark":str(mark),"reconcile_ok":rec.ok,"before_restart":before,"after_restart":after,"restart_equal":before==after,"lineage":{"dataset":res.batch.dataset_fingerprints,"feature":res.feature_run.manifest.fingerprint,"decision":res.strategy_decision.fingerprint,"risk":res.risk_decision.fingerprint,"boundary":res.batch.infrastructure_boundary_fingerprint}}
 rt2.heartbeat()
# verify production shadow/current runtime remains zero-submit via terminal
d=json.load(urllib.request.urlopen("http://127.0.0.1:8765/api/dashboard",timeout=5)); c=d["execution"]["provenance"]["containment"]
evidence["safety"]={"shadow_zero_submit":c["shadow_zero_submit"],"live":c["live"],"broker":c["broker_submission"],"terminal_connected":d["meta"]["runtime_connected"],"runtime_count":len(d["execution"]["runtimes"])}
(OUT/"ACCEPTANCE_EVIDENCE.json").write_text(json.dumps(evidence,indent=2)); print(json.dumps(evidence,indent=2))

from pathlib import Path
from datetime import datetime,timezone,timedelta
from decimal import Decimal
from uuid import UUID,uuid5
import json
from quant_system.backtest.interfaces import ExecutionAssumptions
from quant_system.backtest.simulator import ConservativeBarExecutionSimulator
from quant_system.backtest.events import BarEvent
from quant_system.paper import PaperTradingEngine
from quant_system.execution.models import OrderRequest,OrderType,Side
from quant_system.runtime import PersistentRuntimeStore,PersistentPaperShadowRuntime,RuntimeMode
root=Path(r"C:\Users\lndje\Documents\EQS_Market_Terminal_V5_20260925\app");out=root/"artifacts"/"commissioning"/"ordinary_persistence_failure_v1";out.mkdir(parents=True,exist_ok=True);db=out/"runtime.db";rid="ordinary-persistence-failure";t=datetime(2026,9,26,11,0,tzinfo=timezone.utc)
a=ExecutionAssumptions(commission_bps=Decimal("1"),spread_bps=Decimal("2"),slippage_bps=Decimal("1"),impact_bps=Decimal("1"),financing_bps_annual=Decimal("0"),borrow_bps_annual=Decimal("0"),latency_ms=0,partial_fill_fraction=Decimal("1"))
def eng(): return PaperTradingEngine(ConservativeBarExecutionSimulator(a,max_volume_participation=Decimal("1")),initial_cash=Decimal("10000"))
store=PersistentRuntimeStore(db);rt=PersistentPaperShadowRuntime(runtime_id=rid,mode=RuntimeMode.PAPER,store=store,paper_engine=eng());rt.start();oid=uuid5(UUID("12345678-1234-5678-1234-567812345678"),rid);o=OrderRequest(strategy_id=UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),symbol="BTCUSDT",side=Side.BUY,quantity=Decimal("1"),order_type=OrderType.MARKET,decision_time=t,reference_price=Decimal("100"),order_id=oid);rt.submit_order(o);bar=BarEvent("BTCUSDT",t+timedelta(seconds=1),Decimal("100"),Decimal("101"),Decimal("99"),Decimal("100"),Decimal("100"))
def st(r):
 x=r.paper_engine.export_state();v=r.paper_engine.snapshot({"BTCUSDT":Decimal("100")});return {"seen":len(x.simulator.seen_order_ids),"pending":len(x.simulator.pending_orders),"fills":len(x.fills),"cash":str(v.cash),"position":str(v.positions.get("BTCUSDT",0)),"status":r.status.value}
before=st(rt);orig=store.append_event_and_checkpoint;calls={"n":0}
def fail_once(*args,**kwargs):
 calls["n"]+=1
 if calls["n"]==1: raise OSError("INJECTED_PERSISTENCE_FAILURE")
 return orig(*args,**kwargs)
store.append_event_and_checkpoint=fail_once
err=None
try: rt.on_bar(bar)
except Exception as e: err=f"{type(e).__name__}: {e}"
after=st(rt);cp=store.latest_checkpoint(rid);rt.stop()
rt2=PersistentPaperShadowRuntime(runtime_id=rid,mode=RuntimeMode.PAPER,store=PersistentRuntimeStore(db),paper_engine=eng());rt2.start();recovered=st(rt2);events=[e for e in rt2.store.load_events(rid) if e.event_type=="PAPER_BAR_PROCESSED"];passed=before["fills"]==0 and after["fills"]==0 and after["pending"]==1 and after["position"]=="0" and recovered["fills"]==0 and recovered["pending"]==1 and recovered["position"]=="0" and len(events)==0 and err is not None;res={"before":before,"error":err,"after_failure":after,"recovered":recovered,"paper_bar_receipts":len(events),"pass":passed};(out/"EVIDENCE.json").write_text(json.dumps(res,indent=2));print(json.dumps(res),flush=True);rt2.stop()

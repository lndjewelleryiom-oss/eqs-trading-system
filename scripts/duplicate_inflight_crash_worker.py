from pathlib import Path
from datetime import datetime,timezone,timedelta
from decimal import Decimal
from uuid import UUID,uuid5
import json,sys,os,time
from quant_system.backtest.interfaces import ExecutionAssumptions
from quant_system.backtest.simulator import ConservativeBarExecutionSimulator
from quant_system.backtest.events import BarEvent
from quant_system.paper import PaperTradingEngine
from quant_system.execution.models import OrderRequest,OrderType,Side
from quant_system.runtime import PersistentRuntimeStore,PersistentPaperShadowRuntime,RuntimeMode
root=Path(r"C:\Users\lndje\Documents\EQS_Market_Terminal_V5_20260925\app");out=root/"artifacts"/"commissioning"/"duplicate_inflight_crash_v1";out.mkdir(parents=True,exist_ok=True);db=out/"runtime.db";rid="duplicate-inflight-crash";t=datetime(2026,9,26,6,0,tzinfo=timezone.utc);oid=uuid5(UUID("12345678-1234-5678-1234-567812345678"),rid);phase=sys.argv[1];bar=BarEvent("BTCUSDT",t+timedelta(seconds=1),Decimal("100"),Decimal("101"),Decimal("99"),Decimal("100"),Decimal("100"))
def eng():
 a=ExecutionAssumptions(commission_bps=Decimal("1"),spread_bps=Decimal("2"),slippage_bps=Decimal("1"),impact_bps=Decimal("1"),financing_bps_annual=Decimal("0"),borrow_bps_annual=Decimal("0"),latency_ms=0,partial_fill_fraction=Decimal("1"));return PaperTradingEngine(ConservativeBarExecutionSimulator(a,max_volume_participation=Decimal("1")),initial_cash=Decimal("10000"))
rt=PersistentPaperShadowRuntime(runtime_id=rid,mode=RuntimeMode.PAPER,store=PersistentRuntimeStore(db),paper_engine=eng());rt.start()
def st():
 x=rt.paper_engine.export_state();a=rt.paper_engine.snapshot({"BTCUSDT":Decimal("100")});ev=[e for e in rt.store.load_events(rid) if e.event_type=="PAPER_BAR_PROCESSED"];return {"generation":rt.generation,"seen":len(x.simulator.seen_order_ids),"pending":len(x.simulator.pending_orders),"fills":len(x.fills),"fill_qty":[str(f.fill.quantity) for f in x.fills],"cash":str(a.cash),"equity":str(a.equity),"position":str(a.positions.get("BTCUSDT",0)),"receipts":len(ev)}
if phase=="crash_first":
 o=OrderRequest(strategy_id=UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),symbol="BTCUSDT",side=Side.BUY,quantity=Decimal("1"),order_type=OrderType.MARKET,decision_time=t,reference_price=Decimal("100"),order_id=oid);rt.submit_order(o,lineage={"acceptance":"duplicate-inflight-crash"});os.environ["EQS_ATOMIC_CRASH_POINT"]="AFTER_CHECKPOINT_INSERT";rt.on_bar(bar)
elif phase=="recover_and_duplicate":
 before=st();first=rt.on_bar(bar);after_first=st();err=None
 try: rt.on_bar(bar)
 except Exception as e: err=f"{type(e).__name__}: {e}"
 after_dup=st();passed=before["fills"]==0 and before["pending"]==1 and before["receipts"]==0 and len(first)==1 and after_first["fills"]==1 and after_first["receipts"]==1 and after_dup==after_first and err is not None
 result={"after_crashed_first_attempt":before,"first_committed_delivery":after_first,"duplicate_delivery_error":err,"after_duplicate":after_dup,"pass":passed};(out/"EVIDENCE.json").write_text(json.dumps(result,indent=2));print(json.dumps(result),flush=True);time.sleep(300)
elif phase=="final":
 state=st();passed=state["seen"]==1 and state["pending"]==0 and state["fills"]==1 and state["fill_qty"]==["1"] and state["position"]=="1" and state["cash"]=="9899.9689979" and state["receipts"]==1;print(json.dumps({"final":state,"pass":passed}),flush=True);rt.stop()

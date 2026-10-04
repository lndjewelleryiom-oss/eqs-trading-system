from pathlib import Path
from datetime import datetime,timezone,timedelta
from decimal import Decimal
from uuid import UUID,uuid5
import json,sys,os
from quant_system.backtest.interfaces import ExecutionAssumptions
from quant_system.backtest.simulator import ConservativeBarExecutionSimulator
from quant_system.backtest.events import BarEvent
from quant_system.paper import PaperTradingEngine
from quant_system.execution.models import OrderRequest,OrderType,Side
from quant_system.runtime import PersistentRuntimeStore,PersistentPaperShadowRuntime,RuntimeMode
root=Path(r"C:\Users\lndje\Documents\EQS_Market_Terminal_V5_20260925\app");point=sys.argv[1];iteration=int(sys.argv[2]);phase=sys.argv[3];out=root/"artifacts"/"commissioning"/"atomic_crash_matrix_v1";out.mkdir(parents=True,exist_ok=True);db=out/f"{point}_{iteration}.db";rid=f"atomic-matrix-{point}-{iteration}";t=datetime(2026,9,26,3,0,tzinfo=timezone.utc);oid=uuid5(UUID("12345678-1234-5678-1234-567812345678"),rid)
def eng():
 a=ExecutionAssumptions(commission_bps=Decimal("1"),spread_bps=Decimal("2"),slippage_bps=Decimal("1"),impact_bps=Decimal("1"),financing_bps_annual=Decimal("0"),borrow_bps_annual=Decimal("0"),latency_ms=0,partial_fill_fraction=Decimal("1"));return PaperTradingEngine(ConservativeBarExecutionSimulator(a,max_volume_participation=Decimal("1")),initial_cash=Decimal("10000"))
codes={"AFTER_BEGIN":91,"BEFORE_EVENT_INSERT":92,"AFTER_EVENT_INSERT":93,"AFTER_CHECKPOINT_INSERT":94,"AFTER_COMMIT":95}
def hard_crash(name):
 if phase=="crash" and name==point: os._exit(codes[name])
rt=PersistentPaperShadowRuntime(runtime_id=rid,mode=RuntimeMode.PAPER,store=PersistentRuntimeStore(db,test_fault_injector=hard_crash if phase=="crash" else None),paper_engine=eng());rt.start()
def st():
 x=rt.paper_engine.export_state();a=rt.paper_engine.snapshot({"BTCUSDT":Decimal("100")});return {"seen":len(x.simulator.seen_order_ids),"pending":len(x.simulator.pending_orders),"fills":len(x.fills),"cash":str(a.cash),"position":str(a.positions.get("BTCUSDT",0))}
if phase=="crash":
 o=OrderRequest(strategy_id=UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),symbol="BTCUSDT",side=Side.BUY,quantity=Decimal("1"),order_type=OrderType.MARKET,decision_time=t,reference_price=Decimal("100"),order_id=oid);rt.submit_order(o,lineage={"acceptance":"atomic-crash-matrix-test-injector"});rt.on_bar(BarEvent("BTCUSDT",t+timedelta(seconds=1),Decimal("100"),Decimal("101"),Decimal("99"),Decimal("100"),Decimal("100")))
else:
 state=st(); bars=[e for e in rt.store.load_events(rid) if e.event_type=="PAPER_BAR_PROCESSED"];pre=state["fills"]==0 and state["pending"]==1 and state["position"]=="0" and state["cash"]=="10000" and len(bars)==0;post=state["fills"]==1 and state["pending"]==0 and state["position"]=="1" and len(bars)==1;result={"point":point,"iteration":iteration,"state":state,"receipts":len(bars),"classification":"PRE" if pre else "POST" if post else "PARTIAL","pass":pre or post};(out/f"{point}_{iteration}.json").write_text(json.dumps(result,indent=2));print(json.dumps(result),flush=True);rt.stop()

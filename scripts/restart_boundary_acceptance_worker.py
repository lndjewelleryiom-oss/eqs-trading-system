from pathlib import Path
from datetime import datetime,timezone,timedelta
from decimal import Decimal
from uuid import UUID,uuid5
import json,sys,time
from quant_system.backtest.interfaces import ExecutionAssumptions
from quant_system.backtest.simulator import ConservativeBarExecutionSimulator
from quant_system.backtest.events import BarEvent
from quant_system.paper import PaperTradingEngine
from quant_system.execution.models import OrderRequest,OrderType,Side
from quant_system.runtime import PersistentRuntimeStore,PersistentPaperShadowRuntime,RuntimeMode
ROOT=Path(r"C:\Users\lndje\Documents\EQS_Market_Terminal_V5_20260925\app"); OUT=ROOT/"artifacts"/"commissioning"/"restart_boundary_acceptance_v1";OUT.mkdir(parents=True,exist_ok=True)
case,phase=sys.argv[1:3]; db=OUT/(case+".db"); rid="restart-"+case+"-paper"; t=datetime(2026,9,25,23,30,tzinfo=timezone.utc); oid=uuid5(UUID("12345678-1234-5678-1234-567812345678"),case)
a=ExecutionAssumptions(commission_bps=Decimal("1"),spread_bps=Decimal("2"),slippage_bps=Decimal("1"),impact_bps=Decimal("1"),financing_bps_annual=Decimal("0"),borrow_bps_annual=Decimal("0"),latency_ms=0,partial_fill_fraction=Decimal("1"))
eng=PaperTradingEngine(ConservativeBarExecutionSimulator(a,max_volume_participation=Decimal("1")),initial_cash=Decimal("10000"));rt=PersistentPaperShadowRuntime(runtime_id=rid,mode=RuntimeMode.PAPER,store=PersistentRuntimeStore(db),paper_engine=eng);rt.start()
def snap():
 x=rt.paper_engine.export_state();return {"generation":rt.generation,"seen_orders":len(x.simulator.seen_order_ids),"pending":len(x.simulator.pending_orders),"pending_qty":[str(o.quantity) for o in x.simulator.pending_orders],"fills":len(x.fills),"fill_qty":[str(f.fill.quantity) for f in x.fills]}
if phase=="prime":
 o=OrderRequest(strategy_id=UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),symbol="BTCUSDT",side=Side.BUY,quantity=Decimal("1"),order_type=OrderType.MARKET,decision_time=t,reference_price=Decimal("100"),order_id=oid);rt.submit_order(o,lineage={"acceptance":case})
 if case=="partial_exhaust": rt.on_bar(BarEvent("BTCUSDT",t+timedelta(seconds=1),Decimal("100"),Decimal("101"),Decimal("99"),Decimal("100"),Decimal("0.5")))
 print(json.dumps({"case":case,"phase":phase,**snap()}),flush=True);time.sleep(300)
else:
 before=snap(); x=rt.paper_engine.export_state(); last=x.simulator.last_bar_times.get("BTCUSDT"); ts=(last+timedelta(seconds=1)) if last else t+timedelta(seconds=1)
 steps=[]
 while rt.paper_engine.simulator.pending_count:
  rt.on_bar(BarEvent("BTCUSDT",ts,Decimal("100"),Decimal("101"),Decimal("99"),Decimal("100"),Decimal("1000000")));steps.append(snap());ts+=timedelta(seconds=1)
 after=snap(); total=sum(Decimal(q) for q in after["fill_qty"]); expected_before={"partial_exhaust":(1,["0.5"]), "pre_first_fill":(0,["1"])}[case]
 passed=before["fills"]==expected_before[0] and before["pending_qty"]==expected_before[1] and after["seen_orders"]==1 and after["pending"]==0 and total==Decimal("1")
 out={"case":case,"phase":phase,"before":before,"steps":steps,"after":after,"total_filled":str(total),"pass":passed};(OUT/(case+"_EVIDENCE.json")).write_text(json.dumps(out,indent=2));print(json.dumps(out),flush=True);rt.stop()

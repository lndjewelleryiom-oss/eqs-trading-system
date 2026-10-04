from pathlib import Path
from datetime import datetime,timezone,timedelta
from decimal import Decimal
import json,sys,time
from uuid import uuid5,UUID
from quant_system.backtest.interfaces import ExecutionAssumptions
from quant_system.backtest.simulator import ConservativeBarExecutionSimulator
from quant_system.backtest.events import BarEvent
from quant_system.paper import PaperTradingEngine
from quant_system.execution.models import OrderRequest,OrderType,Side
from quant_system.runtime import PersistentRuntimeStore,PersistentPaperShadowRuntime,RuntimeMode
OUT=Path(r"C:\Users\lndje\Documents\EQS_Market_Terminal_V5_20260925\app\artifacts\commissioning\partial_fill_restart_v1");OUT.mkdir(parents=True,exist_ok=True)
phase=sys.argv[1]; db=OUT/"runtime.db"; rid="partial-fill-restart-paper"; t=datetime(2026,9,25,23,0,tzinfo=timezone.utc); oid=uuid5(UUID("12345678-1234-5678-1234-567812345678"),"partial-fill-restart-v1")
def engine():
 a=ExecutionAssumptions(commission_bps=Decimal("1"),spread_bps=Decimal("2"),slippage_bps=Decimal("1"),impact_bps=Decimal("1"),financing_bps_annual=Decimal("0"),borrow_bps_annual=Decimal("0"),latency_ms=0,partial_fill_fraction=Decimal("0.5"))
 return PaperTradingEngine(ConservativeBarExecutionSimulator(a,max_volume_participation=Decimal("1")),initial_cash=Decimal("10000"))
rt=PersistentPaperShadowRuntime(runtime_id=rid,mode=RuntimeMode.PAPER,store=PersistentRuntimeStore(db),paper_engine=engine());rt.start()
def snap():
 x=rt.paper_engine.export_state(); return {"generation":rt.generation,"seen_orders":len(x.simulator.seen_order_ids),"pending":len(x.simulator.pending_orders),"pending_qty":[str(o.quantity) for o in x.simulator.pending_orders],"fills":len(x.fills),"fill_qty":[str(f.fill.quantity) for f in x.fills]}
if phase=="first":
 o=OrderRequest(strategy_id=UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),symbol="BTCUSDT",side=Side.BUY,quantity=Decimal("1"),order_type=OrderType.MARKET,decision_time=t,reference_price=Decimal("100"),order_id=oid);rt.submit_order(o,lineage={"acceptance":"partial-fill-restart-v1"});rt.on_bar(BarEvent("BTCUSDT",t+timedelta(seconds=1),Decimal("100"),Decimal("101"),Decimal("99"),Decimal("100"),Decimal("1"))); print(json.dumps({"phase":phase,**snap()}),flush=True);time.sleep(300)
else:
 before=snap();rt.on_bar(BarEvent("BTCUSDT",t+timedelta(seconds=2),Decimal("100"),Decimal("101"),Decimal("99"),Decimal("100"),Decimal("1"))); mid=snap();
 while rt.paper_engine.simulator.pending_count:
  n=len(rt.paper_engine.fills)+2; rt.on_bar(BarEvent("BTCUSDT",t+timedelta(seconds=n),Decimal("100"),Decimal("101"),Decimal("99"),Decimal("100"),Decimal("1000000")))
 after=snap();out={"phase":phase,"before":before,"after_second_bar":mid,"after_completion":after,"pass":before["fills"]==1 and before["pending_qty"]==["0.5"] and mid["fills"]==2 and mid["pending_qty"]==["0.25"] and after["seen_orders"]==1 and after["pending"]==0 and sum(Decimal(q) for q in after["fill_qty"])==Decimal("1")};(OUT/"EVIDENCE.json").write_text(json.dumps(out,indent=2));print(json.dumps(out),flush=True);rt.stop()

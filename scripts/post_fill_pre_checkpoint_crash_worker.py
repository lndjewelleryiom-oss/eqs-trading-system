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
ROOT=Path(r"C:\Users\lndje\Documents\EQS_Market_Terminal_V5_20260925\app");OUT=ROOT/"artifacts"/"commissioning"/"post_fill_pre_checkpoint_crash_v1";OUT.mkdir(parents=True,exist_ok=True);db=OUT/"runtime.db";phase=sys.argv[1];t=datetime(2026,9,26,0,0,tzinfo=timezone.utc);oid=uuid5(UUID("12345678-1234-5678-1234-567812345678"),"post-fill-pre-checkpoint")
def eng():
 a=ExecutionAssumptions(commission_bps=Decimal("1"),spread_bps=Decimal("2"),slippage_bps=Decimal("1"),impact_bps=Decimal("1"),financing_bps_annual=Decimal("0"),borrow_bps_annual=Decimal("0"),latency_ms=0,partial_fill_fraction=Decimal("1"))
 return PaperTradingEngine(ConservativeBarExecutionSimulator(a,max_volume_participation=Decimal("1")),initial_cash=Decimal("10000"))
rt=PersistentPaperShadowRuntime(runtime_id="post-fill-pre-checkpoint-paper",mode=RuntimeMode.PAPER,store=PersistentRuntimeStore(db),paper_engine=eng());rt.start()
def state():
 x=rt.paper_engine.export_state(); snap=rt.paper_engine.snapshot({"BTCUSDT":Decimal("100")}); return {"generation":rt.generation,"seen":len(x.simulator.seen_order_ids),"pending":len(x.simulator.pending_orders),"fills":len(x.fills),"fill_qty":[str(f.fill.quantity) for f in x.fills],"cash":str(snap.cash),"equity":str(snap.equity),"position":str(snap.positions.get("BTCUSDT",0))}
if phase=="prime":
 o=OrderRequest(strategy_id=UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),symbol="BTCUSDT",side=Side.BUY,quantity=Decimal("1"),order_type=OrderType.MARKET,decision_time=t,reference_price=Decimal("100"),order_id=oid);rt.submit_order(o,lineage={"acceptance":"post-fill-pre-checkpoint"});print(json.dumps({"phase":"persisted_order",**state()}),flush=True)
 # Fault injection: mutate simulator+ledger directly, deliberately bypassing runtime on_bar checkpoint.
 fills=rt.paper_engine.on_bar(BarEvent("BTCUSDT",t+timedelta(seconds=1),Decimal("100"),Decimal("101"),Decimal("99"),Decimal("100"),Decimal("100")));print(json.dumps({"phase":"mutated_not_checkpointed","new_fills":len(fills),**state()}),flush=True);time.sleep(300)
else:
 before=state(); fills=rt.on_bar(BarEvent("BTCUSDT",t+timedelta(seconds=1),Decimal("100"),Decimal("101"),Decimal("99"),Decimal("100"),Decimal("100")));after=state(); rt.checkpoint(); out={"before_restart_processing":before,"new_fills":len(fills),"after":after,"pass":before["fills"]==0 and before["pending"]==1 and len(fills)==1 and after["fills"]==1 and after["pending"]==0};(OUT/"EVIDENCE.json").write_text(json.dumps(out,indent=2));print(json.dumps(out),flush=True);rt.stop()

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
root=Path(r"C:\Users\lndje\Documents\EQS_Market_Terminal_V5_20260925\app");base=root/"artifacts"/"commissioning"/"persistence_failure_matrix_v1";base.mkdir(parents=True,exist_ok=True);case=sys.argv[1];phase=sys.argv[2];out=base/case;out.mkdir(exist_ok=True);db=out/"runtime.db";rid="pfm-"+case;t=datetime(2026,9,26,12,0,tzinfo=timezone.utc);bar=BarEvent("BTCUSDT",t+timedelta(seconds=1),Decimal("100"),Decimal("101"),Decimal("99"),Decimal("100"),Decimal("100"))
def eng():
 a=ExecutionAssumptions(commission_bps=Decimal("1"),spread_bps=Decimal("2"),slippage_bps=Decimal("1"),impact_bps=Decimal("1"),financing_bps_annual=Decimal("0"),borrow_bps_annual=Decimal("0"),latency_ms=0,partial_fill_fraction=Decimal("1"));return PaperTradingEngine(ConservativeBarExecutionSimulator(a,max_volume_participation=Decimal("1")),initial_cash=Decimal("10000"))
def state(rt):
 x=rt.paper_engine.export_state();v=rt.paper_engine.snapshot({"BTCUSDT":Decimal("100")});return {"generation":rt.generation,"seen":len(x.simulator.seen_order_ids),"pending":len(x.simulator.pending_orders),"fills":len(x.fills),"fill_qty":[str(f.fill.quantity) for f in x.fills],"cash":str(v.cash),"equity":str(v.equity),"position":str(v.positions.get("BTCUSDT",0)),"receipts":rt.store.event_count(rid,"PAPER_BAR_PROCESSED"),"status":rt.status.value}
def newrt(store=None):
 return PersistentPaperShadowRuntime(runtime_id=rid,mode=RuntimeMode.PAPER,store=store or PersistentRuntimeStore(db),paper_engine=eng())
if phase=="verify_recovery":
 rt=newrt();rt.start();res={"recovered":state(rt)};print(json.dumps(res),flush=True);(out/"verify_recovery.json").write_text(json.dumps(res,indent=2));rt.stop()
elif phase=="run":
 rt=newrt();rt.start();oid=uuid5(UUID("12345678-1234-5678-1234-567812345678"),rid);o=OrderRequest(strategy_id=UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),symbol="BTCUSDT",side=Side.BUY,quantity=Decimal("1"),order_type=OrderType.MARKET,decision_time=t,reference_price=Decimal("100"),order_id=oid);rt.submit_order(o);before=state(rt);err=None
 if case=="before_mutation":
  orig=rt.paper_engine.on_bar
  def fail(_): raise OSError("INJECTED_BEFORE_MUTATION")
  rt.paper_engine.on_bar=fail
 elif case=="during_commit":
  orig=rt.store.append_event_and_checkpoint
  def fail(*a,**k): raise OSError("INJECTED_DURING_ATOMIC_COMMIT")
  rt.store.append_event_and_checkpoint=fail
 elif case=="after_commit":
  orig=rt.store.append_event_and_checkpoint
  def fail(*a,**k):
   result=orig(*a,**k);raise OSError("INJECTED_AFTER_COMMIT")
  rt.store.append_event_and_checkpoint=fail
 try: rt.on_bar(bar)
 except Exception as e: err=f"{type(e).__name__}: {e}"
 after=state(rt);(out/"run.json").write_text(json.dumps({"before":before,"error":err,"after":after},indent=2));print(json.dumps({"before":before,"error":err,"after":after}),flush=True)
elif phase=="recover":
 rt=newrt()
 if case=="during_recovery":
  # First create a durable committed bar using a clean runtime, release it, then inject checkpoint read failure.
  rt.start();oid=uuid5(UUID("12345678-1234-5678-1234-567812345678"),rid);o=OrderRequest(strategy_id=UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),symbol="BTCUSDT",side=Side.BUY,quantity=Decimal("1"),order_type=OrderType.MARKET,decision_time=t,reference_price=Decimal("100"),order_id=oid);rt.submit_order(o);rt.on_bar(bar);committed=state(rt);rt.stop()
  store=PersistentRuntimeStore(db);orig=store.latest_checkpoint
  def fail(_): raise OSError("INJECTED_RECOVERY_READ_FAILURE")
  store.latest_checkpoint=fail;rt2=newrt(store);err=None
  try: rt2.start()
  except Exception as e: err=f"{type(e).__name__}: {e}"
  durable=PersistentRuntimeStore(db);res={"committed":committed,"recovery_error":err,"durable_receipts":durable.event_count(rid,"PAPER_BAR_PROCESSED"),"runtime_status":durable.get_runtime(rid).status};print(json.dumps(res),flush=True);(out/"recover.json").write_text(json.dumps(res,indent=2))
 else:
  # Let previous lease expire externally, then recover exact durable state.
  rt.start();res={"recovered":state(rt)};print(json.dumps(res),flush=True);(out/"recover.json").write_text(json.dumps(res,indent=2));rt.stop()

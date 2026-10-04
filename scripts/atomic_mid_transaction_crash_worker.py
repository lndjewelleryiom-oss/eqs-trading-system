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
ROOT=Path(r"C:\Users\lndje\Documents\EQS_Market_Terminal_V5_20260925\app");OUT=ROOT/"artifacts"/"commissioning"/"atomic_mid_transaction_crash_v1";OUT.mkdir(parents=True,exist_ok=True);db=OUT/"runtime.db";phase=sys.argv[1];t=datetime(2026,9,26,2,0,tzinfo=timezone.utc);rid="atomic-midtx-paper";oid=uuid5(UUID("12345678-1234-5678-1234-567812345678"),"atomic-midtx")
def eng():
 a=ExecutionAssumptions(commission_bps=Decimal("1"),spread_bps=Decimal("2"),slippage_bps=Decimal("1"),impact_bps=Decimal("1"),financing_bps_annual=Decimal("0"),borrow_bps_annual=Decimal("0"),latency_ms=0,partial_fill_fraction=Decimal("1"));return PaperTradingEngine(ConservativeBarExecutionSimulator(a,max_volume_participation=Decimal("1")),initial_cash=Decimal("10000"))
rt=PersistentPaperShadowRuntime(runtime_id=rid,mode=RuntimeMode.PAPER,store=PersistentRuntimeStore(db),paper_engine=eng());rt.start()
def st():
 x=rt.paper_engine.export_state();a=rt.paper_engine.snapshot({"BTCUSDT":Decimal("100")});return {"generation":rt.generation,"seen":len(x.simulator.seen_order_ids),"pending":len(x.simulator.pending_orders),"fills":len(x.fills),"cash":str(a.cash),"equity":str(a.equity),"position":str(a.positions.get("BTCUSDT",0))}
if phase=="crash":
 o=OrderRequest(strategy_id=UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),symbol="BTCUSDT",side=Side.BUY,quantity=Decimal("1"),order_type=OrderType.MARKET,decision_time=t,reference_price=Decimal("100"),order_id=oid);rt.submit_order(o,lineage={"acceptance":"atomic-midtx"});os.environ["EQS_CRASH_DURING_ATOMIC_PAPER_COMMIT"]="1";rt.on_bar(BarEvent("BTCUSDT",t+timedelta(seconds=1),Decimal("100"),Decimal("101"),Decimal("99"),Decimal("100"),Decimal("100")))
else:
 before=st();events=rt.store.load_events(rid);bars=[e for e in events if e.event_type=="PAPER_BAR_PROCESSED"];passed=before["fills"]==0 and before["pending"]==1 and before["position"]=="0" and before["cash"]=="10000" and len(bars)==0
 out={"recovered":before,"paper_bar_event_receipts":len(bars),"partial_commit_detected":not passed,"pass":passed};(OUT/"EVIDENCE.json").write_text(json.dumps(out,indent=2));print(json.dumps(out),flush=True);rt.stop()

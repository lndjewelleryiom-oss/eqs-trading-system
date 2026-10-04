from __future__ import annotations
import json,hashlib,time,urllib.request,datetime,sys,os
from pathlib import Path
ROOT=Path(r"C:\Users\lndje\Documents\EQS_Market_Terminal_V5_20260925\app")
RUN_ID=datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
OUT=ROOT/"artifacts"/"commissioning"/("operational_v1_soak_"+RUN_ID)
OUT.mkdir(parents=True,exist_ok=False)
START=time.time(); END=START+24*3600
SOURCES={"BYBIT_LINEAR_BTCUSDT":"https://api.bybit.com/v5/market/tickers?category=linear&symbol=BTCUSDT","OKX_SWAP_BTC_USDT":"https://www.okx.com/api/v5/market/ticker?instId=BTC-USDT-SWAP"}
def utc(): return datetime.datetime.now(datetime.timezone.utc).isoformat()
def heartbeat(**extra):
 d={"run_id":RUN_ID,"pid":os.getpid(),"started_at":datetime.datetime.fromtimestamp(START,datetime.timezone.utc).isoformat(),"updated_at":utc(),"end_epoch":END,"scheduled_end_at":datetime.datetime.fromtimestamp(END,datetime.timezone.utc).isoformat(),"live":True,"complete":False};d.update(extra)
 tmp=OUT/"heartbeat.tmp";tmp.write_text(json.dumps(d,indent=2),encoding="utf-8");tmp.replace(OUT/"heartbeat.json")
heartbeat()
counts={k:{"PASS":0,"FAIL":0} for k in SOURCES}
with (OUT/"feed_receipts.jsonl").open("a",encoding="utf-8") as f:
 while time.time()<END:
  cycle=utc()
  for source,url in SOURCES.items():
   received=utc()
   try:
    req=urllib.request.Request(url,headers={"User-Agent":"EQS-Operational-Commissioning-V1"})
    with urllib.request.urlopen(req,timeout=10) as r: raw=r.read(1_000_001)
    if len(raw)>1_000_000: raise ValueError("response too large")
    data=json.loads(raw)
    if source.startswith("BYBIT") and (data.get("retCode")!=0 or not data.get("result",{}).get("list")): raise ValueError("Bybit payload invalid")
    if source.startswith("OKX") and (data.get("code")!="0" or not data.get("data")): raise ValueError("OKX payload invalid")
    rec={"cycle":cycle,"received_at":received,"source":source,"url_class":"PUBLIC_MARKET_TICKER","sha256":hashlib.sha256(raw).hexdigest(),"bytes":len(raw),"status":"PASS","payload":data};counts[source]["PASS"]+=1
   except Exception as e:
    rec={"cycle":cycle,"received_at":received,"source":source,"url_class":"PUBLIC_MARKET_TICKER","status":"FAIL","error":type(e).__name__+":"+str(e)};counts[source]["FAIL"]+=1
   f.write(json.dumps(rec,separators=(",",":"))+"\n");f.flush();os.fsync(f.fileno())
  heartbeat(counts=counts)
  time.sleep(60)
heartbeat(live=False,complete=True,completed_at=utc(),counts=counts)
(OUT/"COMPLETE.json").write_text(json.dumps({"run_id":RUN_ID,"completed_at":utc(),"counts":counts,"receipts_sha256":hashlib.sha256((OUT/"feed_receipts.jsonl").read_bytes()).hexdigest()},indent=2),encoding="utf-8")

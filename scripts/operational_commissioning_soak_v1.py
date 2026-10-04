from __future__ import annotations
import json,hashlib,time,urllib.request,datetime,sys
from pathlib import Path
ROOT=Path(r"C:\Users\lndje\Documents\EQS_Market_Terminal_V5_20260925\app")
OUT=ROOT/"artifacts"/"commissioning"/"operational_v1_soak_20260925"
OUT.mkdir(parents=True,exist_ok=True)
END=time.time()+24*3600
SOURCES={
"BYBIT_LINEAR_BTCUSDT":"https://api.bybit.com/v5/market/tickers?category=linear&symbol=BTCUSDT",
"OKX_SWAP_BTC_USDT":"https://www.okx.com/api/v5/market/ticker?instId=BTC-USDT-SWAP",
}
def utc(): return datetime.datetime.now(datetime.timezone.utc).isoformat()
with (OUT/"feed_receipts.jsonl").open("a",encoding="utf-8") as f:
 while time.time()<END:
  cycle=utc()
  for source,url in SOURCES.items():
   received=utc()
   try:
    req=urllib.request.Request(url,headers={"User-Agent":"EQS-Operational-Commissioning-V1"})
    with urllib.request.urlopen(req,timeout=10) as r: raw=r.read(1_000_001)
    if len(raw)>1_000_000: raise ValueError("response too large")
    data=json.loads(raw); status="PASS"
    if source.startswith("BYBIT") and (data.get("retCode")!=0 or not data.get("result",{}).get("list")): raise ValueError("Bybit payload invalid")
    if source.startswith("OKX") and (data.get("code")!="0" or not data.get("data")): raise ValueError("OKX payload invalid")
    rec={"cycle":cycle,"received_at":received,"source":source,"url_class":"PUBLIC_MARKET_TICKER","sha256":hashlib.sha256(raw).hexdigest(),"bytes":len(raw),"status":status,"payload":data}
   except Exception as e:
    rec={"cycle":cycle,"received_at":received,"source":source,"url_class":"PUBLIC_MARKET_TICKER","status":"FAIL","error":type(e).__name__+":"+str(e)}
   f.write(json.dumps(rec,separators=(",",":"))+"\n"); f.flush()
  (OUT/"heartbeat.json").write_text(json.dumps({"updated_at":utc(),"end_epoch":END,"live":True},indent=2))
  time.sleep(60)
(OUT/"heartbeat.json").write_text(json.dumps({"updated_at":utc(),"end_epoch":END,"live":False,"complete":True},indent=2))

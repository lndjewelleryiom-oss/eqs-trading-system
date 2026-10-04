from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from urllib.request import Request,urlopen
from .models import BookLevel,BookUpdate,EventKind,MarketDataMeta,PerpetualStateEvent,TradeEvent,PerpetualInstrumentDefinition
UTC=timezone.utc
@dataclass(frozen=True,slots=True)
class CurrentVenueSnapshot:
 venue:str; instrument_id:str; symbol:str; events:tuple; raw_sha256s:tuple[str,...]; received_at:datetime
def _get(url):
 t=datetime.now(UTC); req=Request(url,headers={"User-Agent":"EQS-CurrentMarket-Runtime/1"})
 with urlopen(req,timeout=8) as r: raw=r.read(1_000_001)
 if len(raw)>1_000_000: raise ValueError("response too large")
 return json.loads(raw),raw,t,datetime.now(UTC)
def _meta(venue,i,s,kind,event,pub,avail,recv,ch,seq,raw):
 # Public REST responses can be generated just before local receipt; clamp small clock skew forward.
 pub=max(pub,event); avail=max(avail,pub); recv=max(recv,avail)
 return MarketDataMeta(venue,i,s,kind,event,pub,avail,recv,ch,seq,sha256(raw).hexdigest())
def _ms(x): return datetime.fromtimestamp(int(x)/1000,UTC)
def fetch_current_snapshot(venue:str)->CurrentVenueSnapshot:
 if venue=="BYBIT_LINEAR":
  i="BTC-USDT-PERP:BYBIT_LINEAR"; s="BTCUSDT"; base="https://api.bybit.com"
  tr,traw,a,r=_get(base+"/v5/market/recent-trade?category=linear&symbol=BTCUSDT&limit=20")
  ob,oraw,a2,r2=_get(base+"/v5/market/orderbook?category=linear&symbol=BTCUSDT&limit=20")
  tk,kraw,a3,r3=_get(base+"/v5/market/tickers?category=linear&symbol=BTCUSDT")
  events=[]
  pub=_ms(tr["time"])
  for x in tr["result"]["list"]:
   t=_ms(x["time"]); m=_meta(venue,i,s,EventKind.TRADE,t,pub,a,r,"current/rest/recent-trade",f'{x.get("seq")}:{x["execId"]}',traw); events.append(TradeEvent(m,str(x["execId"]),Decimal(x["price"]),Decimal(x["size"]),str(x["side"]).upper()))
  x=ob["result"]; t=_ms(x.get("cts") or x["ts"]); m=_meta(venue,i,s,EventKind.BOOK_SNAPSHOT,t,_ms(ob["time"]),a2,r2,"current/rest/orderbook",f'{x.get("u")}:{x.get("seq")}',oraw); events.append(BookUpdate(m,tuple(BookLevel(Decimal(p),Decimal(q)) for p,q in x["b"]),tuple(BookLevel(Decimal(p),Decimal(q)) for p,q in x["a"]),final_sequence=int(x["u"])))
  x=tk["result"]["list"][0]; t=_ms(tk["time"]); m=_meta(venue,i,s,EventKind.PERPETUAL_STATE,t,t,a3,r3,"current/rest/tickers",None,kraw); events.append(PerpetualStateEvent(m,mark_price=Decimal(x["markPrice"]),index_price=Decimal(x["indexPrice"]),funding_rate=Decimal(x["fundingRate"]),next_funding_time=_ms(x["nextFundingTime"]),open_interest=Decimal(x["openInterest"]),open_interest_value=Decimal(x["openInterestValue"])))
  raws=(sha256(traw).hexdigest(),sha256(oraw).hexdigest(),sha256(kraw).hexdigest()); recv=max(e.meta.received_at for e in events)
 elif venue=="OKX_SWAP":
  i="BTC-USDT-PERP:OKX_SWAP"; s="BTC-USDT-SWAP"; base="https://www.okx.com"; events=[]; raws=[]
  specs=[("trades","/api/v5/market/trades?instId=BTC-USDT-SWAP&limit=20"),("books","/api/v5/market/books?instId=BTC-USDT-SWAP&sz=20"),("mark","/api/v5/public/mark-price?instType=SWAP&instId=BTC-USDT-SWAP"),("funding","/api/v5/public/funding-rate?instId=BTC-USDT-SWAP"),("oi","/api/v5/public/open-interest?instType=SWAP&instId=BTC-USDT-SWAP")]
  for label,path in specs:
   d,raw,a,r=_get(base+path); raws.append(sha256(raw).hexdigest())
   for x in d["data"]:
    t=_ms(x.get("ts") or x.get("fundingTime"))
    if label=="trades": m=_meta(venue,i,s,EventKind.TRADE,t,t,a,r,"current/rest/trades",str(x["tradeId"]),raw); events.append(TradeEvent(m,str(x["tradeId"]),Decimal(x["px"]),Decimal(x["sz"]),str(x["side"]).upper()))
    elif label=="books": m=_meta(venue,i,s,EventKind.BOOK_SNAPSHOT,t,t,a,r,"current/rest/books",str(x.get("seqId")),raw); events.append(BookUpdate(m,tuple(BookLevel(Decimal(z[0]),Decimal(z[1])) for z in x["bids"]),tuple(BookLevel(Decimal(z[0]),Decimal(z[1])) for z in x["asks"]),final_sequence=int(x["seqId"])))
    else: m=_meta(venue,i,s,EventKind.PERPETUAL_STATE,t,t,a,r,"current/rest/"+label,None,raw); events.append(PerpetualStateEvent(m,mark_price=Decimal(x["markPx"]) if x.get("markPx") else None,funding_rate=Decimal(x["fundingRate"]) if x.get("fundingRate") else None,next_funding_time=_ms(x["nextFundingTime"]) if x.get("nextFundingTime") else None,open_interest=Decimal(x["oi"]) if x.get("oi") else None,open_interest_value=Decimal(x["oiUsd"]) if x.get("oiUsd") else None))
  raws=tuple(raws); recv=max(e.meta.received_at for e in events)
 else: raise ValueError("unsupported commissioning venue")
 events=tuple(sorted(events,key=lambda e:(e.meta.available_at,e.meta.event_time,e.meta.kind.value,e.meta.canonical_identity())))
 return CurrentVenueSnapshot(venue,i,s,events,tuple(raws),recv)

def fetch_current_instrument_definition(venue:str)->PerpetualInstrumentDefinition:
 if venue=="BYBIT_LINEAR":
  i="BTC-USDT-PERP:BYBIT_LINEAR"; sym="BTCUSDT"; d,raw,a,r=_get("https://api.bybit.com/v5/market/instruments-info?category=linear&symbol=BTCUSDT"); x=d["result"]["list"][0]
  launch=_ms(x["launchTime"]); tick=Decimal(x["priceFilter"]["tickSize"]); lot=Decimal(x["lotSizeFilter"]["qtyStep"]); status=x["status"]; base=x["baseCoin"]; quote=x["quoteCoin"]; settle=x["settleCoin"]
  # The venue response is first known to this collector at receipt. launchTime is the venue's effective/listing boundary, not publication evidence.
  return PerpetualInstrumentDefinition(i,venue,sym,base,quote,settle,"LINEAR",tick,lot,None,status,launch,a,a,r,sha256(raw).hexdigest())
 if venue=="OKX_SWAP":
  i="BTC-USDT-PERP:OKX_SWAP"; sym="BTC-USDT-SWAP"; d,raw,a,r=_get("https://www.okx.com/api/v5/public/instruments?instType=SWAP&instId=BTC-USDT-SWAP"); x=d["data"][0]
  listing=_ms(x["listTime"]); tick=Decimal(x["tickSz"]); lot=Decimal(x["lotSz"]); ct=Decimal(x["ctVal"]) if x.get("ctVal") else None; status=x["state"].upper()
  return PerpetualInstrumentDefinition(i,venue,sym,x["ctValCcy"] or "BTC",x["settleCcy"],x["settleCcy"],"LINEAR" if x.get("ctType")=="linear" else "INVERSE",tick,lot,ct,status,listing,a,a,r,sha256(raw).hexdigest())
 raise ValueError("unsupported commissioning venue")

from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime,timezone
from typing import Callable
from quant_system.execution.broker import VenueOrder
from quant_system.execution.bybit_private import BybitFill,BybitLinearPrivateReadAdapter,BybitPrivateReadError,BybitRateLimited

@dataclass(frozen=True,slots=True)
class BybitHistoricalEvidence:
 orders:tuple[VenueOrder,...];fills:tuple[BybitFill,...];observed_at:datetime;complete:bool;reasons:tuple[str,...]
class BybitHistoryReader:
 """Cursor-bounded authenticated history. Read-only and fail-closed on incomplete coverage."""
 def __init__(self,get:Callable[[str,dict[str,str]],dict],*,clock=lambda:datetime.now(timezone.utc),max_pages=20,retries=2):
  self._get=get;self._clock=clock;self.max_pages=max_pages;self.retries=retries
 def _call(self,path,params):
  for attempt in range(self.retries+1):
   try:p=self._get(path,params)
   except Exception as exc:
    if attempt==self.retries:raise BybitPrivateReadError("HISTORY_TRANSPORT_FAILURE") from exc
    continue
   code=int(p.get("retCode",-1))
   if code==0:return p.get("result",{})
   if code in {10006,429}:
    if attempt==self.retries:raise BybitRateLimited("BYBIT_HISTORY_RATE_LIMIT")
    continue
   if code in {10003,10004,10005,10007}:raise BybitPrivateReadError("BYBIT_AUTHENTICATION_OR_PERMISSION_FAILURE")
   raise BybitPrivateReadError("BYBIT_HISTORY_REJECTED")
  raise BybitPrivateReadError("BYBIT_HISTORY_UNAVAILABLE")
 def _pages(self,path,base,key):
  out=[];cursor="";seen=set()
  for _ in range(self.max_pages):
   p=dict(base)
   if cursor:p["cursor"]=cursor
   result=self._call(path,p);rows=result.get("list",[]);next_cursor=str(result.get("nextPageCursor") or "")
   sig=tuple(str(x.get(key,"")) for x in rows)
   if sig in seen and rows:return out,False
   seen.add(sig);out.extend(rows)
   if not next_cursor:return out,True
   if next_cursor==cursor:return out,False
   cursor=next_cursor
  return out,False
 def evidence(self,*,instrument=None,start_time=None,end_time=None):
  base={"category":"linear","limit":"50"}
  if instrument:base["symbol"]=instrument
  if start_time is not None:base["startTime"]=str(start_time)
  if end_time is not None:base["endTime"]=str(end_time)
  orders,oc=self._pages("/v5/order/history",base,"orderId")
  fbase=dict(base);fbase["limit"]="100"
  fills,fc=self._pages("/v5/execution/list",fbase,"execId")
  od={str(x.get("orderId","")):x for x in orders}
  fd={str(x.get("execId","")):x for x in fills}
  complete=oc and fc
  return BybitHistoricalEvidence(tuple(BybitLinearPrivateReadAdapter._order(x) for x in od.values()),tuple(BybitLinearPrivateReadAdapter._fill(x) for x in fd.values()),self._clock(),complete,("COMPLETE",) if complete else ("PAGINATION_INCOMPLETE",))

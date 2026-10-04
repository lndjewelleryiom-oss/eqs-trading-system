from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime,timezone
from decimal import Decimal
from typing import Callable
from uuid import UUID,uuid5
from quant_system.execution.adapter_contract import ExchangeAdapterContract,PrivateReadSnapshot,VenueInstrument
from quant_system.execution.broker import BrokerHealth,VenueOrder,VenueOrderStatus

class BybitPrivateReadError(RuntimeError):pass
class BybitRateLimited(BybitPrivateReadError):pass
_NS=UUID("f083a31e-9445-5e98-8592-6760c93b48e2")
_STATE={"New":VenueOrderStatus.ACKNOWLEDGED,"PartiallyFilled":VenueOrderStatus.PARTIAL,"Filled":VenueOrderStatus.FILLED,"Cancelled":VenueOrderStatus.CANCELED,"PartiallyFilledCanceled":VenueOrderStatus.CANCELED,"Rejected":VenueOrderStatus.REJECTED,"Deactivated":VenueOrderStatus.REJECTED}
@dataclass(frozen=True,slots=True)
class BybitFill:
 exec_id:str;order_id:str;client_order_id:str;symbol:str;side:str;quantity:Decimal;price:Decimal;observed_at:datetime
class BybitLinearPrivateReadAdapter(ExchangeAdapterContract):
 venue="BYBIT_LINEAR";trading_capable=False
 def __init__(self,authenticated_get:Callable[[str,dict[str,str]],dict],*,clock=lambda:datetime.now(timezone.utc),settle_coin="USDT"):
  self._get=authenticated_get;self._clock=clock;self.settle_coin=settle_coin;self._last_error=None
 def _call(self,path,params):
  try:p=self._get(path,params)
  except Exception as exc:self._last_error="TRANSPORT";raise BybitPrivateReadError("PRIVATE_READ_TRANSPORT_FAILURE") from exc
  code=int(p.get("retCode",-1))
  if code==0:self._last_error=None;return p.get("result",{})
  if code in {10006,429}:self._last_error="RATE_LIMIT";raise BybitRateLimited("BYBIT_RATE_LIMIT")
  if code in {10003,10004,10005,10007}:self._last_error="AUTH";raise BybitPrivateReadError("BYBIT_AUTHENTICATION_OR_PERMISSION_FAILURE")
  self._last_error="VENUE_ERROR";raise BybitPrivateReadError("BYBIT_PRIVATE_READ_REJECTED")
 def health(self):return BrokerHealth(self._last_error is None,self._last_error or "OK")
 @staticmethod
 def _cid(row):
  raw=str(row.get("orderLinkId") or "")
  try:return UUID(raw)
  except Exception:return uuid5(_NS,raw or str(row.get("orderId","")))
 @classmethod
 def _order(cls,row):
  return VenueOrder(cls._cid(row),str(row.get("orderId","")),str(row.get("symbol","")),_STATE.get(str(row.get("orderStatus","")),VenueOrderStatus.REJECTED),Decimal(row.get("qty") or "0"),Decimal(row.get("cumExecQty") or "0"))
 @staticmethod
 def _fill(row):
  return BybitFill(str(row.get("execId","")),str(row.get("orderId","")),str(row.get("orderLinkId","")),str(row.get("symbol","")),str(row.get("side","")).upper(),Decimal(row.get("execQty") or "0"),Decimal(row.get("execPrice") or "0"),datetime.fromtimestamp(int(row.get("execTime") or "0")/1000,timezone.utc))
 def instrument(self,symbol):
  r=self._call("/v5/market/instruments-info",{"category":"linear","symbol":symbol})
  row=next((x for x in r.get("list",[]) if x.get("symbol")==symbol),None)
  if not row:raise BybitPrivateReadError("INSTRUMENT_NOT_RETURNED")
  lot=row.get("lotSizeFilter",{});price=row.get("priceFilter",{})
  return VenueInstrument(symbol,Decimal(lot["minOrderQty"]),Decimal(lot["qtyStep"]),Decimal(price["tickSize"]),str(row.get("status","Trading"))=="Trading")
 def private_read(self):
  pos=self._call("/v5/position/list",{"category":"linear","settleCoin":self.settle_coin,"limit":"200"})
  orders=self._call("/v5/order/realtime",{"category":"linear","settleCoin":self.settle_coin,"openOnly":"0","limit":"50"})
  fills=self._call("/v5/execution/list",{"category":"linear","settleCoin":self.settle_coin,"limit":"100"})
  positions={}
  for x in pos.get("list",[]):
   q=Decimal(x.get("size") or "0")
   if x.get("side")=="Sell":q=-q
   positions[x["symbol"]]=positions.get(x["symbol"],Decimal("0"))+q
  return PrivateReadSnapshot(self._clock(),positions,tuple(self._order(x) for x in orders.get("list",[])),tuple(self._fill(x) for x in fills.get("list",[])))
 def get_order(self,client_order_id,symbol=None):
  q={"category":"linear","orderLinkId":str(client_order_id)}
  if symbol:q["symbol"]=symbol
  rows=self._call("/v5/order/realtime",q).get("list",[])
  return None if not rows else self._order(rows[0])

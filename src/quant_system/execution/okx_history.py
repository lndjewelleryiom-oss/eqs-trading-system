from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime,timezone
from decimal import Decimal
from time import sleep
from typing import Callable
from uuid import UUID,uuid5
from quant_system.execution.broker import VenueOrder,VenueOrderStatus
from quant_system.execution.okx_private import OkxFill,OkxPrivateReadError,OkxRateLimited

_ID_NS=UUID("a238a5d7-24f6-55fd-a1f8-944c824db94f")
_STATE={"live":VenueOrderStatus.ACKNOWLEDGED,"partially_filled":VenueOrderStatus.PARTIAL,"filled":VenueOrderStatus.FILLED,"canceled":VenueOrderStatus.CANCELED,"mmp_canceled":VenueOrderStatus.CANCELED}

@dataclass(frozen=True,slots=True)
class OkxHistoricalEvidence:
    orders: tuple[VenueOrder,...]
    fills: tuple[OkxFill,...]
    observed_at: datetime
    complete: bool
    reasons: tuple[str,...]

class OkxHistoryReader:
    """Bounded read-only history reader. Never submits, amends, cancels or transfers."""
    def __init__(self,get:Callable[[str,dict[str,str]],dict],*,clock=lambda:datetime.now(timezone.utc),max_pages=20,retries=2):
        self._get=get;self._clock=clock;self.max_pages=max_pages;self.retries=retries
    def _call(self,path,params):
        for attempt in range(self.retries+1):
            try: payload=self._get(path,params)
            except Exception as exc:
                if attempt==self.retries:raise OkxPrivateReadError("HISTORY_TRANSPORT_FAILURE") from exc
                continue
            code=str(payload.get("code",""))
            if code=="0":return payload.get("data",[])
            if code=="50011":
                if attempt==self.retries:raise OkxRateLimited("OKX_HISTORY_RATE_LIMIT")
                continue
            if code in {"50111","50113","50114"}:raise OkxPrivateReadError("OKX_AUTHENTICATION_FAILURE")
            if code in {"50013","50026"}:raise OkxPrivateReadError("OKX_HISTORY_UNAVAILABLE")
            raise OkxPrivateReadError("OKX_HISTORY_REJECTED")
        raise OkxPrivateReadError("OKX_HISTORY_UNAVAILABLE")
    @staticmethod
    def _cid(row):
        raw=str(row.get("clOrdId") or "")
        try:return UUID(raw)
        except Exception:return uuid5(_ID_NS,raw or str(row.get("ordId","")))
    @classmethod
    def _order(cls,row):
        return VenueOrder(cls._cid(row),str(row.get("ordId","")),str(row.get("instId","")),_STATE.get(str(row.get("state","")),VenueOrderStatus.REJECTED),Decimal(row.get("sz") or "0"),Decimal(row.get("accFillSz") or "0"))
    @staticmethod
    def _fill(row):
        ts=int(row.get("fillTime") or row.get("ts") or "0")
        return OkxFill(str(row.get("tradeId","")),str(row.get("ordId","")),str(row.get("clOrdId","")),str(row.get("instId","")),str(row.get("side","")).upper(),Decimal(row.get("fillSz") or "0"),Decimal(row.get("fillPx") or "0"),datetime.fromtimestamp(ts/1000,timezone.utc))
    def _pages(self,path,base):
        out=[];after=None;seen=set()
        for _ in range(self.max_pages):
            params=dict(base)
            if after:params["after"]=after
            rows=self._call(path,params)
            if not rows:return out,True
            sig=tuple(str(x.get("ordId") or x.get("tradeId") or "") for x in rows)
            if sig in seen:return out,False
            seen.add(sig);out.extend(rows)
            if len(rows)<100:return out,True
            after=str(rows[-1].get("ordId") or rows[-1].get("billId") or "")
            if not after:return out,False
        return out,False
    def evidence(self,*,instrument:str|None=None):
        base={"instType":"SWAP","limit":"100"}
        if instrument:base["instId"]=instrument
        orders,oc=self._pages("/api/v5/trade/orders-history-archive",base)
        fills,fc=self._pages("/api/v5/trade/fills-history",base)
        od={str(x.get("ordId","")):x for x in orders}
        fd={str(x.get("tradeId","")):x for x in fills}
        complete=oc and fc
        return OkxHistoricalEvidence(tuple(self._order(x) for x in od.values()),tuple(self._fill(x) for x in fd.values()),self._clock(),complete,("COMPLETE",) if complete else ("PAGINATION_INCOMPLETE",))

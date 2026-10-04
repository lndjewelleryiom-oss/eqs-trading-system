from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable
from uuid import UUID
from quant_system.execution.adapter_contract import ExchangeAdapterContract, PrivateReadSnapshot, VenueInstrument
from quant_system.execution.broker import BrokerHealth, VenueOrder, VenueOrderStatus

class OkxPrivateReadError(RuntimeError): pass
class OkxRateLimited(OkxPrivateReadError): pass

_STATE={"live":VenueOrderStatus.ACKNOWLEDGED,"partially_filled":VenueOrderStatus.PARTIAL,"filled":VenueOrderStatus.FILLED,"canceled":VenueOrderStatus.CANCELED,"mmp_canceled":VenueOrderStatus.CANCELED}

@dataclass(frozen=True, slots=True)
class OkxFill:
    trade_id: str
    order_id: str
    client_order_id: str
    symbol: str
    side: str
    quantity: Decimal
    price: Decimal
    observed_at: datetime

class OkxSwapPrivateReadAdapter(ExchangeAdapterContract):
    venue="OKX_SWAP"
    trading_capable=False
    def __init__(self, authenticated_get: Callable[[str,dict[str,str]],dict], *, clock=lambda:datetime.now(timezone.utc)):
        self._get=authenticated_get; self._clock=clock; self._last_error=None
    def _call(self,path,params=None):
        try: payload=self._get(path,params or {})
        except Exception as exc: self._last_error=type(exc).__name__; raise OkxPrivateReadError("PRIVATE_READ_TRANSPORT_FAILURE") from exc
        code=str(payload.get("code",""))
        if code=="50011": self._last_error="RATE_LIMIT"; raise OkxRateLimited("OKX_RATE_LIMIT")
        if code!="0": self._last_error="VENUE_ERROR"; raise OkxPrivateReadError("OKX_PRIVATE_READ_REJECTED")
        self._last_error=None; return payload.get("data",[])
    def health(self): return BrokerHealth(self._last_error is None, self._last_error or "OK")
    def instrument(self,symbol):
        rows=self._call("/api/v5/account/instruments",{"instType":"SWAP"})
        row=next((x for x in rows if x.get("instId")==symbol),None)
        if not row: raise OkxPrivateReadError("INSTRUMENT_NOT_RETURNED")
        return VenueInstrument(symbol,Decimal(row["minSz"]),Decimal(row["lotSz"]),Decimal(row["tickSz"]),row.get("state")=="live")
    @staticmethod
    def _order(row):
        raw=row.get("clOrdId") or row.get("ordId")
        try: cid=UUID(raw)
        except Exception: cid=UUID(int=int(row.get("ordId","0")) % (1<<128))
        state=_STATE.get(row.get("state"),VenueOrderStatus.REJECTED)
        return VenueOrder(cid,str(row.get("ordId","")),str(row.get("instId","")),state,Decimal(row.get("sz") or "0"),Decimal(row.get("accFillSz") or "0"))
    def private_read(self):
        positions=self._call("/api/v5/account/positions",{"instType":"SWAP"})
        orders=self._call("/api/v5/trade/orders-pending",{"instType":"SWAP"})
        fills=self._call("/api/v5/trade/fills",{"instType":"SWAP"})
        pos={}
        for row in positions:
            q=Decimal(row.get("pos") or "0")
            side=row.get("posSide","net")
            if side=="short" and q>0:q=-q
            pos[row["instId"]]=pos.get(row["instId"],Decimal("0"))+q
        fs=tuple(OkxFill(str(x.get("tradeId","")),str(x.get("ordId","")),str(x.get("clOrdId","")),str(x.get("instId","")),str(x.get("side","")).upper(),Decimal(x.get("fillSz") or "0"),Decimal(x.get("fillPx") or "0"),datetime.fromtimestamp(int(x.get("ts") or x.get("fillTime") or "0")/1000,timezone.utc)) for x in fills)
        return PrivateReadSnapshot(self._clock(),pos,tuple(self._order(x) for x in orders),fs)
    def get_order(self,client_order_id):
        rows=self._call("/api/v5/trade/order",{"instId":"BTC-USDT-SWAP","clOrdId":str(client_order_id)})
        if not rows: return None
        return self._order(rows[0])

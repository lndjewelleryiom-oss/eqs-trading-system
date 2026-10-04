from datetime import datetime,timezone
from decimal import Decimal
import pytest
from quant_system.execution.okx_private import OkxSwapPrivateReadAdapter,OkxRateLimited
NOW=datetime(2026,9,26,tzinfo=timezone.utc)
def transport(path,params):
    if path.endswith("instruments"): return {"code":"0","data":[{"instId":"BTC-USDT-SWAP","minSz":"0.01","lotSz":"0.01","tickSz":"0.1","state":"live"}]}
    if path.endswith("positions"): return {"code":"0","data":[{"instId":"BTC-USDT-SWAP","pos":"2","posSide":"long"},{"instId":"ETH-USDT-SWAP","pos":"3","posSide":"short"}]}
    if path.endswith("orders-pending"): return {"code":"0","data":[{"ordId":"123","clOrdId":"","instId":"BTC-USDT-SWAP","state":"partially_filled","sz":"2","accFillSz":"1"}]}
    if path.endswith("fills"): return {"code":"0","data":[{"tradeId":"t1","ordId":"123","clOrdId":"","instId":"BTC-USDT-SWAP","side":"buy","fillSz":"1","fillPx":"100","ts":"1790380800000"}]}
    if path.endswith("/order"): return {"code":"0","data":[]}
    raise AssertionError(path)
def test_private_snapshot_normalizes_positions_orders_fills():
    a=OkxSwapPrivateReadAdapter(transport,clock=lambda:NOW); s=a.private_read()
    assert s.positions=={"BTC-USDT-SWAP":Decimal("2"),"ETH-USDT-SWAP":Decimal("-3")}
    assert len(s.open_orders)==1 and s.open_orders[0].filled_quantity==Decimal("1") and len(s.recent_fills)==1
def test_instrument_constraints_are_private_read_normalized():
    i=OkxSwapPrivateReadAdapter(transport).instrument("BTC-USDT-SWAP")
    assert i.min_quantity==Decimal("0.01") and i.quantity_step==Decimal("0.01") and i.price_tick==Decimal("0.1")
def test_adapter_has_no_trading_capability():
    a=OkxSwapPrivateReadAdapter(transport); assert a.trading_capable is False
    with pytest.raises(PermissionError): a.submit_order(None)
    assert not hasattr(a,"withdraw")
def test_rate_limit_is_normalized_and_health_degrades():
    a=OkxSwapPrivateReadAdapter(lambda p,q:{"code":"50011","data":[]})
    with pytest.raises(OkxRateLimited): a.private_read()
    assert not a.health().healthy and a.health().reason=="RATE_LIMIT"
def test_transport_failure_does_not_leak_exception_text():
    def bad(p,q): raise RuntimeError("SECRET_SHOULD_NOT_ESCAPE")
    a=OkxSwapPrivateReadAdapter(bad)
    with pytest.raises(Exception) as e:a.private_read()
    assert "SECRET_SHOULD_NOT_ESCAPE" not in str(e.value)

from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4
import pytest
from quant_system.core.enums import OrderType, Side
from quant_system.execution.adapter_contract import ExchangeAdapterContract, VenueInstrument
from quant_system.execution.models import OrderRequest

def order(q="1", limit=None):
    return OrderRequest(uuid4(),"BTC-USDT-SWAP",Side.BUY,Decimal(q),OrderType.MARKET if limit is None else OrderType.LIMIT,datetime(2026,1,1,tzinfo=timezone.utc),Decimal("100"),None if limit is None else Decimal(limit))
def test_validation_normalizes_quantity_constraints():
    a=ExchangeAdapterContract(); i=VenueInstrument("BTC-USDT-SWAP",Decimal("0.1"),Decimal("0.1"),Decimal("0.5"))
    assert a.validate_order(order("1"),i).valid
    assert "QUANTITY_STEP_MISMATCH" in a.validate_order(order("1.05"),i).reasons
def test_validation_normalizes_tick_constraints():
    a=ExchangeAdapterContract(); i=VenueInstrument("BTC-USDT-SWAP",Decimal("0.1"),Decimal("0.1"),Decimal("0.5"))
    assert "PRICE_TICK_MISMATCH" in a.validate_order(order("1","100.25"),i).reasons
def test_base_contract_is_read_or_validation_only():
    a=ExchangeAdapterContract()
    with pytest.raises(PermissionError,match="TRADING_NOT_ENABLED"): a.submit_order(order())
    with pytest.raises(PermissionError,match="TRADING_NOT_ENABLED"): a.cancel_order(uuid4())
def test_contract_has_no_withdrawal_surface():
    assert not hasattr(ExchangeAdapterContract(),"withdraw") and not hasattr(ExchangeAdapterContract(),"withdrawal")

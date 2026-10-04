from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN

ZERO=Decimal('0')
ONE=Decimal('1')

@dataclass(frozen=True, slots=True)
class ExploratorySizingPolicyV3:
    starting_equity: Decimal=Decimal('10000')
    target_position_notional_fraction: Decimal=Decimal('0.10')
    max_gross_leverage: Decimal=Decimal('1.00')
    quantity_step: Decimal=Decimal('0.00001')

    def __post_init__(self)->None:
        if self.starting_equity<=ZERO: raise ValueError('starting_equity must be positive')
        if not ZERO<self.target_position_notional_fraction<=ONE: raise ValueError('target fraction must be in (0,1]')
        if not ZERO<self.max_gross_leverage<=ONE: raise ValueError('max gross leverage must be in (0,1] for V3 feasibility')
        if self.quantity_step<=ZERO: raise ValueError('quantity_step must be positive')

    @property
    def target_notional(self)->Decimal:
        return self.starting_equity*self.target_position_notional_fraction

    @property
    def gross_notional_limit(self)->Decimal:
        return self.starting_equity*self.max_gross_leverage

    def entry_quantity(self, reference_price: Decimal)->Decimal:
        price=Decimal(reference_price)
        if price<=ZERO: raise ValueError('reference_price must be positive')
        raw=self.target_notional/price
        steps=(raw/self.quantity_step).to_integral_value(rounding=ROUND_DOWN)
        quantity=steps*self.quantity_step
        if quantity<=ZERO: raise ValueError('target notional is below one quantity step')
        notional=quantity*price
        if notional>self.target_notional or notional>self.gross_notional_limit:
            raise RuntimeError('sizing policy produced excessive notional')
        return quantity

    def assert_entry_within_policy(self, *, quantity: Decimal, reference_price: Decimal)->None:
        qty=abs(Decimal(quantity)); price=Decimal(reference_price)
        if qty<=ZERO or price<=ZERO: raise ValueError('positive quantity and price required')
        notional=qty*price
        if notional>self.target_notional:
            raise PermissionError('V3_TARGET_POSITION_NOTIONAL_EXCEEDED')
        if notional>self.gross_notional_limit:
            raise PermissionError('V3_GROSS_LEVERAGE_LIMIT_EXCEEDED')

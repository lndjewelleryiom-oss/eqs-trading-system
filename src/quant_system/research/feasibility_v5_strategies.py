from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from decimal import Decimal

ZERO=Decimal("0")


@dataclass(frozen=True,slots=True)
class SlowFeature:
    close: Decimal
    premium_fraction: Decimal
    mark_index_basis_fraction: Decimal


class SlowMomentumTrend:
    def __init__(self,*,short_lookback_bars:int,long_lookback_bars:int,short_threshold:Decimal,long_threshold:Decimal,premium_abs_max:Decimal,max_holding_bars:int):
        if short_lookback_bars<=0 or long_lookback_bars<=short_lookback_bars:
            raise ValueError("invalid momentum lookbacks")
        if short_threshold<=0 or long_threshold<=0 or premium_abs_max<0 or max_holding_bars<=0:
            raise ValueError("invalid momentum configuration")
        self.short=short_lookback_bars; self.long=long_lookback_bars
        self.short_threshold=short_threshold; self.long_threshold=long_threshold
        self.premium_abs_max=premium_abs_max; self.max_holding=max_holding_bars
        self.closes=deque(maxlen=long_lookback_bars+1); self.age=0

    def observe(self,feature:SlowFeature,position_quantity:Decimal):
        self.closes.append(feature.close)
        current=1 if position_quantity>ZERO else -1 if position_quantity<ZERO else 0
        self.age=self.age+1 if current else 0
        if len(self.closes)<self.long+1:
            return None
        short_return=feature.close/self.closes[-self.short-1]-Decimal("1")
        long_return=feature.close/self.closes[0]-Decimal("1")
        if current:
            if self.age>=self.max_holding:
                return (0,"MAX_HOLD",{"short_return":str(short_return),"long_return":str(long_return)})
            if (current>0 and short_return<=ZERO) or (current<0 and short_return>=ZERO):
                return (0,"MOMENTUM_REVERSAL",{"short_return":str(short_return),"long_return":str(long_return)})
            return None
        if abs(feature.premium_fraction)>self.premium_abs_max:
            return None
        if short_return>=self.short_threshold and long_return>=self.long_threshold:
            return (1,"SLOW_TREND_LONG",{"short_return":str(short_return),"long_return":str(long_return)})
        if short_return<=-self.short_threshold and long_return<=-self.long_threshold:
            return (-1,"SLOW_TREND_SHORT",{"short_return":str(short_return),"long_return":str(long_return)})
        return None


class SlowDonchianBreakout:
    def __init__(self,*,entry_lookback_bars:int,exit_lookback_bars:int,premium_abs_max:Decimal,max_holding_bars:int):
        if entry_lookback_bars<=1 or exit_lookback_bars<=0 or exit_lookback_bars>=entry_lookback_bars:
            raise ValueError("invalid channel lookbacks")
        if premium_abs_max<0 or max_holding_bars<=0:
            raise ValueError("invalid breakout configuration")
        self.entry=entry_lookback_bars; self.exit=exit_lookback_bars
        self.premium_abs_max=premium_abs_max; self.max_holding=max_holding_bars
        self.closes=deque(maxlen=entry_lookback_bars); self.age=0

    def observe(self,feature:SlowFeature,position_quantity:Decimal):
        current=1 if position_quantity>ZERO else -1 if position_quantity<ZERO else 0
        prior=list(self.closes)
        self.closes.append(feature.close)
        self.age=self.age+1 if current else 0
        if len(prior)<self.entry:
            return None
        entry_high=max(prior[-self.entry:]); entry_low=min(prior[-self.entry:])
        exit_high=max(prior[-self.exit:]); exit_low=min(prior[-self.exit:])
        if current>0:
            if self.age>=self.max_holding:
                return (0,"MAX_HOLD",{"entry_high":str(entry_high),"entry_low":str(entry_low)})
            if feature.close<exit_low:
                return (0,"CHANNEL_EXIT_LONG",{"exit_low":str(exit_low)})
            return None
        if current<0:
            if self.age>=self.max_holding:
                return (0,"MAX_HOLD",{"entry_high":str(entry_high),"entry_low":str(entry_low)})
            if feature.close>exit_high:
                return (0,"CHANNEL_EXIT_SHORT",{"exit_high":str(exit_high)})
            return None
        if abs(feature.premium_fraction)>self.premium_abs_max:
            return None
        if feature.close>entry_high:
            return (1,"SLOW_BREAKOUT_LONG",{"entry_high":str(entry_high)})
        if feature.close<entry_low:
            return (-1,"SLOW_BREAKOUT_SHORT",{"entry_low":str(entry_low)})
        return None

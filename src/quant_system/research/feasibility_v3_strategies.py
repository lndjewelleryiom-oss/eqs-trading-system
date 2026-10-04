from __future__ import annotations

from collections import deque
from decimal import Decimal

ZERO = Decimal("0")
ONE = Decimal("1")


def _mean(values: tuple[Decimal, ...]) -> Decimal:
    return sum(values, ZERO) / Decimal(len(values))


def _std(values: tuple[Decimal, ...]) -> Decimal:
    if len(values) < 2:
        return ZERO
    mean = _mean(values)
    variance = sum(((value - mean) ** 2 for value in values), ZERO) / Decimal(len(values))
    return variance.sqrt() if variance > ZERO else ZERO


def _returns(closes: tuple[Decimal, ...]) -> tuple[Decimal, ...]:
    rows: list[Decimal] = []
    for previous, current in zip(closes, closes[1:]):
        if previous <= ZERO:
            raise ValueError("close must be positive")
        rows.append((current / previous) - ONE)
    return tuple(rows)


class VolatilityNormalizedMeanReversion:
    def __init__(self, *, lookback_bars: int, entry_zscore: Decimal, exit_zscore: Decimal, max_holding_bars: int) -> None:
        if lookback_bars < 20:
            raise ValueError("lookback_bars must be >= 20")
        self.lookback_bars = int(lookback_bars)
        self.entry_zscore = Decimal(entry_zscore)
        self.exit_zscore = Decimal(exit_zscore)
        self.max_holding_bars = int(max_holding_bars)
        if not ZERO < self.exit_zscore < self.entry_zscore:
            raise ValueError("require 0 < exit_zscore < entry_zscore")
        if self.max_holding_bars <= 0:
            raise ValueError("max_holding_bars must be positive")
        self._closes: deque[Decimal] = deque(maxlen=self.lookback_bars)
        self._bars_in_position = 0

    def observe(self, close: Decimal, position_quantity: Decimal):
        current_close = Decimal(close)
        prior = tuple(self._closes)
        current_sign = 1 if position_quantity > ZERO else -1 if position_quantity < ZERO else 0
        if current_sign:
            self._bars_in_position += 1
        else:
            self._bars_in_position = 0

        signal = None
        if len(prior) == self.lookback_bars:
            mean = _mean(prior)
            std = _std(prior)
            z = ZERO if std == ZERO else (current_close - mean) / std
            metrics = {"rolling_mean": str(mean), "rolling_std": str(std), "zscore": str(z)}
            if current_sign == 0:
                if z >= self.entry_zscore:
                    signal = (-1, "OVERREACTION_SHORT", metrics)
                elif z <= -self.entry_zscore:
                    signal = (1, "OVERREACTION_LONG", metrics)
            elif abs(z) <= self.exit_zscore:
                signal = (0, "MEAN_REVERSION_EXIT", metrics)
            elif self._bars_in_position >= self.max_holding_bars:
                signal = (0, "MAX_HOLD_EXIT", metrics)
        self._closes.append(current_close)
        return signal


class VolatilityCompressionBreakout:
    def __init__(
        self,
        *,
        fast_vol_bars: int,
        slow_vol_bars: int,
        compression_ratio_max: Decimal,
        breakout_lookback_bars: int,
        exit_lookback_bars: int,
        max_holding_bars: int,
    ) -> None:
        if not 20 <= fast_vol_bars < slow_vol_bars:
            raise ValueError("require 20 <= fast_vol_bars < slow_vol_bars")
        if not 2 <= exit_lookback_bars < breakout_lookback_bars <= slow_vol_bars:
            raise ValueError("invalid breakout/exit lookbacks")
        ratio = Decimal(compression_ratio_max)
        if not ZERO < ratio < ONE:
            raise ValueError("compression_ratio_max must be in (0,1)")
        if max_holding_bars <= breakout_lookback_bars:
            raise ValueError("max_holding_bars must exceed breakout lookback")
        self.fast_vol_bars = int(fast_vol_bars)
        self.slow_vol_bars = int(slow_vol_bars)
        self.compression_ratio_max = ratio
        self.breakout_lookback_bars = int(breakout_lookback_bars)
        self.exit_lookback_bars = int(exit_lookback_bars)
        self.max_holding_bars = int(max_holding_bars)
        self._closes: deque[Decimal] = deque(maxlen=self.slow_vol_bars + 1)
        self._bars_in_position = 0

    def observe(self, close: Decimal, position_quantity: Decimal):
        current_close = Decimal(close)
        prior = tuple(self._closes)
        current_sign = 1 if position_quantity > ZERO else -1 if position_quantity < ZERO else 0
        if current_sign:
            self._bars_in_position += 1
        else:
            self._bars_in_position = 0

        signal = None
        if len(prior) >= self.slow_vol_bars + 1:
            slow_returns = _returns(prior[-(self.slow_vol_bars + 1):])
            fast_returns = slow_returns[-self.fast_vol_bars:]
            slow_vol = _std(slow_returns)
            fast_vol = _std(fast_returns)
            ratio = fast_vol / slow_vol if slow_vol > ZERO else Decimal("999")
            breakout = prior[-self.breakout_lookback_bars:]
            exit_window = prior[-self.exit_lookback_bars:]
            high = max(breakout)
            low = min(breakout)
            exit_high = max(exit_window)
            exit_low = min(exit_window)
            metrics = {
                "fast_vol": str(fast_vol), "slow_vol": str(slow_vol), "compression_ratio": str(ratio),
                "breakout_high": str(high), "breakout_low": str(low),
            }
            if current_sign == 0 and ratio <= self.compression_ratio_max:
                if current_close > high:
                    signal = (1, "COMPRESSION_BREAKOUT_LONG", metrics)
                elif current_close < low:
                    signal = (-1, "COMPRESSION_BREAKOUT_SHORT", metrics)
            elif current_sign > 0 and current_close < exit_low:
                signal = (0, "VOL_BREAKOUT_EXIT_LONG", metrics)
            elif current_sign < 0 and current_close > exit_high:
                signal = (0, "VOL_BREAKOUT_EXIT_SHORT", metrics)
            elif current_sign and self._bars_in_position >= self.max_holding_bars:
                signal = (0, "MAX_HOLD_EXIT", metrics)
        self._closes.append(current_close)
        return signal


class FundingSignCarry:
    def __init__(self, *, minimum_same_sign_prints: int, max_holding_bars: int) -> None:
        if minimum_same_sign_prints < 2:
            raise ValueError("minimum_same_sign_prints must be >= 2")
        if max_holding_bars <= 0:
            raise ValueError("max_holding_bars must be positive")
        self.minimum_same_sign_prints = int(minimum_same_sign_prints)
        self.max_holding_bars = int(max_holding_bars)
        self._signs: deque[int] = deque(maxlen=self.minimum_same_sign_prints)
        self._desired_target: int | None = None
        self._bars_in_position = 0

    def on_funding(self, funding_rate: Decimal) -> None:
        rate = Decimal(funding_rate)
        sign = 1 if rate > ZERO else -1 if rate < ZERO else 0
        self._signs.append(sign)
        if len(self._signs) < self.minimum_same_sign_prints:
            return
        if sign != 0 and all(value == sign for value in self._signs):
            self._desired_target = -sign
        else:
            self._desired_target = 0

    def observe(self, close: Decimal, position_quantity: Decimal):
        del close
        current = 1 if position_quantity > ZERO else -1 if position_quantity < ZERO else 0
        if current:
            self._bars_in_position += 1
            if self._desired_target == 0 or (self._desired_target is not None and self._desired_target != current):
                return 0, "FUNDING_SIGN_EXIT", {"desired_target": str(self._desired_target)}
            if self._bars_in_position >= self.max_holding_bars:
                self._desired_target = None
                return 0, "MAX_HOLD_EXIT", {"bars_held": str(self._bars_in_position)}
            return None
        self._bars_in_position = 0
        if self._desired_target in (-1, 1):
            target = self._desired_target
            return target, "PERSISTENT_FUNDING_CARRY_ENTRY", {"desired_target": str(target)}
        return None

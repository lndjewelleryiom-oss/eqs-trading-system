from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from decimal import Decimal

ZERO = Decimal("0")
ONE = Decimal("1")


@dataclass(frozen=True, slots=True)
class BasisFeature:
    premium_close: Decimal
    mark_close: Decimal
    index_close: Decimal

    @property
    def mark_index_basis(self) -> Decimal:
        if self.index_close <= ZERO:
            raise ValueError("index_close must be positive")
        return (self.mark_close / self.index_close) - ONE


def _sign(value: Decimal) -> int:
    return 1 if value > ZERO else -1 if value < ZERO else 0


class PremiumBasisMeanReversion:
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
        self._values: deque[Decimal] = deque()
        self._sum = ZERO
        self._sumsq = ZERO
        self._bars_in_position = 0

    @staticmethod
    def _std(total: Decimal, total_sq: Decimal, count: int) -> Decimal:
        if count < 2:
            return ZERO
        n = Decimal(count)
        mean = total / n
        variance = (total_sq / n) - (mean * mean)
        return variance.sqrt() if variance > ZERO else ZERO

    def _append(self, value: Decimal) -> None:
        self._values.append(value)
        self._sum += value
        self._sumsq += value * value
        if len(self._values) > self.lookback_bars:
            removed = self._values.popleft()
            self._sum -= removed
            self._sumsq -= removed * removed

    def observe(self, feature: BasisFeature, position_quantity: Decimal):
        premium = Decimal(feature.premium_close)
        current_sign = _sign(Decimal(position_quantity))
        if current_sign:
            self._bars_in_position += 1
        else:
            self._bars_in_position = 0

        signal = None
        if len(self._values) == self.lookback_bars:
            n = Decimal(len(self._values))
            mean = self._sum / n
            std = self._std(self._sum, self._sumsq, len(self._values))
            z = ZERO if std == ZERO else (premium - mean) / std
            basis = feature.mark_index_basis
            metrics = {
                "premium_mean": str(mean),
                "premium_std": str(std),
                "premium_zscore": str(z),
                "mark_index_basis": str(basis),
            }
            if current_sign == 0:
                if z >= self.entry_zscore and basis > ZERO:
                    signal = (-1, "PREMIUM_BASIS_DISLOCATION_SHORT", metrics)
                elif z <= -self.entry_zscore and basis < ZERO:
                    signal = (1, "PREMIUM_BASIS_DISLOCATION_LONG", metrics)
            elif abs(z) <= self.exit_zscore:
                signal = (0, "PREMIUM_MEAN_REVERSION_EXIT", metrics)
            elif self._bars_in_position >= self.max_holding_bars:
                signal = (0, "MAX_HOLD_EXIT", metrics)
        self._append(premium)
        return signal


class FundingBasisConfirmedCarry:
    def __init__(self, *, minimum_same_sign_funding_prints: int, max_holding_bars: int) -> None:
        if minimum_same_sign_funding_prints < 2:
            raise ValueError("minimum_same_sign_funding_prints must be >= 2")
        if max_holding_bars <= 0:
            raise ValueError("max_holding_bars must be positive")
        self.minimum_same_sign_funding_prints = int(minimum_same_sign_funding_prints)
        self.max_holding_bars = int(max_holding_bars)
        self._funding_signs: deque[int] = deque(maxlen=self.minimum_same_sign_funding_prints)
        self._funding_sign = 0
        self._desired_target: int | None = None
        self._bars_in_position = 0

    def on_funding(self, funding_rate: Decimal) -> None:
        sign = _sign(Decimal(funding_rate))
        self._funding_signs.append(sign)
        if len(self._funding_signs) < self.minimum_same_sign_funding_prints:
            self._funding_sign = 0
            self._desired_target = None
            return
        if sign != 0 and all(value == sign for value in self._funding_signs):
            self._funding_sign = sign
            self._desired_target = -sign
        else:
            self._funding_sign = sign
            self._desired_target = 0

    def _confirmed(self, feature: BasisFeature) -> bool:
        return (
            self._funding_sign != 0
            and _sign(feature.premium_close) == self._funding_sign
            and _sign(feature.mark_index_basis) == self._funding_sign
        )

    def observe(self, feature: BasisFeature, position_quantity: Decimal):
        current = _sign(Decimal(position_quantity))
        confirmed = self._confirmed(feature)
        metrics = {
            "funding_sign": self._funding_sign,
            "premium_sign": _sign(feature.premium_close),
            "basis_sign": _sign(feature.mark_index_basis),
            "mark_index_basis": str(feature.mark_index_basis),
        }
        if current:
            self._bars_in_position += 1
            if self._desired_target != current or not confirmed:
                return 0, "FUNDING_BASIS_ALIGNMENT_EXIT", metrics
            if self._bars_in_position >= self.max_holding_bars:
                self._desired_target = None
                self._funding_sign = 0
                return 0, "MAX_HOLD_EXIT", metrics
            return None
        self._bars_in_position = 0
        if self._desired_target in (-1, 1) and confirmed:
            return self._desired_target, "FUNDING_BASIS_CONFIRMED_ENTRY", metrics
        return None

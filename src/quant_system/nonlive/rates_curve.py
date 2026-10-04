from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping

from quant_system.data.rates.models import KeyRateDv01Exposure, TreasuryCurveSnapshot
from quant_system.risk.shared03 import Shared03AssetClass, Shared03ExposureView


@dataclass(frozen=True, slots=True)
class RatesCurveValuation:
    instrument_id: str
    previous_date: str
    current_date: str
    tenor_move_bps: Mapping[str, Decimal]
    linear_dv01_pnl: Decimal
    convexity_adjustment_applied: bool
    broker_submission_enabled: bool = False


class RatesCurvePaperValuator:
    """Daily non-live rates valuation using official curve moves and key-rate DV01.

    The valuation deliberately does not fabricate a bond clean price or intraday mark.
    P&L is first-order key-rate DV01 only. Convexity is carried as required risk metadata
    but not applied until an instrument-specific pricing model is commissioned.
    """

    @staticmethod
    def validate_shared03_exposure(
        view: Shared03ExposureView,
        exposure: KeyRateDv01Exposure,
    ) -> tuple[str, ...]:
        reasons=list(view.validate())
        if view.asset_class != Shared03AssetClass.RATE:
            reasons.append("RATES_ASSET_CLASS_MISMATCH")
        if view.instrument != exposure.instrument_id:
            reasons.append("RATES_INSTRUMENT_MISMATCH")
        if view.currency != exposure.currency:
            reasons.append("RATES_CURRENCY_MISMATCH")
        attrs=view.attributes
        supplied=attrs.get("key_rate_dv01")
        if not isinstance(supplied, dict):
            reasons.append("RATES_KEY_RATE_DV01_INVALID")
        else:
            expected={k:str(v) for k,v in exposure.key_rate_dv01.items()}
            observed={str(k):str(v) for k,v in supplied.items()}
            if observed != expected:
                reasons.append("RATES_KEY_RATE_DV01_MISMATCH")
        return tuple(sorted(set(reasons)))

    def mark_to_market(
        self,
        previous: TreasuryCurveSnapshot,
        current: TreasuryCurveSnapshot,
        exposure: KeyRateDv01Exposure,
        *,
        decision_time,
    ) -> RatesCurveValuation:
        previous.assert_usable_at(decision_time)
        current.assert_usable_at(decision_time)
        if current.as_of_date <= previous.as_of_date:
            raise ValueError("current Treasury curve must be later than previous")
        moves={}
        pnl=Decimal("0")
        for tenor,dv01 in exposure.key_rate_dv01.items():
            if tenor not in previous.curve_bps or tenor not in current.curve_bps:
                raise ValueError("required key-rate tenor missing from Treasury curve: "+tenor)
            delta=current.curve_bps[tenor]-previous.curve_bps[tenor]
            moves[tenor]=delta
            pnl -= dv01*delta
        return RatesCurveValuation(
            instrument_id=exposure.instrument_id,
            previous_date=previous.as_of_date.isoformat(),
            current_date=current.as_of_date.isoformat(),
            tenor_move_bps=moves,
            linear_dv01_pnl=pnl,
            convexity_adjustment_applied=False,
            broker_submission_enabled=False,
        )

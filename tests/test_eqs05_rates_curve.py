from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json

import pytest

from quant_system.data.rates.models import KeyRateDv01Exposure, TreasuryCurveSnapshot
from quant_system.data.rates.treasury import load_treasury_curve_history
from quant_system.nonlive.rates_curve import RatesCurvePaperValuator
from quant_system.risk.shared03 import Shared03AssetClass, Shared03ExposureView

T=datetime(2026,10,4,12,0,tzinfo=timezone.utc)
RAW="a"*64


def snap(d, values, known_from=T):
    return TreasuryCurveSnapshot(
        as_of_date=date.fromisoformat(d),
        known_from=known_from,
        curve_bps={k:Decimal(str(v)) for k,v in values.items()},
        source="fixture",
        raw_sha256=RAW,
    )


def exposure():
    return KeyRateDv01Exposure(
        instrument_id="RATES:UST_BASKET",
        currency="USD",
        key_rate_dv01={"2 Yr":Decimal("20"),"10 Yr":Decimal("100")},
        duration=Decimal("7.5"),
        dv01=Decimal("120"),
        pv01=Decimal("120"),
        convexity=Decimal("0.8"),
    )


def view(exp=None, attrs=None):
    exp=exp or exposure()
    if attrs is None:
        attrs={
            "duration":str(exp.duration),
            "dv01":str(exp.dv01),
            "pv01":str(exp.pv01),
            "key_rate_dv01":{k:str(v) for k,v in exp.key_rate_dv01.items()},
            "convexity":str(exp.convexity),
        }
    return Shared03ExposureView(
        "rates-1",Shared03AssetClass.RATE,"strategy-1",exp.instrument_id,
        "UST_CURVE",Decimal("100000"),exp.currency,attrs)


def test_snapshot_is_point_in_time_bounded():
    s=snap("2026-10-02",{"10 Yr":"528"})
    s.assert_usable_at(T)
    with pytest.raises(ValueError,match="not known"):
        s.assert_usable_at(T-timedelta(seconds=1))


def test_key_rate_dv01_pnl_has_correct_sign():
    p=snap("2026-10-01",{"2 Yr":"478","10 Yr":"524"})
    c=snap("2026-10-02",{"2 Yr":"483","10 Yr":"528"})
    r=RatesCurvePaperValuator().mark_to_market(p,c,exposure(),decision_time=T)
    assert r.tenor_move_bps=={"2 Yr":Decimal("5"),"10 Yr":Decimal("4")}
    assert r.linear_dv01_pnl==Decimal("-500")
    assert r.convexity_adjustment_applied is False
    assert r.broker_submission_enabled is False


def test_falling_yields_produce_positive_long_duration_pnl():
    p=snap("2026-10-01",{"2 Yr":"483","10 Yr":"528"})
    c=snap("2026-10-02",{"2 Yr":"480","10 Yr":"523"})
    r=RatesCurvePaperValuator().mark_to_market(p,c,exposure(),decision_time=T)
    assert r.linear_dv01_pnl==Decimal("560")


def test_missing_key_rate_tenor_fails_closed():
    p=snap("2026-10-01",{"10 Yr":"524"})
    c=snap("2026-10-02",{"10 Yr":"528"})
    with pytest.raises(ValueError,match="required key-rate tenor"):
        RatesCurvePaperValuator().mark_to_market(p,c,exposure(),decision_time=T)


def test_shared03_rate_exposure_binds_key_rate_map():
    e=exposure()
    assert RatesCurvePaperValuator.validate_shared03_exposure(view(e),e)==()


def test_shared03_missing_rate_fields_fails_closed():
    e=exposure()
    reasons=RatesCurvePaperValuator.validate_shared03_exposure(view(e,attrs={}),e)
    assert "RATES_DURATION_MISSING" in reasons
    assert "RATES_KEY_RATE_DV01_INVALID" in reasons


def test_shared03_key_rate_mismatch_fails_closed():
    e=exposure()
    attrs={
        "duration":str(e.duration),"dv01":str(e.dv01),"pv01":str(e.pv01),
        "key_rate_dv01":{"10 Yr":"99"},"convexity":str(e.convexity),
    }
    reasons=RatesCurvePaperValuator.validate_shared03_exposure(view(e,attrs=attrs),e)
    assert "RATES_KEY_RATE_DV01_MISMATCH" in reasons


def test_loader_verifies_normalized_hash_and_known_from(tmp_path):
    normalized=tmp_path/"curve.json"
    seal=tmp_path/"seal.json"
    doc={
        "retrieved_at":"2026-10-04T01:00:00Z",
        "source":"US Treasury",
        "rows":[
            {"date":"2026-10-01","curve_percent":{"2 Yr":4.78,"10 Yr":5.24}},
            {"date":"2026-10-02","curve_percent":{"2 Yr":4.83,"10 Yr":5.28}},
        ],
    }
    normalized.write_text(json.dumps(doc),encoding="utf-8")
    h=hashlib.sha256(normalized.read_bytes()).hexdigest()
    seal.write_text(json.dumps({"result":"PASS","normalized_sha256":h,"raw_html_sha256":RAW}),encoding="utf-8")
    rows=load_treasury_curve_history(normalized,seal)
    assert len(rows)==2
    assert rows[-1].curve_bps["10 Yr"]==Decimal("528.0")
    assert rows[-1].known_from==datetime(2026,10,4,1,0,tzinfo=timezone.utc)


def test_loader_rejects_hash_drift(tmp_path):
    normalized=tmp_path/"curve.json"; seal=tmp_path/"seal.json"
    normalized.write_text(json.dumps({"retrieved_at":"2026-10-04T01:00:00Z","source":"x","rows":[]}),encoding="utf-8")
    seal.write_text(json.dumps({"result":"PASS","normalized_sha256":"0"*64,"raw_html_sha256":RAW}),encoding="utf-8")
    with pytest.raises(ValueError,match="hash"):
        load_treasury_curve_history(normalized,seal)

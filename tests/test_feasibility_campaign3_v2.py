from decimal import Decimal
import copy,json
from pathlib import Path
from quant_system.research.feasibility_campaign3_v2 import load_campaign3,validate_campaign3
from quant_system.research.feasibility_strategy_v2 import FixedFundingPersistenceContrarian
ROOT=Path(__file__).resolve().parents[1]
def contract(): return json.loads((ROOT/'research'/'preregistrations'/'v2'/'FEAS-BINANCE-BTC-FUNDING-003.json').read_text(encoding='utf-8'))
def test_campaign3_frozen_contract_valid():
    r=load_campaign3(ROOT); assert r['campaign']['programme_exit_rule']['campaign_number']==3
def test_campaign3_mutation_fails():
    r=copy.deepcopy(contract()); r['campaign']['strategy']['absolute_funding_entry_threshold']='0.0004'; assert 'FINGERPRINT_MISMATCH' in validate_campaign3(r)
def test_funding_signal_is_contrarian_and_delayed_until_observe():
    s=FixedFundingPersistenceContrarian(absolute_funding_entry_threshold=Decimal('0.0005'),holding_bars=3)
    s.on_funding(Decimal('0.0006')); out=s.observe(Decimal('100'),Decimal('0')); assert out[0]==-1
    s=FixedFundingPersistenceContrarian(absolute_funding_entry_threshold=Decimal('0.0005'),holding_bars=3)
    s.on_funding(Decimal('-0.0006')); out=s.observe(Decimal('100'),Decimal('0')); assert out[0]==1
def test_subthreshold_funding_does_not_trade_and_hold_exits():
    s=FixedFundingPersistenceContrarian(absolute_funding_entry_threshold=Decimal('0.0005'),holding_bars=2)
    s.on_funding(Decimal('0.0004')); assert s.observe(Decimal('100'),Decimal('0')) is None
    s.on_funding(Decimal('0.0006')); assert s.observe(Decimal('100'),Decimal('0'))[0]==-1
    assert s.observe(Decimal('100'),Decimal('-1')) is None
    out=s.observe(Decimal('100'),Decimal('-1')); assert out[0]==0 and out[1]=='FUNDING_HORIZON_EXIT'

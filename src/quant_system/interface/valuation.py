"""Read-only valuation of explicitly described, matching linear positions.

Only persisted metadata is accepted. Missing metadata never implies Binance.
This does not write marks to the trading ledger or grant trading authority.
"""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from .public_market import SYMBOLS, freshness


def decimal(value):
    if value is None or isinstance(value,bool): raise ValueError('Missing numeric value')
    try: result=Decimal(str(value))
    except InvalidOperation as exc: raise ValueError('Invalid numeric value') from exc
    if not result.is_finite(): raise ValueError('Non-finite value')
    return result


def value_portfolio(paper, markets, now=None):
    now=now or datetime.now(timezone.utc)
    result={'state':'UNAVAILABLE','currency':paper.get('currency'),'runtime_id':paper.get('runtime_id'),
        'checkpoint_at':paper.get('valuation_at'),'observed_at':now.isoformat(),'read_only':True,
        'positions':[],'unrealized_pnl':None,'gross_exposure':None,'net_exposure':None,'equity':None,
        'reason':'No verified PAPER checkpoint connected.', 'basis':'Latest persisted positions valued with current matching public marks; not a live account reconciliation.'}
    if not paper.get('available'): return result
    for p in paper.get('positions',[]):
        row={**p,'mark':None,'pnl':None,'notional':None,'valuation_state':'UNAVAILABLE','valuation_reason':'Explicit instrument metadata required.'}
        try:
            qty=decimal(p.get('qty'))
            if qty==0: continue
            contract=p.get('valuation_contract') or {}
            if contract.get('venue')!='BINANCE_USDM' or contract.get('symbol') not in SYMBOLS or contract.get('instrument_type')!='LINEAR_PERPETUAL' or contract.get('quantity_unit')!='BASE_ASSET' or contract.get('settlement_currency')!='USDT' or decimal(contract.get('multiplier'))!=1:
                raise ValueError('Verified Binance linear contract metadata with base quantity and multiplier 1 required.')
            feed=markets.get(contract['symbol'],{})
            if feed.get('source')!='BINANCE_USDM_PUBLIC' or feed.get('symbol')!=contract['symbol'] or feed.get('components',{}).get('mark',{}).get('state')!='LIVE': raise ValueError('Matching fresh mark unavailable.')
            mark=feed.get('mark',{})
            if freshness(mark.get('time'))['state']!='LIVE': raise ValueError('Mark expired or clock skew detected.')
            price=decimal(mark.get('price')); entry=decimal(p.get('avg_price'))
            if price<=0 or entry<=0: raise ValueError('Positive mark and entry required.')
            row.update(mark=float(price),pnl=float(qty*(price-entry)),notional=float(qty*price),valuation_state='VALUED',valuation_reason='Current mark applied to persisted quantity.',mark_at=mark['time'],currency='USDT')
        except (ValueError,TypeError,KeyError) as exc: row['valuation_reason']=str(exc)
        result['positions'].append(row)
    rows=result['positions']; complete=all(r['valuation_state']=='VALUED' for r in rows)
    if complete and paper.get('currency')=='USDT':
        result.update(state='VALUED_CHECKPOINT',unrealized_pnl=sum(r['pnl'] for r in rows),gross_exposure=sum(abs(r['notional']) for r in rows),net_exposure=sum(r['notional'] for r in rows),reason='All open positions valued in USDT; quantities are from the stated checkpoint. Equity requires a verified accounting model.')
    else: result.update(state='PARTIAL' if any(r['valuation_state']=='VALUED' for r in rows) else 'UNAVAILABLE',reason='Complete instrument mapping and an explicit USDT ledger currency are required for totals.')
    return result

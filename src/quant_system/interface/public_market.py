"""Bounded public observations. No credentials, accounts or order routes."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
import json
import math
from threading import Lock
import time
from urllib.request import Request, urlopen

SYMBOLS = ('BTCUSDT', 'ETHUSDT', 'SOLUSDT')
INTERVALS = ('1m', '5m', '15m', '1h')
INTERVAL_MS = {'1m':60000, '5m':300000, '15m':900000, '1h':3600000}

def fetch_json(path):
    with urlopen(Request('https://fapi.binance.com'+path, headers={'User-Agent':'EQS-ReadOnly-Observer/5'}), timeout=5) as response:
        raw = response.read(1_000_001)
        if len(raw)>1_000_000: raise ValueError('Market response exceeds read limit')
        return json.loads(raw)

def finite(value):
    n = float(value)
    if not math.isfinite(n): raise ValueError('Non-finite market value')
    return n

def positive(value):
    n = finite(value)
    if n <= 0: raise ValueError('Price must be positive')
    return n

def freshness(stamp, maximum=30):
    if stamp is None: return {'state':'UNAVAILABLE', 'age_seconds':None}
    age = (time.time()*1000-finite(stamp))/1000
    return {'state':'CLOCK_SKEW' if age < -5 else 'STALE' if age>maximum else 'LIVE', 'age_seconds':round(age,3)}

def parse_component(name, raw, interval):
    if name=='candles':
        if not isinstance(raw,list) or not raw or len(raw)>100: raise ValueError('Invalid candle count')
        rows=[]
        for r in raw:
            if not isinstance(r,list) or len(r)<11: raise ValueError('Incompatible candle')
            o,h,l,c=[positive(v) for v in r[1:5]]; v=finite(r[5]); t=int(r[0]); close=int(r[6])
            if l>min(o,c) or h<max(o,c) or h<l or v<0 or close<t: raise ValueError('Invalid OHLC/volume')
            if rows and t-rows[-1]['time']!=INTERVAL_MS[interval]: raise ValueError('Duplicate, gap or unordered candles')
            rows.append(dict(time=t,open=o,high=h,low=l,close=c,volume=v,close_time=close))
        return rows
    if name=='book':
        sides={}
        for side in ('bids','asks'):
            rows=[[positive(p),finite(q)] for p,q in raw[side]]
            if not rows or len(rows)>20 or any(q<0 for p,q in rows): raise ValueError('Invalid depth levels')
            prices=[p for p,q in rows]
            if len(set(prices))!=len(prices) or prices!=sorted(prices,reverse=side=='bids'): raise ValueError('Unordered depth')
            sides[side]=rows
        if sides['bids'][0][0]>=sides['asks'][0][0]: raise ValueError('Crossed order book')
        return {**sides,'time':int(raw.get('T',raw.get('E'))),'sequence':raw['lastUpdateId']}
    if name=='mark':
        return {'price':positive(raw['markPrice']),'time':int(raw['time']), 'funding':finite(raw['lastFundingRate']), 'next_funding_time':raw.get('nextFundingTime')}
    return {'symbol':name,'price':positive(raw['lastPrice']),'change_pct':finite(raw['priceChangePercent']), 'high':positive(raw['highPrice']),'low':positive(raw['lowPrice']), 'volume_quote':finite(raw['quoteVolume']),'time':int(raw['closeTime'])}

class PublicMarkets:
    def __init__(self):
        self.cache={}
        self.locks={key:Lock() for key in ((s,i) for s in SYMBOLS for i in INTERVALS)}

    def snapshot(self,symbol='BTCUSDT',interval='1m'):
        if symbol not in SYMBOLS or interval not in INTERVALS: raise ValueError('Unsupported market or interval')
        key=(symbol,interval)
        with self.locks[key]:
            cached=self.cache.get(key)
            if cached and time.monotonic()-cached[0]<10: return self._aged(cached[1])
            paths={'candles':f'/fapi/v1/klines?symbol={symbol}&interval={interval}&limit=100','book':f'/fapi/v1/depth?symbol={symbol}&limit=20','mark':f'/fapi/v1/premiumIndex?symbol={symbol}'}
            paths.update({s:f'/fapi/v1/ticker/24hr?symbol={s}' for s in SYMBOLS})
            previous=cached[1] if cached else {}
            components=deepcopy(previous.get('_components',{})); errors={}; received={}
            with ThreadPoolExecutor(max_workers=6) as pool:
                jobs={name:pool.submit(fetch_json,path) for name,path in paths.items()}
                for name,job in jobs.items():
                    try:
                        components[name]=parse_component(name,job.result(),interval)
                        received[name]=datetime.now(timezone.utc).isoformat()
                    except Exception as exc: errors[name]=type(exc).__name__+': '+str(exc)
            now=datetime.now(timezone.utc).isoformat()
            result={'source':'BINANCE_USDM_PUBLIC','symbol':symbol,'interval':interval,
                'fetched_at':now if received else previous.get('fetched_at',now),'last_attempt_at':now,
                'component_received_at':{**previous.get('component_received_at',{}),**received},
                '_components':components,'errors':errors,'read_only':True}
            self.cache[key]=(time.monotonic(),result)
            return self._aged(result)

    def observations(self):
        # Cache records are replaced atomically and never mutated after storage.
        result={}
        for _,record in sorted(list(self.cache.values()),key=lambda pair:pair[0]):
            result[record['symbol']]=self._aged(record)
        return result

    @staticmethod
    def _aged(result):
        data=deepcopy(result); parts=data.pop('_components',{}); candles=parts.get('candles',[])
        book=parts.get('book',{'bids':[],'asks':[]}); mark=parts.get('mark',{})
        stamps={k:(v[-1]['time'] if k=='candles' else v.get('time')) for k,v in parts.items()}
        statuses={}
        for name in ('candles','book','mark',*SYMBOLS):
            statuses[name]=freshness(stamps.get(name),INTERVAL_MS[data['interval']]/1000+30 if name=='candles' else 30)
            if name in data['errors']: statuses[name]['state']='STALE' if name in parts else 'UNAVAILABLE'
        required=[statuses[k]['state'] for k in ('candles','book','mark',data['symbol'])]
        state='UNAVAILABLE' if not candles else 'LIVE' if all(x=='LIVE' for x in required) and not data['errors'] else 'STALE' if all(x!='LIVE' for x in required) else 'PARTIAL'
        return {**data,'state':state,'candles':candles,'book':book,'mark':mark,'tickers':[parts[s] for s in SYMBOLS if s in parts], 'components':statuses,'source_age_seconds':statuses[data['symbol']]['age_seconds'],'refresh_seconds':10,'quote_currency':'USDT','order_book_scope':'20-level snapshot, not a sequence-audited L2 stream'}

class MultiVenuePublicMarkets:
    """Read-only current BTC perpetual observations aligned by selected venue."""
    VENUES = ('BYBIT_LINEAR','OKX_SWAP')
    def __init__(self):
        self.cache={}; self.locks={v:Lock() for v in self.VENUES}
    def snapshot(self,venue='BYBIT_LINEAR',symbol='BTCUSDT',interval='1m'):
        if venue not in self.VENUES: raise ValueError('Unsupported venue')
        if symbol not in ('BTCUSDT','BTC-USDT-SWAP') or interval not in INTERVALS: raise ValueError('Unsupported market or interval')
        with self.locks[venue]:
            cached=self.cache.get((venue,interval))
            if cached and time.monotonic()-cached[0]<10: return self._aged(cached[1])
            if venue=='BYBIT_LINEAR':
                iv={'1m':'1','5m':'5','15m':'15','1h':'60'}[interval]
                base='https://api.bybit.com'
                paths={'candles':f'/v5/market/kline?category=linear&symbol=BTCUSDT&interval={iv}&limit=100','book':'/v5/market/orderbook?category=linear&symbol=BTCUSDT&limit=20','ticker':'/v5/market/tickers?category=linear&symbol=BTCUSDT'}
            else:
                iv={'1m':'1m','5m':'5m','15m':'15m','1h':'1H'}[interval]
                base='https://www.okx.com'
                paths={'candles':f'/api/v5/market/candles?instId=BTC-USDT-SWAP&bar={iv}&limit=100','book':'/api/v5/market/books?instId=BTC-USDT-SWAP&sz=20','ticker':'/api/v5/market/ticker?instId=BTC-USDT-SWAP','mark':'/api/v5/public/mark-price?instType=SWAP&instId=BTC-USDT-SWAP'}
            def get(path):
                with urlopen(Request(base+path,headers={'User-Agent':'EQS-ReadOnly-Observer/5'}),timeout=5) as r:return json.loads(r.read(1_000_001))
            raw={}; errors={}
            with ThreadPoolExecutor(max_workers=4) as pool:
                jobs={k:pool.submit(get,v) for k,v in paths.items()}
                for k,j in jobs.items():
                    try: raw[k]=j.result()
                    except Exception as e: errors[k]=type(e).__name__+': '+str(e)
            try:
                if venue=='BYBIT_LINEAR':
                    kl=raw['candles']['result']['list']; candles=[dict(time=int(x[0]),open=positive(x[1]),high=positive(x[2]),low=positive(x[3]),close=positive(x[4]),volume=finite(x[5]),close_time=int(x[0])+INTERVAL_MS[interval]-1) for x in reversed(kl)]
                    ob=raw['book']['result']; book={'bids':[[positive(a),finite(b)] for a,b in ob['b']],'asks':[[positive(a),finite(b)] for a,b in ob['a']],'time':int(ob['ts']),'sequence':ob.get('u')}
                    q=raw['ticker']['result']['list'][0]; mark=positive(q.get('markPrice') or q['lastPrice']); ticker={'symbol':'BTCUSDT','price':positive(q['lastPrice']),'change_pct':finite(q.get('price24hPcnt',0))*100,'high':positive(q['highPrice24h']),'low':positive(q['lowPrice24h']),'volume_quote':finite(q.get('turnover24h',0)),'time':int(ob['ts'])}
                    display='BTCUSDT'
                else:
                    kl=raw['candles']['data']; candles=[dict(time=int(x[0]),open=positive(x[1]),high=positive(x[2]),low=positive(x[3]),close=positive(x[4]),volume=finite(x[5]),close_time=int(x[0])+INTERVAL_MS[interval]-1) for x in reversed(kl)]
                    ob=raw['book']['data'][0]; book={'bids':[[positive(x[0]),finite(x[1])] for x in ob['bids']],'asks':[[positive(x[0]),finite(x[1])] for x in ob['asks']],'time':int(ob['ts']),'sequence':ob.get('seqId')}
                    q=raw['ticker']['data'][0]; m=raw['mark']['data'][0]; mark=positive(m['markPx']); op=positive(q['open24h']); last=positive(q['last']); ticker={'symbol':'BTC-USDT-SWAP','price':last,'change_pct':(last/op-1)*100,'high':positive(q['high24h']),'low':positive(q['low24h']),'volume_quote':finite(q.get('volCcy24h',0)),'time':int(q['ts'])}; display='BTC-USDT-SWAP'
                now=datetime.now(timezone.utc).isoformat(); result={'source':venue+'_PUBLIC','venue':venue,'symbol':display,'interval':interval,'fetched_at':now,'last_attempt_at':now,'errors':errors,'read_only':True,'candles':candles,'book':book,'mark':{'price':mark,'time':book['time']},'tickers':[ticker],'quote_currency':'USDT','refresh_seconds':10,'order_book_scope':'20-level snapshot, not a sequence-audited L2 stream'}
                self.cache[(venue,interval)]=(time.monotonic(),result); return self._aged(result)
            except Exception as e: raise ValueError('Invalid '+venue+' public payload: '+str(e))
    def _aged(self,d):
        x=deepcopy(d); age=(time.time()*1000-x['mark']['time'])/1000; x['state']='LIVE' if age<=30 else 'STALE'; x['source_age_seconds']=round(age,3); x['components']={'candles':freshness(x['candles'][-1]['time'],INTERVAL_MS[x['interval']]/1000+30),'book':freshness(x['book']['time']),'mark':freshness(x['mark']['time']),x['symbol']:freshness(x['tickers'][0]['time'])}; return x

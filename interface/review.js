/* Investor review surfaces. Every missing observation remains explicit. */
titles.portfolio=['Portfolio & attribution','Portfolio','▤'];
titles.readiness=['Operating readiness','Readiness','◎'];
const compactMoney=n=>n==null?'—':new Intl.NumberFormat('en-GB',{notation:'compact',maximumFractionDigits:2}).format(n);
function currentValuation(){
 const v=state.valuation||{state:'UNAVAILABLE',positions:[],reason:'No valuation observation supplied.'};
 const age=(Date.now()-Date.parse(v.observed_at))/1000;
 const expired=v.positions?.some(p=>p.valuation_state==='VALUED'&&((Date.now()-p.mark_at)/1000>30||(Date.now()-p.mark_at)/1000 < -5));
 return !savedSnapshot&&(expired||age>30||connection==='Observer disconnected')?{...v,state:'STALE',unrealized_pnl:null,gross_exposure:null,net_exposure:null,equity:null,positions:v.positions.map(p=>({...p,mark:null,pnl:null,notional:null,valuation_state:'STALE'})),reason:'The valuation observation expired. Totals are withheld until fresh matching marks arrive.'}:v;
}
function marketContext(){const q=marketFeed?.tickers?.find(t=>t.symbol===trading.symbol),mark=marketFeed?.symbol===trading.symbol?marketFeed.mark:null;
 return `<div class="market-context"><div><span>24h high</span><b>${marketPrice(q?.high)}</b></div><div><span>24h low</span><b>${marketPrice(q?.low)}</b></div><div><span>24h turnover · USDT</span><b>${compactMoney(q?.volume_quote)}</b></div><div><span>Mark · ${componentState('mark')}</span><b>${marketPrice(mark?.price)}</b></div><div><span>Funding rate</span><b>${mark?.funding==null?'—':number(mark.funding*100,4)+'%'}</b></div><div><span>Next funding · UTC</span><b>${tickTime(mark?.next_funding_time)}</b></div></div>`;
}
positionsPanel=function(){
 const p=state.execution.paper||{},v=currentValuation(),valuations=new Map((v.positions||[]).map(r=>[r.symbol,r]));
 const rows=(p.positions||[]).filter(r=>Number(r.qty)!==0).map(r=>({...r,...valuations.get(r.symbol)}));
 register('position-pricing','Open positions & live P&L',intro('Only explicitly persisted Binance USD-M linear perpetual metadata, base-asset quantities, multiplier 1 and fresh matching marks are supported. Positions remain checkpoint observations. Current marks do not prove the account is current. Totals are withheld for incomplete mappings. Equity is unavailable until its accounting model is verified.')+kv({state:v.state,reason:v.reason,currency:v.currency,checkpoint:p.valuation_at,source:state.execution.source})+json(v));
 const tabs=`<div class="positions-tabs" role="group" aria-label="Account records">${[['positions','Positions',rows],['orders','Orders',p.orders],['fills','Fills',p.fills]].map(([key,label,data])=>`<button data-account-tab="${key}" aria-pressed="${trading.tab===key}">${label} ${p.available?'('+ (data||[]).length +')':''}</button>`).join('')}<span class="tag cyan">PAPER</span></div>`;
 let content;
 if(!p.available)content=`<div class="positions-empty"><strong>Account runtime disconnected</strong><p>Market data is live independently. Connect a compatible PAPER checkpoint to inspect positions, orders, fills and ledger balances.</p>${inspect('runtime-source','Connection details →')}</div>`;
 else if(trading.tab==='positions')content=rows.length?table('open-trades',[['symbol','Instrument'],['side','Side'],['qty','Quantity',r=>number(r.qty,6)],['avg_price','Entry',r=>marketPrice(r.avg_price)],['mark','Mark',r=>marketPrice(r.mark)],['pnl','Unrealised P&L',r=>`<span class="${r.pnl==null?'':direction(r.pnl)}">${number(r.pnl)} ${r.currency||''}</span>`],['valuation_state','Valuation',r=>tag(r.valuation_state||'UNAVAILABLE')]],rows):empty('No open positions','The verified checkpoint contains no nonzero positions.');
 else {const data=p[trading.tab]||[];content=data.length?table('account-'+trading.tab,trading.tab==='orders'?[['symbol','Instrument'],['side','Side'],['qty','Quantity'],['state','State'],['decision_time','Decision time',r=>stamp(r.decision_time)]]:[['symbol','Instrument'],['qty','Quantity'],['price','Fill'],['fee','Commission'],['timestamp','Source time',r=>stamp(r.timestamp)]],data):empty('No '+trading.tab+' in checkpoint','This is the persisted PAPER collection.');}
 return panel('PAPER / PERSISTED ACCOUNT','Open trades',tabs+content,inspect('position-pricing','P&L definition ↗'),p.available?'Checkpoint '+stamp(p.valuation_at):'Account connection required');
};
const chartOverview=overview;
overview=function(){
 let html=chartOverview();
 html=html.replace('<div class="trading-grid">',marketContext()+'<div class="trading-grid">');
 const v=currentValuation();
 html=html.replace('<b>— <small>Awaiting valuation</small></b>',`<b class="${v.unrealized_pnl==null?'':direction(v.unrealized_pnl)}">${number(v.unrealized_pnl)}<small>${v.unrealized_pnl==null?'Valuation unavailable':'USDT · checkpoint positions'}</small></b>`);
 html=html.replace('data-inspect="alert-0"','data-go="readiness"');
 return `<div class="review-toolbar"><span>MARKETS / BINANCE PERPETUALS</span><div><button data-export>Export observation</button><button data-go="readiness">Review readiness →</button></div></div>`+html;
};
function portfolio(){
 const p=state.execution.paper||{},v=currentValuation();positionsPanel();
 register('portfolio-valuation','Valuation methodology',intro(v.reason)+json(v));
 return `<div class="portfolio-banner"><div><span class="eyebrow">PAPER PORTFOLIO</span><h2>${p.available?esc(p.runtime_id):'Awaiting account connection'}</h2><p>${p.available?'Checkpoint '+stamp(p.valuation_at):'Connect the runtime to see actual account activity here.'}</p></div>${tag(v.state)}</div><div class="metrics">${metric('Ledger cash',number(p.cash),p.currency||'Currency not persisted','cash')}${metric('Unrealised P&L',number(v.unrealized_pnl),v.currency||'Complete mapping required','portfolio-valuation')}${metric('Gross exposure',number(v.gross_exposure),v.currency||'Fresh marks required','portfolio-valuation')}${metric('Net exposure',number(v.net_exposure),v.currency||'Fresh marks required','portfolio-valuation')}${metric('Commissions',number(p.commissions),'Persisted costs','pnl')}${metric('Financing',number(p.financing_costs),'Persisted costs','pnl')}</div><div class="wide-panel">${positionsPanel()}</div><div class="grid equal">${panel('PERFORMANCE','Equity & drawdown',empty('Verified equity history required','Cash is not equity. Returns, drawdown, Sharpe ratio and win rate are withheld until a verified equity history and measurement window are connected.','performance'))}${panel('ATTRIBUTION','Realised result & costs',`<div class="panel-body">${kv({realised_position_pnl:number(p.realized_pnl),commissions:number(p.commissions),financing:number(p.financing_costs),currency:p.currency||'Not persisted',window:'Cumulative checkpoint; not a daily return',strategy_attribution:'Strategy registry not connected'})}</div>`,inspect('pnl','Definitions →'))}</div>`;
}
function readiness(){
 const p=state.execution.paper||{},v=currentValuation(),prog=state.programme||{},phase=prog.phase_b||{},bounds=prog.hard_boundaries||{},admission=prog.paper_admission||{};marketEvidence();
 const pct=Number.isFinite(Number(phase.progress_fraction))?(Number(phase.progress_fraction)*100).toFixed(2)+'%':'0.00%';
 register('programme-evidence','Canonical programme evidence',json(prog));
 const programmeItems=[
 ['Programme state',prog.programme_state||'UNKNOWN','Canonical EQS-00 state generated from sealed programme evidence.','programme-evidence'],
 ['Phase-B qualification',phase.status||'UNAVAILABLE',(phase.run_id||'No run')+' · '+pct+' credited · commissioning '+(phase.commissioning_seal||'PENDING'),'programme-evidence'],
 ['PAPER admission',admission.state||'BLOCKED','Formal EQS-00 admission gate; missing evidence stays blocked.','programme-evidence'],
 ['Broker submission',bounds.broker_submission||'UNKNOWN','Hard boundary; must remain DISABLED.','programme-evidence'],
 ['LIVE authority',bounds.live_authority===false?'UNAVAILABLE':'UNKNOWN','No LIVE authority exists in the current programme state.','programme-evidence'],
 ['EQS-06 Options',bounds.eqs06_options||'UNKNOWN','Options remain frozen and excluded from PAPER promotion.','programme-evidence'],
 ...Object.entries(prog.workstreams||{}).map(([name,row])=>[name,row.state||'UNKNOWN','Evidence '+(row.evidence_id||'not yet sealed'),'programme-evidence']),
 ...(prog.asset_workstreams||[]).map(row=>[row.workstream+' · '+row.domain,row.scope_state||'UNKNOWN','PAPER readiness: '+(row.paper_readiness||'UNKNOWN'),'programme-evidence']),
 ['Policy generation',prog.policy_generation?.state||'UNKNOWN','Active generation: '+(prog.policy_generation?.generation??'None'),'programme-evidence'],
 ['Capital & Risk reservations',prog.reservations?.state||'UNKNOWN','Active reservations: '+(prog.reservations?.active_count??'Unknown'),'programme-evidence'],
 ['First PAPER run',prog.first_paper_run?.state||'NOT_STARTED','Receipt: '+(prog.first_paper_run?.receipt_sha256||'not yet available'),'programme-evidence']
 ];
 const items=[...programmeItems,
 ['Public market feed',marketIsFresh()?'LIVE':embeddedMarkets?'SAVED CAPTURE':'ATTENTION','Independent candle, book, mark and ticker freshness checks.','public-market'],
 ['Account observations',p.available?'CONNECTED':'NOT CONNECTED','Verified PAPER checkpoint required for positions and cash.','runtime-source'],
 ['Open-position valuation',v.state==='VALUED_CHECKPOINT'?'VALUED CHECKPOINT':'INCOMPLETE',v.reason,'portfolio-valuation'],
 ['Equity & performance','NOT CONNECTED','A verified equity curve, accounting model and measurement window are required.','performance'],
 ['Research evidence','PACKAGED BASELINE','Remote Bybit/OKX research work is not synchronised into this terminal.','campaigns'],
 ['Risk & reconciliation',state.risk.state||'UNKNOWN','Persisted observations only; missing limits never mean zero usage.','risk-source'],
 ['Live execution','NOT ENABLED','No broker route, order controls or live authority is exposed.','risk-source']
 ];
 register('portfolio-valuation','Valuation methodology',intro(v.reason)+json(v));
 return `<div class="review-conclusion"><span class="eyebrow">REVIEW CONCLUSION</span><h2>Market observation ready. Account commissioning incomplete.</h2><p>The terminal can inspect public markets and verified nonlive records. It cannot yet demonstrate a connected portfolio, validated performance or production trading readiness.</p></div><div class="readiness-grid">${items.map(([name,status,detail,id])=>`<article class="readiness-card"><div><h2>${esc(name)}</h2>${tag(status,/LIVE|CONNECTED|VALUED/.test(status)&&!/NOT|INCOMPLETE/.test(status)?'good':'warning')}</div><p>${esc(detail)}</p>${inspect(id,'Inspect evidence →')}</article>`).join('')}</div><div class="wide-panel">${panel('REQUIRES ATTENTION','Exceptions',exceptions(),tag(state.alerts.length+' observations','warning'))}</div>`;
}
// Export is a local evidence receipt, never a transaction or an account mutation.
function exportObservation(){const receipt={schema:'eqs-review-receipt-v1',exported_at:new Date().toISOString(),classification:embeddedMarkets?'SAVED_REVIEW':'READ_ONLY_OBSERVATION',market:marketFeed,market_failure:marketFailure,market_status:marketLabel(),operator:state,valuation:currentValuation()};const blob=new Blob([JSON.stringify(receipt,null,2)],{type:'application/json'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='EQS-observation-'+new Date().toISOString().replace(/[:.]/g,'-')+'.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);notify('Observation exported with source timestamps.');}
document.addEventListener('click',e=>{const b=e.target.closest('button');if(!b)return;
 if(b.hasAttribute('data-chart-style')){trading.style=trading.style==='candles'?'line':'candles';render();}
 if(b.hasAttribute('data-ema')){trading.ema=!trading.ema;render();}
 if(b.hasAttribute('data-expand')){document.body.classList.toggle('chart-expanded');b.textContent=document.body.classList.contains('chart-expanded')?'Close chart':'Expand';}
 if(b.dataset.accountTab){trading.tab=b.dataset.accountTab;render();}
 if(b.hasAttribute('data-export'))exportObservation();
});
document.addEventListener('keydown',e=>{if(e.key==='Escape')document.body.classList.remove('chart-expanded');if(e.key.toLowerCase()==='r'&&!['INPUT','SELECT','TEXTAREA'].includes(e.target.tagName)&&!$('#drawer').open&&!$('#palette').open)refreshMarkets();});
let hoverFrame=0;
document.addEventListener('pointermove',e=>{const svg=e.target.closest('.candlestick-chart');if(!svg||e.pointerType==='touch')return;const box=svg.getBoundingClientRect(),w=svg.viewBox.baseVal.width,xx=(e.clientX-box.left)/box.width*w,rows=visibleCandles(),i=Math.max(0,Math.min(rows.length-1,Math.floor((xx-12)/(w-97)*rows.length)));if(i===trading.point||hoverFrame)return;hoverFrame=requestAnimationFrame(()=>{hoverFrame=0;trading.point=i;renderTrading();});});
let resizeTimer;window.addEventListener('resize',()=>{clearTimeout(resizeTimer);resizeTimer=setTimeout(renderTrading,150);});
// Age visible statuses even when a slow or interrupted request has not completed.
setInterval(()=>{if(!document.hidden&&!embeddedMarkets&&state){const label=marketLabel();if(window.lastMarketLabel!==label){window.lastMarketLabel=label;renderTrading();}}},1000);
if(state)render();

'use strict';

const strategyApiBase='http://127.0.0.1:8770';
const strategyViewState={
  health:null,runs:[],selectedRunId:null,status:null,events:[],results:null,
  error:null,lastReceived:null,busy:false
};

async function strategyFetch(path){
  const response=await fetch(strategyApiBase+path,{cache:'no-store',signal:AbortSignal.timeout(5000)});
  if(!response.ok){
    let detail='HTTP '+response.status;
    try{const body=await response.json();detail=body.detail||body.error||detail;}catch{}
    throw Error(detail);
  }
  return response.json();
}

async function refreshStrategyDetail(runId){
  try{
    const [status,eventPage,results]=await Promise.all([
      strategyFetch('/strategy-runs/'+encodeURIComponent(runId)),
      strategyFetch('/strategy-runs/'+encodeURIComponent(runId)+'/events?after=0&limit=5000'),
      strategyFetch('/strategy-runs/'+encodeURIComponent(runId)+'/results')
    ]);
    strategyViewState.status=status;
    strategyViewState.events=eventPage.events||[];
    strategyViewState.results=results;
    strategyViewState.error=null;
    strategyViewState.lastReceived=new Date().toISOString();
  }catch(error){
    strategyViewState.status=null;strategyViewState.events=[];strategyViewState.results=null;
    strategyViewState.error=error.message;
  }
}

async function refreshStrategyCatalogue(loadDetail=true){
  if(strategyViewState.busy)return;
  strategyViewState.busy=true;
  try{
    const [health,catalogue]=await Promise.all([
      strategyFetch('/healthz'),strategyFetch('/strategy-runs?limit=200')
    ]);
    strategyViewState.health=health;
    strategyViewState.runs=catalogue.runs||[];
    strategyViewState.error=null;
    strategyViewState.lastReceived=new Date().toISOString();
    if(strategyViewState.selectedRunId&&!strategyViewState.runs.some(r=>r.run_id===strategyViewState.selectedRunId)){
      strategyViewState.selectedRunId=null;
    }
    if(!strategyViewState.selectedRunId&&strategyViewState.runs.length){
      strategyViewState.selectedRunId=strategyViewState.runs[0].run_id;
    }
    if(loadDetail&&strategyViewState.selectedRunId)await refreshStrategyDetail(strategyViewState.selectedRunId);
  }catch(error){
    strategyViewState.health=null;strategyViewState.error=error.message;
  }finally{
    strategyViewState.busy=false;
    if(page==='strategies'&&state)render();
  }
}

function strategyEvents(type){
  return strategyViewState.events.filter(e=>e.event_type===type);
}

function strategyChart(){
  const bars=strategyEvents('MARKET_BAR');
  const fills=strategyViewState.events.filter(e=>e.event_type==='FILL'||e.event_type==='PARTIAL_FILL');
  if(!bars.length){
    if(strategyViewState.results?.sealed_oos)return empty('Locked OOS results sealed','Operational progress is visible, but historical bars, fills and performance are withheld by the research boundary.');
    return empty('No visible market events','This run has not emitted visible MARKET_BAR events yet.');
  }
  const rows=bars.map(e=>({time:e.market_time_utc,price:Number(e.payload.close),sequence:e.sequence})).filter(r=>Number.isFinite(r.price));
  if(!rows.length)return empty('No plottable prices','Visible market events do not contain usable close prices.');
  const values=rows.map(r=>r.price),lo=Math.min(...values),hi=Math.max(...values),pad=Math.max((hi-lo)*.25,.01),min=lo-pad,max=hi+pad;
  const t0=Date.parse(rows[0].time),t1=Date.parse(rows.at(-1).time);
  const cw=window.innerWidth<=540?Math.max(280,window.innerWidth-70):810,left=cw<500?12:55,right=cw-75;
  const x=t=>left+(Date.parse(t)-t0)/Math.max(t1-t0,1)*(right-left),y=v=>178-(v-min)/(max-min)*145;
  const points=rows.map(r=>`${x(r.time)},${y(r.price)}`).join(' ');
  const fillMarkers=fills.map(f=>{
    const px=Number(f.payload.price);if(!Number.isFinite(px)||!f.market_time_utc)return '';
    return `<circle cx="${x(f.market_time_utc)}" cy="${y(px)}" r="5" class="strategy-fill-marker"><title>${esc(f.payload.side+' '+f.payload.quantity+' @ '+f.payload.price+' · SIMULATED')}</title></circle>`;
  }).join('');
  return `<div class="chart-wrap"><svg class="chart" viewBox="0 0 ${cw} 215" role="img" aria-label="Historical replay price chart with simulated fill markers" preserveAspectRatio="none">
    ${[0,.25,.5,.75,1].map(t=>{const value=min+(max-min)*t,yy=y(value);return `<line class="gridline" x1="${left}" y1="${yy}" x2="${right}" y2="${yy}"/><text x="${right+8}" y="${yy+4}">${value.toFixed(2)}</text>`;}).join('')}
    <polyline points="${points}" fill="none" stroke="#69c7e8" stroke-width="2" vector-effect="non-scaling-stroke"/>${fillMarkers}
    <text x="${left}" y="205">${shortTime(rows[0].time)}</text><text x="${right-85}" y="205">${shortTime(rows.at(-1).time)}</text>
  </svg></div><div class="chart-inspect">Historical source time · ${rows.length} bars · ${fills.length} simulated fill events</div>`;
}

function strategyLatestProgress(){
  const rows=strategyEvents('RUN_PROGRESS');
  return rows.length?rows.at(-1).payload:{};
}

function strategyRunDetail(){
  const results=strategyViewState.results,status=strategyViewState.status;
  if(!strategyViewState.selectedRunId)return empty('Select a strategy run','Choose a visible run from the catalogue.');
  if(strategyViewState.error)return empty('Run detail unavailable',strategyViewState.error);
  if(!results||!status)return empty('Loading strategy run','Waiting for the research service response.');
  const run=results.run||{};
  const progress=strategyLatestProgress();
  const equity=results.equity||[];
  const latestEquity=equity.length?equity.at(-1).payload:{};
  const equityValues=equity.map(e=>Number(e.payload.equity)).filter(Number.isFinite);
  let peak=-Infinity,maxDrawdown=0;
  for(const value of equityValues){peak=Math.max(peak,value);if(Number.isFinite(peak)&&peak!==0)maxDrawdown=Math.max(maxDrawdown,(peak-value)/Math.abs(peak));}
  const blocked=strategyViewState.events.filter(e=>['RUN_BLOCKED','RUN_FAILED'].includes(e.event_type));
  const fillRows=(results.fills||[]).map(e=>({
    time:e.market_time_utc,side:e.payload.side,quantity:e.payload.quantity,price:e.payload.price,
    commission:e.payload.commission,provenance:e.payload.fill_provenance,order_id:e.payload.order_id
  }));
  const tradeRows=(results.trades||[]).map(e=>({
    time:e.market_time_utc,symbol:e.payload.symbol,realized_pnl:e.payload.realized_pnl,
    forward_gate:e.payload.counts_toward_forward_gate===false?'NO':'UNKNOWN'
  }));
  register('selected-strategy-run','Strategy run · '+run.run_id,intro('Read-only strategy evidence. Historical replay fills are simulated and cannot count toward the forward autonomy gate.')+json({run,status,progress,blockers:blocked}));
  const identity=panel('RUN IDENTITY',run.run_id||strategyViewState.selectedRunId,
    `<div class="panel-body">${kv({
      strategy:run.strategy_id,version:run.strategy_version,mode:run.mode,status:status.job_status,
      result:status.evidence_result??'Withheld / not final',source:run.source_id,instrument:run.instrument_id,
      fill_provenance:run.fill_provenance,broker_submission:status.broker_submission_enabled,
      live_authority:status.live_authority,dataset_hash:run.dataset_hash||'Fixture / not supplied'
    })}</div>`,inspect('selected-strategy-run','Full run record ↗'));
  const progressPanel=panel('TEST PROGRESS','Historical replay',
    `<div class="panel-body">${kv({
      completed:progress.completed_events??'—',total:progress.total_events??'—',
      progress:progress.progress_fraction===undefined?'—':number(progress.progress_fraction*100,1)+'%',
      historical_market_time:progress.current_historical_market_time||'—',
      last_event_sequence:status.last_sequence,event_count:status.event_count,
      worker_state:status.worker_state||'Not active',screen_received:strategyViewState.lastReceived||'—'
    })}</div>`,tag(status.job_status||'UNKNOWN'));
  const performance=panel('PERFORMANCE','Simulated economic account',
    results.sealed_oos?intro('Locked OOS performance is sealed by policy.'):
    `<div class="metrics">${metric('Equity',number(latestEquity.equity),'Simulated / latest event','selected-strategy-run')}${metric('Realised P&L',number(latestEquity.realized_pnl),'Simulated','selected-strategy-run')}${metric('Unrealised P&L',number(latestEquity.unrealized_pnl),'Simulated','selected-strategy-run')}${metric('Commissions',number(latestEquity.commissions),'Modelled cost','selected-strategy-run')}${metric('Max drawdown',number(maxDrawdown*100,2)+'%','Visible equity events','selected-strategy-run')}${metric('Economic trades',String((results.trades||[]).length),'Historical only','selected-strategy-run')}</div>`);
  const diagnostics=panel('DIAGNOSTICS','Failures & blockers',
    blocked.length?blocked.map(e=>`<div class="exception warning"><code>${esc(e.event_type)}</code><p>${esc(e.payload.reason||e.payload.detail||'No reason supplied')}</p></div>`).join(''):
    intro('No RUN_BLOCKED or RUN_FAILED event is visible for this run.'),tag(blocked.length?'ATTENTION':'CLEAR'));
  const orders=panel('ORDERS & FILLS','Simulation ledger',
    fillRows.length?table('strategy-fills',[['time','Time',r=>stamp(r.time)],['side','Side'],['quantity','Qty'],['price','Price'],['commission','Fee'],['provenance','Provenance'],['order_id','Order ID']],fillRows,{search:true}):empty('No visible fills','No simulated fill has been recorded for this run.'));
  const trades=panel('CLOSED TRADES','Economic trade history',
    tradeRows.length?table('strategy-trades',[['time','Time',r=>stamp(r.time)],['symbol','Instrument'],['realized_pnl','Realised P&L'],['forward_gate','Counts toward forward gate']],tradeRows):empty('No closed trades','No completed economic trade is visible.'));
  return `<div class="grid equal">${identity}${progressPanel}</div><div class="wide-panel">${panel('PRICE / SIGNAL / FILL VIEW','Inspectable run chart',strategyChart(),tag(run.mode||'REPLAY','cyan'))}</div><div class="wide-panel">${performance}</div><div class="grid equal">${orders}${trades}</div><div class="wide-panel">${diagnostics}</div>`;
}

strategies=function(){
  const health=strategyViewState.health;
  const runs=strategyViewState.runs||[];
  const stateText=strategyViewState.error?'UNAVAILABLE':health?.storage_state||'CONNECTED';
  const healthPanel=panel('J45–J51 / STRATEGY VIEW','Research run service',
    `<div class="metrics">${metric('API',strategyViewState.error?'Unavailable':'Connected',strategyViewState.error||'Localhost research service','strategy-service')}${metric('Storage',friendly(stateText),health?.allow_new_research?'New research permitted':'New heavy research blocked','strategy-service')}${metric('Visible runs',String(runs.length),'Rejected/blocked/finished retained','strategy-service')}${metric('Engine',health?.engine||'EQS_NATIVE','Native EQS first','strategy-service')}${metric('Broker submission',health?.broker_submission_enabled?'ENABLED':'DISABLED','Separate authority required','strategy-service')}${metric('LIVE authority',health?.live_authority?'TRUE':'FALSE','F7 remains separate','strategy-service')}</div>`,
    tag(strategyViewState.error?'UNAVAILABLE':stateText,strategyViewState.error?'warning':undefined));
  register('strategy-service','J51 research service',intro('Local bounded research service. It cannot load broker connectors or grant LIVE authority.')+json({health,error:strategyViewState.error,last_received:strategyViewState.lastReceived}));
  const catalogue=runs.length?table('strategy-catalogue',[
    ['run_id','Run',r=>`<button class="link-button" data-run="${esc(r.run_id)}">${esc(r.run_id)}</button>`],
    ['strategy_id','Strategy'],['mode','Mode',r=>tag(r.mode,'cyan')],['status','Operational',r=>tag(r.status)],
    ['result','Research result',r=>r.result?tag(r.result):'—'],['source_id','Source'],['instrument_id','Instrument'],
    ['event_count','Events'],['updated_at','Updated',r=>stamp(r.updated_at)]
  ],runs,{search:true,filter:{key:'status',label:'states'}}):
  empty('No research runs recorded',strategyViewState.error||'The catalogue is connected but no run records exist yet. Storage backpressure currently prevents a new production-path replay.');
  return `<div class="wide-panel">${healthPanel}</div><div class="wide-panel">${panel('STRATEGY CATALOGUE','All visible research/test runs',catalogue,tag(runs.length+' runs','cyan'),'<span>Viewing does not require profitability approval</span>')}</div><div class="subheading"><h2>Selected strategy run</h2></div>${strategyRunDetail()}`;
};

const originalDashboardRefresh=refresh;
refresh=async function(){
  await originalDashboardRefresh();
  if(!savedSnapshot)await refreshStrategyCatalogue(page==='strategies');
};

document.addEventListener('click',async event=>{
  const button=event.target.closest('button[data-run]');
  if(!button)return;
  strategyViewState.selectedRunId=button.dataset.run;
  await refreshStrategyDetail(strategyViewState.selectedRunId);
  if(page==='strategies')render();
});

setInterval(()=>{
  if(!document.hidden&&!savedSnapshot&&page==='strategies'){
    refreshStrategyCatalogue(true);
  }
},2000);

if(!savedSnapshot)refreshStrategyCatalogue(false);

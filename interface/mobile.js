const byId=id=>document.getElementById(id);
const esc=value=>String(value??'').replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
const stateClass=value=>{const v=String(value||'').toUpperCase();if(/CRITICAL|HALT|FAIL|BREACH|REJECT/.test(v))return'critical';if(/WARNING|ATTENTION|STALE|BLOCK|PAUSE|UNKNOWN|UNAVAILABLE/.test(v))return'warning';if(/PASS|ACTIVE|HEALTHY|RECENT|CONNECTED|READY/.test(v))return'good';return''};
const row=(title,state,detail)=>`<article class="row"><div class="row-head"><strong>${esc(title)}</strong><span class="state ${stateClass(state)}">${esc(state||'—')}</span></div>${detail?`<p>${esc(detail)}</p>`:''}</article>`;

function compact(value,max=42){const s=String(value??'—');return s.length>max?s.slice(0,max-1)+'…':s}
function formatTime(value){if(!value)return'—';const d=new Date(value);return Number.isNaN(d.getTime())?String(value):d.toLocaleString([], {day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit',second:'2-digit'});}

function render(snapshot){
  const meta=snapshot.meta||{};
  const strategies=snapshot.strategies||{};
  const performance=snapshot.performance||{};
  const managed=performance.managed_acceptance||{};
  const runtimes=((snapshot.jobs||{}).runtimes)||[];
  const alerts=snapshot.alerts||[];

  byId('health').textContent=snapshot.health||'UNKNOWN';
  byId('health').className=stateClass(snapshot.health);
  byId('generated').textContent=`Updated ${formatTime(meta.generated_at)} · ${meta.environment||'Observer'} · ${meta.mode||'PAPER / SHADOW'}`;
  byId('runtime').textContent=meta.runtime_connected?'CONNECTED':'DISCONNECTED';
  byId('runtime').className=stateClass(meta.runtime_connected?'CONNECTED':'UNAVAILABLE');
  byId('runtime-detail').textContent=`${runtimes.length} runtime${runtimes.length===1?'':'s'} observed`;
  byId('strategies').textContent=String(strategies.strategy_count??0);
  byId('strategy-state').textContent=strategies.state||'UNAVAILABLE';
  byId('next-action').textContent=compact(strategies.next_valid_action||'DISCOVERY / WAIT');
  byId('paper-status').textContent=managed.status||'UNAVAILABLE';
  const hours=Number(managed.elapsed_hours||0),required=Number(managed.required_hours||0),trades=Number(managed.attributed_trades||0),requiredTrades=Number(managed.required_attributed_trades||0);
  byId('paper-progress').textContent=required?`${hours.toFixed(1)}/${required.toFixed(0)}h · ${trades}/${requiredTrades} trades`:`${trades} attributed trades`;

  byId('alert-count').textContent=String(alerts.length);
  byId('alerts').innerHTML=alerts.length?alerts.slice(0,12).map(a=>row(a.title||a.code,a.severity,a.detail)).join(''):'<p class="empty">No current alerts.</p>';

  const strategyRows=strategies.strategies||[];
  byId('strategy-count').textContent=String(strategyRows.length);
  byId('strategy-list').innerHTML=strategyRows.length?strategyRows.slice(0,12).map(s=>row(s.strategy_id||s.lineage_id||s.family||'Strategy',s.state,s.family||s.detail||s.reason||'')).join(''):'<p class="empty">No strategies currently published to the lifecycle.</p>';

  byId('runtime-list').innerHTML=runtimes.length?runtimes.slice(0,10).map(r=>row(`${r.mode||'runtime'} · ${r.runtime_id||'unknown'}`,r.status||r.freshness,`Heartbeat ${formatTime(r.last_heartbeat_at)} · freshness ${r.freshness||'UNKNOWN'} · lease ${r.lease_state||'UNKNOWN'}`)).join(''):'<p class="empty">No live runtime records are available.</p>';
  byId('offline').hidden=true;
}

async function refresh(){
  const button=byId('refresh');button.disabled=true;
  try{
    const response=await fetch('/api/dashboard',{cache:'no-store',headers:{Accept:'application/json'}});
    if(!response.ok)throw new Error(`HTTP ${response.status}`);
    render(await response.json());
  }catch(error){
    byId('offline').hidden=false;
    byId('offline').textContent=`Observer unavailable: ${error.message}. Showing the last rendered state.`;
  }finally{button.disabled=false;}
}

byId('refresh').addEventListener('click',refresh);
document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});
refresh();
setInterval(()=>{if(!document.hidden)refresh();},15000);

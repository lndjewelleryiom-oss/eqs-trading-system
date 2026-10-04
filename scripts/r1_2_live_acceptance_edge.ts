import "jsr:@supabase/functions-js/edge-runtime.d.ts";

const TOKEN = Deno.env.get("EQS_R12_EPHEMERAL_TOKEN") ?? "";
const enc = new TextEncoder();
const sleep = (ms:number) => new Promise(r => setTimeout(r, ms));
const now = () => Date.now();

type Seen = {recv:number, data:any};

async function serverClock(url:string, extract:(x:any)=>number) {
  const t0=now(); const r=await fetch(url,{headers:{"user-agent":"eqs-r12-readonly/1"}}); const j=await r.json(); const t1=now();
  if(!r.ok) throw new Error(`clock ${url} HTTP ${r.status}`);
  const s=extract(j); const midpoint=(t0+t1)/2;
  return {server_ms:s, local_midpoint_ms:midpoint, offset_server_minus_local_ms:s-midpoint, rtt_ms:t1-t0};
}

async function getJson(url:string){
  const r=await fetch(url,{headers:{"user-agent":"eqs-r12-readonly/1"}}); const text=await r.text();
  if(!r.ok) throw new Error(`GET ${url} -> ${r.status}: ${text.slice(0,200)}`);
  return JSON.parse(text);
}

async function wsCollect(url:string, subscribe:any|null, durationMs:number, maxMessages=500):Promise<{messages:Seen[], open_ms:number, error:string|null}> {
  return await new Promise((resolve) => {
    const start=now(); const messages:Seen[]=[]; let opened=0; let error:string|null=null; let done=false;
    const ws=new WebSocket(url);
    const finish=()=>{ if(done)return; done=true; try{ws.close(1000,"acceptance complete")}catch{}; resolve({messages,open_ms:opened?opened-start:-1,error}); };
    const timer=setTimeout(finish,durationMs);
    ws.onopen=()=>{opened=now(); if(subscribe) ws.send(JSON.stringify(subscribe));};
    ws.onmessage=(ev)=>{ const recv=now(); try{ const d=JSON.parse(typeof ev.data==="string"?ev.data:new TextDecoder().decode(ev.data)); messages.push({recv,data:d}); }catch(e){ error=`parse:${e}`; } if(messages.length>=maxMessages){clearTimeout(timer);finish();} };
    ws.onerror=()=>{error=error||"websocket error";};
    ws.onclose=(ev)=>{ if(!done && ev.code!==1000){ error=error||`closed:${ev.code}:${ev.reason}`; } };
  });
}

function pct(xs:number[], p:number){ if(!xs.length)return null; const a=[...xs].sort((x,y)=>x-y); const i=Math.min(a.length-1,Math.max(0,Math.floor((a.length-1)*p))); return a[i]; }
function latencySummary(samples:number[]){return {n:samples.length,min_ms:samples.length?Math.min(...samples):null,p50_ms:pct(samples,.5),p95_ms:pct(samples,.95),max_ms:samples.length?Math.max(...samples):null};}
function applyLevels(book:Map<number,number>, rows:any[]){ for(const r of rows||[]){const p=Number(r[0]),q=Number(r[1]); if(q===0)book.delete(p);else book.set(p,q);} }
function crossed(b:Map<number,number>,a:Map<number,number>){ if(!b.size||!a.size)return false; return Math.max(...b.keys())>=Math.min(...a.keys()); }

async function binanceBookSession(){
  const url="wss://fstream.binance.com/public/ws/btcusdt@depth@100ms";
  let ws:WebSocket|null=null; const events:Seen[]=[]; let err:string|null=null; const start=now();
  const opened = await new Promise<boolean>((resolve)=>{ ws=new WebSocket(url); const to=setTimeout(()=>resolve(false),5000); ws!.onopen=()=>{clearTimeout(to);resolve(true)}; ws!.onmessage=(ev)=>{try{events.push({recv:now(),data:JSON.parse(ev.data as string)})}catch{}}; ws!.onerror=()=>{err="websocket error"}; });
  if(!opened){try{ws?.close()}catch{}; throw new Error("Binance depth open timeout");}
  await sleep(700);
  const snap=await getJson("https://fapi.binance.com/fapi/v1/depth?symbol=BTCUSDT&limit=1000");
  await sleep(1300); try{ws?.close(1000,"forced reconnect test")}catch{}
  const last=Number(snap.lastUpdateId); const usable=events.map(x=>x.data).filter((x:any)=>x.e==="depthUpdate" && Number(x.u)>=last);
  let bridge=-1; for(let i=0;i<usable.length;i++){if(Number(usable[i].U)<=last && Number(usable[i].u)>=last){bridge=i;break;}}
  const bids=new Map<number,number>(), asks=new Map<number,number>(); applyLevels(bids,snap.bids);applyLevels(asks,snap.asks);
  let continuity=true, applied=0, prev:number|null=null;
  if(bridge>=0){ for(const e of usable.slice(bridge)){ if(prev!==null && e.pu!==undefined && Number(e.pu)!==prev){continuity=false;break;} applyLevels(bids,e.b);applyLevels(asks,e.a);prev=Number(e.u);applied++; } }
  return {event_count:events.length,snapshot_last_update_id:last,bridge_found:bridge>=0,continuity,applied_updates:applied,crossed_book:crossed(bids,asks),error:err,duration_ms:now()-start};
}

async function binance(){
  const clock=await serverClock("https://fapi.binance.com/fapi/v1/time",j=>Number(j.serverTime));
  const first=await binanceBookSession(); const second=await binanceBookSession();
  const market=await wsCollect("wss://fstream.binance.com/market/stream?streams=btcusdt@aggTrade/btcusdt@markPrice@1s/btcusdt@forceOrder",null,5000,250);
  const channelCounts:any={aggTrade:0,markPriceUpdate:0,forceOrder:0}; const lats:number[]=[]; const ids:string[]=[];
  for(const m of market.messages){const d=m.data.data??m.data; const e=d.e; if(e in channelCounts)channelCounts[e]++; if(e==="aggTrade"){ids.push(String(d.a)); const ts=Number(d.E??d.T); lats.push(m.recv+clock.offset_server_minus_local_ms-ts);} else if(e==="markPriceUpdate"){lats.push(m.recv+clock.offset_server_minus_local_ms-Number(d.E));}}
  const rest=await getJson("https://fapi.binance.com/fapi/v1/aggTrades?symbol=BTCUSDT&limit=1000"); const set=new Set(rest.map((x:any)=>String(x.a))); const overlap=ids.filter(x=>set.has(x));
  return {venue:"BINANCE_USDM",clock,book_initial:first,book_reconnect:second,reconnect_pass:first.bridge_found&&first.continuity&&!first.crossed_book&&second.bridge_found&&second.continuity&&!second.crossed_book,channels:channelCounts,required_channels_pass:channelCounts.aggTrade>0&&channelCounts.markPriceUpdate>0,optional_force_order_seen:channelCounts.forceOrder>0,trade_ws_count:ids.length,rest_trade_count:rest.length,trade_id_overlap_count:overlap.length,backfill_reconcile_pass:overlap.length>0,latency_adjusted:latencySummary(lats),ws_error:market.error};
}

async function bybitBookSession(){
  const x=await wsCollect("wss://stream.bybit.com/v5/public/linear",{op:"subscribe",args:["orderbook.50.BTCUSDT"]},3500,180);
  const msgs=x.messages.map(m=>m.data).filter((d:any)=>d.topic==="orderbook.50.BTCUSDT"); let snapshot=-1; for(let i=0;i<msgs.length;i++)if(msgs[i].type==="snapshot"){snapshot=i;break;}
  const bids=new Map<number,number>(),asks=new Map<number,number>(); let monotonic=true,last:number|null=null,applied=0;
  if(snapshot>=0){const s=msgs[snapshot].data;applyLevels(bids,s.b);applyLevels(asks,s.a);last=Number(s.u); for(const d of msgs.slice(snapshot+1)){if(d.type==="snapshot"){bids.clear();asks.clear();applyLevels(bids,d.data.b);applyLevels(asks,d.data.a);last=Number(d.data.u);continue;} const u=Number(d.data.u); if(last!==null&&u<=last){monotonic=false;continue;} applyLevels(bids,d.data.b);applyLevels(asks,d.data.a);last=u;applied++;}}
  return {message_count:msgs.length,snapshot_first:snapshot===0,snapshot_seen:snapshot>=0,monotonic,applied_updates:applied,crossed_book:crossed(bids,asks),error:x.error};
}

async function bybit(){
 const clock=await serverClock("https://api.bybit.com/v5/market/time",j=>Number(j.result.timeNano)/1e6);
 const first=await bybitBookSession(); const second=await bybitBookSession();
 const x=await wsCollect("wss://stream.bybit.com/v5/public/linear",{op:"subscribe",args:["publicTrade.BTCUSDT","tickers.BTCUSDT","allLiquidation.BTCUSDT"]},5000,250);
 const c:any={publicTrade:0,tickers:0,allLiquidation:0};const ids:string[]=[];const lats:number[]=[];
 for(const m of x.messages){const d=m.data; if(typeof d.topic!=="string")continue; const k=d.topic.split(".")[0]; if(k in c)c[k]++; if(k==="publicTrade")for(const t of d.data||[]){ids.push(String(t.i));lats.push(m.recv+clock.offset_server_minus_local_ms-Number(t.T));} else if(k==="tickers")lats.push(m.recv+clock.offset_server_minus_local_ms-Number(d.ts));}
 const rest=await getJson("https://api.bybit.com/v5/market/recent-trade?category=linear&symbol=BTCUSDT&limit=1000"); const rows=rest.result?.list||[]; const set=new Set(rows.map((r:any)=>String(r.execId))); const overlap=ids.filter(x=>set.has(x));
 return {venue:"BYBIT_LINEAR",clock,book_initial:first,book_reconnect:second,reconnect_pass:first.snapshot_seen&&!first.crossed_book&&second.snapshot_seen&&!second.crossed_book,channels:c,required_channels_pass:c.publicTrade>0&&c.tickers>0,optional_liquidation_seen:c.allLiquidation>0,trade_ws_count:ids.length,rest_trade_count:rows.length,trade_id_overlap_count:overlap.length,backfill_reconcile_pass:overlap.length>0,latency_adjusted:latencySummary(lats),ws_error:x.error};
}

async function okxBookSession(){
 const x=await wsCollect("wss://ws.okx.com:8443/ws/v5/public",{op:"subscribe",args:[{channel:"books",instId:"BTC-USDT-SWAP"}]},4000,180);
 const msgs=x.messages.map(m=>m.data).filter((d:any)=>d.arg?.channel==="books"&&d.data?.length); let snapshot=-1; for(let i=0;i<msgs.length;i++)if(msgs[i].action==="snapshot"){snapshot=i;break;}
 const bids=new Map<number,number>(),asks=new Map<number,number>();let continuity=true,last:number|null=null,applied=0;
 if(snapshot>=0){const s=msgs[snapshot].data[0];applyLevels(bids,s.bids);applyLevels(asks,s.asks);last=Number(s.seqId);for(const d of msgs.slice(snapshot+1)){for(const it of d.data||[]){if(d.action==="snapshot"){bids.clear();asks.clear();applyLevels(bids,it.bids);applyLevels(asks,it.asks);last=Number(it.seqId);continue;} if(last!==null&&Number(it.prevSeqId)!==last){continuity=false;continue;} applyLevels(bids,it.bids);applyLevels(asks,it.asks);last=Number(it.seqId);applied++;}}}
 return {message_count:msgs.length,snapshot_first:snapshot===0,snapshot_seen:snapshot>=0,continuity,applied_updates:applied,crossed_book:crossed(bids,asks),error:x.error};
}

async function okx(){
 const clock=await serverClock("https://www.okx.com/api/v5/public/time",j=>Number(j.data[0].ts));
 const first=await okxBookSession(); const second=await okxBookSession();
 const args=["trades","mark-price","funding-rate","open-interest"].map(channel=>({channel,instId:"BTC-USDT-SWAP"}));
 const x=await wsCollect("wss://ws.okx.com:8443/ws/v5/public",{op:"subscribe",args},5500,250);
 const c:any={trades:0,"mark-price":0,"funding-rate":0,"open-interest":0}; const ids:string[]=[]; const lats:number[]=[]; const errors:any[]=[];
 for(const m of x.messages){const d=m.data;if(d.event==="error")errors.push(d);const ch=d.arg?.channel;if(!(ch in c))continue;c[ch]++;for(const it of d.data||[]){const ts=Number(it.ts??it.fundingTime);if(Number.isFinite(ts))lats.push(m.recv+clock.offset_server_minus_local_ms-ts);if(ch==="trades"&&it.tradeId)ids.push(String(it.tradeId));}}
 const rest=await getJson("https://www.okx.com/api/v5/market/trades?instId=BTC-USDT-SWAP&limit=500"); const rows=rest.data||[];const set=new Set(rows.map((r:any)=>String(r.tradeId)));const overlap=ids.filter(x=>set.has(x));
 return {venue:"OKX_SWAP",clock,book_initial:first,book_reconnect:second,reconnect_pass:first.snapshot_seen&&first.continuity&&!first.crossed_book&&second.snapshot_seen&&second.continuity&&!second.crossed_book,channels:c,required_channels_pass:c.trades>0&&c["mark-price"]>0&&c["funding-rate"]>0&&c["open-interest"]>0,subscription_errors:errors,trade_ws_count:ids.length,rest_trade_count:rows.length,trade_id_overlap_count:overlap.length,backfill_reconcile_pass:overlap.length>0,latency_adjusted:latencySummary(lats),ws_error:x.error};
}

Deno.serve(async (req)=>{
 const u=new URL(req.url); if(u.searchParams.get("token")!==TOKEN)return new Response("unauthorized",{status:401});
 const started=new Date().toISOString(); const settled=await Promise.allSettled([binance(),bybit(),okx()]);
 const venues=settled.map((r,i)=>r.status==="fulfilled"?r.value:{venue:["BINANCE_USDM","BYBIT_LINEAR","OKX_SWAP"][i],fatal_error:String(r.reason)});
 const allCore=venues.every((v:any)=>v.reconnect_pass===true&&v.required_channels_pass===true&&v.backfill_reconcile_pass===true&&v.latency_adjusted?.n>0);
 const result={test_data:false,read_only:true,symbols:["BTCUSDT","BTC-USDT-SWAP"],started_at:started,completed_at:new Date().toISOString(),venues,short_soak_core_pass:allCore,limitations:["This is a short live acceptance run, not the multi-hour/day soak required for full R1.2 completion.","Sparse liquidation channels are observed opportunistically and are not required to emit during the short window."]};
 return new Response(JSON.stringify(result),{headers:{"content-type":"application/json","cache-control":"no-store"}});
});

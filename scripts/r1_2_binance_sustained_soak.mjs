import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { fetchWithBackoff } from './r1_2_retry_backoff.mjs';

const mode = process.argv[2] || 'probe';
const root = process.env.EQS_SOAK_DIR || process.cwd();
const rawPath = path.join(root, 'binance_raw.jsonl');
const checkpointPath = path.join(root, 'binance_hourly_checkpoints.jsonl');
const statePath = path.join(root, 'binance_soak_state.json');
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const now = () => Date.now();
const startedAt = new Date();
let cycleCount = 0;

const counters = {
  socket_frames_received: 0,
  raw_records_persisted: 0,
  parse_failures: 0,
  persistence_failures: 0,
  sequence_gaps: 0,
  forced_disconnects: 0,
  reconnects: 0,
  resynchronizations: 0,
  rest_requests: 0,
  rate_limit_responses: 0,
  rest_retries: 0,
  backoff_ms: 0,
  graceful_rotations: 0,
};

function sha256(text) {
  return crypto.createHash('sha256').update(text).digest('hex');
}

function persistRaw(raw, receivedAt) {
  counters.socket_frames_received += 1;
  try {
    const row = {
      venue: 'BINANCE_USDM',
      channel: 'depth',
      received_at: new Date(receivedAt).toISOString(),
      raw_sha256: sha256(raw),
      size_bytes: Buffer.byteLength(raw),
      raw_b64: Buffer.from(raw).toString('base64'),
    };
    fs.appendFileSync(rawPath, `${JSON.stringify(row)}\n`, { encoding: 'utf8', flush: true });
    counters.raw_records_persisted += 1;
  } catch {
    counters.persistence_failures += 1;
  }
}

function applyLevels(book, rows) {
  for (const row of rows || []) {
    const price = Number(row[0]);
    const qty = Number(row[1]);
    if (qty === 0) book.delete(price);
    else book.set(price, qty);
  }
}

function crossed(bids, asks) {
  if (!bids.size || !asks.size) return false;
  return Math.max(...bids.keys()) >= Math.min(...asks.keys());
}

async function fetchSnapshot() {
  const response = await fetchWithBackoff('https://fapi.binance.com/fapi/v1/depth?symbol=BTCUSDT&limit=1000', {
    maxAttempts: 5,
    onAttempt: () => { counters.rest_requests += 1; },
    onResponse: ({ status }) => { if (status === 429) counters.rate_limit_responses += 1; },
    onRetry: ({ delayMs }) => { counters.rest_retries += 1; counters.backoff_ms += delayMs; },
  });
  if (!response.ok) throw new Error(`snapshot HTTP ${response.status}`);
  return response.json();
}

async function openBufferedCycle(liveMs = 8000, forceClose = true) {
  const events = [];
  let wsError = null;
  let ws;
  await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error('websocket open timeout')), 8000);
    ws = new WebSocket('wss://fstream.binance.com/public/ws/btcusdt@depth@100ms');
    ws.onopen = () => { clearTimeout(timeout); resolve(); };
    ws.onerror = () => { wsError = 'websocket_error'; };
    ws.onmessage = event => {
      const receivedAt = now();
      const raw = String(event.data);
      persistRaw(raw, receivedAt);
      try { events.push({ receivedAt, data: JSON.parse(raw) }); }
      catch { counters.parse_failures += 1; }
    };
  });

  // Critical ordering invariant: the socket remains open while the REST snapshot is requested.
  await sleep(700);
  const snapshotRequestedAt = now();
  const snapshot = await fetchSnapshot();
  const snapshotReceivedAt = now();
  await sleep(liveMs);

  if (forceClose) {
    counters.forced_disconnects += 1;
    try { ws.close(1000, 'forced failure injection'); } catch {}
  } else {
    counters.graceful_rotations += 1;
    try { ws.close(1000, 'planned soak cycle rotation'); } catch {}
  }
  await sleep(250);

  const last = Number(snapshot.lastUpdateId);
  const usable = events.map(item => item.data)
    .filter(item => item.e === 'depthUpdate' && Number(item.u) >= last);
  const bridge = usable.findIndex(item => Number(item.U) <= last && Number(item.u) >= last);
  const bids = new Map();
  const asks = new Map();
  applyLevels(bids, snapshot.bids);
  applyLevels(asks, snapshot.asks);
  let previous = null;
  let applied = 0;
  let gaps = 0;

  if (bridge < 0) {
    gaps += 1;
    counters.sequence_gaps += 1;
  } else {
    counters.resynchronizations += 1;
    for (const update of usable.slice(bridge)) {
      if (previous !== null && update.pu !== undefined && Number(update.pu) !== previous) {
        gaps += 1;
        counters.sequence_gaps += 1;
        break;
      }
      applyLevels(bids, update.b);
      applyLevels(asks, update.a);
      previous = Number(update.u);
      applied += 1;
    }
  }

  cycleCount += 1;
  return {
    cycle: cycleCount,
    event_count: events.length,
    snapshot_last_update_id: last,
    snapshot_requested_while_socket_open: true,
    snapshot_request_ms: snapshotRequestedAt,
    snapshot_received_ms: snapshotReceivedAt,
    bridge_found: bridge >= 0,
    applied_updates: applied,
    sequence_gaps: gaps,
    crossed_book: crossed(bids, asks),
    ws_error: wsError,
    counters: { ...counters },
  };
}

function writeState(lastCycle) {
  fs.writeFileSync(statePath, JSON.stringify({
    test_data: false,
    read_only: true,
    started_at: startedAt.toISOString(),
    updated_at: new Date().toISOString(),
    mode,
    cycle_count: cycleCount,
    counters: { ...counters },
    last_cycle: lastCycle,
  }, null, 2));
}

function writeHourlyCheckpoint() {
  const elapsedMs = now() - startedAt.getTime();
  const detectedDrops = counters.parse_failures + counters.persistence_failures + counters.sequence_gaps;
  const persistenceGap = Math.max(0, counters.socket_frames_received - counters.raw_records_persisted);
  const checkpoint = {
    test_data: false,
    read_only: true,
    checkpoint_at: new Date().toISOString(),
    elapsed_seconds: Math.floor(elapsedMs / 1000),
    counters: { ...counters },
    detected_drops: detectedDrops,
    persistence_gap: persistenceGap,
    healthy: persistenceGap === 0 && counters.sequence_gaps === 0 && counters.parse_failures === 0,
  };
  fs.appendFileSync(checkpointPath, `${JSON.stringify(checkpoint)}\n`, { encoding: 'utf8', flush: true });
  return checkpoint;
}

async function runProbe() {
  const first = await openBufferedCycle(2500, true);
  counters.reconnects += 1;
  const second = await openBufferedCycle(2500, true);
  const result = {
    test_data: false,
    read_only: true,
    started_at: startedAt.toISOString(),
    completed_at: new Date().toISOString(),
    forced_disconnects: counters.forced_disconnects,
    reconnects: counters.reconnects,
    resynchronizations: counters.resynchronizations,
    counters: { ...counters },
    cycles: [first, second],
    pass: [first, second].every(cycle => cycle.bridge_found && cycle.sequence_gaps === 0 && !cycle.crossed_book && !cycle.ws_error),
  };
  writeState(second);
  console.log(JSON.stringify(result, null, 2));
}

async function runSoak() {
  let last = null;
  let nextCheckpoint = startedAt.getTime() + 3_600_000;
  let nextFailure = startedAt.getTime() + 120_000;
  let sessions = 0;
  while (true) {
    const injectFailure = now() >= nextFailure;
    const duration = injectFailure ? 10_000 : Math.max(5000, Math.min(60_000, nextFailure - now()));
    if (sessions > 0) counters.reconnects += 1;
    last = await openBufferedCycle(duration, injectFailure);
    sessions += 1;
    writeState(last);
    if (injectFailure) nextFailure += 3_600_000;
    while (now() >= nextCheckpoint) {
      const checkpoint = writeHourlyCheckpoint();
      console.log(`CHECKPOINT ${JSON.stringify(checkpoint)}`);
      nextCheckpoint += 3_600_000;
    }
    await sleep(500);
  }
}

process.on('uncaughtException', error => {
  console.error(error);
  writeState({ fatal: String(error) });
  process.exit(1);
});
process.on('unhandledRejection', error => {
  console.error(error);
  writeState({ fatal: String(error) });
  process.exit(1);
});

if (mode === 'probe') await runProbe();
else await runSoak();

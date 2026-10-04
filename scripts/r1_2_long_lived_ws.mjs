import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {
  heartbeatSurvivalProved,
  classifyConnectionClose,
  rolloverDeviationSeconds,
} from './r1_2_long_lived_policy.mjs';

const root = process.env.EQS_LONG_LIVED_DIR;
if (!root) throw new Error('EQS_LONG_LIVED_DIR is required');
fs.mkdirSync(root, { recursive: true });
const statePath = path.join(root, 'state.json');
const rawPath = path.join(root, 'mark_price_raw.jsonl');
const eventPath = path.join(root, 'events.jsonl');
const url = 'wss://fstream.binance.com/ws/btcusdt@markPrice@1s';
const runStartedAt = Date.now();
let attempt = 0;
let active = null;
let totalMessages = 0;
let expectedRollovers = 0;
let prematureCloses = 0;
let reconnects = 0;

const iso = ms => new Date(ms).toISOString();
const hash = text => crypto.createHash('sha256').update(text).digest('hex');
function append(pathname, row) {
  fs.appendFileSync(pathname, `${JSON.stringify(row)}\n`, { encoding: 'utf8', flush: true });
}
function atomicState(extra = {}) {
  const now = Date.now();
  const age = active ? now - active.openedAt : 0;
  const state = {
    schema_id: 'EQS-R1.2-LONG-LIVED-WS-STATE-V1',
    test_data: false,
    read_only: true,
    broker_submission_enabled: false,
    live_authority: false,
    credentials_accessed: false,
    url,
    run_started_at: iso(runStartedAt),
    updated_at: iso(now),
    attempt,
    active_connection_opened_at: active ? iso(active.openedAt) : null,
    active_connection_age_seconds: Math.floor(age / 1000),
    active_attempt_messages: active?.messages ?? 0,
    total_messages: totalMessages,
    last_message_at: active?.lastMessageAt ? iso(active.lastMessageAt) : null,
    heartbeat_survival_proved: active ? heartbeatSurvivalProved(age, active.messages) : false,
    expected_rollovers: expectedRollovers,
    premature_closes: prematureCloses,
    reconnects,
    ...extra,
  };
  const tmp = `${statePath}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(state, null, 2));
  fs.renameSync(tmp, statePath);
}

function connect() {
  attempt += 1;
  const ws = new WebSocket(url);
  const createdAt = Date.now();
  active = { ws, createdAt, openedAt: 0, messages: 0, lastMessageAt: 0 };
  append(eventPath, { event: 'CONNECTING', attempt, at: iso(createdAt), url });
  ws.onopen = () => {
    if (active?.ws !== ws) return;
    active.openedAt = Date.now();
    append(eventPath, { event: 'OPEN', attempt, at: iso(active.openedAt) });
    atomicState();
  };
  ws.onmessage = event => {
    if (active?.ws !== ws) return;
    const receivedAt = Date.now();
    const raw = String(event.data);
    active.messages += 1;
    active.lastMessageAt = receivedAt;
    totalMessages += 1;
    append(rawPath, {
      received_at: iso(receivedAt),
      raw_sha256: hash(raw),
      size_bytes: Buffer.byteLength(raw),
      raw_b64: Buffer.from(raw).toString('base64'),
    });
    if (active.messages === 1 || active.messages % 60 === 0) atomicState();
  };
  ws.onerror = () => {
    append(eventPath, { event: 'WS_ERROR', attempt, at: iso(Date.now()) });
  };
  ws.onclose = event => {
    if (active?.ws !== ws) return;
    const closedAt = Date.now();
    const openedAt = active.openedAt || createdAt;
    const ageMs = Math.max(0, closedAt - openedAt);
    const classification = classifyConnectionClose(ageMs);
    if (classification === 'EXPECTED_24H_ROLLOVER_WINDOW') expectedRollovers += 1;
    else prematureCloses += 1;
    append(eventPath, {
      event: 'CLOSE', attempt, at: iso(closedAt), code: event.code,
      reason: event.reason || '', connection_age_seconds: Math.floor(ageMs / 1000),
      classification, rollover_deviation_seconds: rolloverDeviationSeconds(ageMs),
      messages: active.messages,
    });
    atomicState({ last_close_classification: classification, last_close_code: event.code });
    active = null;
    setTimeout(() => { reconnects += 1; connect(); }, 2000);
  };
}

setInterval(() => atomicState(), 30_000).unref();
process.on('SIGTERM', () => { atomicState({ stopped_by: 'SIGTERM' }); process.exit(0); });
process.on('SIGINT', () => { atomicState({ stopped_by: 'SIGINT' }); process.exit(0); });
process.on('uncaughtException', error => {
  append(eventPath, { event: 'FATAL', at: iso(Date.now()), error: String(error) });
  atomicState({ fatal: String(error) });
  process.exit(1);
});
connect();

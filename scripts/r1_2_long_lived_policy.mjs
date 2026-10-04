export const HEARTBEAT_PROOF_MS = 11 * 60 * 1000;
export const DOCUMENTED_ROLLOVER_MS = 24 * 60 * 60 * 1000;
export const ROLLOVER_ACCEPT_EARLIEST_MS = (23 * 60 + 45) * 60 * 1000;

export function heartbeatSurvivalProved(connectionAgeMs, messageCount) {
  return Number(connectionAgeMs) >= HEARTBEAT_PROOF_MS && Number(messageCount) > 0;
}

export function classifyConnectionClose(connectionAgeMs) {
  const age = Number(connectionAgeMs);
  if (!Number.isFinite(age) || age < 0) throw new RangeError('connectionAgeMs must be non-negative');
  return age >= ROLLOVER_ACCEPT_EARLIEST_MS ? 'EXPECTED_24H_ROLLOVER_WINDOW' : 'PREMATURE_CLOSE';
}

export function rolloverDeviationSeconds(connectionAgeMs) {
  return Math.round((Number(connectionAgeMs) - DOCUMENTED_ROLLOVER_MS) / 1000);
}

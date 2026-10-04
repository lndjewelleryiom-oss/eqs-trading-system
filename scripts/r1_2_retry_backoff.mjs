const DEFAULT_RETRY_STATUSES = new Set([429, 500, 502, 503, 504]);

export function shouldRetryStatus(status) {
  return DEFAULT_RETRY_STATUSES.has(Number(status));
}

export function retryDelayMs(attempt, retryAfter = null, nowMs = Date.now()) {
  if (!Number.isInteger(attempt) || attempt < 0) throw new RangeError('attempt must be a non-negative integer');
  let hinted = null;
  if (retryAfter !== null && retryAfter !== undefined && String(retryAfter).trim() !== '') {
    const text = String(retryAfter).trim();
    const seconds = Number(text);
    if (Number.isFinite(seconds) && seconds >= 0) hinted = seconds * 1000;
    else {
      const when = Date.parse(text);
      if (Number.isFinite(when)) hinted = Math.max(0, when - nowMs);
    }
  }
  const exponential = Math.min(5000, 250 * (2 ** attempt));
  return Math.max(0, Math.min(30000, hinted === null ? exponential : hinted));
}

const defaultSleep = ms => new Promise(resolve => setTimeout(resolve, ms));

export async function fetchWithBackoff(url, {
  fetchFn = fetch,
  sleepFn = defaultSleep,
  maxAttempts = 5,
  onAttempt = () => {},
  onResponse = () => {},
  onRetry = () => {},
} = {}) {
  if (!Number.isInteger(maxAttempts) || maxAttempts < 1) throw new RangeError('maxAttempts must be >= 1');
  let lastTransportError = null;
  for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
    onAttempt({ attempt, url });
    let response;
    try {
      response = await fetchFn(url);
      lastTransportError = null;
    } catch (error) {
      lastTransportError = error;
      if (attempt + 1 >= maxAttempts) throw error;
      const delayMs = retryDelayMs(attempt);
      onRetry({ attempt, status: null, delayMs, transportError: String(error) });
      await sleepFn(delayMs);
      continue;
    }
    onResponse({ attempt, status: Number(response.status), ok: Boolean(response.ok) });
    if (response.ok || !shouldRetryStatus(response.status) || attempt + 1 >= maxAttempts) return response;
    const retryAfter = response.headers?.get?.('retry-after') ?? null;
    const delayMs = retryDelayMs(attempt, retryAfter);
    onRetry({ attempt, status: Number(response.status), delayMs, transportError: null });
    await sleepFn(delayMs);
  }
  if (lastTransportError) throw lastTransportError;
  throw new Error('unreachable retry state');
}

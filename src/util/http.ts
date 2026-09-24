/**
 * JSON-over-HTTP with timeouts, bounded retries and a circuit breaker.
 * Uses Node's built-in fetch (undici), so there is no HTTP dependency.
 */
import { AbortError, retry } from './retry';

const SECRET_PARAMS = /([?&](?:apiKey|api_key|key|token|access_token)=)[^&#]*/gi;

/** Removes credentials from URLs before they reach logs or the dashboard. */
export function redactUrl(url: string): string {
  return url.replace(SECRET_PARAMS, '$1***');
}

export class HttpError extends Error {
  readonly status: number;
  readonly url: string;
  readonly bodySnippet: string;
  readonly retryAfterMs: number | null;
  /** Response headers of the failed request, when there was a response (e.g. quota headers on a 429). */
  readonly headers: Headers | null;

  constructor(
    status: number,
    url: string,
    bodySnippet: string,
    retryAfterMs: number | null,
    headers: Headers | null = null,
  ) {
    super(`HTTP ${status} for ${redactUrl(url)}${bodySnippet ? `: ${bodySnippet.slice(0, 160)}` : ''}`);
    this.name = 'HttpError';
    this.status = status;
    this.url = redactUrl(url);
    this.bodySnippet = bodySnippet;
    this.retryAfterMs = retryAfterMs;
    this.headers = headers;
  }

  /** Auth/permission failures are final; retrying cannot fix them. */
  get isBlocked(): boolean {
    return this.status === 401 || this.status === 403 || this.status === 451;
  }

  get isRateLimited(): boolean {
    return this.status === 429;
  }
}

export class TimeoutError extends Error {
  constructor(url: string, ms: number) {
    super(`Timed out after ${ms}ms: ${redactUrl(url)}`);
    this.name = 'TimeoutError';
  }
}

export class ParseError extends Error {
  constructor(url: string, detail: string) {
    super(`Invalid JSON from ${redactUrl(url)}: ${detail}`);
    this.name = 'ParseError';
  }
}

function parseRetryAfter(value: string | null): number | null {
  if (!value) return null;
  const secs = Number(value);
  if (Number.isFinite(secs) && secs >= 0) return secs * 1000;
  const at = Date.parse(value);
  return Number.isFinite(at) ? Math.max(0, at - Date.now()) : null;
}

/** Network errors, timeouts, 408, 429 and 5xx are worth retrying. Everything else is final. */
export function isRetryable(err: unknown): boolean {
  if (err instanceof AbortError) return false;
  if (err instanceof HttpError) return err.status === 408 || err.status === 429 || err.status >= 500;
  if (err instanceof ParseError) return true;
  if (err instanceof TimeoutError) return true;
  return err instanceof TypeError || (err instanceof Error && /fetch failed|ECONN|ETIMEDOUT|EAI_AGAIN|socket/i.test(err.message));
}

export interface FetchJsonOptions {
  timeoutMs?: number;
  headers?: Record<string, string>;
  /** Retries after the first attempt (default 2). */
  retries?: number;
  retryBaseMs?: number;
  retryMaxMs?: number;
  signal?: AbortSignal;
  /** Cap on the response body size in bytes (default 25 MB) to protect memory. */
  maxBytes?: number;
}

export interface FetchJsonResult<T> {
  data: T;
  status: number;
  headers: Headers;
  durationMs: number;
}

async function fetchOnce<T>(url: string, opts: FetchJsonOptions): Promise<FetchJsonResult<T>> {
  const timeoutMs = opts.timeoutMs ?? 10_000;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  timer.unref?.();
  const onOuterAbort = () => controller.abort();
  opts.signal?.addEventListener('abort', onOuterAbort, { once: true });
  const started = Date.now();
  try {
    let res: Response;
    try {
      res = await fetch(url, {
        method: 'GET',
        headers: { Accept: 'application/json', ...opts.headers },
        signal: controller.signal,
      });
    } catch (err) {
      if (opts.signal?.aborted) throw new AbortError();
      if (controller.signal.aborted) throw new TimeoutError(url, timeoutMs);
      throw err;
    }

    const maxBytes = opts.maxBytes ?? 25 * 1024 * 1024;
    const declared = Number(res.headers.get('content-length'));
    if (Number.isFinite(declared) && declared > maxBytes) {
      await res.body?.cancel().catch(() => undefined);
      throw new HttpError(res.status, url, `response too large (${declared} bytes)`, null, res.headers);
    }

    let text: string;
    try {
      text = await res.text();
    } catch (err) {
      if (opts.signal?.aborted) throw new AbortError();
      if (controller.signal.aborted) throw new TimeoutError(url, timeoutMs);
      throw err;
    }
    if (text.length > maxBytes) {
      throw new HttpError(res.status, url, `response too large (${text.length} chars)`, null, res.headers);
    }

    if (!res.ok) {
      throw new HttpError(
        res.status,
        url,
        text.replace(/\s+/g, ' ').trim().slice(0, 300),
        parseRetryAfter(res.headers.get('retry-after')),
        res.headers,
      );
    }
    let data: T;
    try {
      data = JSON.parse(text) as T;
    } catch (err) {
      throw new ParseError(url, `${(err as Error).message}; body starts: ${text.slice(0, 120).replace(/\s+/g, ' ')}`);
    }
    return { data, status: res.status, headers: res.headers, durationMs: Date.now() - started };
  } finally {
    clearTimeout(timer);
    opts.signal?.removeEventListener('abort', onOuterAbort);
  }
}

/**
 * GET a JSON document. Retries transient failures with exponential backoff and jitter,
 * honours Retry-After on 429, and fails fast on 4xx (including 403 blocks).
 */
export async function fetchJson<T = unknown>(url: string, opts: FetchJsonOptions = {}): Promise<FetchJsonResult<T>> {
  return retry((_attempt) => fetchOnce<T>(url, opts), {
    retries: opts.retries ?? 2,
    baseMs: opts.retryBaseMs ?? 500,
    maxMs: opts.retryMaxMs ?? 8_000,
    signal: opts.signal,
    shouldRetry: (err) => isRetryable(err),
    delayFor: (err) => (err instanceof HttpError && err.retryAfterMs !== null ? err.retryAfterMs : null),
  });
}

export type BreakerState = 'closed' | 'open' | 'half-open';

export interface CircuitBreakerOptions {
  /** Consecutive failures that open the circuit. */
  failureThreshold: number;
  /** First cooldown once open. Doubles on every re-open up to maxCooldownMs. */
  cooldownMs: number;
  maxCooldownMs: number;
}

export interface BreakerSnapshot {
  name: string;
  state: BreakerState;
  consecutiveFailures: number;
  openCount: number;
  nextAttemptAt: number | null;
  lastError: string | null;
}

/**
 * Stops hammering an endpoint that keeps failing. While open, `canRequest()` is false until the
 * cooldown expires; then one trial request is allowed (half-open). Success closes the circuit and
 * resets the cooldown; failure re-opens it with a doubled cooldown.
 */
export class CircuitBreaker {
  readonly name: string;
  private readonly opts: CircuitBreakerOptions;
  private _state: BreakerState = 'closed';
  private failures = 0;
  private opens = 0;
  private currentCooldown: number;
  private openedUntil = 0;
  private openedAt = 0;
  private trialInFlight = false;
  private lastErr: string | null = null;

  constructor(name: string, opts: CircuitBreakerOptions) {
    this.name = name;
    this.opts = opts;
    this.currentCooldown = opts.cooldownMs;
  }

  get state(): BreakerState {
    if (this._state === 'open') {
      const now = Date.now();
      // A clock that stepped back more than a minute since the circuit opened would keep it open for that long too.
      if (now >= this.openedUntil || now < this.openedAt - 60_000) this._state = 'half-open';
    }
    return this._state;
  }

  canRequest(): boolean {
    const s = this.state;
    if (s === 'closed') return true;
    if (s === 'half-open' && !this.trialInFlight) {
      this.trialInFlight = true;
      return true;
    }
    return false;
  }

  recordSuccess(): void {
    this._state = 'closed';
    this.failures = 0;
    this.trialInFlight = false;
    this.currentCooldown = this.opts.cooldownMs;
    this.lastErr = null;
  }

  /** `cooldownOverrideMs` lets callers open immediately for a known-long condition (e.g. HTTP 403). */
  recordFailure(err?: unknown, cooldownOverrideMs?: number): void {
    this.failures++;
    this.lastErr = err instanceof Error ? err.message : err === undefined ? 'unknown error' : String(err);
    const wasTrial = this.trialInFlight;
    this.trialInFlight = false;
    if (cooldownOverrideMs !== undefined || wasTrial || this.failures >= this.opts.failureThreshold) {
      const cooldown = cooldownOverrideMs ?? this.currentCooldown;
      this._state = 'open';
      this.opens++;
      this.openedAt = Date.now();
      this.openedUntil = this.openedAt + cooldown;
      this.currentCooldown = Math.min(this.opts.maxCooldownMs, Math.max(this.currentCooldown, cooldown) * 2);
    }
  }

  snapshot(): BreakerSnapshot {
    const state = this.state;
    return {
      name: this.name,
      state,
      consecutiveFailures: this.failures,
      openCount: this.opens,
      nextAttemptAt: state === 'open' ? this.openedUntil : null,
      lastError: this.lastErr,
    };
  }
}

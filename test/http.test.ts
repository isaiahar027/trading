import http from 'node:http';
import type { AddressInfo } from 'node:net';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  CircuitBreaker,
  fetchJson,
  HttpError,
  isRetryable,
  ParseError,
  redactUrl,
  TimeoutError,
} from '../src/util/http';
import { AbortError } from '../src/util/retry';

// ---------------------------------------------------------------------------------------------
// Local test server (127.0.0.1, ephemeral port). No request ever leaves the machine.
// ---------------------------------------------------------------------------------------------

let server: http.Server;
let base = '';
const hits = new Map<string, number>();
const lastHeaders = new Map<string, http.IncomingHttpHeaders>();
const pendingTimers = new Set<NodeJS.Timeout>();

function later(ms: number, fn: () => void): void {
  const t = setTimeout(() => {
    pendingTimers.delete(t);
    fn();
  }, ms);
  pendingTimers.add(t);
}

function send(res: http.ServerResponse, status: number, body: string, headers: Record<string, string> = {}): void {
  if (res.destroyed || res.writableEnded) return;
  res.writeHead(status, { 'Content-Type': 'application/json', ...headers });
  res.end(body);
}

function handle(req: http.IncomingMessage, res: http.ServerResponse): void {
  res.on('error', () => undefined);
  req.on('error', () => undefined);
  const url = new URL(req.url ?? '/', 'http://localhost');
  const path = url.pathname;
  const n = (hits.get(path) ?? 0) + 1;
  hits.set(path, n);
  lastHeaders.set(path, req.headers);

  switch (path) {
    case '/ok':
      return send(res, 200, JSON.stringify({ hello: 'world', q: url.searchParams.get('q') }), { 'x-requests-remaining': '42' });
    case '/echo':
      return send(res, 200, JSON.stringify({ accept: req.headers.accept, custom: req.headers['x-custom'] ?? null }));
    case '/flaky':
      return n === 1 ? send(res, 500, 'oops') : send(res, 200, JSON.stringify({ ok: true, attempt: n }));
    case '/always500':
      return send(res, 503, 'service   unavailable\n now');
    case '/forbidden':
      return send(res, 403, 'blocked by policy');
    case '/unauthorized':
      return send(res, 401, 'bad key');
    case '/notfound':
      return send(res, 404, '{"message":"Unknown sport"}');
    case '/ratelimited-once':
      return n === 1 ? send(res, 429, 'slow down', { 'Retry-After': '1' }) : send(res, 200, JSON.stringify({ ok: true }));
    case '/ratelimited-7s':
      return send(res, 429, 'slow down', { 'Retry-After': '7' });
    case '/ratelimited-date':
      return send(res, 429, 'slow down', { 'Retry-After': new Date(Date.now() + 5_000).toUTCString() });
    case '/slow':
      return later(400, () => send(res, 200, '{"late":true}'));
    case '/hang':
      return; // never answers; the socket is closed in afterAll
    case '/badjson':
      return send(res, 200, 'not json {');
    case '/big': {
      if (res.destroyed) return;
      res.writeHead(200, { 'Content-Type': 'application/json', 'Content-Length': '5000' });
      res.end('x'.repeat(5000));
      return;
    }
    case '/big-chunked': {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.write(`"${'y'.repeat(2000)}`);
      res.end(`${'y'.repeat(3000)}"`);
      return;
    }
    default:
      return send(res, 404, '{"error":"no route"}');
  }
}

beforeAll(async () => {
  server = http.createServer(handle);
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
});

afterAll(async () => {
  for (const t of pendingTimers) clearTimeout(t);
  pendingTimers.clear();
  server.closeAllConnections();
  await new Promise<void>((resolve) => server.close(() => resolve()));
});

beforeEach(() => {
  hits.clear();
  lastHeaders.clear();
});

afterEach(() => {
  vi.restoreAllMocks();
});

const FAST = { retryBaseMs: 1, retryMaxMs: 5 } as const;

async function rejection(p: Promise<unknown>): Promise<unknown> {
  try {
    await p;
  } catch (err) {
    return err;
  }
  throw new Error('expected the promise to reject');
}

// ---------------------------------------------------------------------------------------------

describe('fetchJson', () => {
  it('returns parsed data, status, headers and duration', async () => {
    const r = await fetchJson<{ hello: string; q: string }>(`${base}/ok?q=nba`, FAST);
    expect(r.status).toBe(200);
    expect(r.data).toEqual({ hello: 'world', q: 'nba' });
    expect(r.headers.get('x-requests-remaining')).toBe('42');
    expect(r.durationMs).toBeGreaterThanOrEqual(0);
    expect(hits.get('/ok')).toBe(1);
  });

  it('sends Accept: application/json plus custom headers', async () => {
    const r = await fetchJson<{ accept: string; custom: string }>(`${base}/echo`, { ...FAST, headers: { 'X-Custom': 'yes' } });
    expect(r.data.accept).toBe('application/json');
    expect(r.data.custom).toBe('yes');
  });

  it('retries a 500 and then succeeds', async () => {
    const r = await fetchJson<{ ok: boolean; attempt: number }>(`${base}/flaky`, FAST);
    expect(r.data).toEqual({ ok: true, attempt: 2 });
    expect(hits.get('/flaky')).toBe(2);
  });

  it('gives up after retries + 1 attempts on persistent 5xx', async () => {
    const err = await rejection(fetchJson(`${base}/always500`, { ...FAST, retries: 2 }));
    expect(err).toBeInstanceOf(HttpError);
    const e = err as HttpError;
    expect(e.status).toBe(503);
    expect(e.bodySnippet).toBe('service unavailable now'); // whitespace collapsed
    expect(hits.get('/always500')).toBe(3);
  });

  it('does not retry 403 (blocked)', async () => {
    const err = await rejection(fetchJson(`${base}/forbidden`, { ...FAST, retries: 5 }));
    expect(err).toBeInstanceOf(HttpError);
    const e = err as HttpError;
    expect(e.status).toBe(403);
    expect(e.isBlocked).toBe(true);
    expect(e.isRateLimited).toBe(false);
    expect(hits.get('/forbidden')).toBe(1);
  });

  it('does not retry 401 or 404', async () => {
    const e401 = (await rejection(fetchJson(`${base}/unauthorized`, { ...FAST, retries: 5 }))) as HttpError;
    expect(e401.status).toBe(401);
    expect(e401.isBlocked).toBe(true);
    const e404 = (await rejection(fetchJson(`${base}/notfound`, { ...FAST, retries: 5 }))) as HttpError;
    expect(e404.status).toBe(404);
    expect(e404.isBlocked).toBe(false);
    expect(e404.bodySnippet).toContain('Unknown sport');
    expect(hits.get('/unauthorized')).toBe(1);
    expect(hits.get('/notfound')).toBe(1);
  });

  it('honours Retry-After (seconds) on 429 instead of the tiny backoff', async () => {
    const started = Date.now();
    const r = await fetchJson<{ ok: boolean }>(`${base}/ratelimited-once`, { retryBaseMs: 1, retryMaxMs: 5_000 });
    const elapsed = Date.now() - started;
    expect(r.data.ok).toBe(true);
    expect(hits.get('/ratelimited-once')).toBe(2);
    expect(elapsed).toBeGreaterThanOrEqual(950);
  });

  it('caps Retry-After at retryMaxMs', async () => {
    const started = Date.now();
    const err = (await rejection(fetchJson(`${base}/ratelimited-7s`, { retries: 1, retryBaseMs: 1, retryMaxMs: 30 }))) as HttpError;
    expect(Date.now() - started).toBeLessThan(3_000);
    expect(err.status).toBe(429);
    expect(hits.get('/ratelimited-7s')).toBe(2);
  });

  it('parses Retry-After seconds and HTTP-date forms into retryAfterMs', async () => {
    const secs = (await rejection(fetchJson(`${base}/ratelimited-7s`, { ...FAST, retries: 0 }))) as HttpError;
    expect(secs.isRateLimited).toBe(true);
    expect(secs.retryAfterMs).toBe(7_000);

    const date = (await rejection(fetchJson(`${base}/ratelimited-date`, { ...FAST, retries: 0 }))) as HttpError;
    expect(date.retryAfterMs).not.toBeNull();
    expect(date.retryAfterMs as number).toBeGreaterThan(3_000);
    expect(date.retryAfterMs as number).toBeLessThanOrEqual(5_000);

    const none = (await rejection(fetchJson(`${base}/forbidden`, { ...FAST, retries: 0 }))) as HttpError;
    expect(none.retryAfterMs).toBeNull();
  });

  it('keeps the failed response headers on HttpError (e.g. quota headers on a 429)', async () => {
    const err = (await rejection(fetchJson(`${base}/ratelimited-7s`, { ...FAST, retries: 0 }))) as HttpError;
    expect(err.headers).not.toBeNull();
    expect(err.headers?.get('retry-after')).toBe('7');
    expect(new HttpError(500, 'u', '', null).headers).toBeNull();
  });

  it('times out with TimeoutError', async () => {
    const err = await rejection(fetchJson(`${base}/slow`, { timeoutMs: 50, retries: 0 }));
    expect(err).toBeInstanceOf(TimeoutError);
    expect((err as Error).message).toContain('Timed out after 50ms');
  });

  it('retries timeouts', async () => {
    const err = await rejection(fetchJson(`${base}/slow`, { timeoutMs: 50, retries: 1, ...FAST }));
    expect(err).toBeInstanceOf(TimeoutError);
    expect(hits.get('/slow')).toBe(2);
  });

  it('rejects invalid JSON with ParseError (retried as transient)', async () => {
    const err = await rejection(fetchJson(`${base}/badjson`, { ...FAST, retries: 1 }));
    expect(err).toBeInstanceOf(ParseError);
    expect((err as Error).message).toContain('not json');
    expect(hits.get('/badjson')).toBe(2);
  });

  it('rejects bodies over maxBytes (declared Content-Length) without retrying', async () => {
    const err = await rejection(fetchJson(`${base}/big`, { ...FAST, maxBytes: 1_000 }));
    expect(err).toBeInstanceOf(HttpError);
    expect((err as Error).message).toContain('response too large');
    expect(hits.get('/big')).toBe(1);
  });

  it('rejects chunked bodies over maxBytes', async () => {
    const err = await rejection(fetchJson(`${base}/big-chunked`, { ...FAST, maxBytes: 1_000 }));
    expect(err).toBeInstanceOf(HttpError);
    expect((err as Error).message).toContain('response too large');
    const ok = await fetchJson<string>(`${base}/big-chunked`, { ...FAST, maxBytes: 10_000 });
    expect(ok.data).toHaveLength(5000);
  });

  it('rejects with AbortError when the caller aborts mid-request, without retrying', async () => {
    const ac = new AbortController();
    setTimeout(() => ac.abort(), 30).unref();
    const err = await rejection(fetchJson(`${base}/hang`, { ...FAST, retries: 3, timeoutMs: 5_000, signal: ac.signal }));
    expect(err).toBeInstanceOf(AbortError);
    expect(hits.get('/hang')).toBe(1);
  });

  it('makes no request when the signal is already aborted', async () => {
    const ac = new AbortController();
    ac.abort();
    const err = await rejection(fetchJson(`${base}/ok`, { ...FAST, signal: ac.signal }));
    expect(err).toBeInstanceOf(AbortError);
    expect(hits.get('/ok')).toBeUndefined();
  });

  it('surfaces connection failures as retryable errors', async () => {
    const tmp = http.createServer();
    await new Promise<void>((resolve) => tmp.listen(0, '127.0.0.1', resolve));
    const port = (tmp.address() as AddressInfo).port;
    await new Promise<void>((resolve) => tmp.close(() => resolve()));
    const err = await rejection(fetchJson(`http://127.0.0.1:${port}/x`, { ...FAST, retries: 1, timeoutMs: 2_000 }));
    expect(err).toBeInstanceOf(Error);
    expect(isRetryable(err)).toBe(true);
  });

  it('never exposes the API key in error messages', async () => {
    const err = (await rejection(fetchJson(`${base}/forbidden?apiKey=SECRET123&markets=h2h`, { ...FAST, retries: 0 }))) as HttpError;
    expect(err.message).not.toContain('SECRET123');
    expect(err.url).not.toContain('SECRET123');
    expect(err.url).toContain('apiKey=***');
    expect(err.url).toContain('markets=h2h');

    const t = await rejection(fetchJson(`${base}/slow?apiKey=SECRET123`, { timeoutMs: 30, retries: 0 }));
    expect((t as Error).message).not.toContain('SECRET123');
    const p = await rejection(fetchJson(`${base}/badjson?apiKey=SECRET123`, { ...FAST, retries: 0 }));
    expect((p as Error).message).not.toContain('SECRET123');
  });
});

describe('redactUrl', () => {
  it('masks credential query parameters and keeps the rest', () => {
    expect(redactUrl('https://api.the-odds-api.com/v4/sports?apiKey=abc123&regions=us')).toBe(
      'https://api.the-odds-api.com/v4/sports?apiKey=***&regions=us',
    );
    expect(redactUrl('https://x.test/a?regions=us&apikey=abc')).toBe('https://x.test/a?regions=us&apikey=***');
    expect(redactUrl('https://x.test/a?api_key=1&key=2&token=3&access_token=4&keep=5')).toBe(
      'https://x.test/a?api_key=***&key=***&token=***&access_token=***&keep=5',
    );
    expect(redactUrl('https://x.test/a?APIKEY=abc#frag')).toBe('https://x.test/a?APIKEY=***#frag');
  });

  it('leaves URLs without credentials untouched', () => {
    const u = 'https://x.test/v4/sports/basketball_nba/odds?markets=h2h&monkey=1&keyboard=2';
    expect(redactUrl(u)).toBe(u);
  });

  it('is stable across repeated calls (global regex state)', () => {
    const u = 'https://x.test/?apiKey=a';
    expect(redactUrl(u)).toBe('https://x.test/?apiKey=***');
    expect(redactUrl(u)).toBe('https://x.test/?apiKey=***');
  });
});

describe('isRetryable / HttpError', () => {
  it('classifies errors', () => {
    const h = (s: number) => new HttpError(s, 'https://x.test/', '', null);
    for (const s of [408, 429, 500, 502, 503, 504]) expect(isRetryable(h(s)), String(s)).toBe(true);
    for (const s of [400, 401, 403, 404, 422, 451]) expect(isRetryable(h(s)), String(s)).toBe(false);
    expect(isRetryable(new TimeoutError('https://x.test/', 10))).toBe(true);
    expect(isRetryable(new ParseError('https://x.test/', 'bad'))).toBe(true);
    expect(isRetryable(new TypeError('fetch failed'))).toBe(true);
    expect(isRetryable(new Error('read ECONNRESET'))).toBe(true);
    expect(isRetryable(new Error('getaddrinfo EAI_AGAIN'))).toBe(true);
    expect(isRetryable(new AbortError())).toBe(false);
    expect(isRetryable(new Error('something else'))).toBe(false);
    expect(isRetryable('string error')).toBe(false);
    expect(isRetryable(undefined)).toBe(false);
  });

  it('exposes isBlocked / isRateLimited and a redacted, bounded message', () => {
    expect(new HttpError(401, 'u', '', null).isBlocked).toBe(true);
    expect(new HttpError(403, 'u', '', null).isBlocked).toBe(true);
    expect(new HttpError(451, 'u', '', null).isBlocked).toBe(true);
    expect(new HttpError(429, 'u', '', 1000).isBlocked).toBe(false);
    expect(new HttpError(429, 'u', '', 1000).isRateLimited).toBe(true);
    const e = new HttpError(500, 'https://x.test/?token=zzz', 'b'.repeat(1000), null);
    expect(e.name).toBe('HttpError');
    expect(e.message).not.toContain('zzz');
    expect(e.message.length).toBeLessThan(250);
  });
});

// ---------------------------------------------------------------------------------------------
// CircuitBreaker with a controlled clock
// ---------------------------------------------------------------------------------------------

describe('CircuitBreaker', () => {
  let now = 1_700_000_000_000;
  beforeEach(() => {
    now = 1_700_000_000_000;
    vi.spyOn(Date, 'now').mockImplementation(() => now);
  });

  const make = () => new CircuitBreaker('odds-api', { failureThreshold: 3, cooldownMs: 1_000, maxCooldownMs: 5_000 });

  it('starts closed and allows requests', () => {
    const b = make();
    expect(b.state).toBe('closed');
    expect(b.canRequest()).toBe(true);
    expect(b.canRequest()).toBe(true);
    expect(b.snapshot()).toEqual({
      name: 'odds-api',
      state: 'closed',
      consecutiveFailures: 0,
      openCount: 0,
      nextAttemptAt: null,
      lastError: null,
    });
  });

  it('opens after failureThreshold consecutive failures', () => {
    const b = make();
    b.recordFailure(new Error('e1'));
    b.recordFailure(new Error('e2'));
    expect(b.state).toBe('closed');
    expect(b.canRequest()).toBe(true);
    b.recordFailure(new Error('e3'));
    expect(b.state).toBe('open');
    expect(b.canRequest()).toBe(false);
    expect(b.snapshot()).toMatchObject({ state: 'open', consecutiveFailures: 3, openCount: 1, nextAttemptAt: now + 1_000, lastError: 'e3' });
  });

  it('a success resets the failure count', () => {
    const b = make();
    b.recordFailure(new Error('a'));
    b.recordFailure(new Error('b'));
    b.recordSuccess();
    b.recordFailure(new Error('c'));
    b.recordFailure(new Error('d'));
    expect(b.state).toBe('closed');
    expect(b.snapshot().consecutiveFailures).toBe(2);
  });

  it('goes half-open after the cooldown and allows exactly one trial request', () => {
    const b = make();
    for (let i = 0; i < 3; i++) b.recordFailure(new Error('x'));
    now += 999;
    expect(b.state).toBe('open');
    expect(b.canRequest()).toBe(false);
    now += 1;
    expect(b.state).toBe('half-open');
    expect(b.snapshot().nextAttemptAt).toBeNull();
    expect(b.canRequest()).toBe(true);
    expect(b.canRequest()).toBe(false); // trial already in flight
    expect(b.canRequest()).toBe(false);
  });

  it('closes on a successful trial and resets the cooldown', () => {
    const b = make();
    for (let i = 0; i < 3; i++) b.recordFailure(new Error('x'));
    now += 1_000;
    expect(b.canRequest()).toBe(true);
    b.recordSuccess();
    expect(b.state).toBe('closed');
    expect(b.canRequest()).toBe(true);
    expect(b.snapshot()).toMatchObject({ consecutiveFailures: 0, lastError: null, openCount: 1 });

    // Next time it opens, the cooldown is back to the base 1 s.
    for (let i = 0; i < 3; i++) b.recordFailure(new Error('y'));
    expect(b.snapshot().nextAttemptAt).toBe(now + 1_000);
  });

  it('re-opens on a failed trial with a doubled cooldown, capped at maxCooldownMs', () => {
    const b = make();
    for (let i = 0; i < 3; i++) b.recordFailure(new Error('x'));
    const cooldowns: number[] = [1_000];
    for (let round = 0; round < 5; round++) {
      now = (b.snapshot().nextAttemptAt as number) + 0;
      expect(b.state).toBe('half-open');
      expect(b.canRequest()).toBe(true);
      b.recordFailure(new Error('trial failed'));
      expect(b.state).toBe('open');
      cooldowns.push((b.snapshot().nextAttemptAt as number) - now);
    }
    expect(cooldowns).toEqual([1_000, 2_000, 4_000, 5_000, 5_000, 5_000]);
    expect(b.snapshot().openCount).toBe(6);
  });

  it('re-opens from half-open on any failure even without a trial having been granted', () => {
    const b = make();
    for (let i = 0; i < 3; i++) b.recordFailure(new Error('x'));
    now += 1_000;
    expect(b.state).toBe('half-open');
    b.recordFailure(new Error('late failure'));
    expect(b.state).toBe('open');
  });

  it('opens immediately with a cooldown override (e.g. invalid key / quota)', () => {
    const b = new CircuitBreaker('odds-api', { failureThreshold: 4, cooldownMs: 30_000, maxCooldownMs: 600_000 });
    b.recordFailure(new Error('HTTP 401'), 30 * 60_000);
    expect(b.state).toBe('open');
    expect(b.snapshot().nextAttemptAt).toBe(now + 30 * 60_000);
    now += 30 * 60_000 - 1;
    expect(b.canRequest()).toBe(false);
    now += 1;
    expect(b.canRequest()).toBe(true);
    // The trial fails normally: next cooldown is doubled from the override, capped at max (10 min).
    b.recordFailure(new Error('still 401'));
    expect(b.snapshot().nextAttemptAt).toBe(now + 600_000);
  });

  it('records non-Error failures readably', () => {
    const b = make();
    b.recordFailure('plain string');
    expect(b.snapshot().lastError).toBe('plain string');
    b.recordFailure();
    expect(b.snapshot().lastError).toBe('unknown error');
    b.recordSuccess();
    expect(b.snapshot().lastError).toBeNull();
  });
});

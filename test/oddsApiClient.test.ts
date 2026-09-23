import * as fs from 'node:fs';
import * as path from 'node:path';
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { loadConfig } from '../src/config';
import type { AppConfig } from '../src/config';
import { OddsApiClient, OddsApiError } from '../src/sources/oddsApiClient';
import type { LeagueDef } from '../src/types';
import { HttpError, ParseError, TimeoutError } from '../src/util/http';
import type { FetchJsonOptions, FetchJsonResult, fetchJson } from '../src/util/http';
import { setLogLevel } from '../src/util/logger';
import { AbortError } from '../src/util/retry';

const API_KEY = 'SeCrEt0ddsKey9f8e7d6c5b4a';
const NOW = Date.parse('2026-10-27T23:30:00.123Z');
const NBA: LeagueDef = { key: 'NBA', name: 'NBA', oddsApiKey: 'basketball_nba', threeWay: false };
const EPL: LeagueDef = { key: 'EPL', name: 'Premier League', oddsApiKey: 'soccer_epl', threeWay: true };
const TEN_BOOKS = ['pinnacle', 'draftkings', 'fanduel', 'betmgm', 'williamhill_us', 'betonlineag', 'lowvig', 'betrivers', 'bovada', 'fanatics'];

function fixture(name: string): unknown {
  return JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures', name), 'utf8')) as unknown;
}

function makeCfg(overrides: Partial<AppConfig['oddsApi']> = {}): AppConfig['oddsApi'] {
  return {
    ...loadConfig({}, []).oddsApi,
    apiKey: API_KEY,
    baseUrl: 'https://api.the-odds-api.com',
    books: ['pinnacle', 'draftkings', 'fanduel', 'betmgm'],
    markets: ['h2h', 'spreads', 'totals'],
    requestTimeoutMs: 12_345,
    prematchHorizonHours: 24,
    includeLinks: true,
    linkState: 'nj',
    reserveCredits: 200,
    ...overrides,
  };
}

type Step = { data: unknown; status?: number; headers?: Record<string, string> } | { error: unknown };

interface Call {
  url: string;
  opts: FetchJsonOptions | undefined;
}

/** Scripted stand-in for util/http fetchJson. Never touches the network. */
function fakeFetch(steps: Step[] | (() => Step)) {
  const calls: Call[] = [];
  const queue = Array.isArray(steps) ? steps.slice() : null;
  const impl = async (url: string, opts?: FetchJsonOptions): Promise<FetchJsonResult<unknown>> => {
    calls.push({ url, opts });
    const step = queue ? queue.shift() : (steps as () => Step)();
    if (!step) throw new Error('fake fetchJson: no scripted response left');
    if ('error' in step) throw step.error;
    return { data: step.data, status: step.status ?? 200, headers: new Headers(step.headers ?? {}), durationMs: 5 };
  };
  return { calls, fetchJson: impl as typeof fetchJson };
}

function usageHeaders(remaining: number, used: number, last: number): Record<string, string> {
  return { 'x-requests-remaining': String(remaining), 'x-requests-used': String(used), 'x-requests-last': String(last) };
}

function httpError(status: number, body = '', retryAfterMs: number | null = null, headers?: Record<string, string>): HttpError {
  const url = `https://api.the-odds-api.com/v4/sports/basketball_nba/odds?apiKey=${API_KEY}&markets=h2h`;
  const err = new HttpError(status, url, body, retryAfterMs);
  if (headers) Object.assign(err, { headers: new Headers(headers) });
  return err;
}

async function expectKind(p: Promise<unknown>, kind: OddsApiError['kind'], status?: number | null): Promise<OddsApiError> {
  const err = await p.then(
    () => {
      throw new Error(`expected OddsApiError(${kind}) but the call succeeded`);
    },
    (e: unknown) => e,
  );
  expect(err).toBeInstanceOf(OddsApiError);
  const e = err as OddsApiError;
  expect(e.kind).toBe(kind);
  if (status !== undefined) expect(e.status).toBe(status);
  expect(e.message).not.toContain(API_KEY);
  return e;
}

function client(steps: Step[] | (() => Step), cfg = makeCfg(), now: () => number = () => NOW) {
  const fake = fakeFetch(steps);
  return { c: new OddsApiClient(cfg, { fetchJson: fake.fetchJson, now }), calls: fake.calls };
}

beforeAll(() => {
  // Expected warnings from the error-path tests would only clutter the output.
  setLogLevel('error');
});

afterAll(() => {
  setLogLevel('info');
});

afterEach(() => {
  vi.useRealTimers();
});

describe('OddsApiClient — requests', () => {
  it('builds the odds URL', async () => {
    const { c, calls } = client([{ data: [] }]);
    await c.fetchOdds(NBA);
    expect(calls).toHaveLength(1);
    const raw = calls[0].url;
    expect(raw).toContain('bookmakers=pinnacle,draftkings,fanduel,betmgm');
    expect(raw).toContain('markets=h2h,spreads,totals');
    const u = new URL(raw);
    expect(u.origin + u.pathname).toBe('https://api.the-odds-api.com/v4/sports/basketball_nba/odds');
    expect(u.searchParams.get('apiKey')).toBe(API_KEY);
    expect(u.searchParams.get('oddsFormat')).toBe('decimal');
    expect(u.searchParams.get('dateFormat')).toBe('iso');
    expect(u.searchParams.get('includeLinks')).toBe('true');
    // now + 24h, whole seconds, no milliseconds.
    expect(u.searchParams.get('commenceTimeTo')).toBe('2026-10-28T23:30:00Z');
    expect(u.searchParams.get('commenceTimeTo')).toMatch(/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$/);
  });

  it('omits includeLinks when disabled and honours the horizon', async () => {
    const { c, calls } = client([{ data: [] }], makeCfg({ includeLinks: false, prematchHorizonHours: 6 }));
    await c.fetchOdds(EPL);
    const u = new URL(calls[0].url);
    expect(u.pathname).toBe('/v4/sports/soccer_epl/odds');
    expect(u.searchParams.has('includeLinks')).toBe(false);
    expect(u.searchParams.get('commenceTimeTo')).toBe('2026-10-28T05:30:00Z');
  });

  it('passes timeout, retries and the abort signal to fetchJson', async () => {
    const { c, calls } = client([{ data: [] }, { data: [] }]);
    const ac = new AbortController();
    await c.fetchOdds(NBA, ac.signal);
    await c.fetchEvents(NBA, ac.signal);
    for (const call of calls) {
      expect(call.opts?.timeoutMs).toBe(12_345);
      expect(call.opts?.retries).toBe(2);
      expect(call.opts?.signal).toBe(ac.signal);
    }
  });

  it('builds the free events URL', async () => {
    const { c, calls } = client([{ data: [] }]);
    await c.fetchEvents(NBA);
    const u = new URL(calls[0].url);
    expect(u.origin + u.pathname).toBe('https://api.the-odds-api.com/v4/sports/basketball_nba/events');
    expect(u.searchParams.get('apiKey')).toBe(API_KEY);
    expect(u.searchParams.get('dateFormat')).toBe('iso');
    expect(u.searchParams.get('commenceTimeTo')).toBe('2026-10-28T23:30:00Z');
    expect(u.searchParams.has('bookmakers')).toBe(false);
    expect(u.searchParams.has('markets')).toBe(false);
  });

  it('returns a parsed snapshot using the configured books and link state', async () => {
    const fetchedAt = Date.parse('2026-10-27T23:30:05Z');
    const { c } = client([{ data: fixture('oddsapi-nba.json'), headers: usageHeaders(19_850, 150, 3) }], makeCfg({ books: ['pinnacle', 'fanduel'] }), () => fetchedAt);
    const snap = await c.fetchOdds(NBA);
    expect(snap.source).toBe('odds-api');
    expect(snap.league).toBe('NBA');
    expect(snap.fetchedAt).toBe(fetchedAt);
    expect(snap.books).toEqual(['pinnacle', 'fanduel']);
    expect(new Set(snap.quotes.map((q) => q.book))).toEqual(new Set(['pinnacle', 'fanduel']));
    const fdLinks = snap.quotes.filter((q) => q.book === 'fanduel').map((q) => q.link);
    expect(fdLinks.every((l) => typeof l === 'string' && l.startsWith('https://nj.sportsbook.fanduel.com/'))).toBe(true);
  });

  it('maps events to summaries and skips malformed ones', async () => {
    const data = [
      { id: 'a1', sport_key: 'basketball_nba', commence_time: '2026-10-28T00:00:00Z', home_team: 'Boston Celtics', away_team: 'New York Knicks' },
      { id: 'a2', sport_key: 'basketball_nba', commence_time: 'soon', home_team: 'X', away_team: 'Y' },
      { id: 'a3', sport_key: 'basketball_nba', commence_time: '2026-10-28T02:30:00Z', home_team: 'Los Angeles Lakers' },
      null,
      { id: 'a1', sport_key: 'basketball_nba', commence_time: '2026-10-28T00:00:00Z', home_team: 'Dup', away_team: 'Dup2' },
      { id: 'a4', sport_key: 'basketball_nba', commence_time: '2026-10-27T23:10:00Z', home_team: 'Orlando Magic', away_team: 'Miami Heat' },
    ];
    const { c } = client([{ data, headers: usageHeaders(19_850, 150, 0) }]);
    const events = await c.fetchEvents(NBA);
    expect(events).toEqual([
      { id: 'a1', league: 'NBA', home: 'Boston Celtics', away: 'New York Knicks', startTime: Date.parse('2026-10-28T00:00:00Z') },
      { id: 'a4', league: 'NBA', home: 'Orlando Magic', away: 'Miami Heat', startTime: Date.parse('2026-10-27T23:10:00Z') },
    ]);
    expect(c.usage.last).toBe(0);
  });
});

describe('OddsApiClient — credits', () => {
  it('computes the cost per odds call as markets × ceil(books / 10)', () => {
    expect(new OddsApiClient(makeCfg({ books: TEN_BOOKS })).costPerOddsCall()).toBe(3);
    expect(new OddsApiClient(makeCfg({ books: [...TEN_BOOKS, 'espnbet'] })).costPerOddsCall()).toBe(6);
    expect(new OddsApiClient(makeCfg({ books: ['pinnacle', 'draftkings'] })).costPerOddsCall()).toBe(3);
    expect(new OddsApiClient(makeCfg({ books: TEN_BOOKS, markets: ['h2h'] })).costPerOddsCall()).toBe(1);
  });

  it('reads usage headers after every response, keeping values the response omits', async () => {
    let t = NOW;
    const { c } = client(
      [
        { data: [], headers: usageHeaders(19_850, 150, 3) },
        { data: [] },
        { data: [], headers: { 'x-requests-remaining': '19847' } },
        { error: httpError(500, 'boom', null, usageHeaders(19_844, 156, 3)) },
      ],
      makeCfg(),
      () => t,
    );
    expect(c.usage).toEqual({ remaining: null, used: null, last: null, updatedAt: null });

    await c.fetchOdds(NBA);
    expect(c.usage).toEqual({ remaining: 19_850, used: 150, last: 3, updatedAt: NOW });

    t = NOW + 1000;
    await c.fetchOdds(NBA);
    expect(c.usage).toEqual({ remaining: 19_850, used: 150, last: 3, updatedAt: NOW });

    t = NOW + 2000;
    await c.fetchOdds(NBA);
    expect(c.usage).toEqual({ remaining: 19_847, used: 150, last: 3, updatedAt: NOW + 2000 });

    t = NOW + 3000;
    await expectKind(c.fetchOdds(NBA), 'network', 500);
    expect(c.usage).toEqual({ remaining: 19_844, used: 156, last: 3, updatedAt: NOW + 3000 });
  });

  it('ignores garbage usage headers', async () => {
    const { c } = client([{ data: [], headers: { 'x-requests-remaining': 'lots', 'x-requests-used': '-4' } }]);
    await c.fetchOdds(NBA);
    expect(c.usage.remaining).toBeNull();
    expect(c.usage.used).toBeNull();
  });

  it('returns a copy of usage', async () => {
    const { c } = client([{ data: [], headers: usageHeaders(100, 1, 1) }]);
    await c.fetchOdds(NBA);
    const u = c.usage;
    u.remaining = 5;
    expect(c.usage.remaining).toBe(100);
  });
});

describe('OddsApiClient — error mapping', () => {
  it('no key: throws no-key without making a request', async () => {
    const { c, calls } = client([{ data: [] }], makeCfg({ apiKey: '' }));
    expect(c.enabled).toBe(false);
    await expectKind(c.fetchOdds(NBA), 'no-key', null);
    await expectKind(c.fetchEvents(NBA), 'no-key', null);
    expect(calls).toHaveLength(0);

    const blank = client([{ data: [] }], makeCfg({ apiKey: '   ' }));
    expect(blank.c.enabled).toBe(false);
    await expectKind(blank.c.fetchOdds(NBA), 'no-key');
    expect(blank.calls).toHaveLength(0);
  });

  it('league without an Odds API sport key is unavailable without a request', async () => {
    const { c, calls } = client([{ data: [] }]);
    await expectKind(c.fetchOdds({ ...NBA, oddsApiKey: null }), 'unavailable', null);
    expect(calls).toHaveLength(0);
  });

  it('401 -> invalid-key and opens the breaker for 30 minutes', async () => {
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(NOW);
    const { c, calls } = client([
      { error: httpError(401, '{"message":"API key is not valid","error_code":"INVALID_KEY"}') },
      { data: [], headers: usageHeaders(500, 10, 3) },
    ]);
    await expectKind(c.fetchOdds(NBA), 'invalid-key', 401);
    expect(c.health().status).toBe('down');

    await expectKind(c.fetchOdds(NBA), 'circuit-open', null);
    vi.setSystemTime(NOW + 29 * 60_000);
    await expectKind(c.fetchEvents(NBA), 'circuit-open');
    expect(calls).toHaveLength(1);

    vi.setSystemTime(NOW + 30 * 60_000 + 1);
    await c.fetchOdds(NBA);
    expect(calls).toHaveLength(2);
    expect(c.health().status).toBe('ok');
  });

  it('401 whose body says the usage quota is reached -> quota', async () => {
    const { c } = client([{ error: httpError(401, '{"message":"Usage quota has been reached","error_code":"OUT_OF_USAGE_CREDITS"}') }]);
    await expectKind(c.fetchOdds(NBA), 'quota', 401);
    expect(c.health().status).toBe('down');
    expect(c.health().detail).toContain('credits exhausted');
  });

  it('429 -> rate-limited when credits remain', async () => {
    const { c } = client([{ error: httpError(429, 'Too many requests', null, usageHeaders(900, 100, 0)) }]);
    await expectKind(c.fetchOdds(NBA), 'rate-limited', 429);
    expect(c.health().status).toBe('degraded');
  });

  it('429 -> quota when remaining is 0 (header on the error)', async () => {
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(NOW);
    const { c, calls } = client([{ error: httpError(429, 'Too many requests', null, usageHeaders(0, 20_000, 0)) }, { data: [] }]);
    await expectKind(c.fetchOdds(NBA), 'quota', 429);
    expect(c.usage.remaining).toBe(0);
    // Quota opens the breaker for an hour.
    vi.setSystemTime(NOW + 59 * 60_000);
    await expectKind(c.fetchOdds(NBA), 'circuit-open');
    expect(calls).toHaveLength(1);
  });

  it('429 -> quota when the body mentions the usage limit', async () => {
    const { c } = client([{ error: httpError(429, 'You have reached your usage limit for this month') }]);
    await expectKind(c.fetchOdds(NBA), 'quota', 429);
  });

  it('429 -> quota when the last known remaining is 0', async () => {
    const { c } = client([{ data: [], headers: usageHeaders(0, 500, 3) }, { error: httpError(429, 'slow down') }]);
    await c.fetchOdds(NBA);
    await expectKind(c.fetchOdds(NBA), 'quota', 429);
  });

  it('404 and 422 -> unavailable and never trip the breaker', async () => {
    const { c, calls } = client([
      { error: httpError(422, '{"message":"Invalid sport"}') },
      { error: httpError(404, '{"message":"Unknown sport"}') },
      { error: httpError(422, '') },
      { error: httpError(404, '') },
      { error: httpError(422, '') },
      { error: httpError(404, '') },
      { data: [] },
    ]);
    for (let i = 0; i < 6; i++) {
      const e = await expectKind(c.fetchOdds(NBA), 'unavailable');
      expect([404, 422]).toContain(e.status);
    }
    expect(c.health().status).toBe('ok');
    expect(c.health().consecutiveFailures).toBe(0);
    await c.fetchOdds(NBA);
    expect(calls).toHaveLength(7);
  });

  it('unavailable clears a failure streak because the API answered', async () => {
    const { c } = client([{ error: httpError(503, 'down') }, { error: httpError(503, 'down') }, { error: httpError(422, '') }]);
    await expectKind(c.fetchOdds(NBA), 'network');
    await expectKind(c.fetchOdds(NBA), 'network');
    expect(c.health().consecutiveFailures).toBe(2);
    await expectKind(c.fetchOdds(NBA), 'unavailable', 422);
    expect(c.health().consecutiveFailures).toBe(0);
  });

  it('5xx, timeouts and transport errors -> network', async () => {
    const { c } = client([
      { error: httpError(502, 'Bad gateway') },
      { error: new TimeoutError(`https://api.the-odds-api.com/v4/sports/x/odds?apiKey=${API_KEY}`, 15_000) },
      { error: new TypeError('fetch failed') },
    ]);
    await expectKind(c.fetchOdds(NBA), 'network', 502);
    await expectKind(c.fetchOdds(NBA), 'network', null);
    await expectKind(c.fetchOdds(NBA), 'network', null);
  });

  it('bad payloads -> bad-payload', async () => {
    const { c } = client([
      { data: { message: 'Something unexpected' } },
      { error: new ParseError(`https://api.the-odds-api.com/v4/sports/x/odds?apiKey=${API_KEY}`, 'Unexpected token <') },
      { data: 'nope' },
    ]);
    const e = await expectKind(c.fetchOdds(NBA), 'bad-payload', 200);
    expect(e.message).toMatch(/Unexpected Odds API payload/);
    await expectKind(c.fetchOdds(NBA), 'bad-payload', null);
    await expectKind(c.fetchEvents(NBA), 'bad-payload', 200);
    expect(c.health().consecutiveFailures).toBe(3);
  });

  it('passes an abort through as AbortError without counting a failure', async () => {
    const { c } = client([{ error: new AbortError() }]);
    await expect(c.fetchOdds(NBA)).rejects.toBeInstanceOf(AbortError);
    expect(c.health().consecutiveFailures).toBe(0);
    expect(c.health().status).toBe('ok');
  });
});

describe('OddsApiClient — circuit breaker', () => {
  it('opens after 4 consecutive failures, refuses without requesting, then recovers', async () => {
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(NOW);
    let failing = true;
    const { c, calls } = client(() => (failing ? { error: httpError(503, 'Service Unavailable') } : { data: [], headers: usageHeaders(900, 100, 3) }));

    for (let i = 0; i < 4; i++) await expectKind(c.fetchOdds(NBA), 'network', 503);
    expect(calls).toHaveLength(4);

    const refused = await expectKind(c.fetchOdds(NBA), 'circuit-open', null);
    expect(refused.message).toMatch(/paused/);
    await expectKind(c.fetchEvents(NBA), 'circuit-open');
    expect(calls).toHaveLength(4);
    const h = c.health();
    expect(h.status).toBe('down');
    expect(h.consecutiveFailures).toBe(4);
    expect(h.detail).toMatch(/paused after errors until/);

    // After the 30 s cooldown one trial request is allowed; success closes the circuit.
    vi.setSystemTime(NOW + 30_001);
    failing = false;
    await c.fetchOdds(NBA);
    expect(calls).toHaveLength(5);
    expect(c.health().status).toBe('ok');
    await c.fetchOdds(NBA);
    expect(calls).toHaveLength(6);
  });

  it('a failed trial re-opens the circuit immediately', async () => {
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(NOW);
    const { c, calls } = client(() => ({ error: httpError(500, 'err') }));
    for (let i = 0; i < 4; i++) await expectKind(c.fetchOdds(NBA), 'network');
    vi.setSystemTime(NOW + 30_001);
    await expectKind(c.fetchOdds(NBA), 'network', 500);
    await expectKind(c.fetchOdds(NBA), 'circuit-open');
    expect(calls).toHaveLength(5);
  });

  it('an aborted trial does not wedge the breaker half-open', async () => {
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(NOW);
    const steps: Step[] = [
      ...Array.from({ length: 4 }, () => ({ error: httpError(500, 'err') })),
      { error: new AbortError() },
      { data: [] },
    ];
    const { c, calls } = client(steps);
    for (let i = 0; i < 4; i++) await expectKind(c.fetchOdds(NBA), 'network');
    vi.setSystemTime(NOW + 30_001);
    await expect(c.fetchOdds(NBA)).rejects.toBeInstanceOf(AbortError);
    await c.fetchOdds(NBA);
    expect(calls).toHaveLength(6);
  });
});

describe('OddsApiClient — health', () => {
  it('disabled without a key', () => {
    const h = new OddsApiClient(makeCfg({ apiKey: '' })).health();
    expect(h.name).toBe('The Odds API');
    expect(h.status).toBe('disabled');
    expect(h.detail).toMatch(/ODDS_API_KEY/);
  });

  it('ok after a success, with credits in the detail', async () => {
    const { c } = client([{ data: [], headers: usageHeaders(19_850, 150, 3) }], makeCfg({ books: TEN_BOOKS }));
    const before = c.health();
    expect(before.status).toBe('ok');
    expect(before.lastSuccess).toBeNull();
    expect(before.detail).toMatch(/credits remaining: unknown/);

    await c.fetchOdds(NBA);
    const h = c.health();
    expect(h).toMatchObject({ name: 'The Odds API', status: 'ok', lastSuccess: NOW, consecutiveFailures: 0 });
    expect(h.detail).toContain('19850 credits remaining');
    expect(h.detail).toContain('3 credits per odds call');
  });

  it('degraded after a recent failure, ok again after a success', async () => {
    const { c } = client([{ error: httpError(500, 'oops') }, { data: [], headers: usageHeaders(1000, 10, 3) }]);
    await expectKind(c.fetchOdds(NBA), 'network');
    let h = c.health();
    expect(h.status).toBe('degraded');
    expect(h.consecutiveFailures).toBe(1);
    expect(h.lastError).toMatch(/HTTP 500/);
    await c.fetchOdds(NBA);
    h = c.health();
    expect(h.status).toBe('ok');
    expect(h.consecutiveFailures).toBe(0);
  });

  it('degraded when credits are down to the reserve, down when exhausted', async () => {
    const { c } = client([{ data: [], headers: usageHeaders(150, 19_850, 3) }, { data: [], headers: usageHeaders(0, 20_000, 3) }]);
    await c.fetchOdds(NBA);
    expect(c.health().status).toBe('degraded');
    expect(c.health().detail).toMatch(/reserve/);
    await c.fetchOdds(NBA);
    expect(c.health().status).toBe('down');
    expect(c.health().detail).toMatch(/exhausted/);
  });

  it('down with an invalid key', async () => {
    const { c } = client([{ error: httpError(401, 'Unauthorized') }]);
    await expectKind(c.fetchOdds(NBA), 'invalid-key');
    const h = c.health();
    expect(h.status).toBe('down');
    expect(h.detail).toMatch(/ODDS_API_KEY/);
  });
});

describe('OddsApiClient — never leaks the API key', () => {
  it('scrubs the key from error messages, lastError and health detail', async () => {
    const leaky = `https://api.the-odds-api.com/v4/sports/basketball_nba/odds?apiKey=${API_KEY}&markets=h2h`;
    const { c } = client([
      { error: new TypeError(`fetch failed for ${leaky}`) },
      { error: new Error(`connect ECONNREFUSED while using key ${API_KEY}`) },
      { error: httpError(500, `internal error for key ${API_KEY}`) },
      { error: httpError(401, `key ${API_KEY} is invalid`) },
    ]);
    const errors: OddsApiError[] = [];
    errors.push(await expectKind(c.fetchOdds(NBA), 'network'));
    errors.push(await expectKind(c.fetchOdds(NBA), 'network'));
    errors.push(await expectKind(c.fetchOdds(NBA), 'network', 500));
    errors.push(await expectKind(c.fetchOdds(NBA), 'invalid-key', 401));
    for (const e of errors) {
      expect(e.message).not.toContain(API_KEY);
      expect(String(e.stack)).not.toContain(API_KEY);
    }
    const h = c.health();
    expect(JSON.stringify(h)).not.toContain(API_KEY);
    expect(h.lastError).toContain('***');
  });

  it('never writes the key to the logs', async () => {
    const writes: string[] = [];
    const out = vi.spyOn(process.stdout, 'write').mockImplementation((chunk: string | Uint8Array) => {
      writes.push(String(chunk));
      return true;
    });
    const err = vi.spyOn(process.stderr, 'write').mockImplementation((chunk: string | Uint8Array) => {
      writes.push(String(chunk));
      return true;
    });
    try {
      setLogLevel('debug');
      const { c } = client([
        { data: fixture('oddsapi-nba.json'), headers: usageHeaders(100, 1, 3) },
        { error: new TypeError(`fetch failed ${API_KEY}`) },
        { error: httpError(422, `bad ${API_KEY}`) },
      ]);
      await c.fetchOdds(NBA);
      await expectKind(c.fetchOdds(NBA), 'network');
      await expectKind(c.fetchOdds(EPL), 'unavailable');
    } finally {
      setLogLevel('error');
      out.mockRestore();
      err.mockRestore();
    }
    expect(writes.length).toBeGreaterThan(0);
    expect(writes.join('')).not.toContain(API_KEY);
  });
});

import * as fs from 'node:fs';
import * as http from 'node:http';
import * as net from 'node:net';
import * as os from 'node:os';
import * as path from 'node:path';
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { DashboardServer, type ServerDeps } from '../src/server/httpServer';
import type { PlaceBetInput } from '../src/server/betJournal';
import type { BetRecord, BetResult, BetSummary, DashboardState, Opportunity, RuntimeSettings } from '../src/types';
import { setLogLevel } from '../src/util/logger';

// ---------------------------------------------------------------------------------------------------------------
// Fakes (mirroring the real error classes by name only, like the server does)

class ValidationError extends Error {}

class UnknownBetError extends Error {
  constructor(id: string) {
    super(`Unknown bet id: ${id}`);
    this.name = 'UnknownBetError';
  }
}

const CSP =
  "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'";

function makeSettings(): RuntimeSettings {
  return {
    bankroll: 1000,
    kellyMultiplier: 0.25,
    maxStakePct: 0.02,
    maxStakeAbs: 100,
    maxDailyExposurePct: 0.1,
    minEvPrematch: 0.02,
    minEvLive: 0.03,
    watchEv: 0.01,
    enabledLeagues: ['NBA', 'NFL'],
    showArbs: true,
  };
}

function makeSummary(now = 0): BetSummary {
  return {
    totalBets: 0,
    pending: 0,
    staked: 0,
    profit: 0,
    roiPct: null,
    avgEvPct: null,
    avgClvPct: null,
    stakedToday: now,
  };
}

function makeOpp(overrides: Partial<Opportunity> = {}): Opportunity {
  return {
    id: 'demo:nba-1|ev|spread|home|-3.5',
    type: 'ev',
    eventId: 'demo:nba-1',
    league: 'NBA',
    eventName: 'Knicks @ Celtics',
    home: 'Celtics',
    away: 'Knicks',
    startTime: 1_700_000_000_000,
    isLive: true,
    kind: 'spread',
    side: 'home',
    line: -3.5,
    pick: 'Celtics -3.5',
    dkDecimal: 2.1,
    dkAmerican: 110,
    fairProb: 0.5,
    fairDecimal: 2,
    fairAmerican: 100,
    evPct: 0.05,
    minAcceptableAmerican: 104,
    kellyFraction: 0.01,
    stake: 10,
    confidence: 0.8,
    urgency: 'critical',
    urgencyScore: 80,
    verdict: 'BET_NOW',
    reasons: ['Fair +100 vs DraftKings +110 (EV +5.0%)'],
    sharpSource: 'pinnacle',
    sharpAgeSec: 5,
    dkAgeSec: 5,
    staleLine: false,
    firstSeen: 1,
    lastSeen: 2,
    status: 'active',
    expiresInSec: 45,
    dkUrl: null,
    ...overrides,
  };
}

function makeState(generatedAt: number, opportunities: Opportunity[] = [], detail?: string): DashboardState {
  return {
    generatedAt,
    opportunities,
    health: {
      startedAt: 0,
      now: generatedAt,
      demoMode: true,
      sources: [{ name: 'Demo', status: 'ok', lastSuccess: generatedAt, lastError: null, consecutiveFailures: 0, detail }],
      oddsApiCreditsRemaining: null,
      oddsApiCreditsUsed: null,
      eventsTracked: 1,
      liveEvents: 1,
      quotesTracked: 10,
      memoryMb: 50,
      lastEngineRunMs: generatedAt,
      engineRunDurationMs: 3,
    },
    settings: makeSettings(),
    betSummary: makeSummary(),
    remainingDailyExposure: 100,
  };
}

function makeJournal() {
  const placed: PlaceBetInput[] = [];
  const listLimits: Array<number | undefined> = [];
  const bets = new Map<string, BetRecord>();
  let nextId = 1;
  const journal: ServerDeps['journal'] = {
    place(input: PlaceBetInput): BetRecord {
      placed.push(input);
      const decimalTaken = input.americanTaken > 0 ? 1 + input.americanTaken / 100 : 1 + 100 / -input.americanTaken;
      const rec: BetRecord = {
        id: `bet-${nextId++}`,
        placedAt: 123,
        opportunityId: input.opportunityId,
        eventId: input.eventId,
        league: input.league,
        eventName: input.eventName,
        startTime: input.startTime,
        pick: input.pick,
        kind: input.kind,
        side: input.side,
        line: input.line,
        wasLive: input.wasLive,
        americanTaken: input.americanTaken,
        decimalTaken,
        stake: input.stake,
        fairProbAtPlace: input.fairProbAtPlace,
        evPctAtPlace: input.fairProbAtPlace * decimalTaken - 1,
        closingFairProb: null,
        result: 'pending',
        settledAt: null,
        profit: null,
      };
      if (input.notes !== undefined) rec.notes = input.notes;
      bets.set(rec.id, rec);
      return rec;
    },
    settle(id: string, result: Exclude<BetResult, 'pending'>): BetRecord {
      const prev = bets.get(id);
      if (!prev) throw new UnknownBetError(id);
      const profit = result === 'won' ? prev.stake * (prev.decimalTaken - 1) : result === 'lost' ? -prev.stake : 0;
      const next: BetRecord = { ...prev, result, settledAt: 456, profit };
      bets.set(id, next);
      return next;
    },
    list(limit?: number): BetRecord[] {
      listLimits.push(limit);
      return [...bets.values()].reverse().slice(0, limit ?? 500);
    },
    summary(now: number): BetSummary {
      return { ...makeSummary(now), totalBets: bets.size };
    },
  };
  return { journal, placed, listLimits, bets };
}

function makeSettingsStore() {
  let current = makeSettings();
  const copy = (): RuntimeSettings => ({ ...current, enabledLeagues: current.enabledLeagues.slice() });
  const store: ServerDeps['settings'] = {
    get: copy,
    update(patch: unknown): RuntimeSettings {
      if (typeof patch !== 'object' || patch === null || Array.isArray(patch)) {
        throw new ValidationError('Settings must be a JSON object');
      }
      const p = patch as Record<string, unknown>;
      for (const key of Object.keys(p)) {
        if (!(key in current)) throw new ValidationError(`Unknown setting "${key}"`);
      }
      if ('bankroll' in p && (typeof p.bankroll !== 'number' || p.bankroll < 1)) {
        throw new ValidationError('Bankroll (bankroll) must be between 1 and 100000000');
      }
      current = { ...current, ...(p as Partial<RuntimeSettings>) };
      return copy();
    },
  };
  return store;
}

// ---------------------------------------------------------------------------------------------------------------
// Harness

let rootDir = '';
let publicDir = '';
const running: DashboardServer[] = [];

beforeAll(() => {
  setLogLevel('error');
  rootDir = fs.mkdtempSync(path.join(os.tmpdir(), 'odds-http-test-'));
  publicDir = path.join(rootDir, 'public');
  fs.mkdirSync(publicDir);
  fs.writeFileSync(path.join(publicDir, 'index.html'), '<!doctype html><title>Odds Hub</title>');
  fs.writeFileSync(path.join(publicDir, 'app.js'), 'console.log("app v1");');
  fs.writeFileSync(path.join(publicDir, 'styles.css'), 'body{color:#fff}');
  fs.writeFileSync(path.join(publicDir, 'favicon.svg'), '<svg xmlns="http://www.w3.org/2000/svg"></svg>');
  fs.writeFileSync(path.join(publicDir, 'secret.txt'), 'not whitelisted');
  fs.writeFileSync(path.join(rootDir, 'package.json'), '{"secret":true}');
});

afterAll(() => {
  setLogLevel('info');
  fs.rmSync(rootDir, { recursive: true, force: true });
});

afterEach(async () => {
  vi.restoreAllMocks();
  await Promise.all(running.splice(0).map((s) => s.stop()));
});

interface Harness {
  server: DashboardServer;
  port: number;
  base: string;
  journal: ReturnType<typeof makeJournal>;
  onSettingsChanged: ReturnType<typeof vi.fn>;
  opp: Opportunity;
  clock: { t: number };
}

async function startServer(overrides: Partial<ServerDeps> = {}): Promise<Harness> {
  const journal = makeJournal();
  const opp = makeOpp();
  const clock = { t: 1_000_000 };
  const onSettingsChanged = vi.fn();
  const deps: ServerDeps = {
    host: '127.0.0.1',
    port: 0,
    user: 'admin',
    password: '',
    publicDir,
    getState: () => makeState(1, [opp]),
    settings: makeSettingsStore(),
    journal: journal.journal,
    findOpportunity: (id) => (id === opp.id ? opp : undefined),
    onSettingsChanged,
    now: () => clock.t,
    ...overrides,
  };
  const server = new DashboardServer(deps);
  running.push(server);
  const { port } = await server.start();
  return { server, port, base: `http://127.0.0.1:${port}`, journal, onSettingsChanged, opp, clock };
}

interface RawResponse {
  status: number;
  headers: http.IncomingHttpHeaders;
  body: string;
}

/** Raw node:http request: the path is sent verbatim (fetch would normalise `..` segments away). */
function rawRequest(
  port: number,
  opts: { method?: string; path: string; headers?: Record<string, string>; body?: string },
): Promise<RawResponse> {
  return new Promise((resolve, reject) => {
    const req = http.request(
      { host: '127.0.0.1', port, method: opts.method ?? 'GET', path: opts.path, headers: opts.headers, agent: false },
      (res) => {
        const chunks: Buffer[] = [];
        res.on('data', (c: Buffer) => chunks.push(c));
        res.on('end', () => resolve({ status: res.statusCode ?? 0, headers: res.headers, body: Buffer.concat(chunks).toString('utf8') }));
        res.on('error', reject);
      },
    );
    req.on('error', reject);
    req.end(opts.body);
  });
}

function basic(user: string, pass: string): string {
  return `Basic ${Buffer.from(`${user}:${pass}`).toString('base64')}`;
}

async function waitUntil(pred: () => boolean, timeoutMs = 3000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (!pred()) {
    if (Date.now() > deadline) throw new Error('waitUntil: condition not met in time');
    await new Promise((r) => setTimeout(r, 5));
  }
}

interface SseConn {
  status: number;
  headers: http.IncomingHttpHeaders;
  events: Array<{ event: string; data: string }>;
  comments: string[];
  raw: string;
  ended: boolean;
  close(): void;
}

function openSse(port: number, headers: Record<string, string> = {}): Promise<SseConn> {
  return new Promise((resolve, reject) => {
    const req = http.get({ host: '127.0.0.1', port, path: '/api/stream', headers, agent: false }, (res) => {
      const conn: SseConn = {
        status: res.statusCode ?? 0,
        headers: res.headers,
        events: [],
        comments: [],
        raw: '',
        ended: false,
        close: () => req.destroy(),
      };
      let buf = '';
      res.setEncoding('utf8');
      res.on('data', (chunk: string) => {
        conn.raw += chunk;
        buf += chunk;
        let idx = buf.indexOf('\n\n');
        while (idx >= 0) {
          const block = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          let event = 'message';
          const data: string[] = [];
          for (const line of block.split('\n')) {
            if (line.startsWith(':')) conn.comments.push(line.slice(1).trim());
            else if (line.startsWith('event:')) event = line.slice(6).trim();
            else if (line.startsWith('data:')) data.push(line.slice(5).replace(/^ /, ''));
          }
          if (data.length > 0) conn.events.push({ event, data: data.join('\n') });
          idx = buf.indexOf('\n\n');
        }
      });
      res.on('error', () => {
        conn.ended = true;
      });
      res.on('close', () => {
        conn.ended = true;
      });
      resolve(conn);
    });
    req.on('error', reject);
  });
}

function expectSecurityHeaders(headers: Headers | http.IncomingHttpHeaders): void {
  const get = (name: string): string | null =>
    headers instanceof Headers ? headers.get(name) : ((headers[name.toLowerCase()] as string | undefined) ?? null);
  expect(get('x-content-type-options')).toBe('nosniff');
  expect(get('x-frame-options')).toBe('DENY');
  expect(get('referrer-policy')).toBe('no-referrer');
  expect(get('content-security-policy')).toBe(CSP);
}

const JSON_HEADERS = { 'Content-Type': 'application/json' };

// ---------------------------------------------------------------------------------------------------------------

describe('DashboardServer: health and auth', () => {
  it('serves /healthz without auth even when a password is set', async () => {
    const h = await startServer({ password: 'secret' });
    h.clock.t += 42_000;
    const res = await fetch(`${h.base}/healthz`);
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ ok: true, uptimeSec: 42 });
    expectSecurityHeaders(res.headers);
  });

  it('rejects missing and wrong credentials with 401 + WWW-Authenticate', async () => {
    const h = await startServer({ password: 'secret' });
    const none = await fetch(`${h.base}/api/state`);
    expect(none.status).toBe(401);
    expect(none.headers.get('www-authenticate')).toMatch(/^Basic realm=/);
    expect(await none.json()).toEqual({ error: 'unauthorized' });
    expectSecurityHeaders(none.headers);

    const wrongPass = await fetch(`${h.base}/api/state`, { headers: { Authorization: basic('admin', 'nope') } });
    expect(wrongPass.status).toBe(401);
    const wrongUser = await fetch(`${h.base}/api/state`, { headers: { Authorization: basic('root', 'secret') } });
    expect(wrongUser.status).toBe(401);
    const garbage = await fetch(`${h.base}/api/state`, { headers: { Authorization: 'Basic !!!notbase64' } });
    expect(garbage.status).toBe(401);
    const bearer = await fetch(`${h.base}/api/state`, { headers: { Authorization: 'Bearer secret' } });
    expect(bearer.status).toBe(401);
    const staticNoAuth = await fetch(`${h.base}/`);
    expect(staticNoAuth.status).toBe(401);
    const streamNoAuth = await rawRequest(h.port, { path: '/api/stream' });
    expect(streamNoAuth.status).toBe(401);
    expect(h.server.clientCount()).toBe(0);

    const ok = await fetch(`${h.base}/api/state`, { headers: { Authorization: basic('admin', 'secret') } });
    expect(ok.status).toBe(200);
    const okStatic = await fetch(`${h.base}/`, { headers: { Authorization: basic('admin', 'secret') } });
    expect(okStatic.status).toBe(200);
  });

  it('accepts passwords containing colons', async () => {
    const h = await startServer({ password: 'a:b:c' });
    const ok = await fetch(`${h.base}/api/settings`, { headers: { Authorization: basic('admin', 'a:b:c') } });
    expect(ok.status).toBe(200);
  });

  it('requires no auth when the password is empty', async () => {
    const h = await startServer({ password: '' });
    const res = await fetch(`${h.base}/api/state`);
    expect(res.status).toBe(200);
  });
});

describe('DashboardServer: static files', () => {
  it('serves whitelisted files with correct content types and no-cache', async () => {
    const h = await startServer();
    const cases: Array<[string, string, string]> = [
      ['/', 'text/html', '<!doctype html><title>Odds Hub</title>'],
      ['/index.html', 'text/html', '<!doctype html><title>Odds Hub</title>'],
      ['/app.js', 'text/javascript', 'console.log("app v1");'],
      ['/styles.css', 'text/css', 'body{color:#fff}'],
      ['/favicon.svg', 'image/svg+xml', '<svg xmlns="http://www.w3.org/2000/svg"></svg>'],
    ];
    for (const [p, type, body] of cases) {
      const res = await fetch(`${h.base}${p}`);
      expect(res.status, p).toBe(200);
      expect(res.headers.get('content-type'), p).toContain(type);
      expect(res.headers.get('cache-control'), p).toBe('no-cache');
      expectSecurityHeaders(res.headers);
      expect(await res.text(), p).toBe(body);
    }
  });

  it('answers HEAD without a body and rejects other methods', async () => {
    const h = await startServer();
    const head = await rawRequest(h.port, { method: 'HEAD', path: '/app.js' });
    expect(head.status).toBe(200);
    expect(head.headers['content-length']).toBe(String('console.log("app v1");'.length));
    expect(head.body).toBe('');
    const post = await rawRequest(h.port, { method: 'POST', path: '/app.js', headers: JSON_HEADERS, body: '{}' });
    expect(post.status).toBe(405);
    expect(post.headers.allow).toBe('GET, HEAD');
  });

  it('never serves non-whitelisted files or traversal paths', async () => {
    const h = await startServer();
    const paths = [
      '/secret.txt',
      '/package.json',
      '/../package.json',
      '/%2e%2e/package.json',
      '/%2E%2E/%2e%2e/etc/passwd',
      '/..%2fpackage.json',
      '/public/../package.json',
      '/app.js/../../package.json',
      '/%2e%2e/app.js',
      '//etc/passwd',
      '/app.js%00.txt',
      '/APP.JS',
      '/index.html/',
    ];
    for (const p of paths) {
      const res = await rawRequest(h.port, { path: p });
      expect(res.status, p).toBe(404);
      expect(res.body, p).not.toContain('secret');
      expect(res.headers['content-type'], p).toContain('application/json');
      expectSecurityHeaders(res.headers);
    }
  });

  it('picks up changed files (mtime check)', async () => {
    const h = await startServer();
    const file = path.join(publicDir, 'app.js');
    expect(await (await fetch(`${h.base}/app.js`)).text()).toBe('console.log("app v1");');
    fs.writeFileSync(file, 'console.log("app v2 longer");');
    const future = new Date(Date.now() + 60_000);
    fs.utimesSync(file, future, future);
    expect(await (await fetch(`${h.base}/app.js`)).text()).toBe('console.log("app v2 longer");');
    fs.writeFileSync(file, 'console.log("app v1");');
  });

  it('returns 404 when a whitelisted file is missing from publicDir', async () => {
    const emptyDir = fs.mkdtempSync(path.join(rootDir, 'empty-'));
    const h = await startServer({ publicDir: emptyDir });
    const res = await fetch(`${h.base}/`);
    expect(res.status).toBe(404);
    expect(await res.json()).toEqual({ error: 'not found' });
  });
});

describe('DashboardServer: API routes', () => {
  it('GET /api/state returns the current dashboard state', async () => {
    const state = makeState(777, [makeOpp()]);
    const h = await startServer({ getState: () => state });
    const res = await fetch(`${h.base}/api/state`);
    expect(res.status).toBe(200);
    expect(res.headers.get('content-type')).toContain('application/json');
    expect(res.headers.get('cache-control')).toBe('no-store');
    expectSecurityHeaders(res.headers);
    expect(await res.json()).toEqual(state);
  });

  it('unknown routes -> 404 JSON; wrong method on a known route -> 405', async () => {
    const h = await startServer();
    const missing = await fetch(`${h.base}/api/nope`);
    expect(missing.status).toBe(404);
    expect(await missing.json()).toEqual({ error: 'not found' });
    expectSecurityHeaders(missing.headers);
    const wrong = await fetch(`${h.base}/api/state`, { method: 'DELETE' });
    expect(wrong.status).toBe(405);
  });

  it('handler errors -> 500 {error:"internal"} and the server keeps working', async () => {
    let fail = true;
    const h = await startServer({
      getState: () => {
        if (fail) throw new Error('boom');
        return makeState(1);
      },
    });
    const res = await fetch(`${h.base}/api/state`);
    expect(res.status).toBe(500);
    expect(await res.json()).toEqual({ error: 'internal' });
    expectSecurityHeaders(res.headers);
    fail = false;
    expect((await fetch(`${h.base}/api/state`)).status).toBe(200);
  });

  it('GET /api/bets returns bets + summary and honours ?limit', async () => {
    const h = await startServer();
    h.clock.t = 5_555;
    const res = await fetch(`${h.base}/api/bets?limit=7`);
    expect(res.status).toBe(200);
    const body = (await res.json()) as { bets: BetRecord[]; summary: BetSummary };
    expect(body.bets).toEqual([]);
    expect(body.summary.stakedToday).toBe(5_555);
    await fetch(`${h.base}/api/bets?limit=abc`);
    await fetch(`${h.base}/api/bets?limit=999999`);
    await fetch(`${h.base}/api/bets`);
    expect(h.journal.listLimits).toEqual([7, 500, 5000, 500]);
  });
});

describe('DashboardServer: POST /api/bets', () => {
  it('maps the opportunity into PlaceBetInput and returns 201', async () => {
    const h = await startServer();
    const res = await fetch(`${h.base}/api/bets`, {
      method: 'POST',
      headers: JSON_HEADERS,
      body: JSON.stringify({ opportunityId: h.opp.id, stake: 25, americanTaken: 105, notes: '  got +105  ' }),
    });
    expect(res.status).toBe(201);
    expectSecurityHeaders(res.headers);
    const rec = (await res.json()) as BetRecord;
    expect(rec.id).toBe('bet-1');
    expect(rec.stake).toBe(25);
    expect(h.journal.placed).toEqual([
      {
        opportunityId: h.opp.id,
        eventId: h.opp.eventId,
        league: 'NBA',
        eventName: 'Knicks @ Celtics',
        startTime: h.opp.startTime,
        pick: 'Celtics -3.5',
        kind: 'spread',
        side: 'home',
        line: -3.5,
        wasLive: true,
        americanTaken: 105,
        stake: 25,
        fairProbAtPlace: 0.5,
        notes: 'got +105',
      },
    ]);
  });

  it('accepts a matching Origin, omits empty notes and a charset in Content-Type', async () => {
    const h = await startServer();
    const res = await rawRequest(h.port, {
      method: 'POST',
      path: '/api/bets',
      headers: { 'Content-Type': 'application/json; charset=utf-8', Origin: `http://127.0.0.1:${h.port}` },
      body: JSON.stringify({ opportunityId: h.opp.id, stake: '12', americanTaken: '-120', notes: '   ' }),
    });
    expect(res.status).toBe(201);
    expect(h.journal.placed[0].stake).toBe(12);
    expect(h.journal.placed[0].americanTaken).toBe(-120);
    expect('notes' in h.journal.placed[0]).toBe(false);
  });

  it('404 for an unknown opportunity', async () => {
    const h = await startServer();
    const res = await fetch(`${h.base}/api/bets`, {
      method: 'POST',
      headers: JSON_HEADERS,
      body: JSON.stringify({ opportunityId: 'does-not-exist', stake: 10, americanTaken: 110 }),
    });
    expect(res.status).toBe(404);
    expect(((await res.json()) as { error: string }).error).toMatch(/not found/i);
    expect(h.journal.placed).toHaveLength(0);
  });

  it('415 for a wrong content type, 400 for bad JSON or invalid fields', async () => {
    const h = await startServer();
    const post = (body: string, headers: Record<string, string> = JSON_HEADERS) =>
      fetch(`${h.base}/api/bets`, { method: 'POST', headers, body });

    const wrongType = await post(JSON.stringify({ opportunityId: h.opp.id, stake: 1, americanTaken: 110 }), {
      'Content-Type': 'text/plain',
    });
    expect(wrongType.status).toBe(415);
    const formType = await post('a=b', { 'Content-Type': 'application/x-www-form-urlencoded' });
    expect(formType.status).toBe(415);

    const badJson = await post('{"opportunityId": ');
    expect(badJson.status).toBe(400);
    expect(await badJson.json()).toEqual({ error: 'Invalid JSON body' });

    expect((await post('[1,2]')).status).toBe(400);
    expect((await post('null')).status).toBe(400);
    expect((await post(JSON.stringify({ stake: 10, americanTaken: 110 }))).status).toBe(400);
    expect((await post(JSON.stringify({ opportunityId: h.opp.id, stake: -5, americanTaken: 110 }))).status).toBe(400);
    expect((await post(JSON.stringify({ opportunityId: h.opp.id, stake: 'x', americanTaken: 110 }))).status).toBe(400);
    expect((await post(JSON.stringify({ opportunityId: h.opp.id, stake: 10, americanTaken: 50 }))).status).toBe(400);
    expect((await post(JSON.stringify({ opportunityId: h.opp.id, stake: 10, americanTaken: 110, notes: 5 }))).status).toBe(400);
    expect(h.journal.placed).toHaveLength(0);
  });

  it('maps a journal ValidationError to 400 with its message', async () => {
    const h = await startServer({
      journal: {
        ...makeJournal().journal,
        place: () => {
          throw new ValidationError('Stake is unrealistically large');
        },
      },
    });
    const res = await fetch(`${h.base}/api/bets`, {
      method: 'POST',
      headers: JSON_HEADERS,
      body: JSON.stringify({ opportunityId: h.opp.id, stake: 10, americanTaken: 110 }),
    });
    expect(res.status).toBe(400);
    expect(await res.json()).toEqual({ error: 'Stake is unrealistically large' });
  });

  it('413 for bodies over 32 KB', async () => {
    const h = await startServer();
    const res = await fetch(`${h.base}/api/bets`, {
      method: 'POST',
      headers: JSON_HEADERS,
      body: JSON.stringify({ opportunityId: h.opp.id, stake: 10, americanTaken: 110, notes: 'x'.repeat(40 * 1024) }),
    });
    expect(res.status).toBe(413);
    expectSecurityHeaders(res.headers);
    expect(h.journal.placed).toHaveLength(0);
    // Exactly at the limit is still accepted by the size check.
    const base = JSON.stringify({ opportunityId: h.opp.id, stake: 10, americanTaken: 110, notes: '' });
    const padded = JSON.stringify({
      opportunityId: h.opp.id,
      stake: 10,
      americanTaken: 110,
      notes: 'y'.repeat(32 * 1024 - base.length),
    });
    expect(Buffer.byteLength(padded)).toBe(32 * 1024);
    const atLimit = await fetch(`${h.base}/api/bets`, { method: 'POST', headers: JSON_HEADERS, body: padded });
    expect(atLimit.status).toBe(201);
  });

  it('403 when the Origin header does not match Host', async () => {
    const h = await startServer();
    const body = JSON.stringify({ opportunityId: h.opp.id, stake: 10, americanTaken: 110 });
    for (const origin of ['http://evil.example', `http://localhost:${h.port}`, 'null', `http://127.0.0.1:${h.port + 1}`]) {
      const res = await rawRequest(h.port, {
        method: 'POST',
        path: '/api/bets',
        headers: { ...JSON_HEADERS, Origin: origin },
        body,
      });
      expect(res.status, origin).toBe(403);
      expectSecurityHeaders(res.headers);
    }
    const put = await rawRequest(h.port, {
      method: 'PUT',
      path: '/api/settings',
      headers: { ...JSON_HEADERS, Origin: 'https://evil.example' },
      body: JSON.stringify({ bankroll: 5 }),
    });
    expect(put.status).toBe(403);
    expect(h.journal.placed).toHaveLength(0);
  });

  it('survives a client that aborts mid-body', async () => {
    const h = await startServer();
    const sock = net.connect(h.port, '127.0.0.1');
    await new Promise<void>((resolve) => sock.once('connect', () => resolve()));
    sock.write(
      'POST /api/bets HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\nContent-Length: 1000\r\n\r\n{"oppor',
    );
    await new Promise((r) => setTimeout(r, 20));
    sock.destroy();
    await new Promise((r) => setTimeout(r, 20));
    expect((await fetch(`${h.base}/healthz`)).status).toBe(200);
    expect(h.journal.placed).toHaveLength(0);
  });

  it('answers malformed HTTP with 400 and keeps running', async () => {
    const h = await startServer();
    const sock = net.connect(h.port, '127.0.0.1');
    let received = '';
    sock.setEncoding('utf8');
    sock.on('data', (d: string) => {
      received += d;
    });
    const closed = new Promise<void>((resolve) => sock.once('close', () => resolve()));
    sock.write('THIS IS NOT HTTP\r\n\r\n');
    await closed;
    expect(received).toMatch(/^HTTP\/1\.1 400/);
    expect((await fetch(`${h.base}/healthz`)).status).toBe(200);
  });
});

describe('DashboardServer: settle', () => {
  it('settles a bet, validates the result and 404s unknown ids', async () => {
    const h = await startServer();
    const placed = await fetch(`${h.base}/api/bets`, {
      method: 'POST',
      headers: JSON_HEADERS,
      body: JSON.stringify({ opportunityId: h.opp.id, stake: 20, americanTaken: 150 }),
    });
    const rec = (await placed.json()) as BetRecord;

    const ok = await fetch(`${h.base}/api/bets/${rec.id}/settle`, {
      method: 'POST',
      headers: JSON_HEADERS,
      body: JSON.stringify({ result: 'won' }),
    });
    expect(ok.status).toBe(200);
    const settled = (await ok.json()) as BetRecord;
    expect(settled.result).toBe('won');
    expect(settled.profit).toBeCloseTo(30, 9);

    const badResult = await fetch(`${h.base}/api/bets/${rec.id}/settle`, {
      method: 'POST',
      headers: JSON_HEADERS,
      body: JSON.stringify({ result: 'pending' }),
    });
    expect(badResult.status).toBe(400);

    const unknown = await fetch(`${h.base}/api/bets/nope/settle`, {
      method: 'POST',
      headers: JSON_HEADERS,
      body: JSON.stringify({ result: 'lost' }),
    });
    expect(unknown.status).toBe(404);
    expect(((await unknown.json()) as { error: string }).error).toMatch(/unknown bet/i);

    const weirdId = await rawRequest(h.port, {
      method: 'POST',
      path: '/api/bets/%00%2F/settle',
      headers: JSON_HEADERS,
      body: JSON.stringify({ result: 'lost' }),
    });
    expect(weirdId.status).toBe(404);

    const wrongType = await fetch(`${h.base}/api/bets/${rec.id}/settle`, {
      method: 'POST',
      headers: { 'Content-Type': 'text/plain' },
      body: JSON.stringify({ result: 'lost' }),
    });
    expect(wrongType.status).toBe(415);

    const wrongMethod = await fetch(`${h.base}/api/bets/${rec.id}/settle`);
    expect(wrongMethod.status).toBe(405);
  });
});

describe('DashboardServer: settings', () => {
  it('GET returns settings; PUT applies a patch and calls onSettingsChanged', async () => {
    const h = await startServer();
    const get = await fetch(`${h.base}/api/settings`);
    expect(get.status).toBe(200);
    expect(await get.json()).toEqual(makeSettings());

    const put = await fetch(`${h.base}/api/settings`, {
      method: 'PUT',
      headers: JSON_HEADERS,
      body: JSON.stringify({ bankroll: 2500, showArbs: false }),
    });
    expect(put.status).toBe(200);
    const updated = (await put.json()) as RuntimeSettings;
    expect(updated.bankroll).toBe(2500);
    expect(updated.showArbs).toBe(false);
    expect(h.onSettingsChanged).toHaveBeenCalledTimes(1);
    expect(h.onSettingsChanged).toHaveBeenCalledWith(updated);
    expect(((await (await fetch(`${h.base}/api/settings`)).json()) as RuntimeSettings).bankroll).toBe(2500);
  });

  it('PUT with a ValidationError -> 400 {error} and no change notification', async () => {
    const h = await startServer();
    const bad = await fetch(`${h.base}/api/settings`, {
      method: 'PUT',
      headers: JSON_HEADERS,
      body: JSON.stringify({ bankroll: 0 }),
    });
    expect(bad.status).toBe(400);
    expect(await bad.json()).toEqual({ error: 'Bankroll (bankroll) must be between 1 and 100000000' });
    const unknownKey = await fetch(`${h.base}/api/settings`, {
      method: 'PUT',
      headers: JSON_HEADERS,
      body: JSON.stringify({ hack: true }),
    });
    expect(unknownKey.status).toBe(400);
    const notObject = await fetch(`${h.base}/api/settings`, { method: 'PUT', headers: JSON_HEADERS, body: '"x"' });
    expect(notObject.status).toBe(400);
    const badJson = await fetch(`${h.base}/api/settings`, { method: 'PUT', headers: JSON_HEADERS, body: '{bad' });
    expect(badJson.status).toBe(400);
    expect(h.onSettingsChanged).not.toHaveBeenCalled();
  });

  it('a throwing onSettingsChanged listener does not fail the request', async () => {
    const h = await startServer({
      onSettingsChanged: () => {
        throw new Error('listener broke');
      },
    });
    const put = await fetch(`${h.base}/api/settings`, {
      method: 'PUT',
      headers: JSON_HEADERS,
      body: JSON.stringify({ bankroll: 3000 }),
    });
    expect(put.status).toBe(200);
  });

  it('non-validation errors from the store -> 500 internal', async () => {
    const h = await startServer({
      settings: {
        get: makeSettings,
        update: () => {
          throw new Error('EACCES: disk says no');
        },
      },
    });
    const put = await fetch(`${h.base}/api/settings`, {
      method: 'PUT',
      headers: JSON_HEADERS,
      body: JSON.stringify({ bankroll: 3000 }),
    });
    expect(put.status).toBe(500);
    expect(await put.json()).toEqual({ error: 'internal' });
  });
});

describe('DashboardServer: SSE stream', () => {
  it('sends the current state immediately, then broadcasts, with SSE headers', async () => {
    const initial = makeState(1, [makeOpp()]);
    const h = await startServer({ getState: () => initial });
    const conn = await openSse(h.port);
    expect(conn.status).toBe(200);
    expect(conn.headers['content-type']).toContain('text/event-stream');
    expect(conn.headers['cache-control']).toContain('no-cache');
    expect(conn.headers['x-accel-buffering']).toBe('no');
    expectSecurityHeaders(conn.headers);

    await waitUntil(() => conn.events.length >= 1);
    expect(conn.events[0].event).toBe('state');
    expect(JSON.parse(conn.events[0].data)).toEqual(initial);
    expect(h.server.clientCount()).toBe(1);

    const next = makeState(2);
    h.server.broadcast(next);
    await waitUntil(() => conn.events.length >= 2);
    expect(conn.events[1].event).toBe('state');
    expect(JSON.parse(conn.events[1].data)).toEqual(next);
    conn.close();
  });

  it('broadcasts to every connected client', async () => {
    const h = await startServer();
    const a = await openSse(h.port);
    const b = await openSse(h.port);
    await waitUntil(() => a.events.length === 1 && b.events.length === 1);
    h.server.broadcast(makeState(99));
    await waitUntil(() => a.events.length === 2 && b.events.length === 2);
    expect((JSON.parse(a.events[1].data) as DashboardState).generatedAt).toBe(99);
    expect((JSON.parse(b.events[1].data) as DashboardState).generatedAt).toBe(99);
    a.close();
    b.close();
  });

  it('sends a ": ping" heartbeat comment', async () => {
    const h = await startServer({ heartbeatMs: 20 });
    const conn = await openSse(h.port);
    await waitUntil(() => conn.comments.includes('ping'));
    expect(conn.raw).toContain('\n: ping\n\n');
    conn.close();
  });

  it('removes a client when it disconnects', async () => {
    const h = await startServer();
    const conn = await openSse(h.port);
    await waitUntil(() => h.server.clientCount() === 1);
    conn.close();
    await waitUntil(() => h.server.clientCount() === 0);
    // Broadcasting with no clients is a no-op.
    h.server.broadcast(makeState(3));
    expect(h.server.clientCount()).toBe(0);
  });

  it('allows at most 25 clients and answers 503 beyond that', async () => {
    const h = await startServer();
    const conns: SseConn[] = [];
    for (let i = 0; i < 25; i++) conns.push(await openSse(h.port));
    expect(conns.every((c) => c.status === 200)).toBe(true);
    await waitUntil(() => h.server.clientCount() === 25);

    const over = await rawRequest(h.port, { path: '/api/stream' });
    expect(over.status).toBe(503);
    expect(JSON.parse(over.body)).toEqual({ error: 'too many live connections' });
    expectSecurityHeaders(over.headers);
    expect(h.server.clientCount()).toBe(25);

    conns[0].close();
    await waitUntil(() => h.server.clientCount() === 24);
    const again = await openSse(h.port);
    expect(again.status).toBe(200);
    await waitUntil(() => h.server.clientCount() === 25);
    for (const c of [...conns, again]) c.close();
    await waitUntil(() => h.server.clientCount() === 0);
  });

  it('drops a client whose socket stays backed up past the stall limit', async () => {
    const h = await startServer({ sseStallMs: 60_000 });
    const sock = net.connect(h.port, '127.0.0.1');
    await new Promise<void>((resolve) => sock.once('connect', () => resolve()));
    // Never read from this socket: the kernel buffers fill up and the server-side response backs up.
    sock.write('GET /api/stream HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n');
    await waitUntil(() => h.server.clientCount() === 1);
    const big = makeState(5, [], 'x'.repeat(1024 * 1024));
    for (let i = 0; i < 50 && h.server.clientCount() > 0; i++) {
      h.server.broadcast(big);
      h.clock.t += 61_000;
      await new Promise((r) => setImmediate(r));
    }
    expect(h.server.clientCount()).toBe(0);
    sock.destroy();
  });

  it('drops a backed-up client that buffers more than the byte cap', async () => {
    const h = await startServer({ sseMaxBufferBytes: 64 * 1024 });
    const sock = net.connect(h.port, '127.0.0.1');
    await new Promise<void>((resolve) => sock.once('connect', () => resolve()));
    sock.write('GET /api/stream HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n');
    await waitUntil(() => h.server.clientCount() === 1);
    const big = makeState(6, [], 'y'.repeat(1024 * 1024));
    for (let i = 0; i < 50 && h.server.clientCount() > 0; i++) {
      h.server.broadcast(big);
      await new Promise((r) => setImmediate(r));
    }
    expect(h.server.clientCount()).toBe(0);
    sock.destroy();
  });

  it('a slow client that catches up receives the latest state', async () => {
    const h = await startServer();
    const sock = net.connect(h.port, '127.0.0.1');
    await new Promise<void>((resolve) => sock.once('connect', () => resolve()));
    sock.write('GET /api/stream HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n');
    await waitUntil(() => h.server.clientCount() === 1);
    const big = makeState(7, [], 'z'.repeat(1024 * 1024));
    for (let i = 0; i < 4; i++) h.server.broadcast(big);
    h.server.broadcast(makeState(424242));
    expect(h.server.clientCount()).toBe(1);

    let received = '';
    sock.setEncoding('utf8');
    sock.on('data', (d: string) => {
      received += d;
    });
    await waitUntil(() => received.includes('"generatedAt":424242'), 5000);
    expect(h.server.clientCount()).toBe(1);
    sock.destroy();
    await waitUntil(() => h.server.clientCount() === 0);
  });
});

describe('DashboardServer: lifecycle', () => {
  it('stop() closes SSE clients and resolves; the port is released', async () => {
    const h = await startServer();
    const conn = await openSse(h.port);
    await waitUntil(() => conn.events.length === 1);
    const keepAlive = await fetch(`${h.base}/api/state`);
    expect(keepAlive.status).toBe(200);
    await keepAlive.text();

    await h.server.stop();
    expect(h.server.clientCount()).toBe(0);
    await waitUntil(() => conn.ended);
    await expect(rawRequest(h.port, { path: '/healthz' })).rejects.toThrow();
    // Idempotent.
    await h.server.stop();
  });

  it('stop() before start() resolves; start() twice rejects', async () => {
    const idle = new DashboardServer({
      host: '127.0.0.1',
      port: 0,
      user: '',
      password: '',
      publicDir,
      getState: () => makeState(1),
      settings: makeSettingsStore(),
      journal: makeJournal().journal,
      findOpportunity: () => undefined,
    });
    await expect(idle.stop()).resolves.toBeUndefined();

    const h = await startServer();
    await expect(h.server.start()).rejects.toThrow(/already started/);
  });

  it('start() rejects when the port is taken', async () => {
    const h = await startServer();
    const clash = new DashboardServer({
      host: '127.0.0.1',
      port: h.port,
      user: '',
      password: '',
      publicDir,
      getState: () => makeState(1),
      settings: makeSettingsStore(),
      journal: makeJournal().journal,
      findOpportunity: () => undefined,
    });
    await expect(clash.start()).rejects.toThrow(/EADDRINUSE/);
    await clash.stop();
  });
});

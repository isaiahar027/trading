import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { loadConfig } from '../src/config';
import type { AppConfig } from '../src/config';
import { MarketStore } from '../src/engine/marketStore';
import { BetJournal } from '../src/server/betJournal';
import { closingFairProb, closingLine, closingQuotes, remainingExposure, shiftLineProb, startApp } from '../src/index';
import type { RunningApp } from '../src/index';
import type { Quote, RawEvent, RuntimeSettings } from '../src/types';
import type { FetchJsonOptions, FetchJsonResult, fetchJson } from '../src/util/http';

const SEC = 1000;
const MIN = 60 * SEC;
const T = Date.UTC(2026, 9, 28, 0, 0, 0); // start time of the test event
const PUBLIC_DIR = path.resolve(__dirname, '..', 'public');
const FIXTURE = path.resolve(__dirname, 'fixtures', 'oddsapi-nba.json');

const tmpDirs: string[] = [];
const apps: RunningApp[] = [];

function tmpDir(): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'odds-hub-index-'));
  tmpDirs.push(dir);
  return dir;
}

afterEach(async () => {
  while (apps.length > 0) await apps.pop()?.stop();
  while (tmpDirs.length > 0) fs.rmSync(tmpDirs.pop() as string, { recursive: true, force: true });
});

function config(env: Record<string, string>, argv: string[] = []): AppConfig {
  const cfg = loadConfig({ DATA_DIR: tmpDir(), LOG_LEVEL: 'warn', ...env }, argv);
  cfg.server.port = 0; // any free port
  return cfg;
}

async function run(cfg: AppConfig, opts: Parameters<typeof startApp>[1] = {}): Promise<RunningApp> {
  const app = await startApp(cfg, { publicDir: PUBLIC_DIR, ...opts });
  apps.push(app);
  return app;
}

async function waitFor(check: () => boolean, timeoutMs = 5_000): Promise<void> {
  const until = Date.now() + timeoutMs;
  while (!check()) {
    if (Date.now() > until) throw new Error('condition not met in time');
    await new Promise((r) => setTimeout(r, 25));
  }
}

// ---------------------------------------------------------------------------------------------------------------

describe('remainingExposure', () => {
  const s = { bankroll: 1000, maxDailyExposurePct: 0.15 } as RuntimeSettings;
  it('is the daily cap minus what was logged today, never negative, in cents', () => {
    expect(remainingExposure(s, 0)).toBe(150);
    expect(remainingExposure(s, 40.255)).toBe(109.75);
    expect(remainingExposure(s, 500)).toBe(0);
    expect(remainingExposure(s, Number.NaN)).toBe(150);
  });
});

describe('closing line (CLV) capture', () => {
  const event = (isLive: boolean): RawEvent => ({
    source: 'odds-api',
    sourceEventId: 'g1',
    league: 'NBA',
    home: 'Celtics',
    away: 'Knicks',
    startTime: T,
    isLive,
  });
  const ml = (book: string, home: number, away: number, observedAt: number, updatedAt: number): Quote[] =>
    (['home', 'away'] as const).map((side, i) => ({
      book,
      source: 'odds-api',
      sourceEventId: 'g1',
      kind: 'moneyline',
      side,
      line: null,
      decimal: i === 0 ? home : away,
      suspended: false,
      isMainLine: true,
      observedAt,
      bookUpdatedAt: updatedAt,
    }));
  const ingest = (store: MarketStore, fetchedAt: number, isLive: boolean, quotes: Quote[]): void => {
    store.ingest({ source: 'odds-api', league: 'NBA', fetchedAt, events: [event(isLive)], quotes, complete: true, books: ['pinnacle', 'draftkings'] });
  };
  const cfg = loadConfig({ DEVIG_METHOD: 'multiplicative' }, []);
  const bet = { eventId: 'odds-api:g1', league: 'NBA', kind: 'moneyline' as const, side: 'home' as const, line: null, startTime: T };

  it('uses the prices in effect at the start, not the live prices polled afterwards', () => {
    const store = new MarketStore();
    ingest(store, T - 40 * MIN, false, [...ml('pinnacle', 1.9, 2.0, T - 40 * MIN, T - 50 * MIN), ...ml('draftkings', 1.95, 1.87, T - 40 * MIN, T - 50 * MIN)]);
    // First live poll: Pinnacle moved after the start.
    ingest(store, T + 2 * MIN, true, [...ml('pinnacle', 1.5, 2.7, T + 2 * MIN, T + MIN), ...ml('draftkings', 1.95, 1.87, T + 2 * MIN, T - 50 * MIN)]);

    const quotes = closingQuotes(store, 'odds-api:g1', T);
    const pinHome = quotes.find((q) => q.book === 'pinnacle' && q.side === 'home');
    expect(pinHome?.decimal).toBe(1.9);
    expect(pinHome?.observedAt).toBe(T);
    const expected = 1 / 1.9 / (1 / 1.9 + 1 / 2.0);
    expect(closingFairProb(store, bet, cfg)).toBeCloseTo(expected, 12);
  });

  it('returns null when every price we hold was set after the start or is too old', () => {
    const live = new MarketStore();
    ingest(live, T + 2 * MIN, true, ml('pinnacle', 1.5, 2.7, T + 2 * MIN, T + MIN));
    expect(closingFairProb(live, bet, cfg)).toBeNull();

    const old = new MarketStore({ eventRetentionMs: 24 * 3_600_000 });
    ingest(old, T - 3 * 3_600_000, false, ml('pinnacle', 1.9, 2.0, T - 3 * 3_600_000, T - 3 * 3_600_000));
    expect(closingFairProb(old, bet, cfg)).toBeNull();
    expect(closingFairProb(new MarketStore(), bet, cfg)).toBeNull();
  });
});

describe('closing line when the market moved off the bet\'s number', () => {
  const event: RawEvent = { source: 'odds-api', sourceEventId: 'g1', league: 'NBA', home: 'Celtics', away: 'Knicks', startTime: T, isLive: false };
  const q = (book: string, kind: 'spread' | 'total', side: 'home' | 'away' | 'over' | 'under', line: number, decimal: number, at: number): Quote => ({
    book,
    source: 'odds-api',
    sourceEventId: 'g1',
    kind,
    side,
    line,
    decimal,
    suspended: false,
    isMainLine: true,
    observedAt: at,
    bookUpdatedAt: at,
  });
  const spreads = (book: string, homeLine: number, at: number): Quote[] => [
    q(book, 'spread', 'home', homeLine, 1.95, at),
    q(book, 'spread', 'away', -homeLine, 1.95, at),
  ];
  const totals = (book: string, line: number, at: number): Quote[] => [q(book, 'total', 'over', line, 1.95, at), q(book, 'total', 'under', line, 1.95, at)];
  const snapshot = (store: MarketStore, at: number, quotes: Quote[]): void => {
    store.ingest({ source: 'odds-api', league: 'NBA', fetchedAt: at, events: [event], quotes, complete: true, books: ['pinnacle', 'draftkings'] });
  };
  const cfg = loadConfig({ DEVIG_METHOD: 'multiplicative' }, []);
  const bet = (kind: 'spread' | 'total', side: 'home' | 'away' | 'over' | 'under', line: number) => ({
    eventId: 'odds-api:g1',
    league: 'NBA',
    kind,
    side,
    line,
    startTime: T,
  });

  it('converts the closing -5.5 to the bet\'s -3.5 (and +3.5) instead of recording no CLV', () => {
    const store = new MarketStore();
    snapshot(store, T - 60 * MIN, [...spreads('pinnacle', -3.5, T - 60 * MIN), ...spreads('draftkings', -3.5, T - 60 * MIN)]);
    // Pinnacle steams to -5.5 before tip-off; the complete snapshot drops its -3.5 quotes.
    snapshot(store, T - 10 * MIN, [...spreads('pinnacle', -5.5, T - 10 * MIN), ...spreads('draftkings', -3.5, T - 10 * MIN)]);
    expect(closingQuotes(store, 'odds-api:g1', T).some((x) => x.book === 'pinnacle' && x.line === -3.5)).toBe(false);

    const home = closingLine(store, bet('spread', 'home', -3.5), cfg);
    expect(home?.approx).toBe(true);
    // 50% at -5.5, two points better with an NBA margin sd of 12.
    expect(home?.prob).toBeCloseTo(shiftLineProb('spread', 'home', -5.5, 0.5, -3.5, 12), 12);
    expect(home?.prob).toBeGreaterThan(0.56);
    expect(home?.prob).toBeLessThan(0.57);
    const away = closingLine(store, bet('spread', 'away', 3.5), cfg);
    expect(away?.approx).toBe(true);
    expect((away?.prob ?? 0) + (home?.prob ?? 0)).toBeCloseTo(1, 6);
    expect(closingFairProb(store, bet('spread', 'home', -3.5), cfg)).toBeCloseTo(home?.prob ?? 0, 12);
  });

  it('moves totals the right way and stays exact when the number did not move', () => {
    const store = new MarketStore();
    snapshot(store, T - 10 * MIN, [...totals('pinnacle', 228.5, T - 10 * MIN), ...spreads('pinnacle', -3.5, T - 10 * MIN)]);
    const over = closingLine(store, bet('total', 'over', 224.5), cfg);
    expect(over?.approx).toBe(true);
    expect(over?.prob).toBeGreaterThan(0.5); // the total closed 4 points higher: Over 224.5 was the better bet
    const under = closingLine(store, bet('total', 'under', 224.5), cfg);
    expect(under?.prob).toBeLessThan(0.5);
    expect(closingLine(store, bet('spread', 'home', -3.5), cfg)).toEqual({ prob: 0.5, approx: false });
  });

  it('gives up when the line moved too far to convert reliably', () => {
    const store = new MarketStore();
    snapshot(store, T - 10 * MIN, spreads('pinnacle', -10.5, T - 10 * MIN));
    expect(closingLine(store, bet('spread', 'home', -3.5), cfg)).toBeNull();
  });
});

// ---------------------------------------------------------------------------------------------------------------

describe('startApp', () => {
  it('demo mode: serves the dashboard and a state built from the simulated feed', async () => {
    const cfg = config({}, ['--demo']);
    const app = await run(cfg, { demoSeed: 7 });
    expect(app.mode).toBe('demo');
    const base = `http://127.0.0.1:${app.port}`;

    const health = await fetch(`${base}/healthz`);
    expect(health.status).toBe(200);
    expect(await health.json()).toMatchObject({ ok: true });

    const state = app.tick();
    expect(state.health.demoMode).toBe(true);
    expect(state.health.sources[0]).toMatchObject({ name: 'Demo feed', status: 'ok' });
    expect(state.health.eventsTracked).toBeGreaterThan(5);
    expect(state.health.quotesTracked).toBeGreaterThan(100);
    expect(state.health.oddsApiCreditsRemaining).toBeNull();
    expect(state.leagues?.map((l) => l.key)).toContain('EPL');
    expect(state.remainingDailyExposure).toBe(150);

    const res = await fetch(`${base}/api/state`);
    expect(res.status).toBe(200);
    const body = (await res.json()) as { generatedAt: number; settings: RuntimeSettings };
    expect(body.settings.bankroll).toBe(1000);
    expect(typeof body.generatedAt).toBe('number');

    const page = await fetch(`${base}/`);
    expect(page.status).toBe(200);
    expect((await page.text()).toLowerCase()).toContain('<html');

    // Settings changes are saved to DATA_DIR and applied on the next engine pass.
    const put = await fetch(`${base}/api/settings`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ bankroll: 2000 }),
    });
    expect(put.status).toBe(200);
    expect(app.tick().remainingDailyExposure).toBe(300);
    // Demo settings live in DATA_DIR/demo, apart from the real ones.
    expect(JSON.parse(fs.readFileSync(path.join(cfg.dataDir, 'demo', 'settings.json'), 'utf8')).bankroll).toBe(2000);
    expect(fs.existsSync(path.join(cfg.dataDir, 'settings.json'))).toBe(false);
  });

  it('keeps demo bets and settings out of the real (live) journal and settings', async () => {
    const demoCfg = config({ BANKROLL: '1000' }, ['--demo']);
    const demo = await run(demoCfg, { demoSeed: 7 });
    const base = `http://127.0.0.1:${demo.port}`;
    const opp = demo.tick().opportunities.find((o) => o.status === 'active');
    expect(opp).toBeDefined();
    const placed = await fetch(`${base}/api/bets`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ opportunityId: opp!.id, stake: 10, americanTaken: 110 }),
    });
    expect(placed.status).toBe(201);
    await fetch(`${base}/api/settings`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ bankroll: 2000 }) });
    expect(demo.tick().betSummary.stakedToday).toBe(10);
    await demo.stop();

    // Same DATA_DIR, real mode (no key: idle), BANKROLL from .env.
    const liveCfg = config({ BANKROLL: '1000' });
    liveCfg.dataDir = demoCfg.dataDir;
    const live = await run(liveCfg);
    const state = live.tick();
    expect(state.health.demoMode).toBe(false);
    expect(state.betSummary.totalBets).toBe(0);
    expect(state.betSummary.stakedToday).toBe(0);
    expect(state.settings.bankroll).toBe(1000);
    expect(state.remainingDailyExposure).toBe(150);
  });

  it('idle mode (no key, no demo): stays up, polls nothing and reports the source as disabled', async () => {
    const calls: string[] = [];
    const fake = (async (url: string) => {
      calls.push(url);
      throw new Error('must not be called');
    }) as unknown as typeof fetchJson;
    const app = await run(config({}), { oddsApiDeps: { fetchJson: fake } });
    expect(app.mode).toBe('idle');
    const state = app.tick();
    expect(state.health.demoMode).toBe(false);
    expect(state.health.sources).toHaveLength(1);
    expect(state.health.sources[0]).toMatchObject({ name: 'The Odds API', status: 'disabled' });
    expect(state.opportunities).toEqual([]);
    await new Promise((r) => setTimeout(r, 100));
    expect(calls).toEqual([]);
  });

  it('live mode: refreshes events for free, then polls odds once and ingests them (fake API, no network)', async () => {
    const fixture = JSON.parse(fs.readFileSync(FIXTURE, 'utf8')) as Array<Record<string, unknown>>;
    const clockStart = Date.parse('2026-10-27T23:30:00Z');
    const realStart = Date.now();
    const now = (): number => clockStart + (Date.now() - realStart);
    const calls: string[] = [];
    const fake = async <T>(url: string, _opts?: FetchJsonOptions): Promise<FetchJsonResult<T>> => {
      calls.push(url);
      const headers = new Headers({ 'x-requests-remaining': '19997', 'x-requests-used': '3', 'x-requests-last': '3' });
      const data = url.includes('/events?')
        ? fixture
            .filter((e) => typeof e.home_team === 'string')
            .map((e) => ({ id: e.id, home_team: e.home_team, away_team: e.away_team, commence_time: e.commence_time }))
        : fixture;
      return { data: data as T, status: 200, headers, durationMs: 1 };
    };
    const cfg = config({ ODDS_API_KEY: 'test-key-123', LEAGUES: 'NBA', BOOK_STATE: 'nj' });
    const app = await run(cfg, { oddsApiDeps: { fetchJson: fake as typeof fetchJson }, now });
    expect(app.mode).toBe('live');

    await waitFor(() => app.tick().health.quotesTracked > 0);
    const state = app.tick();
    expect(state.health.eventsTracked).toBe(3);
    expect(state.health.oddsApiCreditsRemaining).toBe(19997);
    expect(state.health.sources.map((s) => s.name)).toEqual(['The Odds API', 'Poll scheduler']);
    expect(state.health.sources[0].status).toBe('ok');
    // The fixture's Magic @ Heat game started at 23:10, so the league is live.
    expect(state.health.sources[1].detail).toMatch(/NBA: 1 live, polled every \d+ s/);
    expect(JSON.stringify(state)).not.toContain('test-key-123');

    const events = calls.filter((u) => u.includes('/events?'));
    const odds = calls.filter((u) => u.includes('/odds?'));
    expect(events).toHaveLength(1);
    expect(odds).toHaveLength(1);
    for (const u of calls) expect(u.startsWith('https://api.the-odds-api.com/v4/sports/basketball_nba/')).toBe(true);
  });

  it('records the closing line of a pending pre-game bet once its game starts', async () => {
    const fixture = JSON.parse(fs.readFileSync(FIXTURE, 'utf8')) as unknown[];
    const fake = async <T>(url: string): Promise<FetchJsonResult<T>> => {
      const data = url.includes('/events?')
        ? (fixture as Array<Record<string, unknown>>).map((e) => ({ id: e.id, home_team: e.home_team, away_team: e.away_team, commence_time: e.commence_time }))
        : fixture;
      return { data: data as T, status: 200, headers: new Headers({ 'x-requests-remaining': '500' }), durationMs: 1 };
    };
    const cfg = config({ ODDS_API_KEY: 'k-123456', LEAGUES: 'NBA', DEVIG_METHOD: 'multiplicative' });
    const start = Date.parse('2026-10-28T00:00:00Z'); // Knicks @ Celtics in the fixture
    const journalFile = path.join(cfg.dataDir, 'bets.jsonl');
    const bet = new BetJournal(journalFile).load().place({
      opportunityId: 'odds-api:8c1f0e4a2b7d4c6e9f1a3b5c7d9e0f12|ev|moneyline|home|',
      eventId: 'odds-api:8c1f0e4a2b7d4c6e9f1a3b5c7d9e0f12',
      league: 'NBA',
      eventName: 'New York Knicks @ Boston Celtics',
      startTime: start,
      pick: 'Boston Celtics ML',
      kind: 'moneyline',
      side: 'home',
      line: null,
      wasLive: false,
      americanTaken: -250,
      stake: 10,
      fairProbAtPlace: 0.69,
    });

    let clock = start - 30 * MIN;
    const app = await run(cfg, { oddsApiDeps: { fetchJson: fake as typeof fetchJson }, now: () => clock });
    await waitFor(() => app.tick().health.quotesTracked > 0);
    expect(app.tick().betSummary.avgClvPct).toBeNull(); // not started yet

    clock = start + 5 * SEC;
    const state = app.tick();
    // Pinnacle 1.408 / 3.05 at the close, multiplicative no-vig.
    const closing = 1 / 1.408 / (1 / 1.408 + 1 / 3.05);
    expect(state.betSummary.avgClvPct).toBeCloseTo(closing * 1.4 - 1, 9);
    expect(new BetJournal(journalFile).load().get(bet.id)?.closingFairProb).toBeCloseTo(closing, 12);
  });

  it('retries saving a closing line after a disk error instead of giving up on it', async () => {
    const fixture = JSON.parse(fs.readFileSync(FIXTURE, 'utf8')) as unknown[];
    const fake = async <T>(url: string): Promise<FetchJsonResult<T>> => {
      const data = url.includes('/events?')
        ? (fixture as Array<Record<string, unknown>>).map((e) => ({ id: e.id, home_team: e.home_team, away_team: e.away_team, commence_time: e.commence_time }))
        : fixture;
      return { data: data as T, status: 200, headers: new Headers({ 'x-requests-remaining': '500' }), durationMs: 1 };
    };
    const cfg = config({ ODDS_API_KEY: 'k-123456', LEAGUES: 'NBA', DEVIG_METHOD: 'multiplicative' });
    const start = Date.parse('2026-10-28T00:00:00Z');
    const journalFile = path.join(cfg.dataDir, 'bets.jsonl');
    const bet = new BetJournal(journalFile).load().place({
      opportunityId: 'odds-api:8c1f0e4a2b7d4c6e9f1a3b5c7d9e0f12|ev|moneyline|home|',
      eventId: 'odds-api:8c1f0e4a2b7d4c6e9f1a3b5c7d9e0f12',
      league: 'NBA',
      eventName: 'New York Knicks @ Boston Celtics',
      startTime: start,
      pick: 'Boston Celtics ML',
      kind: 'moneyline',
      side: 'home',
      line: null,
      wasLive: false,
      americanTaken: -250,
      stake: 10,
      fairProbAtPlace: 0.69,
    });
    let clock = start - 30 * MIN;
    const app = await run(cfg, { oddsApiDeps: { fetchJson: fake as typeof fetchJson }, now: () => clock });
    await waitFor(() => app.tick().health.quotesTracked > 0);

    // The journal cannot be written when the game starts (a directory is in the file's way: EISDIR).
    const aside = `${journalFile}.aside`;
    fs.renameSync(journalFile, aside);
    fs.mkdirSync(journalFile);
    clock = start + 5 * SEC;
    expect(app.tick().betSummary.avgClvPct).toBeNull();

    // The disk recovers; the next attempt records the closing line from the price history at the start.
    fs.rmdirSync(journalFile);
    fs.renameSync(aside, journalFile);
    clock = start + 40 * SEC;
    const closing = 1 / 1.408 / (1 / 1.408 + 1 / 3.05);
    expect(app.tick().betSummary.avgClvPct).toBeCloseTo(closing * 1.4 - 1, 9);
    expect(new BetJournal(journalFile).load().get(bet.id)?.closingFairProb).toBeCloseTo(closing, 12);
  });

  it('warns at startup that INCLUDE_ALT_LINES does nothing with The Odds API', async () => {
    const writes: string[] = [];
    const spy = vi.spyOn(process.stderr, 'write').mockImplementation((chunk: string | Uint8Array) => {
      writes.push(String(chunk));
      return true;
    });
    try {
      const fake = (async () => ({ data: [], status: 200, headers: new Headers(), durationMs: 1 })) as unknown as typeof fetchJson;
      await run(config({ ODDS_API_KEY: 'k-123456', LEAGUES: 'NBA', INCLUDE_ALT_LINES: 'true' }), { oddsApiDeps: { fetchJson: fake } });
      await run(config({ INCLUDE_ALT_LINES: 'true' }, ['--demo']), { demoSeed: 1 });
    } finally {
      spy.mockRestore();
    }
    const warnings = writes.filter((w) => w.includes('INCLUDE_ALT_LINES'));
    expect(warnings).toHaveLength(1); // live mode only; the demo feed does have alternate lines
    expect(warnings[0]).toMatch(/main lines only/);
  });

  it('warns when ODDS_API_RESET_DAY is not the 1st (credits reset on the 1st for every account)', async () => {
    const writes: string[] = [];
    const spy = vi.spyOn(process.stderr, 'write').mockImplementation((chunk: string | Uint8Array) => {
      writes.push(String(chunk));
      return true;
    });
    try {
      const fake = (async () => ({ data: [], status: 200, headers: new Headers(), durationMs: 1 })) as unknown as typeof fetchJson;
      await run(config({ ODDS_API_KEY: 'k-123456', LEAGUES: 'NBA' }), { oddsApiDeps: { fetchJson: fake } });
      expect(writes.some((w) => w.includes('ODDS_API_RESET_DAY'))).toBe(false);
      await run(config({ ODDS_API_KEY: 'k-123456', LEAGUES: 'NBA', ODDS_API_RESET_DAY: '15' }), { oddsApiDeps: { fetchJson: fake } });
    } finally {
      spy.mockRestore();
    }
    expect(writes.filter((w) => w.includes('ODDS_API_RESET_DAY=15'))).toHaveLength(1);
  });

  it('requires the dashboard password on everything except /healthz', async () => {
    const app = await run(config({ DASHBOARD_PASSWORD: 'secret' }, ['--demo']), { demoSeed: 3 });
    const base = `http://127.0.0.1:${app.port}`;
    expect((await fetch(`${base}/healthz`)).status).toBe(200);
    expect((await fetch(`${base}/api/state`)).status).toBe(401);
    const auth = { Authorization: `Basic ${Buffer.from('admin:secret').toString('base64')}` };
    expect((await fetch(`${base}/api/state`, { headers: auth })).status).toBe(200);
    const wrong = { Authorization: `Basic ${Buffer.from('admin:nope').toString('base64')}` };
    expect((await fetch(`${base}/api/state`, { headers: wrong })).status).toBe(401);
  });

  it('refuses to start when DATA_DIR is not a writable directory', async () => {
    const file = path.join(tmpDir(), 'not-a-dir');
    fs.writeFileSync(file, 'x');
    const cfg = config({}, ['--demo']);
    cfg.dataDir = file;
    await expect(startApp(cfg, { publicDir: PUBLIC_DIR })).rejects.toThrow();
  });
});

import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';
import { loadConfig } from '../src/config';
import type { AppConfig } from '../src/config';
import { MarketStore } from '../src/engine/marketStore';
import { BetJournal } from '../src/server/betJournal';
import { closingFairProb, closingQuotes, remainingExposure, startApp } from '../src/index';
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
    expect(JSON.parse(fs.readFileSync(path.join(cfg.dataDir, 'settings.json'), 'utf8')).bankroll).toBe(2000);
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

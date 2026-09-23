/**
 * Odds Decision Hub: entry point and orchestrator.
 *
 *   loadDotEnv -> loadConfig -> data dir -> settings -> bet journal -> market store -> tracker -> notifier
 *   -> dashboard server, then one feed:
 *     - demo mode: DemoFeed.tick every 3 s into the store (simulated odds, no network, no alerts),
 *     - no ODDS_API_KEY: no polling; the dashboard explains how to add a key or run the demo,
 *     - otherwise: a never-ending poll loop against The Odds API (free event refresh, then the credit-budgeted
 *       odds poll the scheduler picks).
 *   An engine tick every second prices the board (computeOpportunities), tracks picks over time, pushes the state
 *   to every dashboard (SSE), sends phone alerts for new urgent picks and records closing lines (CLV) for pending
 *   pre-game bets once their game starts.
 *
 * Read-only by design: the only outbound calls are The Odds API and the optional notification webhooks. Nothing here
 * logs in to, automates or contacts a sportsbook; the user places every bet.
 */
import * as fs from 'node:fs';
import * as path from 'node:path';
import { performance } from 'node:perf_hooks';
import { loadConfig, loadDotEnv } from './config';
import type { AppConfig } from './config';
import { fairProbForPick } from './engine/fairPrice';
import type { FairOptions } from './engine/fairPrice';
import { MarketStore } from './engine/marketStore';
import type { StoredQuote } from './engine/marketStore';
import { OpportunityTracker, computeOpportunities } from './engine/opportunities';
import { BetJournal } from './server/betJournal';
import { DashboardServer } from './server/httpServer';
import { Notifier } from './server/notifier';
import type { NotifierDeps } from './server/notifier';
import { SettingsStore } from './server/settingsStore';
import { DemoFeed } from './sources/demoFeed';
import { OddsApiClient, OddsApiError } from './sources/oddsApiClient';
import type { OddsApiClientDeps } from './sources/oddsApiClient';
import { PollScheduler } from './sources/scheduler';
import type {
  BetRecord,
  BetSummary,
  DashboardState,
  Health,
  LeagueDef,
  Opportunity,
  RuntimeSettings,
  SourceHealth,
} from './types';
import { createLogger, setLogLevel } from './util/logger';
import { AbortError, sleep } from './util/retry';

const log = createLogger('main');

const ENGINE_TICK_MS = 1_000;
const DEMO_TICK_MS = 3_000;
const PRUNE_EVERY_MS = 60_000;
const POLL_MIN_SLEEP_MS = 250;
const POLL_MAX_SLEEP_MS = 5_000;
/** A league the API reports as unavailable (out of season / unknown sport key) is skipped this long. */
const UNAVAILABLE_BACKOFF_MS = 6 * 3_600_000;
/** Closing lines are only captured this soon after the start (later prices would be live prices). */
const CLOSING_WINDOW_MS = 15 * 60_000;
/** Oldest pre-start price accepted as the closing line (pre-game leagues can be polled rarely on small plans). */
const CLOSING_MAX_AGE_MS = 2 * 3_600_000;
/**
 * With The Odds API a pre-game league may be polled less often than the store's default 20-minute event retention,
 * so events (and their price history, needed for CLV and stale-line checks) are kept longer between polls.
 */
const ODDS_API_EVENT_RETENTION_MS = 2 * 3_600_000;
const STOP_TIMEOUT_MS = 4_000;
const DK = 'draftkings';

export type AppMode = 'demo' | 'live' | 'idle';

export interface AppOptions {
  /** Directory with index.html, app.js, styles.css, favicon.svg (default: ../public next to this file). */
  publicDir?: string;
  engineTickMs?: number;
  demoTickMs?: number;
  demoSeed?: number;
  /** Test hooks: inject a fake fetchJson / clock into the Odds API client. */
  oddsApiDeps?: OddsApiClientDeps;
  notifierDeps?: NotifierDeps;
  now?: () => number;
}

export interface RunningApp {
  readonly port: number;
  readonly mode: AppMode;
  /** Latest dashboard state (rebuilt every engine tick). */
  state(): DashboardState;
  /** Runs one engine pass now and returns the new state. */
  tick(): DashboardState;
  stop(): Promise<void>;
}

// ---------------------------------------------------------------------------------------------------------------
// small pure helpers (exported for tests)

/** Stake still allowed today: bankroll × daily exposure % minus what was logged today, never negative, in cents. */
export function remainingExposure(settings: RuntimeSettings, stakedToday: number): number {
  const cap = settings.bankroll * settings.maxDailyExposurePct;
  const staked = Number.isFinite(stakedToday) ? stakedToday : 0;
  const left = Number.isFinite(cap) ? cap - staked : 0;
  return Math.max(0, Math.round(left * 100) / 100);
}

/**
 * The event's quotes as they stood at `startTime` (the closing line): each price is read from its history at the
 * start, quotes first seen after the start with no earlier price are dropped, and freshness is capped at the start so
 * a quote re-observed during the game still counts as "seen at the close".
 */
export function closingQuotes(store: MarketStore, eventId: string, startTime: number): StoredQuote[] {
  const out: StoredQuote[] = [];
  for (const q of store.quotesForEvent(eventId)) {
    const price = store.priceAt(q, startTime);
    if (price === null || !(price > 1)) continue;
    const seenAfterStart = q.observedAt > startTime;
    out.push({
      ...q,
      decimal: price,
      observedAt: Math.min(q.observedAt, startTime),
      // A suspension seen after kick-off says nothing about the pre-game market.
      suspended: seenAfterStart ? false : q.suspended,
    });
  }
  return out;
}

/** Closing fair win probability for a bet's pick, or null when the store has no usable pre-start reference. */
export function closingFairProb(
  store: MarketStore,
  bet: Pick<BetRecord, 'eventId' | 'league' | 'kind' | 'side' | 'line' | 'startTime'>,
  cfg: Pick<AppConfig, 'leagues' | 'model' | 'oddsApi'>,
): number | null {
  const quotes = closingQuotes(store, bet.eventId, bet.startTime);
  if (quotes.length === 0) return null;
  const league = cfg.leagues.find((l) => l.key === bet.league);
  const opts: FairOptions = {
    sharpBooks: cfg.oddsApi.sharpBooks,
    excludeBooks: [DK],
    method: cfg.model.devigMethod,
    minConsensusBooks: cfg.model.minConsensusBooks,
    threeWay: league?.threeWay === true,
    now: bet.startTime,
    maxAgeMs: CLOSING_MAX_AGE_MS,
  };
  const p = fairProbForPick(quotes, bet.kind, bet.side, bet.line, opts);
  return p !== null && p > 0 && p < 1 ? p : null;
}

function isLoopback(host: string): boolean {
  const h = host.trim().toLowerCase();
  return h === 'localhost' || h === '::1' || h === '[::1]' || h.startsWith('127.');
}

function dashboardUrl(host: string, port: number): string {
  const h = host === '0.0.0.0' || host === '::' || host === '' ? 'localhost' : host.includes(':') ? `[${host}]` : host;
  return `http://${h}:${port}/`;
}

function errorText(err: unknown): string {
  if (err instanceof Error) return err.message;
  return typeof err === 'string' ? err : 'unknown error';
}

function isAbort(err: unknown, signal: AbortSignal): boolean {
  return err instanceof AbortError || signal.aborted;
}

function fmtDuration(ms: number): string {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 90) return `${s} s`;
  const m = Math.round(s / 60);
  if (m < 90) return `${m} min`;
  const h = s / 3600;
  return `${h < 10 ? h.toFixed(1) : Math.round(h)} h`;
}

function clampNum(x: number, lo: number, hi: number): number {
  return Number.isFinite(x) ? Math.min(hi, Math.max(lo, x)) : hi;
}

/**
 * Creates the data directory if needed and proves it is writable (a read-only or root-owned Docker volume would
 * otherwise only fail later, when the user saves settings or logs a bet).
 */
function ensureDataDir(dir: string): void {
  fs.mkdirSync(dir, { recursive: true });
  const probe = path.join(dir, `.write-test-${process.pid}`);
  fs.writeFileSync(probe, 'ok');
  fs.rmSync(probe, { force: true });
}

// ---------------------------------------------------------------------------------------------------------------
// the application

class OddsHub implements RunningApp {
  readonly mode: AppMode;
  port = 0;

  private readonly cfg: AppConfig;
  private readonly now: () => number;
  private readonly engineTickMs: number;
  private readonly demoTickMs: number;
  private readonly publicDir: string;

  private readonly settings: SettingsStore;
  private readonly journal: BetJournal;
  private readonly store: MarketStore;
  private readonly tracker: OpportunityTracker;
  private readonly notifier: Notifier;
  private readonly client: OddsApiClient;
  private readonly scheduler: PollScheduler;
  private readonly demo: DemoFeed | null;
  private server: DashboardServer | null = null;

  private readonly abort = new AbortController();
  private readonly startedAt: number;
  private latest: DashboardState | null = null;
  private engineTimer: NodeJS.Timeout | null = null;
  private demoTimer: NodeJS.Timeout | null = null;
  private pollDone: Promise<void> = Promise.resolve();
  private stopping: Promise<void> | null = null;
  private lastPruneAt = 0;
  private lastEngineRunAt: number | null = null;
  private engineDurationMs: number | null = null;

  /** Pending pre-game bets whose closing line could not be captured (never retried). Pruned to pending bets. */
  private readonly closingGaveUp = new Set<string>();

  // demo feed health
  private demoLastTick: number | null = null;
  private demoFailures = 0;
  private demoLastError: string | null = null;

  // poll loop health
  private lastOddsPollAt: number | null = null;
  private loopFailures = 0;
  private loopLastError: string | null = null;
  private budgetWarned = false;

  constructor(cfg: AppConfig, opts: AppOptions) {
    this.cfg = cfg;
    this.now = opts.now ?? Date.now;
    this.engineTickMs = opts.engineTickMs ?? ENGINE_TICK_MS;
    this.demoTickMs = opts.demoTickMs ?? DEMO_TICK_MS;
    this.publicDir = opts.publicDir ?? path.resolve(__dirname, '..', 'public');
    this.startedAt = this.now();

    const client = new OddsApiClient(cfg.oddsApi, { now: this.now, ...opts.oddsApiDeps });
    this.client = client;
    this.mode = cfg.demoMode ? 'demo' : client.enabled ? 'live' : 'idle';

    const leagueKeys = cfg.leagues.map((l) => l.key);
    this.settings = new SettingsStore(path.join(cfg.dataDir, 'settings.json'), cfg.defaults, leagueKeys);
    this.settings.load();
    this.journal = new BetJournal(path.join(cfg.dataDir, 'bets.jsonl'), { now: this.now }).load();
    this.store = new MarketStore(this.mode === 'live' ? { eventRetentionMs: ODDS_API_EVENT_RETENTION_MS } : {});
    this.tracker = new OpportunityTracker({ goneRetentionSec: cfg.model.goneRetentionSec });
    this.notifier = new Notifier(cfg.notify, opts.notifierDeps);
    this.scheduler = new PollScheduler(cfg.oddsApi, cfg.leagues);
    this.scheduler.setEnabledLeagues(this.settings.get().enabledLeagues);
    this.demo = this.mode === 'demo' ? new DemoFeed(cfg.leagues, cfg.oddsApi.books, { seed: opts.demoSeed }) : null;
  }

  state(): DashboardState {
    return this.latest ?? this.runEngine();
  }

  tick(): DashboardState {
    return this.runEngine();
  }

  async start(): Promise<void> {
    // A first (empty) state so /api/state never has to wait for a tick.
    this.runEngine();
    const server = new DashboardServer({
      host: this.cfg.server.host,
      port: this.cfg.server.port,
      user: this.cfg.server.user,
      password: this.cfg.server.password,
      publicDir: this.publicDir,
      getState: () => this.state(),
      settings: this.settings,
      journal: this.journal,
      findOpportunity: (id) => this.tracker.find(id),
      onSettingsChanged: (s) => this.onSettingsChanged(s),
      now: this.now,
    });
    const { port } = await server.start();
    this.server = server;
    this.port = port;
    this.logBanner();

    if (this.demo) {
      this.demoTick();
      this.demoTimer = setInterval(() => this.demoTick(), this.demoTickMs);
      this.demoTimer.unref();
    } else if (this.mode === 'live') {
      this.pollDone = this.pollLoop().catch((err: unknown) => {
        // pollLoop never rejects by design; this is a last line of defence.
        log.error('odds poll loop stopped unexpectedly', { error: errorText(err) });
      });
    }
    this.scheduleEngine(0);
  }

  stop(): Promise<void> {
    if (this.stopping) return this.stopping;
    this.stopping = (async () => {
      this.abort.abort();
      if (this.engineTimer) clearTimeout(this.engineTimer);
      if (this.demoTimer) clearInterval(this.demoTimer);
      this.engineTimer = null;
      this.demoTimer = null;
      const server = this.server;
      const pending = Promise.allSettled([this.pollDone, server ? server.stop() : Promise.resolve()]);
      await Promise.race([pending, sleep(STOP_TIMEOUT_MS).catch(() => undefined)]);
    })();
    return this.stopping;
  }

  // -------------------------------------------------------------------------------------------------------------
  // engine

  private scheduleEngine(delayMs: number): void {
    if (this.abort.signal.aborted) return;
    if (this.engineTimer) clearTimeout(this.engineTimer);
    this.engineTimer = setTimeout(() => {
      this.engineTimer = null;
      try {
        this.runEngine();
      } catch (err) {
        log.error('engine tick failed', { error: errorText(err) });
      }
      this.scheduleEngine(this.engineTickMs);
    }, delayMs);
    this.engineTimer.unref();
  }

  private runEngine(): DashboardState {
    const now = this.now();
    if (now - this.lastPruneAt >= PRUNE_EVERY_MS) {
      this.lastPruneAt = now;
      const pruned = this.store.prune(now);
      if (pruned.eventsRemoved > 0) log.debug(`pruned ${pruned.eventsRemoved} events and ${pruned.quotesRemoved} quotes`);
    }

    const settings = this.settings.get();
    let summary = this.journal.summary(now);
    const remaining = remainingExposure(settings, summary.stakedToday);

    const started = performance.now();
    const candidates = computeOpportunities(this.store, {
      settings,
      model: this.cfg.model,
      sharpBooks: this.cfg.oddsApi.sharpBooks,
      leagues: this.cfg.leagues,
      now,
      remainingDailyExposure: remaining,
    });
    const opportunities = this.tracker.update(candidates, now);
    this.engineDurationMs = Math.round((performance.now() - started) * 10) / 10;
    this.lastEngineRunAt = now;

    if (this.captureClosingLines(now) > 0) summary = this.journal.summary(now);

    const state = this.buildState(now, opportunities, settings, summary, remaining);
    this.latest = state;
    this.server?.broadcast(state);
    this.sendAlerts();
    return state;
  }

  private sendAlerts(): void {
    // Demo prices are fake: never push them to a phone as "BET NOW".
    if (this.mode === 'demo' || !this.notifier.enabled) return;
    const alerts = this.tracker.newlyAlertable(this.cfg.notify.minUrgency);
    if (alerts.length === 0) return;
    this.notifier
      .notify(alerts)
      .then((sent) => {
        if (sent > 0) log.info(`sent ${sent} alert${sent === 1 ? '' : 's'}`);
      })
      .catch((err: unknown) => log.warn('alerts failed', { error: errorText(err) }));
  }

  /**
   * Records the closing fair probability (CLV) of pending pre-game bets whose game has started, once per bet.
   * Returns the number of bets updated.
   */
  private captureClosingLines(now: number): number {
    const pending = this.journal.pendingWithoutClosing();
    if (pending.length === 0) {
      this.closingGaveUp.clear();
      return 0;
    }
    let updated = 0;
    const pendingIds = new Set<string>();
    for (const bet of pending) {
      pendingIds.add(bet.id);
      if (bet.wasLive || bet.startTime > now || this.closingGaveUp.has(bet.id)) continue;
      let prob: number | null = null;
      try {
        prob = closingFairProb(this.store, bet, this.cfg);
      } catch (err) {
        log.warn('closing line lookup failed', { bet: bet.id, error: errorText(err) });
      }
      if (prob !== null) {
        try {
          this.journal.updateClosing(bet.id, prob);
          updated++;
          const clv = prob * bet.decimalTaken - 1;
          log.info(`closing line recorded for ${bet.pick} (${bet.eventName}): fair ${(prob * 100).toFixed(1)}%, CLV ${(clv * 100).toFixed(1)}%`);
        } catch (err) {
          this.closingGaveUp.add(bet.id);
          log.warn('could not save closing line', { bet: bet.id, error: errorText(err) });
        }
      } else if (now - bet.startTime > CLOSING_WINDOW_MS) {
        this.closingGaveUp.add(bet.id);
        log.debug(`no closing line available for ${bet.pick} (${bet.eventName})`);
      }
    }
    for (const id of this.closingGaveUp) if (!pendingIds.has(id)) this.closingGaveUp.delete(id);
    return updated;
  }

  private onSettingsChanged(s: RuntimeSettings): void {
    this.scheduler.setEnabledLeagues(s.enabledLeagues);
    log.info('settings updated', {
      bankroll: s.bankroll,
      leagues: s.enabledLeagues.join(','),
      minEvPrematch: s.minEvPrematch,
      minEvLive: s.minEvLive,
    });
    // Re-price right away so the dashboard reflects the new settings without waiting for the next tick.
    this.scheduleEngine(0);
  }

  // -------------------------------------------------------------------------------------------------------------
  // feeds

  private demoTick(): void {
    const now = this.now();
    try {
      for (const snapshot of this.demo ? this.demo.tick(now) : []) this.store.ingest(snapshot);
      this.demoLastTick = now;
      this.demoFailures = 0;
    } catch (err) {
      this.demoFailures++;
      this.demoLastError = errorText(err);
      log.error('demo feed tick failed', { error: this.demoLastError });
    }
  }

  /** Never-ending poll loop against The Odds API. Never rejects; exits when the shutdown signal fires. */
  private async pollLoop(): Promise<void> {
    const signal = this.abort.signal;
    while (!signal.aborted) {
      let waitMs = POLL_MAX_SLEEP_MS;
      try {
        waitMs = await this.pollOnce(signal);
        this.loopFailures = 0;
      } catch (err) {
        if (isAbort(err, signal)) break;
        this.loopFailures++;
        this.loopLastError = errorText(err);
        log.error('odds poll failed', { error: this.loopLastError });
      }
      try {
        await sleep(clampNum(waitMs, POLL_MIN_SLEEP_MS, POLL_MAX_SLEEP_MS), signal);
      } catch {
        break;
      }
    }
  }

  /** One pass: refresh event lists that are due (free), then spend credits on the league that is due. Returns ms to wait. */
  private async pollOnce(signal: AbortSignal): Promise<number> {
    // 1. Free event-list refresh: tells the scheduler which leagues have games live or coming up.
    for (const league of this.scheduler.leaguesNeedingEventRefresh(this.now())) {
      if (signal.aborted) throw new AbortError();
      try {
        const events = await this.client.fetchEvents(league, signal);
        this.scheduler.updateEvents(league.key, events, this.now());
        log.debug(`${league.key}: ${events.length} upcoming/live events`);
      } catch (err) {
        if (isAbort(err, signal)) throw err;
        if (err instanceof OddsApiError && err.kind === 'circuit-open') return POLL_MAX_SLEEP_MS;
        this.handleLeagueError(league, err, 'events');
      }
    }

    // 2. Odds for the league the credit budget says is due.
    const cost = this.client.costPerOddsCall();
    const at = this.now();
    const remaining = this.client.usage.remaining;
    if (this.scheduler.isBudgetExhausted(remaining, cost)) {
      if (!this.budgetWarned) {
        this.budgetWarned = true;
        log.warn(
          `Odds API credits are down to the ${this.cfg.oddsApi.reserveCredits}-credit reserve (${remaining ?? 'unknown'} left); ` +
            `odds polling paused until the quota resets on ${new Date(this.scheduler.nextResetAt(at)).toISOString().slice(0, 10)}`,
        );
      }
    } else {
      this.budgetWarned = false;
    }

    const plan = this.scheduler.nextDue(at, remaining, cost);
    if (plan) {
      const key = plan.league.key;
      try {
        const snapshot = await this.client.fetchOdds(plan.league, signal);
        const result = this.store.ingest(snapshot);
        const liveCount = snapshot.events.filter((e) => e.isLive).length;
        const done = this.now();
        this.scheduler.markPolled(key, done, true, liveCount);
        this.lastOddsPollAt = done;
        const usage = this.client.usage;
        log.info(
          `${key}: ${snapshot.events.length} events (${liveCount} live), ${result.quotesUpserted} prices, ` +
            `${result.priceChanges} changed; ${usage.last ?? cost} credits used, ${usage.remaining ?? '?'} left; ` +
            `next ${plan.reason} poll in ~${plan.intervalSec} s`,
        );
      } catch (err) {
        if (isAbort(err, signal)) throw err;
        if (err instanceof OddsApiError && err.kind === 'circuit-open') return POLL_MAX_SLEEP_MS;
        this.handleLeagueError(plan.league, err, 'odds');
      }
    }
    return this.scheduler.msUntilNextDue(this.now(), this.client.usage.remaining, cost);
  }

  private handleLeagueError(league: LeagueDef, err: unknown, what: 'events' | 'odds'): void {
    const now = this.now();
    if (err instanceof OddsApiError && err.kind === 'unavailable') {
      this.scheduler.markUnavailable(league.key, now + UNAVAILABLE_BACKOFF_MS);
      log.info(`${league.key} is not available on The Odds API right now; skipping it for 6 hours`);
      return;
    }
    if (what === 'events') this.scheduler.markEventRefreshFailed(league.key, now);
    else this.scheduler.markPolled(league.key, now, false);
    // OddsApiClient already logged the failure with details (API key removed).
    if (!(err instanceof OddsApiError)) log.warn(`${league.key} ${what} request failed`, { error: errorText(err) });
  }

  // -------------------------------------------------------------------------------------------------------------
  // state

  private buildState(
    now: number,
    opportunities: Opportunity[],
    settings: RuntimeSettings,
    betSummary: BetSummary,
    remainingDailyExposure: number,
  ): DashboardState {
    return {
      generatedAt: now,
      opportunities,
      health: this.buildHealth(now),
      settings,
      betSummary,
      remainingDailyExposure,
      leagues: this.cfg.leagues.map((l) => ({ key: l.key, name: l.name })),
    };
  }

  private buildHealth(now: number): Health {
    const stats = this.store.stats();
    const usage = this.client.usage;
    const demo = this.mode === 'demo';
    return {
      startedAt: this.startedAt,
      now,
      demoMode: demo,
      sources: this.sources(now),
      oddsApiCreditsRemaining: demo ? null : usage.remaining,
      oddsApiCreditsUsed: demo ? null : usage.used,
      eventsTracked: stats.events,
      liveEvents: stats.liveEvents,
      quotesTracked: stats.quotes,
      memoryMb: Math.round((process.memoryUsage.rss() / 1_048_576) * 10) / 10,
      lastEngineRunMs: this.lastEngineRunAt,
      engineRunDurationMs: this.engineDurationMs,
    };
  }

  private sources(now: number): SourceHealth[] {
    if (this.mode === 'demo') {
      const leagues = [...new Set(this.store.events().map((e) => e.league))].sort();
      return [
        {
          name: 'Demo feed',
          status: this.demoFailures > 0 ? 'degraded' : 'ok',
          lastSuccess: this.demoLastTick,
          lastError: this.demoLastError,
          consecutiveFailures: this.demoFailures,
          detail:
            `Simulated odds (not real prices) for ${leagues.length > 0 ? leagues.join(', ') : 'the demo leagues'}, ` +
            `updated every ${Math.round(this.demoTickMs / 1000)} s. No Odds API credits are used and no alerts are sent.`,
        },
      ];
    }
    const out: SourceHealth[] = [this.client.health()];
    if (this.mode === 'live') out.push(this.schedulerHealth(now));
    return out;
  }

  private schedulerHealth(now: number): SourceHealth {
    const usage = this.client.usage;
    const cost = this.client.costPerOddsCall();
    const exhausted = this.scheduler.isBudgetExhausted(usage.remaining, cost);
    const cph = this.scheduler.creditsPerHour(usage.remaining, now);
    const reset = new Date(this.scheduler.nextResetAt(now)).toISOString().slice(0, 10);
    const parts: string[] = [
      exhausted
        ? `Paused: only the ${this.cfg.oddsApi.reserveCredits}-credit reserve is left until the reset on ${reset}`
        : `Budget ${cph.toFixed(1)} credits/h until the reset on ${reset} (${cost} per odds call)`,
    ];
    const horizon = this.cfg.oddsApi.prematchHorizonHours;
    for (const st of this.scheduler.snapshot()) {
      let text: string;
      if (st.unavailableUntil !== null && st.unavailableUntil > now) {
        text = `unavailable, retry in ${fmtDuration(st.unavailableUntil - now)}`;
      } else if (st.eventsRefreshedAt === null) {
        text = 'loading schedule';
      } else if (st.liveCount > 0) {
        text = `${st.liveCount} live${st.intervalSec !== null ? `, polled every ${fmtDuration(st.intervalSec * 1000)}` : ''}`;
      } else if (st.nextStartTime !== null && st.nextStartTime - now <= horizon * 3_600_000) {
        text = `next game in ${fmtDuration(st.nextStartTime - now)}${st.intervalSec !== null ? `, polled every ${fmtDuration(st.intervalSec * 1000)}` : ''}`;
      } else {
        text = `no games in the next ${horizon} h (no credits spent)`;
      }
      parts.push(`${st.league}: ${text}`);
    }
    return {
      name: 'Poll scheduler',
      status: exhausted ? 'down' : this.loopFailures > 0 ? 'degraded' : 'ok',
      lastSuccess: this.lastOddsPollAt,
      lastError: this.loopLastError,
      consecutiveFailures: this.loopFailures,
      detail: parts.join(' · '),
    };
  }

  private logBanner(): void {
    const cfg = this.cfg;
    const settings = this.settings.get();
    const modeText =
      this.mode === 'demo'
        ? 'DEMO (simulated odds, not real prices; no credits used, no alerts sent)'
        : this.mode === 'live'
          ? 'LIVE (The Odds API)'
          : 'IDLE (no ODDS_API_KEY set: nothing is polled; add a key or run `npm run demo`)';
    const auth = cfg.server.password !== '';
    log.info('Odds Decision Hub: read-only analytics. You place every bet yourself.');
    log.info(`mode: ${modeText}`);
    log.info(`leagues: ${settings.enabledLeagues.join(', ')}`);
    log.info(`books: ${cfg.oddsApi.books.join(', ')} (reference: ${cfg.oddsApi.sharpBooks.join(' > ')})`);
    if (this.mode === 'live') {
      log.info(
        `credits: ${this.client.costPerOddsCall()} per odds call (${cfg.oddsApi.markets.length} markets x ` +
          `${Math.ceil(cfg.oddsApi.books.length / 10)} region); plan ${cfg.oddsApi.monthlyCredits}/month, ` +
          `reserve ${cfg.oddsApi.reserveCredits}, resets on day ${cfg.oddsApi.resetDayOfMonth}`,
      );
    }
    log.info(`dashboard: ${dashboardUrl(cfg.server.host, this.port)} (auth ${auth ? `on, user "${cfg.server.user}"` : 'off'})`);
    log.info(`data: ${cfg.dataDir}`);
    if (!auth && !isLoopback(cfg.server.host)) {
      log.warn(
        `HOST=${cfg.server.host} is not a loopback address and DASHBOARD_PASSWORD is empty: anyone who can reach this ` +
          'port can see and change your settings and bets. Set DASHBOARD_PASSWORD, or keep the port private ' +
          '(Docker Compose publishes it on 127.0.0.1 only).',
      );
    }
    if (this.notifier.enabled && this.mode === 'demo') log.info('alerts: configured, but not sent in demo mode');
    else if (this.notifier.enabled) log.info(`alerts: on (minimum urgency ${cfg.notify.minUrgency})`);
  }
}

/** Builds and starts the whole service. Throws when the data directory or the port is unusable. */
export async function startApp(cfg: AppConfig, opts: AppOptions = {}): Promise<RunningApp> {
  setLogLevel(cfg.logLevel);
  ensureDataDir(cfg.dataDir);
  const app = new OddsHub(cfg, opts);
  await app.start();
  return app;
}

// ---------------------------------------------------------------------------------------------------------------
// process entry point

async function main(): Promise<void> {
  loadDotEnv();
  let cfg: AppConfig;
  try {
    cfg = loadConfig(process.env, process.argv);
  } catch (err) {
    process.stderr.write(`Invalid configuration: ${errorText(err)}\nFix the value in .env (see .env.example) and restart.\n`);
    process.exit(1);
  }
  setLogLevel(cfg.logLevel);

  process.on('unhandledRejection', (reason) => {
    log.error('unhandled promise rejection (kept running)', {
      error: errorText(reason),
      stack: reason instanceof Error ? reason.stack?.split('\n').slice(0, 6).join(' | ') : undefined,
    });
  });
  process.on('uncaughtException', (err) => {
    log.error('uncaught exception (kept running)', {
      error: errorText(err),
      stack: err.stack?.split('\n').slice(0, 6).join(' | '),
    });
  });

  let app: RunningApp;
  try {
    app = await startApp(cfg);
  } catch (err) {
    const code = (err as NodeJS.ErrnoException).code;
    if (code === 'EACCES' || code === 'EPERM' || code === 'EROFS') {
      log.error(
        `DATA_DIR ${cfg.dataDir} is not writable (${code}). Settings and bets are stored there. ` +
          'Under Docker run `sudo chown -R 1000:1000 data` next to docker-compose.yml, then restart.',
      );
    } else if (code === 'EADDRINUSE') {
      log.error(`Port ${cfg.server.port} on ${cfg.server.host} is already in use. Stop the other process or set PORT.`);
    } else {
      log.error('startup failed', { error: errorText(err) });
    }
    process.exit(1);
  }

  let shuttingDown = false;
  const shutdown = (signal: string): void => {
    if (shuttingDown) {
      log.warn(`${signal} again, exiting immediately`);
      process.exit(1);
    }
    shuttingDown = true;
    log.info(`${signal} received, shutting down`);
    const force = setTimeout(() => {
      log.warn('shutdown took too long, exiting');
      process.exit(0);
    }, STOP_TIMEOUT_MS + 500);
    force.unref();
    app
      .stop()
      .catch((err: unknown) => log.warn('error during shutdown', { error: errorText(err) }))
      .finally(() => {
        log.info('stopped');
        process.exit(0);
      });
  };
  process.on('SIGTERM', () => shutdown('SIGTERM'));
  process.on('SIGINT', () => shutdown('SIGINT'));
}

if (require.main === module) {
  main().catch((err: unknown) => {
    process.stderr.write(`Fatal: ${errorText(err)}\n`);
    process.exit(1);
  });
}

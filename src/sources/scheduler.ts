/**
 * Budget-aware poll planner for The Odds API.
 *
 * Every odds call costs credits (markets x ceil(books / 10)), and the monthly quota is small compared to how often a
 * live market moves. The scheduler spreads the credits that are left until the next quota reset over the leagues that
 * actually have something worth pricing (live games, or games starting within the pre-match horizon), giving live
 * leagues the biggest share. The free events endpoint is used to learn start times, so a league with nothing on
 * never costs a credit.
 *
 * Pure state machine: no timers, no I/O. Every method takes the current time so it is fully deterministic in tests.
 */
import type { AppConfig } from '../config';
import type { LeagueDef, LeagueKey } from '../types';

export interface LeagueScheduleState {
  league: LeagueKey;
  lastPollAt: number | null;
  nextDueAt: number | null;
  intervalSec: number | null;
  liveCount: number;
  nextStartTime: number | null;
  eventsRefreshedAt: number | null;
  unavailableUntil: number | null;
  consecutiveErrors: number;
}

export interface PollPlan {
  league: LeagueDef;
  reason: 'live' | 'prematch';
  intervalSec: number;
}

type OddsApiConfig = AppConfig['oddsApi'];

const HOUR_MS = 3_600_000;
/** An event counts as live from its start until 4 hours later (unless a feed says otherwise sooner). */
const LIVE_WINDOW_MS = 4 * HOUR_MS;
/** Event list refresh cadence (free endpoint). */
const EVENTS_MAX_AGE_MS = 10 * 60_000;
const EVENTS_HOT_MAX_AGE_MS = 2 * 60_000;
const EVENTS_HOT_WINDOW_MS = 60 * 60_000;
/** Retry backoff for a failing events refresh (see markEventRefreshFailed). */
const EVENTS_RETRY_BASE_MS = 30_000;
const EVENTS_RETRY_MAX_MS = 10 * 60_000;
/** Pre-match leagues with a start inside this window get a double share of credits. */
const SOON_WINDOW_MS = 2 * HOUR_MS;
const WEIGHT_LIVE = 4;
const WEIGHT_SOON = 2;
const WEIGHT_OTHER = 1;
/** Odds poll error backoff: 15 s, 30 s, 60 s, ... capped at 10 min. */
const ERROR_BACKOFF_BASE_MS = 15_000;
const ERROR_BACKOFF_MAX_MS = 10 * 60_000;
/** msUntilNextDue never asks the caller to sleep longer than this. */
const MAX_WAIT_MS = 60_000;
/** Bounded memory: start times kept per league. */
const MAX_START_TIMES = 500;
const MAX_TRACKED_ERRORS = 1_000;

interface LeagueEntry {
  def: LeagueDef;
  order: number;
  state: LeagueScheduleState;
  /** Sorted start times of events that were live or upcoming at the last events refresh. */
  startTimes: number[];
  /** When liveCount was last set (events refresh or odds poll). */
  liveUpdatedAt: number | null;
  eventFailures: number;
  eventRetryAt: number | null;
}

interface PlannedLeague {
  entry: LeagueEntry;
  live: boolean;
  weight: number;
  intervalSec: number;
  /** Epoch ms when the league is due; -Infinity when it has never been polled. */
  dueAt: number;
}

function isFiniteNumber(v: unknown): v is number {
  return typeof v === 'number' && Number.isFinite(v);
}

function errorBackoffMs(consecutiveErrors: number): number {
  if (consecutiveErrors <= 0) return 0;
  return Math.min(ERROR_BACKOFF_BASE_MS * 2 ** Math.min(consecutiveErrors - 1, 30), ERROR_BACKOFF_MAX_MS);
}

export class PollScheduler {
  private readonly cfg: OddsApiConfig;
  private readonly entries = new Map<LeagueKey, LeagueEntry>();
  private enabled: Set<LeagueKey>;
  /** Latest clock value seen by any method; used to derive time-dependent fields in snapshot(). */
  private lastAt: number | null = null;

  constructor(cfg: OddsApiConfig, leagues: LeagueDef[]) {
    this.cfg = cfg;
    let order = 0;
    for (const def of leagues) {
      if (this.entries.has(def.key)) continue;
      this.entries.set(def.key, {
        def: { ...def },
        order: order++,
        state: {
          league: def.key,
          lastPollAt: null,
          nextDueAt: null,
          intervalSec: null,
          liveCount: 0,
          nextStartTime: null,
          eventsRefreshedAt: null,
          unavailableUntil: null,
          consecutiveErrors: 0,
        },
        startTimes: [],
        liveUpdatedAt: null,
        eventFailures: 0,
        eventRetryAt: null,
      });
    }
    // Until told otherwise every league passed in is enabled.
    this.enabled = new Set(this.entries.keys());
  }

  /** Restricts polling to these leagues (unknown keys are ignored). State of disabled leagues is kept. */
  setEnabledLeagues(keys: LeagueKey[]): void {
    this.enabled = new Set(keys.filter((k) => this.entries.has(k)));
  }

  /** Records the result of a (free) events refresh for a league. */
  updateEvents(league: LeagueKey, events: { startTime: number }[], at: number): void {
    const entry = this.entries.get(league);
    if (!entry) return;
    this.touch(at);
    const starts: number[] = [];
    let live = 0;
    for (const ev of events) {
      const start = ev?.startTime;
      if (!isFiniteNumber(start)) continue;
      if (start <= at && at < start + LIVE_WINDOW_MS) live++;
      if (start + LIVE_WINDOW_MS > at) starts.push(start);
    }
    starts.sort((a, b) => a - b);
    entry.startTimes = starts.length > MAX_START_TIMES ? starts.slice(0, MAX_START_TIMES) : starts;
    entry.liveUpdatedAt = at;
    entry.eventFailures = 0;
    entry.eventRetryAt = null;
    entry.state.liveCount = live;
    entry.state.eventsRefreshedAt = at;
    entry.state.nextStartTime = this.nextStart(entry, at);
  }

  /**
   * Optional: tells the scheduler an events refresh failed so the league is retried with backoff
   * (30 s, 60 s, ... up to 10 min) instead of on every loop iteration. A successful updateEvents clears it.
   */
  markEventRefreshFailed(league: LeagueKey, at: number): void {
    const entry = this.entries.get(league);
    if (!entry) return;
    this.touch(at);
    entry.eventFailures = Math.min(entry.eventFailures + 1, MAX_TRACKED_ERRORS);
    const delay = Math.min(EVENTS_RETRY_BASE_MS * 2 ** Math.min(entry.eventFailures - 1, 30), EVENTS_RETRY_MAX_MS);
    entry.eventRetryAt = at + delay;
  }

  /**
   * Leagues whose event list should be refreshed now: never refreshed, older than 10 min, or older than 2 min
   * when the league is live or has a start within the next 60 min. Unavailable leagues are skipped.
   */
  leaguesNeedingEventRefresh(at: number): LeagueDef[] {
    this.touch(at);
    const out: LeagueDef[] = [];
    for (const entry of this.eligible(at)) {
      if (entry.eventRetryAt !== null && at < entry.eventRetryAt) continue;
      const refreshedAt = entry.state.eventsRefreshedAt;
      if (refreshedAt === null) {
        out.push(entry.def);
        continue;
      }
      const age = at - refreshedAt;
      if (age >= EVENTS_MAX_AGE_MS) {
        out.push(entry.def);
        continue;
      }
      if (age >= EVENTS_HOT_MAX_AGE_MS) {
        const next = this.nextStart(entry, at);
        const startingSoon = next !== null && next - at <= EVENTS_HOT_WINDOW_MS;
        if (startingSoon || this.isLive(entry, at)) out.push(entry.def);
      }
    }
    return out;
  }

  /** Credits that may be spent per hour so the quota (minus the reserve) lasts until the next reset. */
  creditsPerHour(remaining: number | null, at: number): number {
    const hoursUntilReset = (this.nextResetAt(at) - at) / HOUR_MS;
    return Math.max(0, this.spendable(remaining)) / Math.max(1, hoursUntilReset);
  }

  /** Next occurrence of cfg.resetDayOfMonth at 00:00 UTC strictly after `at`. */
  nextResetAt(at: number): number {
    const raw = Math.round(this.cfg.resetDayOfMonth);
    const day = Number.isFinite(raw) ? Math.min(28, Math.max(1, raw)) : 1;
    const d = new Date(at);
    const thisMonth = Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), day);
    if (thisMonth > at) return thisMonth;
    return Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + 1, day);
  }

  /** The league to poll now, or null when nothing is due or the budget cannot pay for a call. */
  nextDue(at: number, remaining: number | null, costPerCall: number): PollPlan | null {
    this.touch(at);
    if (this.isBudgetExhausted(remaining, costPerCall)) {
      this.clearPlanFields();
      return null;
    }
    const plan = this.plan(at, remaining, costPerCall);
    let best: PlannedLeague | null = null;
    for (const p of plan) {
      if (p.dueAt > at) continue;
      if (best === null || this.before(p, best)) best = p;
    }
    if (!best) return null;
    return { league: best.entry.def, reason: best.live ? 'live' : 'prematch', intervalSec: best.intervalSec };
  }

  /** Milliseconds until the earliest league becomes due: 0 if one is due now; 60 000 when nothing is active. */
  msUntilNextDue(at: number, remaining: number | null, costPerCall: number): number {
    this.touch(at);
    if (this.isBudgetExhausted(remaining, costPerCall)) {
      this.clearPlanFields();
      return MAX_WAIT_MS;
    }
    const plan = this.plan(at, remaining, costPerCall);
    if (plan.length === 0) return MAX_WAIT_MS;
    let wait = MAX_WAIT_MS;
    for (const p of plan) wait = Math.min(wait, p.dueAt - at);
    return Math.max(0, Math.min(MAX_WAIT_MS, Math.ceil(wait)));
  }

  /**
   * Records an odds poll. `ok=false` increments the error counter (adds backoff); `ok=true` resets it.
   * `liveCount` (from the odds snapshot) overrides the count derived from the events list.
   */
  markPolled(league: LeagueKey, at: number, ok: boolean, liveCount?: number): void {
    const entry = this.entries.get(league);
    if (!entry) return;
    this.touch(at);
    entry.state.lastPollAt = at;
    entry.state.consecutiveErrors = ok ? 0 : Math.min(entry.state.consecutiveErrors + 1, MAX_TRACKED_ERRORS);
    if (isFiniteNumber(liveCount) && liveCount >= 0) {
      entry.state.liveCount = Math.floor(liveCount);
      entry.liveUpdatedAt = at;
    }
  }

  /** League is out of season / unknown to the feed until `until`: no odds polls and no event refreshes. */
  markUnavailable(league: LeagueKey, until: number): void {
    const entry = this.entries.get(league);
    if (!entry) return;
    entry.state.unavailableUntil = isFiniteNumber(until) ? until : null;
  }

  /** True when the credits above the reserve cannot pay for one call (`costPerCall`, default 1). */
  isBudgetExhausted(remaining: number | null, costPerCall = 1): boolean {
    return this.spendable(remaining) < this.sanitizeCost(costPerCall);
  }

  /** State of every enabled league that has an Odds API sport key, in configuration order. */
  snapshot(): LeagueScheduleState[] {
    const out: LeagueScheduleState[] = [];
    for (const entry of this.entries.values()) {
      if (!this.enabled.has(entry.def.key) || !entry.def.oddsApiKey) continue;
      const state = { ...entry.state };
      if (this.lastAt !== null) state.nextStartTime = this.nextStart(entry, this.lastAt);
      out.push(state);
    }
    return out;
  }

  // ---------------------------------------------------------------------------------------------------------------

  private touch(at: number): void {
    if (isFiniteNumber(at) && (this.lastAt === null || at > this.lastAt)) this.lastAt = at;
  }

  private spendable(remaining: number | null): number {
    const credits = isFiniteNumber(remaining) ? remaining : this.cfg.monthlyCredits;
    return credits - this.cfg.reserveCredits;
  }

  private sanitizeCost(costPerCall: number): number {
    return isFiniteNumber(costPerCall) && costPerCall > 0 ? costPerCall : 1;
  }

  /** Enabled leagues with a sport key that are not marked unavailable at `at`. */
  private eligible(at: number): LeagueEntry[] {
    const out: LeagueEntry[] = [];
    for (const entry of this.entries.values()) {
      if (!this.enabled.has(entry.def.key) || !entry.def.oddsApiKey) continue;
      const until = entry.state.unavailableUntil;
      if (until !== null && until > at) continue;
      out.push(entry);
    }
    return out;
  }

  private nextStart(entry: LeagueEntry, at: number): number | null {
    for (const s of entry.startTimes) if (s > at) return s;
    return null;
  }

  private isLive(entry: LeagueEntry, at: number): boolean {
    if (entry.state.liveCount <= 0 || entry.liveUpdatedAt === null) return false;
    // A live count nobody has confirmed for 4 hours is stale.
    return at - entry.liveUpdatedAt < LIVE_WINDOW_MS;
  }

  /** Orders due leagues: live first, then the most overdue, then configuration order. */
  private before(a: PlannedLeague, b: PlannedLeague): boolean {
    if (a.live !== b.live) return a.live;
    if (a.dueAt !== b.dueAt) return a.dueAt < b.dueAt;
    return a.entry.order < b.entry.order;
  }

  private clearPlanFields(): void {
    for (const entry of this.entries.values()) {
      entry.state.intervalSec = null;
      entry.state.nextDueAt = null;
    }
  }

  /**
   * Computes the interval and due time of every active league and stores them in the league state for snapshot().
   *
   * share_i = creditsPerHour * w_i / Σw, interval_i = clamp(3600 * cost / share_i, minInterval, maxInterval).
   * If clamping to maxIntervalSec would make the plan spend more than creditsPerHour, the leagues held at the max
   * get only the budget the others leave over, so the quota always lasts until the reset day.
   */
  private plan(at: number, remaining: number | null, costPerCall: number): PlannedLeague[] {
    const cost = this.sanitizeCost(costPerCall);
    const cph = this.creditsPerHour(remaining, at);
    const horizonMs = Math.max(0, this.cfg.prematchHorizonHours) * HOUR_MS;

    this.clearPlanFields();
    const active: Array<{ entry: LeagueEntry; live: boolean; weight: number }> = [];
    for (const entry of this.eligible(at)) {
      const live = this.isLive(entry, at);
      const next = this.nextStart(entry, at);
      const upcoming = next !== null && next - at <= horizonMs;
      if (!live && !upcoming) continue;
      const weight = live ? WEIGHT_LIVE : next !== null && next - at <= SOON_WINDOW_MS ? WEIGHT_SOON : WEIGHT_OTHER;
      active.push({ entry, live, weight });
    }
    if (active.length === 0) return [];

    const totalWeight = active.reduce((sum, a) => sum + a.weight, 0);
    const callSeconds = 3600 * cost; // interval (s) = callSeconds / (credits per hour for this league)
    const sized = active.map((a) => {
      const share = (cph * a.weight) / totalWeight;
      const raw = share > 0 ? callSeconds / share : Number.POSITIVE_INFINITY;
      const floor = a.live ? this.cfg.minIntervalLiveSec : this.cfg.minIntervalPrematchSec;
      const ceiling = Math.max(floor, this.cfg.maxIntervalSec);
      const interval = Math.min(ceiling, Math.max(floor, raw));
      return { ...a, interval, heldAtMax: raw > ceiling };
    });

    if (cph > 0) {
      const spend = sized.reduce((sum, s) => sum + callSeconds / s.interval, 0);
      if (spend > cph * (1 + 1e-9)) {
        const held = sized.filter((s) => s.heldAtMax);
        const heldWeight = held.reduce((sum, s) => sum + s.weight, 0);
        const fixedSpend = sized.filter((s) => !s.heldAtMax).reduce((sum, s) => sum + callSeconds / s.interval, 0);
        const left = Math.max(0, cph - fixedSpend);
        for (const s of held) {
          const share = heldWeight > 0 ? (left * s.weight) / heldWeight : 0;
          if (share > 0) s.interval = Math.max(s.interval, callSeconds / share);
        }
      }
    }

    return sized.map((s) => {
      const intervalSec = Math.ceil(s.interval);
      const last = s.entry.state.lastPollAt;
      const dueAt =
        last === null
          ? Number.NEGATIVE_INFINITY
          : last + intervalSec * 1000 + errorBackoffMs(s.entry.state.consecutiveErrors);
      s.entry.state.intervalSec = intervalSec;
      s.entry.state.nextDueAt = last === null ? at : dueAt;
      return { entry: s.entry, live: s.live, weight: s.weight, intervalSec, dueAt };
    });
  }
}

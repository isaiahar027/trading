import { describe, expect, it } from 'vitest';
import { loadConfig, type AppConfig } from '../src/config';
import { PollScheduler } from '../src/sources/scheduler';
import type { LeagueDef } from '../src/types';

type OddsCfg = AppConfig['oddsApi'];

const MIN = 60_000;
const HOUR = 60 * MIN;
const COST = 3;

function cfg(overrides: Partial<OddsCfg> = {}): OddsCfg {
  // Defaults: 20 000 credits/month, reserve 200, reset day 1, live min 20 s, pre-match min 180 s, max 1800 s, 24 h horizon.
  return { ...loadConfig({}, []).oddsApi, ...overrides };
}

function league(key: string, oddsApiKey: string | null = key.toLowerCase()): LeagueDef {
  return { key, name: key, oddsApiKey, threeWay: false };
}

const NBA = league('NBA', 'basketball_nba');
const NFL = league('NFL', 'americanfootball_nfl');
const MLB = league('MLB', 'baseball_mlb');
const NHL = league('NHL', 'icehockey_nhl');

/** 2026-09-01T00:00Z: exactly 30 days (720 h) before the next reset on day 1. */
const T0 = Date.UTC(2026, 8, 1, 0, 0, 0);

function stateOf(s: PollScheduler, key: string) {
  const st = s.snapshot().find((x) => x.league === key);
  if (!st) throw new Error(`no state for ${key}`);
  return st;
}

describe('PollScheduler.nextResetAt', () => {
  it('returns the next reset day at 00:00 UTC', () => {
    const s = new PollScheduler(cfg({ resetDayOfMonth: 1 }), [NBA]);
    expect(s.nextResetAt(Date.UTC(2026, 8, 23, 12, 0, 0))).toBe(Date.UTC(2026, 9, 1));
  });

  it('is strictly after `at` (exactly at the reset instant -> next month)', () => {
    const s = new PollScheduler(cfg({ resetDayOfMonth: 1 }), [NBA]);
    expect(s.nextResetAt(Date.UTC(2026, 9, 1))).toBe(Date.UTC(2026, 10, 1));
    expect(s.nextResetAt(Date.UTC(2026, 9, 1) - 1)).toBe(Date.UTC(2026, 9, 1));
  });

  it('handles end of month and end of year', () => {
    const s = new PollScheduler(cfg({ resetDayOfMonth: 1 }), [NBA]);
    expect(s.nextResetAt(Date.UTC(2026, 0, 31, 23, 59, 59, 999))).toBe(Date.UTC(2026, 1, 1));
    expect(s.nextResetAt(Date.UTC(2026, 11, 31, 23, 59, 59))).toBe(Date.UTC(2027, 0, 1));
    const s28 = new PollScheduler(cfg({ resetDayOfMonth: 28 }), [NBA]);
    expect(s28.nextResetAt(Date.UTC(2026, 1, 27, 23, 0))).toBe(Date.UTC(2026, 1, 28));
    expect(s28.nextResetAt(Date.UTC(2026, 1, 28, 0, 0, 0, 1))).toBe(Date.UTC(2026, 2, 28));
    expect(s28.nextResetAt(Date.UTC(2026, 11, 29))).toBe(Date.UTC(2027, 0, 28));
  });

  it('same day: before midnight UTC -> in minutes; after midnight UTC -> next month', () => {
    const s = new PollScheduler(cfg({ resetDayOfMonth: 15 }), [NBA]);
    expect(s.nextResetAt(Date.UTC(2026, 8, 14, 23, 30))).toBe(Date.UTC(2026, 8, 15));
    expect(s.nextResetAt(Date.UTC(2026, 8, 15, 0, 30))).toBe(Date.UTC(2026, 9, 15));
  });
});

describe('PollScheduler.creditsPerHour', () => {
  const s = new PollScheduler(cfg(), [NBA]);

  it('spreads credits above the reserve over the hours left until reset', () => {
    expect(s.creditsPerHour(null, T0)).toBeCloseTo((20_000 - 200) / 720, 9);
    expect(s.creditsPerHour(10_200, T0)).toBeCloseTo(10_000 / 720, 9);
  });

  it('never goes negative when remaining is at or below the reserve', () => {
    expect(s.creditsPerHour(200, T0)).toBe(0);
    expect(s.creditsPerHour(50, T0)).toBe(0);
  });

  it('divides by at least one hour right before the reset', () => {
    const at = Date.UTC(2026, 8, 30, 23, 30);
    expect(s.creditsPerHour(1_200, at)).toBeCloseTo(1_000, 9);
  });
});

describe('PollScheduler budget', () => {
  it('isBudgetExhausted compares spendable credits with the call cost', () => {
    const s = new PollScheduler(cfg(), [NBA]);
    expect(s.isBudgetExhausted(null)).toBe(false);
    expect(s.isBudgetExhausted(200)).toBe(true);
    expect(s.isBudgetExhausted(201)).toBe(false);
    expect(s.isBudgetExhausted(202, COST)).toBe(true);
    expect(s.isBudgetExhausted(203, COST)).toBe(false);
  });

  it('nextDue returns null when the budget cannot pay for a call', () => {
    const s = new PollScheduler(cfg(), [NBA]);
    s.updateEvents('NBA', [{ startTime: T0 - 30 * MIN }], T0);
    expect(s.nextDue(T0, 202, COST)).toBeNull();
    expect(s.nextDue(T0, 0, COST)).toBeNull();
    expect(s.msUntilNextDue(T0, 0, COST)).toBe(60_000);
    expect(s.nextDue(T0, 203, COST)?.league.key).toBe('NBA');
  });
});

describe('PollScheduler intervals', () => {
  it('polls live leagues more often than pre-match leagues', () => {
    const s = new PollScheduler(cfg(), [NBA, NFL]);
    s.updateEvents('NBA', [{ startTime: T0 - 30 * MIN }], T0); // live
    s.updateEvents('NFL', [{ startTime: T0 + 5 * HOUR }], T0); // pre-match, > 2 h away
    const first = s.nextDue(T0, null, COST);
    expect(first).toMatchObject({ reason: 'live' });
    expect(first?.league.key).toBe('NBA');
    const live = stateOf(s, 'NBA').intervalSec ?? 0;
    const pre = stateOf(s, 'NFL').intervalSec ?? 0;
    expect(live).toBeGreaterThan(0);
    expect(pre).toBeGreaterThan(live * 3);

    // Drive the scheduler for two simulated hours and count polls per league.
    const polls: Record<string, number> = { NBA: 0, NFL: 0 };
    for (let at = T0; at < T0 + 2 * HOUR; at += 5_000) {
      const plan = s.nextDue(at, null, COST);
      if (!plan) continue;
      polls[plan.league.key]++;
      s.markPolled(plan.league.key, at, true);
    }
    expect(polls.NBA).toBeGreaterThan(polls.NFL * 3);
    expect(polls.NFL).toBeGreaterThanOrEqual(1);
  });

  it('computes the documented shares: live 4, starting within 2 h 2, other 1', () => {
    const s = new PollScheduler(cfg(), [NBA, NFL, MLB]);
    s.updateEvents('NBA', [{ startTime: T0 - 10 * MIN }], T0);
    s.updateEvents('NFL', [{ startTime: T0 + 90 * MIN }], T0);
    s.updateEvents('MLB', [{ startTime: T0 + 10 * HOUR }], T0);
    // Large budget so no league hits the 1800 s max: remaining 100 200 -> 100 000 / 720 h credits per hour.
    s.nextDue(T0, 100_200, COST);
    const cph = 100_000 / 720;
    const expected = (w: number) => Math.ceil((3600 * COST) / ((cph * w) / 7));
    expect(stateOf(s, 'NBA').intervalSec).toBe(expected(4));
    expect(stateOf(s, 'NFL').intervalSec).toBe(Math.max(180, expected(2)));
    expect(stateOf(s, 'MLB').intervalSec).toBe(expected(1));
  });

  it('clamps intervals to the configured minimums when credits are plentiful', () => {
    const s = new PollScheduler(cfg(), [NBA, NFL]);
    s.updateEvents('NBA', [{ startTime: T0 - 30 * MIN }], T0);
    s.updateEvents('NFL', [{ startTime: T0 + 5 * HOUR }], T0);
    s.nextDue(T0, 10_000_000, COST);
    expect(stateOf(s, 'NBA').intervalSec).toBe(20);
    expect(stateOf(s, 'NFL').intervalSec).toBe(180);
  });

  it('clamps to maxIntervalSec when the budget can afford it', () => {
    // Live league held at a high floor (900 s) leaves credits over, so the pre-match league can be held at the max.
    const s = new PollScheduler(cfg({ minIntervalLiveSec: 900 }), [NBA, NFL]);
    s.updateEvents('NBA', [{ startTime: T0 - 30 * MIN }], T0);
    s.updateEvents('NFL', [{ startTime: T0 + 5 * HOUR }], T0);
    // 16 400 remaining -> 22.5 credits/h: raw live 600 s (-> floor 900), raw pre-match 2400 s (-> max 1800).
    s.nextDue(T0, 16_400, COST);
    expect(stateOf(s, 'NBA').intervalSec).toBe(900);
    expect(stateOf(s, 'NFL').intervalSec).toBe(1800);
  });

  it('stretches past maxIntervalSec rather than overspend the monthly budget', () => {
    const keys = ['L1', 'L2', 'L3', 'L4', 'L5', 'L6', 'L7', 'L8', 'L9', 'L10'];
    const s = new PollScheduler(cfg(), keys.map((k) => league(k)));
    for (const k of keys) s.updateEvents(k, [{ startTime: T0 + 6 * HOUR }], T0);
    s.nextDue(T0, null, COST);
    const cph = s.creditsPerHour(null, T0);
    let spend = 0;
    for (const st of s.snapshot()) {
      expect(st.intervalSec).toBeGreaterThan(1800);
      spend += (3600 * COST) / (st.intervalSec ?? 1);
    }
    expect(spend).toBeLessThanOrEqual(cph + 1e-9);
  });

  it('never makes inactive leagues due', () => {
    const s = new PollScheduler(cfg(), [NBA, NFL, MLB]);
    // NBA: never refreshed. NFL: next game beyond the 24 h horizon. MLB: only a game that ended long ago.
    s.updateEvents('NFL', [{ startTime: T0 + 30 * HOUR }], T0);
    s.updateEvents('MLB', [{ startTime: T0 - 5 * HOUR }], T0);
    for (let at = T0; at < T0 + HOUR; at += MIN) expect(s.nextDue(at, null, COST)).toBeNull();
    expect(s.msUntilNextDue(T0, null, COST)).toBe(60_000);
    expect(stateOf(s, 'NFL').intervalSec).toBeNull();
    expect(stateOf(s, 'MLB').liveCount).toBe(0);
  });
});

describe('PollScheduler due logic', () => {
  it('prefers live leagues, then the most overdue', () => {
    const s = new PollScheduler(cfg(), [NBA, NFL, MLB]);
    s.updateEvents('NBA', [{ startTime: T0 + 3 * HOUR }], T0);
    s.updateEvents('NFL', [{ startTime: T0 + 3 * HOUR }], T0);
    s.updateEvents('MLB', [{ startTime: T0 - 20 * MIN }], T0);
    expect(s.nextDue(T0, null, COST)?.league.key).toBe('MLB');
    s.markPolled('MLB', T0, true);
    s.markPolled('NBA', T0 - 2 * HOUR, true);
    s.markPolled('NFL', T0 - 3 * HOUR, true);
    // Both pre-match leagues are overdue; NFL was polled longer ago.
    expect(s.nextDue(T0 + 1_000, null, COST)?.league.key).toBe('NFL');
  });

  it('adds error backoff of 15 s doubling up to 10 min, and resets it on success', () => {
    const s = new PollScheduler(cfg(), [NBA]);
    s.updateEvents('NBA', [{ startTime: T0 - 30 * MIN }], T0);
    expect(s.nextDue(T0, null, COST)?.league.key).toBe('NBA');
    const interval = stateOf(s, 'NBA').intervalSec ?? 0;
    const ms = interval * 1000;

    s.markPolled('NBA', T0, false);
    expect(s.nextDue(T0 + ms, null, COST)).toBeNull();
    expect(s.nextDue(T0 + ms + 15_000, null, COST)?.league.key).toBe('NBA');

    s.markPolled('NBA', T0, false);
    expect(stateOf(s, 'NBA').consecutiveErrors).toBe(2);
    expect(s.nextDue(T0 + ms + 29_999, null, COST)).toBeNull();
    expect(s.nextDue(T0 + ms + 30_000, null, COST)).not.toBeNull();

    for (let i = 0; i < 20; i++) s.markPolled('NBA', T0, false);
    expect(s.nextDue(T0 + ms + 10 * MIN - 1, null, COST)).toBeNull();
    expect(s.nextDue(T0 + ms + 10 * MIN, null, COST)).not.toBeNull();

    s.markPolled('NBA', T0, true);
    expect(stateOf(s, 'NBA').consecutiveErrors).toBe(0);
    expect(s.nextDue(T0 + ms, null, COST)).not.toBeNull();
  });

  it('skips unavailable leagues for polling and event refresh until the mark expires', () => {
    const s = new PollScheduler(cfg(), [NBA, NFL]);
    s.updateEvents('NBA', [{ startTime: T0 - 30 * MIN }], T0);
    s.markUnavailable('NBA', T0 + 6 * HOUR);
    expect(s.nextDue(T0, null, COST)).toBeNull();
    expect(s.leaguesNeedingEventRefresh(T0 + HOUR).map((l) => l.key)).toEqual(['NFL']);
    const later = T0 + 6 * HOUR + 1;
    expect(s.leaguesNeedingEventRefresh(later).map((l) => l.key)).toEqual(['NBA', 'NFL']);
    s.updateEvents('NBA', [{ startTime: later - 10 * MIN }], later);
    expect(s.nextDue(later, null, COST)?.league.key).toBe('NBA');
  });

  it('msUntilNextDue: 0 when due, time to the earliest due league, capped at 60 s', () => {
    const s = new PollScheduler(cfg(), [NBA]);
    expect(s.msUntilNextDue(T0, null, COST)).toBe(60_000); // nothing active yet
    s.updateEvents('NBA', [{ startTime: T0 - 30 * MIN }], T0);
    expect(s.msUntilNextDue(T0, null, COST)).toBe(0);

    // Plenty of credits: live interval hits the 20 s floor.
    s.markPolled('NBA', T0, true);
    expect(s.msUntilNextDue(T0 + 5_000, 10_000_000, COST)).toBe(15_000);
    expect(s.msUntilNextDue(T0 + 25_000, 10_000_000, COST)).toBe(0);
    // Default budget: interval is minutes long, the wait is capped.
    expect(s.msUntilNextDue(T0 + 5_000, null, COST)).toBe(60_000);
  });

  it('setEnabledLeagues restricts polling, ignores unknown keys and keeps state', () => {
    const s = new PollScheduler(cfg(), [NBA, NFL, league('XFL', null)]);
    s.updateEvents('NBA', [{ startTime: T0 - 30 * MIN }], T0);
    s.updateEvents('NFL', [{ startTime: T0 + 3 * HOUR }], T0);
    s.markPolled('NBA', T0 - HOUR, true);

    s.setEnabledLeagues(['NFL', 'NOPE']);
    expect(s.snapshot().map((x) => x.league)).toEqual(['NFL']);
    expect(s.nextDue(T0, null, COST)?.league.key).toBe('NFL');
    expect(s.leaguesNeedingEventRefresh(T0 + 11 * MIN).map((l) => l.key)).toEqual(['NFL']);

    s.setEnabledLeagues(['NBA', 'NFL', 'XFL']);
    // XFL has no Odds API key, so it is never scheduled or reported.
    expect(s.snapshot().map((x) => x.league)).toEqual(['NBA', 'NFL']);
    expect(stateOf(s, 'NBA').lastPollAt).toBe(T0 - HOUR);
    expect(s.nextDue(T0, null, COST)?.league.key).toBe('NBA');
  });
});

describe('PollScheduler events', () => {
  it('derives liveCount and nextStartTime from events; markPolled overrides liveCount', () => {
    const s = new PollScheduler(cfg(), [NBA]);
    s.updateEvents(
      'NBA',
      [
        { startTime: T0 - 5 * HOUR }, // finished (> 4 h ago)
        { startTime: T0 - 3 * HOUR }, // live
        { startTime: T0 }, // starts now -> live
        { startTime: T0 + 2 * HOUR },
        { startTime: T0 + HOUR },
        { startTime: Number.NaN },
      ],
      T0,
    );
    const st = stateOf(s, 'NBA');
    expect(st.liveCount).toBe(2);
    expect(st.nextStartTime).toBe(T0 + HOUR);
    expect(st.eventsRefreshedAt).toBe(T0);

    s.markPolled('NBA', T0 + MIN, true, 5);
    expect(stateOf(s, 'NBA').liveCount).toBe(5);
    s.markPolled('NBA', T0 + 2 * MIN, true);
    expect(stateOf(s, 'NBA').liveCount).toBe(5);
    s.markPolled('NBA', T0 + 3 * MIN, true, 0);
    expect(stateOf(s, 'NBA').liveCount).toBe(0);
  });

  it('refreshes events: never refreshed, every 10 min, every 2 min when live or a start is within 60 min', () => {
    const s = new PollScheduler(cfg(), [NBA, NFL, MLB]);
    const keys = (at: number) => s.leaguesNeedingEventRefresh(at).map((l) => l.key);
    expect(keys(T0)).toEqual(['NBA', 'NFL', 'MLB']);

    s.updateEvents('NBA', [{ startTime: T0 + 5 * HOUR }], T0); // quiet
    s.updateEvents('NFL', [{ startTime: T0 + 45 * MIN }], T0); // starting soon
    s.updateEvents('MLB', [{ startTime: T0 - 30 * MIN }], T0); // live
    expect(keys(T0 + MIN)).toEqual([]);
    expect(keys(T0 + 2 * MIN)).toEqual(['NFL', 'MLB']);
    expect(keys(T0 + 9 * MIN)).toEqual(['NFL', 'MLB']);
    expect(keys(T0 + 10 * MIN)).toEqual(['NBA', 'NFL', 'MLB']);
  });

  it('backs off event refresh retries after failures (optional helper)', () => {
    const s = new PollScheduler(cfg(), [NBA]);
    s.markEventRefreshFailed('NBA', T0);
    expect(s.leaguesNeedingEventRefresh(T0 + 29_999)).toEqual([]);
    expect(s.leaguesNeedingEventRefresh(T0 + 30_000).map((l) => l.key)).toEqual(['NBA']);
    s.markEventRefreshFailed('NBA', T0 + 30_000);
    expect(s.leaguesNeedingEventRefresh(T0 + 30_000 + 59_999)).toEqual([]);
    s.updateEvents('NBA', [], T0 + 2 * MIN);
    expect(s.leaguesNeedingEventRefresh(T0 + 12 * MIN).map((l) => l.key)).toEqual(['NBA']);
  });

  it('ignores unknown leagues and keeps memory bounded', () => {
    const s = new PollScheduler(cfg(), [NBA]);
    s.updateEvents('NOPE', [{ startTime: T0 }], T0);
    s.markPolled('NOPE', T0, true, 3);
    s.markUnavailable('NOPE', T0 + HOUR);
    expect(s.snapshot().map((x) => x.league)).toEqual(['NBA']);
    const many = Array.from({ length: 5_000 }, (_, i) => ({ startTime: T0 + (i + 1) * MIN }));
    s.updateEvents('NBA', many, T0);
    expect(stateOf(s, 'NBA').nextStartTime).toBe(T0 + MIN);
  });
});

describe('PollScheduler clock steps', () => {
  it('keeps polling and refreshing events after the wall clock steps back an hour', () => {
    const s = new PollScheduler(cfg(), [NBA]);
    s.updateEvents('NBA', [{ startTime: T0 - 30 * MIN }], T0);
    expect(s.nextDue(T0, null, COST)?.league.key).toBe('NBA');
    s.markPolled('NBA', T0, true, 1);
    const interval = (stateOf(s, 'NBA').intervalSec ?? 0) * 1000;
    expect(interval).toBeGreaterThan(0);

    const back = T0 + 10_000 - HOUR; // 10 s later in real time, but the clock now reads an hour earlier
    expect(s.nextDue(back, null, COST)).toBeNull(); // polled 10 s ago: not due yet
    expect(s.msUntilNextDue(back, null, COST)).toBeLessThanOrEqual(interval);
    expect(s.nextDue(back + interval, null, COST)?.league.key).toBe('NBA');
    // The live league's event list (refreshed "10 s ago") is due again 2 minutes later, not an hour later.
    expect(s.leaguesNeedingEventRefresh(back + 2 * MIN).map((l) => l.key)).toEqual(['NBA']);
  });
});

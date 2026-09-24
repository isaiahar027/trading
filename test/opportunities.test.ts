import { describe, expect, it } from 'vitest';
import type { AppConfig } from '../src/config';
import { suggestStake } from '../src/engine/kelly';
import { MarketStore } from '../src/engine/marketStore';
import {
  OpportunityTracker,
  computeOpportunities,
  describePick,
  sortOpportunities,
} from '../src/engine/opportunities';
import type { EngineContext } from '../src/engine/opportunities';
import type { LeagueDef, MarketKind, Opportunity, Quote, RawEvent, RuntimeSettings, Side, Urgency, Verdict } from '../src/types';
import { americanToDecimal } from '../src/util/odds';

const NOW = 1_700_000_000_000;
const SEC = 1000;
const MIN = 60 * SEC;
const HOUR = 60 * MIN;

const LEAGUES: LeagueDef[] = [
  { key: 'NBA', name: 'NBA', oddsApiKey: 'basketball_nba', threeWay: false },
  { key: 'EPL', name: 'Premier League', oddsApiKey: 'soccer_epl', threeWay: true },
];

const DESYNC_TEXT = 'DraftKings repriced after the sharp line — the sharp feed may be lagging';

function settings(over: Partial<RuntimeSettings> = {}): RuntimeSettings {
  return {
    bankroll: 1000,
    kellyMultiplier: 0.25,
    maxStakePct: 0.02,
    maxStakeAbs: 100,
    maxDailyExposurePct: 0.15,
    minEvPrematch: 0.02,
    minEvLive: 0.03,
    watchEv: 0.005,
    enabledLeagues: ['NBA', 'EPL'],
    showArbs: false,
    ...over,
  };
}

function model(over: Partial<AppConfig['model']> = {}): AppConfig['model'] {
  return {
    devigMethod: 'multiplicative',
    minConsensusBooks: 3,
    liveMaxSharpAgeSec: 45,
    prematchMaxSharpAgeSec: 900,
    liveMaxDkAgeSec: 45,
    prematchMaxDkAgeSec: 600,
    staleMoveProb: 0.02,
    staleWindowSec: 120,
    maxPlausibleEv: 0.25,
    goneRetentionSec: 90,
    maxDecimalOdds: 11,
    includeAltLines: false,
    ...over,
  };
}

function ctx(
  over: {
    settings?: Partial<RuntimeSettings>;
    model?: Partial<AppConfig['model']>;
    now?: number;
    remainingDailyExposure?: number;
  } = {},
): EngineContext {
  return {
    settings: settings(over.settings),
    model: model(over.model),
    sharpBooks: ['pinnacle', 'betonlineag', 'lowvig'],
    leagues: LEAGUES,
    now: over.now ?? NOW,
    remainingDailyExposure: over.remainingDailyExposure ?? 150,
  };
}

/** Pre-match by default, far enough out (5 h) that no start-time urgency bonus applies. */
function rawEvent(over: Partial<RawEvent> = {}): RawEvent {
  return {
    source: 'odds-api',
    sourceEventId: 'g1',
    league: 'NBA',
    home: 'Celtics',
    away: 'Knicks',
    startTime: NOW + 5 * HOUR,
    isLive: false,
    ...over,
  };
}

const LIVE: Partial<RawEvent> = { isLive: true, startTime: NOW - HOUR };

function quote(book: string, kind: MarketKind, side: Side, line: number | null, decimal: number, over: Partial<Quote> = {}): Quote {
  return {
    book,
    source: 'odds-api',
    sourceEventId: 'g1',
    kind,
    side,
    line,
    decimal,
    suspended: false,
    isMainLine: true,
    observedAt: NOW - 5 * SEC,
    bookUpdatedAt: NOW - 10 * MIN,
    ...over,
  };
}

function ml(book: string, home: number, away: number, over: Partial<Quote> = {}): Quote[] {
  return [quote(book, 'moneyline', 'home', null, home, over), quote(book, 'moneyline', 'away', null, away, over)];
}

function spread(book: string, homeLine: number, home: number, away: number, over: Partial<Quote> = {}): Quote[] {
  return [quote(book, 'spread', 'home', homeLine, home, over), quote(book, 'spread', 'away', -homeLine, away, over)];
}

function ingest(store: MarketStore, events: RawEvent[], quotes: Quote[], fetchedAt = NOW - 5 * SEC, league = 'NBA'): void {
  store.ingest({ source: 'odds-api', league, fetchedAt, events, quotes, complete: false, books: [] });
}

function build(quotes: Quote[], eventOver: Partial<RawEvent> = {}): MarketStore {
  const store = new MarketStore();
  const ev = rawEvent(eventOver);
  ingest(store, [ev], quotes, NOW - 5 * SEC, ev.league);
  return store;
}

/** Pinnacle 1.90 / 2.00 -> fair home 0.512821. DraftKings home at `dkHome` (away 1.80 is always -EV). */
function baseQuotes(dkHome: number, dkOver: Partial<Quote> = {}, pinOver: Partial<Quote> = {}): Quote[] {
  return [...ml('pinnacle', 1.9, 2.0, pinOver), ...ml('draftkings', dkHome, 1.8, dkOver)];
}

const FAIR_HOME = 1 / 1.9 / (1 / 1.9 + 1 / 2.0);

/**
 * Pinnacle home moved 2.05 -> 1.80 at NOW-70s (inside the 120 s window). DraftKings home stays at 2.10 unless
 * `dkMovedAt` is given, in which case DraftKings moved 2.00 -> 2.10 at that time.
 */
function staleScenario(
  store: MarketStore,
  ev: RawEvent,
  opts: { dkMovedAt?: number; sourceEventId?: string } = {},
): void {
  const id = opts.sourceEventId ?? ev.sourceEventId;
  const at = (o: Partial<Quote>): Partial<Quote> => ({ sourceEventId: id, ...o });
  const early = NOW - 300 * SEC;
  ingest(
    store,
    [ev],
    [
      ...ml('pinnacle', 2.05, 1.85, at({ observedAt: early, bookUpdatedAt: early })),
      ...ml('draftkings', opts.dkMovedAt ? 2.0 : 2.1, 1.8, at({ observedAt: early, bookUpdatedAt: NOW - 10 * MIN })),
    ],
    early,
  );
  ingest(store, [ev], [
    ...ml('pinnacle', 1.8, 2.1, at({ bookUpdatedAt: NOW - 70 * SEC })),
    quote('draftkings', 'moneyline', 'home', null, 2.1, at({ bookUpdatedAt: opts.dkMovedAt ?? NOW - 10 * MIN })),
    quote('draftkings', 'moneyline', 'away', null, 1.8, at({ bookUpdatedAt: NOW - 10 * MIN })),
  ]);
}

function only(list: Opportunity[]): Opportunity {
  expect(list).toHaveLength(1);
  return list[0];
}

function worseByOne(american: number): number {
  return american === 100 ? -101 : american - 1;
}

function expectMinAcceptable(o: Opportunity, minEv: number): void {
  const am = o.minAcceptableAmerican;
  expect(Number.isInteger(am)).toBe(true);
  expect(Math.abs(am)).toBeGreaterThanOrEqual(100);
  expect(o.fairProb * americanToDecimal(am) - 1).toBeGreaterThanOrEqual(minEv - 1e-12);
  expect(o.fairProb * americanToDecimal(worseByOne(am)) - 1).toBeLessThan(minEv);
}

describe('describePick', () => {
  it('formats moneylines, draws, spreads and totals', () => {
    expect(describePick('moneyline', 'home', null, 'Celtics', 'Knicks')).toBe('Celtics ML');
    expect(describePick('moneyline', 'away', null, 'Celtics', 'Knicks')).toBe('Knicks ML');
    expect(describePick('moneyline', 'draw', null, 'Arsenal', 'Chelsea')).toBe('Draw');
    expect(describePick('spread', 'home', -3.5, 'Celtics', 'Knicks')).toBe('Celtics -3.5');
    expect(describePick('spread', 'away', 3.5, 'Celtics', 'Knicks')).toBe('Knicks +3.5');
    expect(describePick('spread', 'away', -7, 'Celtics', 'Knicks')).toBe('Knicks -7');
    expect(describePick('spread', 'home', 0, 'Celtics', 'Knicks')).toBe('Celtics PK');
    expect(describePick('total', 'over', 224.5, 'Celtics', 'Knicks')).toBe('Over 224.5');
    expect(describePick('total', 'under', 224.5, 'Celtics', 'Knicks')).toBe('Under 224.5');
  });
});

describe('computeOpportunities — EV picks', () => {
  it('flags a +EV DraftKings price as BET pre-match with a Kelly stake', () => {
    const link = 'https://sportsbook.draftkings.com/event/123';
    const store = build(baseQuotes(2.1, { link }));
    const c = ctx();
    const o = only(computeOpportunities(store, c));

    expect(o.id).toBe('odds-api:g1|ev|moneyline|home|');
    expect(o.type).toBe('ev');
    expect(o.verdict).toBe('BET');
    expect(o.status).toBe('active');
    expect(o.isLive).toBe(false);
    expect(o.pick).toBe('Celtics ML');
    expect(o.eventName).toBe('Knicks @ Celtics');
    expect(o.league).toBe('NBA');
    expect(o.fairProb).toBeCloseTo(FAIR_HOME, 9);
    expect(o.evPct).toBeCloseTo(FAIR_HOME * 2.1 - 1, 9);
    expect(o.dkDecimal).toBe(2.1);
    expect(o.dkAmerican).toBe(110);
    expect(o.fairDecimal).toBeCloseTo(1.95, 9);
    expect(o.fairAmerican).toBe(-105);
    expect(o.sharpSource).toBe('pinnacle');
    expect(o.sharpAgeSec).toBe(5);
    expect(o.dkAgeSec).toBe(5);
    expect(o.staleLine).toBe(false);
    expect(o.expiresInSec).toBe(900);
    expect(o.dkUrl).toBe(link);
    expect(o.firstSeen).toBe(NOW);
    expect(o.lastSeen).toBe(NOW);
    // 0.9 (Pinnacle) × (1 − 0.3 × 5/900)
    expect(o.confidence).toBeCloseTo(0.9 * (1 - (0.3 * 5) / 900), 9);
    // min(25, ev*500) only: pre-match 5 h out, no stale line.
    expect(o.urgencyScore).toBe(25);
    expect(o.urgency).toBe('low');

    const expected = suggestStake({
      prob: o.fairProb,
      decimal: 2.1,
      bankroll: 1000,
      kellyMultiplier: 0.25,
      confidence: o.confidence,
      maxStakePct: 0.02,
      maxStakeAbs: 100,
      remainingDailyExposure: 150,
    });
    expect(o.stake).toBe(expected.stake);
    expect(o.stake).toBe(15);
    expect(o.kellyFraction).toBeCloseTo(0.015, 9);

    expect(o.reasons.length).toBeGreaterThanOrEqual(2);
    expect(o.reasons.length).toBeLessThanOrEqual(5);
    expect(o.reasons.some((r) => r.includes('Pinnacle no-vig'))).toBe(true);
    expect(o.reasons.some((r) => r.includes('+110') && r.includes('+7.7%'))).toBe(true);
    expect(o.reasons.some((r) => r.startsWith('Take it at -101 or better'))).toBe(true);
    expectMinAcceptable(o, c.settings.minEvPrematch);
  });

  it('flags the same price as BET_NOW when the event is live', () => {
    const store = build(baseQuotes(2.1), LIVE);
    const o = only(computeOpportunities(store, ctx()));
    expect(o.verdict).toBe('BET_NOW');
    expect(o.isLive).toBe(true);
    expect(o.expiresInSec).toBe(45);
    expect(o.confidence).toBeCloseTo(0.9 * 0.85 * (1 - (0.3 * 5) / 45), 9);
    expect(o.urgencyScore).toBe(70);
    expect(o.urgency).toBe('high');
    expect(o.stake).toBeGreaterThan(0);
    expect(o.reasons.some((r) => r.includes('act within ~45s'))).toBe(true);
    expectMinAcceptable(o, 0.03);
  });

  it('carries the live score onto the opportunity', () => {
    const score = { home: 50, away: 48, period: 'Q3', clock: '04:12', updatedAt: NOW - 10 * SEC };
    const store = build(baseQuotes(2.1), { ...LIVE, score });
    const o = only(computeOpportunities(store, ctx()));
    expect(o.score).toEqual(score);
  });

  it('ignores prices below watchEv', () => {
    // 0.512821 × 1.95 − 1 ≈ 0
    expect(computeOpportunities(build(baseQuotes(1.95)), ctx())).toEqual([]);
  });

  it('shows prices between watchEv and minEv as WATCH with no stake', () => {
    const c = ctx();
    const o = only(computeOpportunities(build(baseQuotes(1.98)), c));
    expect(o.evPct).toBeCloseTo(FAIR_HOME * 1.98 - 1, 9);
    expect(o.evPct).toBeGreaterThan(0.005);
    expect(o.evPct).toBeLessThan(0.02);
    expect(o.verdict).toBe('WATCH');
    expect(o.stake).toBe(0);
    expect(o.kellyFraction).toBe(0);
    expect(o.urgencyScore).toBeLessThanOrEqual(40);
    expect(o.reasons.some((r) => r.startsWith('Needs -101 or better'))).toBe(true);
    expectMinAcceptable(o, c.settings.minEvPrematch);
  });

  it('caps WATCH urgency at 40 even when live', () => {
    const o = only(computeOpportunities(build(baseQuotes(1.98), LIVE), ctx()));
    expect(o.verdict).toBe('WATCH');
    expect(o.urgencyScore).toBe(40);
    expect(o.urgency).toBe('medium');
  });

  it('ignores implausibly large edges (bad data)', () => {
    const store = build(baseQuotes(3.0));
    expect(computeOpportunities(store, ctx())).toEqual([]);
    expect(computeOpportunities(store, ctx({ model: { maxPlausibleEv: 0.6 } }))).toHaveLength(1);
  });

  it('ignores DraftKings prices longer than maxDecimalOdds', () => {
    const store = build(baseQuotes(2.1));
    expect(computeOpportunities(store, ctx({ model: { maxDecimalOdds: 2.05 } }))).toEqual([]);
  });

  it('ignores stale sharp data (pre-match and live limits)', () => {
    const pre = build(baseQuotes(2.1, {}, { observedAt: NOW - 901 * SEC }));
    expect(computeOpportunities(pre, ctx())).toEqual([]);
    const live = build(baseQuotes(2.1, {}, { observedAt: NOW - 46 * SEC }), LIVE);
    expect(computeOpportunities(live, ctx())).toEqual([]);
  });

  it('ignores stale DraftKings quotes (pre-match and live limits)', () => {
    const pre = build(baseQuotes(2.1, { observedAt: NOW - 601 * SEC }));
    expect(computeOpportunities(pre, ctx())).toEqual([]);
    const live = build(baseQuotes(2.1, { observedAt: NOW - 46 * SEC }), LIVE);
    expect(computeOpportunities(live, ctx())).toEqual([]);
  });

  it('treats prices observed "in the future" (the clock stepped back) as stale, not 0 s old', () => {
    const future = { observedAt: NOW + 30 * MIN };
    expect(computeOpportunities(build(baseQuotes(2.1, future, future)), ctx())).toEqual([]);
    expect(computeOpportunities(build(baseQuotes(2.1, future, future), LIVE), ctx())).toEqual([]);
    expect(computeOpportunities(build(baseQuotes(2.1, {}, future)), ctx())).toEqual([]); // sharp side only
    // A couple of seconds of clock jitter is fine.
    const jitter = { observedAt: NOW + 2 * SEC };
    expect(only(computeOpportunities(build(baseQuotes(2.1, jitter, jitter)), ctx())).verdict).toBe('BET');
  });

  it('ignores suspended DraftKings quotes', () => {
    const store = build([...ml('pinnacle', 1.9, 2.0), quote('draftkings', 'moneyline', 'home', null, 2.1, { suspended: true })]);
    expect(computeOpportunities(store, ctx())).toEqual([]);
  });

  it('excludes alternate lines unless includeAltLines is on', () => {
    const store = build([
      ...spread('pinnacle', -3.5, 1.95, 1.95),
      quote('draftkings', 'spread', 'home', -3.5, 2.08, { isMainLine: false }),
    ]);
    expect(computeOpportunities(store, ctx())).toEqual([]);
    const o = only(computeOpportunities(store, ctx({ model: { includeAltLines: true } })));
    expect(o.pick).toBe('Celtics -3.5');
    expect(o.id).toBe('odds-api:g1|ev|spread|home|-3.5');
    expect(o.evPct).toBeCloseTo(0.04, 9);
  });

  it('handles three-way soccer moneylines with a draw', () => {
    const quotes = [
      quote('pinnacle', 'moneyline', 'home', null, 2.0),
      quote('pinnacle', 'moneyline', 'draw', null, 3.5),
      quote('pinnacle', 'moneyline', 'away', null, 4.0),
      quote('draftkings', 'moneyline', 'home', null, 1.9),
      quote('draftkings', 'moneyline', 'draw', null, 3.9),
      quote('draftkings', 'moneyline', 'away', null, 3.6),
    ];
    const store = build(quotes, { league: 'EPL', home: 'Arsenal', away: 'Chelsea' });
    const o = only(computeOpportunities(store, ctx()));
    const booksum = 1 / 2 + 1 / 3.5 + 1 / 4;
    expect(o.pick).toBe('Draw');
    expect(o.fairProb).toBeCloseTo(1 / 3.5 / booksum, 9);
    expect(o.verdict).toBe('BET');
  });

  it('skips disabled leagues and events that started more than 6 h ago', () => {
    const store = build(baseQuotes(2.1));
    expect(computeOpportunities(store, ctx({ settings: { enabledLeagues: ['EPL'] } }))).toEqual([]);
    const old = build(baseQuotes(2.1), { isLive: true, startTime: NOW - 7 * HOUR });
    expect(computeOpportunities(old, ctx())).toEqual([]);
  });

  it('hides pre-game prices once the game has started but the feed has not reported it live yet', () => {
    // Polled 5 s before the start: the store still has the event as pre-game.
    const started = build(baseQuotes(2.1), { isLive: false, startTime: NOW - 2 * SEC });
    expect(computeOpportunities(started, ctx({ settings: { showArbs: true } }))).toEqual([]);
    // One second before the start the same prices are still a pre-game pick.
    const soon = build(baseQuotes(2.1), { isLive: false, startTime: NOW + SEC });
    expect(only(computeOpportunities(soon, ctx())).verdict).toBe('BET');
  });

  it('computes the worst acceptable price for positive American odds too', () => {
    // Pinnacle 2.50 / 1.60 -> fair home 0.390244; DK +170.
    const store = build([...ml('pinnacle', 2.5, 1.6), ...ml('draftkings', 2.7, 1.5)]);
    const c = ctx();
    const o = only(computeOpportunities(store, c));
    expect(o.minAcceptableAmerican).toBe(162);
    expectMinAcceptable(o, c.settings.minEvPrematch);
    expect(o.dkAmerican).toBeGreaterThanOrEqual(o.minAcceptableAmerican);
  });

  it('mentions the daily exposure cap when it limits the stake', () => {
    const capped = only(computeOpportunities(build(baseQuotes(2.1)), ctx({ remainingDailyExposure: 5 })));
    expect(capped.stake).toBe(5);
    expect(capped.reasons.some((r) => r.includes("today's remaining exposure"))).toBe(true);

    const used = only(computeOpportunities(build(baseQuotes(2.1)), ctx({ remainingDailyExposure: 0 })));
    expect(used.verdict).toBe('BET');
    expect(used.stake).toBe(0);
    expect(used.reasons.some((r) => r.includes('exposure limit is used up'))).toBe(true);
  });
});

describe('computeOpportunities — desync guard', () => {
  it('forces WATCH live when DraftKings repriced after the sharp line', () => {
    const store = build(baseQuotes(2.1, { bookUpdatedAt: NOW - 5 * SEC }), LIVE);
    const o = only(computeOpportunities(store, ctx()));
    expect(o.verdict).toBe('WATCH');
    expect(o.stake).toBe(0);
    expect(o.reasons).toContain(DESYNC_TEXT);
  });

  it('only lowers confidence pre-match', () => {
    const normal = only(computeOpportunities(build(baseQuotes(2.1)), ctx()));
    const desync = only(computeOpportunities(build(baseQuotes(2.1, { bookUpdatedAt: NOW - 5 * SEC })), ctx()));
    expect(desync.verdict).toBe('BET');
    expect(desync.confidence).toBeCloseTo(normal.confidence * 0.85, 9);
  });

  it('does not trigger when DraftKings moved within 10 s of the reference', () => {
    const store = build(baseQuotes(2.1, { bookUpdatedAt: NOW - 10 * MIN + 9 * SEC }), LIVE);
    expect(only(computeOpportunities(store, ctx())).verdict).toBe('BET_NOW');
  });
});

describe('computeOpportunities — stale lines', () => {
  it('detects a Pinnacle move DraftKings has not followed (pre-match -> BET_NOW)', () => {
    const store = new MarketStore();
    staleScenario(store, rawEvent());
    const c = ctx();
    const o = only(computeOpportunities(store, c));
    const fair = 1 / 1.8 / (1 / 1.8 + 1 / 2.1);
    expect(o.fairProb).toBeCloseTo(fair, 9);
    expect(o.staleLine).toBe(true);
    expect(o.verdict).toBe('BET_NOW');
    expect(o.expiresInSec).toBe(120);
    expect(o.urgencyScore).toBe(55);
    expect(o.urgency).toBe('high');
    expect(o.reasons).toContain('Pinnacle moved +105 → -125 in the last 70s; DraftKings still +110');
    expectMinAcceptable(o, c.settings.minEvPrematch);
  });

  it('makes a live stale line critical with a short expiry', () => {
    const store = new MarketStore();
    staleScenario(store, rawEvent(LIVE));
    const o = only(computeOpportunities(store, ctx()));
    expect(o.staleLine).toBe(true);
    expect(o.verdict).toBe('BET_NOW');
    expect(o.expiresInSec).toBe(20);
    expect(o.urgencyScore).toBe(100);
    expect(o.urgency).toBe('critical');
  });

  it('is not flagged when DraftKings already moved after Pinnacle', () => {
    const store = new MarketStore();
    staleScenario(store, rawEvent(), { dkMovedAt: NOW - 65 * SEC });
    const o = only(computeOpportunities(store, ctx()));
    expect(o.staleLine).toBe(false);
    expect(o.verdict).toBe('BET');
    expect(o.reasons.some((r) => r.includes('moved'))).toBe(false);
  });

  it('is not flagged when the sharp move is too small', () => {
    const store = new MarketStore();
    staleScenario(store, rawEvent());
    const o = only(computeOpportunities(store, ctx({ model: { staleMoveProb: 0.1 } })));
    expect(o.staleLine).toBe(false);
  });
});

describe('computeOpportunities — stale lines measured on no-vig prices from the start of the move', () => {
  it('does not count a wider sharp margin (or a move against the pick) as steam', () => {
    // Live. Pinnacle 1.40 / 3.25 (2.2% margin) -> 1.36 / 3.10 (5.8% margin) 20 s ago. The favourite's raw implied
    // probability rises by 0.021, but its no-vig probability falls (0.6989 -> 0.6951). DraftKings is still 1.49.
    const store = new MarketStore();
    const ev = rawEvent(LIVE);
    const early = NOW - 300 * SEC;
    ingest(store, [ev], [
      ...ml('pinnacle', 1.4, 3.25, { observedAt: early, bookUpdatedAt: early }),
      ...ml('draftkings', 1.49, 2.6, { observedAt: early }),
    ], early);
    ingest(store, [ev], [...ml('pinnacle', 1.36, 3.1, { bookUpdatedAt: NOW - 20 * SEC }), ...ml('draftkings', 1.49, 2.6)]);
    const o = only(computeOpportunities(store, ctx()));
    expect(o.side).toBe('home');
    expect(o.fairProb).toBeLessThan(1 / 1.4 / (1 / 1.4 + 1 / 3.25));
    expect(o.staleLine).toBe(false);
    expect(o.verdict).toBe('BET_NOW'); // live and +EV, but not a stale line
    expect(o.urgency).not.toBe('critical');
    expect(o.expiresInSec).toBe(45);
    expect(o.reasons.some((r) => r.includes('moved'))).toBe(false);
  });

  it('is not flagged when DraftKings already followed the move, even if the sharp book ticked again since', () => {
    // Pre-match. Pinnacle home 2.00 -> 1.80 at NOW-110s; DraftKings follows 2.15 -> 1.95 at NOW-60s; Pinnacle then
    // ticks 1.80 -> 1.79 at NOW-15s. DraftKings has repriced since the move began: not a stale line.
    const store = new MarketStore();
    const ev = rawEvent();
    const early = NOW - 300 * SEC;
    ingest(store, [ev], [
      ...ml('pinnacle', 2.0, 1.85, { observedAt: early, bookUpdatedAt: early }),
      ...ml('draftkings', 2.15, 1.75, { observedAt: early }),
    ], early);
    ingest(store, [ev], [
      ...ml('pinnacle', 1.8, 2.1, { observedAt: NOW - 100 * SEC, bookUpdatedAt: NOW - 110 * SEC }),
      ...ml('draftkings', 2.15, 1.75, { observedAt: NOW - 100 * SEC }),
    ], NOW - 100 * SEC);
    ingest(store, [ev], [
      ...ml('pinnacle', 1.8, 2.1, { observedAt: NOW - 55 * SEC, bookUpdatedAt: NOW - 55 * SEC }),
      ...ml('draftkings', 1.95, 1.85, { observedAt: NOW - 55 * SEC, bookUpdatedAt: NOW - 60 * SEC }),
    ], NOW - 55 * SEC);
    ingest(store, [ev], [...ml('pinnacle', 1.79, 2.12, { bookUpdatedAt: NOW - 15 * SEC }), ...ml('draftkings', 1.95, 1.85, { bookUpdatedAt: NOW - 60 * SEC })]);
    const o = only(computeOpportunities(store, ctx()));
    expect(o.side).toBe('home');
    expect(o.staleLine).toBe(false);
    expect(o.verdict).toBe('BET');
    expect(o.expiresInSec).toBe(900);
  });

  it('dates a multi-step move from when it began', () => {
    // Same sharp moves, but DraftKings never changed: stale, and the move is reported as 110 s old, not 15 s.
    const store = new MarketStore();
    const ev = rawEvent();
    const early = NOW - 300 * SEC;
    ingest(store, [ev], [
      ...ml('pinnacle', 2.0, 1.85, { observedAt: early, bookUpdatedAt: early }),
      ...ml('draftkings', 2.15, 1.75, { observedAt: early }),
    ], early);
    ingest(store, [ev], [
      ...ml('pinnacle', 1.8, 2.1, { observedAt: NOW - 100 * SEC, bookUpdatedAt: NOW - 110 * SEC }),
      ...ml('draftkings', 2.15, 1.75, { observedAt: NOW - 100 * SEC }),
    ], NOW - 100 * SEC);
    ingest(store, [ev], [...ml('pinnacle', 1.79, 2.12, { bookUpdatedAt: NOW - 15 * SEC }), ...ml('draftkings', 2.15, 1.75)]);
    const o = only(computeOpportunities(store, ctx()));
    expect(o.staleLine).toBe(true);
    expect(o.verdict).toBe('BET_NOW');
    expect(o.reasons).toContain('Pinnacle moved +100 → -127 in the last 110s; DraftKings still +115');
  });
});

describe('computeOpportunities — longshot confidence', () => {
  it('never turns a better DraftKings price into WATCH (confidence does not depend on the price judged)', () => {
    // Live, Pinnacle 1.33 / 3.50 with 40 s old data: fair away ≈ +263 (27.5%).
    const at = (dkAway: number): Opportunity => {
      const store = build(
        [...ml('pinnacle', 1.33, 3.5, { observedAt: NOW - 40 * SEC }), ...ml('draftkings', 1.25, dkAway)],
        LIVE,
      );
      return only(computeOpportunities(store, ctx()));
    };
    const plus290 = at(3.9);
    const plus310 = at(4.1);
    expect(plus290.verdict).toBe('BET_NOW');
    expect(plus310.verdict).toBe('BET_NOW');
    expect(plus310.confidence).toBeCloseTo(plus290.confidence, 12);
    expect(plus310.evPct).toBeGreaterThan(plus290.evPct);
    expect(plus310.stake).toBeGreaterThanOrEqual(plus290.stake);
    expect(plus310.minAcceptableAmerican).toBe(plus290.minAcceptableAmerican);
  });

  it('still discounts picks whose fair price is a longshot', () => {
    // Pinnacle 1.20 / 5.50: fair away ≈ 5.58 (17.9%). Pre-match, fresh data.
    const store = build([...ml('pinnacle', 1.2, 5.5), ...ml('draftkings', 1.15, 6.2)]);
    const o = only(computeOpportunities(store, ctx()));
    expect(o.side).toBe('away');
    expect(o.confidence).toBeCloseTo(0.9 * (1 - (0.3 * 5) / 900) * 0.85, 9);
  });
});

describe('computeOpportunities — reference fallback, ordering, correlation', () => {
  it('falls back to a consensus with lower confidence', () => {
    const pinnacle = only(computeOpportunities(build(baseQuotes(2.1)), ctx()));
    const store = build([
      ...ml('fanduel', 1.9, 2.0),
      ...ml('betmgm', 1.9, 2.0),
      ...ml('williamhill_us', 1.9, 2.0),
      ...ml('draftkings', 2.1, 1.8),
    ]);
    const o = only(computeOpportunities(store, ctx()));
    expect(o.sharpSource).toBe('consensus(3)');
    expect(o.fairProb).toBeCloseTo(FAIR_HOME, 9);
    expect(o.confidence).toBeCloseTo(0.7 * (1 - (0.3 * 5) / 900), 9);
    expect(o.confidence).toBeLessThan(pinnacle.confidence);
    expect(o.stake).toBeLessThan(pinnacle.stake);
    expect(o.reasons.some((r) => r.includes('consensus of 3 books'))).toBe(true);

    expect(computeOpportunities(store, ctx({ model: { minConsensusBooks: 4 } }))).toEqual([]);
  });

  it('ranks a live stale line (critical) above a pre-match pick', () => {
    const store = new MarketStore();
    const pre = rawEvent({ sourceEventId: 'g2', home: 'Lakers', away: 'Suns' });
    ingest(store, [pre], baseQuotes(2.1).map((q) => ({ ...q, sourceEventId: 'g2' })));
    staleScenario(store, rawEvent(LIVE));
    const list = computeOpportunities(store, ctx());
    expect(list).toHaveLength(2);
    expect(list[0].eventId).toBe('odds-api:g1');
    expect(list[0].urgency).toBe('critical');
    expect(list[0].verdict).toBe('BET_NOW');
    expect(list[1].eventId).toBe('odds-api:g2');
    expect(list[1].verdict).toBe('BET');
    expect(list[0].urgencyScore).toBeGreaterThan(list[1].urgencyScore);
  });

  it('adds a correlation note to the weaker pick on the same event', () => {
    const store = build([
      ...ml('pinnacle', 1.9, 2.0),
      ...spread('pinnacle', -3.5, 1.95, 1.95),
      ...ml('draftkings', 2.1, 1.8),
      ...spread('draftkings', -3.5, 2.08, 1.8),
    ]);
    const list = computeOpportunities(store, ctx());
    expect(list).toHaveLength(2);
    const mlPick = list.find((o) => o.kind === 'moneyline');
    const spreadPick = list.find((o) => o.kind === 'spread');
    expect(mlPick?.verdict).toBe('BET');
    expect(spreadPick?.verdict).toBe('BET');
    expect(spreadPick?.reasons).toContain('Correlated with Celtics ML — pick one');
    expect(mlPick?.reasons.some((r) => r.startsWith('Correlated'))).toBe(false);
    for (const o of list) expect(o.reasons.length).toBeLessThanOrEqual(5);
  });
});

describe('computeOpportunities — arbs', () => {
  const arbQuotes = (): Quote[] => [...ml('fanduel', 1.75, 2.0), ...ml('draftkings', 2.2, 1.7, { link: 'https://dk.example/x' })];

  it('detects a DraftKings vs other-book arb with correct leg stakes and profit', () => {
    const store = build(arbQuotes());
    const o = only(computeOpportunities(store, ctx({ settings: { showArbs: true } })));
    const S = 1 / 2.2 + 1 / 2.0;
    expect(o.type).toBe('arb');
    expect(o.id).toBe('odds-api:g1|arb|moneyline|home|');
    expect(o.evPct).toBeCloseTo(1 / S - 1, 9);
    // No reference market (one other book, no sharp): the DraftKings leg alone claims no edge.
    expect(o.fairProb).toBeCloseTo(1 / 2.2, 9);
    expect(o.verdict).toBe('BET');
    expect(o.urgencyScore).toBe(70);
    expect(o.urgency).toBe('high');
    expect(o.pick).toBe('Celtics ML');
    expect(o.dkUrl).toBe('https://dk.example/x');
    expect(o.arb).toBeDefined();
    const arb = o.arb!;
    expect(arb.totalStake).toBe(40); // min(2×1000×0.02, 2×100, 150)
    expect(arb.profitPct).toBeCloseTo(1 / S - 1, 9);
    expect(arb.legs).toHaveLength(2);
    // Split to the cent so both outcomes return the same amount: 19.05 × 2.2 ≈ 20.95 × 2.0 ≈ 41.91 > 40.
    expect(arb.legs[0]).toMatchObject({ book: 'draftkings', side: 'home', decimal: 2.2, american: 120, stake: 19.05 });
    expect(arb.legs[1]).toMatchObject({ book: 'fanduel', side: 'away', decimal: 2.0, american: 100, stake: 20.95 });
    for (const leg of arb.legs) expect(leg.stake * leg.decimal).toBeGreaterThan(arb.totalStake);
    expect(o.stake).toBe(19.05);
    expect(o.reasons.some((r) => r.includes('Bet $19.05 on Celtics ML at DraftKings and $20.95 on Knicks ML at FanDuel'))).toBe(true);
    expect(o.reasons.some((r) => r.includes('another sportsbook'))).toBe(true);
    expect(o.reasons.some((r) => r.includes('FanDuel'))).toBe(true);
  });

  it('reports the reference no-vig probability as fairProb, not the arb leg weight', () => {
    // Pinnacle 2.00 / 1.87 is the reference; DraftKings home 2.10 + FanDuel away 2.05 lock ≈ +3.7%.
    const store = build([...ml('pinnacle', 2.0, 1.87), ...ml('fanduel', 1.8, 2.05), ...ml('draftkings', 2.1, 1.7)]);
    const arb = computeOpportunities(store, ctx({ settings: { showArbs: true } })).find((o) => o.type === 'arb');
    expect(arb).toBeDefined();
    const S = 1 / 2.1 + 1 / 2.05;
    const pinHome = 1 / 2.0 / (1 / 2.0 + 1 / 1.87);
    expect(arb!.evPct).toBeCloseTo(1 / S - 1, 9);
    expect(arb!.fairProb).toBeCloseTo(pinHome, 9);
    expect(arb!.fairProb).not.toBeCloseTo(1 / 2.1 / S, 3);
    // What "I placed it" would journal as the EV of the DraftKings leg: ≈ +1.3%, not the +3.7% locked by both legs.
    expect(arb!.fairProb * 2.1 - 1).toBeCloseTo(pinHome * 2.1 - 1, 9);
    expect(arb!.fairProb * 2.1 - 1).toBeLessThan(0.02);
  });

  it('marks a live arb BET_NOW with a higher urgency', () => {
    const o = only(computeOpportunities(build(arbQuotes(), LIVE), ctx({ settings: { showArbs: true } })));
    expect(o.verdict).toBe('BET_NOW');
    expect(o.urgencyScore).toBe(100);
    expect(o.urgency).toBe('critical');
  });

  it('reports nothing when showArbs is off or the arb is too thin', () => {
    expect(computeOpportunities(build(arbQuotes()), ctx())).toEqual([]);
    // 1/2.0 + 1/2.005 ≈ 0.99875 -> profit ≈ 0.125% < 0.5%
    const thin = build([...ml('fanduel', 1.75, 2.005), ...ml('draftkings', 2.0, 1.7)]);
    expect(computeOpportunities(thin, ctx({ settings: { showArbs: true } }))).toEqual([]);
  });

  it('never uses a reference (sharp) book as the other leg', () => {
    // Pinnacle's away 2.10 would complete a 2.2 / 2.10 arb, but the reference books are offshore and only used for
    // pricing; FanDuel's 1.75 does not complete one.
    const store = build([...ml('pinnacle', 1.8, 2.1), ...ml('fanduel', 1.8, 1.75), ...ml('draftkings', 2.2, 1.7)]);
    const list = computeOpportunities(store, ctx({ settings: { showArbs: true } }));
    expect(list.filter((o) => o.type === 'arb')).toEqual([]);
    expect(list.some((o) => o.type === 'ev' && o.side === 'home')).toBe(true);
  });

  it('ignores suspended or stale other legs', () => {
    const suspended = build([
      ...ml('fanduel', 1.75, 2.0, { suspended: true }),
      ...ml('draftkings', 2.2, 1.7),
    ]);
    expect(computeOpportunities(suspended, ctx({ settings: { showArbs: true } }))).toEqual([]);
    const stale = build([...ml('fanduel', 1.75, 2.0, { observedAt: NOW - 700 * SEC }), ...ml('draftkings', 2.2, 1.7)]);
    expect(computeOpportunities(stale, ctx({ settings: { showArbs: true } }))).toEqual([]);
  });
});

// ---------------------------------------------------------------------------------------------------------------

function opp(id: string, over: Partial<Opportunity> = {}): Opportunity {
  return {
    id,
    type: 'ev',
    eventId: 'odds-api:g1',
    league: 'NBA',
    eventName: 'Knicks @ Celtics',
    home: 'Celtics',
    away: 'Knicks',
    startTime: NOW + HOUR,
    isLive: false,
    kind: 'moneyline',
    side: 'home',
    line: null,
    pick: 'Celtics ML',
    dkDecimal: 2.1,
    dkAmerican: 110,
    fairProb: 0.5,
    fairDecimal: 2,
    fairAmerican: 100,
    evPct: 0.05,
    minAcceptableAmerican: 104,
    kellyFraction: 0.01,
    stake: 10,
    confidence: 0.9,
    urgency: 'medium',
    urgencyScore: 40,
    verdict: 'BET',
    reasons: ['r1', 'r2'],
    sharpSource: 'pinnacle',
    sharpAgeSec: 5,
    dkAgeSec: 5,
    staleLine: false,
    firstSeen: 0,
    lastSeen: 0,
    status: 'active',
    expiresInSec: 300,
    dkUrl: null,
    ...over,
  };
}

function withUrgency(id: string, urgency: Urgency, verdict: Verdict = 'BET'): Opportunity {
  const score: Record<Urgency, number> = { critical: 90, high: 60, medium: 40, low: 10 };
  return opp(id, { urgency, urgencyScore: score[urgency], verdict });
}

describe('sortOpportunities', () => {
  it('orders active before gone, then verdict, urgency, EV and id', () => {
    const list = [
      opp('g', { status: 'gone', verdict: 'BET_NOW', urgencyScore: 99 }),
      opp('w', { verdict: 'WATCH', urgencyScore: 40 }),
      opp('b2', { verdict: 'BET', urgencyScore: 30, evPct: 0.02 }),
      opp('b1', { verdict: 'BET', urgencyScore: 30, evPct: 0.04 }),
      opp('b3', { verdict: 'BET', urgencyScore: 50 }),
      opp('n', { verdict: 'BET_NOW', urgencyScore: 10 }),
      opp('a', { verdict: 'BET', urgencyScore: 30, evPct: 0.04 }),
    ];
    const sorted = sortOpportunities(list);
    expect(sorted.map((o) => o.id)).toEqual(['n', 'b3', 'a', 'b1', 'b2', 'w', 'g']);
    expect(list[0].id).toBe('g'); // input not mutated
  });
});

describe('OpportunityTracker', () => {
  it('keeps firstSeen for existing ids and refreshes lastSeen and data', () => {
    const t = new OpportunityTracker({ goneRetentionSec: 90 });
    const first = t.update([opp('a')], NOW);
    expect(first[0]).toMatchObject({ id: 'a', firstSeen: NOW, lastSeen: NOW, status: 'active' });
    const second = t.update([opp('a', { evPct: 0.07, firstSeen: NOW + SEC, lastSeen: NOW + SEC })], NOW + SEC);
    expect(second[0]).toMatchObject({ firstSeen: NOW, lastSeen: NOW + SEC, evPct: 0.07 });
    expect(t.find('a')?.firstSeen).toBe(NOW);
    expect(t.find('missing')).toBeUndefined();
  });

  it('marks vanished items gone, keeps them for goneRetentionSec, then drops them', () => {
    const t = new OpportunityTracker({ goneRetentionSec: 90 });
    t.update([opp('a')], NOW);
    t.update([opp('a')], NOW + SEC);
    const afterGone = t.update([opp('b')], NOW + 2 * SEC);
    expect(afterGone.map((o) => o.id)).toEqual(['b', 'a']);
    const gone = t.find('a');
    expect(gone?.status).toBe('gone');
    expect(gone?.lastSeen).toBe(NOW + SEC);

    t.update([opp('b')], NOW + SEC + 90 * SEC);
    expect(t.find('a')?.status).toBe('gone');
    t.update([opp('b')], NOW + SEC + 91 * SEC);
    expect(t.find('a')).toBeUndefined();
    expect(t.current().map((o) => o.id)).toEqual(['b']);
  });

  it('reactivates a gone id that comes back, keeping its firstSeen', () => {
    const t = new OpportunityTracker({ goneRetentionSec: 90 });
    t.update([opp('a')], NOW);
    t.update([], NOW + SEC);
    t.update([opp('a')], NOW + 2 * SEC);
    expect(t.find('a')).toMatchObject({ status: 'active', firstSeen: NOW, lastSeen: NOW + 2 * SEC });
  });

  it('starts the price window (actionableSince) when a pick becomes actionable, not when it was first seen', () => {
    const t = new OpportunityTracker({ goneRetentionSec: 90 });
    t.update([opp('x', { verdict: 'WATCH' })], 1_000);
    t.update([opp('x', { verdict: 'WATCH' })], 58_000);
    expect(t.find('x')?.actionableSince).toBeUndefined();
    const now = t.update([opp('x', { verdict: 'BET_NOW', isLive: true, expiresInSec: 20 })], 60_000)[0];
    expect(now.firstSeen).toBe(1_000);
    expect(now.actionableSince).toBe(60_000); // 20 s to act from here, not already expired
    expect(t.update([opp('x', { verdict: 'BET_NOW', isLive: true })], 65_000)[0].actionableSince).toBe(60_000);
    // A new stale-line signal or a verdict change restarts it; so does coming back after being gone.
    expect(t.update([opp('x', { verdict: 'BET_NOW', isLive: true, staleLine: true })], 66_000)[0].actionableSince).toBe(66_000);
    expect(t.update([opp('x', { verdict: 'BET_NOW', isLive: true, staleLine: false })], 67_000)[0].actionableSince).toBe(66_000);
    expect(t.update([opp('x', { verdict: 'BET' })], 68_000)[0].actionableSince).toBe(68_000);
    t.update([], 69_000);
    expect(t.find('x')?.status).toBe('gone');
    expect(t.update([opp('x', { verdict: 'BET' })], 70_000)[0].actionableSince).toBe(70_000);
    expect(t.update([opp('x', { verdict: 'WATCH' })], 71_000)[0].actionableSince).toBeUndefined();
  });

  it('caps the number of items after sorting', () => {
    const t = new OpportunityTracker({ goneRetentionSec: 90, maxItems: 3 });
    const list = t.update(
      [
        opp('low', { urgencyScore: 10 }),
        opp('top', { urgencyScore: 90 }),
        opp('mid', { urgencyScore: 50 }),
        opp('watch', { verdict: 'WATCH', urgencyScore: 40 }),
        opp('now', { verdict: 'BET_NOW', urgencyScore: 20 }),
      ],
      NOW,
    );
    expect(list.map((o) => o.id)).toEqual(['now', 'top', 'mid']);
    expect(t.current()).toHaveLength(3);
  });

  it('reports new and escalated alertable items exactly once', () => {
    const t = new OpportunityTracker({ goneRetentionSec: 90 });
    t.update([withUrgency('a', 'critical', 'BET_NOW'), withUrgency('w', 'medium', 'WATCH')], NOW);
    expect(t.newlyAlertable('high').map((o) => o.id)).toEqual(['a']);
    // Same delta until the next update.
    expect(t.newlyAlertable('high').map((o) => o.id)).toEqual(['a']);
    expect(t.newlyAlertable('medium').map((o) => o.id)).toEqual(['a']);

    t.update([withUrgency('a', 'critical', 'BET_NOW'), withUrgency('b', 'high')], NOW + SEC);
    expect(t.newlyAlertable('high').map((o) => o.id)).toEqual(['b']);
    expect(t.newlyAlertable('critical')).toEqual([]);

    t.update([withUrgency('a', 'critical', 'BET_NOW'), withUrgency('b', 'high'), withUrgency('c', 'medium')], NOW + 2 * SEC);
    expect(t.newlyAlertable('high')).toEqual([]);

    t.update([withUrgency('a', 'critical', 'BET_NOW'), withUrgency('b', 'high'), withUrgency('c', 'high')], NOW + 3 * SEC);
    expect(t.newlyAlertable('high').map((o) => o.id)).toEqual(['c']);

    t.update([withUrgency('a', 'critical', 'BET_NOW'), withUrgency('b', 'high'), withUrgency('c', 'high')], NOW + 4 * SEC);
    expect(t.newlyAlertable('high')).toEqual([]);

    // A brief flicker (gone for one tick, back at the same urgency) is not re-alerted.
    t.update([withUrgency('b', 'high'), withUrgency('c', 'high')], NOW + 5 * SEC);
    expect(t.newlyAlertable('high')).toEqual([]);
    t.update([withUrgency('a', 'critical', 'BET_NOW'), withUrgency('b', 'high'), withUrgency('c', 'high')], NOW + 6 * SEC);
    expect(t.newlyAlertable('high')).toEqual([]);
  });

  it('never alerts WATCH items and alerts when a WATCH item becomes actionable', () => {
    const t = new OpportunityTracker({ goneRetentionSec: 90 });
    t.update([withUrgency('w', 'medium', 'WATCH')], NOW);
    expect(t.newlyAlertable('low')).toEqual([]);
    t.update([withUrgency('w', 'medium', 'BET')], NOW + SEC);
    expect(t.newlyAlertable('medium').map((o) => o.id)).toEqual(['w']);
    t.update([withUrgency('w', 'medium', 'BET')], NOW + 2 * SEC);
    expect(t.newlyAlertable('medium')).toEqual([]);
  });
});

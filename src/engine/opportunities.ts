/**
 * Turns the market board into ranked, plain-English betting decisions.
 *
 * For every DraftKings price of an enabled league we compare against a no-vig fair probability (first usable sharp
 * book, else a consensus), then decide:
 *   - EV = p·d − 1 at the DraftKings price,
 *   - confidence (reference quality, freshness, live noise, longshot noise, feed desync),
 *   - verdict (BET_NOW / BET / WATCH), urgency and a rough expiry,
 *   - suggested stake (fractional Kelly with caps) and the worst price still worth taking,
 *   - stale-line flag when the sharp book moved recently and DraftKings has not followed.
 * Optionally it also reports two/three-way arbitrage between a DraftKings price and the best prices at other
 * (non-reference) books.
 *
 * `computeOpportunities` is pure with respect to `ctx.now` (no clock reads) and runs on every engine tick, so it
 * avoids work on markets that DraftKings does not quote. `OpportunityTracker` keeps ids stable across ticks,
 * remembers when each one first appeared, shows vanished ones as "gone" for a while, and reports which items
 * became worth alerting on.
 */
import type { AppConfig } from '../config';
import type {
  ArbLeg,
  CanonicalEvent,
  LeagueDef,
  MarketKind,
  Opportunity,
  RuntimeSettings,
  Side,
  Urgency,
  Verdict,
} from '../types';
import { createLogger } from '../util/logger';
import { decimalToAmerican, formatAmerican } from '../util/odds';
import { fairForGroup, groupQuotes, requiredSides } from './fairPrice';
import type { FairOptions, FairResult, MarketGroup } from './fairPrice';
import { suggestStake } from './kelly';
import type { StakeResult } from './kelly';
import type { MarketStore, StoredQuote } from './marketStore';

export interface EngineContext {
  settings: RuntimeSettings;
  model: AppConfig['model'];
  sharpBooks: string[];
  leagues: LeagueDef[];
  now: number;
  remainingDailyExposure: number;
}

const log = createLogger('opportunities');

const DK = 'draftkings';
/** Events that started longer ago than this are ignored (finished or bad data). */
const MAX_EVENT_AGE_MS = 6 * 60 * 60_000;
/** DraftKings repricing this long after the reference last moved means the reference may be lagging. */
const DESYNC_MS = 10_000;
/** DraftKings must have last changed at least this long before the sharp move to count as stale. */
const STALE_LAG_MS = 5_000;
const MIN_BET_CONFIDENCE = 0.5;
const MIN_ARB_PROFIT = 0.005;
const ARB_BASE_CONFIDENCE = 0.95;
const WATCH_URGENCY_CAP = 40;
const MAX_REASONS = 5;

const DESYNC_TEXT = 'DraftKings repriced after the sharp line — the sharp feed may be lagging';

const BOOK_NAMES = new Map<string, string>([
  ['pinnacle', 'Pinnacle'],
  ['draftkings', 'DraftKings'],
  ['fanduel', 'FanDuel'],
  ['betmgm', 'BetMGM'],
  ['williamhill_us', 'Caesars'],
  ['betonlineag', 'BetOnline'],
  ['lowvig', 'LowVig'],
  ['betrivers', 'BetRivers'],
  ['bovada', 'Bovada'],
  ['fanatics', 'Fanatics'],
  ['espnbet', 'ESPN BET'],
  ['ballybet', 'Bally Bet'],
  ['hardrockbet', 'Hard Rock Bet'],
  ['betus', 'BetUS'],
  ['mybookieag', 'MyBookie'],
  ['betparx', 'betPARX'],
  ['unibet_us', 'Unibet'],
  ['matchbook', 'Matchbook'],
  ['betfair_ex_uk', 'Betfair Exchange'],
  ['betfair_ex_eu', 'Betfair Exchange'],
  ['williamhill', 'William Hill'],
  ['bet365', 'bet365'],
]);

const VERDICT_RANK: Record<Verdict, number> = { BET_NOW: 0, BET: 1, WATCH: 2 };
const URGENCY_RANK: Record<Urgency, number> = { low: 0, medium: 1, high: 2, critical: 3 };

// -------------------------------------------------------------------------------------------------------------
// formatting helpers

function bookName(key: string): string {
  const known = BOOK_NAMES.get(key.toLowerCase());
  if (known) return known;
  return key
    .split(/[_\s]+/)
    .filter(Boolean)
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(' ');
}

/** 3.5 -> "3.5", 3 -> "3", strips float noise. */
function fmtNumber(x: number): string {
  return String(Number(x.toFixed(2)));
}

function fmtLine(line: number): string {
  if (line === 0) return 'PK';
  return line > 0 ? `+${fmtNumber(line)}` : `-${fmtNumber(Math.abs(line))}`;
}

function capitalise(s: string): string {
  return s.charAt(0).toUpperCase() + s.slice(1);
}

/** American display for a decimal price; +100 for even money. */
function fmtDecimalAsAmerican(decimal: number): string {
  return fmtAmerican(decimalToAmerican(decimal));
}

function fmtAmerican(american: number): string {
  return american === -100 ? '+100' : formatAmerican(american);
}

function signedPct(x: number): string {
  return `${x >= 0 ? '+' : '-'}${Math.abs(x * 100).toFixed(1)}%`;
}

function pct(x: number): string {
  return `${(x * 100).toFixed(1)}%`;
}

function dollars(x: number): string {
  return `$${Math.max(0, Math.floor(x)).toLocaleString('en-US')}`;
}

/** "$19" for whole dollars, "$19.05" otherwise (arb legs are split to the cent). */
function money(x: number): string {
  const cents = Math.max(0, Math.round(x * 100));
  const whole = Math.floor(cents / 100).toLocaleString('en-US');
  return cents % 100 === 0 ? `$${whole}` : `$${whole}.${String(cents % 100).padStart(2, '0')}`;
}

// -------------------------------------------------------------------------------------------------------------
// numeric helpers

function clamp(x: number, lo: number, hi: number): number {
  return x < lo ? lo : x > hi ? hi : x;
}

function ageMs(now: number, at: number): number {
  return Number.isFinite(at) ? Math.max(0, now - at) : Number.POSITIVE_INFINITY;
}

function ageSec(now: number, at: number): number {
  const ms = ageMs(now, at);
  return Number.isFinite(ms) ? Math.round(ms / 1000) : 0;
}

/** Unrounded American equivalent of a decimal price (> 1). */
function exactAmerican(decimal: number): number {
  return decimal >= 2 ? (decimal - 1) * 100 : -100 / (decimal - 1);
}

/** Worst (lowest) whole American price whose decimal is at least `decimal`; −100 is reported as +100. */
function worstAmericanAtLeast(decimal: number): number {
  const am = Math.ceil(exactAmerican(decimal));
  return am === -100 ? 100 : am;
}

function urgencyFor(score: number): Urgency {
  if (score >= 75) return 'critical';
  if (score >= 55) return 'high';
  if (score >= 30) return 'medium';
  return 'low';
}

function round1(x: number): number {
  return Math.round(x * 10) / 10;
}

// -------------------------------------------------------------------------------------------------------------
// public API

export function describePick(kind: MarketKind, side: Side, line: number | null, home: string, away: string): string {
  const team = side === 'home' ? home : side === 'away' ? away : capitalise(side);
  switch (kind) {
    case 'moneyline':
      return side === 'draw' ? 'Draw' : `${team} ML`;
    case 'spread':
      return line === null || !Number.isFinite(line) ? team : `${team} ${fmtLine(line)}`;
    case 'total': {
      const label = side === 'over' ? 'Over' : side === 'under' ? 'Under' : capitalise(side);
      return line === null || !Number.isFinite(line) ? label : `${label} ${fmtNumber(line)}`;
    }
    default: {
      const unknown: never = kind;
      return `${team} ${String(unknown)}`;
    }
  }
}

export function sortOpportunities(list: Opportunity[]): Opportunity[] {
  return [...list].sort(compareOpportunities);
}

function compareOpportunities(a: Opportunity, b: Opportunity): number {
  const status = (a.status === 'gone' ? 1 : 0) - (b.status === 'gone' ? 1 : 0);
  if (status !== 0) return status;
  const verdict = (VERDICT_RANK[a.verdict] ?? 3) - (VERDICT_RANK[b.verdict] ?? 3);
  if (verdict !== 0) return verdict;
  if (a.urgencyScore !== b.urgencyScore) return b.urgencyScore - a.urgencyScore;
  if (a.evPct !== b.evPct) return b.evPct - a.evPct;
  return a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
}

export function computeOpportunities(store: MarketStore, ctx: EngineContext): Opportunity[] {
  const out: Opportunity[] = [];
  if (!store || !ctx || !Number.isFinite(ctx.now)) return out;
  const enabled = new Set(ctx.settings.enabledLeagues);
  if (enabled.size === 0) return out;
  const leagues = new Map<string, LeagueDef>();
  for (const l of ctx.leagues) leagues.set(l.key, l);
  const minStart = ctx.now - MAX_EVENT_AGE_MS;

  let failures = 0;
  let lastError: unknown = null;
  for (const event of store.events()) {
    if (!enabled.has(event.league)) continue;
    if (!(event.startTime >= minStart)) continue;
    try {
      evaluateEvent(store, event, leagues.get(event.league), ctx, out);
    } catch (err) {
      failures++;
      lastError = err;
    }
  }
  if (failures > 0) log.warn('Skipped events that failed evaluation', { failures, error: lastError });
  return sortOpportunities(out);
}

// -------------------------------------------------------------------------------------------------------------
// per-event evaluation

interface EventFrame {
  store: MarketStore;
  event: CanonicalEvent;
  ctx: EngineContext;
  live: boolean;
  maxSharpAgeMs: number;
  maxDkAgeMs: number;
  minEv: number;
  /**
   * Books never used as the other leg of an arb: DraftKings itself and the reference (sharp) books. The default
   * sharp books (Pinnacle, BetOnline, LowVig) are offshore books a US DraftKings bettor cannot use, and with their
   * thin margin every +EV DraftKings price would also show up as a "DraftKings vs Pinnacle arb".
   */
  arbExcludedBooks: Set<string>;
}

/** An EV opportunity whose reasons are finalised once every pick of the event is known (correlation note). */
interface EvDraft {
  opp: Opportunity;
  evLine: string;
  refLine: string;
  signal: string | null;
  action: string;
}

function evaluateEvent(
  store: MarketStore,
  event: CanonicalEvent,
  league: LeagueDef | undefined,
  ctx: EngineContext,
  out: Opportunity[],
): void {
  const quotes = store.quotesForEvent(event.id);
  let hasDk = false;
  for (const q of quotes) {
    if (q.book === DK) {
      hasDk = true;
      break;
    }
  }
  if (!hasDk) return;

  const { model, settings } = ctx;
  const live = event.isLive === true;
  // The game has started but the feed has not reported it live yet (it is polled every few minutes): the prices we
  // hold are pre-game prices that the book no longer offers. Wait for the next poll instead of showing them.
  if (!live && event.startTime <= ctx.now) return;
  const frame: EventFrame = {
    store,
    event,
    ctx,
    live,
    maxSharpAgeMs: (live ? model.liveMaxSharpAgeSec : model.prematchMaxSharpAgeSec) * 1000,
    maxDkAgeMs: (live ? model.liveMaxDkAgeSec : model.prematchMaxDkAgeSec) * 1000,
    minEv: live ? settings.minEvLive : settings.minEvPrematch,
    arbExcludedBooks: new Set([DK, ...ctx.sharpBooks.map((b) => b.toLowerCase())]),
  };
  const threeWay = league?.threeWay === true;
  const fairOpts: FairOptions = {
    sharpBooks: ctx.sharpBooks,
    excludeBooks: [DK],
    method: model.devigMethod,
    minConsensusBooks: model.minConsensusBooks,
    threeWay,
    now: ctx.now,
    maxAgeMs: frame.maxSharpAgeMs,
  };

  const drafts: EvDraft[] = [];
  for (const group of groupQuotes(quotes)) {
    const dkOutcomes = group.books.get(DK);
    if (!dkOutcomes) continue;
    const candidates: StoredQuote[] = [];
    for (const q of dkOutcomes.values()) {
      if (isUsableDkQuote(q, frame)) candidates.push(q);
    }
    if (candidates.length === 0) continue;

    const fair = fairForGroup(group, fairOpts);
    if (fair) {
      for (const dk of candidates) {
        const draft = evDraft(frame, group, fair, dk);
        if (draft) drafts.push(draft);
      }
    }

    if (settings.showArbs) {
      const sides = requiredSides(group.kind, threeWay);
      for (const dk of candidates) {
        const arb = arbOpportunity(frame, group, sides, dk);
        if (arb) out.push(arb);
      }
    }
  }

  for (const draft of drafts) out.push(finaliseDraft(draft, drafts));
}

function isUsableDkQuote(q: StoredQuote, frame: EventFrame): boolean {
  const { model } = frame.ctx;
  if (q.suspended) return false;
  if (!(typeof q.decimal === 'number' && Number.isFinite(q.decimal) && q.decimal > 1)) return false;
  if (!(ageMs(frame.ctx.now, q.observedAt) <= frame.maxDkAgeMs)) return false;
  if (q.decimal > model.maxDecimalOdds) return false;
  if (!q.isMainLine && !model.includeAltLines) return false;
  return true;
}

function evDraft(frame: EventFrame, group: MarketGroup, fair: FairResult, dk: StoredQuote): EvDraft | null {
  const { ctx, event, live, store, minEv } = frame;
  const { settings, model, now } = ctx;
  const side = dk.side;
  const p = fair.probs[side];
  if (typeof p !== 'number' || !(p > 0 && p < 1)) return null;
  const d = dk.decimal;
  const ev = p * d - 1;
  if (!(ev >= settings.watchEv) || ev > model.maxPlausibleEv) return null;

  // 4. Desync guard: DraftKings moved after the reference last moved.
  const desync = dk.lastChangeAt > fair.lastChangeAt + DESYNC_MS;
  const forcedWatch = desync && live;

  // 5. Stale line: the sharp book moved towards our side recently and DraftKings has not followed.
  let staleLine = false;
  let staleText: string | null = null;
  const sharpQuote = fair.sharpQuotes ? fair.sharpQuotes[side] : undefined;
  if (sharpQuote) {
    const sharpThen = store.priceAt(sharpQuote, now - model.staleWindowSec * 1000);
    const sharpNow = sharpQuote.decimal;
    if (sharpThen !== null && sharpThen > 1 && sharpNow > 1) {
      const moveProb = 1 / sharpNow - 1 / sharpThen;
      if (moveProb >= model.staleMoveProb && dk.lastChangeAt <= sharpQuote.lastChangeAt - STALE_LAG_MS) {
        staleLine = true;
        const ago = Math.max(1, ageSec(now, sharpQuote.lastChangeAt));
        staleText =
          `${bookName(sharpQuote.book)} moved ${fmtDecimalAsAmerican(sharpThen)} → ${fmtDecimalAsAmerican(sharpNow)} ` +
          `in the last ${ago}s; DraftKings still ${fmtDecimalAsAmerican(d)}`;
      }
    }
  }

  // 6. Confidence.
  let confidence =
    fair.sharpBook !== null
      ? fair.sharpBook.toLowerCase() === 'pinnacle'
        ? 0.9
        : 0.8
      : Math.min(0.85, 0.55 + 0.05 * fair.bookCount);
  if (live) confidence *= 0.85;
  const sharpAgeMs = ageMs(now, fair.observedAt);
  const ageFraction = frame.maxSharpAgeMs > 0 ? clamp(sharpAgeMs / frame.maxSharpAgeMs, 0, 1) : 1;
  confidence *= 1 - 0.3 * ageFraction;
  if (d > 4) confidence *= 0.85;
  if (desync && !live) confidence *= 0.85;
  confidence = clamp(Number.isFinite(confidence) ? confidence : 0.1, 0.1, 1);

  // 7. Verdict.
  let verdict: Verdict =
    ev >= minEv && confidence >= MIN_BET_CONFIDENCE ? (live || staleLine ? 'BET_NOW' : 'BET') : 'WATCH';
  if (forcedWatch) verdict = 'WATCH';

  // 8. Urgency.
  let score = 0;
  if (live) score += 45;
  if (staleLine) score += 30;
  score += Math.min(25, Math.max(0, ev * 500));
  if (!live) {
    const untilStart = event.startTime - now;
    if (untilStart <= 15 * 60_000) score += 15;
    else if (untilStart <= 60 * 60_000) score += 8;
    else if (untilStart <= 3 * 60 * 60_000) score += 3;
  }
  if (verdict === 'WATCH') score = Math.min(score, WATCH_URGENCY_CAP);
  const urgencyScore = round1(clamp(score, 0, 100));

  // 9. Expiry estimate.
  const expiresInSec = live
    ? staleLine
      ? 20
      : 45
    : staleLine
      ? 120
      : event.startTime - now < 60 * 60_000
        ? 300
        : 900;

  // 10. Stake.
  let stakeResult: StakeResult | null = null;
  if (verdict !== 'WATCH') {
    stakeResult = suggestStake({
      prob: p,
      decimal: d,
      bankroll: settings.bankroll,
      kellyMultiplier: settings.kellyMultiplier,
      confidence,
      maxStakePct: settings.maxStakePct,
      maxStakeAbs: settings.maxStakeAbs,
      remainingDailyExposure: ctx.remainingDailyExposure,
    });
  }
  const stake = stakeResult ? stakeResult.stake : 0;
  const kellyFraction = stakeResult ? stakeResult.fraction : 0;

  // 11. Prices.
  const minAcceptableAmerican = worstAmericanAtLeast((1 + minEv) / p);
  const fairDecimal = 1 / p;
  const fairAmerican = decimalToAmerican(fairDecimal);
  const dkAmerican = decimalToAmerican(d);

  // 12–14. Identity and presentation.
  const pick = describePick(group.kind, side, dk.line, event.home, event.away);
  const sharpAgeSec = ageSec(now, fair.observedAt);
  const evLine = `DraftKings ${fmtAmerican(dkAmerican)} vs fair ${fmtAmerican(fairAmerican)} (${pct(p)} to win) — EV ${signedPct(ev)}`;
  const refLine =
    fair.sharpBook !== null
      ? `Reference: ${bookName(fair.sharpBook)} no-vig (data ${sharpAgeSec}s old)`
      : `Reference: consensus of ${fair.bookCount} books — no sharp book available (data ${sharpAgeSec}s old)`;
  const signal = staleText ?? (desync ? (live ? DESYNC_TEXT : `${DESYNC_TEXT}; confidence reduced`) : null);

  let action: string;
  if (verdict !== 'WATCH') {
    action = `Take it at ${fmtAmerican(minAcceptableAmerican)} or better`;
    if (live) action += ` — live price, act within ~${expiresInSec}s`;
    else if (staleLine) action += ` — DraftKings is likely to move within ~${Math.round(expiresInSec / 60)} min`;
    if (stakeResult && stakeResult.cappedBy === 'dailyExposure') {
      action +=
        stake > 0
          ? `; stake capped by today's remaining exposure (${dollars(ctx.remainingDailyExposure)} left)`
          : `; today's exposure limit is used up, so no stake is suggested`;
    }
  } else if (forcedWatch) {
    action = 'Watch only until the sharp line confirms the new DraftKings price';
  } else if (ev < minEv) {
    action = `Needs ${fmtAmerican(minAcceptableAmerican)} or better to reach the ${pct(minEv)} minimum EV`;
  } else {
    action = `Model confidence ${Math.round(confidence * 100)}% is below ${Math.round(MIN_BET_CONFIDENCE * 100)}% — watch only`;
  }

  const opp: Opportunity = {
    id: `${event.id}|ev|${group.kind}|${side}|${dk.line ?? ''}`,
    type: 'ev',
    eventId: event.id,
    league: event.league,
    eventName: `${event.away} @ ${event.home}`,
    home: event.home,
    away: event.away,
    startTime: event.startTime,
    isLive: live,
    kind: group.kind,
    side,
    line: dk.line,
    pick,
    dkDecimal: d,
    dkAmerican,
    fairProb: p,
    fairDecimal,
    fairAmerican,
    evPct: ev,
    minAcceptableAmerican,
    kellyFraction,
    stake,
    confidence,
    urgency: urgencyFor(urgencyScore),
    urgencyScore,
    verdict,
    reasons: [],
    sharpSource: fair.source,
    sharpAgeSec,
    dkAgeSec: ageSec(now, dk.observedAt),
    staleLine,
    firstSeen: now,
    lastSeen: now,
    status: 'active',
    expiresInSec,
    dkUrl: dk.link ?? null,
  };
  if (event.score) opp.score = { ...event.score };
  return { opp, evLine, refLine, signal, action };
}

/** Adds the correlation note (a higher-EV actionable pick exists on the same event) and assembles reasons. */
function finaliseDraft(draft: EvDraft, all: EvDraft[]): Opportunity {
  const self = draft.opp;
  let better: Opportunity | null = null;
  for (const other of all) {
    const o = other.opp;
    if (o === self || o.verdict === 'WATCH') continue;
    const higher = o.evPct > self.evPct || (o.evPct === self.evPct && o.id < self.id);
    if (!higher) continue;
    if (!better || o.evPct > better.evPct || (o.evPct === better.evPct && o.id < better.id)) better = o;
  }
  const reasons = [draft.evLine, draft.refLine];
  if (draft.signal) reasons.push(draft.signal);
  if (better) reasons.push(`Correlated with ${better.pick} — pick one`);
  reasons.push(draft.action);
  self.reasons = reasons.slice(0, MAX_REASONS);
  return self;
}

// -------------------------------------------------------------------------------------------------------------
// arbitrage

function isUsableArbLeg(q: StoredQuote, frame: EventFrame): boolean {
  if (q.suspended) return false;
  if (!(typeof q.decimal === 'number' && Number.isFinite(q.decimal) && q.decimal > 1)) return false;
  if (!(ageMs(frame.ctx.now, q.observedAt) <= frame.maxDkAgeMs)) return false;
  if (!q.isMainLine && !frame.ctx.model.includeAltLines) return false;
  return true;
}

function arbOpportunity(frame: EventFrame, group: MarketGroup, sides: Side[], dk: StoredQuote): Opportunity | null {
  const { ctx, event, live } = frame;
  const { settings, model, now } = ctx;
  const s = dk.side;
  if (!sides.includes(s)) return null;

  const others: StoredQuote[] = [];
  let booksum = 1 / dk.decimal;
  for (const side of sides) {
    if (side === s) continue;
    let best: StoredQuote | null = null;
    for (const [book, outcomes] of group.books) {
      if (frame.arbExcludedBooks.has(book.toLowerCase())) continue;
      const q = outcomes.get(side);
      if (!q || !isUsableArbLeg(q, frame)) continue;
      if (!best || q.decimal > best.decimal || (q.decimal === best.decimal && q.book < best.book)) best = q;
    }
    if (!best) return null;
    others.push(best);
    booksum += 1 / best.decimal;
  }
  if (!(booksum < 1)) return null;
  const profitPct = 1 / booksum - 1;
  if (profitPct < MIN_ARB_PROFIT || profitPct > model.maxPlausibleEv) return null;

  const cap = Math.min(
    2 * settings.bankroll * settings.maxStakePct,
    2 * settings.maxStakeAbs,
    ctx.remainingDailyExposure,
  );
  const totalStake = Number.isFinite(cap) && cap > 0 ? Math.floor(cap) : 0;
  const legQuotes = [dk, ...others];
  const legs: ArbLeg[] = legQuotes.map((q) => ({
    book: q.book,
    side: q.side,
    line: q.line,
    decimal: q.decimal,
    american: decimalToAmerican(q.decimal),
    // Split to the cent: with whole dollars the rounding alone can cost more than a thin arb's locked profit.
    stake: Math.round(((totalStake * (1 / q.decimal)) / booksum) * 100) / 100,
  }));
  const dkLeg = legs[0];

  const fairProb = 1 / dk.decimal / booksum;
  const fairDecimal = 1 / fairProb;
  const othersSum = booksum - 1 / dk.decimal;
  const minDkInverse = 1 / (1 + MIN_ARB_PROFIT) - othersSum;
  const minAcceptableAmerican = minDkInverse > 0 && minDkInverse < 1 ? worstAmericanAtLeast(1 / minDkInverse) : dkLeg.american;

  let oldestOtherObserved = Number.POSITIVE_INFINITY;
  for (const q of others) oldestOtherObserved = Math.min(oldestOtherObserved, q.observedAt);
  const oldestLegMs = Math.max(ageMs(now, dk.observedAt), ageMs(now, oldestOtherObserved));
  let confidence = ARB_BASE_CONFIDENCE;
  if (live) confidence *= 0.85;
  confidence *= 1 - 0.3 * (frame.maxDkAgeMs > 0 ? clamp(oldestLegMs / frame.maxDkAgeMs, 0, 1) : 1);
  confidence = clamp(confidence, 0.1, 1);

  const urgencyScore = round1(clamp(50 + (live ? 30 : 0) + Math.min(20, profitPct * 1000), 0, 100));
  const expiresInSec = live ? 20 : 120;
  const pick = describePick(group.kind, s, dk.line, event.home, event.away);
  const legText = (leg: ArbLeg): string =>
    `${bookName(leg.book)} ${describePick(group.kind, leg.side, leg.line, event.home, event.away)} ${fmtAmerican(leg.american)}`;
  const otherLegs = legs.slice(1);

  const reasons = [
    `Arbitrage: ${legs.map(legText).join(' + ')} locks in ${signedPct(profitPct)} whatever the result`,
    totalStake > 0
      ? `Bet ${money(dkLeg.stake)} on ${pick} at DraftKings and ${otherLegs
          .map((l) => `${money(l.stake)} on ${describePick(group.kind, l.side, l.line, event.home, event.away)} at ${bookName(l.book)}`)
          .join(' and ')} — the other leg is at another sportsbook`
      : `The other leg is at another sportsbook (${otherLegs.map((l) => bookName(l.book)).join(', ')}); today's exposure limit is used up, so no stakes are suggested`,
    `Place both legs quickly — if either price moves the arb disappears${live ? ` (live: act within ~${expiresInSec}s)` : ''}`,
    `The DraftKings leg still works at ${fmtAmerican(minAcceptableAmerican)} or better`,
  ];

  const opp: Opportunity = {
    id: `${event.id}|arb|${group.kind}|${s}|${dk.line ?? ''}`,
    type: 'arb',
    eventId: event.id,
    league: event.league,
    eventName: `${event.away} @ ${event.home}`,
    home: event.home,
    away: event.away,
    startTime: event.startTime,
    isLive: live,
    kind: group.kind,
    side: s,
    line: dk.line,
    pick,
    dkDecimal: dk.decimal,
    dkAmerican: dkLeg.american,
    fairProb,
    fairDecimal,
    fairAmerican: decimalToAmerican(fairDecimal),
    evPct: profitPct,
    minAcceptableAmerican,
    kellyFraction: settings.bankroll > 0 ? dkLeg.stake / settings.bankroll : 0,
    stake: dkLeg.stake,
    confidence,
    urgency: urgencyFor(urgencyScore),
    urgencyScore,
    verdict: live ? 'BET_NOW' : 'BET',
    reasons,
    sharpSource: others.map((q) => q.book).join('+'),
    sharpAgeSec: ageSec(now, oldestOtherObserved),
    dkAgeSec: ageSec(now, dk.observedAt),
    staleLine: false,
    firstSeen: now,
    lastSeen: now,
    status: 'active',
    expiresInSec,
    dkUrl: dk.link ?? null,
    arb: { legs, profitPct, totalStake },
  };
  if (event.score) opp.score = { ...event.score };
  return opp;
}

// -------------------------------------------------------------------------------------------------------------
// tracker

interface PreviousState {
  urgency: Urgency;
  verdict: Verdict;
}

const DEFAULT_MAX_ITEMS = 300;

export class OpportunityTracker {
  private readonly goneRetentionMs: number;
  private readonly maxItems: number;
  private items = new Map<string, Opportunity>();
  private list: Opportunity[] = [];
  /** State of every tracked id just before the most recent update() (for newlyAlertable). */
  private previous = new Map<string, PreviousState>();

  constructor(opts: { goneRetentionSec: number; maxItems?: number }) {
    const retention = opts && Number.isFinite(opts.goneRetentionSec) ? Math.max(0, opts.goneRetentionSec) : 0;
    this.goneRetentionMs = retention * 1000;
    const max = opts ? opts.maxItems : undefined;
    this.maxItems = typeof max === 'number' && Number.isFinite(max) && max >= 1 ? Math.floor(max) : DEFAULT_MAX_ITEMS;
  }

  update(candidates: Opportunity[], now: number): Opportunity[] {
    const previous = new Map<string, PreviousState>();
    for (const [id, o] of this.items) previous.set(id, { urgency: o.urgency, verdict: o.verdict });

    const next = new Map<string, Opportunity>();
    for (const c of Array.isArray(candidates) ? candidates : []) {
      if (!c || typeof c.id !== 'string' || next.has(c.id)) continue;
      const existing = this.items.get(c.id);
      next.set(c.id, { ...c, firstSeen: existing ? existing.firstSeen : now, lastSeen: now, status: 'active' });
    }
    for (const [id, old] of this.items) {
      if (next.has(id)) continue;
      if (now - old.lastSeen > this.goneRetentionMs) continue;
      next.set(id, old.status === 'gone' ? old : { ...old, status: 'gone' });
    }

    const sorted = sortOpportunities([...next.values()]);
    if (sorted.length > this.maxItems) sorted.length = this.maxItems;
    this.list = sorted;
    this.items = new Map(sorted.map((o) => [o.id, o]));
    this.previous = previous;
    return sorted.slice();
  }

  current(): Opportunity[] {
    return this.list.slice();
  }

  find(id: string): Opportunity | undefined {
    return this.items.get(id);
  }

  /**
   * Active, actionable (not WATCH) items at or above `minUrgency` that became so in the most recent update():
   * new ids, ids whose urgency rose to the threshold, and ids that were previously only WATCH.
   */
  newlyAlertable(minUrgency: Urgency): Opportunity[] {
    const minRank = URGENCY_RANK[minUrgency] ?? URGENCY_RANK.critical;
    const out: Opportunity[] = [];
    for (const o of this.list) {
      if (o.status !== 'active' || o.verdict === 'WATCH') continue;
      if ((URGENCY_RANK[o.urgency] ?? 0) < minRank) continue;
      const prev = this.previous.get(o.id);
      if (!prev || (URGENCY_RANK[prev.urgency] ?? 0) < minRank || prev.verdict === 'WATCH') out.push(o);
    }
    return out;
  }
}

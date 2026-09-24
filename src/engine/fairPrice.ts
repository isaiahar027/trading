/**
 * Fair (no-vig) probabilities for one market.
 *
 * Quotes are grouped into markets by `groupKey` (all outcomes that belong together: both sides of a spread at the
 * same home line, both sides of a total at the same number, every moneyline outcome). For each market the
 * reference is the first usable sharp book in priority order; failing that, a consensus of every usable book.
 *
 * A book's market is usable only when it has every required side (and no side that does not belong to the market,
 * e.g. a draw price in a two-way moneyline), none of them suspended, every price valid, and every quote observed
 * within `maxAgeMs` before `now` (and not after it: a future observation means the clock stepped back).
 */
import type { DevigMethod } from '../config';
import type { MarketKind, Side } from '../types';
import { devig } from './devig';
import type { StoredQuote } from './marketStore';

export type BookOutcomes = Map<Side, StoredQuote>;

export interface MarketGroup {
  groupKey: string;
  kind: MarketKind;
  books: Map<string, BookOutcomes>;
}

export interface FairOptions {
  sharpBooks: string[];
  excludeBooks: string[];
  method: DevigMethod;
  minConsensusBooks: number;
  threeWay: boolean;
  now: number;
  maxAgeMs: number;
}

export interface FairResult {
  groupKey: string;
  kind: MarketKind;
  probs: Partial<Record<Side, number>>;
  /** 'pinnacle' | 'betonlineag' | 'consensus(4)' */
  source: string;
  /** Book used when a single sharp book was used. */
  sharpBook: string | null;
  bookCount: number;
  /** Oldest observedAt among quotes used (freshness of the data we hold). */
  observedAt: number;
  /** Newest lastChangeAt among quotes used (when the reference last moved). */
  lastChangeAt: number;
  /** Quotes used, keyed by side, for the single sharp book (null for consensus). */
  sharpQuotes: Partial<Record<Side, StoredQuote>> | null;
}

/** -0 -> 0 so `home -0` and `away +0` (pick'em) share a group. */
function normZero(x: number): number {
  return x === 0 ? 0 : x;
}

export function groupKey(kind: MarketKind, side: Side, line: number | null): string {
  switch (kind) {
    case 'moneyline':
      return 'moneyline|';
    case 'spread': {
      if (line === null || !Number.isFinite(line)) return 'spread|';
      const homeLine = side === 'home' ? line : -line;
      return `spread|${normZero(homeLine)}`;
    }
    case 'total':
      return `total|${line === null || !Number.isFinite(line) ? '' : normZero(line)}`;
    default: {
      const unknown: never = kind;
      return `${String(unknown)}|`;
    }
  }
}

export function requiredSides(kind: MarketKind, threeWay: boolean): Side[] {
  switch (kind) {
    case 'moneyline':
      return threeWay ? ['home', 'draw', 'away'] : ['home', 'away'];
    case 'spread':
      return ['home', 'away'];
    case 'total':
      return ['over', 'under'];
    default: {
      const unknown: never = kind;
      throw new RangeError(`Unknown market kind: ${String(unknown)}`);
    }
  }
}

/**
 * Groups quotes into markets. Insertion order of groups and books follows the input order. If the same
 * (group, book, side) appears twice, the most recently observed quote wins.
 */
export function groupQuotes(quotes: StoredQuote[]): MarketGroup[] {
  const groups = new Map<string, MarketGroup>();
  for (const q of quotes) {
    if (!q) continue;
    const key = groupKey(q.kind, q.side, q.line);
    let group = groups.get(key);
    if (!group) {
      group = { groupKey: key, kind: q.kind, books: new Map() };
      groups.set(key, group);
    }
    let outcomes = group.books.get(q.book);
    if (!outcomes) {
      outcomes = new Map();
      group.books.set(q.book, outcomes);
    }
    const prev = outcomes.get(q.side);
    if (!prev || q.observedAt > prev.observedAt) outcomes.set(q.side, q);
  }
  return [...groups.values()];
}

/**
 * Observations more than this far after `now` mean the wall clock stepped backwards since they were taken: their age
 * is unknown, so they are not treated as fresh.
 */
const FUTURE_SKEW_MS = 5_000;

/** The required-side quotes (in `sides` order) when the book's market is usable, else null. */
function usableQuotes(
  outcomes: BookOutcomes | undefined,
  sides: Side[],
  minObservedAt: number,
  maxObservedAt: number,
): StoredQuote[] | null {
  if (!outcomes) return null;
  for (const side of outcomes.keys()) {
    if (!sides.includes(side)) return null;
  }
  const out: StoredQuote[] = [];
  for (const side of sides) {
    const q = outcomes.get(side);
    if (!q || q.suspended) return null;
    if (!(q.observedAt >= minObservedAt && q.observedAt <= maxObservedAt)) return null;
    if (typeof q.decimal !== 'number' || !Number.isFinite(q.decimal) || q.decimal <= 1) return null;
    out.push(q);
  }
  return out;
}

/** devig that never throws; null unless every probability is finite and inside (0, 1). */
function safeDevig(quotes: StoredQuote[], method: DevigMethod): number[] | null {
  let probs: number[];
  try {
    probs = devig(
      quotes.map((q) => q.decimal),
      method,
    );
  } catch {
    return null;
  }
  if (probs.length !== quotes.length) return null;
  for (const p of probs) {
    if (!Number.isFinite(p) || p <= 0 || p >= 1) return null;
  }
  return probs;
}

function findBook(group: MarketGroup, name: string): [string, BookOutcomes] | null {
  const exact = group.books.get(name);
  if (exact) return [name, exact];
  const lower = name.toLowerCase();
  for (const [book, outcomes] of group.books) {
    if (book.toLowerCase() === lower) return [book, outcomes];
  }
  return null;
}

function freshness(quotes: StoredQuote[]): { observedAt: number; lastChangeAt: number } {
  let observedAt = Number.POSITIVE_INFINITY;
  let lastChangeAt = Number.NEGATIVE_INFINITY;
  for (const q of quotes) {
    if (q.observedAt < observedAt) observedAt = q.observedAt;
    if (q.lastChangeAt > lastChangeAt) lastChangeAt = q.lastChangeAt;
  }
  return { observedAt, lastChangeAt };
}

export function fairForGroup(group: MarketGroup, opts: FairOptions): FairResult | null {
  if (!group || !(group.books instanceof Map)) return null;
  const sides = requiredSides(group.kind, opts.threeWay);
  const excluded = new Set((opts.excludeBooks ?? []).map((b) => b.toLowerCase()));
  const minObservedAt = opts.now - opts.maxAgeMs;
  const maxObservedAt = opts.now + FUTURE_SKEW_MS;

  // 1. First usable sharp book in priority order.
  const tried = new Set<string>();
  for (const name of opts.sharpBooks ?? []) {
    const lower = name.toLowerCase();
    if (excluded.has(lower) || tried.has(lower)) continue;
    tried.add(lower);
    const found = findBook(group, name);
    if (!found) continue;
    const [book, outcomes] = found;
    const used = usableQuotes(outcomes, sides, minObservedAt, maxObservedAt);
    if (!used) continue;
    const probs = safeDevig(used, opts.method);
    if (!probs) continue;
    const sharpQuotes: Partial<Record<Side, StoredQuote>> = {};
    const probsBySide: Partial<Record<Side, number>> = {};
    sides.forEach((side, i) => {
      probsBySide[side] = probs[i];
      sharpQuotes[side] = used[i];
    });
    return {
      groupKey: group.groupKey,
      kind: group.kind,
      probs: probsBySide,
      source: book,
      sharpBook: book,
      bookCount: 1,
      ...freshness(used),
      sharpQuotes,
    };
  }

  // 2. Consensus of every usable, non-excluded book.
  const minBooks = Number.isFinite(opts.minConsensusBooks) ? Math.max(1, Math.ceil(opts.minConsensusBooks)) : 1;
  const sums = new Array<number>(sides.length).fill(0);
  const allUsed: StoredQuote[] = [];
  let count = 0;
  for (const [book, outcomes] of group.books) {
    if (excluded.has(book.toLowerCase())) continue;
    const used = usableQuotes(outcomes, sides, minObservedAt, maxObservedAt);
    if (!used) continue;
    const probs = safeDevig(used, opts.method);
    if (!probs) continue;
    for (let i = 0; i < sides.length; i++) sums[i] += probs[i];
    allUsed.push(...used);
    count++;
  }
  if (count < minBooks || count === 0) return null;
  const probsBySide: Partial<Record<Side, number>> = {};
  sides.forEach((side, i) => {
    probsBySide[side] = sums[i] / count;
  });
  return {
    groupKey: group.groupKey,
    kind: group.kind,
    probs: probsBySide,
    source: `consensus(${count})`,
    sharpBook: null,
    bookCount: count,
    ...freshness(allUsed),
    sharpQuotes: null,
  };
}

export function fairProbForPick(
  quotes: StoredQuote[],
  kind: MarketKind,
  side: Side,
  line: number | null,
  opts: FairOptions,
): number | null {
  const key = groupKey(kind, side, line);
  const relevant = quotes.filter((q) => q && q.kind === kind && groupKey(q.kind, q.side, q.line) === key);
  if (relevant.length === 0) return null;
  const group = groupQuotes(relevant).find((g) => g.groupKey === key);
  if (!group) return null;
  const fair = fairForGroup(group, opts);
  const p = fair?.probs[side];
  return typeof p === 'number' ? p : null;
}

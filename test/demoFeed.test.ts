import { describe, expect, it } from 'vitest';
import { DEFAULT_LEAGUES, DEFAULT_ODDS_API_BOOKS } from '../src/config';
import { DemoFeed } from '../src/sources/demoFeed';
import type { LeagueDef, Quote, Side, SourceSnapshot } from '../src/types';

const T0 = Date.UTC(2026, 8, 23, 18, 0, 0);
const MIN = 60_000;
const HOUR = 60 * MIN;
const TICK = 3_000;
const DK_LINK = 'https://sportsbook.draftkings.com/';

function feed(seed = 42, leagues: LeagueDef[] = DEFAULT_LEAGUES, books: string[] = DEFAULT_ODDS_API_BOOKS): DemoFeed {
  return new DemoFeed(leagues, books, { seed });
}

/** Market group key as the engine defines it (spread keyed by the home line). */
function groupKey(q: Quote): string {
  if (q.kind === 'moneyline') return 'moneyline|';
  if (q.kind === 'spread') {
    const home = q.side === 'home' ? (q.line as number) : -(q.line as number);
    return `spread|${home === 0 ? 0 : home}`;
  }
  return `total|${q.line}`;
}

function requiredSides(kind: Quote['kind'], threeWay: boolean): Side[] {
  if (kind === 'moneyline') return threeWay ? ['home', 'draw', 'away'] : ['home', 'away'];
  if (kind === 'spread') return ['home', 'away'];
  return ['over', 'under'];
}

/** event -> group -> book -> side -> quote */
function index(snap: SourceSnapshot): Map<string, Map<string, Map<string, Map<Side, Quote>>>> {
  const out = new Map<string, Map<string, Map<string, Map<Side, Quote>>>>();
  for (const q of snap.quotes) {
    let ev = out.get(q.sourceEventId);
    if (!ev) out.set(q.sourceEventId, (ev = new Map()));
    const gk = `${q.kind}#${groupKey(q)}`;
    let group = ev.get(gk);
    if (!group) ev.set(gk, (group = new Map()));
    let book = group.get(q.book);
    if (!book) group.set(q.book, (book = new Map()));
    if (book.has(q.side)) throw new Error(`duplicate quote ${q.sourceEventId} ${q.book} ${gk} ${q.side}`);
    book.set(q.side, q);
  }
  return out;
}

/** Best EV of a non-suspended DraftKings price against the multiplicative no-vig Pinnacle price in the same market. */
function dkEdges(snap: SourceSnapshot): number[] {
  const edges: number[] = [];
  for (const groups of index(snap).values()) {
    for (const books of groups.values()) {
      const pin = books.get('pinnacle');
      const dk = books.get('draftkings');
      if (!pin || !dk || pin.size !== dk.size) continue;
      const overround = [...pin.values()].reduce((s, q) => s + 1 / q.decimal, 0);
      for (const [side, q] of dk) {
        const ref = pin.get(side);
        if (!ref || q.suspended || ref.suspended) continue;
        const fair = 1 / ref.decimal / overround;
        edges.push(fair * q.decimal - 1);
      }
    }
  }
  return edges;
}

describe('DemoFeed', () => {
  it('is deterministic for a seed and differs across seeds', () => {
    const a = feed(7);
    const b = feed(7);
    const c = feed(8);
    let differs = false;
    for (let at = T0; at < T0 + 5 * MIN; at += TICK) {
      const sa = a.tick(at);
      expect(b.tick(at)).toEqual(sa);
      if (JSON.stringify(c.tick(at)) !== JSON.stringify(sa)) differs = true;
    }
    expect(differs).toBe(true);
  });

  it('produces one complete demo snapshot per demo league with ~14 events, about half live', () => {
    const snaps = feed().tick(T0);
    expect(snaps.map((s) => s.league)).toEqual(['NFL', 'NBA', 'MLB', 'NHL', 'EPL']);
    const events = snaps.flatMap((s) => s.events);
    expect(events.length).toBe(14);
    const live = events.filter((e) => e.isLive);
    expect(live.length).toBeGreaterThanOrEqual(5);
    expect(live.length).toBeLessThanOrEqual(9);
    for (const e of live) {
      expect(e.startTime).toBeLessThanOrEqual(T0);
      expect(e.score).toBeDefined();
      expect(typeof e.score?.period).toBe('string');
    }
    for (const e of events.filter((x) => !x.isLive)) {
      expect(e.startTime).toBeGreaterThanOrEqual(T0 + 10 * MIN);
      expect(e.startTime).toBeLessThanOrEqual(T0 + 20 * HOUR);
      expect(e.score).toBeUndefined();
    }
    for (const s of snaps) {
      expect(s.source).toBe('demo');
      expect(s.complete).toBe(true);
      expect(s.fetchedAt).toBe(T0);
      expect(s.books.slice(0, 2)).toEqual(['pinnacle', 'draftkings']);
      expect(s.books.length).toBeGreaterThanOrEqual(4);
      expect(s.books.length).toBeLessThanOrEqual(5);
      for (const e of s.events) {
        expect(e.source).toBe('demo');
        expect(e.league).toBe(s.league);
        expect(e.home).not.toBe(e.away);
      }
    }
    expect(events[0].sourceEventId).toBe('nfl-1');
  });

  it('keeps event ids stable across ticks', () => {
    const f = feed();
    const ids = (snaps: SourceSnapshot[]) => snaps.flatMap((s) => s.events.map((e) => e.sourceEventId));
    const first = ids(f.tick(T0));
    expect(new Set(first).size).toBe(first.length);
    for (let at = T0 + TICK; at <= T0 + 10 * MIN; at += TICK) expect(ids(f.tick(at))).toEqual(first);
  });

  it('emits valid quotes with every required side per book and market', () => {
    const f = feed(3);
    const problems: string[] = [];
    const bad = (q: Quote, why: string) => problems.push(`${why}: ${JSON.stringify(q)}`);
    let checked = 0;
    for (let at = T0; at <= T0 + 20 * MIN; at += TICK) {
      for (const snap of f.tick(at)) {
        const threeWay = snap.league === 'EPL';
        const eventIds = new Set(snap.events.map((e) => e.sourceEventId));
        for (const q of snap.quotes) {
          checked++;
          if (!eventIds.has(q.sourceEventId)) bad(q, 'unknown event');
          if (!snap.books.includes(q.book)) bad(q, 'book not in snapshot.books');
          if (q.source !== 'demo') bad(q, 'source');
          if (!Number.isFinite(q.decimal) || q.decimal <= 1) bad(q, 'decimal');
          if (q.observedAt !== at) bad(q, 'observedAt');
          if (q.bookUpdatedAt === null || q.bookUpdatedAt > at) bad(q, 'bookUpdatedAt');
          if (q.kind === 'moneyline' ? q.line !== null : !Number.isFinite(q.line)) bad(q, 'line');
          if (q.book === 'draftkings' ? q.link !== DK_LINK : q.link !== undefined) bad(q, 'link');
        }
        for (const groups of index(snap).values()) {
          for (const [gk, books] of groups) {
            const kind = gk.split('#')[0] as Quote['kind'];
            const required = requiredSides(kind, threeWay).sort().join(',');
            for (const [book, sides] of books) {
              const quotes = [...sides.values()];
              if ([...sides.keys()].sort().join(',') !== required) bad(quotes[0], `sides of ${book} ${gk}`);
              if (kind === 'spread' && quotes[0].line !== -(quotes[1].line as number)) bad(quotes[0], 'spread lines');
              if (kind === 'total' && quotes[0].line !== quotes[1].line) bad(quotes[0], 'total lines');
            }
          }
        }
      }
    }
    expect(checked).toBeGreaterThan(100_000);
    expect(problems.slice(0, 5)).toEqual([]);
  });

  it('gives soccer a three-way moneyline with a draw and no draw elsewhere', () => {
    const snaps = feed().tick(T0);
    for (const snap of snaps) {
      const draws = snap.quotes.filter((q) => q.kind === 'moneyline' && q.side === 'draw');
      if (snap.league === 'EPL') {
        expect(draws.length).toBe(snap.events.length * snap.books.length);
      } else {
        expect(draws).toEqual([]);
      }
    }
  });

  it('over 30 minutes produces a DraftKings price >2% better than the Pinnacle no-vig price, and mostly non-edges', () => {
    const f = feed(42);
    let edges = 0;
    let prices = 0;
    let best = -1;
    let scoreChanged = false;
    let altLines = 0;
    const firstScore = new Map<string, string>();
    for (let at = T0; at <= T0 + 30 * MIN; at += TICK) {
      for (const snap of f.tick(at)) {
        for (const ev of dkEdges(snap)) {
          prices++;
          if (ev > 0.02) edges++;
          best = Math.max(best, ev);
        }
        altLines += snap.quotes.filter((q) => !q.isMainLine && q.book === 'draftkings').length;
        for (const e of snap.events) {
          if (!e.isLive || !e.score) continue;
          const s = `${e.score.home}-${e.score.away}`;
          const prev = firstScore.get(e.sourceEventId);
          if (prev === undefined) firstScore.set(e.sourceEventId, s);
          else if (prev !== s) scoreChanged = true;
        }
      }
    }
    expect(best).toBeGreaterThan(0.02);
    expect(edges).toBeGreaterThan(0);
    // Plenty of non-edges: the vast majority of DraftKings prices are not +2% EV.
    expect(edges / prices).toBeLessThan(0.05);
    expect(altLines).toBeGreaterThan(0);
    expect(scoreChanged).toBe(true);
  });

  it('regularly leaves DraftKings stale after a Pinnacle move and produces occasional DK vs other-book arbs', () => {
    const f = feed(42);
    // Pinnacle implied-probability history per (event, market group, side).
    const history = new Map<string, Array<{ t: number; p: number }>>();
    let staleTicks = 0;
    let arbTicks = 0;
    let ticks = 0;
    for (let at = T0; at <= T0 + 60 * MIN; at += TICK) {
      ticks++;
      let stale = false;
      let arb = false;
      for (const snap of f.tick(at)) {
        for (const [eventId, groups] of index(snap)) {
          for (const [gk, books] of groups) {
            const pin = books.get('pinnacle');
            const dk = books.get('draftkings');
            if (!dk) continue;
            for (const [side, q] of dk) {
              if (q.suspended) continue;
              const ref = pin?.get(side);
              if (ref) {
                const key = `${eventId}|${gk}|${side}`;
                const h = history.get(key) ?? [];
                h.push({ t: at, p: 1 / ref.decimal });
                while (h.length > 0 && h[0].t < at - 120_000) h.shift();
                history.set(key, h);
                const moved = 1 / ref.decimal - h[0].p;
                if (moved >= 0.02 && (q.bookUpdatedAt as number) <= (ref.bookUpdatedAt as number) - 5_000) stale = true;
              }
              // DK side + best other side at a book that is neither DraftKings nor Pinnacle.
              let sum = 1 / q.decimal;
              let complete = true;
              for (const other of dk.keys()) {
                if (other === side) continue;
                let best = 0;
                for (const [book, quotes] of books) {
                  if (book === 'draftkings' || book === 'pinnacle') continue;
                  const o = quotes.get(other);
                  if (o && !o.suspended) best = Math.max(best, o.decimal);
                }
                if (best === 0) complete = false;
                else sum += 1 / best;
              }
              if (complete && sum < 1 / 1.005) arb = true;
            }
          }
        }
      }
      if (stale) staleTicks++;
      if (arb) arbTicks++;
    }
    expect(staleTicks).toBeGreaterThan(0);
    expect(arbTicks).toBeGreaterThan(0);
    // Arbs are occasional, not constant.
    expect(arbTicks / ticks).toBeLessThan(0.6);
  });

  it('quotes alternate spread lines (isMainLine false) for DraftKings and Pinnacle on some events', () => {
    const snaps = feed().tick(T0);
    const alt = snaps.flatMap((s) => s.quotes).filter((q) => !q.isMainLine);
    expect(alt.length).toBeGreaterThan(0);
    expect(alt.every((q) => q.kind === 'spread')).toBe(true);
    expect(new Set(alt.map((q) => q.book))).toEqual(new Set(['draftkings', 'pinnacle']));
    const altEvents = new Set(alt.map((q) => q.sourceEventId));
    expect(altEvents.size).toBeGreaterThanOrEqual(2);
    const main = snaps.flatMap((s) => s.quotes).filter((q) => q.isMainLine && q.book === 'draftkings');
    for (const q of alt.filter((x) => x.book === 'draftkings')) {
      const m = main.find((x) => x.sourceEventId === q.sourceEventId && x.kind === 'spread' && x.side === q.side);
      expect(m?.line).not.toBe(q.line);
    }
  });

  it('rolls finished live games into new pre-match games', () => {
    const f = feed(5);
    const first = f.tick(T0);
    const initialLive = new Set(first.flatMap((s) => s.events.filter((e) => e.isLive).map((e) => e.sourceEventId)));
    const counts = first.map((s) => s.events.length);
    const seen = new Set(first.flatMap((s) => s.events.map((e) => e.sourceEventId)));
    const newIds: string[] = [];
    let last = first;
    for (let at = T0 + 10_000; at <= T0 + 4 * HOUR; at += 10_000) {
      last = f.tick(at);
      expect(last.map((s) => s.events.length)).toEqual(counts);
      for (const e of last.flatMap((s) => s.events)) {
        if (seen.has(e.sourceEventId)) continue;
        seen.add(e.sourceEventId);
        newIds.push(e.sourceEventId);
        // A new game is announced pre-match.
        expect(e.isLive).toBe(false);
        expect(e.startTime).toBeGreaterThan(at);
      }
    }
    const current = new Set(last.flatMap((s) => s.events.map((e) => e.sourceEventId)));
    // Every game that was live at the start has finished within 4 hours.
    for (const id of initialLive) expect(current.has(id)).toBe(false);
    expect(newIds.length).toBeGreaterThanOrEqual(initialLive.size);
  });

  it('keeps event and quote counts bounded over 24 simulated hours', () => {
    const f = feed(11);
    let maxEvents = 0;
    let maxQuotes = 0;
    let liveSeen = 0;
    for (let at = T0; at <= T0 + 24 * HOUR; at += 30_000) {
      const snaps = f.tick(at);
      const events = snaps.flatMap((s) => s.events);
      const quotes = snaps.reduce((n, s) => n + s.quotes.length, 0);
      expect(new Set(events.map((e) => `${e.league}:${e.sourceEventId}`)).size).toBe(events.length);
      maxEvents = Math.max(maxEvents, events.length);
      maxQuotes = Math.max(maxQuotes, quotes);
      liveSeen = Math.max(liveSeen, events.filter((e) => e.isLive).length);
    }
    expect(maxEvents).toBe(14);
    // 14 events x 5 books x (3 + 2 + 2) sides + alternate lines stays well under 600.
    expect(maxQuotes).toBeLessThanOrEqual(600);
    expect(liveSeen).toBeGreaterThan(0);
  });

  it('uses only the demo leagues present, falling back to the first five leagues', () => {
    const nba = DEFAULT_LEAGUES.find((l) => l.key === 'NBA') as LeagueDef;
    const ncaaf = DEFAULT_LEAGUES.find((l) => l.key === 'NCAAF') as LeagueDef;
    const ucl = DEFAULT_LEAGUES.find((l) => l.key === 'UCL') as LeagueDef;
    expect(feed(1, [ncaaf, nba]).tick(T0).map((s) => s.league)).toEqual(['NBA']);

    const fallback = feed(1, [ncaaf, ucl]).tick(T0);
    expect(fallback.map((s) => s.league)).toEqual(['NCAAF', 'UCL']);
    const uclDraws = fallback[1].quotes.filter((q) => q.side === 'draw');
    expect(uclDraws.length).toBeGreaterThan(0);

    expect(feed(1, []).tick(T0)).toEqual([]);
  });

  it('picks 2-3 other books from the configured list, avoiding sharp books', () => {
    expect(feed(1, DEFAULT_LEAGUES, DEFAULT_ODDS_API_BOOKS).tick(T0)[0].books).toEqual([
      'pinnacle',
      'draftkings',
      'fanduel',
      'betmgm',
      'williamhill_us',
    ]);
    expect(feed(1, DEFAULT_LEAGUES, ['draftkings']).tick(T0)[0].books).toEqual([
      'pinnacle',
      'draftkings',
      'fanduel',
      'betmgm',
    ]);
    expect(feed(1, DEFAULT_LEAGUES, ['DraftKings', 'bovada', 'lowvig']).tick(T0)[0].books).toEqual([
      'pinnacle',
      'draftkings',
      'bovada',
      'lowvig',
    ]);
  });

  it('survives odd clocks: non-finite times, repeated and backwards ticks', () => {
    const f = feed();
    expect(f.tick(Number.NaN)).toEqual([]);
    const a = f.tick(T0);
    expect(f.tick(T0).map((s) => s.events)).toEqual(a.map((s) => s.events));
    const back = f.tick(T0 - 60_000);
    expect(back.flatMap((s) => s.events).length).toBe(14);
    expect(back.every((s) => s.quotes.every((q) => q.decimal > 1))).toBe(true);
  });
});

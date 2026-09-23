import { describe, expect, it } from 'vitest';
import { MarketStore } from '../src/engine/marketStore';
import type { StoredQuote } from '../src/engine/marketStore';
import type { MarketKind, Quote, QuoteSource, RawEvent, Side, SourceSnapshot } from '../src/types';

const T0 = 1_700_000_000_000;
const MIN = 60_000;
const HOUR = 60 * MIN;

function rawEvent(id: string, over: Partial<RawEvent> = {}): RawEvent {
  return {
    source: 'odds-api',
    sourceEventId: id,
    league: 'NBA',
    home: 'Celtics',
    away: 'Knicks',
    startTime: T0 + 2 * HOUR,
    isLive: false,
    ...over,
  };
}

function quote(
  sourceEventId: string,
  book: string,
  kind: MarketKind,
  side: Side,
  line: number | null,
  decimal: number,
  over: Partial<Quote> = {},
): Quote {
  return {
    book,
    source: 'odds-api',
    sourceEventId,
    kind,
    side,
    line,
    decimal,
    suspended: false,
    isMainLine: true,
    observedAt: T0,
    bookUpdatedAt: null,
    ...over,
  };
}

function snapshot(over: Partial<SourceSnapshot> = {}): SourceSnapshot {
  return {
    source: 'odds-api',
    league: 'NBA',
    fetchedAt: T0,
    events: [],
    quotes: [],
    complete: false,
    books: ['pinnacle', 'draftkings'],
    ...over,
  };
}

/** Moneyline for both books on one event. */
function moneylineQuotes(id: string, at: number, source: QuoteSource = 'odds-api'): Quote[] {
  return [
    quote(id, 'pinnacle', 'moneyline', 'home', null, 1.8, { observedAt: at, source }),
    quote(id, 'pinnacle', 'moneyline', 'away', null, 2.1, { observedAt: at, source }),
    quote(id, 'draftkings', 'moneyline', 'home', null, 1.77, { observedAt: at, source }),
    quote(id, 'draftkings', 'moneyline', 'away', null, 2.05, { observedAt: at, source }),
  ];
}

function mlKey(eventId: string, book: string, side: Side): string {
  return MarketStore.quoteKey(eventId, { book, kind: 'moneyline', side, line: null });
}

/** Every quote reachable through the event index must be in the global index and vice versa. */
function expectIndexesConsistent(store: MarketStore): void {
  let viaEvents = 0;
  for (const ev of store.events()) {
    for (const q of store.quotesForEvent(ev.id)) {
      expect(q.eventId).toBe(ev.id);
      expect(store.getQuote(q.key)).toBe(q);
      viaEvents++;
    }
  }
  expect(viaEvents).toBe(store.stats().quotes);
}

describe('MarketStore ids', () => {
  it('builds canonical event ids and quote keys', () => {
    expect(MarketStore.eventId('odds-api', 'abc')).toBe('odds-api:abc');
    expect(MarketStore.eventId('demo', 'nba-1')).toBe('demo:nba-1');
    expect(MarketStore.quoteKey('odds-api:abc', { book: 'pinnacle', kind: 'moneyline', side: 'home', line: null })).toBe(
      'odds-api:abc|pinnacle|moneyline|home|',
    );
    expect(MarketStore.quoteKey('odds-api:abc', { book: 'draftkings', kind: 'spread', side: 'away', line: 3.5 })).toBe(
      'odds-api:abc|draftkings|spread|away|3.5',
    );
    expect(MarketStore.quoteKey('e', { book: 'b', kind: 'total', side: 'over', line: 224.5 })).toBe('e|b|total|over|224.5');
  });
});

describe('MarketStore.ingest upsert', () => {
  it('creates canonical events and stored quotes', () => {
    const store = new MarketStore();
    const res = store.ingest(
      snapshot({
        events: [rawEvent('e1', { isLive: true, score: { home: 10, away: 8, period: 'Q1', updatedAt: T0 } })],
        quotes: [
          quote('e1', 'pinnacle', 'moneyline', 'home', null, 1.8, { bookUpdatedAt: T0 - 5000, link: 'https://x/1' }),
          quote('e1', 'pinnacle', 'moneyline', 'away', null, 2.1),
          quote('e1', 'draftkings', 'spread', 'home', -3.5, 1.91),
        ],
      }),
    );
    expect(res).toMatchObject({ eventsUpserted: 1, quotesUpserted: 3, quotesRemoved: 0, priceChanges: 0, quotesRejected: 0 });

    const ev = store.getEvent('odds-api:e1');
    expect(ev).toMatchObject({
      id: 'odds-api:e1',
      league: 'NBA',
      home: 'Celtics',
      away: 'Knicks',
      startTime: T0 + 2 * HOUR,
      isLive: true,
      links: { 'odds-api': 'e1' },
      lastSeen: T0,
    });
    expect(ev?.score).toEqual({ home: 10, away: 8, period: 'Q1', updatedAt: T0 });
    expect(store.leagueOf('odds-api:e1')).toBe('NBA');

    const home = store.getQuote(mlKey('odds-api:e1', 'pinnacle', 'home'));
    expect(home).toBeDefined();
    expect(home).toMatchObject({
      eventId: 'odds-api:e1',
      key: 'odds-api:e1|pinnacle|moneyline|home|',
      decimal: 1.8,
      bookUpdatedAt: T0 - 5000,
      lastChangeAt: T0 - 5000,
      link: 'https://x/1',
    });
    expect(home?.history).toEqual([{ t: T0 - 5000, decimal: 1.8 }]);

    // No bookUpdatedAt -> observedAt is the change time.
    const away = store.getQuote(mlKey('odds-api:e1', 'pinnacle', 'away'));
    expect(away?.lastChangeAt).toBe(T0);
    expect(away?.history).toEqual([{ t: T0, decimal: 2.1 }]);

    expect(store.quotesForEvent('odds-api:e1')).toHaveLength(3);
    expect(store.quotesForEvent('odds-api:missing')).toEqual([]);
    expect(store.getEvent('odds-api:missing')).toBeUndefined();
    expect(store.leagueOf('odds-api:missing')).toBeUndefined();
  });

  it('updates event fields on later snapshots', () => {
    const store = new MarketStore();
    store.ingest(snapshot({ events: [rawEvent('e1', { score: { home: 1, away: 0, updatedAt: T0 } })] }));
    store.ingest(
      snapshot({
        fetchedAt: T0 + 30_000,
        events: [rawEvent('e1', { isLive: true, startTime: T0 + HOUR, home: 'Boston Celtics' })],
      }),
    );
    const ev = store.getEvent('odds-api:e1');
    expect(ev).toMatchObject({ isLive: true, startTime: T0 + HOUR, home: 'Boston Celtics', lastSeen: T0 + 30_000 });
    expect(ev?.score).toBeUndefined();
    expect(store.events()).toHaveLength(1);
  });

  it('appends history only when the price changes', () => {
    const store = new MarketStore();
    const key = mlKey('odds-api:e1', 'pinnacle', 'home');
    store.ingest(snapshot({ events: [rawEvent('e1')], quotes: [quote('e1', 'pinnacle', 'moneyline', 'home', null, 1.8)] }));

    // Same price (float noise below 4 dp) -> only freshness fields refresh.
    const r1 = store.ingest(
      snapshot({
        fetchedAt: T0 + 10_000,
        events: [rawEvent('e1')],
        quotes: [
          quote('e1', 'pinnacle', 'moneyline', 'home', null, 1.800001, {
            observedAt: T0 + 10_000,
            bookUpdatedAt: T0 + 9_000,
            suspended: true,
            link: 'https://new',
          }),
        ],
      }),
    );
    expect(r1.priceChanges).toBe(0);
    expect(r1.quotesUpserted).toBe(1);
    let q = store.getQuote(key) as StoredQuote;
    expect(q.history).toEqual([{ t: T0, decimal: 1.8 }]);
    expect(q.decimal).toBe(1.8);
    expect(q.lastChangeAt).toBe(T0);
    expect(q.observedAt).toBe(T0 + 10_000);
    expect(q.bookUpdatedAt).toBe(T0 + 9_000);
    expect(q.suspended).toBe(true);
    expect(q.link).toBe('https://new');

    // Real change -> new point at bookUpdatedAt ?? observedAt.
    const r2 = store.ingest(
      snapshot({
        fetchedAt: T0 + 20_000,
        events: [rawEvent('e1')],
        quotes: [quote('e1', 'pinnacle', 'moneyline', 'home', null, 1.75, { observedAt: T0 + 20_000, bookUpdatedAt: T0 + 18_000 })],
      }),
    );
    expect(r2.priceChanges).toBe(1);
    q = store.getQuote(key) as StoredQuote;
    expect(q.decimal).toBe(1.75);
    expect(q.lastChangeAt).toBe(T0 + 18_000);
    expect(q.suspended).toBe(false);
    expect(q.link).toBeUndefined();
    expect(q.history).toEqual([
      { t: T0, decimal: 1.8 },
      { t: T0 + 18_000, decimal: 1.75 },
    ]);

    const r3 = store.ingest(
      snapshot({
        fetchedAt: T0 + 30_000,
        events: [rawEvent('e1')],
        quotes: [quote('e1', 'pinnacle', 'moneyline', 'home', null, 1.7, { observedAt: T0 + 30_000 })],
      }),
    );
    expect(r3.priceChanges).toBe(1);
    q = store.getQuote(key) as StoredQuote;
    expect(q.lastChangeAt).toBe(T0 + 30_000);
    expect(q.history.map((p) => p.decimal)).toEqual([1.8, 1.75, 1.7]);
  });

  it('keeps history chronological when a source reports an older update time', () => {
    const store = new MarketStore();
    store.ingest(
      snapshot({
        events: [rawEvent('e1')],
        quotes: [quote('e1', 'pinnacle', 'moneyline', 'home', null, 1.8, { bookUpdatedAt: T0 })],
      }),
    );
    store.ingest(
      snapshot({
        fetchedAt: T0 + 5000,
        events: [rawEvent('e1')],
        quotes: [quote('e1', 'pinnacle', 'moneyline', 'home', null, 1.9, { observedAt: T0 + 5000, bookUpdatedAt: T0 - 60_000 })],
      }),
    );
    const q = store.getQuote(mlKey('odds-api:e1', 'pinnacle', 'home')) as StoredQuote;
    expect(q.history).toEqual([
      { t: T0, decimal: 1.8 },
      { t: T0, decimal: 1.9 },
    ]);
    expect(store.priceAt(q, T0)).toBe(1.9);
  });
});

describe('MarketStore history trimming', () => {
  function priceSeries(store: MarketStore, points: Array<[number, number]>): StoredQuote {
    for (const [t, d] of points) {
      store.ingest(
        snapshot({
          fetchedAt: t,
          events: [rawEvent('e1', { startTime: T0 + 5 * HOUR })],
          quotes: [quote('e1', 'pinnacle', 'moneyline', 'home', null, d, { observedAt: t })],
        }),
      );
    }
    return store.getQuote(mlKey('odds-api:e1', 'pinnacle', 'home')) as StoredQuote;
  }

  it('trims by max points, keeping the most recent ones', () => {
    const store = new MarketStore({ historyMaxPoints: 3 });
    const q = priceSeries(store, [
      [T0, 1.5],
      [T0 + 1000, 1.6],
      [T0 + 2000, 1.7],
      [T0 + 3000, 1.8],
      [T0 + 4000, 1.9],
    ]);
    expect(q.history).toEqual([
      { t: T0 + 2000, decimal: 1.7 },
      { t: T0 + 3000, decimal: 1.8 },
      { t: T0 + 4000, decimal: 1.9 },
    ]);
  });

  it('trims by age but keeps the price in effect at the cutoff and always the latest point', () => {
    const store = new MarketStore({ historyMaxAgeMs: 10 * MIN, eventRetentionMs: 10 * HOUR });
    const q = priceSeries(store, [
      [T0, 1.5],
      [T0 + 1 * MIN, 1.6],
      [T0 + 20 * MIN, 1.7],
      [T0 + 25 * MIN, 1.8],
    ]);
    // cutoff = T0+15m: T0 is dropped; T0+1m is kept because it is still the price at the cutoff.
    expect(q.history).toEqual([
      { t: T0 + 1 * MIN, decimal: 1.6 },
      { t: T0 + 20 * MIN, decimal: 1.7 },
      { t: T0 + 25 * MIN, decimal: 1.8 },
    ]);
    expect(store.priceAt(q, T0 + 15 * MIN)).toBe(1.6);

    // Much later every point is older than the window: only the latest survives.
    store.prune(T0 + 2 * HOUR);
    expect(q.history).toEqual([{ t: T0 + 25 * MIN, decimal: 1.8 }]);
    expect(store.priceAt(q, T0 + 2 * HOUR)).toBe(1.8);
  });
});

describe('MarketStore.priceAt', () => {
  it('returns the last known price at or before t', () => {
    const store = new MarketStore();
    const q: StoredQuote = {
      ...quote('e1', 'pinnacle', 'moneyline', 'home', null, 1.9),
      eventId: 'odds-api:e1',
      key: 'k',
      lastChangeAt: T0 + 20_000,
      history: [
        { t: T0, decimal: 2.0 },
        { t: T0 + 10_000, decimal: 1.95 },
        { t: T0 + 20_000, decimal: 1.9 },
      ],
    };
    expect(store.priceAt(q, T0 - 1)).toBeNull();
    expect(store.priceAt(q, T0)).toBe(2.0);
    expect(store.priceAt(q, T0 + 9_999)).toBe(2.0);
    expect(store.priceAt(q, T0 + 10_000)).toBe(1.95);
    expect(store.priceAt(q, T0 + 15_000)).toBe(1.95);
    expect(store.priceAt(q, T0 + 99_000)).toBe(1.9);
    expect(store.priceAt(q, Number.NaN)).toBeNull();
    expect(store.priceAt({ ...q, history: [] }, T0)).toBeNull();
  });
});

describe('MarketStore complete snapshots', () => {
  it('removes missing quotes only within the same source + league', () => {
    const store = new MarketStore();
    store.ingest(snapshot({ events: [rawEvent('nba1')], quotes: moneylineQuotes('nba1', T0) }));
    store.ingest(
      snapshot({
        league: 'NFL',
        events: [rawEvent('nfl1', { league: 'NFL' })],
        quotes: moneylineQuotes('nfl1', T0),
      }),
    );
    store.ingest(
      snapshot({
        source: 'demo',
        events: [rawEvent('d1', { source: 'demo' })],
        quotes: moneylineQuotes('d1', T0, 'demo'),
      }),
    );
    expect(store.stats().quotes).toBe(12);

    // Complete NBA odds-api snapshot with no quotes at all for nba1's DK home price.
    const res = store.ingest(
      snapshot({
        fetchedAt: T0 + 1000,
        complete: true,
        events: [rawEvent('nba1')],
        quotes: moneylineQuotes('nba1', T0 + 1000).filter((q) => !(q.book === 'draftkings' && q.side === 'home')),
      }),
    );
    expect(res.quotesRemoved).toBe(1);
    expect(store.getQuote(mlKey('odds-api:nba1', 'draftkings', 'home'))).toBeUndefined();
    expect(store.quotesForEvent('odds-api:nba1')).toHaveLength(3);
    expect(store.quotesForEvent('odds-api:nfl1')).toHaveLength(4);
    expect(store.quotesForEvent('demo:d1')).toHaveLength(4);
    expectIndexesConsistent(store);
  });

  it('only removes quotes of the books the snapshot is authoritative for', () => {
    const store = new MarketStore();
    store.ingest(snapshot({ events: [rawEvent('e1')], quotes: moneylineQuotes('e1', T0) }));
    const res = store.ingest(
      snapshot({
        fetchedAt: T0 + 1000,
        complete: true,
        books: ['pinnacle'],
        events: [rawEvent('e1')],
        quotes: [quote('e1', 'pinnacle', 'moneyline', 'home', null, 1.8, { observedAt: T0 + 1000 })],
      }),
    );
    expect(res.quotesRemoved).toBe(1);
    expect(store.getQuote(mlKey('odds-api:e1', 'pinnacle', 'away'))).toBeUndefined();
    expect(store.getQuote(mlKey('odds-api:e1', 'pinnacle', 'home'))).toBeDefined();
    expect(store.getQuote(mlKey('odds-api:e1', 'draftkings', 'home'))).toBeDefined();
    expect(store.getQuote(mlKey('odds-api:e1', 'draftkings', 'away'))).toBeDefined();
    expectIndexesConsistent(store);
  });

  it('matches authoritative books case-insensitively', () => {
    const store = new MarketStore();
    store.ingest(snapshot({ events: [rawEvent('e1')], quotes: moneylineQuotes('e1', T0) }));
    store.ingest(snapshot({ complete: true, books: ['DraftKings'], events: [rawEvent('e1')], quotes: [] }));
    expect(store.quotesForEvent('odds-api:e1').map((q) => q.book)).toEqual(['pinnacle', 'pinnacle']);
  });

  it('events missing from a complete snapshot lose their quotes and go not-live, then prune removes them', () => {
    const store = new MarketStore({ eventRetentionMs: 20 * MIN });
    store.ingest(
      snapshot({
        events: [rawEvent('e1', { isLive: true }), rawEvent('e2', { isLive: true })],
        quotes: [...moneylineQuotes('e1', T0), ...moneylineQuotes('e2', T0)],
      }),
    );
    expect(store.stats()).toEqual({ events: 2, liveEvents: 2, quotes: 8 });

    const res = store.ingest(
      snapshot({
        fetchedAt: T0 + 60_000,
        complete: true,
        events: [rawEvent('e2', { isLive: true })],
        quotes: moneylineQuotes('e2', T0 + 60_000),
      }),
    );
    expect(res.quotesRemoved).toBe(4);
    const e1 = store.getEvent('odds-api:e1');
    expect(e1).toBeDefined();
    expect(e1?.isLive).toBe(false);
    expect(e1?.lastSeen).toBe(T0);
    expect(store.quotesForEvent('odds-api:e1')).toEqual([]);
    expect(store.stats()).toEqual({ events: 2, liveEvents: 1, quotes: 4 });

    const pruned = store.prune(T0 + 20 * MIN + 1);
    expect(pruned).toEqual({ eventsRemoved: 1, quotesRemoved: 0 });
    expect(store.getEvent('odds-api:e1')).toBeUndefined();
    expect(store.getEvent('odds-api:e2')).toBeDefined();
    expectIndexesConsistent(store);
  });

  it('a complete snapshot for another league does not touch live flags elsewhere', () => {
    const store = new MarketStore();
    store.ingest(snapshot({ events: [rawEvent('e1', { isLive: true })], quotes: moneylineQuotes('e1', T0) }));
    store.ingest(snapshot({ league: 'NFL', complete: true, events: [], quotes: [] }));
    expect(store.getEvent('odds-api:e1')?.isLive).toBe(true);
    expect(store.stats().quotes).toBe(4);
  });

  it('non-complete snapshots never delete', () => {
    const store = new MarketStore();
    store.ingest(snapshot({ events: [rawEvent('e1', { isLive: true })], quotes: moneylineQuotes('e1', T0) }));
    const res = store.ingest(snapshot({ fetchedAt: T0 + 1000, complete: false, events: [], quotes: [] }));
    expect(res.quotesRemoved).toBe(0);
    expect(store.stats()).toEqual({ events: 1, liveEvents: 1, quotes: 4 });
  });
});

describe('MarketStore rejection', () => {
  it('rejects quotes whose event is not in the snapshot', () => {
    const store = new MarketStore();
    store.ingest(snapshot({ events: [rawEvent('e1')], quotes: moneylineQuotes('e1', T0) }));
    // e1 is known to the store but not part of this snapshot: its quotes must be skipped too.
    const res = store.ingest(
      snapshot({
        fetchedAt: T0 + 1000,
        events: [rawEvent('e2')],
        quotes: [
          quote('ghost', 'pinnacle', 'moneyline', 'home', null, 1.5),
          quote('e1', 'pinnacle', 'moneyline', 'home', null, 1.5, { observedAt: T0 + 1000 }),
          quote('e2', 'pinnacle', 'moneyline', 'home', null, 1.5),
        ],
      }),
    );
    expect(res.quotesRejected).toBe(2);
    expect(res.quotesUpserted).toBe(1);
    expect(store.getEvent('odds-api:ghost')).toBeUndefined();
    expect(store.getQuote(mlKey('odds-api:ghost', 'pinnacle', 'home'))).toBeUndefined();
    expect(store.getQuote(mlKey('odds-api:e1', 'pinnacle', 'home'))?.decimal).toBe(1.8);
    expect(store.getQuote(mlKey('odds-api:e2', 'pinnacle', 'home'))?.decimal).toBe(1.5);
  });

  it('rejects malformed quotes and events', () => {
    const store = new MarketStore();
    const res = store.ingest(
      snapshot({
        events: [
          rawEvent('e1'),
          rawEvent('bad-time', { startTime: Number.NaN }),
          rawEvent('other-league', { league: 'NFL' }),
          rawEvent('other-source', { source: 'demo' }),
        ],
        quotes: [
          quote('e1', 'pinnacle', 'moneyline', 'home', null, 1.0),
          quote('e1', 'pinnacle', 'moneyline', 'home', null, Number.POSITIVE_INFINITY),
          quote('e1', 'pinnacle', 'moneyline', 'home', 1.5, 1.8),
          quote('e1', 'pinnacle', 'spread', 'home', null, 1.9),
          quote('e1', 'pinnacle', 'spread', 'over', 3.5, 1.9),
          quote('e1', 'pinnacle', 'total', 'home', 220.5, 1.9),
          quote('e1', 'pinnacle', 'total', 'over', 220.5, 1.9, { source: 'demo' }),
          quote('e1', '', 'moneyline', 'home', null, 1.8),
          quote('e1', 'pinnacle', 'total', 'over', 220.5, 1.9),
          quote('bad-time', 'pinnacle', 'moneyline', 'home', null, 1.8),
          quote('other-league', 'pinnacle', 'moneyline', 'home', null, 1.8),
          quote('other-source', 'pinnacle', 'moneyline', 'home', null, 1.8),
        ],
      }),
    );
    expect(res.eventsUpserted).toBe(1);
    expect(res.quotesUpserted).toBe(1);
    expect(res.quotesRejected).toBe(11);
    expect(store.stats()).toEqual({ events: 1, liveEvents: 0, quotes: 1 });
  });

  it('ignores a malformed snapshot without throwing', () => {
    const store = new MarketStore();
    const bad = { source: 'odds-api', league: 'NBA', fetchedAt: Number.NaN, events: [], quotes: [] } as unknown as SourceSnapshot;
    expect(store.ingest(bad)).toMatchObject({ eventsUpserted: 0, quotesUpserted: 0, quotesRemoved: 0, priceChanges: 0 });
    expect(store.ingest(null as unknown as SourceSnapshot).eventsUpserted).toBe(0);
  });
});

describe('MarketStore.prune', () => {
  it('removes events not seen within the retention window', () => {
    const store = new MarketStore({ eventRetentionMs: 20 * MIN });
    store.ingest(snapshot({ events: [rawEvent('old')], quotes: moneylineQuotes('old', T0) }));
    store.ingest(
      snapshot({ fetchedAt: T0 + 15 * MIN, events: [rawEvent('fresh')], quotes: moneylineQuotes('fresh', T0 + 15 * MIN) }),
    );
    expect(store.prune(T0 + 20 * MIN)).toEqual({ eventsRemoved: 0, quotesRemoved: 0 });
    const res = store.prune(T0 + 20 * MIN + 1);
    expect(res).toEqual({ eventsRemoved: 1, quotesRemoved: 4 });
    expect(store.getEvent('odds-api:old')).toBeUndefined();
    expect(store.getQuote(mlKey('odds-api:old', 'pinnacle', 'home'))).toBeUndefined();
    expect(store.quotesForEvent('odds-api:old')).toEqual([]);
    expect(store.leagueOf('odds-api:old')).toBeUndefined();
    expect(store.stats()).toEqual({ events: 1, liveEvents: 0, quotes: 4 });
    expectIndexesConsistent(store);
  });

  it('removes events that started more than 12 hours ago even if still reported', () => {
    const store = new MarketStore({ eventRetentionMs: 20 * MIN });
    const now = T0 + 24 * HOUR;
    store.ingest(
      snapshot({
        fetchedAt: now,
        events: [
          rawEvent('ancient', { startTime: now - 12 * HOUR - 1, isLive: true }),
          rawEvent('recent', { startTime: now - 12 * HOUR + 1, isLive: true }),
        ],
        quotes: [...moneylineQuotes('ancient', now), ...moneylineQuotes('recent', now)],
      }),
    );
    expect(store.prune(now)).toEqual({ eventsRemoved: 1, quotesRemoved: 4 });
    expect(store.getEvent('odds-api:ancient')).toBeUndefined();
    expect(store.getEvent('odds-api:recent')).toBeDefined();
    expectIndexesConsistent(store);
  });

  it('enforces maxEvents by dropping the oldest lastSeen first', () => {
    const store = new MarketStore({ maxEvents: 3, eventRetentionMs: 10 * HOUR });
    for (let i = 0; i < 3; i++) {
      store.ingest(
        snapshot({ fetchedAt: T0 + i * 1000, events: [rawEvent(`e${i}`)], quotes: moneylineQuotes(`e${i}`, T0 + i * 1000) }),
      );
    }
    expect(store.stats().events).toBe(3);

    // Ingest itself keeps the store within the cap.
    const res = store.ingest(
      snapshot({
        fetchedAt: T0 + 10_000,
        events: [rawEvent('e3'), rawEvent('e4')],
        quotes: [...moneylineQuotes('e3', T0 + 10_000), ...moneylineQuotes('e4', T0 + 10_000)],
      }),
    );
    expect(res.eventsRemoved).toBe(2);
    expect(res.quotesRemoved).toBe(8);
    expect(
      store
        .events()
        .map((e) => e.id)
        .sort(),
    ).toEqual(['odds-api:e2', 'odds-api:e3', 'odds-api:e4']);
    expect(store.prune(T0 + 10_000)).toEqual({ eventsRemoved: 0, quotesRemoved: 0 });
    expectIndexesConsistent(store);
  });

  it('keeps indexes consistent through complete snapshots, prune and re-ingest', () => {
    const store = new MarketStore({ eventRetentionMs: 5 * MIN });
    store.ingest(
      snapshot({
        events: [rawEvent('a'), rawEvent('b')],
        quotes: [
          ...moneylineQuotes('a', T0),
          ...moneylineQuotes('b', T0),
          quote('a', 'pinnacle', 'spread', 'home', -3.5, 1.9),
          quote('a', 'pinnacle', 'spread', 'away', 3.5, 1.95),
        ],
      }),
    );
    expectIndexesConsistent(store);
    store.ingest(snapshot({ fetchedAt: T0 + MIN, complete: true, events: [rawEvent('a')], quotes: moneylineQuotes('a', T0 + MIN) }));
    expect(store.quotesForEvent('odds-api:a')).toHaveLength(4);
    expect(store.quotesForEvent('odds-api:b')).toHaveLength(0);
    expectIndexesConsistent(store);

    store.prune(T0 + 5 * MIN + 1);
    expect(store.getEvent('odds-api:b')).toBeUndefined();
    expect(store.getEvent('odds-api:a')).toBeDefined();
    expectIndexesConsistent(store);

    // A pruned event can come back cleanly with fresh history.
    store.ingest(snapshot({ fetchedAt: T0 + 6 * MIN, events: [rawEvent('b')], quotes: moneylineQuotes('b', T0 + 6 * MIN) }));
    const back = store.getQuote(mlKey('odds-api:b', 'pinnacle', 'home'));
    expect(back?.history).toEqual([{ t: T0 + 6 * MIN, decimal: 1.8 }]);
    expect(store.getEvent('odds-api:b')?.lastSeen).toBe(T0 + 6 * MIN);
    expectIndexesConsistent(store);

    // Complete snapshot scope still tracks the re-added event.
    store.ingest(snapshot({ fetchedAt: T0 + 7 * MIN, complete: true, events: [rawEvent('a')], quotes: moneylineQuotes('a', T0 + 7 * MIN) }));
    expect(store.quotesForEvent('odds-api:b')).toHaveLength(0);
    expectIndexesConsistent(store);
  });

  it('caps quotes per event, evicting the least recently observed', () => {
    const store = new MarketStore({ maxQuotesPerEvent: 3 });
    store.ingest(
      snapshot({
        events: [rawEvent('e1')],
        quotes: [
          quote('e1', 'pinnacle', 'total', 'over', 220.5, 1.9, { observedAt: T0 - 3000 }),
          quote('e1', 'pinnacle', 'total', 'under', 220.5, 1.9, { observedAt: T0 - 1000 }),
          quote('e1', 'pinnacle', 'total', 'over', 221.5, 1.9, { observedAt: T0 - 2000 }),
          quote('e1', 'pinnacle', 'total', 'under', 221.5, 1.9, { observedAt: T0 }),
        ],
      }),
    );
    const lines = store.quotesForEvent('odds-api:e1').map((q) => `${q.side}${q.line}`);
    expect(lines.sort()).toEqual(['over221.5', 'under220.5', 'under221.5']);
    expectIndexesConsistent(store);
  });
});

describe('MarketStore.stats', () => {
  it('counts events, live events and quotes', () => {
    const store = new MarketStore();
    expect(store.stats()).toEqual({ events: 0, liveEvents: 0, quotes: 0 });
    store.ingest(
      snapshot({
        events: [rawEvent('a', { isLive: true }), rawEvent('b'), rawEvent('c', { isLive: true })],
        quotes: [...moneylineQuotes('a', T0), ...moneylineQuotes('b', T0)],
      }),
    );
    expect(store.stats()).toEqual({ events: 3, liveEvents: 2, quotes: 8 });
    expect(store.events().map((e) => e.id)).toEqual(['odds-api:a', 'odds-api:b', 'odds-api:c']);
  });
});

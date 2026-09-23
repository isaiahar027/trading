import { describe, expect, it } from 'vitest';
import { devig } from '../src/engine/devig';
import { fairForGroup, fairProbForPick, groupKey, groupQuotes, requiredSides } from '../src/engine/fairPrice';
import type { FairOptions, MarketGroup } from '../src/engine/fairPrice';
import type { StoredQuote } from '../src/engine/marketStore';
import type { MarketKind, Side } from '../src/types';

const NOW = 1_700_000_000_000;
const EVENT = 'odds-api:e1';

function sq(
  book: string,
  kind: MarketKind,
  side: Side,
  line: number | null,
  decimal: number,
  over: Partial<StoredQuote> = {},
): StoredQuote {
  const observedAt = over.observedAt ?? NOW - 1000;
  const lastChangeAt = over.lastChangeAt ?? NOW - 60_000;
  return {
    book,
    source: 'odds-api',
    sourceEventId: 'e1',
    kind,
    side,
    line,
    decimal,
    suspended: false,
    isMainLine: true,
    bookUpdatedAt: null,
    eventId: EVENT,
    key: `${EVENT}|${book}|${kind}|${side}|${line ?? ''}`,
    history: [{ t: lastChangeAt, decimal }],
    ...over,
    observedAt,
    lastChangeAt,
  };
}

/** Two-way moneyline for one book. */
function ml(book: string, home: number, away: number, over: Partial<StoredQuote> = {}): StoredQuote[] {
  return [sq(book, 'moneyline', 'home', null, home, over), sq(book, 'moneyline', 'away', null, away, over)];
}

function opts(over: Partial<FairOptions> = {}): FairOptions {
  return {
    sharpBooks: ['pinnacle', 'betonlineag', 'lowvig'],
    excludeBooks: ['draftkings'],
    method: 'multiplicative',
    minConsensusBooks: 3,
    threeWay: false,
    now: NOW,
    maxAgeMs: 60_000,
    ...over,
  };
}

function onlyGroup(quotes: StoredQuote[]): MarketGroup {
  const groups = groupQuotes(quotes);
  expect(groups).toHaveLength(1);
  return groups[0];
}

describe('groupKey', () => {
  it('puts both sides of a spread at the same home line in one group', () => {
    expect(groupKey('spread', 'home', -3.5)).toBe('spread|-3.5');
    expect(groupKey('spread', 'away', 3.5)).toBe('spread|-3.5');
    expect(groupKey('spread', 'away', -3.5)).toBe('spread|3.5');
    expect(groupKey('spread', 'home', 3.5)).toBe('spread|3.5');
  });

  it('normalises -0 to 0', () => {
    expect(groupKey('spread', 'home', 0)).toBe('spread|0');
    expect(groupKey('spread', 'away', 0)).toBe('spread|0');
    expect(groupKey('spread', 'home', -0)).toBe('spread|0');
    expect(groupKey('total', 'over', -0)).toBe('total|0');
  });

  it('uses the total for both over and under, and one moneyline group', () => {
    expect(groupKey('total', 'over', 224.5)).toBe('total|224.5');
    expect(groupKey('total', 'under', 224.5)).toBe('total|224.5');
    expect(groupKey('moneyline', 'home', null)).toBe('moneyline|');
    expect(groupKey('moneyline', 'draw', null)).toBe('moneyline|');
    expect(groupKey('moneyline', 'away', null)).toBe('moneyline|');
  });
});

describe('requiredSides', () => {
  it('lists the outcomes of each market', () => {
    expect(requiredSides('moneyline', false)).toEqual(['home', 'away']);
    expect(requiredSides('moneyline', true)).toEqual(['home', 'draw', 'away']);
    expect(requiredSides('spread', false)).toEqual(['home', 'away']);
    expect(requiredSides('spread', true)).toEqual(['home', 'away']);
    expect(requiredSides('total', false)).toEqual(['over', 'under']);
  });

  it('returns a fresh array each call', () => {
    const a = requiredSides('total', false);
    a.push('home');
    expect(requiredSides('total', false)).toEqual(['over', 'under']);
  });
});

describe('groupQuotes', () => {
  it('groups by market and book', () => {
    const quotes = [
      sq('pinnacle', 'spread', 'home', -3.5, 1.95),
      sq('pinnacle', 'spread', 'away', 3.5, 1.95),
      sq('draftkings', 'spread', 'away', 3.5, 1.91),
      sq('draftkings', 'spread', 'home', -4.5, 2.05),
      sq('pinnacle', 'total', 'over', 224.5, 1.9),
      sq('pinnacle', 'total', 'under', 224.5, 2.0),
      ...ml('pinnacle', 1.6, 2.5),
    ];
    const groups = groupQuotes(quotes);
    expect(groups.map((g) => g.groupKey)).toEqual(['spread|-3.5', 'spread|-4.5', 'total|224.5', 'moneyline|']);
    const spread = groups[0];
    expect(spread.kind).toBe('spread');
    expect([...spread.books.keys()]).toEqual(['pinnacle', 'draftkings']);
    expect([...(spread.books.get('pinnacle')?.keys() ?? [])]).toEqual(['home', 'away']);
    expect(spread.books.get('draftkings')?.get('away')?.decimal).toBe(1.91);
    expect(groups[2].books.get('pinnacle')?.get('under')?.decimal).toBe(2.0);
  });

  it('keeps the most recently observed quote on duplicates', () => {
    const group = onlyGroup([
      sq('pinnacle', 'moneyline', 'home', null, 1.7, { observedAt: NOW - 5000 }),
      sq('pinnacle', 'moneyline', 'home', null, 1.8, { observedAt: NOW - 1000 }),
      sq('pinnacle', 'moneyline', 'home', null, 1.9, { observedAt: NOW - 9000 }),
    ]);
    expect(group.books.get('pinnacle')?.get('home')?.decimal).toBe(1.8);
  });

  it('returns nothing for no quotes', () => {
    expect(groupQuotes([])).toEqual([]);
  });
});

describe('fairForGroup — sharp books', () => {
  it('uses the first usable sharp book in priority order', () => {
    const group = onlyGroup([...ml('draftkings', 1.8, 2.0), ...ml('betonlineag', 1.85, 2.02), ...ml('pinnacle', 1.9, 2.0)]);
    const fair = fairForGroup(group, opts());
    expect(fair).not.toBeNull();
    const expected = devig([1.9, 2.0], 'multiplicative');
    expect(fair?.source).toBe('pinnacle');
    expect(fair?.sharpBook).toBe('pinnacle');
    expect(fair?.bookCount).toBe(1);
    expect(fair?.groupKey).toBe('moneyline|');
    expect(fair?.kind).toBe('moneyline');
    expect(fair?.probs.home).toBeCloseTo(expected[0], 12);
    expect(fair?.probs.away).toBeCloseTo(expected[1], 12);
    expect(fair?.sharpQuotes?.home?.book).toBe('pinnacle');
    expect(fair?.sharpQuotes?.away?.decimal).toBe(2.0);

    const reordered = fairForGroup(group, opts({ sharpBooks: ['betonlineag', 'pinnacle'] }));
    expect(reordered?.source).toBe('betonlineag');
    expect(reordered?.probs.home).toBeCloseTo(devig([1.85, 2.02], 'multiplicative')[0], 12);
  });

  it('applies the requested devig method', () => {
    const group = onlyGroup(ml('pinnacle', 1.3, 3.6));
    const fair = fairForGroup(group, opts({ method: 'worst' }));
    const expected = devig([1.3, 3.6], 'worst');
    expect(fair?.probs.home).toBeCloseTo(expected[0], 12);
    expect(fair?.probs.away).toBeCloseTo(expected[1], 12);
  });

  it('ignores excluded books even when listed as sharp', () => {
    const group = onlyGroup([...ml('pinnacle', 1.9, 2.0), ...ml('betonlineag', 1.85, 2.02)]);
    const fair = fairForGroup(group, opts({ excludeBooks: ['draftkings', 'Pinnacle'] }));
    expect(fair?.source).toBe('betonlineag');
  });

  it('matches sharp book names case-insensitively', () => {
    const group = onlyGroup(ml('pinnacle', 1.9, 2.0));
    expect(fairForGroup(group, opts({ sharpBooks: ['PINNACLE'] }))?.sharpBook).toBe('pinnacle');
  });

  it('falls through an incomplete sharp market', () => {
    const group = onlyGroup([sq('pinnacle', 'moneyline', 'home', null, 1.9), ...ml('betonlineag', 1.85, 2.02)]);
    expect(fairForGroup(group, opts())?.source).toBe('betonlineag');
  });

  it('falls through a suspended sharp market', () => {
    const group = onlyGroup([
      sq('pinnacle', 'moneyline', 'home', null, 1.9),
      sq('pinnacle', 'moneyline', 'away', null, 2.0, { suspended: true }),
      ...ml('betonlineag', 1.85, 2.02),
    ]);
    expect(fairForGroup(group, opts())?.source).toBe('betonlineag');
  });

  it('falls through a stale sharp market', () => {
    const group = onlyGroup([
      sq('pinnacle', 'moneyline', 'home', null, 1.9, { observedAt: NOW - 60_001 }),
      sq('pinnacle', 'moneyline', 'away', null, 2.0),
      ...ml('betonlineag', 1.85, 2.02, { observedAt: NOW - 60_000 }),
    ]);
    expect(fairForGroup(group, opts())?.source).toBe('betonlineag');
  });

  it('falls through every sharp book to consensus', () => {
    const group = onlyGroup([
      ...ml('pinnacle', 1.9, 2.0, { suspended: true }),
      sq('betonlineag', 'moneyline', 'home', null, 1.85),
      ...ml('lowvig', 1.9, 2.0, { observedAt: NOW - 120_000 }),
      ...ml('fanduel', 1.83, 2.0),
      ...ml('betmgm', 1.87, 1.95),
      ...ml('caesars', 1.8, 2.05),
      ...ml('draftkings', 1.7, 2.3),
    ]);
    const fair = fairForGroup(group, opts());
    expect(fair?.source).toBe('consensus(3)');
    expect(fair?.sharpBook).toBeNull();
    expect(fair?.sharpQuotes).toBeNull();
    expect(fair?.bookCount).toBe(3);
    const per = [
      devig([1.83, 2.0], 'multiplicative'),
      devig([1.87, 1.95], 'multiplicative'),
      devig([1.8, 2.05], 'multiplicative'),
    ];
    expect(fair?.probs.home).toBeCloseTo((per[0][0] + per[1][0] + per[2][0]) / 3, 12);
    expect(fair?.probs.away).toBeCloseTo((per[0][1] + per[1][1] + per[2][1]) / 3, 12);
  });
});

describe('fairForGroup — consensus', () => {
  it('needs at least minConsensusBooks usable books', () => {
    const group = onlyGroup([...ml('fanduel', 1.83, 2.0), ...ml('betmgm', 1.87, 1.95), ...ml('draftkings', 1.7, 2.3)]);
    expect(fairForGroup(group, opts({ minConsensusBooks: 3 }))).toBeNull();
    const fair = fairForGroup(group, opts({ minConsensusBooks: 2 }));
    expect(fair?.source).toBe('consensus(2)');
    expect(fair?.bookCount).toBe(2);
  });

  it('does not count excluded, stale, suspended or incomplete books', () => {
    const group = onlyGroup([
      ...ml('fanduel', 1.83, 2.0),
      ...ml('betmgm', 1.87, 1.95),
      ...ml('draftkings', 1.7, 2.3),
      ...ml('bovada', 1.8, 2.0, { observedAt: NOW - 61_000 }),
      ...ml('betrivers', 1.8, 2.0, { suspended: true }),
      sq('fanatics', 'moneyline', 'home', null, 1.8),
    ]);
    expect(fairForGroup(group, opts({ minConsensusBooks: 3 }))).toBeNull();
    expect(fairForGroup(group, opts({ minConsensusBooks: 2 }))?.bookCount).toBe(2);
  });

  it('returns null for an empty group or when nothing is usable', () => {
    expect(fairForGroup({ groupKey: 'moneyline|', kind: 'moneyline', books: new Map() }, opts({ minConsensusBooks: 0 }))).toBeNull();
    const group = onlyGroup(ml('draftkings', 1.9, 1.9));
    expect(fairForGroup(group, opts({ minConsensusBooks: 1 }))).toBeNull();
  });
});

describe('fairForGroup — freshness semantics', () => {
  it('reports the oldest observedAt and newest lastChangeAt of the sharp quotes used', () => {
    const group = onlyGroup([
      sq('pinnacle', 'moneyline', 'home', null, 1.9, { observedAt: NOW - 20_000, lastChangeAt: NOW - 90_000 }),
      sq('pinnacle', 'moneyline', 'away', null, 2.0, { observedAt: NOW - 5_000, lastChangeAt: NOW - 30_000 }),
      // Not used: must not influence the result.
      ...ml('draftkings', 1.8, 2.0, { observedAt: NOW - 50_000, lastChangeAt: NOW - 1_000 }),
      ...ml('betonlineag', 1.8, 2.0, { observedAt: NOW - 55_000, lastChangeAt: NOW - 500 }),
    ]);
    const fair = fairForGroup(group, opts());
    expect(fair?.source).toBe('pinnacle');
    expect(fair?.observedAt).toBe(NOW - 20_000);
    expect(fair?.lastChangeAt).toBe(NOW - 30_000);
  });

  it('spans every book used by a consensus', () => {
    const group = onlyGroup([
      ...ml('fanduel', 1.83, 2.0, { observedAt: NOW - 10_000, lastChangeAt: NOW - 100_000 }),
      ...ml('betmgm', 1.87, 1.95, { observedAt: NOW - 40_000, lastChangeAt: NOW - 70_000 }),
      ...ml('caesars', 1.8, 2.05, { observedAt: NOW - 2_000, lastChangeAt: NOW - 8_000 }),
      ...ml('draftkings', 1.8, 2.05, { observedAt: NOW - 59_000, lastChangeAt: NOW - 100 }),
    ]);
    const fair = fairForGroup(group, opts());
    expect(fair?.source).toBe('consensus(3)');
    expect(fair?.observedAt).toBe(NOW - 40_000);
    expect(fair?.lastChangeAt).toBe(NOW - 8_000);
  });
});

describe('fairForGroup — three-way moneyline', () => {
  const threeWay = [
    sq('pinnacle', 'moneyline', 'home', null, 2.4),
    sq('pinnacle', 'moneyline', 'draw', null, 3.4),
    sq('pinnacle', 'moneyline', 'away', null, 3.1),
  ];

  it('devigs home/draw/away together', () => {
    const fair = fairForGroup(onlyGroup(threeWay), opts({ threeWay: true }));
    const expected = devig([2.4, 3.4, 3.1], 'multiplicative');
    expect(fair?.probs.home).toBeCloseTo(expected[0], 12);
    expect(fair?.probs.draw).toBeCloseTo(expected[1], 12);
    expect(fair?.probs.away).toBeCloseTo(expected[2], 12);
    expect((fair?.probs.home ?? 0) + (fair?.probs.draw ?? 0) + (fair?.probs.away ?? 0)).toBeCloseTo(1, 9);
    expect(Object.keys(fair?.sharpQuotes ?? {}).sort()).toEqual(['away', 'draw', 'home']);
  });

  it('requires the draw price when the league is three-way', () => {
    const group = onlyGroup(ml('pinnacle', 2.4, 3.1));
    expect(fairForGroup(group, opts({ threeWay: true, minConsensusBooks: 1 }))).toBeNull();
  });

  it('refuses to devig a market with a draw price as two-way', () => {
    expect(fairForGroup(onlyGroup(threeWay), opts({ threeWay: false, minConsensusBooks: 1 }))).toBeNull();
  });
});

describe('fairProbForPick', () => {
  const quotes = [
    sq('pinnacle', 'spread', 'home', -3.5, 1.8),
    sq('pinnacle', 'spread', 'away', 3.5, 2.1),
    sq('pinnacle', 'spread', 'home', -4.5, 2.0),
    sq('pinnacle', 'spread', 'away', 4.5, 1.85),
    sq('pinnacle', 'total', 'over', 224.5, 1.95),
    sq('pinnacle', 'total', 'under', 224.5, 1.95),
    ...ml('pinnacle', 1.5, 2.8),
    sq('draftkings', 'spread', 'away', 3.5, 2.05),
  ];

  it('returns the fair probability of the pick from its own market', () => {
    const spread = devig([1.8, 2.1], 'multiplicative');
    expect(fairProbForPick(quotes, 'spread', 'away', 3.5, opts())).toBeCloseTo(spread[1], 12);
    expect(fairProbForPick(quotes, 'spread', 'home', -3.5, opts())).toBeCloseTo(spread[0], 12);
    expect(fairProbForPick(quotes, 'spread', 'away', 4.5, opts())).toBeCloseTo(devig([2.0, 1.85], 'multiplicative')[1], 12);
    expect(fairProbForPick(quotes, 'total', 'over', 224.5, opts())).toBeCloseTo(0.5, 12);
    expect(fairProbForPick(quotes, 'moneyline', 'home', null, opts())).toBeCloseTo(
      devig([1.5, 2.8], 'multiplicative')[0],
      12,
    );
  });

  it('returns null when the market is unknown or has no usable reference', () => {
    expect(fairProbForPick(quotes, 'spread', 'away', 5.5, opts())).toBeNull();
    expect(fairProbForPick(quotes, 'total', 'over', 230.5, opts())).toBeNull();
    expect(fairProbForPick(quotes, 'moneyline', 'draw', null, opts())).toBeNull();
    expect(fairProbForPick(quotes, 'spread', 'away', 3.5, opts({ now: NOW + 10 * 60_000 }))).toBeNull();
    expect(fairProbForPick([], 'moneyline', 'home', null, opts())).toBeNull();
  });
});

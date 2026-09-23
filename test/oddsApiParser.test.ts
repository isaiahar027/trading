import * as fs from 'node:fs';
import * as path from 'node:path';
import { describe, expect, it } from 'vitest';
import { parseOddsResponse, resolveLink } from '../src/sources/oddsApiParser';
import type { LeagueDef, Quote, SourceSnapshot } from '../src/types';

const NBA: LeagueDef = { key: 'NBA', name: 'NBA', oddsApiKey: 'basketball_nba', threeWay: false };
const EPL: LeagueDef = { key: 'EPL', name: 'Premier League', oddsApiKey: 'soccer_epl', threeWay: true };

const ALL_BOOKS = ['pinnacle', 'draftkings', 'fanduel', 'betmgm'];
const NBA_FETCHED_AT = Date.parse('2026-10-27T23:30:00Z');
const EPL_FETCHED_AT = Date.parse('2026-10-31T12:00:00Z');

const CELTICS = '8c1f0e4a2b7d4c6e9f1a3b5c7d9e0f12';
const MAGIC = '3d5b7f9a1c2e4a6b8d0f2a4c6e8b0d21';
const LAKERS = 'f0e1d2c3b4a5968778695a4b3c2d1e0f';
const ARSENAL = '5a1e3c9b7d2f4e6a8c0b1d3f5e7a9c2b';
const LIVERPOOL = '7b9d1f3a5c7e9b1d3f5a7c9e1b3d5f7a';

function fixture(name: string): unknown {
  return JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures', name), 'utf8')) as unknown;
}

function find(snap: SourceSnapshot, where: Partial<Quote>): Quote[] {
  return snap.quotes.filter((q) =>
    (Object.keys(where) as (keyof Quote)[]).every((k) => q[k] === where[k]),
  );
}

function one(snap: SourceSnapshot, where: Partial<Quote>): Quote {
  const list = find(snap, where);
  expect(list, JSON.stringify(where)).toHaveLength(1);
  return list[0];
}

function nba(books = ALL_BOOKS, linkState = 'nj'): SourceSnapshot {
  return parseOddsResponse(fixture('oddsapi-nba.json'), NBA, books, NBA_FETCHED_AT, { linkState });
}

describe('parseOddsResponse — NBA fixture', () => {
  it('builds the snapshot envelope', () => {
    const snap = nba();
    expect(snap.source).toBe('odds-api');
    expect(snap.league).toBe('NBA');
    expect(snap.fetchedAt).toBe(NBA_FETCHED_AT);
    expect(snap.complete).toBe(true);
    expect(snap.books).toEqual(ALL_BOOKS);
  });

  it('keeps valid events and skips malformed ones (missing team, bad commence_time)', () => {
    const snap = nba();
    expect(snap.events.map((e) => e.sourceEventId)).toEqual([CELTICS, MAGIC, LAKERS]);
    const celtics = snap.events[0];
    expect(celtics).toEqual({
      source: 'odds-api',
      sourceEventId: CELTICS,
      league: 'NBA',
      home: 'Boston Celtics',
      away: 'New York Knicks',
      startTime: Date.parse('2026-10-28T00:00:00Z'),
      isLive: false,
    });
  });

  it('marks events live by commence time', () => {
    const snap = nba();
    const live = Object.fromEntries(snap.events.map((e) => [e.sourceEventId, e.isLive]));
    expect(live).toEqual({ [CELTICS]: false, [MAGIC]: true, [LAKERS]: false });

    const atKickoff = parseOddsResponse(fixture('oddsapi-nba.json'), NBA, ALL_BOOKS, Date.parse('2026-10-28T00:00:00Z'));
    expect(atKickoff.events.find((e) => e.sourceEventId === CELTICS)?.isLive).toBe(true);
    const oneMsBefore = parseOddsResponse(fixture('oddsapi-nba.json'), NBA, ALL_BOOKS, Date.parse('2026-10-28T00:00:00Z') - 1);
    expect(oneMsBefore.events.find((e) => e.sourceEventId === CELTICS)?.isLive).toBe(false);
  });

  it('skips malformed items and ignores unknown markets, keeping everything else', () => {
    const snap = nba();
    // Celtics 21 (pin 6, dk 6, fd 5, mgm 4) + Magic 12 + Lakers 4.
    expect(snap.quotes).toHaveLength(37);
    // price 0.9 (BetMGM Over) is skipped, the Under survives.
    expect(find(snap, { sourceEventId: CELTICS, book: 'betmgm', kind: 'total' }).map((q) => q.side)).toEqual(['under']);
    // "Boston Celtic" is not a team in the event -> skipped.
    expect(find(snap, { sourceEventId: CELTICS, book: 'betmgm', kind: 'moneyline' }).map((q) => q.side)).toEqual(['away']);
    // FanDuel Knicks spread has no point -> skipped.
    expect(find(snap, { sourceEventId: CELTICS, book: 'fanduel', kind: 'spread' }).map((q) => q.side)).toEqual(['home']);
    // alternate_spreads and player_points are not modelled.
    expect(snap.quotes.some((q) => q.line === -11.5 || q.line === 11.5 || q.line === 27.5)).toBe(false);
    // Every quote points at an event of the snapshot.
    const ids = new Set(snap.events.map((e) => e.sourceEventId));
    expect(snap.quotes.every((q) => ids.has(q.sourceEventId))).toBe(true);
    // No price at or below 1 ever gets through.
    expect(snap.quotes.every((q) => q.decimal > 1)).toBe(true);
  });

  it('maps moneyline outcomes to home/away by team name regardless of order', () => {
    const snap = nba();
    expect(one(snap, { sourceEventId: CELTICS, book: 'draftkings', kind: 'moneyline', side: 'home' }).decimal).toBe(1.38);
    expect(one(snap, { sourceEventId: CELTICS, book: 'draftkings', kind: 'moneyline', side: 'away' }).decimal).toBe(3.15);
    // Magic event lists the away team first.
    expect(one(snap, { sourceEventId: MAGIC, book: 'pinnacle', kind: 'moneyline', side: 'home' }).decimal).toBe(1.62);
    expect(one(snap, { sourceEventId: MAGIC, book: 'pinnacle', kind: 'moneyline', side: 'away' }).decimal).toBe(2.42);
    for (const q of find(snap, { kind: 'moneyline' })) expect(q.line).toBeNull();
    expect(find(snap, { side: 'draw' })).toHaveLength(0);
  });

  it('keeps spread lines from each side’s own perspective', () => {
    const snap = nba();
    const home = one(snap, { sourceEventId: CELTICS, book: 'pinnacle', kind: 'spread', side: 'home' });
    const away = one(snap, { sourceEventId: CELTICS, book: 'pinnacle', kind: 'spread', side: 'away' });
    expect([home.line, home.decimal]).toEqual([-7.5, 1.943]);
    expect([away.line, away.decimal]).toEqual([7.5, 1.952]);
    const magicAway = one(snap, { sourceEventId: MAGIC, book: 'draftkings', kind: 'spread', side: 'away' });
    expect(magicAway.line).toBe(2.5);
    const mgmHome = one(snap, { sourceEventId: CELTICS, book: 'betmgm', kind: 'spread', side: 'home' });
    expect(mgmHome.line).toBe(-7);
  });

  it('parses totals with the same line for over and under', () => {
    const snap = nba();
    const over = one(snap, { sourceEventId: CELTICS, book: 'draftkings', kind: 'total', side: 'over' });
    const under = one(snap, { sourceEventId: CELTICS, book: 'draftkings', kind: 'total', side: 'under' });
    expect(over.line).toBe(225.5);
    expect(under.line).toBe(225.5);
    expect(one(snap, { sourceEventId: CELTICS, book: 'pinnacle', kind: 'total', side: 'under' }).decimal).toBe(1.97);
  });

  it('fills the fixed quote fields', () => {
    const snap = nba();
    for (const q of snap.quotes) {
      expect(q.source).toBe('odds-api');
      expect(q.suspended).toBe(false);
      expect(q.isMainLine).toBe(true);
      expect(q.observedAt).toBe(NBA_FETCHED_AT);
    }
  });

  it('uses market last_update, falling back to the bookmaker last_update', () => {
    const snap = nba();
    expect(one(snap, { sourceEventId: CELTICS, book: 'draftkings', kind: 'spread', side: 'home' }).bookUpdatedAt).toBe(
      Date.parse('2026-10-27T23:26:40Z'),
    );
    // DK totals market has no last_update -> bookmaker's.
    expect(one(snap, { sourceEventId: CELTICS, book: 'draftkings', kind: 'total', side: 'over' }).bookUpdatedAt).toBe(
      Date.parse('2026-10-27T23:29:12Z'),
    );
  });

  it('filters bookmakers to the requested books', () => {
    const snap = nba(['pinnacle', 'DraftKings']);
    expect(new Set(snap.quotes.map((q) => q.book))).toEqual(new Set(['pinnacle', 'draftkings']));
    expect(snap.quotes).toHaveLength(28);
    expect(snap.books).toEqual(['pinnacle', 'draftkings']);
  });

  it('keeps every bookmaker when the books list is empty', () => {
    const snap = nba([]);
    expect(snap.quotes).toHaveLength(37);
    expect(snap.books).toEqual(['betmgm', 'draftkings', 'fanduel', 'pinnacle']);
  });

  it('resolves links: outcome, then market, then bookmaker; fills {state}; drops unusable ones', () => {
    const snap = nba();
    expect(one(snap, { sourceEventId: CELTICS, book: 'draftkings', kind: 'moneyline', side: 'home' }).link).toBe(
      'https://sportsbook.draftkings.com/event/32145678?outcomes=0ML86545123_1',
    );
    expect(one(snap, { sourceEventId: CELTICS, book: 'draftkings', kind: 'spread', side: 'away' }).link).toBe(
      'https://sportsbook.draftkings.com/event/32145678?category=game-lines',
    );
    expect(one(snap, { sourceEventId: CELTICS, book: 'fanduel', kind: 'moneyline', side: 'away' }).link).toBe(
      'https://nj.sportsbook.fanduel.com/addToBetslip?marketId=734.12345678&selectionId=1002',
    );
    expect(one(snap, { sourceEventId: CELTICS, book: 'fanduel', kind: 'total', side: 'over' }).link).toBe(
      'https://nj.sportsbook.fanduel.com/navigation/nba',
    );
    expect(one(snap, { sourceEventId: CELTICS, book: 'betmgm', kind: 'spread', side: 'home' }).link).toBe(
      'https://sports.nj.betmgm.com/en/sports/events/new-york-knicks-at-boston-celtics-16543210',
    );
    const pinnacle = one(snap, { sourceEventId: CELTICS, book: 'pinnacle', kind: 'moneyline', side: 'home' });
    expect(pinnacle.link).toBeUndefined();
    expect('link' in pinnacle).toBe(false);
  });

  it('drops {state} links when no state is configured', () => {
    const snap = nba(ALL_BOOKS, '');
    expect(find(snap, { book: 'fanduel' }).every((q) => q.link === undefined)).toBe(true);
    expect(find(snap, { book: 'betmgm' }).every((q) => q.link === undefined)).toBe(true);
    expect(one(snap, { sourceEventId: CELTICS, book: 'draftkings', kind: 'moneyline', side: 'away' }).link).toBe(
      'https://sportsbook.draftkings.com/event/32145678?outcomes=0ML86545123_3',
    );
  });
});

describe('parseOddsResponse — EPL fixture (3-way)', () => {
  const epl = () => parseOddsResponse(fixture('oddsapi-epl.json'), EPL, ALL_BOOKS, EPL_FETCHED_AT, { linkState: 'nj' });

  it('includes the draw for three-way leagues', () => {
    const snap = epl();
    expect(snap.events.map((e) => e.sourceEventId)).toEqual([ARSENAL, LIVERPOOL]);
    expect(snap.events.every((e) => !e.isLive)).toBe(true);
    const dkMl = find(snap, { sourceEventId: ARSENAL, book: 'draftkings', kind: 'moneyline' });
    expect(Object.fromEntries(dkMl.map((q) => [q.side, q.decimal]))).toEqual({ home: 1.8, away: 4.5, draw: 3.8 });
    expect(one(snap, { sourceEventId: ARSENAL, book: 'draftkings', kind: 'moneyline', side: 'draw' }).link).toBe(
      'https://sportsbook.draftkings.com/event/31998877?outcomes=0ML7771_2',
    );
    // Case-insensitive "draw".
    expect(one(snap, { sourceEventId: ARSENAL, book: 'fanduel', kind: 'moneyline', side: 'draw' }).decimal).toBe(3.75);
    expect(find(snap, { side: 'draw' })).toHaveLength(5);
  });

  it('skips unknown outcome names, missing points and unknown markets', () => {
    const snap = epl();
    // Arsenal 19 (pin 7, dk 7, fd 3, mgm 2) + Liverpool 9 (pin 5, dk 4).
    expect(snap.quotes).toHaveLength(28);
    expect(find(snap, { sourceEventId: ARSENAL, book: 'betmgm' }).map((q) => q.side).sort()).toEqual(['away', 'home']);
    expect(find(snap, { sourceEventId: LIVERPOOL, book: 'draftkings', kind: 'total' }).map((q) => q.side)).toEqual(['under']);
    // h2h_lay ignored: Pinnacle has exactly one Arsenal moneyline.
    expect(one(snap, { sourceEventId: ARSENAL, book: 'pinnacle', kind: 'moneyline', side: 'home' }).decimal).toBe(1.83);
  });

  it('handles fractional soccer spreads and totals', () => {
    const snap = epl();
    expect(one(snap, { sourceEventId: ARSENAL, book: 'pinnacle', kind: 'spread', side: 'home' }).line).toBe(-0.5);
    expect(one(snap, { sourceEventId: ARSENAL, book: 'pinnacle', kind: 'spread', side: 'away' }).line).toBe(0.5);
    expect(one(snap, { sourceEventId: LIVERPOOL, book: 'pinnacle', kind: 'total', side: 'over' }).line).toBe(2.75);
  });

  it('does not produce draws for two-way leagues', () => {
    const twoWay: LeagueDef = { ...EPL, threeWay: false };
    const snap = parseOddsResponse(fixture('oddsapi-epl.json'), twoWay, ALL_BOOKS, EPL_FETCHED_AT);
    expect(find(snap, { side: 'draw' })).toHaveLength(0);
    expect(snap.quotes).toHaveLength(23);
  });
});

describe('parseOddsResponse — edge cases', () => {
  const T0 = Date.parse('2026-10-27T20:00:00Z');

  function event(bookmakers: unknown[], extra: Record<string, unknown> = {}): Record<string, unknown> {
    return {
      id: 'evt1',
      sport_key: 'basketball_nba',
      commence_time: '2026-10-28T00:00:00Z',
      home_team: 'Boston Celtics',
      away_team: 'New York Knicks',
      bookmakers,
      ...extra,
    };
  }

  it('throws on a payload that is not an array', () => {
    expect(() => parseOddsResponse({ message: 'Unknown sport' }, NBA, [], T0)).toThrow(/Unexpected Odds API payload.*Unknown sport/);
    expect(() => parseOddsResponse(null, NBA, [], T0)).toThrow(/Unexpected Odds API payload/);
    expect(() => parseOddsResponse('oops', NBA, [], T0)).toThrow(/Unexpected Odds API payload/);
    expect(() => parseOddsResponse(undefined, NBA, [], T0)).toThrow(/Unexpected Odds API payload/);
  });

  it('never throws for bad items inside an array', () => {
    const raw: unknown[] = [
      null,
      42,
      'x',
      [],
      { id: 'no-teams' },
      event('not-an-array' as unknown as unknown[], { id: 'bad-bookmakers' }),
      event([null, { key: 'draftkings' }, { key: 'draftkings', markets: [null, { key: 'h2h' }, { key: 'h2h', outcomes: [null, {}] }] }], {
        id: 'bad-markets',
      }),
      event([
        {
          key: 'draftkings',
          markets: [
            { key: 'h2h', outcomes: [{ name: 'Boston Celtics', price: 'abc' }, { name: 'New York Knicks', price: Number.NaN }] },
            { key: 'spreads', outcomes: [{ name: 'Boston Celtics', price: 1.9, point: 'x' }] },
            { key: 'totals', outcomes: [{ name: 'Over', price: 1.9, point: -1 }, { name: 'Sideways', price: 1.9, point: 220 }] },
          ],
        },
      ]),
    ];
    const snap = parseOddsResponse(raw, NBA, [], T0);
    expect(snap.events.map((e) => e.sourceEventId)).toEqual(['bad-bookmakers', 'bad-markets', 'evt1']);
    expect(snap.quotes).toHaveLength(0);
  });

  it('skips duplicate event ids', () => {
    const a = event([{ key: 'draftkings', markets: [{ key: 'h2h', outcomes: [{ name: 'Boston Celtics', price: 1.5 }] }] }]);
    const b = event([{ key: 'draftkings', markets: [{ key: 'h2h', outcomes: [{ name: 'Boston Celtics', price: 1.7 }] }] }], {
      home_team: 'Somebody Else',
    });
    const snap = parseOddsResponse([a, b], NBA, [], T0);
    expect(snap.events).toHaveLength(1);
    expect(snap.events[0].home).toBe('Boston Celtics');
    expect(snap.quotes.map((q) => q.decimal)).toEqual([1.5]);
  });

  it('keeps the most recently updated duplicate (book, kind, side, line)', () => {
    const raw = [
      event([
        {
          key: 'draftkings',
          last_update: '2026-10-27T19:00:00Z',
          markets: [
            { key: 'h2h', last_update: '2026-10-27T19:59:00Z', outcomes: [{ name: 'Boston Celtics', price: 1.5 }] },
            { key: 'h2h', last_update: '2026-10-27T19:59:30Z', outcomes: [{ name: 'Boston Celtics', price: 1.55 }] },
            { key: 'h2h', last_update: '2026-10-27T19:58:00Z', outcomes: [{ name: 'Boston Celtics', price: 1.45 }] },
            { key: 'spreads', outcomes: [{ name: 'Boston Celtics', price: 1.9, point: -3.5 }] },
            { key: 'spreads', outcomes: [{ name: 'Boston Celtics', price: 1.95, point: -4.5 }] },
          ],
        },
        {
          key: 'DraftKings',
          last_update: '2026-10-27T19:59:45Z',
          markets: [{ key: 'h2h', outcomes: [{ name: 'Boston Celtics', price: 1.6 }] }],
        },
      ]),
    ];
    const snap = parseOddsResponse(raw, NBA, ['draftkings'], T0);
    const ml = snap.quotes.filter((q) => q.kind === 'moneyline');
    expect(ml).toHaveLength(1);
    expect(ml[0].decimal).toBe(1.6);
    expect(ml[0].bookUpdatedAt).toBe(Date.parse('2026-10-27T19:59:45Z'));
    // Different lines are different quotes, not duplicates.
    expect(snap.quotes.filter((q) => q.kind === 'spread').map((q) => q.line)).toEqual([-3.5, -4.5]);
  });

  it('normalizes a pick-em spread of -0 to 0 and accepts numeric strings', () => {
    const raw = [
      event([
        {
          key: 'pinnacle',
          markets: [
            {
              key: 'spreads',
              outcomes: [
                { name: 'Boston Celtics', price: '1.95', point: -0 },
                { name: 'New York Knicks', price: 1.93, point: '0' },
              ],
            },
          ],
        },
      ]),
    ];
    const snap = parseOddsResponse(raw, NBA, [], T0);
    expect(snap.quotes).toHaveLength(2);
    for (const q of snap.quotes) expect(Object.is(q.line, 0)).toBe(true);
    expect(snap.quotes[0].decimal).toBe(1.95);
    expect(snap.quotes[0].bookUpdatedAt).toBeNull();
  });

  it('ignores a moneyline point and matches team names case-insensitively', () => {
    const raw = [
      event([{ key: 'pinnacle', markets: [{ key: 'h2h', outcomes: [{ name: 'boston celtics', price: 1.5, point: 3 }] }] }]),
    ];
    const snap = parseOddsResponse(raw, NBA, [], T0);
    expect(snap.quotes).toHaveLength(1);
    expect(snap.quotes[0].side).toBe('home');
    expect(snap.quotes[0].line).toBeNull();
  });

  it('does not accept a draw in a spread market', () => {
    const raw = [event([{ key: 'pinnacle', markets: [{ key: 'spreads', outcomes: [{ name: 'Draw', price: 3.5, point: 0 }] }] }])];
    expect(parseOddsResponse(raw, EPL, [], T0).quotes).toHaveLength(0);
  });
});

describe('resolveLink', () => {
  it('accepts plain https links', () => {
    expect(resolveLink('https://sportsbook.draftkings.com/event/1', '')).toBe('https://sportsbook.draftkings.com/event/1');
    expect(resolveLink('  https://sportsbook.draftkings.com/event/1  ', 'nj')).toBe('https://sportsbook.draftkings.com/event/1');
  });

  it('rejects missing and non-https links', () => {
    expect(resolveLink(undefined, 'nj')).toBeUndefined();
    expect(resolveLink(null, 'nj')).toBeUndefined();
    expect(resolveLink('', 'nj')).toBeUndefined();
    expect(resolveLink('http://sportsbook.draftkings.com/', 'nj')).toBeUndefined();
    expect(resolveLink('javascript:alert(1)', 'nj')).toBeUndefined();
    expect(resolveLink('//sportsbook.draftkings.com/', 'nj')).toBeUndefined();
    expect(resolveLink('/event/1', 'nj')).toBeUndefined();
    expect(resolveLink('https://', 'nj')).toBeUndefined();
    expect(resolveLink('https://exa mple.com/', 'nj')).toBeUndefined();
    expect(resolveLink(`https://example.com/${'a'.repeat(3000)}`, 'nj')).toBeUndefined();
  });

  it('replaces {state} case-insensitively', () => {
    expect(resolveLink('https://{state}.sportsbook.fanduel.com/x', 'nj')).toBe('https://nj.sportsbook.fanduel.com/x');
    expect(resolveLink('https://{STATE}.sportsbook.fanduel.com/x?s={State}', 'NJ')).toBe(
      'https://nj.sportsbook.fanduel.com/x?s=nj',
    );
  });

  it('returns undefined when a placeholder cannot be filled', () => {
    expect(resolveLink('https://{state}.sportsbook.fanduel.com/x', '')).toBeUndefined();
    expect(resolveLink('https://{state}.sportsbook.fanduel.com/x', '   ')).toBeUndefined();
    expect(resolveLink('https://{state}.sportsbook.fanduel.com/x', 'n.j/evil')).toBeUndefined();
    expect(resolveLink('https://sportsbook.example.com/{region}/x', 'nj')).toBeUndefined();
    expect(resolveLink('https://{state}.example.com/{lang}/x', 'nj')).toBeUndefined();
  });
});

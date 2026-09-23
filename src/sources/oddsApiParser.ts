/**
 * Turns a The Odds API v4 `/odds` response into a SourceSnapshot.
 *
 * Pure function of its inputs: no I/O, no clock reads. Malformed events, bookmakers, markets and
 * outcomes are skipped (and counted) instead of failing the whole board; only a payload that is not
 * an array at all is rejected.
 */
import type { LeagueDef, MarketKind, Quote, RawEvent, Side, SourceSnapshot } from '../types';
import { createLogger } from '../util/logger';
import { parseDecimal } from '../util/odds';

const log = createLogger('odds-api-parser');

export interface OddsApiOutcome {
  name: string;
  price: number;
  point?: number;
  link?: string | null;
}

export interface OddsApiMarket {
  key: string;
  last_update?: string;
  outcomes: OddsApiOutcome[];
  link?: string | null;
}

export interface OddsApiBookmaker {
  key: string;
  title: string;
  last_update?: string;
  markets: OddsApiMarket[];
  link?: string | null;
}

export interface OddsApiEvent {
  id: string;
  sport_key: string;
  sport_title?: string;
  commence_time: string;
  home_team: string;
  away_team: string;
  bookmakers?: OddsApiBookmaker[];
}

/** Odds API market key -> our market kind. Anything else (h2h_lay, alternate_*, player props, ...) is ignored. */
const MARKET_KINDS: ReadonlyMap<string, MarketKind> = new Map<string, MarketKind>([
  ['h2h', 'moneyline'],
  ['spreads', 'spread'],
  ['totals', 'total'],
]);

const MAX_LINK_LENGTH = 2048;
const PLACEHOLDER = /\{[^{}]*\}/;
const HAS_STATE_PLACEHOLDER = /\{state\}/i;
const STATE_PLACEHOLDERS = /\{state\}/gi;
const SAFE_STATE = /^[a-z0-9-]{1,32}$/;

type JsonRecord = Record<string, unknown>;

function isRecord(v: unknown): v is JsonRecord {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

function nonEmptyString(v: unknown): string | null {
  if (typeof v !== 'string') return null;
  const s = v.trim();
  return s === '' ? null : s;
}

function parseTime(v: unknown): number | null {
  if (typeof v !== 'string' || v.trim() === '') return null;
  const t = Date.parse(v);
  return Number.isFinite(t) ? t : null;
}

function finiteNumber(v: unknown): number | null {
  if (typeof v === 'number') return Number.isFinite(v) ? v : null;
  if (typeof v === 'string' && v.trim() !== '') {
    const n = Number(v.trim());
    return Number.isFinite(n) ? n : null;
  }
  return null;
}

/** -0 -> 0 so that line keys and comparisons are stable. */
function normalizeZero(n: number): number {
  return n === 0 ? 0 : n;
}

function describePayload(raw: unknown): string {
  if (raw === null) return 'null';
  if (raw === undefined) return 'undefined';
  if (isRecord(raw)) {
    const message = raw.message;
    if (typeof message === 'string') return `object with message "${message.slice(0, 200)}"`;
    const keys = Object.keys(raw).slice(0, 10);
    return keys.length > 0 ? `object with keys ${keys.join(', ')}` : 'empty object';
  }
  return typeof raw;
}

/**
 * Makes a bookmaker deep link safe to put in an `href`.
 * Only absolute `https://` URLs are accepted. `{state}` (any case) is replaced by `state`; if a placeholder
 * would remain (unknown placeholder, or no state configured) the link is unusable and undefined is returned.
 */
export function resolveLink(link: string | null | undefined, state: string): string | undefined {
  if (typeof link !== 'string') return undefined;
  let url = link.trim();
  if (url === '' || url.length > MAX_LINK_LENGTH) return undefined;
  if (!/^https:\/\//i.test(url)) return undefined;

  if (HAS_STATE_PLACEHOLDER.test(url)) {
    const st = typeof state === 'string' ? state.trim().toLowerCase() : '';
    if (st === '' || !SAFE_STATE.test(st)) return undefined;
    url = url.replace(STATE_PLACEHOLDERS, st);
  }
  if (PLACEHOLDER.test(url) || /[{}]/.test(url)) return undefined;
  // No whitespace or control characters: the result goes straight into an href.
  if (/[\s\u0000-\u001f\u007f]/.test(url)) return undefined;

  try {
    const parsed = new URL(url);
    if (parsed.protocol !== 'https:' || parsed.hostname === '') return undefined;
  } catch {
    return undefined;
  }
  return url;
}

function sideFor(kind: MarketKind, name: string, home: string, away: string, threeWay: boolean): Side | null {
  if (kind === 'total') {
    if (/^over$/i.test(name)) return 'over';
    if (/^under$/i.test(name)) return 'under';
    return null;
  }
  if (name === home) return 'home';
  if (name === away) return 'away';
  const lower = name.toLowerCase();
  if (lower === home.toLowerCase()) return 'home';
  if (lower === away.toLowerCase()) return 'away';
  // A draw only exists in a 3-way moneyline; a "Draw" spread outcome would be orphaned junk.
  if (kind === 'moneyline' && threeWay && /^draw$/i.test(name)) return 'draw';
  return null;
}

function firstResolvableLink(candidates: unknown[], state: string): string | undefined {
  for (const c of candidates) {
    if (c === null || c === undefined) continue;
    const resolved = resolveLink(typeof c === 'string' ? c : null, state);
    if (resolved !== undefined) return resolved;
  }
  return undefined;
}

function isNewer(candidate: Quote, existing: Quote): boolean {
  const a = candidate.bookUpdatedAt ?? Number.NEGATIVE_INFINITY;
  const b = existing.bookUpdatedAt ?? Number.NEGATIVE_INFINITY;
  return a > b;
}

/**
 * Parses an Odds API `/v4/sports/{sport}/odds` response (decimal odds, ISO dates).
 *
 * @param raw       the decoded JSON body
 * @param league    league the request was made for (events are attributed to it)
 * @param books     bookmaker keys to keep; empty = keep every bookmaker in the payload
 * @param fetchedAt when the response was received (epoch ms); drives observedAt and isLive
 */
export function parseOddsResponse(
  raw: unknown,
  league: LeagueDef,
  books: string[],
  fetchedAt: number,
  opts?: { linkState?: string },
): SourceSnapshot {
  if (!Array.isArray(raw)) {
    throw new Error(`Unexpected Odds API payload: expected an array of events, got ${describePayload(raw)}`);
  }

  const linkState = opts?.linkState ?? '';
  const wanted = new Set(books.map((b) => b.trim().toLowerCase()).filter((b) => b !== ''));
  const seenBooks = new Set<string>();
  const seenEventIds = new Set<string>();
  const events: RawEvent[] = [];
  const quotes = new Map<string, Quote>();
  let skipped = 0;
  let duplicates = 0;

  for (const item of raw) {
    if (!isRecord(item)) {
      skipped++;
      continue;
    }
    const id = nonEmptyString(item.id);
    const home = nonEmptyString(item.home_team);
    const away = nonEmptyString(item.away_team);
    const startTime = parseTime(item.commence_time);
    if (id === null || home === null || away === null || startTime === null || home === away || seenEventIds.has(id)) {
      skipped++;
      continue;
    }
    seenEventIds.add(id);
    events.push({
      source: 'odds-api',
      sourceEventId: id,
      league: league.key,
      home,
      away,
      startTime,
      isLive: startTime <= fetchedAt,
    });

    const bookmakers = item.bookmakers;
    if (bookmakers === undefined || bookmakers === null) continue;
    if (!Array.isArray(bookmakers)) {
      skipped++;
      continue;
    }

    for (const bm of bookmakers) {
      if (!isRecord(bm)) {
        skipped++;
        continue;
      }
      const bookKey = nonEmptyString(bm.key);
      if (bookKey === null || !Array.isArray(bm.markets)) {
        skipped++;
        continue;
      }
      const book = bookKey.toLowerCase();
      if (wanted.size > 0 && !wanted.has(book)) continue;
      seenBooks.add(book);
      const bookUpdated = parseTime(bm.last_update);

      for (const market of bm.markets) {
        if (!isRecord(market)) {
          skipped++;
          continue;
        }
        const marketKey = nonEmptyString(market.key);
        if (marketKey === null) {
          skipped++;
          continue;
        }
        const kind = MARKET_KINDS.get(marketKey.toLowerCase());
        if (kind === undefined) continue; // a market we do not model
        if (!Array.isArray(market.outcomes)) {
          skipped++;
          continue;
        }
        const bookUpdatedAt = parseTime(market.last_update) ?? bookUpdated;

        for (const outcome of market.outcomes) {
          if (!isRecord(outcome)) {
            skipped++;
            continue;
          }
          const name = nonEmptyString(outcome.name);
          const decimal = parseDecimal(typeof outcome.price === 'number' || typeof outcome.price === 'string' ? outcome.price : null);
          const side = name === null ? null : sideFor(kind, name, home, away, league.threeWay);
          if (name === null || decimal === null || side === null) {
            skipped++;
            continue;
          }

          let line: number | null = null;
          if (kind !== 'moneyline') {
            const point = finiteNumber(outcome.point);
            if (point === null || (kind === 'total' && point <= 0)) {
              skipped++;
              continue;
            }
            line = normalizeZero(point);
          }

          const quote: Quote = {
            book,
            source: 'odds-api',
            sourceEventId: id,
            kind,
            side,
            line,
            decimal,
            suspended: false,
            isMainLine: true,
            observedAt: fetchedAt,
            bookUpdatedAt,
          };
          const link = firstResolvableLink([outcome.link, market.link, bm.link], linkState);
          if (link !== undefined) quote.link = link;

          const key = `${id}|${book}|${kind}|${side}|${line ?? ''}`;
          const existing = quotes.get(key);
          if (existing === undefined) {
            quotes.set(key, quote);
          } else {
            duplicates++;
            if (isNewer(quote, existing)) quotes.set(key, quote);
          }
        }
      }
    }
  }

  if (skipped > 0 || duplicates > 0) {
    log.debug(`${league.key}: skipped ${skipped} malformed item(s), merged ${duplicates} duplicate quote(s)`, {
      events: events.length,
      quotes: quotes.size,
    });
  }

  const snapshotBooks = wanted.size > 0 ? [...wanted] : [...seenBooks].sort();
  return {
    source: 'odds-api',
    league: league.key,
    fetchedAt,
    events,
    quotes: [...quotes.values()],
    complete: true,
    books: snapshotBooks,
  };
}

/**
 * In-memory board of every event and quote we currently know about.
 *
 * - Events are keyed by canonical id `${source}:${sourceEventId}`; quotes by
 *   `${eventId}|${book}|${kind}|${side}|${line ?? ''}`.
 * - Each quote keeps a short price history (appended only when the price actually changes) so the engine
 *   can ask "what was this price N seconds ago?" for stale-line detection.
 * - A `complete` snapshot is authoritative for its (source, league, books): stored quotes in that scope that
 *   the snapshot no longer contains are deleted, and events it no longer lists are marked not-live and left
 *   for `prune()`.
 * - Memory is bounded: max events, max quotes per event, max history points per quote, max history age.
 *
 * Returned events and quotes are the store's live objects (no copies, for speed on the 1 s engine tick).
 * Callers must treat them as read-only.
 */
import type { CanonicalEvent, LeagueKey, LiveScore, MarketKind, Quote, QuoteSource, RawEvent, Side, SourceSnapshot } from '../types';
import { createLogger } from '../util/logger';
import { roundOdds } from '../util/odds';

export interface PricePoint {
  t: number;
  decimal: number;
}

export interface StoredQuote extends Quote {
  eventId: string;
  key: string;
  /** Chronological (non-decreasing t) price changes; the last point is the current price. */
  history: PricePoint[];
  /** When the price last changed (bookUpdatedAt ?? observedAt of the change). */
  lastChangeAt: number;
}

export interface IngestResult {
  eventsUpserted: number;
  quotesUpserted: number;
  quotesRemoved: number;
  priceChanges: number;
  /** Quotes skipped because they were malformed or had no matching RawEvent in the snapshot. */
  quotesRejected?: number;
  /** Events dropped during ingest to respect `maxEvents`. */
  eventsRemoved?: number;
}

export interface MarketStoreOptions {
  historyMaxPoints?: number;
  historyMaxAgeMs?: number;
  eventRetentionMs?: number;
  maxEvents?: number;
  /** Safety cap on quotes held per event (oldest observedAt evicted first). Default 2000. */
  maxQuotesPerEvent?: number;
}

const DEFAULT_HISTORY_MAX_POINTS = 120;
const DEFAULT_HISTORY_MAX_AGE_MS = 30 * 60_000;
const DEFAULT_EVENT_RETENTION_MS = 20 * 60_000;
const DEFAULT_MAX_EVENTS = 5000;
const DEFAULT_MAX_QUOTES_PER_EVENT = 2000;
/** Events that started more than this long ago are always pruned. */
const STARTED_EVENT_MAX_AGE_MS = 12 * 60 * 60_000;

const SIDES_BY_KIND: Record<MarketKind, ReadonlySet<Side>> = {
  moneyline: new Set<Side>(['home', 'away', 'draw']),
  spread: new Set<Side>(['home', 'away']),
  total: new Set<Side>(['over', 'under']),
};

const log = createLogger('marketStore');

interface EventEntry {
  event: CanonicalEvent;
  /** `${source}|${league}` — the scope a complete snapshot is authoritative for. */
  scope: string;
  quoteKeys: Set<string>;
}

function scopeKey(source: QuoteSource, league: LeagueKey): string {
  return `${source}|${league}`;
}

function isFiniteNumber(x: unknown): x is number {
  return typeof x === 'number' && Number.isFinite(x);
}

function isNonEmptyString(x: unknown): x is string {
  return typeof x === 'string' && x.length > 0;
}

function positiveInt(value: number | undefined, fallback: number): number {
  return isFiniteNumber(value) && value >= 1 ? Math.floor(value) : fallback;
}

function nonNegative(value: number | undefined, fallback: number): number {
  return isFiniteNumber(value) && value >= 0 ? value : fallback;
}

function copyScore(score: LiveScore): LiveScore {
  const out: LiveScore = {
    home: isFiniteNumber(score.home) ? score.home : null,
    away: isFiniteNumber(score.away) ? score.away : null,
    updatedAt: isFiniteNumber(score.updatedAt) ? score.updatedAt : 0,
  };
  if (typeof score.period === 'string') out.period = score.period;
  if (typeof score.clock === 'string') out.clock = score.clock;
  return out;
}

function isValidRawEvent(ev: RawEvent | null | undefined): ev is RawEvent {
  return (
    !!ev &&
    typeof ev === 'object' &&
    isNonEmptyString(ev.sourceEventId) &&
    typeof ev.home === 'string' &&
    typeof ev.away === 'string' &&
    isFiniteNumber(ev.startTime) &&
    typeof ev.isLive === 'boolean'
  );
}

function isValidQuote(q: Quote | null | undefined): q is Quote {
  if (!q || typeof q !== 'object') return false;
  if (!isNonEmptyString(q.book) || !isNonEmptyString(q.sourceEventId)) return false;
  const sides = SIDES_BY_KIND[q.kind];
  if (!sides || !sides.has(q.side)) return false;
  if (q.kind === 'moneyline' ? q.line !== null : !isFiniteNumber(q.line)) return false;
  if (!isFiniteNumber(q.decimal) || q.decimal <= 1) return false;
  if (!isFiniteNumber(q.observedAt)) return false;
  return true;
}

export class MarketStore {
  private readonly historyMaxPoints: number;
  private readonly historyMaxAgeMs: number;
  private readonly eventRetentionMs: number;
  private readonly maxEvents: number;
  private readonly maxQuotesPerEvent: number;

  private readonly eventsById = new Map<string, EventEntry>();
  private readonly quotesByKey = new Map<string, StoredQuote>();
  /** scope (`source|league`) -> event ids in that scope. */
  private readonly scopes = new Map<string, Set<string>>();

  constructor(opts: MarketStoreOptions = {}) {
    this.historyMaxPoints = positiveInt(opts.historyMaxPoints, DEFAULT_HISTORY_MAX_POINTS);
    this.historyMaxAgeMs = nonNegative(opts.historyMaxAgeMs, DEFAULT_HISTORY_MAX_AGE_MS);
    this.eventRetentionMs = nonNegative(opts.eventRetentionMs, DEFAULT_EVENT_RETENTION_MS);
    this.maxEvents = positiveInt(opts.maxEvents, DEFAULT_MAX_EVENTS);
    this.maxQuotesPerEvent = positiveInt(opts.maxQuotesPerEvent, DEFAULT_MAX_QUOTES_PER_EVENT);
  }

  static eventId(source: QuoteSource, sourceEventId: string): string {
    return `${source}:${sourceEventId}`;
  }

  static quoteKey(eventId: string, q: Pick<Quote, 'book' | 'kind' | 'side' | 'line'>): string {
    return `${eventId}|${q.book}|${q.kind}|${q.side}|${q.line ?? ''}`;
  }

  ingest(snapshot: SourceSnapshot): IngestResult {
    const result: IngestResult = {
      eventsUpserted: 0,
      quotesUpserted: 0,
      quotesRemoved: 0,
      priceChanges: 0,
      quotesRejected: 0,
      eventsRemoved: 0,
    };
    if (
      !snapshot ||
      typeof snapshot !== 'object' ||
      !isNonEmptyString(snapshot.source) ||
      !isNonEmptyString(snapshot.league) ||
      !isFiniteNumber(snapshot.fetchedAt) ||
      !Array.isArray(snapshot.events) ||
      !Array.isArray(snapshot.quotes)
    ) {
      log.warn('Ignoring malformed snapshot');
      return result;
    }

    const { source, league, fetchedAt } = snapshot;
    const scope = scopeKey(source, league);
    let eventsRejected = 0;

    // 1. Events. sourceEventId -> canonical id, only for events actually present in this snapshot.
    const snapshotEvents = new Map<string, string>();
    for (const ev of snapshot.events) {
      if (!isValidRawEvent(ev) || ev.source !== source || ev.league !== league) {
        eventsRejected++;
        continue;
      }
      const id = MarketStore.eventId(source, ev.sourceEventId);
      this.upsertEvent(id, ev, scope, fetchedAt);
      snapshotEvents.set(ev.sourceEventId, id);
      result.eventsUpserted++;
    }

    // 2. Quotes.
    const seenKeys = new Set<string>();
    const touchedEvents = new Set<string>();
    let rejected = 0;
    for (const q of snapshot.quotes) {
      if (!isValidQuote(q) || q.source !== source) {
        rejected++;
        continue;
      }
      const eventId = snapshotEvents.get(q.sourceEventId);
      const entry = eventId === undefined ? undefined : this.eventsById.get(eventId);
      if (eventId === undefined || !entry) {
        rejected++;
        continue;
      }
      const key = MarketStore.quoteKey(eventId, q);
      seenKeys.add(key);
      const existing = this.quotesByKey.get(key);
      if (existing) {
        if (this.updateQuote(existing, q, fetchedAt)) result.priceChanges++;
      } else {
        this.insertQuote(entry, eventId, key, q);
        touchedEvents.add(eventId);
      }
      result.quotesUpserted++;
    }
    result.quotesRejected = rejected;

    // 3. Complete snapshot: authoritative for (source, league, books).
    if (snapshot.complete) {
      const books = new Set((Array.isArray(snapshot.books) ? snapshot.books : []).map((b) => String(b).toLowerCase()));
      const presentIds = new Set(snapshotEvents.values());
      const scopeIds = this.scopes.get(scope);
      if (scopeIds) {
        for (const eventId of scopeIds) {
          const entry = this.eventsById.get(eventId);
          if (!entry) continue;
          if (!presentIds.has(eventId)) entry.event.isLive = false;
          if (books.size === 0) continue;
          for (const key of entry.quoteKeys) {
            if (seenKeys.has(key)) continue;
            const stored = this.quotesByKey.get(key);
            if (!stored) {
              entry.quoteKeys.delete(key);
              continue;
            }
            if (!books.has(stored.book.toLowerCase())) continue;
            this.deleteQuote(entry, key);
            result.quotesRemoved++;
          }
        }
      }
    }

    // 4. Memory caps.
    for (const eventId of touchedEvents) {
      const entry = this.eventsById.get(eventId);
      if (entry) result.quotesRemoved += this.enforceQuoteCap(entry);
    }
    const capped = this.enforceMaxEvents();
    result.eventsRemoved = capped.eventsRemoved;
    result.quotesRemoved += capped.quotesRemoved;

    if (rejected > 0 || eventsRejected > 0) {
      log.debug(`${source}/${league}: skipped ${eventsRejected} event(s) and ${rejected} quote(s)`, {
        events: snapshot.events.length,
        quotes: snapshot.quotes.length,
      });
    }
    return result;
  }

  getEvent(eventId: string): CanonicalEvent | undefined {
    return this.eventsById.get(eventId)?.event;
  }

  events(): CanonicalEvent[] {
    const out: CanonicalEvent[] = [];
    for (const entry of this.eventsById.values()) out.push(entry.event);
    return out;
  }

  quotesForEvent(eventId: string): StoredQuote[] {
    const entry = this.eventsById.get(eventId);
    if (!entry) return [];
    const out: StoredQuote[] = [];
    for (const key of entry.quoteKeys) {
      const q = this.quotesByKey.get(key);
      if (q) out.push(q);
    }
    return out;
  }

  getQuote(key: string): StoredQuote | undefined {
    return this.quotesByKey.get(key);
  }

  /** Last known decimal price at or before time t (from history); null if unknown. */
  priceAt(q: StoredQuote, t: number): number | null {
    if (!q || !Array.isArray(q.history) || !isFiniteNumber(t)) return null;
    const h = q.history;
    for (let i = h.length - 1; i >= 0; i--) {
      if (h[i].t <= t) return h[i].decimal;
    }
    return null;
  }

  prune(at: number): { eventsRemoved: number; quotesRemoved: number } {
    const out = { eventsRemoved: 0, quotesRemoved: 0 };
    if (!isFiniteNumber(at)) return out;
    const seenCutoff = at - this.eventRetentionMs;
    const startCutoff = at - STARTED_EVENT_MAX_AGE_MS;
    for (const [id, entry] of this.eventsById) {
      if (entry.event.lastSeen < seenCutoff || entry.event.startTime < startCutoff) {
        out.quotesRemoved += this.removeEvent(id);
        out.eventsRemoved++;
      }
    }
    const capped = this.enforceMaxEvents();
    out.eventsRemoved += capped.eventsRemoved;
    out.quotesRemoved += capped.quotesRemoved;
    for (const q of this.quotesByKey.values()) this.trimHistory(q.history, at);
    return out;
  }

  stats(): { events: number; liveEvents: number; quotes: number } {
    let live = 0;
    for (const entry of this.eventsById.values()) if (entry.event.isLive) live++;
    return { events: this.eventsById.size, liveEvents: live, quotes: this.quotesByKey.size };
  }

  leagueOf(eventId: string): LeagueKey | undefined {
    return this.eventsById.get(eventId)?.event.league;
  }

  // ---------------------------------------------------------------------------------------------------------
  // internals

  private upsertEvent(id: string, ev: RawEvent, scope: string, fetchedAt: number): void {
    const existing = this.eventsById.get(id);
    if (!existing) {
      const event: CanonicalEvent = {
        id,
        league: ev.league,
        home: ev.home,
        away: ev.away,
        startTime: ev.startTime,
        isLive: ev.isLive,
        links: { [ev.source]: ev.sourceEventId },
        lastSeen: fetchedAt,
      };
      if (ev.score) event.score = copyScore(ev.score);
      this.eventsById.set(id, { event, scope, quoteKeys: new Set() });
      this.addToScope(scope, id);
      return;
    }
    const event = existing.event;
    event.league = ev.league;
    event.home = ev.home;
    event.away = ev.away;
    event.startTime = ev.startTime;
    event.isLive = ev.isLive;
    if (ev.score) event.score = copyScore(ev.score);
    else delete event.score;
    event.links[ev.source] = ev.sourceEventId;
    event.lastSeen = Math.max(event.lastSeen, fetchedAt);
    if (existing.scope !== scope) {
      this.removeFromScope(existing.scope, id);
      existing.scope = scope;
      this.addToScope(scope, id);
    }
  }

  private insertQuote(entry: EventEntry, eventId: string, key: string, q: Quote): void {
    const changeAt = isFiniteNumber(q.bookUpdatedAt) ? q.bookUpdatedAt : q.observedAt;
    const stored: StoredQuote = {
      book: q.book,
      source: q.source,
      sourceEventId: q.sourceEventId,
      kind: q.kind,
      side: q.side,
      line: q.line,
      decimal: q.decimal,
      suspended: q.suspended === true,
      isMainLine: q.isMainLine !== false,
      observedAt: q.observedAt,
      bookUpdatedAt: isFiniteNumber(q.bookUpdatedAt) ? q.bookUpdatedAt : null,
      eventId,
      key,
      history: [{ t: changeAt, decimal: q.decimal }],
      lastChangeAt: changeAt,
    };
    if (typeof q.link === 'string') stored.link = q.link;
    this.quotesByKey.set(key, stored);
    entry.quoteKeys.add(key);
  }

  /** Returns true when the price changed. */
  private updateQuote(stored: StoredQuote, q: Quote, now: number): boolean {
    const bookUpdatedAt = isFiniteNumber(q.bookUpdatedAt) ? q.bookUpdatedAt : null;
    let changed = false;
    if (roundOdds(stored.decimal) !== roundOdds(q.decimal)) {
      const last = stored.history[stored.history.length - 1];
      // History stays chronological even if a source reports an out-of-order update time.
      const changeAt = Math.max(bookUpdatedAt ?? q.observedAt, last ? last.t : Number.NEGATIVE_INFINITY);
      stored.decimal = q.decimal;
      stored.history.push({ t: changeAt, decimal: q.decimal });
      stored.lastChangeAt = changeAt;
      this.trimHistory(stored.history, now);
      changed = true;
    }
    stored.observedAt = q.observedAt;
    stored.bookUpdatedAt = bookUpdatedAt;
    stored.suspended = q.suspended === true;
    stored.isMainLine = q.isMainLine !== false;
    if (typeof q.link === 'string') stored.link = q.link;
    else delete stored.link;
    return changed;
  }

  /**
   * Keeps at most `historyMaxPoints` points and drops points older than `historyMaxAgeMs`, except the newest
   * point at or before the age cutoff (it is still the price in effect at the cutoff, so `priceAt` stays exact
   * for every t inside the window). The latest point is therefore always kept.
   */
  private trimHistory(history: PricePoint[], now: number): void {
    if (history.length > this.historyMaxPoints) history.splice(0, history.length - this.historyMaxPoints);
    if (history.length <= 1) return;
    const cutoff = now - this.historyMaxAgeMs;
    let anchor = -1;
    for (let i = 0; i < history.length && history[i].t <= cutoff; i++) anchor = i;
    if (anchor > 0) history.splice(0, anchor);
  }

  private deleteQuote(entry: EventEntry, key: string): void {
    this.quotesByKey.delete(key);
    entry.quoteKeys.delete(key);
  }

  /** Removes an event and its quotes. Returns the number of quotes removed. */
  private removeEvent(id: string): number {
    const entry = this.eventsById.get(id);
    if (!entry) return 0;
    let removed = 0;
    for (const key of entry.quoteKeys) {
      if (this.quotesByKey.delete(key)) removed++;
    }
    entry.quoteKeys.clear();
    this.removeFromScope(entry.scope, id);
    this.eventsById.delete(id);
    return removed;
  }

  private addToScope(scope: string, id: string): void {
    let ids = this.scopes.get(scope);
    if (!ids) {
      ids = new Set();
      this.scopes.set(scope, ids);
    }
    ids.add(id);
  }

  private removeFromScope(scope: string, id: string): void {
    const ids = this.scopes.get(scope);
    if (!ids) return;
    ids.delete(id);
    if (ids.size === 0) this.scopes.delete(scope);
  }

  /** Evicts the least recently observed quotes of an event beyond `maxQuotesPerEvent`. */
  private enforceQuoteCap(entry: EventEntry): number {
    const excess = entry.quoteKeys.size - this.maxQuotesPerEvent;
    if (excess <= 0) return 0;
    const quotes: StoredQuote[] = [];
    for (const key of entry.quoteKeys) {
      const q = this.quotesByKey.get(key);
      if (q) quotes.push(q);
      else entry.quoteKeys.delete(key);
    }
    quotes.sort((a, b) => a.observedAt - b.observedAt || (a.key < b.key ? -1 : a.key > b.key ? 1 : 0));
    let removed = 0;
    for (let i = 0; i < quotes.length && entry.quoteKeys.size > this.maxQuotesPerEvent; i++) {
      this.deleteQuote(entry, quotes[i].key);
      removed++;
    }
    return removed;
  }

  /** Drops the events with the oldest lastSeen until at most `maxEvents` remain. */
  private enforceMaxEvents(): { eventsRemoved: number; quotesRemoved: number } {
    const out = { eventsRemoved: 0, quotesRemoved: 0 };
    const excess = this.eventsById.size - this.maxEvents;
    if (excess <= 0) return out;
    const ordered = [...this.eventsById.values()].map((e) => e.event);
    ordered.sort(
      (a, b) => a.lastSeen - b.lastSeen || a.startTime - b.startTime || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0),
    );
    for (let i = 0; i < excess; i++) {
      out.quotesRemoved += this.removeEvent(ordered[i].id);
      out.eventsRemoved++;
    }
    return out;
  }
}

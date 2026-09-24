/**
 * Client for The Odds API v4 (https://the-odds-api.com), a licensed odds aggregator.
 *
 * This is the only place that talks to the odds provider. It never contacts a sportsbook.
 * Responsibilities: URL building, credit-usage tracking from response headers, a circuit breaker,
 * and mapping every failure to a typed OddsApiError. The API key never appears in logs, errors or health.
 */
import type { AppConfig } from '../config';
import type { LeagueDef, LeagueKey, SourceHealth, SourceSnapshot, SourceStatus } from '../types';
import { CircuitBreaker, HttpError, ParseError, fetchJson, redactUrl } from '../util/http';
import type { FetchJsonResult } from '../util/http';
import { createLogger } from '../util/logger';
import { AbortError } from '../util/retry';
import { parseOddsResponse } from './oddsApiParser';

const log = createLogger('odds-api');

export interface OddsApiUsage {
  remaining: number | null;
  used: number | null;
  last: number | null;
  updatedAt: number | null;
}

export interface OddsApiEventSummary {
  id: string;
  league: LeagueKey;
  home: string;
  away: string;
  startTime: number;
}

export type OddsApiErrorKind =
  | 'no-key'
  | 'invalid-key'
  | 'quota'
  | 'unavailable'
  | 'rate-limited'
  | 'network'
  | 'circuit-open'
  | 'bad-payload'
  /** The API rejected the request itself (e.g. INVALID_MARKET, INVALID_BOOKMAKERS): a configuration problem. */
  | 'bad-request';

export class OddsApiError extends Error {
  readonly kind: OddsApiErrorKind;
  readonly status: number | null;

  constructor(kind: OddsApiErrorKind, message: string, status: number | null = null) {
    super(message);
    this.name = 'OddsApiError';
    this.kind = kind;
    this.status = status;
  }
}

export interface OddsApiClientDeps {
  fetchJson?: typeof fetchJson;
  now?: () => number;
}

const SOURCE_NAME = 'The Odds API';
const BREAKER = { failureThreshold: 4, cooldownMs: 30_000, maxCooldownMs: 10 * 60_000 };
const INVALID_KEY_COOLDOWN_MS = 30 * 60_000;
const QUOTA_COOLDOWN_MS = 60 * 60_000;
const MAX_RATE_LIMIT_COOLDOWN_MS = 10 * 60_000;
const QUOTA_BODY = /quota|usage limit|usage credits|out_of_usage/i;
/** A rejected request (bad market, bookmaker or date parameter) is a config error: retry rarely until it is fixed. */
const BAD_REQUEST_COOLDOWN_MS = 10 * 60_000;
/** Error codes that mean "this sport key is not available", i.e. the league (not the request) is the problem. */
const SPORT_UNAVAILABLE_CODES = new Set(['UNKNOWN_SPORT', 'INVALID_SPORT', 'EVENT_NOT_FOUND']);
const MAX_EVENT_SUMMARIES = 2000;

interface HeaderReader {
  get(name: string): string | null;
}

function isHeaderReader(v: unknown): v is HeaderReader {
  return typeof v === 'object' && v !== null && typeof (v as { get?: unknown }).get === 'function';
}

/** Error objects may carry the failed response's headers (e.g. `err.headers`); use them when present. */
function headersOf(err: unknown): HeaderReader | null {
  if (typeof err !== 'object' || err === null) return null;
  const h = (err as { headers?: unknown }).headers;
  return isHeaderReader(h) ? h : null;
}

function headerNumber(h: HeaderReader, name: string): number | null {
  let raw: string | null;
  try {
    raw = h.get(name);
  } catch {
    return null;
  }
  if (raw === null || raw.trim() === '') return null;
  const n = Number(raw.trim());
  return Number.isFinite(n) && n >= 0 ? n : null;
}

/** `YYYY-MM-DDTHH:MM:SSZ` — the Odds API rejects timestamps with milliseconds. */
function isoSeconds(ms: number): string {
  return new Date(ms).toISOString().replace(/\.\d{3}Z$/, 'Z');
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

/** The Odds API `error_code` (e.g. "INVALID_MARKET") from an error body, when present. */
function apiErrorCode(body: string): string | null {
  const m = /"error_code"\s*:\s*"([A-Za-z0-9_]{1,64})"/.exec(body);
  return m ? m[1].toUpperCase() : null;
}

function errorMessage(err: unknown): string {
  if (err instanceof Error) return err.message;
  return typeof err === 'string' ? err : 'unknown error';
}

export class OddsApiClient {
  private readonly cfg: AppConfig['oddsApi'];
  private readonly fetchImpl: typeof fetchJson;
  private readonly now: () => number;
  private readonly breaker = new CircuitBreaker('odds-api', BREAKER);

  private _usage: OddsApiUsage = { remaining: null, used: null, last: null, updatedAt: null };
  private lastSuccess: number | null = null;
  private lastError: string | null = null;
  private consecutiveFailures = 0;
  private invalidKey = false;
  private quotaExhausted = false;
  /** The API rejected our request parameters (cleared by the next successful odds call). */
  private badRequest = false;

  constructor(cfg: AppConfig['oddsApi'], deps: OddsApiClientDeps = {}) {
    this.cfg = cfg;
    this.fetchImpl = deps.fetchJson ?? fetchJson;
    this.now = deps.now ?? (() => Date.now());
  }

  get enabled(): boolean {
    return this.cfg.apiKey.trim() !== '';
  }

  get usage(): OddsApiUsage {
    return { ...this._usage };
  }

  /** Credits one `/odds` call costs: markets × regions, where every 10 bookmakers count as one region. */
  costPerOddsCall(): number {
    return this.cfg.markets.length * Math.ceil(this.cfg.books.length / 10);
  }

  health(): SourceHealth {
    const base = {
      name: SOURCE_NAME,
      lastSuccess: this.lastSuccess,
      lastError: this.lastError,
      consecutiveFailures: this.consecutiveFailures,
    };
    if (!this.enabled) {
      return {
        ...base,
        status: 'disabled',
        detail:
          'No ODDS_API_KEY set, so live odds are off. Add it to .env, then run `docker compose up -d` (Docker) or restart ' +
          '`npm start`. Simulated odds: DEMO_MODE=true (Docker) or `npm run demo`.',
      };
    }

    const breaker = this.breaker.snapshot();
    const { remaining, used, last } = this._usage;
    const exhausted = this.quotaExhausted || remaining === 0;
    const lowCredits = remaining !== null && remaining > 0 && remaining <= this.cfg.reserveCredits;

    let status: SourceStatus = 'ok';
    if (breaker.state === 'open' || this.invalidKey || exhausted || this.badRequest) status = 'down';
    else if (this.consecutiveFailures > 0 || breaker.state === 'half-open' || lowCredits) status = 'degraded';

    const parts: string[] = [];
    parts.push(remaining === null ? 'credits remaining: unknown until the first request' : `${remaining} credits remaining`);
    if (used !== null) parts.push(`${used} used`);
    if (last !== null) parts.push(`last call cost ${last}`);
    parts.push(`${this.costPerOddsCall()} credits per odds call`);
    if (this.invalidKey) parts.push('API key rejected, check ODDS_API_KEY');
    if (this.badRequest) parts.push('request rejected by the API, check ODDS_API_MARKETS / ODDS_API_BOOKS (see the error below)');
    if (exhausted) parts.push('monthly credits exhausted');
    else if (lowCredits) parts.push(`credits at or below the ${this.cfg.reserveCredits} reserve`);
    if (breaker.state === 'open' && breaker.nextAttemptAt !== null) {
      parts.push(`paused after errors until ${isoSeconds(breaker.nextAttemptAt)}`);
    }
    return { ...base, status, detail: parts.join(' · ') };
  }

  /** Odds for one league (costs `costPerOddsCall()` credits). */
  async fetchOdds(league: LeagueDef, signal?: AbortSignal): Promise<SourceSnapshot> {
    const sport = this.preflight(league);
    let url =
      `${this.cfg.baseUrl}/v4/sports/${encodeURIComponent(sport)}/odds` +
      `?apiKey=${encodeURIComponent(this.cfg.apiKey.trim())}` +
      `&bookmakers=${this.cfg.books.map(encodeURIComponent).join(',')}` +
      `&markets=${this.cfg.markets.map(encodeURIComponent).join(',')}` +
      `&oddsFormat=decimal&dateFormat=iso` +
      `&commenceTimeTo=${this.commenceTimeTo()}`;
    if (this.cfg.includeLinks) url += '&includeLinks=true';

    const res = await this.call(url, league, signal);
    let snapshot: SourceSnapshot;
    try {
      snapshot = parseOddsResponse(res.data, league, this.cfg.books, this.now(), {
        linkState: this.cfg.linkState,
        maxMarketLagLiveMs: this.cfg.maxMarketLagLiveSec * 1000,
        maxMarketLagPrematchMs: this.cfg.maxMarketLagPrematchSec * 1000,
      });
    } catch (err) {
      throw this.failure(new OddsApiError('bad-payload', `${league.key} odds: ${this.sanitize(errorMessage(err))}`, res.status));
    }
    this.succeeded('odds');
    log.debug(`${league.key}: ${snapshot.events.length} events, ${snapshot.quotes.length} quotes`, {
      ms: res.durationMs,
      creditsRemaining: this._usage.remaining,
    });
    return snapshot;
  }

  /** Upcoming and live events for one league. The events endpoint costs 0 credits. */
  async fetchEvents(league: LeagueDef, signal?: AbortSignal): Promise<OddsApiEventSummary[]> {
    const sport = this.preflight(league);
    const url =
      `${this.cfg.baseUrl}/v4/sports/${encodeURIComponent(sport)}/events` +
      `?apiKey=${encodeURIComponent(this.cfg.apiKey.trim())}` +
      `&dateFormat=iso` +
      `&commenceTimeTo=${this.commenceTimeTo()}`;

    const res = await this.call(url, league, signal);
    if (!Array.isArray(res.data)) {
      const got = res.data === null ? 'null' : typeof res.data;
      throw this.failure(new OddsApiError('bad-payload', `${league.key} events: expected an array, got ${got}`, res.status));
    }
    const out: OddsApiEventSummary[] = [];
    const seen = new Set<string>();
    for (const item of res.data) {
      if (out.length >= MAX_EVENT_SUMMARIES) break;
      if (!isRecord(item)) continue;
      const { id, home_team: home, away_team: away, commence_time: commence } = item;
      if (typeof id !== 'string' || id === '' || seen.has(id)) continue;
      if (typeof home !== 'string' || home.trim() === '' || typeof away !== 'string' || away.trim() === '') continue;
      const startTime = typeof commence === 'string' ? Date.parse(commence) : Number.NaN;
      if (!Number.isFinite(startTime)) continue;
      seen.add(id);
      out.push({ id, league: league.key, home: home.trim(), away: away.trim(), startTime });
    }
    this.succeeded('events');
    return out;
  }

  /** Upper bound for event start times: now + prematch horizon, without milliseconds. */
  private commenceTimeTo(): string {
    return isoSeconds(this.now() + this.cfg.prematchHorizonHours * 3_600_000);
  }

  /** Checks that do not need a request. Returns the Odds API sport key. */
  private preflight(league: LeagueDef): string {
    if (!this.enabled) {
      throw new OddsApiError('no-key', 'No ODDS_API_KEY configured; The Odds API is disabled');
    }
    if (!league.oddsApiKey) {
      throw new OddsApiError('unavailable', `${league.key} has no Odds API sport key`);
    }
    return league.oddsApiKey;
  }

  /** One HTTP GET guarded by the circuit breaker. Records usage; maps and records failures. */
  private async call(url: string, league: LeagueDef, signal?: AbortSignal): Promise<FetchJsonResult<unknown>> {
    const trial = this.breaker.state === 'half-open';
    if (!this.breaker.canRequest()) {
      const next = this.breaker.snapshot().nextAttemptAt;
      throw new OddsApiError(
        'circuit-open',
        `The Odds API is paused after repeated errors${next !== null ? ` until ${isoSeconds(next)}` : ''}`,
      );
    }
    try {
      const res = await this.fetchImpl<unknown>(url, { timeoutMs: this.cfg.requestTimeoutMs, retries: 2, signal });
      this.readUsage(res.headers);
      log.debug(`GET ${this.sanitize(url)} -> ${res.status} in ${res.durationMs}ms`, { creditsRemaining: this._usage.remaining });
      return res;
    } catch (err) {
      if (err instanceof AbortError || signal?.aborted) {
        // Shutdown, not an API failure. Release a half-open trial slot so the breaker cannot wedge.
        if (trial) this.breaker.recordFailure(err, 0);
        throw err instanceof AbortError ? err : new AbortError();
      }
      const headers = headersOf(err);
      if (headers) this.readUsage(headers);
      throw this.mapError(err, league);
    }
  }

  private mapError(err: unknown, league: LeagueDef): OddsApiError {
    const what = `${league.key} (${league.oddsApiKey ?? 'no sport key'})`;
    if (err instanceof HttpError) {
      const status = err.status;
      const body = this.sanitize(err.bodySnippet).slice(0, 200);
      const suffix = body ? `: ${body}` : '';

      if (status === 401 || status === 403) {
        if (QUOTA_BODY.test(err.bodySnippet)) return this.quota(err, status, suffix);
        this.invalidKey = true;
        return this.failure(
          new OddsApiError('invalid-key', `The Odds API rejected the API key (HTTP ${status})${suffix}`, status),
          INVALID_KEY_COOLDOWN_MS,
        );
      }
      if (status === 429) {
        if (this._usage.remaining === 0 || QUOTA_BODY.test(err.bodySnippet)) return this.quota(err, status, suffix);
        const cooldown =
          err.retryAfterMs !== null && err.retryAfterMs > 0 ? Math.min(err.retryAfterMs, MAX_RATE_LIMIT_COOLDOWN_MS) : undefined;
        return this.failure(new OddsApiError('rate-limited', `The Odds API rate limit was hit (HTTP 429)${suffix}`, status), cooldown);
      }
      if (status === 400 || status === 404 || status === 422) {
        const code = apiErrorCode(err.bodySnippet);
        const sportProblem =
          code !== null ? SPORT_UNAVAILABLE_CODES.has(code) : status === 404 || /\bsport\b/i.test(err.bodySnippet);
        if (sportProblem) {
          // The API answered: this league's sport key is unknown or not offered right now. Not a breaker failure.
          this.breaker.recordSuccess();
          this.consecutiveFailures = 0;
          const e = new OddsApiError('unavailable', `${what} is unavailable on The Odds API (HTTP ${status})${suffix}`, status);
          log.info(e.message);
          return e;
        }
        // The request itself was rejected (INVALID_MARKET, INVALID_BOOKMAKERS, ...). That affects every league, so it
        // is reported as a failure with the API's reason instead of blaming (and skipping) the league.
        this.badRequest = true;
        return this.failure(
          new OddsApiError(
            'bad-request',
            `The Odds API rejected the request for ${what} (HTTP ${status}${code ? ` ${code}` : ''})${suffix}. ` +
              'Check ODDS_API_MARKETS (only h2h, spreads, totals) and ODDS_API_BOOKS in .env.',
            status,
          ),
          BAD_REQUEST_COOLDOWN_MS,
        );
      }
      return this.failure(new OddsApiError('network', `The Odds API request for ${what} failed (HTTP ${status})${suffix}`, status));
    }
    if (err instanceof ParseError) {
      return this.failure(new OddsApiError('bad-payload', `The Odds API returned invalid JSON for ${what}: ${this.sanitize(err.message)}`));
    }
    return this.failure(new OddsApiError('network', `The Odds API request for ${what} failed: ${this.sanitize(errorMessage(err))}`));
  }

  private quota(err: HttpError, status: number, suffix: string): OddsApiError {
    this.quotaExhausted = true;
    return this.failure(new OddsApiError('quota', `The Odds API usage quota is exhausted (HTTP ${status})${suffix}`, status), QUOTA_COOLDOWN_MS);
  }

  /**
   * Records a failed call: breaker failure (optionally opening it for `cooldownMs`), health counters, log line.
   * Returns the error so callers can `throw this.failure(...)`. Messages are already sanitized.
   */
  private failure(e: OddsApiError, cooldownMs?: number): OddsApiError {
    this.breaker.recordFailure(new Error(e.message), cooldownMs);
    this.consecutiveFailures++;
    this.lastError = e.message;
    log.warn(`${e.kind}: ${e.message}`);
    return e;
  }

  /** `odds` = an odds call succeeded (only that proves the market/bookmaker parameters are accepted). */
  private succeeded(what: 'odds' | 'events'): void {
    this.breaker.recordSuccess();
    this.lastSuccess = this.now();
    this.consecutiveFailures = 0;
    this.invalidKey = false;
    if (what === 'odds') this.badRequest = false;
    if (this._usage.remaining === null || this._usage.remaining > 0) this.quotaExhausted = false;
  }

  private readUsage(headers: unknown): void {
    if (!isHeaderReader(headers)) return;
    const remaining = headerNumber(headers, 'x-requests-remaining');
    const used = headerNumber(headers, 'x-requests-used');
    const last = headerNumber(headers, 'x-requests-last');
    if (remaining === null && used === null && last === null) return;
    const prev = this._usage;
    this._usage = {
      remaining: remaining ?? prev.remaining,
      used: used ?? prev.used,
      last: last ?? prev.last,
      updatedAt: this.now(),
    };
    if (remaining !== null && remaining > 0) this.quotaExhausted = false;
  }

  /** Removes the API key from any text that could reach logs, errors or the dashboard. */
  private sanitize(text: string): string {
    let out = redactUrl(text);
    const key = this.cfg.apiKey.trim();
    if (key.length >= 4) {
      out = out.split(key).join('***');
      const encoded = encodeURIComponent(key);
      if (encoded !== key) out = out.split(encoded).join('***');
    }
    return out;
  }
}

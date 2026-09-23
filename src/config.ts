import * as fs from 'node:fs';
import * as path from 'node:path';
import type { LeagueDef, LeagueKey, RuntimeSettings } from './types';

/**
 * Central configuration. Everything is driven by environment variables (see .env.example).
 * `loadConfig()` is pure with respect to the env object passed in, so tests can build configs freely.
 */

export const DEFAULT_LEAGUES: LeagueDef[] = [
  { key: 'NFL', name: 'NFL', oddsApiKey: 'americanfootball_nfl', threeWay: false },
  { key: 'NCAAF', name: 'College Football', oddsApiKey: 'americanfootball_ncaaf', threeWay: false },
  { key: 'NBA', name: 'NBA', oddsApiKey: 'basketball_nba', threeWay: false },
  { key: 'NCAAB', name: 'College Basketball', oddsApiKey: 'basketball_ncaab', threeWay: false },
  { key: 'WNBA', name: 'WNBA', oddsApiKey: 'basketball_wnba', threeWay: false },
  { key: 'MLB', name: 'MLB', oddsApiKey: 'baseball_mlb', threeWay: false },
  { key: 'NHL', name: 'NHL', oddsApiKey: 'icehockey_nhl', threeWay: false },
  { key: 'EPL', name: 'Premier League', oddsApiKey: 'soccer_epl', threeWay: true },
  { key: 'UCL', name: 'Champions League', oddsApiKey: 'soccer_uefa_champs_league', threeWay: true },
  { key: 'MLS', name: 'MLS', oddsApiKey: 'soccer_usa_mls', threeWay: true },
  { key: 'LALIGA', name: 'La Liga', oddsApiKey: 'soccer_spain_la_liga', threeWay: true },
  { key: 'SERIEA', name: 'Serie A', oddsApiKey: 'soccer_italy_serie_a', threeWay: true },
  { key: 'BUNDESLIGA', name: 'Bundesliga', oddsApiKey: 'soccer_germany_bundesliga', threeWay: true },
  { key: 'LIGUE1', name: 'Ligue 1', oddsApiKey: 'soccer_france_ligue_one', threeWay: true },
  { key: 'UFC', name: 'UFC / MMA', oddsApiKey: 'mma_mixed_martial_arts', threeWay: false },
];

export const DEFAULT_ENABLED_LEAGUES: LeagueKey[] = ['NFL', 'NCAAF', 'NBA', 'NCAAB', 'WNBA', 'MLB', 'NHL', 'EPL', 'UCL', 'MLS'];

/** Bookmakers requested from The Odds API. 10 books cost the same credits as one region. */
export const DEFAULT_ODDS_API_BOOKS = [
  'pinnacle',
  'draftkings',
  'fanduel',
  'betmgm',
  'williamhill_us',
  'betonlineag',
  'lowvig',
  'betrivers',
  'bovada',
  'fanatics',
];

export type DevigMethod = 'worst' | 'multiplicative' | 'additive' | 'power' | 'shin';
export type LogLevel = 'debug' | 'info' | 'warn' | 'error';
export type NotifyUrgency = 'critical' | 'high' | 'medium';

export interface AppConfig {
  demoMode: boolean;
  logLevel: LogLevel;
  dataDir: string;

  server: {
    host: string;
    port: number;
    user: string;
    /** Empty string = no auth (only acceptable when bound to localhost / behind a tunnel). */
    password: string;
  };

  leagues: LeagueDef[];

  oddsApi: {
    apiKey: string;
    baseUrl: string;
    books: string[];
    /** Books considered sharp, in priority order. The first one available is the reference. */
    sharpBooks: string[];
    markets: string[];
    monthlyCredits: number;
    /** Day of month the Odds API quota resets (1-28). */
    resetDayOfMonth: number;
    /** Keep this many credits in reserve and never spend them. */
    reserveCredits: number;
    minIntervalLiveSec: number;
    minIntervalPrematchSec: number;
    maxIntervalSec: number;
    requestTimeoutMs: number;
    /** Only spend credits on a league if it has an event starting within this many hours (or live). */
    prematchHorizonHours: number;
    /** Ask the feed for bookmaker deep links (used for the "Open in DraftKings" button). */
    includeLinks: boolean;
    /** Two-letter state used to fill `{state}` placeholders in bookmaker links, e.g. "nj". */
    linkState: string;
  };

  model: {
    devigMethod: DevigMethod;
    /** Minimum number of non-DK books for a consensus fair price when no sharp book is available. */
    minConsensusBooks: number;
    liveMaxSharpAgeSec: number;
    prematchMaxSharpAgeSec: number;
    /** Max age of a DraftKings quote before it is considered unreliable. */
    liveMaxDkAgeSec: number;
    prematchMaxDkAgeSec: number;
    /** Implied-probability move of the sharp book that counts as "steam". */
    staleMoveProb: number;
    /** Look-back window for detecting sharp moves the DK line has not followed. */
    staleWindowSec: number;
    /** Ignore edges above this (almost always bad data / mismatched lines). */
    maxPlausibleEv: number;
    /** Keep 'gone' opportunities visible for this long so the user sees them vanish. */
    goneRetentionSec: number;
    /** Ignore DK prices longer than this (e.g. +2000 longshots are too noisy). */
    maxDecimalOdds: number;
    includeAltLines: boolean;
  };

  notify: {
    discordWebhookUrl: string;
    ntfyUrl: string;
    telegramBotToken: string;
    telegramChatId: string;
    minUrgency: NotifyUrgency;
    /** Minimum seconds between notifications for the same opportunity id. */
    cooldownSec: number;
  };

  /** Initial runtime settings; dashboard edits override these and persist in DATA_DIR/settings.json. */
  defaults: RuntimeSettings;
}

type Env = Record<string, string | undefined>;

function str(env: Env, key: string, def: string): string {
  const v = env[key];
  return v === undefined || v.trim() === '' ? def : v.trim();
}

function num(env: Env, key: string, def: number, opts: { min?: number; max?: number; int?: boolean } = {}): number {
  const raw = env[key];
  if (raw === undefined || raw.trim() === '') return def;
  const n = Number(raw.trim());
  if (!Number.isFinite(n)) throw new Error(`Config ${key} must be a number, got "${raw}"`);
  if (opts.int && !Number.isInteger(n)) throw new Error(`Config ${key} must be an integer, got "${raw}"`);
  if (opts.min !== undefined && n < opts.min) throw new Error(`Config ${key} must be >= ${opts.min}, got ${n}`);
  if (opts.max !== undefined && n > opts.max) throw new Error(`Config ${key} must be <= ${opts.max}, got ${n}`);
  return n;
}

function bool(env: Env, key: string, def: boolean): boolean {
  const raw = env[key];
  if (raw === undefined || raw.trim() === '') return def;
  const v = raw.trim().toLowerCase();
  if (['1', 'true', 'yes', 'on'].includes(v)) return true;
  if (['0', 'false', 'no', 'off'].includes(v)) return false;
  throw new Error(`Config ${key} must be true/false, got "${raw}"`);
}

function list(env: Env, key: string, def: string[]): string[] {
  const raw = env[key];
  if (raw === undefined || raw.trim() === '') return def.slice();
  return raw
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean);
}

function oneOf<T extends string>(env: Env, key: string, def: T, allowed: readonly T[]): T {
  const v = str(env, key, def).toLowerCase() as T;
  if (!allowed.includes(v)) throw new Error(`Config ${key} must be one of ${allowed.join('|')}, got "${v}"`);
  return v;
}

/**
 * ODDS_API_SPORTS=NBA:basketball_nba,KBO:baseball_kbo overrides or adds Odds API sport keys; use NBA:none to disable one.
 */
function applyOverrides(leagues: LeagueDef[], raw: string | undefined): void {
  if (!raw || !raw.trim()) return;
  for (const pair of raw.split(',')) {
    const [k, v] = pair.split(':').map((s) => s.trim());
    if (!k || v === undefined) continue;
    const league = leagues.find((l) => l.key.toUpperCase() === k.toUpperCase());
    const value = v.toLowerCase() === 'none' ? null : v;
    if (league) {
      league.oddsApiKey = value;
    } else {
      leagues.push({
        key: k.toUpperCase(),
        name: k.toUpperCase(),
        oddsApiKey: value,
        threeWay: !!value && value.startsWith('soccer_'),
      });
    }
  }
}

export function loadConfig(env: Env = process.env, argv: string[] = process.argv): AppConfig {
  const leagues = DEFAULT_LEAGUES.map((l) => ({ ...l }));
  applyOverrides(leagues, env.ODDS_API_SPORTS);

  const enabledLeagues = list(env, 'LEAGUES', DEFAULT_ENABLED_LEAGUES).map((s) => s.toUpperCase());
  for (const key of enabledLeagues) {
    if (!leagues.some((l) => l.key === key)) {
      throw new Error(`LEAGUES contains unknown league "${key}". Known: ${leagues.map((l) => l.key).join(', ')}`);
    }
  }

  const books = list(env, 'ODDS_API_BOOKS', DEFAULT_ODDS_API_BOOKS).map((b) => b.toLowerCase());
  if (!books.includes('draftkings')) books.unshift('draftkings');

  const bankroll = num(env, 'BANKROLL', 1000, { min: 1 });

  return {
    demoMode: argv.includes('--demo') || bool(env, 'DEMO_MODE', false),
    logLevel: oneOf<LogLevel>(env, 'LOG_LEVEL', 'info', ['debug', 'info', 'warn', 'error']),
    dataDir: path.resolve(str(env, 'DATA_DIR', './data')),

    server: {
      host: str(env, 'HOST', '127.0.0.1'),
      port: num(env, 'PORT', 8080, { min: 1, max: 65535, int: true }),
      user: str(env, 'DASHBOARD_USER', 'admin'),
      password: env.DASHBOARD_PASSWORD ?? '',
    },

    leagues,

    oddsApi: {
      apiKey: str(env, 'ODDS_API_KEY', ''),
      baseUrl: str(env, 'ODDS_API_BASE_URL', 'https://api.the-odds-api.com').replace(/\/+$/, ''),
      books,
      sharpBooks: list(env, 'SHARP_BOOKS', ['pinnacle', 'betonlineag', 'lowvig']).map((b) => b.toLowerCase()),
      markets: list(env, 'ODDS_API_MARKETS', ['h2h', 'spreads', 'totals']),
      monthlyCredits: num(env, 'ODDS_API_MONTHLY_CREDITS', 20000, { min: 1 }),
      resetDayOfMonth: num(env, 'ODDS_API_RESET_DAY', 1, { min: 1, max: 28, int: true }),
      reserveCredits: num(env, 'ODDS_API_RESERVE_CREDITS', 200, { min: 0 }),
      minIntervalLiveSec: num(env, 'ODDS_API_MIN_INTERVAL_LIVE_SEC', 20, { min: 5 }),
      minIntervalPrematchSec: num(env, 'ODDS_API_MIN_INTERVAL_PREMATCH_SEC', 180, { min: 30 }),
      maxIntervalSec: num(env, 'ODDS_API_MAX_INTERVAL_SEC', 1800, { min: 60 }),
      requestTimeoutMs: num(env, 'ODDS_API_TIMEOUT_MS', 15000, { min: 1000 }),
      prematchHorizonHours: num(env, 'ODDS_API_PREMATCH_HORIZON_HOURS', 24, { min: 1 }),
      includeLinks: bool(env, 'ODDS_API_INCLUDE_LINKS', true),
      linkState: str(env, 'BOOK_STATE', '').toLowerCase(),
    },

    model: {
      devigMethod: oneOf<DevigMethod>(env, 'DEVIG_METHOD', 'worst', ['worst', 'multiplicative', 'additive', 'power', 'shin']),
      minConsensusBooks: num(env, 'MIN_CONSENSUS_BOOKS', 3, { min: 1, int: true }),
      liveMaxSharpAgeSec: num(env, 'LIVE_MAX_SHARP_AGE_SEC', 45, { min: 5 }),
      prematchMaxSharpAgeSec: num(env, 'PREMATCH_MAX_SHARP_AGE_SEC', 900, { min: 30 }),
      liveMaxDkAgeSec: num(env, 'LIVE_MAX_DK_AGE_SEC', 45, { min: 5 }),
      prematchMaxDkAgeSec: num(env, 'PREMATCH_MAX_DK_AGE_SEC', 600, { min: 30 }),
      staleMoveProb: num(env, 'STALE_MOVE_PROB', 0.02, { min: 0.001, max: 0.5 }),
      staleWindowSec: num(env, 'STALE_WINDOW_SEC', 120, { min: 10 }),
      maxPlausibleEv: num(env, 'MAX_PLAUSIBLE_EV', 0.25, { min: 0.01 }),
      goneRetentionSec: num(env, 'GONE_RETENTION_SEC', 90, { min: 0 }),
      maxDecimalOdds: num(env, 'MAX_DECIMAL_ODDS', 11, { min: 1.5 }),
      includeAltLines: bool(env, 'INCLUDE_ALT_LINES', false),
    },

    notify: {
      discordWebhookUrl: str(env, 'DISCORD_WEBHOOK_URL', ''),
      ntfyUrl: str(env, 'NTFY_URL', ''),
      telegramBotToken: str(env, 'TELEGRAM_BOT_TOKEN', ''),
      telegramChatId: str(env, 'TELEGRAM_CHAT_ID', ''),
      minUrgency: oneOf<NotifyUrgency>(env, 'NOTIFY_MIN_URGENCY', 'critical', ['critical', 'high', 'medium']),
      cooldownSec: num(env, 'NOTIFY_COOLDOWN_SEC', 300, { min: 0 }),
    },

    defaults: {
      bankroll,
      kellyMultiplier: num(env, 'KELLY_MULTIPLIER', 0.25, { min: 0.01, max: 1 }),
      maxStakePct: num(env, 'MAX_STAKE_PCT', 0.02, { min: 0.001, max: 1 }),
      maxStakeAbs: num(env, 'MAX_STAKE_ABS', 100, { min: 1 }),
      maxDailyExposurePct: num(env, 'MAX_DAILY_EXPOSURE_PCT', 0.15, { min: 0.001, max: 1 }),
      minEvPrematch: num(env, 'MIN_EV_PREMATCH', 0.02, { min: 0, max: 1 }),
      minEvLive: num(env, 'MIN_EV_LIVE', 0.03, { min: 0, max: 1 }),
      watchEv: num(env, 'WATCH_EV', 0.005, { min: 0, max: 1 }),
      enabledLeagues,
      showArbs: bool(env, 'SHOW_ARBS', true),
    },
  };
}

/**
 * Loads `.env` (if present) into process.env without overriding variables already set,
 * using Node's built-in loader so there is no runtime dependency.
 */
export function loadDotEnv(file = path.resolve(process.cwd(), '.env')): void {
  try {
    if (fs.existsSync(file)) process.loadEnvFile(file);
  } catch (err) {
    // A malformed .env must not crash the process; the config validator reports bad values.
    console.error(`[config] could not read ${file}: ${(err as Error).message}`);
  }
}

export function leagueByKey(config: AppConfig, key: LeagueKey): LeagueDef | undefined {
  return config.leagues.find((l) => l.key === key);
}

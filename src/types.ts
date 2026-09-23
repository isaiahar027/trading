/**
 * Shared domain contracts. Every module speaks in these types.
 *
 * Conventions
 *  - Times are epoch milliseconds (number) unless the field name ends in `Sec`.
 *  - Odds are stored as decimal odds (> 1). American odds are derived for display.
 *  - Spread `line` is always from the perspective of the quoted side
 *    (home -3.5 has line -3.5; the matching away quote has line +3.5).
 *  - Total `line` is the total itself (224.5) for both Over and Under.
 *  - Moneyline `line` is null.
 */

export type LeagueKey = string; // e.g. 'NBA', 'NFL', 'EPL'

export type MarketKind = 'moneyline' | 'spread' | 'total';

export type Side = 'home' | 'away' | 'draw' | 'over' | 'under';

export type QuoteSource = 'odds-api' | 'demo';

export interface LeagueDef {
  key: LeagueKey;
  name: string;
  /** The Odds API sport key. null = no sharp reference available. */
  oddsApiKey: string | null;
  /** Soccer style moneyline with a draw outcome. */
  threeWay: boolean;
}

export interface LiveScore {
  home: number | null;
  away: number | null;
  /** Free text such as "Q3", "2nd Half", "Top 7th". */
  period?: string;
  /** Free text game clock such as "04:12". */
  clock?: string;
  updatedAt: number;
}

/** An event as reported by a single source, before cross-source matching. */
export interface RawEvent {
  source: QuoteSource;
  /** DraftKings event id, Odds API event id, or demo id. */
  sourceEventId: string;
  league: LeagueKey;
  home: string;
  away: string;
  startTime: number;
  isLive: boolean;
  score?: LiveScore;
}

/** One price for one outcome at one bookmaker. */
export interface Quote {
  /** Bookmaker key: 'draftkings', 'pinnacle', 'fanduel', ... (Odds API keys). */
  book: string;
  source: QuoteSource;
  /** Must equal the `sourceEventId` of a RawEvent in the same snapshot. */
  sourceEventId: string;
  kind: MarketKind;
  side: Side;
  line: number | null;
  /** Decimal odds, strictly > 1. */
  decimal: number;
  /** True when the book has the market locked/suspended. Never suggest a suspended quote. */
  suspended: boolean;
  /** Main (featured) line vs alternate line. Sources without the notion report true. */
  isMainLine: boolean;
  /** When this process received the price. */
  observedAt: number;
  /** When the bookmaker last changed it, if the source says so (Odds API `last_update`). */
  bookUpdatedAt: number | null;
  /** Bookmaker deep link for this outcome/market/event when the feed provides one. */
  link?: string;
}

/** Everything one poll of one source returned for one league. */
export interface SourceSnapshot {
  source: QuoteSource;
  league: LeagueKey;
  fetchedAt: number;
  events: RawEvent[];
  quotes: Quote[];
  /**
   * When true the snapshot is the complete current board for (source, league, books):
   * quotes previously stored for this source+league that are absent now should be dropped.
   */
  complete: boolean;
  /** Books this snapshot is authoritative for (used together with `complete`). */
  books: string[];
}

/** An event after cross-source matching. */
export interface CanonicalEvent {
  id: string;
  league: LeagueKey;
  home: string;
  away: string;
  startTime: number;
  isLive: boolean;
  score?: LiveScore;
  /** Map of source -> sourceEventId linked to this canonical event. */
  links: Partial<Record<QuoteSource, string>>;
  lastSeen: number;
}

export type OppType = 'ev' | 'arb';
export type Urgency = 'critical' | 'high' | 'medium' | 'low';
export type Verdict = 'BET_NOW' | 'BET' | 'WATCH';

export interface ArbLeg {
  book: string;
  side: Side;
  line: number | null;
  decimal: number;
  american: number;
  stake: number;
}

export interface Opportunity {
  /** Stable id: `${eventId}|${type}|${kind}|${side}|${line ?? ''}` */
  id: string;
  type: OppType;
  eventId: string;
  league: LeagueKey;
  /** "Away @ Home" */
  eventName: string;
  home: string;
  away: string;
  startTime: number;
  isLive: boolean;
  score?: LiveScore;
  kind: MarketKind;
  side: Side;
  line: number | null;
  /** Human readable pick: "Celtics -3.5", "Over 224.5", "Knicks ML", "Draw". */
  pick: string;
  dkDecimal: number;
  dkAmerican: number;
  /** De-vigged fair win probability of the pick. */
  fairProb: number;
  fairDecimal: number;
  fairAmerican: number;
  /** Expected value per $1 staked at the DK price, e.g. 0.034 = +3.4%. */
  evPct: number;
  /** Worst American price that still clears the minimum EV threshold. */
  minAcceptableAmerican: number;
  /** Fraction of bankroll to stake after Kelly multiplier, confidence and caps. */
  kellyFraction: number;
  /** Suggested stake in dollars (rounded). 0 when caps/exposure block it. */
  stake: number;
  /** 0..1 */
  confidence: number;
  urgency: Urgency;
  /** 0..100 */
  urgencyScore: number;
  verdict: Verdict;
  /** Short plain-English bullets explaining the call. */
  reasons: string[];
  /** 'pinnacle' or 'consensus(5)' */
  sharpSource: string;
  sharpAgeSec: number;
  dkAgeSec: number;
  /** Sharp market moved recently while DraftKings has not caught up. */
  staleLine: boolean;
  firstSeen: number;
  lastSeen: number;
  status: 'active' | 'gone';
  /** Rough estimate of how long the price is likely to survive. */
  expiresInSec: number;
  dkUrl: string | null;
  /** Present for type === 'arb': the two legs (DK leg first). */
  arb?: { legs: ArbLeg[]; profitPct: number; totalStake: number };
}

export type SourceStatus = 'ok' | 'degraded' | 'down' | 'disabled';

export interface SourceHealth {
  name: string;
  status: SourceStatus;
  lastSuccess: number | null;
  lastError: string | null;
  consecutiveFailures: number;
  detail?: string;
}

export interface Health {
  startedAt: number;
  now: number;
  demoMode: boolean;
  sources: SourceHealth[];
  oddsApiCreditsRemaining: number | null;
  oddsApiCreditsUsed: number | null;
  eventsTracked: number;
  liveEvents: number;
  quotesTracked: number;
  memoryMb: number;
  lastEngineRunMs: number | null;
  engineRunDurationMs: number | null;
}

/** User adjustable at runtime from the dashboard; persisted to DATA_DIR/settings.json. */
export interface RuntimeSettings {
  bankroll: number;
  /** Fraction of full Kelly, e.g. 0.25 */
  kellyMultiplier: number;
  /** Max stake as a fraction of bankroll, e.g. 0.02 */
  maxStakePct: number;
  /** Absolute max stake in dollars. */
  maxStakeAbs: number;
  /** Max total stake placed per calendar day as a fraction of bankroll. */
  maxDailyExposurePct: number;
  minEvPrematch: number;
  minEvLive: number;
  /** Opportunities between watchEv and minEv are shown as WATCH. */
  watchEv: number;
  enabledLeagues: LeagueKey[];
  showArbs: boolean;
}

export type BetResult = 'pending' | 'won' | 'lost' | 'push' | 'void';

export interface BetRecord {
  id: string;
  placedAt: number;
  opportunityId: string;
  eventId: string;
  league: LeagueKey;
  eventName: string;
  startTime: number;
  pick: string;
  kind: MarketKind;
  side: Side;
  line: number | null;
  wasLive: boolean;
  /** Odds the user actually got. */
  americanTaken: number;
  decimalTaken: number;
  stake: number;
  /** Model numbers at the time of placing. */
  fairProbAtPlace: number;
  evPctAtPlace: number;
  /** Closing fair probability when captured (for CLV). */
  closingFairProb: number | null;
  result: BetResult;
  settledAt: number | null;
  /** Profit in dollars once settled (negative for losses). */
  profit: number | null;
  notes?: string;
}

export interface BetSummary {
  totalBets: number;
  pending: number;
  staked: number;
  profit: number;
  roiPct: number | null;
  /** Average EV at placement across all bets. */
  avgEvPct: number | null;
  /** Average closing line value where available. */
  avgClvPct: number | null;
  stakedToday: number;
}

/** Payload pushed to dashboards on every engine tick. */
export interface DashboardState {
  generatedAt: number;
  opportunities: Opportunity[];
  health: Health;
  settings: RuntimeSettings;
  betSummary: BetSummary;
  /** Remaining stake allowed today under maxDailyExposurePct. */
  remainingDailyExposure: number;
  /** Every configured league (for the dashboard's league pickers), in configuration order. */
  leagues?: { key: LeagueKey; name: string }[];
}

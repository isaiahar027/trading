/**
 * Deterministic simulated odds feed for demo mode (`npm run demo`) and tests. No network, no real prices.
 *
 * Every event has a latent "true" state: an expected home margin and an expected total that random-walk (gently
 * before the game with occasional steam moves, harder in-game), plus a live score produced by a scoring process that is
 * consistent with those expectations. Fair probabilities for the home moneyline, the home spread and the total follow
 * from the current score and the distribution of what is still to be scored (normal for basketball / football /
 * baseball, Poisson for hockey / soccer, which also gives soccer its draw price). Main lines follow the expected final
 * margin and total, so live lines move with the game as they do at real books.
 *
 * Books price that fair state with their own margin and reaction speed:
 *   - pinnacle: ~2% margin, reprices on every tick (the sharp reference),
 *   - draftkings: ~4.5% margin, reprices 5–90 s after a move (sometimes minutes later), with an own opinion that
 *     drifts slowly (so a soft price lasts minutes instead of flickering),
 *   - 2–3 other books from the configured list: ~4–5% margin, their own lags and opinions.
 * Big in-game swings make the lagging books suspend their board until they reprice. This regularly yields +EV
 * DraftKings prices, stale DraftKings lines after a Pinnacle jump, the odd DraftKings vs other-book arb, and plenty of
 * ordinary no-edge prices. Finished games roll into new pre-match games, so the feed runs forever with a fixed number
 * of events (memory stays flat).
 */
import type { LeagueDef, LiveScore, MarketKind, Quote, RawEvent, Side, SourceSnapshot } from '../types';
import { americanToDecimal, decimalToAmerican } from '../util/odds';

export interface DemoFeedOptions {
  seed?: number;
}

const DEFAULT_SEED = 0x5eed;
const DK_BOOK = 'draftkings';
const SHARP_BOOK = 'pinnacle';
const DK_DEMO_LINK = 'https://sportsbook.draftkings.com/';
const PREFERRED_LEAGUES = ['NBA', 'NFL', 'MLB', 'NHL', 'EPL'];
const EVENTS_PER_LEAGUE: Record<string, number> = { NBA: 3, NFL: 3, MLB: 3, NHL: 2, EPL: 3 };
const DEFAULT_EVENTS_PER_LEAGUE = 3;
const MAX_EVENTS = 14;
const FALLBACK_LEAGUE_COUNT = 5;
/** Sharp references in the default config; not used as "soft" demo books when others are available. */
const SHARP_LIKE = new Set(['pinnacle', 'draftkings', 'betonlineag', 'lowvig']);
const FALLBACK_OTHER_BOOKS = ['fanduel', 'betmgm', 'williamhill_us'];
const MAX_OTHER_BOOKS = 3;
const MIN_OTHER_BOOKS = 2;

const MINUTE_MS = 60_000;
const HOUR_MS = 60 * MINUTE_MS;
/** Longest stretch of simulated time one tick advances the model by (e.g. after the host slept). */
const MAX_STEP_SEC = 60;
/** Step used to play games that are already in progress when the feed starts. */
const WARMUP_STEP_SEC = 20;

/** Random-walk volatility of the expected margin/total, as a fraction of the sport's sd, per sqrt(second). */
const PREMATCH_DRIFT = 0.0006;
const LIVE_DRIFT = 0.0015;
/** Pre-match steam: one sharp move per event every ~2 h of 6–15% of the sport's sd. */
const STEAM_MEAN_SEC = 2 * 3600;
const STEAM_MIN = 0.06;
const STEAM_MAX = 0.15;
/** In-game swings of at least this much win/cover/over probability make lagging books suspend until they reprice. */
const SUSPEND_JUMP = 0.08;
const SUSPEND_MIN_SEC = 8;
/**
 * How much of a lagging book's own opinion survives each reprice. Real books keep a view for a while instead of
 * re-rolling it every few seconds, so a soft price lasts minutes (and does not flicker in and out of the board).
 */
const BIAS_PERSISTENCE = 0.85;
const SUSPEND_MAX_SEC = 40;
/** Poisson support for hockey/soccer goals still to be scored per team. */
const MAX_GOALS = 15;

type SportKind = 'basketball' | 'football' | 'baseball' | 'hockey' | 'soccer';

interface SportProfile {
  kind: SportKind;
  /** Real-time length of a game once it starts. */
  durationMin: number;
  /** Goals-style sport modelled with Poisson scoring (hockey, soccer). */
  poisson: boolean;
  /** [points, probability] of one scoring play. */
  points: ReadonlyArray<readonly [number, number]>;
  /** Standard deviation of the final home margin / total at kick-off (normal model only). */
  marginSd: number;
  totalSd: number;
  /** Pre-game expected home margin and total are drawn from these ranges. */
  marginRange: readonly [number, number];
  totalRange: readonly [number, number];
  /** Pre-match run/puck line magnitude (0 = spread follows the expected margin). */
  fixedSpread: number;
  /** Distance of the alternate spread from the main line. */
  altOffset: number;
  /** A main line moves once the expected value is this far from it. */
  hysteresis: number;
  /** Scale of the random walks of the expected margin / total. */
  driftMarginSd: number;
  driftTotalSd: number;
}

const SPORTS: Record<SportKind, SportProfile> = {
  basketball: {
    kind: 'basketball',
    durationMin: 150,
    poisson: false,
    points: [
      [2, 0.72],
      [3, 0.22],
      [1, 0.06],
    ],
    marginSd: 12,
    totalSd: 18,
    marginRange: [-8, 10],
    totalRange: [212, 236],
    fixedSpread: 0,
    altOffset: 2,
    hysteresis: 0.75,
    driftMarginSd: 12,
    driftTotalSd: 18,
  },
  football: {
    kind: 'football',
    durationMin: 190,
    poisson: false,
    points: [
      [7, 0.6],
      [3, 0.4],
    ],
    marginSd: 13.5,
    totalSd: 13.5,
    marginRange: [-7, 9],
    totalRange: [38, 52],
    fixedSpread: 0,
    altOffset: 3,
    hysteresis: 0.75,
    driftMarginSd: 13.5,
    driftTotalSd: 13.5,
  },
  baseball: {
    kind: 'baseball',
    durationMin: 175,
    poisson: false,
    points: [
      [1, 0.7],
      [2, 0.2],
      [3, 0.08],
      [4, 0.02],
    ],
    marginSd: 4.2,
    totalSd: 4.3,
    marginRange: [-1.2, 1.4],
    totalRange: [7, 10],
    fixedSpread: 1.5,
    altOffset: 1,
    hysteresis: 0.6,
    driftMarginSd: 4.2,
    driftTotalSd: 4.3,
  },
  hockey: {
    kind: 'hockey',
    durationMin: 150,
    poisson: true,
    points: [[1, 1]],
    marginSd: 0,
    totalSd: 0,
    marginRange: [-0.9, 0.9],
    totalRange: [5.5, 6.8],
    fixedSpread: 1.5,
    altOffset: 1,
    hysteresis: 0.6,
    driftMarginSd: 2.3,
    driftTotalSd: 2.4,
  },
  soccer: {
    kind: 'soccer',
    durationMin: 115,
    poisson: true,
    points: [[1, 1]],
    marginSd: 0,
    totalSd: 0,
    marginRange: [-1, 1.2],
    totalRange: [2.3, 3.2],
    fixedSpread: 0,
    altOffset: 1,
    hysteresis: 0.6,
    driftMarginSd: 1.6,
    driftTotalSd: 1.6,
  },
};

const TEAMS: Record<string, readonly string[]> = {
  NBA: [
    'Boston Celtics', 'New York Knicks', 'Milwaukee Bucks', 'Philadelphia 76ers', 'Miami Heat', 'Cleveland Cavaliers',
    'Denver Nuggets', 'Los Angeles Lakers', 'Golden State Warriors', 'Phoenix Suns', 'Dallas Mavericks',
    'Oklahoma City Thunder', 'Minnesota Timberwolves', 'Sacramento Kings', 'Indiana Pacers', 'Orlando Magic',
  ],
  NFL: [
    'Kansas City Chiefs', 'Buffalo Bills', 'Philadelphia Eagles', 'San Francisco 49ers', 'Dallas Cowboys',
    'Baltimore Ravens', 'Detroit Lions', 'Miami Dolphins', 'Cincinnati Bengals', 'Green Bay Packers',
    'Los Angeles Rams', 'Houston Texans', 'Pittsburgh Steelers', 'Seattle Seahawks',
  ],
  MLB: [
    'New York Yankees', 'Boston Red Sox', 'Los Angeles Dodgers', 'Atlanta Braves', 'Houston Astros',
    'Philadelphia Phillies', 'Chicago Cubs', 'San Diego Padres', 'Seattle Mariners', 'Toronto Blue Jays',
    'Baltimore Orioles', 'New York Mets', 'Milwaukee Brewers', 'Cleveland Guardians',
  ],
  NHL: [
    'Boston Bruins', 'Toronto Maple Leafs', 'New York Rangers', 'Florida Panthers', 'Colorado Avalanche',
    'Edmonton Oilers', 'Vegas Golden Knights', 'Dallas Stars', 'Carolina Hurricanes', 'Tampa Bay Lightning',
  ],
  EPL: [
    'Arsenal', 'Manchester City', 'Liverpool', 'Chelsea', 'Tottenham Hotspur', 'Manchester United',
    'Newcastle United', 'Aston Villa', 'Brighton', 'West Ham United', 'Brentford', 'Crystal Palace',
  ],
};

const GENERIC_TEAMS: readonly string[] = [
  'Riverton', 'Lakeside', 'Hillcrest', 'Bayport', 'Fairview', 'Northgate', 'Westbrook', 'Eastwood',
  'Kingsford', 'Maple Ridge', 'Stonebridge', 'Clearwater', 'Oakdale', 'Pinecrest', 'Redfield', 'Silver Lake',
];

interface BookProfile {
  key: string;
  margin: number;
  /** Reprices on every tick (sharp reference). */
  instant: boolean;
  /** Standard deviation (logit) of the book's own opinion; it drifts at every reprice (see BIAS_PERSISTENCE). */
  biasSd: number;
  /** American odds rounding (1 = whole numbers, 5 = multiples of 5). */
  roundTo: number;
  /** Usual reaction lag, skewed toward the short end. */
  lagMinSec: number;
  lagMaxSec: number;
  /** Chance that a reprice comes much later than usual. */
  slowChance: number;
  slowMinSec: number;
  slowMaxSec: number;
  link?: string;
  /** Quotes the alternate spread on events that have one. */
  altLines: boolean;
}

interface BookMarket {
  /**
   * The book's own opinion on this market (logit shift): an AR(1) process, so a mispriced market stays mispriced for
   * a few minutes.
   */
  bias: number;
  line: number | null;
  decimals: number[];
  altLine: number | null;
  altDecimals: number[] | null;
  updatedAt: number;
  nextRepriceAt: number;
  suspended: boolean;
}

interface DemoEvent {
  id: string;
  home: string;
  away: string;
  startTime: number;
  durationMs: number;
  live: boolean;
  score: { home: number; away: number; updatedAt: number } | null;
  /** Pre-game expected home margin and total. */
  expMargin0: number;
  expTotal0: number;
  /** Random-walk adjustments to the expected margin / total (news, steam, momentum). */
  marginShift: number;
  totalShift: number;
  /** Current main lines (home spread line; total). */
  spreadLine: number;
  totalLine: number;
  /** Signed offset of the alternate spread from the main line (0 = no alternate line). */
  altOffset: number;
  /** Per book (aligned with DemoFeed.books), per market kind (aligned with KINDS). */
  markets: BookMarket[][];
}

interface LeagueSlots {
  league: LeagueDef;
  sport: SportProfile;
  threeWay: boolean;
  teams: readonly string[];
  events: DemoEvent[];
  nextSerial: number;
}

/** Fair probabilities of an event at one moment. */
interface FairModel {
  pHome: number;
  /** 0 for two-way moneylines. */
  pDraw: number;
  /** P(home covers `line`), line from the home side's perspective (-3.5 = home gives 3.5). */
  cover(line: number): number;
  /** P(final total > line). */
  over(line: number): number;
}

interface PricedMarket {
  line: number | null;
  decimals: number[];
  altLine: number | null;
  altDecimals: number[] | null;
}

const KINDS: readonly MarketKind[] = ['moneyline', 'spread', 'total'];

function mulberry32(seed: number): () => number {
  let a = seed | 0;
  return () => {
    a = (a + 0x6d2b79f5) | 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function clamp(x: number, lo: number, hi: number): number {
  return Math.min(hi, Math.max(lo, x));
}

function sigmoid(x: number): number {
  return 1 / (1 + Math.exp(-x));
}

function logit(p: number): number {
  const q = clamp(p, 1e-6, 1 - 1e-6);
  return Math.log(q / (1 - q));
}

/** Standard normal CDF (Abramowitz & Stegun 7.1.26, |error| < 1.5e-7). */
function normCdf(x: number): number {
  const z = Math.abs(x) / Math.SQRT2;
  const t = 1 / (1 + 0.3275911 * z);
  const poly = t * (0.254829592 + t * (-0.284496736 + t * (1.421413741 + t * (-1.453152027 + t * 1.061405429))));
  const erf = 1 - poly * Math.exp(-z * z);
  return x >= 0 ? 0.5 * (1 + erf) : 0.5 * (1 - erf);
}

function poissonPmf(lambda: number): number[] {
  const out = new Array<number>(MAX_GOALS + 1);
  let p = Math.exp(-lambda);
  for (let k = 0; k <= MAX_GOALS; k++) {
    out[k] = p;
    p = (p * lambda) / (k + 1);
  }
  return out;
}

/** Nearest x.5 line to `x`. */
function halfLine(x: number): number {
  return Math.floor(x) + 0.5;
}

function ordinal(n: number): string {
  if (n === 1) return '1st';
  if (n === 2) return '2nd';
  if (n === 3) return '3rd';
  return `${n}th`;
}

function mmss(ms: number): string {
  const total = Math.max(0, Math.round(ms / 1000));
  return `${String(Math.floor(total / 60)).padStart(2, '0')}:${String(total % 60).padStart(2, '0')}`;
}

function sportFor(league: LeagueDef): SportProfile {
  const key = league.key.toUpperCase();
  const sportKey = (league.oddsApiKey ?? '').toLowerCase();
  if (key === 'NBA' || sportKey.startsWith('basketball')) return SPORTS.basketball;
  if (key === 'NFL' || sportKey.startsWith('americanfootball')) return SPORTS.football;
  if (key === 'MLB' || sportKey.startsWith('baseball')) return SPORTS.baseball;
  if (key === 'NHL' || sportKey.startsWith('icehockey')) return SPORTS.hockey;
  if (key === 'EPL' || sportKey.startsWith('soccer') || league.threeWay) return SPORTS.soccer;
  return SPORTS.hockey;
}

function avgPoints(sport: SportProfile): number {
  return sport.points.reduce((sum, [pts, p]) => sum + pts * p, 0);
}

function sidesFor(kind: MarketKind, threeWay: boolean): Side[] {
  if (kind === 'moneyline') return threeWay ? ['home', 'draw', 'away'] : ['home', 'away'];
  if (kind === 'spread') return ['home', 'away'];
  return ['over', 'under'];
}

function lineFor(kind: MarketKind, side: Side, line: number | null): number | null {
  if (kind === 'moneyline' || line === null) return null;
  if (kind === 'spread' && side === 'away') {
    const away = -line;
    return away === 0 ? 0 : away;
  }
  return line;
}

/**
 * Book prices for the fair probabilities of one market. The margin is added in odds-ratio form (implied_i =
 * sigmoid(logit(p_i) + c) with c solved so the implied probabilities sum to 1 + margin): the overround is the same at
 * every price level and, like at real books, most of it sits on the longshot. Prices are then rounded the way the book
 * displays American odds and converted back to decimal.
 */
function bookPrices(probs: number[], margin: number, roundTo: number): number[] {
  const logits = probs.map((p) => logit(clamp(Number.isFinite(p) ? p : 0.5, 0.002, 0.998)));
  const target = 1 + margin;
  let lo = 0;
  let hi = 10;
  for (let i = 0; i < 40; i++) {
    const c = (lo + hi) / 2;
    let total = 0;
    for (const l of logits) total += sigmoid(l + c);
    if (total < target) lo = c;
    else hi = c;
  }
  const shift = (lo + hi) / 2;
  return logits.map((l) => {
    const implied = clamp(sigmoid(l + shift), 0.001, 0.995);
    let american = decimalToAmerican(1 / implied);
    if (roundTo > 1) american = Math.round(american / roundTo) * roundTo;
    if (american > -100 && american < 100) american = american >= 0 ? 100 : -100;
    return americanToDecimal(american);
  });
}

function sameNumbers(a: number[] | null, b: number[] | null): boolean {
  if (a === null || b === null) return a === b;
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
  return true;
}

export class DemoFeed {
  private readonly rng: () => number;
  private readonly books: BookProfile[];
  private readonly bookKeys: string[];
  private readonly leagues: LeagueSlots[] = [];
  private readonly slotCounts: number[] = [];
  private initialized = false;
  private lastAt: number | null = null;

  constructor(leagues: LeagueDef[], books: string[], opts: DemoFeedOptions = {}) {
    const seed = typeof opts.seed === 'number' && Number.isFinite(opts.seed) ? Math.floor(opts.seed) : DEFAULT_SEED;
    this.rng = mulberry32(seed);
    this.books = DemoFeed.pickBooks(books);
    this.bookKeys = this.books.map((b) => b.key);

    const preferred = leagues.filter((l) => PREFERRED_LEAGUES.includes(l.key.toUpperCase()));
    const chosen = preferred.length > 0 ? preferred : leagues.slice(0, FALLBACK_LEAGUE_COUNT);
    let budget = MAX_EVENTS;
    for (const league of chosen) {
      if (budget <= 0) break;
      if (this.leagues.some((l) => l.league.key === league.key)) continue;
      const count = Math.min(EVENTS_PER_LEAGUE[league.key.toUpperCase()] ?? DEFAULT_EVENTS_PER_LEAGUE, budget);
      budget -= count;
      this.leagues.push({
        league: { ...league },
        sport: sportFor(league),
        threeWay: league.threeWay,
        teams: TEAMS[league.key.toUpperCase()] ?? GENERIC_TEAMS,
        events: [],
        nextSerial: 1,
      });
      this.slotCounts.push(count);
    }
  }

  /** Advances the simulation to `at` and returns one complete snapshot per demo league. */
  tick(at: number): SourceSnapshot[] {
    if (!Number.isFinite(at)) return [];
    if (!this.initialized) this.init(at);
    const stepSec = this.lastAt === null ? 0 : clamp((at - this.lastAt) / 1000, 0, MAX_STEP_SEC);
    if (this.lastAt === null || at > this.lastAt) this.lastAt = at;
    // The simulation never runs backwards: a clock that stepped back re-reports the latest state.
    const now = this.lastAt;

    const out: SourceSnapshot[] = [];
    for (const slots of this.leagues) {
      const events: RawEvent[] = [];
      const quotes: Quote[] = [];
      for (let i = 0; i < slots.events.length; i++) {
        let ev = slots.events[i];
        if (now >= ev.startTime + ev.durationMs) ev = this.rollover(slots, i, now);
        const jump = this.advance(slots, ev, now, stepSec);
        this.reprice(slots, ev, now, jump);
        events.push(this.rawEvent(slots, ev, now));
        this.pushQuotes(slots, ev, at, quotes);
      }
      out.push({
        source: 'demo',
        league: slots.league.key,
        fetchedAt: at,
        events,
        quotes,
        complete: true,
        books: this.bookKeys.slice(),
      });
    }
    return out;
  }

  // ---------------------------------------------------------------------------------------------------------------
  // Setup

  private static pickBooks(configured: string[]): BookProfile[] {
    const keys = configured.map((b) => b.trim().toLowerCase()).filter((b) => b.length > 0);
    const others: string[] = [];
    for (const k of keys) {
      if (others.length < MAX_OTHER_BOOKS && !SHARP_LIKE.has(k) && !others.includes(k)) others.push(k);
    }
    for (const k of keys) {
      if (others.length < MAX_OTHER_BOOKS && k !== SHARP_BOOK && k !== DK_BOOK && !others.includes(k)) others.push(k);
    }
    for (const k of FALLBACK_OTHER_BOOKS) {
      if (others.length < MIN_OTHER_BOOKS && !others.includes(k)) others.push(k);
    }
    const margins = [0.04, 0.047, 0.05];
    return [
      {
        key: SHARP_BOOK, margin: 0.02, instant: true, biasSd: 0, roundTo: 1,
        lagMinSec: 0, lagMaxSec: 0, slowChance: 0, slowMinSec: 0, slowMaxSec: 0, altLines: true,
      },
      {
        key: DK_BOOK, margin: 0.045, instant: false, biasSd: 0.06, roundTo: 5,
        lagMinSec: 5, lagMaxSec: 90, slowChance: 0.07, slowMinSec: 120, slowMaxSec: 360,
        link: DK_DEMO_LINK, altLines: true,
      },
      ...others.map((key, i) => ({
        key, margin: margins[i % margins.length], instant: false, biasSd: 0.03, roundTo: 5,
        lagMinSec: 5, lagMaxSec: 60, slowChance: 0.03, slowMinSec: 90, slowMaxSec: 240, altLines: false,
      })),
    ];
  }

  private init(at: number): void {
    this.initialized = true;
    let slotIndex = 0;
    this.leagues.forEach((slots, li) => {
      for (let i = 0; i < this.slotCounts[li]; i++) {
        const live = slotIndex % 2 === 0;
        slotIndex++;
        const durationMs = slots.sport.durationMin * MINUTE_MS;
        const startTime = live
          ? at - Math.round(this.uniform(5 * MINUTE_MS, durationMs - 15 * MINUTE_MS))
          : at + Math.round(this.uniform(10 * MINUTE_MS, 20 * HOUR_MS));
        const ev = this.newEvent(slots, startTime);
        slots.events.push(ev);
        if (live) {
          // Play the game from the start until now so score, lines and prices are consistent.
          const stepMs = WARMUP_STEP_SEC * 1000;
          for (let t = startTime; t < at; t += stepMs) this.advance(slots, ev, t, t === startTime ? 0 : WARMUP_STEP_SEC);
          this.advance(slots, ev, at, 0);
        }
        this.priceAllBooks(slots, ev, at);
      }
    });
  }

  private newEvent(slots: LeagueSlots, startTime: number): DemoEvent {
    const sport = slots.sport;
    const [home, away] = this.pickTeams(slots);
    const expMargin0 = this.uniform(sport.marginRange[0], sport.marginRange[1]);
    const expTotal0 = this.uniform(sport.totalRange[0], sport.totalRange[1]);
    const serial = slots.nextSerial++;
    const spreadLine =
      sport.fixedSpread > 0 ? (expMargin0 >= 0 ? -sport.fixedSpread : sport.fixedSpread) : halfLine(-expMargin0);
    return {
      id: `${slots.league.key.toLowerCase()}-${serial}`,
      home,
      away,
      startTime,
      durationMs: sport.durationMin * MINUTE_MS,
      live: false,
      score: null,
      expMargin0,
      expTotal0,
      marginShift: 0,
      totalShift: 0,
      spreadLine,
      totalLine: halfLine(expTotal0),
      // Every third event carries an alternate spread (DraftKings and Pinnacle quote it).
      altOffset: serial % 3 === 1 ? (this.rng() < 0.5 ? -1 : 1) * sport.altOffset : 0,
      markets: [],
    };
  }

  private pickTeams(slots: LeagueSlots): [string, string] {
    const inUse = new Set<string>();
    for (const ev of slots.events) {
      inUse.add(ev.home);
      inUse.add(ev.away);
    }
    let pool = slots.teams.filter((t) => !inUse.has(t));
    if (pool.length < 2) pool = slots.teams.slice();
    const a = Math.floor(this.rng() * pool.length);
    let b = Math.floor(this.rng() * (pool.length - 1));
    if (b >= a) b++;
    return [pool[a], pool[b]];
  }

  /** Replaces a finished game with a new pre-match game (new id) starting 10 min – 3 h from now. */
  private rollover(slots: LeagueSlots, index: number, at: number): DemoEvent {
    const startTime = at + Math.round(this.uniform(10 * MINUTE_MS, 3 * HOUR_MS));
    const ev = this.newEvent(slots, startTime);
    slots.events[index] = ev;
    this.priceAllBooks(slots, ev, at);
    return ev;
  }

  // ---------------------------------------------------------------------------------------------------------------
  // Simulation

  private expectedTotal(sport: SportProfile, ev: DemoEvent): number {
    return Math.max(sport.totalRange[0] * 0.3, ev.expTotal0 + ev.totalShift);
  }

  private expectedMargin(sport: SportProfile, ev: DemoEvent): number {
    const total = this.expectedTotal(sport, ev);
    return clamp(ev.expMargin0 + ev.marginShift, -0.7 * total, 0.7 * total);
  }

  private remaining(ev: DemoEvent, at: number): number {
    if (!ev.live) return 1;
    return clamp(1 - (at - ev.startTime) / ev.durationMs, 0, 1);
  }

  /** Fair probabilities at `at` from the score and the distribution of what is still to be scored. */
  private fair(slots: LeagueSlots, ev: DemoEvent, at: number): FairModel {
    const sport = slots.sport;
    const rem = this.remaining(ev, at);
    const h = ev.score?.home ?? 0;
    const a = ev.score?.away ?? 0;
    const expTotal = this.expectedTotal(sport, ev);
    const expMargin = this.expectedMargin(sport, ev);

    if (!sport.poisson) {
      const sdMargin = Math.max(0.5, sport.marginSd * Math.sqrt(rem));
      const sdTotal = Math.max(0.5, sport.totalSd * Math.sqrt(rem));
      const finalMargin = h - a + expMargin * rem;
      const finalTotal = h + a + expTotal * rem;
      return {
        pHome: normCdf(finalMargin / sdMargin),
        pDraw: 0,
        cover: (line) => normCdf((finalMargin + line) / sdMargin),
        over: (line) => normCdf((finalTotal - line) / sdTotal),
      };
    }

    // Goals still to come per team are Poisson; D = home - away remaining, S = home + away remaining.
    const ph = poissonPmf(Math.max(0.02, (expTotal + expMargin) / 2) * rem);
    const pa = poissonPmf(Math.max(0.02, (expTotal - expMargin) / 2) * rem);
    const diff = new Array<number>(2 * MAX_GOALS + 1).fill(0);
    const sum = new Array<number>(2 * MAX_GOALS + 1).fill(0);
    for (let x = 0; x <= MAX_GOALS; x++) {
      for (let y = 0; y <= MAX_GOALS; y++) {
        const p = ph[x] * pa[y];
        diff[x - y + MAX_GOALS] += p;
        sum[x + y] += p;
      }
    }
    const diffAbove = (threshold: number): number => {
      let p = 0;
      for (let d = -MAX_GOALS; d <= MAX_GOALS; d++) if (d > threshold) p += diff[d + MAX_GOALS];
      return p;
    };
    const tieAt = a - h;
    const pWin = diffAbove(tieAt);
    const pTie = tieAt >= -MAX_GOALS && tieAt <= MAX_GOALS ? diff[tieAt + MAX_GOALS] : 0;
    return {
      // Two-way (hockey) moneylines include overtime: a regulation tie is treated as a coin flip.
      pHome: slots.threeWay ? pWin : pWin + pTie / 2,
      pDraw: slots.threeWay ? pTie : 0,
      cover: (line) => diffAbove(a - h - line),
      over: (line) => {
        let p = 0;
        for (let s = 0; s < sum.length; s++) if (s + h + a > line) p += sum[s];
        return p;
      },
    };
  }

  private jumpSize(before: FairModel, after: FairModel, ev: DemoEvent): number {
    return Math.max(
      Math.abs(after.pHome - before.pHome),
      Math.abs(after.pDraw - before.pDraw),
      Math.abs(after.cover(ev.spreadLine) - before.cover(ev.spreadLine)),
      Math.abs(after.over(ev.totalLine) - before.over(ev.totalLine)),
    );
  }

  /**
   * Moves an event forward by `stepSec` ending at `at`: kick-off, random walk of the expectations, pre-match steam,
   * in-game scoring, line moves. Returns the biggest probability swing caused by a steam move or a score (0 if none).
   */
  private advance(slots: LeagueSlots, ev: DemoEvent, at: number, stepSec: number): number {
    const sport = slots.sport;
    if (!ev.live && at >= ev.startTime) {
      ev.live = true;
      ev.score = { home: 0, away: 0, updatedAt: at };
    }
    let jump = 0;
    if (stepSec > 0) {
      const root = Math.sqrt(stepSec);
      const drift = ev.live ? LIVE_DRIFT : PREMATCH_DRIFT;
      ev.marginShift += this.normal() * drift * sport.driftMarginSd * root;
      ev.totalShift += this.normal() * drift * sport.driftTotalSd * root;

      if (!ev.live) {
        if (this.rng() < 1 - Math.exp(-stepSec / STEAM_MEAN_SEC)) {
          const before = this.fair(slots, ev, at);
          const size = this.uniform(STEAM_MIN, STEAM_MAX) * (this.rng() < 0.5 ? -1 : 1);
          if (this.rng() < 0.6) ev.marginShift += size * sport.driftMarginSd;
          else ev.totalShift += size * sport.driftTotalSd;
          jump = this.jumpSize(before, this.fair(slots, ev, at), ev);
        }
      } else if (ev.score) {
        const expTotal = this.expectedTotal(sport, ev);
        const rate = expTotal / (avgPoints(sport) * (ev.durationMs / 1000));
        const plays = this.poissonCount(rate * stepSec);
        if (plays > 0) {
          const before = this.fair(slots, ev, at);
          const homeShare = clamp(0.5 + this.expectedMargin(sport, ev) / (2 * expTotal), 0.15, 0.85);
          for (let i = 0; i < plays; i++) {
            const pts = this.samplePoints(sport);
            if (this.rng() < homeShare) ev.score.home += pts;
            else ev.score.away += pts;
          }
          ev.score.updatedAt = at;
          jump = this.jumpSize(before, this.fair(slots, ev, at), ev);
        }
      }
      ev.marginShift = clamp(ev.marginShift, -2 * sport.driftMarginSd, 2 * sport.driftMarginSd);
      ev.totalShift = clamp(ev.totalShift, -0.4 * ev.expTotal0, 0.4 * ev.expTotal0);
    }
    this.moveLines(sport, ev, at);
    return jump;
  }

  /** Keeps the main spread/total near the expected final margin/total, with hysteresis like a real book. */
  private moveLines(sport: SportProfile, ev: DemoEvent, at: number): void {
    const rem = this.remaining(ev, at);
    const h = ev.score?.home ?? 0;
    const a = ev.score?.away ?? 0;
    const expectedFinalMargin = h - a + this.expectedMargin(sport, ev) * rem;
    const expectedFinalTotal = h + a + this.expectedTotal(sport, ev) * rem;
    if (sport.fixedSpread > 0 && !ev.live) {
      if (expectedFinalMargin > sport.hysteresis / 2) ev.spreadLine = -sport.fixedSpread;
      else if (expectedFinalMargin < -sport.hysteresis / 2) ev.spreadLine = sport.fixedSpread;
    } else if (Math.abs(-expectedFinalMargin - ev.spreadLine) > sport.hysteresis) {
      ev.spreadLine = halfLine(-expectedFinalMargin);
    }
    if (Math.abs(expectedFinalTotal - ev.totalLine) > sport.hysteresis) {
      ev.totalLine = Math.max(h + a + 0.5, halfLine(expectedFinalTotal));
    }
  }

  private samplePoints(sport: SportProfile): number {
    let u = this.rng();
    for (const [pts, p] of sport.points) {
      if (u < p) return pts;
      u -= p;
    }
    return sport.points[0][0];
  }

  // ---------------------------------------------------------------------------------------------------------------
  // Books

  /** Prices every book from scratch (new event); lagging books look like they last moved up to a minute ago. */
  private priceAllBooks(slots: LeagueSlots, ev: DemoEvent, at: number): void {
    const model = this.fair(slots, ev, at);
    ev.markets = this.books.map((book) =>
      KINDS.map((kind) => {
        const bias = book.biasSd > 0 ? this.normal() * book.biasSd : 0;
        return {
          bias,
          ...this.priceMarket(slots, ev, model, book, kind, bias),
          updatedAt: book.instant ? at : at - Math.round(this.uniform(0, 60_000)),
          nextRepriceAt: book.instant ? at : at + this.lagMs(book),
          suspended: false,
        };
      }),
    );
  }

  /**
   * Instant books reprice every tick. Lagging books reprice when their timer fires; a jump pulls the timer in (after
   * their usual lag) and a big in-game swing suspends their markets until then, like a book locking its board after
   * a goal or touchdown.
   */
  private reprice(slots: LeagueSlots, ev: DemoEvent, at: number, jump: number): void {
    const suspend = ev.live && jump >= SUSPEND_JUMP;
    let model: FairModel | null = null;
    for (let b = 0; b < this.books.length; b++) {
      const book = this.books[b];
      for (let k = 0; k < KINDS.length; k++) {
        const m = ev.markets[b][k];
        if (!book.instant) {
          if (suspend) {
            m.suspended = true;
            m.nextRepriceAt = at + Math.round(this.uniform(SUSPEND_MIN_SEC, SUSPEND_MAX_SEC) * 1000);
          } else if (jump > 0) {
            m.nextRepriceAt = Math.min(m.nextRepriceAt, at + this.lagMs(book));
          }
          if (at < m.nextRepriceAt) continue;
          m.nextRepriceAt = at + this.lagMs(book);
          if (m.suspended) {
            m.suspended = false;
            m.updatedAt = at;
          }
        }
        if (book.biasSd > 0) {
          m.bias =
            BIAS_PERSISTENCE * m.bias + Math.sqrt(1 - BIAS_PERSISTENCE * BIAS_PERSISTENCE) * this.normal() * book.biasSd;
        }
        model ??= this.fair(slots, ev, at);
        const next = this.priceMarket(slots, ev, model, book, KINDS[k], m.bias);
        const changed =
          m.line !== next.line ||
          m.altLine !== next.altLine ||
          !sameNumbers(m.decimals, next.decimals) ||
          !sameNumbers(m.altDecimals, next.altDecimals);
        if (changed) {
          m.line = next.line;
          m.decimals = next.decimals;
          m.altLine = next.altLine;
          m.altDecimals = next.altDecimals;
          m.updatedAt = at;
        }
      }
    }
  }

  private priceMarket(
    slots: LeagueSlots,
    ev: DemoEvent,
    model: FairModel,
    book: BookProfile,
    kind: MarketKind,
    bias: number,
  ): PricedMarket {
    const shade = (p: number): number => sigmoid(logit(p) + bias);
    const price = (probs: number[]): number[] => bookPrices(probs, book.margin, book.roundTo);
    if (kind === 'moneyline') {
      if (slots.threeWay) {
        const pDraw = model.pDraw;
        const pHome = (1 - pDraw) * shade(model.pHome / Math.max(1e-9, 1 - pDraw));
        return { line: null, decimals: price([pHome, pDraw, 1 - pDraw - pHome]), altLine: null, altDecimals: null };
      }
      const pHome = shade(model.pHome);
      return { line: null, decimals: price([pHome, 1 - pHome]), altLine: null, altDecimals: null };
    }
    if (kind === 'spread') {
      const pCover = shade(model.cover(ev.spreadLine));
      let altLine: number | null = null;
      let altDecimals: number[] | null = null;
      if (book.altLines && ev.altOffset !== 0) {
        altLine = ev.spreadLine + ev.altOffset;
        const pAlt = shade(model.cover(altLine));
        altDecimals = price([pAlt, 1 - pAlt]);
      }
      return { line: ev.spreadLine, decimals: price([pCover, 1 - pCover]), altLine, altDecimals };
    }
    const pOver = shade(model.over(ev.totalLine));
    return { line: ev.totalLine, decimals: price([pOver, 1 - pOver]), altLine: null, altDecimals: null };
  }

  /** Reaction lag: usually lagMin–lagMax (skewed short), occasionally much later. */
  private lagMs(book: BookProfile): number {
    const sec =
      this.rng() < book.slowChance
        ? this.uniform(book.slowMinSec, book.slowMaxSec)
        : book.lagMinSec + (book.lagMaxSec - book.lagMinSec) * this.rng() ** 2;
    return Math.round(sec * 1000);
  }

  // ---------------------------------------------------------------------------------------------------------------
  // Output

  private rawEvent(slots: LeagueSlots, ev: DemoEvent, now: number): RawEvent {
    const raw: RawEvent = {
      source: 'demo',
      sourceEventId: ev.id,
      league: slots.league.key,
      home: ev.home,
      away: ev.away,
      startTime: ev.startTime,
      isLive: ev.live,
    };
    if (ev.live && ev.score) {
      raw.score = {
        home: ev.score.home,
        away: ev.score.away,
        updatedAt: ev.score.updatedAt,
        ...this.gameClock(slots.sport, ev, now),
      };
    }
    return raw;
  }

  private gameClock(sport: SportProfile, ev: DemoEvent, now: number): Pick<LiveScore, 'period' | 'clock'> {
    const frac = clamp((now - ev.startTime) / ev.durationMs, 0, 0.9999);
    switch (sport.kind) {
      case 'basketball':
      case 'football': {
        const q = Math.floor(frac * 4);
        const quarterMs = (sport.kind === 'basketball' ? 12 : 15) * MINUTE_MS;
        return { period: `Q${q + 1}`, clock: mmss((1 - (frac * 4 - q)) * quarterMs) };
      }
      case 'hockey': {
        const p = Math.floor(frac * 3);
        return { period: `P${p + 1}`, clock: mmss((1 - (frac * 3 - p)) * 20 * MINUTE_MS) };
      }
      case 'baseball': {
        const half = Math.floor(frac * 18);
        return { period: `${half % 2 === 0 ? 'Top' : 'Bot'} ${ordinal(Math.floor(half / 2) + 1)}` };
      }
      case 'soccer': {
        const minute = Math.floor(frac * 90) + 1;
        return { period: minute <= 45 ? '1st Half' : '2nd Half', clock: `${minute}'` };
      }
    }
  }

  private pushQuotes(slots: LeagueSlots, ev: DemoEvent, at: number, out: Quote[]): void {
    for (let b = 0; b < this.books.length; b++) {
      const book = this.books[b];
      for (let k = 0; k < KINDS.length; k++) {
        const kind = KINDS[k];
        const m = ev.markets[b][k];
        const sides = sidesFor(kind, slots.threeWay);
        for (let i = 0; i < sides.length; i++) {
          out.push(this.quote(book, ev, kind, sides[i], lineFor(kind, sides[i], m.line), m.decimals[i], true, m, at));
        }
        if (m.altLine !== null && m.altDecimals) {
          out.push(this.quote(book, ev, kind, 'home', m.altLine, m.altDecimals[0], false, m, at));
          out.push(this.quote(book, ev, kind, 'away', lineFor(kind, 'away', m.altLine), m.altDecimals[1], false, m, at));
        }
      }
    }
  }

  private quote(
    book: BookProfile,
    ev: DemoEvent,
    kind: MarketKind,
    side: Side,
    line: number | null,
    decimal: number,
    isMainLine: boolean,
    m: BookMarket,
    at: number,
  ): Quote {
    const q: Quote = {
      book: book.key,
      source: 'demo',
      sourceEventId: ev.id,
      kind,
      side,
      line,
      decimal,
      suspended: m.suspended,
      isMainLine,
      observedAt: at,
      bookUpdatedAt: m.updatedAt,
    };
    if (book.link) q.link = book.link;
    return q;
  }

  // ---------------------------------------------------------------------------------------------------------------
  // Randomness: every draw comes from the seeded PRNG, so a seed plus the same tick times reproduces the feed exactly.

  private uniform(lo: number, hi: number): number {
    return lo + (hi - lo) * this.rng();
  }

  /** Poisson-distributed count (Knuth), capped for safety; lambda is small here (< 2). */
  private poissonCount(lambda: number): number {
    if (!(lambda > 0)) return 0;
    const limit = Math.exp(-lambda);
    let k = 0;
    let p = this.rng();
    while (p > limit && k < 20) {
      k++;
      p *= this.rng();
    }
    return k;
  }

  private normal(): number {
    const u1 = Math.max(this.rng(), 1e-12);
    const u2 = this.rng();
    return Math.sqrt(-2 * Math.log(u1)) * Math.cos(2 * Math.PI * u2);
  }
}

import * as fs from 'node:fs';
import * as path from 'node:path';
import * as vm from 'node:vm';
import { describe, expect, it } from 'vitest';

/**
 * Frontend checks without a browser:
 *  - static CSP / safety rules for public/ (no inline code or styles, no external resources, no innerHTML);
 *  - the pure helpers app.js exports when loaded outside a browser (formatting, filtering, alerts, settings).
 */

const PUBLIC = path.join(__dirname, '..', 'public');
const read = (f: string): string => fs.readFileSync(path.join(PUBLIC, f), 'utf8');

const html = read('index.html');
const js = read('app.js');
/** app.js without comments, for the static code checks. */
const jsCode = js.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
const css = read('styles.css');
const svg = read('favicon.svg');

interface Opp {
  id: string;
  type: 'ev' | 'arb';
  league: string;
  pick: string;
  status: 'active' | 'gone';
  verdict: 'BET_NOW' | 'BET' | 'WATCH';
  urgency: 'critical' | 'high' | 'medium' | 'low';
  urgencyScore: number;
  evPct: number;
  isLive: boolean;
  firstSeen: number;
  expiresInSec: number;
}

interface Filters {
  leagues: string[];
  liveOnly: boolean;
  minEv: number;
  showWatch: boolean;
}

interface Parts {
  live: Opp[];
  pre: Opp[];
  watch: Opp[];
  arbs: Opp[];
  hidden: number;
  hiddenWatch: number;
}

interface FrontendLib {
  DK_FALLBACK_URL: string;
  fmtAmerican(n: unknown): string;
  fmtAmericanAscii(n: unknown): string;
  americanToDecimal(a: number): number | null;
  parseAmericanInput(raw: unknown): number | null;
  fmtPct(x: unknown, digits?: number, signed?: boolean): string;
  fmtMoney(n: unknown, opts?: { signed?: boolean; cents?: boolean }): string;
  fmtCredits(n: unknown): string;
  fmtAge(ms: unknown, coarse?: boolean): string;
  fmtAgo(ms: unknown): string;
  fmtLine(line: unknown): string;
  describePick(kind: string, side: string, line: number | null, home: string, away: string): string;
  bookName(key: unknown): string;
  sharpLabel(src: unknown): string;
  verdictLabel(v: string): string;
  fmtScore(score: unknown): string;
  safeDkUrl(u: unknown): string;
  isDashboardState(st: unknown): boolean;
  sanitizeFilters(raw: unknown): Filters;
  passesFilters(o: Opp, f: Filters): boolean;
  partitionOpportunities(list: unknown, f: Filters): Parts;
  actionableCount(parts: Parts): number;
  selectAlerts(opps: Opp[], seen: Map<string, number>, primed: boolean, cap?: number): Opp[];
  countdownInfo(o: Opp, now: number): { remaining: number; elapsed: number; frac: number };
  countdownLabel(info: { remaining: number; elapsed: number; frac: number }): string;
  evAtAmerican(prob: number, american: number): number | null;
  pctToFraction(p: number): number;
  fractionToPct(f: number): number;
  buildSettingsPatch(
    form: Record<string, unknown>,
    base: Record<string, unknown> | null,
  ): { patch?: Record<string, unknown>; error?: string };
}

function loadLib(): FrontendLib {
  const sandbox: { module: { exports: unknown }; URL: typeof URL; console: Console } = {
    module: { exports: {} },
    URL,
    console,
  };
  vm.createContext(sandbox);
  vm.runInContext(js, sandbox, { filename: 'app.js' });
  return sandbox.module.exports as FrontendLib;
}

const lib = loadLib();

function opp(over: Partial<Opp> & { id: string }): Opp {
  return {
    type: 'ev',
    league: 'NBA',
    pick: 'Celtics -3.5',
    status: 'active',
    verdict: 'BET',
    urgency: 'medium',
    urgencyScore: 40,
    evPct: 0.03,
    isLive: false,
    firstSeen: 0,
    expiresInSec: 45,
    ...over,
  };
}

const noFilters = (): Filters => ({ leagues: [], liveOnly: false, minEv: 0, showWatch: false });

describe('public/ static safety (CSP: self only)', () => {
  it('index.html has no inline scripts, inline styles, style attributes or inline handlers', () => {
    const scripts = html.match(/<script\b[^>]*>/gi) ?? [];
    expect(scripts.length).toBe(1);
    expect(scripts[0]).toMatch(/src="app\.js"/);
    expect(html).toMatch(/<script src="app\.js"><\/script>/);
    expect(html).not.toMatch(/<style\b/i);
    expect(html).not.toMatch(/\sstyle\s*=/i);
    expect(html).not.toMatch(/\son[a-z]+\s*=/i);
    expect(html).not.toMatch(/javascript:/i);
  });

  it('index.html loads no external resources', () => {
    const refs = [...html.matchAll(/\b(?:src|href)="([^"]+)"/gi)].map((m) => m[1]);
    expect(refs.length).toBeGreaterThan(0);
    for (const r of refs) expect(r).not.toMatch(/^(https?:)?\/\//i);
    expect(refs).toContain('styles.css');
    expect(refs).toContain('favicon.svg');
  });

  it('app.js never builds HTML from strings or evaluates code', () => {
    expect(jsCode.length).toBeGreaterThan(10_000);
    expect(jsCode).not.toMatch(/innerHTML|outerHTML|insertAdjacentHTML|document\.write/);
    expect(jsCode).not.toMatch(/\beval\s*\(|new Function\s*\(/);
    expect(jsCode).not.toMatch(/setAttribute\(\s*['"]style['"]/);
    expect(jsCode).not.toMatch(/\.cssText\s*=/);
  });

  it('app.js only references its own origin (plus the DraftKings fallback link and the SVG namespace)', () => {
    const urls = [...jsCode.matchAll(/https?:\/\/[^\s'"`)]+/g)].map((m) => m[0]);
    const allowed = new Set(['https://sportsbook.draftkings.com/', 'http://www.w3.org/2000/svg']);
    for (const u of urls) expect(allowed.has(u), u).toBe(true);
    for (const call of js.matchAll(/(?:fetch|api)\(\s*'[A-Z]*'?,?\s*'([^']+)'/g)) expect(call[1]).not.toMatch(/^\/|^https?:/);
    expect(js).toMatch(/new window\.EventSource\('api\/stream'\)/);
  });

  it('styles.css and favicon.svg pull in nothing external', () => {
    expect(css).not.toMatch(/@import/i);
    expect(css).not.toMatch(/url\(\s*['"]?(https?:)?\/\//i);
    expect(svg).not.toMatch(/<script|href=|xlink:href|<style/i);
    expect(svg).toMatch(/^<svg xmlns="http:\/\/www\.w3\.org\/2000\/svg"/);
  });

  it('every element id used by app.js exists in index.html', () => {
    const ids = new Set([...html.matchAll(/\bid="([^"]+)"/g)].map((m) => m[1]));
    const pairIds = [...js.matchAll(/\['[A-Za-z]+', '([a-z0-9-]+)'\]/g)].map((m) => m[1]);
    expect(pairIds.length).toBeGreaterThan(50);
    for (const id of pairIds) expect(ids.has(id), id).toBe(true);
    for (const tab of ['opps', 'arbs', 'bets']) {
      expect(ids.has(`tab-${tab}`)).toBe(true);
      expect(ids.has(`panel-${tab}`)).toBe(true);
    }
  });

  it('has the responsible-gambling footer, dark default, light theme and reduced-motion support', () => {
    expect(html).toContain('Read-only analytics. You place every bet yourself. Gamble responsibly — 1-800-GAMBLER.');
    expect(html).toMatch(/<html lang="en" data-theme="dark">/);
    expect(css).toMatch(/:root\[data-theme="light"\]/);
    expect(css).toMatch(/@media \(prefers-reduced-motion: reduce\)/);
    expect(css).toMatch(/font-variant-numeric: tabular-nums/);
  });
});

describe('app.js pure helpers', () => {
  it('does not boot the browser app outside a browser', () => {
    expect(typeof lib.fmtAmerican).toBe('function');
  });

  it('formats and parses American odds', () => {
    expect(lib.fmtAmerican(110)).toBe('+110');
    expect(lib.fmtAmerican(-120.4)).toBe('−120');
    expect(lib.fmtAmerican(Number.NaN)).toBe('—');
    expect(lib.fmtAmerican(null)).toBe('—');
    expect(lib.fmtAmericanAscii(-120)).toBe('-120');
    expect(lib.fmtAmericanAscii(150)).toBe('+150');
    expect(lib.parseAmericanInput('+110')).toBe(110);
    expect(lib.parseAmericanInput(' -120 ')).toBe(-120);
    expect(lib.parseAmericanInput('−135')).toBe(-135);
    expect(lib.parseAmericanInput('250')).toBe(250);
    expect(lib.parseAmericanInput('even')).toBe(100);
    expect(lib.parseAmericanInput('95')).toBeNull();
    expect(lib.parseAmericanInput('-99')).toBeNull();
    expect(lib.parseAmericanInput('1.91')).toBeNull();
    expect(lib.parseAmericanInput('abc')).toBeNull();
    expect(lib.parseAmericanInput(undefined)).toBeNull();
    expect(lib.americanToDecimal(150)).toBeCloseTo(2.5, 12);
    expect(lib.americanToDecimal(-200)).toBeCloseTo(1.5, 12);
    expect(lib.americanToDecimal(50)).toBeNull();
    expect(lib.evAtAmerican(0.5, 110)).toBeCloseTo(0.05, 12);
    expect(lib.evAtAmerican(0.5, 20)).toBeNull();
  });

  it('formats money, percentages, credits and durations', () => {
    expect(lib.fmtMoney(38)).toBe('$38');
    expect(lib.fmtMoney(1234.5)).toBe('$1,234.50');
    expect(lib.fmtMoney(1000000)).toBe('$1,000,000');
    expect(lib.fmtMoney(-20)).toBe('−$20');
    expect(lib.fmtMoney(12.4, { signed: true })).toBe('+$12.40');
    expect(lib.fmtMoney(0, { signed: true })).toBe('$0');
    expect(lib.fmtMoney(20.68, { cents: true })).toBe('$20.68');
    expect(lib.fmtMoney(12.5, { cents: false })).toBe('$13');
    expect(lib.fmtMoney(Number.POSITIVE_INFINITY)).toBe('—');
    expect(lib.fmtPct(0.052)).toBe('+5.2%');
    expect(lib.fmtPct(-0.0134)).toBe('−1.3%');
    expect(lib.fmtPct(0.019, 1, false)).toBe('1.9%');
    expect(lib.fmtPct(0)).toBe('0.0%');
    expect(lib.fmtCredits(18432)).toBe('18.4k');
    expect(lib.fmtCredits(950)).toBe('950');
    expect(lib.fmtCredits(123456)).toBe('123k');
    expect(lib.fmtAge(12_000)).toBe('12s');
    expect(lib.fmtAge(252_000)).toBe('4m 12s');
    expect(lib.fmtAge(252_000, true)).toBe('4m');
    expect(lib.fmtAge(42 * 60_000)).toBe('42m');
    expect(lib.fmtAge((3 * 60 + 5) * 60_000)).toBe('3h 5m');
    expect(lib.fmtAge(50 * 3_600_000)).toBe('2d 2h');
    expect(lib.fmtAgo(800)).toBe('just now');
    expect(lib.fmtAgo(9_000)).toBe('9s ago');
  });

  it('describes picks, books, references and scores like the engine', () => {
    expect(lib.describePick('spread', 'home', -3.5, 'Celtics', 'Knicks')).toBe('Celtics -3.5');
    expect(lib.describePick('spread', 'away', 3.5, 'Celtics', 'Knicks')).toBe('Knicks +3.5');
    expect(lib.describePick('spread', 'away', 0, 'Celtics', 'Knicks')).toBe('Knicks PK');
    expect(lib.describePick('total', 'over', 224.5, 'Celtics', 'Knicks')).toBe('Over 224.5');
    expect(lib.describePick('total', 'under', 6.5, 'A', 'B')).toBe('Under 6.5');
    expect(lib.describePick('moneyline', 'away', null, 'Celtics', 'Knicks')).toBe('Knicks ML');
    expect(lib.describePick('moneyline', 'draw', null, 'Arsenal', 'Chelsea')).toBe('Draw');
    expect(lib.bookName('williamhill_us')).toBe('Caesars');
    expect(lib.bookName('draftkings')).toBe('DraftKings');
    expect(lib.bookName('some_new_book')).toBe('Some New Book');
    expect(lib.bookName('')).toBe('Other book');
    expect(lib.sharpLabel('pinnacle')).toBe('Pinnacle no-vig');
    expect(lib.sharpLabel('consensus(5)')).toBe('Consensus of 5 books');
    expect(lib.verdictLabel('BET_NOW')).toBe('BET NOW');
    expect(lib.fmtScore({ home: 84, away: 88, period: 'Q3', clock: '04:12', updatedAt: 1 })).toBe('88–84 · Q3 04:12');
    expect(lib.fmtScore({ home: null, away: null, period: '2nd Half', updatedAt: 1 })).toBe('2nd Half');
    expect(lib.fmtScore(undefined)).toBe('');
  });

  it('only ever links to https URLs', () => {
    expect(lib.safeDkUrl('https://sportsbook.draftkings.com/event/123')).toBe('https://sportsbook.draftkings.com/event/123');
    expect(lib.safeDkUrl('javascript:alert(1)')).toBe(lib.DK_FALLBACK_URL);
    expect(lib.safeDkUrl('http://sportsbook.draftkings.com/')).toBe(lib.DK_FALLBACK_URL);
    expect(lib.safeDkUrl('not a url')).toBe(lib.DK_FALLBACK_URL);
    expect(lib.safeDkUrl(null)).toBe(lib.DK_FALLBACK_URL);
  });

  it('recognises dashboard states', () => {
    const st = { generatedAt: 1, opportunities: [], health: {}, settings: {} };
    expect(lib.isDashboardState(st)).toBe(true);
    expect(lib.isDashboardState({ ...st, opportunities: null })).toBe(false);
    expect(lib.isDashboardState({ ...st, generatedAt: 'x' })).toBe(false);
    expect(lib.isDashboardState(null)).toBe(false);
  });

  it('sanitizes persisted filters', () => {
    expect(lib.sanitizeFilters(null)).toEqual(noFilters());
    expect(lib.sanitizeFilters('garbage')).toEqual(noFilters());
    expect(lib.sanitizeFilters({ leagues: ['NBA', 5, '', 'x'.repeat(50)], liveOnly: 'yes', minEv: 99, showWatch: true })).toEqual({
      leagues: ['NBA'],
      liveOnly: false,
      minEv: 10,
      showWatch: true,
    });
    expect(lib.sanitizeFilters({ minEv: 2.3 }).minEv).toBe(2.5);
  });

  it('partitions opportunities into live / pre-game / watch / arbs, sorted like the engine', () => {
    const list = [
      opp({ id: 'pre-bet', verdict: 'BET', urgencyScore: 30 }),
      opp({ id: 'live-gone', isLive: true, verdict: 'BET_NOW', status: 'gone', urgencyScore: 99 }),
      opp({ id: 'live-high', isLive: true, verdict: 'BET_NOW', urgency: 'high', urgencyScore: 60 }),
      opp({ id: 'live-crit', isLive: true, verdict: 'BET_NOW', urgency: 'critical', urgencyScore: 90 }),
      opp({ id: 'pre-now', verdict: 'BET_NOW', urgencyScore: 35 }),
      opp({ id: 'watch', verdict: 'WATCH', evPct: 0.01 }),
      opp({ id: 'arb', type: 'arb', verdict: 'BET', evPct: 0.012 }),
      { id: 42 },
      null,
    ];
    const parts = lib.partitionOpportunities(list, noFilters());
    expect(parts.live.map((o) => o.id)).toEqual(['live-crit', 'live-high', 'live-gone']);
    expect(parts.pre.map((o) => o.id)).toEqual(['pre-now', 'pre-bet']);
    expect(parts.watch.map((o) => o.id)).toEqual(['watch']);
    expect(parts.arbs.map((o) => o.id)).toEqual(['arb']);
    expect(parts.hidden).toBe(0);
    expect(lib.actionableCount(parts)).toBe(5);
    expect(lib.partitionOpportunities('nope', noFilters()).live).toEqual([]);
  });

  it('applies league, live-only and min EV filters and counts what they hide', () => {
    const list = [
      opp({ id: 'nba-live', isLive: true, verdict: 'BET_NOW', evPct: 0.05 }),
      opp({ id: 'nfl-pre', league: 'NFL', evPct: 0.021 }),
      opp({ id: 'nba-pre-low', evPct: 0.021 }),
      opp({ id: 'nba-watch', verdict: 'WATCH', evPct: 0.01 }),
      opp({ id: 'nfl-arb', league: 'NFL', type: 'arb', evPct: 0.006 }),
    ];
    const nba = lib.partitionOpportunities(list, { ...noFilters(), leagues: ['NBA'] });
    expect([...nba.live, ...nba.pre, ...nba.arbs].map((o) => o.id).sort()).toEqual(['nba-live', 'nba-pre-low']);
    expect(nba.hidden).toBe(2);
    const liveOnly = lib.partitionOpportunities(list, { ...noFilters(), liveOnly: true });
    expect(liveOnly.pre).toEqual([]);
    expect(liveOnly.live.map((o) => o.id)).toEqual(['nba-live']);
    expect(liveOnly.hiddenWatch).toBe(1);
    const minEv = lib.partitionOpportunities(list, { ...noFilters(), minEv: 3 });
    expect(minEv.pre).toEqual([]);
    expect(minEv.live.map((o) => o.id)).toEqual(['nba-live']);
    expect(minEv.arbs.map((o) => o.id)).toEqual(['nfl-arb']); // arbs are judged on locked profit, not the EV slider
    expect(lib.passesFilters(opp({ id: 'edge', evPct: 0.02 }), { ...noFilters(), minEv: 2 })).toBe(true);
  });

  it('alerts once per new high/critical actionable pick, again when urgency rises, and stays bounded', () => {
    const seen = new Map<string, number>();
    const first = [opp({ id: 'a', urgency: 'critical', verdict: 'BET_NOW' }), opp({ id: 'b', urgency: 'high' })];
    expect(lib.selectAlerts(first, seen, false)).toEqual([]); // priming: record silently
    expect(seen.size).toBe(2);
    expect(lib.selectAlerts(first, seen, true)).toEqual([]); // nothing new
    const next = [
      ...first.slice(0, 1),
      opp({ id: 'b', urgency: 'critical' }), // rose high -> critical
      opp({ id: 'c', urgency: 'high' }), // new
      opp({ id: 'd', urgency: 'medium' }), // below threshold
      opp({ id: 'e', urgency: 'critical', verdict: 'WATCH' }), // watch never alerts
      opp({ id: 'f', urgency: 'critical', status: 'gone' }), // gone never alerts
    ];
    expect(lib.selectAlerts(next, seen, true).map((o) => o.id)).toEqual(['b', 'c']);
    expect(lib.selectAlerts(next, seen, true)).toEqual([]);
    const capped = new Map<string, number>();
    const many = Array.from({ length: 30 }, (_, i) => opp({ id: `x${i}`, urgency: 'high' }));
    lib.selectAlerts(many, capped, true, 10);
    expect(capped.size).toBe(10);
    expect([...capped.keys()][0]).toBe('x20');
  });

  it('computes the act-within countdown from firstSeen and expiresInSec', () => {
    const o = opp({ id: 'cd', firstSeen: 1_000_000, expiresInSec: 45 });
    const mid = lib.countdownInfo(o, 1_000_000 + 15_000);
    expect(mid.remaining).toBe(30_000);
    expect(mid.frac).toBeCloseTo(2 / 3, 9);
    expect(lib.countdownLabel(mid)).toBe('Act within ~30s');
    const late = lib.countdownInfo(o, 1_000_000 + 200_000);
    expect(late.frac).toBe(0);
    expect(lib.countdownLabel(late)).toBe('Open 3m · verify price');
  });

  it('builds a validated settings patch in server units with only changed fields', () => {
    const base = {
      bankroll: 1000,
      kellyMultiplier: 0.25,
      maxStakePct: 0.02,
      maxStakeAbs: 100,
      maxDailyExposurePct: 0.15,
      minEvPrematch: 0.02,
      minEvLive: 0.03,
      watchEv: 0.005,
      enabledLeagues: ['NBA', 'NFL'],
      showArbs: true,
    };
    const form = {
      bankroll: '1000',
      kelly: '0.25',
      maxStakePct: '2',
      maxStakeAbs: '100',
      dailyPct: '15',
      minEvPrematch: '2',
      minEvLive: '3',
      watchEv: '0.5',
      leagues: ['NFL', 'NBA'],
      showArbs: true,
    };
    expect(lib.buildSettingsPatch(form, base)).toEqual({ patch: {} });
    expect(lib.buildSettingsPatch({ ...form, bankroll: '2500', maxStakePct: '2.5', leagues: ['NBA'], showArbs: false }, base)).toEqual({
      patch: { bankroll: 2500, maxStakePct: 0.025, enabledLeagues: ['NBA'], showArbs: false },
    });
    expect(lib.buildSettingsPatch({ ...form, watchEv: '2.5' }, base).error).toMatch(/Watch list threshold/);
    expect(lib.buildSettingsPatch({ ...form, leagues: [] }, base).error).toMatch(/at least one league/);
    expect(lib.buildSettingsPatch({ ...form, kelly: '1.5' }, base).error).toMatch(/Kelly/);
    expect(lib.buildSettingsPatch({ ...form, bankroll: '' }, base).error).toMatch(/Bankroll/);
    expect(lib.buildSettingsPatch({ ...form, minEvLive: '60' }, base).error).toMatch(/Min EV live/);
    expect(lib.pctToFraction(1.5)).toBe(0.015);
    expect(lib.fractionToPct(0.015)).toBe(1.5);
    expect(lib.fractionToPct(0.001)).toBe(0.1);
  });
});

/*
 * Odds Hub dashboard — vanilla JS, no build step, no external resources.
 *
 * CSP (script-src 'self'; style-src 'self') rules this file follows:
 *  - no inline handlers, no `style=""` attributes, never setAttribute('style', ...);
 *    dynamic styling only via classes or the CSSOM (el.style.transform = ...), which CSP allows;
 *  - all server data is rendered with textContent / createElement, never innerHTML.
 *
 * The pure helpers at the top are also exported via `module.exports` when this file is loaded
 * outside a browser (unit tests); in the browser the IIFE boots the dashboard instead.
 */
(function () {
  'use strict';

  // =================================================================================================
  // Pure helpers (no DOM)
  // =================================================================================================

  var MINUS = '−';
  var DK_FALLBACK_URL = 'https://sportsbook.draftkings.com/';
  var MAX_ALERT_IDS = 1000;
  var URGENCY_RANK = { low: 0, medium: 1, high: 2, critical: 3 };
  var VERDICT_RANK = { BET_NOW: 0, BET: 1, WATCH: 2 };

  /** Known leagues (mirrors DEFAULT_LEAGUES in src/config.ts); state-provided leagues are merged in. */
  var KNOWN_LEAGUES = [
    ['NFL', 'NFL'],
    ['NCAAF', 'College Football'],
    ['NBA', 'NBA'],
    ['NCAAB', 'College Basketball'],
    ['WNBA', 'WNBA'],
    ['MLB', 'MLB'],
    ['NHL', 'NHL'],
    ['EPL', 'Premier League'],
    ['UCL', 'Champions League'],
    ['MLS', 'MLS'],
    ['LALIGA', 'La Liga'],
    ['SERIEA', 'Serie A'],
    ['BUNDESLIGA', 'Bundesliga'],
    ['LIGUE1', 'Ligue 1'],
    ['UFC', 'UFC / MMA'],
  ];

  var BOOK_NAMES = {
    draftkings: 'DraftKings',
    pinnacle: 'Pinnacle',
    fanduel: 'FanDuel',
    betmgm: 'BetMGM',
    williamhill_us: 'Caesars',
    caesars: 'Caesars',
    betonlineag: 'BetOnline',
    lowvig: 'LowVig',
    betrivers: 'BetRivers',
    bovada: 'Bovada',
    fanatics: 'Fanatics',
    espnbet: 'ESPN BET',
    ballybet: 'Bally Bet',
    betus: 'BetUS',
    mybookieag: 'MyBookie',
    hardrockbet: 'Hard Rock Bet',
    pointsbetus: 'PointsBet',
    superbook: 'SuperBook',
    wynnbet: 'WynnBET',
    unibet_us: 'Unibet',
    betparx: 'betPARX',
    fliff: 'Fliff',
    circasports: 'Circa Sports',
    betfair_ex_uk: 'Betfair Exchange',
    matchbook: 'Matchbook',
  };

  function isNum(n) {
    return typeof n === 'number' && isFinite(n);
  }

  function clamp(n, lo, hi) {
    return Math.min(hi, Math.max(lo, n));
  }

  /** Display American odds with a typographic minus: +110, −120. */
  function fmtAmerican(n) {
    if (!isNum(n)) return '—';
    var r = Math.round(n);
    if (r === 0) return '—';
    return r > 0 ? '+' + r : MINUS + Math.abs(r);
  }

  /** American odds with an ASCII sign, for inputs and copied text: +110, -120. */
  function fmtAmericanAscii(n) {
    if (!isNum(n)) return '';
    var r = Math.round(n);
    return r > 0 ? '+' + r : String(r);
  }

  function americanToDecimal(a) {
    if (!isNum(a) || Math.abs(a) < 100) return null;
    return a > 0 ? 1 + a / 100 : 1 + 100 / -a;
  }

  /** Parses "+110", "-120", "−120", "110", "even" -> integer American odds, or null. */
  function parseAmericanInput(raw) {
    if (typeof raw !== 'string') return null;
    var s = raw.trim().toLowerCase().replace(/[−‒–—]/g, '-').replace(/\s+/g, '');
    if (s === 'even' || s === 'evens' || s === 'ev') return 100;
    if (!/^[+-]?\d{3,6}(\.\d+)?$/.test(s)) return null;
    var n = Math.round(Number(s));
    if (!isNum(n) || Math.abs(n) < 100 || Math.abs(n) > 100000) return null;
    return n;
  }

  /** 0.052 -> "+5.2%"; signed=false -> "5.2%". */
  function fmtPct(x, digits, signed) {
    if (!isNum(x)) return '—';
    var d = digits === undefined ? 1 : digits;
    var v = x * 100;
    var abs = Math.abs(v).toFixed(d);
    if (Number(abs) === 0) return abs + '%';
    if (signed === false) return (v < 0 ? MINUS : '') + abs + '%';
    return (v > 0 ? '+' : MINUS) + abs + '%';
  }

  function groupThousands(intStr) {
    return intStr.replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  }

  /**
   * Dollars: 38 -> "$38", 1234.5 -> "$1,234.50", signed -> "+$12.40" / "−$20".
   * Cents are shown only when the amount has them (or opts.cents === true).
   */
  function fmtMoney(n, opts) {
    if (!isNum(n)) return '—';
    var o = opts || {};
    var cents = Math.round(Math.abs(n) * 100);
    var showCents = o.cents === true || (o.cents !== false && cents % 100 !== 0);
    var units = showCents ? cents : Math.round(cents / 100) * 100;
    var body = '$' + groupThousands(String(Math.floor(units / 100))) + (showCents ? '.' + String(units % 100).padStart(2, '0') : '');
    if (units === 0) return body;
    if (n < 0) return MINUS + body;
    return o.signed ? '+' + body : body;
  }

  function fmtCount(n) {
    if (!isNum(n)) return '—';
    return groupThousands(String(Math.round(n)));
  }

  function fmtCredits(n) {
    if (!isNum(n)) return '—';
    if (n >= 100000) return Math.round(n / 1000) + 'k';
    if (n >= 10000) return (n / 1000).toFixed(1) + 'k';
    return fmtCount(n);
  }

  /** Compact duration: 12s, 4m 12s (under 10 min), 42m, 3h 5m, 2d 4h. coarse drops seconds past 1 min. */
  function fmtAge(ms, coarse) {
    if (!isNum(ms)) return '—';
    var s = Math.max(0, Math.round(ms / 1000));
    if (s < 60) return s + 's';
    var m = Math.floor(s / 60);
    if (m < 60) {
      var rs = s % 60;
      return !coarse && m < 10 && rs ? m + 'm ' + rs + 's' : m + 'm';
    }
    var hrs = Math.floor(m / 60);
    if (hrs < 24) return m % 60 ? hrs + 'h ' + (m % 60) + 'm' : hrs + 'h';
    var d = Math.floor(hrs / 24);
    return hrs % 24 ? d + 'd ' + (hrs % 24) + 'h' : d + 'd';
  }

  function fmtAgo(ms) {
    if (!isNum(ms)) return '—';
    if (ms < 1500) return 'just now';
    return fmtAge(ms) + ' ago';
  }

  function sameLocalDay(a, b) {
    return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
  }

  function fmtClock(t, now) {
    if (!isNum(t)) return '—';
    var d = new Date(t);
    var n = new Date(isNum(now) ? now : Date.now());
    var time = d.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
    if (sameLocalDay(d, n)) return time;
    var tomorrow = new Date(n.getTime());
    tomorrow.setDate(n.getDate() + 1);
    if (sameLocalDay(d, tomorrow)) return 'Tomorrow ' + time;
    var yesterday = new Date(n.getTime());
    yesterday.setDate(n.getDate() - 1);
    if (sameLocalDay(d, yesterday)) return 'Yesterday ' + time;
    return d.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' }) + ' ' + time;
  }

  function fmtStart(t, now) {
    if (!isNum(t)) return '—';
    if (t <= now) return 'Started ' + fmtAge(now - t, true) + ' ago';
    return fmtClock(t, now) + ' · in ' + fmtAge(t - now, true);
  }

  function trimNum(x) {
    return String(Number(x.toFixed(2)));
  }

  function fmtLine(line) {
    if (!isNum(line)) return '';
    if (line === 0) return 'PK';
    return (line > 0 ? '+' : '-') + trimNum(Math.abs(line));
  }

  /** Mirrors describePick() in the engine: "Celtics -3.5", "Over 224.5", "Knicks ML", "Draw". */
  function describePick(kind, side, line, home, away) {
    var team = side === 'home' ? home : side === 'away' ? away : null;
    if (kind === 'moneyline') return side === 'draw' ? 'Draw' : (team || side) + ' ML';
    if (kind === 'spread') return (team || side) + ' ' + fmtLine(line);
    if (kind === 'total') return (side === 'over' ? 'Over' : 'Under') + (isNum(line) ? ' ' + trimNum(line) : '');
    return String(side);
  }

  function bookName(key) {
    if (typeof key !== 'string' || key === '') return 'Other book';
    var k = key.toLowerCase();
    if (Object.prototype.hasOwnProperty.call(BOOK_NAMES, k)) return BOOK_NAMES[k];
    return k
      .split(/[_\s-]+/)
      .filter(Boolean)
      .map(function (w) {
        return w.charAt(0).toUpperCase() + w.slice(1);
      })
      .join(' ');
  }

  /** 'pinnacle' -> "Pinnacle no-vig"; 'consensus(5)' -> "Consensus of 5 books". */
  function sharpLabel(src) {
    if (typeof src !== 'string' || src === '') return 'Sharp reference';
    var m = /^consensus\((\d+)\)$/i.exec(src);
    if (m) return 'Consensus of ' + m[1] + ' books';
    return bookName(src) + ' no-vig';
  }

  function verdictLabel(v) {
    return v === 'BET_NOW' ? 'BET NOW' : v === 'BET' ? 'BET' : 'WATCH';
  }

  function fmtScore(score) {
    if (!score || typeof score !== 'object') return '';
    var parts = [];
    if (isNum(score.away) && isNum(score.home)) parts.push(score.away + '–' + score.home);
    var pc = [score.period, score.clock]
      .filter(function (s) {
        return typeof s === 'string' && s.trim() !== '';
      })
      .join(' ');
    if (pc) parts.push(pc);
    return parts.join(' · ');
  }

  /** Only https links are ever used; anything else falls back to the DraftKings sportsbook home page. */
  function safeDkUrl(u) {
    if (typeof u === 'string' && u !== '') {
      try {
        var url = new URL(u);
        if (url.protocol === 'https:') return url.href;
      } catch (e) {
        /* fall through to the default */
      }
    }
    return DK_FALLBACK_URL;
  }

  function isOpportunity(o) {
    return (
      !!o &&
      typeof o === 'object' &&
      typeof o.id === 'string' &&
      (o.type === 'ev' || o.type === 'arb') &&
      typeof o.pick === 'string' &&
      (o.status === 'active' || o.status === 'gone')
    );
  }

  function isDashboardState(st) {
    return (
      !!st &&
      typeof st === 'object' &&
      isNum(st.generatedAt) &&
      Array.isArray(st.opportunities) &&
      !!st.health &&
      typeof st.health === 'object' &&
      !!st.settings &&
      typeof st.settings === 'object'
    );
  }

  /** Same ordering as the engine's sortOpportunities: active first, BET_NOW > BET > WATCH, urgency, EV, id. */
  function compareOpps(a, b) {
    var ga = a.status === 'gone' ? 1 : 0;
    var gb = b.status === 'gone' ? 1 : 0;
    if (ga !== gb) return ga - gb;
    var va = VERDICT_RANK[a.verdict] !== undefined ? VERDICT_RANK[a.verdict] : 3;
    var vb = VERDICT_RANK[b.verdict] !== undefined ? VERDICT_RANK[b.verdict] : 3;
    if (va !== vb) return va - vb;
    var ua = isNum(a.urgencyScore) ? a.urgencyScore : 0;
    var ub = isNum(b.urgencyScore) ? b.urgencyScore : 0;
    if (ua !== ub) return ub - ua;
    var ea = isNum(a.evPct) ? a.evPct : 0;
    var eb = isNum(b.evPct) ? b.evPct : 0;
    if (ea !== eb) return eb - ea;
    return a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
  }

  function defaultFilters() {
    return { leagues: [], liveOnly: false, minEv: 0, showWatch: false };
  }

  /** Validates filters loaded from localStorage (anything malformed falls back to defaults). */
  function sanitizeFilters(raw) {
    var f = defaultFilters();
    if (!raw || typeof raw !== 'object') return f;
    if (Array.isArray(raw.leagues)) {
      f.leagues = raw.leagues
        .filter(function (x) {
          return typeof x === 'string' && x.length > 0 && x.length <= 40;
        })
        .slice(0, 60);
    }
    f.liveOnly = raw.liveOnly === true;
    f.showWatch = raw.showWatch === true;
    if (isNum(raw.minEv)) f.minEv = clamp(Math.round(raw.minEv * 2) / 2, 0, 10);
    return f;
  }

  /** minEv is in percent (2 = 2%) and applies to EV picks only (arbs are judged on locked profit). */
  function passesFilters(o, f) {
    if (f.leagues.length > 0 && f.leagues.indexOf(o.league) < 0) return false;
    if (f.liveOnly && !o.isLive) return false;
    if (o.type !== 'arb' && f.minEv > 0 && !(isNum(o.evPct) && o.evPct * 100 >= f.minEv - 1e-9)) return false;
    return true;
  }

  /**
   * Splits opportunities into dashboard sections after filtering.
   * live/pre: actionable EV picks (BET_NOW/BET); watch: WATCH verdicts; arbs: type 'arb'.
   * `keep` (optional Set/Map of ids): WATCH picks that were actionable a moment ago stay in live/pre (shown greyed as
   * "price moved") instead of silently jumping to the hidden watch list.
   * hidden: active actionable picks (EV or arb) removed by the filters.
   */
  function partitionOpportunities(list, f, keep) {
    var out = { live: [], pre: [], watch: [], arbs: [], hidden: 0, hiddenWatch: 0 };
    if (!Array.isArray(list)) return out;
    for (var i = 0; i < list.length; i++) {
      var o = list[i];
      if (!isOpportunity(o)) continue;
      if (!passesFilters(o, f)) {
        if (o.status === 'active') {
          if (o.verdict === 'WATCH' && o.type !== 'arb') out.hiddenWatch++;
          else out.hidden++;
        }
        continue;
      }
      if (o.type === 'arb') out.arbs.push(o);
      else if (o.verdict === 'WATCH' && !(keep && o.status === 'active' && keep.has(o.id))) out.watch.push(o);
      else if (o.isLive) out.live.push(o);
      else out.pre.push(o);
    }
    out.live.sort(compareOpps);
    out.pre.sort(compareOpps);
    out.watch.sort(compareOpps);
    out.arbs.sort(compareOpps);
    return out;
  }

  function isActionable(o) {
    return o.status === 'active' && o.verdict !== 'WATCH';
  }

  /** Number of active BET_NOW/BET picks the user can act on (EV picks + arbs), for the tab title. */
  function actionableCount(parts) {
    var n = 0;
    var lists = [parts.live, parts.pre, parts.arbs];
    for (var i = 0; i < lists.length; i++) {
      for (var j = 0; j < lists[i].length; j++) if (isActionable(lists[i][j])) n++;
    }
    return n;
  }

  /**
   * Returns opportunities that should trigger an alert: active, not WATCH, urgency high/critical, and either
   * never alerted before or whose urgency rose since the last alert. Mutates `seen` (id -> urgency rank,
   * insertion ordered, capped at `cap`). When `primed` is false the ids are recorded silently.
   */
  function selectAlerts(opps, seen, primed, cap) {
    var out = [];
    var max = isNum(cap) && cap > 0 ? cap : MAX_ALERT_IDS;
    for (var i = 0; i < opps.length; i++) {
      var o = opps[i];
      if (!isActionable(o)) continue;
      var rank = URGENCY_RANK[o.urgency] !== undefined ? URGENCY_RANK[o.urgency] : 0;
      if (rank < URGENCY_RANK.high) continue;
      var prev = seen.get(o.id);
      if (prev === undefined || rank > prev) {
        seen.delete(o.id);
        seen.set(o.id, rank);
        if (primed) out.push(o);
      }
    }
    while (seen.size > max) {
      var first = seen.keys().next();
      if (first.done) break;
      seen.delete(first.value);
    }
    return out;
  }

  /**
   * Countdown for a card: time left of the expected price window, measured from when the pick became actionable
   * (actionableSince), falling back to firstSeen for older servers.
   */
  function countdownInfo(o, now) {
    var total = Math.max(1000, (isNum(o.expiresInSec) ? o.expiresInSec : 0) * 1000);
    var first = isNum(o.actionableSince) ? o.actionableSince : isNum(o.firstSeen) ? o.firstSeen : now;
    var elapsed = Math.max(0, now - first);
    var remaining = total - elapsed;
    return { remaining: remaining, elapsed: elapsed, frac: clamp(remaining / total, 0, 1) };
  }

  function countdownLabel(info) {
    if (info.remaining >= 1000) return 'Act within ~' + fmtAge(info.remaining, true);
    return 'Open ' + fmtAge(info.elapsed, true) + ' · verify price';
  }

  /**
   * The part of a reason that only changes when the pick does: the ages the engine writes into reasons every second
   * ("data 12s old", "in the last 40s") are masked, so a card is not rebuilt (and its buttons replaced) every tick.
   */
  function reasonShape(r) {
    return String(r)
      .replace(/\(data \d+s old\)/g, '(data #s old)')
      .replace(/in the last \d+s/g, 'in the last #s');
  }

  /**
   * League chips in a stable order: the configured league order first, then anything else alphabetically.
   * Counts never reorder chips (a chip moving under the finger selects the wrong league).
   */
  function chipOrder(keys, configured) {
    var rank = new Map();
    (Array.isArray(configured) ? configured : []).forEach(function (k, i) {
      if (!rank.has(k)) rank.set(k, i);
    });
    var uniq = [];
    (Array.isArray(keys) ? keys : []).forEach(function (k) {
      if (typeof k === 'string' && uniq.indexOf(k) < 0) uniq.push(k);
    });
    return uniq.sort(function (a, b) {
      var ra = rank.has(a) ? rank.get(a) : Infinity;
      var rb = rank.has(b) ? rank.get(b) : Infinity;
      if (ra !== rb) return ra - rb;
      return a < b ? -1 : a > b ? 1 : 0;
    });
  }

  /**
   * Status line of the "I placed it" dialog for the pick as the server has it now (`cur`, null when it expired) and
   * as it was when the dialog opened (`snap`). kind: ok | warn.
   */
  function placeLiveStatus(cur, snap) {
    if (!cur) {
      return {
        kind: 'warn',
        text: 'This pick has expired on the server. If you already placed the bet, log it anyway: the details shown here are saved with it.',
      };
    }
    if (cur.status === 'gone') return { kind: 'warn', text: 'Price moved — this pick is gone. Log it only if you already placed the bet.' };
    var take = fmtAmerican(cur.minAcceptableAmerican);
    var dkDec = americanToDecimal(cur.dkAmerican);
    var minDec = americanToDecimal(cur.minAcceptableAmerican);
    var below = cur.type !== 'arb' && dkDec !== null && minDec !== null && dkDec < minDec - 1e-9;
    var was = snap && cur.dkAmerican !== snap.dkAmerican ? ' (was ' + fmtAmerican(snap.dkAmerican) + ' when you opened this)' : '';
    if (cur.verdict === 'WATCH' || below) {
      return {
        kind: 'warn',
        text: 'Price moved below your minimum — DraftKings now ' + fmtAmerican(cur.dkAmerican) + ', needs ' + take + ' or better' + was + '. Log it only if you already placed the bet.',
      };
    }
    return { kind: 'ok', text: 'DraftKings now ' + fmtAmerican(cur.dkAmerican) + ' · take at ' + take + ' or better' + was };
  }

  /** The pick fields the server needs to log a bet on a pick it no longer tracks. */
  function pickSnapshot(o) {
    return {
      eventId: o.eventId,
      league: o.league,
      eventName: o.eventName,
      startTime: o.startTime,
      pick: o.pick,
      kind: o.kind,
      side: o.side,
      line: o.line === undefined ? null : o.line,
      isLive: o.isLive === true,
      fairProb: o.fairProb,
    };
  }

  /**
   * Remembers actionable picks (id -> {o, seenAt}) so a bet placed on a pick that has since left the board can still be
   * logged. Newest first when listed; entries older than maxAgeMs or beyond `cap` are dropped. Mutates and returns `recent`.
   */
  function rememberPicks(recent, opps, now, maxAgeMs, cap) {
    for (var i = 0; i < opps.length; i++) {
      var o = opps[i];
      if (!isOpportunity(o) || o.status !== 'active' || o.verdict === 'WATCH') continue;
      recent.delete(o.id);
      recent.set(o.id, { o: o, seenAt: now });
    }
    recent.forEach(function (v, id) {
      if (!v || !isNum(v.seenAt) || now - v.seenAt > maxAgeMs) recent.delete(id);
    });
    while (recent.size > cap) recent.delete(recent.keys().next().value);
    return recent;
  }

  /** EV per $1 at the given American odds for a fair win probability. */
  function evAtAmerican(prob, american) {
    var dec = americanToDecimal(american);
    if (dec === null || !isNum(prob)) return null;
    return prob * dec - 1;
  }

  function pctToFraction(p) {
    return Math.round(p * 1e4) / 1e6;
  }

  function fractionToPct(f) {
    return Math.round(f * 1e6) / 1e4;
  }

  function sameNumber(a, b) {
    return isNum(a) && isNum(b) && Math.abs(a - b) < 1e-9;
  }

  /**
   * Validates the settings form (values in display units: dollars, ×, percent) and returns the patch of
   * changed RuntimeSettings fields relative to `base`, or an error message.
   */
  function buildSettingsPatch(form, base) {
    var num = function (v) {
      if (typeof v === 'number') return v;
      if (typeof v !== 'string' || v.trim() === '') return NaN;
      return Number(v.trim());
    };
    var bankroll = num(form.bankroll);
    var kelly = num(form.kelly);
    var maxPct = num(form.maxStakePct);
    var maxAbs = num(form.maxStakeAbs);
    var daily = num(form.dailyPct);
    var minPre = num(form.minEvPrematch);
    var minLive = num(form.minEvLive);
    var watch = num(form.watchEv);
    var checks = [
      [bankroll, 1, 1e8, 'Bankroll must be between $1 and $100,000,000'],
      [kelly, 0.01, 1, 'Kelly multiplier must be between 0.01 and 1'],
      [maxPct, 0.1, 100, 'Max stake % must be between 0.1% and 100%'],
      [maxAbs, 1, 1e7, 'Max stake $ must be between $1 and $10,000,000'],
      [daily, 0.1, 100, 'Daily exposure must be between 0.1% and 100%'],
      [minPre, 0, 50, 'Min EV pre-game must be between 0% and 50%'],
      [minLive, 0, 50, 'Min EV live must be between 0% and 50%'],
      [watch, 0, 50, 'Watch list threshold must be between 0% and 50%'],
    ];
    for (var i = 0; i < checks.length; i++) {
      var c = checks[i];
      if (!isNum(c[0]) || c[0] < c[1] || c[0] > c[2]) return { error: c[3] };
    }
    if (watch > Math.min(minPre, minLive) + 1e-9) {
      return { error: 'Watch list threshold must be at or below both minimum EV settings' };
    }
    var leagues = Array.isArray(form.leagues) ? form.leagues.slice() : [];
    if (leagues.length === 0) return { error: 'Enable at least one league' };
    var next = {
      bankroll: bankroll,
      kellyMultiplier: kelly,
      maxStakePct: pctToFraction(maxPct),
      maxStakeAbs: maxAbs,
      maxDailyExposurePct: pctToFraction(daily),
      minEvPrematch: pctToFraction(minPre),
      minEvLive: pctToFraction(minLive),
      watchEv: pctToFraction(watch),
      enabledLeagues: leagues,
      showArbs: form.showArbs === true,
    };
    var patch = {};
    var b = base || {};
    Object.keys(next).forEach(function (k) {
      var v = next[k];
      if (k === 'enabledLeagues') {
        var prev = Array.isArray(b[k]) ? b[k].slice().sort().join(',') : null;
        if (prev !== v.slice().sort().join(',')) patch[k] = v;
      } else if (k === 'showArbs') {
        if (b[k] !== v) patch[k] = v;
      } else if (!sameNumber(b[k], v)) {
        patch[k] = v;
      }
    });
    return { patch: patch };
  }

  var lib = {
    MINUS: MINUS,
    DK_FALLBACK_URL: DK_FALLBACK_URL,
    KNOWN_LEAGUES: KNOWN_LEAGUES,
    fmtAmerican: fmtAmerican,
    fmtAmericanAscii: fmtAmericanAscii,
    americanToDecimal: americanToDecimal,
    parseAmericanInput: parseAmericanInput,
    fmtPct: fmtPct,
    fmtMoney: fmtMoney,
    fmtCount: fmtCount,
    fmtCredits: fmtCredits,
    fmtAge: fmtAge,
    fmtAgo: fmtAgo,
    fmtStart: fmtStart,
    fmtLine: fmtLine,
    describePick: describePick,
    bookName: bookName,
    sharpLabel: sharpLabel,
    verdictLabel: verdictLabel,
    fmtScore: fmtScore,
    safeDkUrl: safeDkUrl,
    isOpportunity: isOpportunity,
    isDashboardState: isDashboardState,
    compareOpps: compareOpps,
    sanitizeFilters: sanitizeFilters,
    passesFilters: passesFilters,
    partitionOpportunities: partitionOpportunities,
    actionableCount: actionableCount,
    selectAlerts: selectAlerts,
    countdownInfo: countdownInfo,
    countdownLabel: countdownLabel,
    reasonShape: reasonShape,
    chipOrder: chipOrder,
    placeLiveStatus: placeLiveStatus,
    pickSnapshot: pickSnapshot,
    rememberPicks: rememberPicks,
    evAtAmerican: evAtAmerican,
    pctToFraction: pctToFraction,
    fractionToPct: fractionToPct,
    buildSettingsPatch: buildSettingsPatch,
  };

  if (typeof module === 'object' && module !== null && typeof module.exports === 'object') {
    module.exports = lib;
  }
  if (typeof window === 'undefined' || typeof document === 'undefined') return;

  // =================================================================================================
  // Browser app
  // =================================================================================================

  var SVG_NS = 'http://www.w3.org/2000/svg';
  var LS_PREFIX = 'oddsHub.';
  var POLL_MS = 10000;
  var STALL_MS = 20000;
  var FORCE_RECONNECT_MS = 45000;
  var NEW_FLASH_MS = 4000;
  var MAX_TOASTS = 4;
  var GONE_FADE_SEC = 90;
  /** A pick that drops from BET/BET NOW to WATCH stays in its section, greyed, this long (or until it recovers). */
  var DEMOTED_KEEP_MS = 90000;
  /** Picks offered for "log a bet on a pick that has disappeared" (kept in this browser only). */
  var RECENT_KEEP_MS = 6 * 3600 * 1000;
  var RECENT_CAP = 30;

  function lsGet(key, def) {
    try {
      var raw = window.localStorage.getItem(LS_PREFIX + key);
      if (raw === null) return def;
      return JSON.parse(raw);
    } catch (e) {
      return def;
    }
  }

  function lsSet(key, value) {
    try {
      window.localStorage.setItem(LS_PREFIX + key, JSON.stringify(value));
    } catch (e) {
      /* storage unavailable (private mode / quota): preferences just won't persist */
    }
  }

  function loadRecent() {
    var map = new Map();
    var raw = lsGet('recent', []);
    if (!Array.isArray(raw)) return map;
    raw.slice(-RECENT_CAP).forEach(function (e) {
      if (e && isOpportunity(e.o) && isNum(e.seenAt) && Math.abs(Date.now() - e.seenAt) < RECENT_KEEP_MS) map.set(e.o.id, { o: e.o, seenAt: e.seenAt });
    });
    return map;
  }

  function saveRecent(force) {
    var sig = Array.from(S.recent.keys()).join('\n');
    if (!force && sig === S.recentSavedSig) return;
    S.recentSavedSig = sig;
    lsSet('recent', Array.from(S.recent.values()));
  }

  function applyTheme(theme) {
    var t = theme === 'light' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', t);
    return t;
  }

  // Apply the saved theme before first paint (this script is loaded synchronously in <head>).
  applyTheme(lsGet('theme', 'dark'));

  var S = {
    state: null,
    offset: 0,
    lastStateAt: 0,
    conn: 'connecting',
    es: null,
    pollTimer: null,
    reconnectTimer: null,
    reconnectDelay: 2000,
    firstStateTimer: null,
    lastForcedReconnect: 0,
    cards: new Map(),
    /** Ids that were actionable (BET / BET NOW) in the previous render: new flash + "price moved" detection. */
    actionableIds: new Set(),
    /** id -> when it dropped from actionable to WATCH (kept in its section greyed for DEMOTED_KEEP_MS). */
    demoted: new Map(),
    recent: loadRecent(),
    recentSavedSig: '',
    recentSig: '',
    alerted: new Map(),
    primed: false,
    parts: null,
    filters: sanitizeFilters(lsGet('filters', null)),
    tab: 'opps',
    alertsOn: lsGet('alerts', false) === true,
    audio: null,
    bets: null,
    betsLoading: false,
    betFilter: 'all',
    editBets: new Set(),
    summary: null,
    logged: new Map(),
    place: null,
    settingsBase: null,
    chipsSig: '',
    bannersSig: '',
    pickSeq: 0,
    warned: false,
  };

  var el = {};

  function $(id) {
    return document.getElementById(id);
  }

  function serverNow() {
    return Date.now() + S.offset;
  }

  // ------------------------------------------------------------------------------------------- DOM utils

  function append(parent, child) {
    if (child === null || child === undefined || child === false) return;
    if (Array.isArray(child)) {
      for (var i = 0; i < child.length; i++) append(parent, child[i]);
      return;
    }
    if (typeof child === 'object' && child.nodeType) parent.appendChild(child);
    else parent.appendChild(document.createTextNode(String(child)));
  }

  function h(tag, cls, children) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    append(node, children);
    return node;
  }

  function icon(name, cls) {
    var svg = document.createElementNS(SVG_NS, 'svg');
    svg.setAttribute('class', 'icon' + (cls ? ' ' + cls : ''));
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('focusable', 'false');
    var use = document.createElementNS(SVG_NS, 'use');
    use.setAttribute('href', '#i-' + name);
    svg.appendChild(use);
    return svg;
  }

  function button(cls, children, action) {
    var b = h('button', cls, children);
    b.type = 'button';
    if (action) b.setAttribute('data-action', action);
    return b;
  }

  /** Registers an element whose text is a relative time, refreshed every second by tick(). */
  function rel(node, t, kind) {
    if (isNum(t)) {
      node.setAttribute('data-rel', kind);
      node.setAttribute('data-t', String(Math.round(t)));
    } else {
      node.removeAttribute('data-rel');
    }
    renderRel(node, serverNow());
    return node;
  }

  function renderRel(node, now) {
    var kind = node.getAttribute('data-rel');
    if (!kind) return;
    var t = Number(node.getAttribute('data-t'));
    var text;
    if (!isNum(t)) text = '—';
    else if (kind === 'ago') text = fmtAgo(now - t);
    else if (kind === 'age') text = fmtAge(now - t);
    else if (kind === 'start') text = fmtStart(t, now);
    else text = fmtClock(t, now);
    if (node.textContent !== text) node.textContent = text;
  }

  function setText(node, text) {
    if (node && node.textContent !== text) node.textContent = text;
  }

  // ------------------------------------------------------------------------------------------- API

  function ApiError(message, status) {
    this.name = 'ApiError';
    this.message = message;
    this.status = status;
  }
  ApiError.prototype = Object.create(Error.prototype);
  ApiError.prototype.constructor = ApiError;

  function api(method, url, body) {
    var opts = { method: method, headers: { Accept: 'application/json' }, cache: 'no-store', credentials: 'same-origin' };
    if (body !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    return fetch(url, opts).then(
      function (res) {
        return res
          .json()
          .catch(function () {
            return null;
          })
          .then(function (data) {
            if (!res.ok) {
              var msg = data && typeof data.error === 'string' && data.error ? data.error : 'Request failed (HTTP ' + res.status + ')';
              throw new ApiError(msg, res.status);
            }
            return data;
          });
      },
      function () {
        throw new ApiError('Network error — is the dashboard server running?', 0);
      }
    );
  }

  // ------------------------------------------------------------------------------------------- toasts

  function toast(message, kind, timeoutMs) {
    var k = kind || 'ok';
    var node = h('div', 'toast toast-' + k, [icon(k === 'error' ? 'alert' : k === 'info' ? 'info' : 'check'), h('span', 'toast-text', message)]);
    el.toasts.appendChild(node);
    while (el.toasts.children.length > MAX_TOASTS) el.toasts.removeChild(el.toasts.firstElementChild);
    window.setTimeout(function () {
      node.classList.add('toast-out');
      window.setTimeout(function () {
        if (node.parentNode) node.parentNode.removeChild(node);
      }, 300);
    }, timeoutMs || (k === 'error' ? 7000 : 4000));
  }

  // ------------------------------------------------------------------------------------------- state

  function applyState(st) {
    if (!isDashboardState(st)) {
      if (!S.warned) {
        S.warned = true;
        console.warn('Odds Hub: ignoring malformed state payload');
      }
      return;
    }
    // Ignore out-of-order or repeated frames (e.g. a poll racing the stream). A jump back of more than a
    // minute means the server restarted with a different clock, so accept that.
    if (S.state && st.generatedAt <= S.state.generatedAt && S.state.generatedAt - st.generatedAt < 60000) return;
    S.offset = st.generatedAt - Date.now();
    S.lastStateAt = Date.now();
    S.state = st;
    if (st.betSummary && typeof st.betSummary === 'object') S.summary = st.betSummary;
    rememberPicks(S.recent, st.opportunities, st.generatedAt, RECENT_KEEP_MS, RECENT_CAP);
    saveRecent(false);
    renderAll();
  }

  function findOpp(id) {
    if (!S.state) return null;
    var list = S.state.opportunities;
    for (var i = 0; i < list.length; i++) if (list[i] && list[i].id === id) return list[i];
    return null;
  }

  function primarySource(health) {
    var sources = health && Array.isArray(health.sources) ? health.sources : [];
    for (var i = 0; i < sources.length; i++) if (/odds\s*api/i.test(String(sources[i].name))) return sources[i];
    return sources[0] || null;
  }

  function knownLeagues() {
    var map = new Map();
    KNOWN_LEAGUES.forEach(function (l) {
      map.set(l[0], l[1]);
    });
    var st = S.state;
    if (st) {
      if (Array.isArray(st.leagues)) {
        st.leagues.forEach(function (l) {
          if (typeof l === 'string') {
            if (!map.has(l)) map.set(l, l);
          } else if (l && typeof l.key === 'string') {
            map.set(l.key, typeof l.name === 'string' && l.name ? l.name : l.key);
          }
        });
      }
      (st.settings.enabledLeagues || []).forEach(function (k) {
        if (typeof k === 'string' && !map.has(k)) map.set(k, k);
      });
      st.opportunities.forEach(function (o) {
        if (o && typeof o.league === 'string' && !map.has(o.league)) map.set(o.league, o.league);
      });
    }
    return Array.from(map.entries());
  }

  function renderAll() {
    renderTopbar();
    renderBanners();
    renderOpportunities();
    renderCounts();
    checkAlerts();
    if (S.place && el.dlgPlace.open) updatePlaceLive();
    if (el.dlgHealth.open) renderHealth();
    renderBetSummary();
    renderRecent();
  }

  // ------------------------------------------------------------------------------------------- top bar

  function renderTopbar() {
    var st = S.state;
    if (!st) return;
    var hl = st.health;
    var feed = primarySource(hl);
    el.demoBadge.hidden = !hl.demoMode;

    var status = feed ? feed.status : 'unknown';
    var text;
    if (hl.demoMode) {
      text = 'Demo feed';
      status = feed && feed.status !== 'disabled' ? feed.status : 'ok';
    } else if (!feed) {
      text = 'No feed';
    } else if (feed.status === 'disabled') {
      text = 'No API key';
    } else {
      var label = feed.status === 'ok' ? 'Odds API' : feed.status === 'degraded' ? 'Odds API degraded' : 'Odds API down';
      text = isNum(hl.oddsApiCreditsRemaining) ? label + ' · ' + fmtCredits(hl.oddsApiCreditsRemaining) + ' credits' : label;
    }
    el.feedDot.setAttribute('data-status', status);
    setText(el.feedText, text);
    el.feedBtn.setAttribute('aria-label', 'Data source: ' + text + ' — open system health');

    rel(el.statUpdated, st.generatedAt, 'ago');
    setText(el.statLive, fmtCount(hl.liveEvents));
    setText(el.statBankroll, fmtMoney(st.settings.bankroll));
    setText(el.statLeft, fmtMoney(st.remainingDailyExposure));
    var cap = st.settings.bankroll * st.settings.maxDailyExposurePct;
    setText(el.statLeftOf, isNum(cap) ? ' of ' + fmtMoney(cap) : '');
    el.statLeft.classList.toggle('is-zero', isNum(st.remainingDailyExposure) && st.remainingDailyExposure <= 0);
  }

  function setConn(state) {
    S.conn = state;
    renderConn();
  }

  function renderConn() {
    var state = S.conn;
    var text = 'Connecting…';
    if (state === 'live') {
      var quiet = S.lastStateAt ? Date.now() - S.lastStateAt : 0;
      if (quiet > STALL_MS) {
        state = 'stalled';
        text = 'No updates ' + fmtAge(quiet);
      } else {
        text = 'Live';
      }
    } else if (state === 'reconnecting') {
      text = S.pollTimer ? 'Reconnecting… (polling)' : 'Reconnecting…';
    }
    el.conn.setAttribute('data-state', state);
    setText(el.connText, text);
  }

  function renderBanners() {
    var st = S.state;
    var items = [];
    if (st) {
      var hl = st.health;
      var feed = primarySource(hl);
      if (hl.demoMode) {
        items.push(['warn', 'Demo mode — these are simulated odds, not real prices. Do not bet on them.']);
      } else if (feed && (feed.status === 'down' || feed.status === 'degraded')) {
        var err = feed.lastError ? ': ' + feed.lastError : '';
        items.push([
          feed.status === 'down' ? 'error' : 'warn',
          feed.name + ' is ' + feed.status + err + '. Prices may be stale; retrying automatically.',
        ]);
      }
      if (!hl.demoMode && isNum(hl.oddsApiCreditsRemaining) && hl.oddsApiCreditsRemaining < 500) {
        items.push(['warn', 'Only ' + fmtCount(hl.oddsApiCreditsRemaining) + ' Odds API credits left this period — polling slows down to protect the reserve.']);
      }
      if (isNum(st.remainingDailyExposure) && st.remainingDailyExposure <= 0) {
        items.push(['info', 'Daily exposure limit reached — suggested stakes show $0 until tomorrow. You can raise the limit in Settings.']);
      }
    }
    if (S.conn === 'reconnecting' && st) {
      items.push(['warn', 'Live connection lost — reconnecting. Prices below may be out of date.']);
    }
    var sig = JSON.stringify(items);
    if (sig === S.bannersSig) return;
    S.bannersSig = sig;
    el.banners.replaceChildren();
    items.forEach(function (it) {
      el.banners.appendChild(h('div', 'banner banner-' + it[0], [icon(it[0] === 'info' ? 'info' : 'alert'), h('span', null, it[1])]));
    });
  }

  // ------------------------------------------------------------------------------------------- filters

  function saveFilters() {
    lsSet('filters', S.filters);
  }

  function renderChips() {
    var st = S.state;
    var counts = new Map();
    var keys = [];
    if (st) {
      (st.settings.enabledLeagues || []).forEach(function (k) {
        if (typeof k === 'string') keys.push(k);
      });
      st.opportunities.forEach(function (o) {
        if (!isOpportunity(o) || o.status !== 'active') return;
        keys.push(o.league);
        if (o.verdict !== 'WATCH') counts.set(o.league, (counts.get(o.league) || 0) + 1);
      });
    }
    S.filters.leagues.forEach(function (k) {
      keys.push(k);
    });
    // Stable order (configuration order); only the count badges change as picks come and go.
    var order = chipOrder(
      keys,
      knownLeagues().map(function (e) {
        return e[0];
      })
    );
    var sig = JSON.stringify([order, S.filters.leagues]);
    if (sig !== S.chipsSig) {
      S.chipsSig = sig;
      el.chips.replaceChildren();
      var all = button('chip-btn', 'All leagues', 'league-all');
      all.setAttribute('aria-pressed', String(S.filters.leagues.length === 0));
      el.chips.appendChild(all);
      order.forEach(function (key) {
        var b = button('chip-btn', [h('span', null, key), h('span', 'chip-count')], 'league');
        b.setAttribute('data-league', key);
        b.setAttribute('aria-pressed', String(S.filters.leagues.indexOf(key) >= 0));
        el.chips.appendChild(b);
      });
    }
    Array.prototype.forEach.call(el.chips.querySelectorAll('[data-league]'), function (b) {
      var n = counts.get(b.getAttribute('data-league')) || 0;
      var badge = b.querySelector('.chip-count');
      if (badge) {
        setText(badge, n > 0 ? String(n) : '');
        badge.hidden = n === 0;
      }
      b.classList.toggle('is-empty', n === 0);
    });
  }

  function syncFilterControls() {
    el.fLive.checked = S.filters.liveOnly;
    el.fWatch.checked = S.filters.showWatch;
    el.fMinEv.value = String(S.filters.minEv);
    setText(el.fMinEvOut, S.filters.minEv > 0 ? S.filters.minEv.toFixed(1) + '%' : 'Any');
  }

  function onFiltersChanged() {
    saveFilters();
    syncFilterControls();
    renderOpportunities();
    renderCounts();
    // Picks revealed by a looser filter are not "new": record them without alerting.
    if (S.parts) selectAlerts(S.parts.live.concat(S.parts.pre, S.parts.arbs), S.alerted, false, MAX_ALERT_IDS);
  }

  function resetFilters() {
    S.filters = defaultFilters();
    onFiltersChanged();
  }

  // ------------------------------------------------------------------------------------------- opportunities

  /** Ids that just dropped from BET / BET NOW to WATCH keep their place (greyed) for DEMOTED_KEEP_MS. */
  function updateDemoted(list, now) {
    var next = new Map();
    list.forEach(function (o) {
      if (!isOpportunity(o) || o.type === 'arb' || o.status !== 'active' || o.verdict !== 'WATCH') return;
      var since = S.demoted.get(o.id);
      if (since === undefined && S.actionableIds.has(o.id)) since = now;
      if (since !== undefined && now - since < DEMOTED_KEEP_MS) next.set(o.id, since);
    });
    S.demoted = next;
  }

  function renderOpportunities() {
    renderChips();
    var st = S.state;
    var list = st ? st.opportunities : [];
    updateDemoted(list, Date.now());
    var parts = partitionOpportunities(list, S.filters, S.demoted);
    S.parts = parts;
    var keep = new Set();
    var now = serverNow();

    var build = function (list, variant) {
      return list.map(function (o) {
        keep.add(o.id);
        return ensureCard(o, variant, now).el;
      });
    };

    var liveEls = build(parts.live, 'full');
    var preEls = build(parts.pre, 'full');
    var watchEls = S.filters.showWatch ? build(parts.watch, 'watch') : [];
    var arbEls = build(parts.arbs, 'arb');

    reconcile(el.gridLive, liveEls);
    reconcile(el.gridPre, preEls);
    reconcile(el.gridWatch, watchEls);
    reconcile(el.gridArbs, arbEls);

    S.cards.forEach(function (c, id) {
      if (!keep.has(id)) {
        if (c.el.parentNode) c.el.parentNode.removeChild(c.el);
        S.cards.delete(id);
      }
    });
    if (st) {
      S.actionableIds = new Set(
        st.opportunities
          .filter(function (o) {
            return isOpportunity(o) && isActionable(o);
          })
          .map(function (o) {
            return o.id;
          })
      );
    }

    var activeLive = parts.live.filter(isActionable).length;
    var activePre = parts.pre.filter(isActionable).length;
    var noCards = parts.live.length === 0 && parts.pre.length === 0;
    el.secLive.hidden = noCards;
    el.secPre.hidden = noCards;
    el.emptyLive.hidden = parts.live.length > 0;
    el.emptyPre.hidden = parts.pre.length > 0;
    setText(el.nLive, activeLive ? activeLive + (activeLive === 1 ? ' pick' : ' picks') : '');
    setText(el.nPre, activePre ? activePre + (activePre === 1 ? ' pick' : ' picks') : '');
    renderEmptyOpps(parts, noCards);

    // Watch list
    var watchActive = parts.watch.filter(function (o) {
      return o.status === 'active';
    }).length;
    el.gridWatch.hidden = !S.filters.showWatch;
    el.watchToggle.setAttribute('aria-expanded', String(S.filters.showWatch));
    setText(el.watchToggleText, S.filters.showWatch ? 'Hide' : 'Show');
    setText(el.nWatch, watchActive ? String(watchActive) : '');
    el.secWatch.classList.toggle('is-open', S.filters.showWatch);
    el.secWatch.hidden = !st || (watchActive === 0 && parts.watch.length === 0);

    // Arbs notice on the main panel + arbs panel empty state
    var arbsActive = parts.arbs.filter(isActionable).length;
    el.arbNotice.hidden = arbsActive === 0;
    if (arbsActive > 0) {
      var best = parts.arbs[0];
      setText(
        el.arbNoticeText,
        arbsActive +
          (arbsActive === 1 ? ' arb' : ' arbs') +
          ' open — DraftKings vs another sportsbook' +
          (best && isNum(best.evPct) ? ', best ' + fmtPct(best.evPct) + ' locked' : '')
      );
    }
    renderEmptyArbs(parts);
  }

  function reconcile(container, nodes) {
    var prev = null;
    for (var i = 0; i < nodes.length; i++) {
      var node = nodes[i];
      var expected = prev ? prev.nextElementSibling : container.firstElementChild;
      if (expected !== node) container.insertBefore(node, expected);
      prev = node;
    }
    var extra = prev ? prev.nextElementSibling : container.firstElementChild;
    while (extra) {
      var next = extra.nextElementSibling;
      container.removeChild(extra);
      extra = next;
    }
  }

  function loggedText(id) {
    var l = S.logged.get(id);
    return l ? fmtMoney(l.stake) : '';
  }

  function stakeSub(o, bankroll) {
    if (o.verdict === 'WATCH') return 'not yet';
    if (!(o.stake > 0)) {
      var st = S.state;
      if (st && isNum(st.remainingDailyExposure) && st.remainingDailyExposure <= 0) return 'daily limit hit';
      return 'below $1 min';
    }
    var frac = isNum(o.kellyFraction) && o.kellyFraction > 0 ? o.kellyFraction : isNum(bankroll) && bankroll > 0 ? o.stake / bankroll : null;
    return frac === null ? '' : fmtPct(frac, 1, false) + ' of bankroll';
  }

  function reasonsOf(o) {
    return Array.isArray(o.reasons)
      ? o.reasons
          .filter(function (r) {
            return typeof r === 'string';
          })
          .slice(0, 6)
      : [];
  }

  function confOf(o) {
    return isNum(o.confidence) ? Math.round(clamp(o.confidence, 0, 1) * 100) : null;
  }

  function cardModel(o, variant) {
    var st = S.state;
    var bankroll = st ? st.settings.bankroll : null;
    var vm = {
      v: variant,
      status: o.status,
      verdict: o.verdict,
      urgency: o.urgency,
      stale: !!o.staleLine,
      live: !!o.isLive,
      league: o.league,
      event: o.eventName,
      pick: o.pick,
      start: o.startTime,
      score: o.isLive ? fmtScore(o.score) : '',
      dk: fmtAmerican(o.dkAmerican),
      min: fmtAmerican(o.minAcceptableAmerican),
      fair: fmtAmerican(o.fairAmerican),
      ev: fmtPct(o.evPct),
      stake: fmtMoney(o.stake),
      stakeSub: stakeSub(o, bankroll),
      src: sharpLabel(o.sharpSource),
      // Shapes only: the ages inside reasons change every second and are updated in place (updateCardLive).
      reasons: reasonsOf(o).map(reasonShape),
      url: safeDkUrl(o.dkUrl),
      logged: loggedText(o.id),
    };
    if (variant === 'arb' && o.arb && Array.isArray(o.arb.legs)) {
      vm.legs = o.arb.legs.map(function (leg) {
        return {
          book: bookName(leg.book),
          dk: leg.book === 'draftkings',
          pick: describePick(o.kind, leg.side, leg.line, o.home, o.away),
          price: fmtAmerican(leg.american),
          stake: fmtMoney(leg.stake),
          returns: isNum(leg.stake) && isNum(leg.decimal) ? leg.stake * leg.decimal : null,
        };
      });
      vm.profit = fmtPct(o.arb.profitPct);
      vm.total = fmtMoney(o.arb.totalStake);
      vm.totalRaw = o.arb.totalStake;
    }
    return vm;
  }

  function cardClasses(o, variant, c) {
    var cls = ['card', 'card-' + variant];
    cls.push(o.verdict === 'BET_NOW' ? 'v-betnow' : o.verdict === 'BET' ? 'v-bet' : 'v-watch');
    cls.push('u-' + (URGENCY_RANK[o.urgency] !== undefined ? o.urgency : 'low'));
    if (o.isLive) cls.push('is-live');
    if (o.staleLine) cls.push('is-stale');
    if (o.status === 'gone') cls.push('is-gone');
    else if (o.verdict === 'WATCH' && variant === 'full') cls.push('is-demoted');
    else if (o.urgency === 'critical' && o.verdict !== 'WATCH') cls.push('is-critical');
    if (c.isNew) cls.push('is-new');
    return cls.join(' ');
  }

  function ensureCard(o, variant, now) {
    var c = S.cards.get(o.id);
    if (!c) {
      var node = h('article', 'card');
      node.setAttribute('data-id', o.id);
      c = { el: node, sig: '', refs: {}, opp: o, isNew: false, goneFrom: null, actions: null, actionsKey: '' };
      S.cards.set(o.id, c);
    }
    // Flash whenever a pick becomes actionable (new, or back from WATCH / gone), not only when its id is new.
    if (S.primed && isActionable(o) && !S.actionableIds.has(o.id) && !c.isNew) {
      c.isNew = true;
      window.setTimeout(function () {
        c.isNew = false;
        c.el.classList.remove('is-new');
      }, NEW_FLASH_MS);
    }
    var vm = cardModel(o, variant);
    var sig = JSON.stringify(vm);
    if (sig !== c.sig) {
      var focusKey = focusedAction(c.el);
      if (variant === 'arb') buildArbCard(c, o, vm);
      else if (variant === 'watch') buildWatchCard(c, o, vm);
      else buildFullCard(c, o, vm);
      c.sig = sig;
      restoreFocus(c.el, focusKey);
    }
    c.opp = o;
    c.el.className = cardClasses(o, variant, c);
    updateCardLive(c, o, now);
    return c;
  }

  function focusedAction(node) {
    var a = document.activeElement;
    if (a && a !== document.body && node.contains(a)) return a.getAttribute('data-action');
    return null;
  }

  function restoreFocus(node, key) {
    if (!key) return;
    var target = node.querySelector('[data-action="' + key + '"]');
    if (target) target.focus({ preventScroll: true });
  }

  function verdictBadge(v) {
    return h('span', 'badge badge-' + (v === 'BET_NOW' ? 'betnow' : v === 'BET' ? 'bet' : 'watch'), verdictLabel(v));
  }

  function urgencyChip(u) {
    var key = URGENCY_RANK[u] !== undefined ? u : 'low';
    return h('span', 'chip chip-urg chip-' + key, key.charAt(0).toUpperCase() + key.slice(1));
  }

  function whenNode(o, vm) {
    var wrap = h('span', 'when');
    if (o.isLive) {
      wrap.appendChild(h('span', 'live-tag', 'LIVE'));
      if (vm.score) wrap.appendChild(h('span', 'score', vm.score));
    } else {
      wrap.appendChild(rel(h('span', 'start'), o.startTime, 'start'));
    }
    return wrap;
  }

  function ticketCell(cls, label, value, sub) {
    return h('div', 'tk ' + cls, [h('span', 'tk-label', label), h('span', 'tk-value', value), sub ? h('span', 'tk-sub', sub) : null]);
  }

  function dkLink(vm, small) {
    var a = h('a', 'btn btn-dk' + (small ? ' btn-small' : ''), [icon('external'), h('span', null, small ? 'DraftKings' : 'Open in DraftKings')]);
    a.href = vm.url;
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
    a.setAttribute('data-action', 'open');
    return a;
  }

  /** Confidence meter; its value is set (and kept current) by setConf, never by rebuilding the card. */
  function confMeter(c) {
    var meter = h('span', 'meter');
    var fill = h('span', 'meter-fill');
    meter.appendChild(fill);
    var text = h('span', 'conf-text');
    var wrap = h('span', 'conf', [meter, text]);
    wrap.setAttribute('title', 'Model confidence in the fair price');
    c.refs.confFill = fill;
    c.refs.confText = text;
    c.refs.conf = undefined;
    return wrap;
  }

  function setConf(c, conf) {
    if (!c.refs.confFill || c.refs.conf === conf) return;
    c.refs.conf = conf;
    c.refs.confFill.style.width = (conf === null ? 0 : conf) + '%';
    c.refs.confFill.className = 'meter-fill' + (conf === null ? '' : conf >= 75 ? ' lvl-hi' : conf >= 55 ? ' lvl-mid' : ' lvl-lo');
    setText(c.refs.confText, conf === null ? '—' : conf + '% conf');
  }

  function reasonList(c, o) {
    var items = reasonsOf(o).map(function (r) {
      return h('li', null, r);
    });
    c.refs.reasons = items;
    return items.length ? h('ul', 'reasons', items) : null;
  }

  /** Action buttons survive card rebuilds (same nodes), so a tap that spans a rebuild still clicks and keeps focus. */
  function cardActions(c, key, build) {
    if (!c.actions || c.actionsKey !== key) {
      c.actions = build();
      c.actionsKey = key;
    }
    return c.actions;
  }

  /** Replaces a card's content except its actions node, which stays in place. */
  function mountCard(node, before, actions, after) {
    Array.prototype.slice.call(node.childNodes).forEach(function (ch) {
      if (ch !== actions) node.removeChild(ch);
    });
    if (actions.parentNode !== node) node.appendChild(actions);
    before.forEach(function (n) {
      if (n) node.insertBefore(n, actions);
    });
    after.forEach(function (n) {
      if (n) node.appendChild(n);
    });
  }

  function pickHeading(c, text, cls) {
    var id = 'pick-' + ++S.pickSeq;
    var node = h('h3', cls, text);
    node.id = id;
    c.el.setAttribute('aria-labelledby', id);
    return node;
  }

  function copyButton(label) {
    var b = button('btn btn-ghost btn-copy', [icon('copy'), h('span', 'btn-label', label)], 'copy');
    b.title = label;
    return b;
  }

  function goneBanner() {
    return h('div', 'gone-banner', 'Price moved — gone');
  }

  function statusBanner(o, variant) {
    if (o.status === 'gone') return goneBanner();
    if (variant === 'full' && o.verdict === 'WATCH') return h('div', 'gone-banner demoted-banner', 'Price moved — below your minimum');
    return null;
  }

  function buildFullCard(c, o, vm) {
    var node = c.el;
    c.refs = {};

    var bar = h('div', 'cd-bar');
    var track = h('div', 'cd-track', bar);
    track.setAttribute('aria-hidden', 'true');
    c.refs.bar = bar;

    var top = h('div', 'card-top', [verdictBadge(o.verdict), urgencyChip(o.urgency)]);
    if (o.staleLine) top.appendChild(h('span', 'chip chip-stale', [icon('zap'), 'Stale line']));
    if (vm.logged) top.appendChild(h('span', 'chip chip-logged', [icon('check'), 'Logged ' + vm.logged]));
    var timer = h('span', 'timer');
    c.refs.timer = timer;
    top.appendChild(timer);

    var head = h('div', 'card-head', [
      pickHeading(c, o.pick, 'pick'),
      h('p', 'card-ctx', [h('span', 'league-tag', o.league), whenNode(o, vm), h('span', 'event-name', o.eventName)]),
    ]);

    var minLabel = o.verdict === 'WATCH' ? 'Bet at ≥' : 'Take at ≥';
    var ticket = h('div', 'ticket', [
      ticketCell('tk-price', 'DraftKings', vm.dk),
      ticketCell('tk-min', minLabel, vm.min),
      ticketCell('tk-stake', 'Stake', vm.stake, vm.stakeSub),
      ticketCell('tk-ev', 'EV', vm.ev),
    ]);

    var sharpAge = h('span', 'age');
    var dkAge = h('span', 'age');
    c.refs.sharpAge = sharpAge;
    c.refs.dkAge = dkAge;
    var meta = h('div', 'card-meta', [
      h('span', 'fair', ['Fair ', h('strong', null, vm.fair)]),
      confMeter(c),
      h('span', 'src', [vm.src, ' · sharp ', sharpAge, ' · DK ', dkAge]),
    ]);

    var reasons = reasonList(c, o);

    var actions = cardActions(c, 'full|' + vm.url, function () {
      return h('div', 'card-actions', [
        dkLink(vm, false),
        button('btn btn-secondary', [icon('check-square'), h('span', null, 'I placed it')], 'place'),
        copyButton('Copy pick'),
      ]);
    });

    mountCard(node, [track, top, head, ticket, meta, reasons], actions, [statusBanner(o, 'full')]);
  }

  function buildWatchCard(c, o, vm) {
    var node = c.el;
    c.refs = {};
    var sharpAge = h('span', 'age');
    c.refs.sharpAge = sharpAge;
    var top = h('div', 'card-top', [verdictBadge(o.verdict), h('span', 'league-tag', o.league), whenNode(o, vm)]);
    if (vm.logged) top.appendChild(h('span', 'chip chip-logged', [icon('check'), 'Logged ' + vm.logged]));
    var head = h('div', 'card-head', [pickHeading(c, o.pick, 'pick pick-sm'), h('p', 'card-ctx', h('span', 'event-name', o.eventName))]);
    var line = h('p', 'watch-line', [
      h('span', null, ['DK ', h('strong', null, vm.dk)]),
      h('span', null, ['Bet at ≥ ', h('strong', 'accent', vm.min)]),
      h('span', null, ['Fair ', h('strong', null, vm.fair)]),
      h('span', null, ['EV ', h('strong', null, vm.ev)]),
    ]);
    var src = h('p', 'card-meta', h('span', 'src', [vm.src, ' · sharp ', sharpAge]));
    var actions = cardActions(c, 'watch|' + vm.url, function () {
      var copy = copyButton('Copy pick');
      copy.className = 'btn btn-ghost btn-small btn-copy btn-icon';
      return h('div', 'card-actions card-actions-sm', [
        dkLink(vm, true),
        button('btn btn-ghost btn-small', [icon('check-square'), h('span', null, 'I placed it')], 'place'),
        copy,
      ]);
    });
    mountCard(node, [top, head, line, src], actions, [statusBanner(o, 'watch')]);
  }

  function buildArbCard(c, o, vm) {
    var node = c.el;
    c.refs = {};
    var bar = h('div', 'cd-bar');
    var track = h('div', 'cd-track', bar);
    track.setAttribute('aria-hidden', 'true');
    c.refs.bar = bar;
    var timer = h('span', 'timer');
    c.refs.timer = timer;

    var top = h('div', 'card-top', [verdictBadge(o.verdict), h('span', 'chip chip-arb', [icon('split'), 'Arb']), timer]);
    if (vm.logged) top.insertBefore(h('span', 'chip chip-logged', [icon('check'), 'Logged ' + vm.logged]), timer);
    var head = h('div', 'card-head', [
      pickHeading(c, (vm.profit || '') + ' locked', 'pick'),
      h('p', 'card-ctx', [h('span', 'league-tag', o.league), whenNode(o, vm), h('span', 'event-name', o.eventName)]),
    ]);

    var legs = h('div', 'legs');
    legs.setAttribute('role', 'table');
    legs.setAttribute('aria-label', 'Arb legs');
    var headRow = h('div', 'leg leg-head', [h('span', null, 'Book'), h('span', null, 'Bet'), h('span', 'num', 'Price'), h('span', 'num', 'Stake')]);
    headRow.setAttribute('role', 'row');
    legs.appendChild(headRow);
    var returns = null;
    (vm.legs || []).forEach(function (leg) {
      var row = h('div', 'leg' + (leg.dk ? ' leg-dk' : ''), [
        h('span', 'leg-book', leg.book),
        h('span', 'leg-pick', leg.pick),
        h('span', 'leg-price num', leg.price),
        h('span', 'leg-stake num', leg.stake),
      ]);
      row.setAttribute('role', 'row');
      Array.prototype.forEach.call(row.children, function (cell) {
        cell.setAttribute('role', 'cell');
      });
      legs.appendChild(row);
      if (leg.returns !== null) returns = returns === null ? leg.returns : Math.min(returns, leg.returns);
    });
    Array.prototype.forEach.call(headRow.children, function (cell) {
      cell.setAttribute('role', 'columnheader');
    });

    var summary = h('p', 'arb-total', [
      'Total ',
      h('strong', null, vm.total || '—'),
      returns !== null && isNum(vm.totalRaw)
        ? [' → returns ≈ ', h('strong', null, fmtMoney(returns)), ' either way (', h('strong', returns - vm.totalRaw >= 0 ? 'pos' : 'neg', fmtMoney(returns - vm.totalRaw, { signed: true })), ')']
        : null,
    ]);

    var reasons = reasonList(c, o);
    var actions = cardActions(c, 'arb|' + vm.url, function () {
      return h('div', 'card-actions', [
        dkLink(vm, false),
        button('btn btn-secondary', [icon('check-square'), h('span', null, 'I placed the DK leg')], 'place'),
        copyButton('Copy legs'),
      ]);
    });
    mountCard(node, [track, top, head, legs, summary, reasons], actions, [statusBanner(o, 'arb')]);
  }

  /** Per-state updates that must not rebuild the card: ages, reason texts, confidence, countdown and the gone fade. */
  function updateCardLive(c, o, now) {
    var st = S.state;
    var gen = st ? st.generatedAt : now;
    if (c.refs.sharpAge) rel(c.refs.sharpAge, isNum(o.sharpAgeSec) ? gen - o.sharpAgeSec * 1000 : null, 'age');
    if (c.refs.dkAge) rel(c.refs.dkAge, isNum(o.dkAgeSec) ? gen - o.dkAgeSec * 1000 : null, 'age');
    if (c.refs.reasons) {
      var texts = reasonsOf(o);
      c.refs.reasons.forEach(function (li, i) {
        if (i < texts.length) setText(li, texts[i]);
      });
    }
    setConf(c, confOf(o));
    if (o.status === 'gone') {
      if (c.goneFrom === null) {
        c.goneFrom = isNum(o.lastSeen) ? o.lastSeen : now;
        var elapsed = Math.max(0, (now - c.goneFrom) / 1000);
        c.el.style.animationDelay = '-' + Math.min(elapsed, GONE_FADE_SEC).toFixed(1) + 's';
      }
    } else if (c.goneFrom !== null) {
      c.goneFrom = null;
      c.el.style.animationDelay = '';
    }
    updateCountdown(c, now);
  }

  function updateCountdown(c, now) {
    var bar = c.refs.bar;
    var timer = c.refs.timer;
    if (!bar || !timer) return;
    var o = c.opp;
    if (o.status !== 'active' || o.verdict === 'WATCH') {
      bar.style.transform = 'scaleX(0)';
      setText(timer, '');
      return;
    }
    var info = countdownInfo(o, now);
    bar.style.transform = 'scaleX(' + info.frac.toFixed(4) + ')';
    var lvl = info.frac > 0.5 ? 'lvl-ok' : info.frac > 0.2 ? 'lvl-warn' : 'lvl-hot';
    if (bar.getAttribute('data-lvl') !== lvl) {
      bar.setAttribute('data-lvl', lvl);
      bar.className = 'cd-bar ' + lvl;
    }
    setText(timer, countdownLabel(info));
    timer.classList.toggle('is-over', info.remaining <= 0);
  }

  function renderEmptyOpps(parts, noCards) {
    var box = el.emptyOpps;
    box.hidden = !noCards;
    if (!noCards) return;
    var st = S.state;
    var title;
    var body = [];
    var actions = [];
    var kind = 'idle';
    if (!st) {
      kind = 'loading';
      title = 'Connecting to the odds engine…';
      body.push('Live prices appear here as soon as the first update arrives.');
    } else {
      var hl = st.health;
      var feed = primarySource(hl);
      if (!hl.demoMode && feed && feed.status === 'disabled') {
        kind = 'setup';
        title = 'No odds feed configured';
        body.push(['Add ', h('code', null, 'ODDS_API_KEY'), ' to your ', h('code', null, '.env'), ' (get a key at the-odds-api.com), then apply it:']);
        body.push(['Docker: ', h('code', null, 'docker compose up -d'), ' (', h('code', null, 'docker compose restart'), ' does not re-read ', h('code', null, '.env'), '). Otherwise restart ', h('code', null, 'npm start'), '.']);
        body.push(['To try simulated odds first: set ', h('code', null, 'DEMO_MODE=true'), ' (Docker) or run ', h('code', null, 'npm run demo'), '.']);
      } else if (!hl.demoMode && feed && feed.status === 'down') {
        kind = 'error';
        title = 'The odds feed is down';
        body.push(feed.lastError ? 'Last error: ' + feed.lastError : 'The server cannot reach The Odds API right now.');
        body.push('It retries automatically; picks return as soon as fresh prices arrive.');
      } else if (parts.hidden > 0) {
        kind = 'filtered';
        title = parts.hidden + (parts.hidden === 1 ? ' pick is' : ' picks are') + ' hidden by your filters';
        body.push('Loosen the league, live-only or min EV filters to see them.');
        actions.push(button('btn btn-secondary', 'Reset filters', 'reset-filters'));
      } else if (!isNum(hl.eventsTracked) || hl.eventsTracked === 0) {
        title = 'Waiting for games';
        body.push('No events are being tracked yet for your enabled leagues. Games within the next day appear automatically.');
      } else {
        title = 'No edges right now';
        body.push(
          'Watching ' +
            fmtCount(hl.eventsTracked) +
            ' events (' +
            fmtCount(hl.liveEvents) +
            ' live). DraftKings is in line with the sharp market — that is normal most of the time.'
        );
        var watchN = parts.watch.filter(function (o) {
          return o.status === 'active';
        }).length;
        if (watchN > 0) {
          body.push(watchN + (watchN === 1 ? ' close call is' : ' close calls are') + ' on the watch list below.');
        }
        if (!S.alertsOn) {
          body.push('Turn on alerts to get a sound and a notification the moment a bet appears.');
          actions.push(button('btn btn-secondary', [icon('bell'), h('span', null, 'Enable alerts')], 'enable-alerts'));
        }
      }
    }
    var sig = JSON.stringify([kind, title, body.length, actions.length, st ? [st.health.eventsTracked, st.health.liveEvents, parts.hidden] : null]);
    if (box.getAttribute('data-sig') === sig) return;
    box.setAttribute('data-sig', sig);
    box.replaceChildren();
    box.setAttribute('data-kind', kind);
    box.appendChild(h('div', 'empty-icon', icon(kind === 'setup' || kind === 'error' ? 'alert' : kind === 'filtered' ? 'sliders' : 'activity')));
    box.appendChild(h('h2', 'empty-title', title));
    body.forEach(function (p) {
      box.appendChild(h('p', 'empty-text', p));
    });
    if (actions.length) box.appendChild(h('div', 'empty-actions', actions));
  }

  function renderEmptyArbs(parts) {
    var box = el.emptyArbs;
    var st = S.state;
    var empty = parts.arbs.length === 0;
    box.hidden = !empty;
    if (!empty) return;
    var title;
    var text;
    var action = null;
    if (!st) {
      title = 'Connecting…';
      text = 'Arbs appear here when the first update arrives.';
    } else if (!st.settings.showArbs) {
      title = 'Arbs are turned off';
      text = 'Turn on “Show arbs” in Settings to see DraftKings prices that lock a profit against another sportsbook.';
      action = button('btn btn-secondary', [icon('sliders'), h('span', null, 'Open settings')], 'open-settings');
    } else {
      title = 'No arbs right now';
      text = 'Arbs are rare and usually last seconds. When one appears it shows here with both legs and exact stakes.';
    }
    var sig = title + '|' + text;
    if (box.getAttribute('data-sig') === sig) return;
    box.setAttribute('data-sig', sig);
    box.replaceChildren(h('div', 'empty-icon', icon('split')), h('h2', 'empty-title', title), h('p', 'empty-text', text), action ? h('div', 'empty-actions', action) : null);
  }

  function renderCounts() {
    var parts = S.parts;
    var n = parts ? parts.live.filter(isActionable).length + parts.pre.filter(isActionable).length : 0;
    var arbs = parts ? parts.arbs.filter(isActionable).length : 0;
    setText(el.countOpps, String(n));
    el.countOpps.classList.toggle('is-hot', !!parts && parts.live.some(function (o) {
      return isActionable(o) && o.urgency === 'critical';
    }));
    setText(el.countArbs, String(arbs));
    el.countArbs.hidden = arbs === 0;
    var pending = S.summary && isNum(S.summary.pending) ? S.summary.pending : 0;
    setText(el.countBets, String(pending));
    el.countBets.hidden = pending === 0;
    var total = parts ? actionableCount(parts) : 0;
    var title = (total > 0 ? '(' + total + ') ' : '') + 'Odds Hub';
    if (document.title !== title) document.title = title;
  }

  // ------------------------------------------------------------------------------------------- card actions

  function copyText(text) {
    var fallback = function () {
      try {
        var ta = document.createElement('textarea');
        ta.value = text;
        ta.setAttribute('readonly', '');
        ta.className = 'clipboard-helper';
        document.body.appendChild(ta);
        ta.select();
        var ok = document.execCommand('copy');
        document.body.removeChild(ta);
        return Promise.resolve(ok);
      } catch (e) {
        return Promise.resolve(false);
      }
    };
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text).then(
        function () {
          return true;
        },
        fallback
      );
    }
    return fallback();
  }

  function copyTextFor(o) {
    if (o.type === 'arb' && o.arb && Array.isArray(o.arb.legs)) {
      var legs = o.arb.legs.map(function (leg) {
        return bookName(leg.book) + ': ' + describePick(o.kind, leg.side, leg.line, o.home, o.away) + ' ' + fmtAmericanAscii(leg.american) + ' — stake ' + fmtMoney(leg.stake);
      });
      return 'Arb ' + fmtPct(o.arb.profitPct).replace(MINUS, '-') + ' · ' + o.eventName + '\n' + legs.join('\n');
    }
    return (
      o.pick + ' ' + fmtAmericanAscii(o.dkAmerican) + ' (take ' + fmtAmericanAscii(o.minAcceptableAmerican) + ' or better) · ' + o.eventName
    );
  }

  function onCardAction(e) {
    var target = e.target.closest('[data-action]');
    if (!target) return;
    var action = target.getAttribute('data-action');
    if (action === 'open') return; // real link, let the browser open it
    if (action === 'reset-filters') return resetFilters();
    if (action === 'enable-alerts') return toggleAlerts();
    if (action === 'open-settings') return openSettings();
    if (action === 'league-all') {
      S.filters.leagues = [];
      return onFiltersChanged();
    }
    if (action === 'league') {
      var key = target.getAttribute('data-league');
      var idx = S.filters.leagues.indexOf(key);
      if (idx >= 0) S.filters.leagues.splice(idx, 1);
      else S.filters.leagues.push(key);
      return onFiltersChanged();
    }
    var card = target.closest('.card');
    if (!card) return;
    var id = card.getAttribute('data-id');
    var c = S.cards.get(id);
    var o = findOpp(id) || (c ? c.opp : null);
    if (!o) return;
    if (action === 'copy') {
      copyText(copyTextFor(o)).then(function (ok) {
        toast(ok ? 'Copied: ' + o.pick : 'Could not copy — select the text manually', ok ? 'ok' : 'error', 2500);
      });
    } else if (action === 'place') {
      openPlace(o);
    }
  }

  // ------------------------------------------------------------------------------------------- place bet modal

  function openPlace(o) {
    S.place = { id: o.id, snap: o };
    setText(el.placeKicker, o.league + ' · ' + o.eventName + (o.isLive ? ' · LIVE' : ''));
    setText(el.placePick, o.pick);
    el.placeOdds.value = fmtAmericanAscii(o.dkAmerican);
    el.placeStake.value = o.stake > 0 ? String(o.stake) : '';
    el.placeNotes.value = '';
    el.placeError.hidden = true;
    el.placeSubmit.disabled = false;
    if (o.type === 'arb' && o.arb && Array.isArray(o.arb.legs)) {
      var others = o.arb.legs
        .filter(function (l) {
          return l.book !== 'draftkings';
        })
        .map(function (l) {
          return describePick(o.kind, l.side, l.line, o.home, o.away) + ' ' + fmtAmerican(l.american) + ' at ' + bookName(l.book) + ' (' + fmtMoney(l.stake) + ')';
        });
      setText(el.placeNote, 'This logs the DraftKings leg only. Place the other leg yourself: ' + others.join('; ') + '.');
      el.placeNote.hidden = false;
    } else {
      el.placeNote.hidden = true;
    }
    updatePlaceLive();
    updatePlaceCalc();
    el.dlgPlace.showModal();
    el.placeOdds.focus();
    el.placeOdds.select();
  }

  function currentPlaceOpp() {
    if (!S.place) return null;
    return findOpp(S.place.id);
  }

  function updatePlaceLive() {
    if (!S.place) return;
    var status = placeLiveStatus(currentPlaceOpp(), S.place.snap);
    el.placeLive.setAttribute('data-kind', status.kind);
    setText(el.placeLive, status.text);
    updatePlaceCalc();
  }

  function updatePlaceCalc() {
    if (!S.place) return;
    var o = currentPlaceOpp() || S.place.snap;
    var am = parseAmericanInput(el.placeOdds.value);
    var stake = Number(el.placeStake.value);
    var node = el.placeCalc;
    if (am === null) {
      node.setAttribute('data-kind', 'muted');
      setText(node, 'Enter the odds you got, e.g. +110 or -120.');
      return;
    }
    var ev = evAtAmerican(o.fairProb, am);
    var dec = americanToDecimal(am);
    var st = S.state;
    var minEv = st ? (o.isLive ? st.settings.minEvLive : st.settings.minEvPrematch) : 0;
    var parts = ['EV at ' + fmtAmerican(am) + ': ' + fmtPct(ev)];
    if (isNum(stake) && stake > 0 && dec !== null) parts.push('to win ' + fmtMoney(stake * (dec - 1), { cents: true }));
    var kind = ev === null ? 'muted' : ev >= minEv - 1e-9 ? 'ok' : ev > 0 ? 'warn' : 'bad';
    var minDec = americanToDecimal(o.minAcceptableAmerican);
    if (o.type !== 'arb' && dec !== null && minDec !== null && dec < minDec - 1e-9) {
      parts.push('below your minimum ' + fmtAmerican(o.minAcceptableAmerican));
    }
    node.setAttribute('data-kind', kind);
    setText(node, parts.join(' · '));
  }

  function submitPlace(e) {
    e.preventDefault();
    if (!S.place) return;
    var am = parseAmericanInput(el.placeOdds.value);
    var stake = Number(el.placeStake.value);
    var showErr = function (msg) {
      setText(el.placeError, msg);
      el.placeError.hidden = false;
    };
    if (am === null) {
      showErr('Enter the American odds you got, like +110 or -120.');
      el.placeOdds.focus();
      return;
    }
    if (!isNum(stake) || stake <= 0) {
      showErr('Enter the stake you placed (more than $0).');
      el.placeStake.focus();
      return;
    }
    el.placeError.hidden = true;
    el.placeSubmit.disabled = true;
    var id = S.place.id;
    var pick = S.place.snap.pick;
    var notes = el.placeNotes.value.trim();
    var body = {
      opportunityId: id,
      stake: Math.round(stake * 100) / 100,
      americanTaken: am,
      // Used by the server only if the pick has expired there, so a bet already placed can still be logged.
      snapshot: pickSnapshot(currentPlaceOpp() || S.place.snap),
    };
    if (notes) body.notes = notes;
    api('POST', 'api/bets', body)
      .then(function (rec) {
        var prev = S.logged.get(id);
        S.logged.set(id, { stake: (prev ? prev.stake : 0) + body.stake });
        el.dlgPlace.close();
        toast('Logged ' + pick + ' ' + fmtAmerican(am) + ' · ' + fmtMoney(body.stake));
        if (rec && typeof rec === 'object' && Array.isArray(S.bets)) S.bets.unshift(rec);
        loadBets(true);
        renderOpportunities();
      })
      .catch(function (err) {
        showErr(err.status === 404 ? 'This pick expired on the server and could not be logged: ' + err.message : err.message);
      })
      .then(function () {
        el.placeSubmit.disabled = false;
      });
  }

  // ------------------------------------------------------------------------------------------- settings modal

  function fillSettings(s) {
    S.settingsBase = s;
    el.sBankroll.value = String(s.bankroll);
    el.sKelly.value = String(s.kellyMultiplier);
    el.sMaxPct.value = String(fractionToPct(s.maxStakePct));
    el.sMaxAbs.value = String(s.maxStakeAbs);
    el.sDaily.value = String(fractionToPct(s.maxDailyExposurePct));
    el.sMinPre.value = String(fractionToPct(s.minEvPrematch));
    el.sMinLive.value = String(fractionToPct(s.minEvLive));
    el.sWatch.value = String(fractionToPct(s.watchEv));
    el.sArbs.checked = s.showArbs === true;
    var enabled = new Set(Array.isArray(s.enabledLeagues) ? s.enabledLeagues : []);
    el.sLeagues.replaceChildren();
    knownLeagues().forEach(function (entry) {
      var input = h('input');
      input.type = 'checkbox';
      input.value = entry[0];
      input.checked = enabled.has(entry[0]);
      var label = h('label', 'check', [input, h('span', 'check-box', icon('check')), h('span', 'check-text', [h('strong', null, entry[0]), entry[1] !== entry[0] ? h('span', 'check-sub', entry[1]) : null])]);
      el.sLeagues.appendChild(label);
    });
    el.settingsError.hidden = true;
    el.settingsSubmit.disabled = false;
  }

  function openSettings() {
    if (S.state) {
      fillSettings(S.state.settings);
      el.dlgSettings.showModal();
      return;
    }
    api('GET', 'api/settings')
      .then(function (s) {
        fillSettings(s);
        el.dlgSettings.showModal();
      })
      .catch(function (err) {
        toast('Could not load settings: ' + err.message, 'error');
      });
  }

  function submitSettings(e) {
    e.preventDefault();
    var leagues = Array.prototype.filter
      .call(el.sLeagues.querySelectorAll('input[type=checkbox]'), function (i) {
        return i.checked;
      })
      .map(function (i) {
        return i.value;
      });
    var result = buildSettingsPatch(
      {
        bankroll: el.sBankroll.value,
        kelly: el.sKelly.value,
        maxStakePct: el.sMaxPct.value,
        maxStakeAbs: el.sMaxAbs.value,
        dailyPct: el.sDaily.value,
        minEvPrematch: el.sMinPre.value,
        minEvLive: el.sMinLive.value,
        watchEv: el.sWatch.value,
        leagues: leagues,
        showArbs: el.sArbs.checked,
      },
      S.settingsBase
    );
    if (result.error) {
      setText(el.settingsError, result.error);
      el.settingsError.hidden = false;
      return;
    }
    if (Object.keys(result.patch).length === 0) {
      el.dlgSettings.close();
      toast('No changes', 'info', 2000);
      return;
    }
    el.settingsSubmit.disabled = true;
    el.settingsError.hidden = true;
    api('PUT', 'api/settings', result.patch)
      .then(function (updated) {
        if (S.state && updated && typeof updated === 'object') S.state.settings = updated;
        el.dlgSettings.close();
        toast('Settings saved — applied on the next update');
        renderTopbar();
        renderOpportunities();
      })
      .catch(function (err) {
        setText(el.settingsError, err.message);
        el.settingsError.hidden = false;
      })
      .then(function () {
        el.settingsSubmit.disabled = false;
      });
  }

  function setAllLeagues(on) {
    Array.prototype.forEach.call(el.sLeagues.querySelectorAll('input[type=checkbox]'), function (i) {
      i.checked = on;
    });
  }

  // ------------------------------------------------------------------------------------------- health drawer

  function kv(label, value, cls) {
    return h('div', 'kv' + (cls ? ' ' + cls : ''), [h('dt', null, label), h('dd', null, value)]);
  }

  function renderHealth() {
    var body = el.healthBody;
    var st = S.state;
    body.replaceChildren();
    if (!st) {
      body.appendChild(h('p', 'empty-text', 'Waiting for the first update from the server…'));
      setText(el.healthSub, '');
      return;
    }
    var hl = st.health;
    setText(el.healthSub, hl.demoMode ? 'Demo mode — simulated odds' : 'Live data from The Odds API');

    var sources = h('section', 'h-section', h('h3', 'h-title', 'Data sources'));
    (Array.isArray(hl.sources) ? hl.sources : []).forEach(function (s) {
      var row = h('div', 'source');
      row.appendChild(
        h('div', 'source-head', [
          h('span', 'status-dot', null),
          h('strong', 'source-name', String(s.name)),
          h('span', 'status-word status-' + s.status, String(s.status)),
        ])
      );
      row.querySelector('.status-dot').setAttribute('data-status', String(s.status));
      if (s.detail) row.appendChild(h('p', 'source-detail', String(s.detail)));
      var meta = h('p', 'source-meta');
      meta.appendChild(document.createTextNode('Last success: '));
      meta.appendChild(isNum(s.lastSuccess) ? rel(h('span'), s.lastSuccess, 'ago') : h('span', null, 'never'));
      if (isNum(s.consecutiveFailures) && s.consecutiveFailures > 0) {
        meta.appendChild(document.createTextNode(' · ' + s.consecutiveFailures + ' failed in a row'));
      }
      row.appendChild(meta);
      if (s.lastError) row.appendChild(h('p', 'source-error', String(s.lastError)));
      sources.appendChild(row);
    });
    if (!hl.sources || hl.sources.length === 0) sources.appendChild(h('p', 'empty-text', 'No sources reported.'));
    body.appendChild(sources);

    var credits = h('section', 'h-section', h('h3', 'h-title', 'Odds API credits'));
    var rem = hl.oddsApiCreditsRemaining;
    var used = hl.oddsApiCreditsUsed;
    if (isNum(rem) || isNum(used)) {
      var dl = h('dl', 'kv-grid', [kv('Remaining', fmtCount(rem)), kv('Used this period', fmtCount(used))]);
      credits.appendChild(dl);
      if (isNum(rem) && isNum(used) && rem + used > 0) {
        var fill = h('span', 'meter-fill');
        var pctUsed = clamp((used / (rem + used)) * 100, 0, 100);
        fill.style.width = pctUsed.toFixed(1) + '%';
        fill.classList.add(pctUsed > 90 ? 'lvl-lo' : pctUsed > 70 ? 'lvl-mid' : 'lvl-hi');
        credits.appendChild(h('span', 'meter meter-wide', fill));
        credits.appendChild(h('p', 'source-meta', pctUsed.toFixed(0) + '% of this period’s credits used'));
      }
    } else {
      credits.appendChild(h('p', 'source-meta', hl.demoMode ? 'Not used in demo mode.' : 'No usage reported yet.'));
    }
    body.appendChild(credits);

    var tracking = h('section', 'h-section', [
      h('h3', 'h-title', 'Tracking'),
      h('dl', 'kv-grid', [kv('Events', fmtCount(hl.eventsTracked)), kv('Live events', fmtCount(hl.liveEvents)), kv('Quotes', fmtCount(hl.quotesTracked)), kv('Opportunities', fmtCount(st.opportunities.length))]),
    ]);
    body.appendChild(tracking);

    var engine = h('section', 'h-section', h('h3', 'h-title', 'Engine & server'));
    var lastRun = isNum(hl.lastEngineRunMs) ? rel(h('span'), hl.lastEngineRunMs, 'ago') : 'never';
    var uptime = isNum(hl.startedAt) ? fmtAge(Math.max(0, hl.now - hl.startedAt), true) : '—';
    engine.appendChild(
      h('dl', 'kv-grid', [
        kv('Last engine run', lastRun),
        kv('Run time', isNum(hl.engineRunDurationMs) ? Math.round(hl.engineRunDurationMs) + ' ms' : '—'),
        kv('Memory', isNum(hl.memoryMb) ? Math.round(hl.memoryMb) + ' MB' : '—'),
        kv('Uptime', uptime),
        kv('Dashboard link', S.conn === 'live' ? 'Live stream' : S.pollTimer ? 'Polling every 10s' : 'Connecting'),
        kv('Clock offset', Math.abs(S.offset) < 1000 ? 'in sync' : fmtAge(Math.abs(S.offset)) + (S.offset > 0 ? ' behind server' : ' ahead of server')),
      ])
    );
    body.appendChild(engine);
  }

  function openHealth() {
    renderHealth();
    el.dlgHealth.showModal();
  }

  // ------------------------------------------------------------------------------------------- bets

  function rebuildLogged() {
    if (!Array.isArray(S.bets)) return;
    var map = new Map();
    S.bets.forEach(function (b) {
      if (!b || typeof b.opportunityId !== 'string' || b.result === 'void') return;
      var prev = map.get(b.opportunityId);
      map.set(b.opportunityId, { stake: (prev ? prev.stake : 0) + (isNum(b.stake) ? b.stake : 0) });
    });
    S.logged = map;
  }

  function loadBets(silent) {
    if (S.betsLoading) return;
    S.betsLoading = true;
    api('GET', 'api/bets?limit=500')
      .then(function (data) {
        if (data && Array.isArray(data.bets)) {
          S.bets = data.bets;
          if (data.summary && typeof data.summary === 'object') S.summary = data.summary;
          rebuildLogged();
        }
      })
      .catch(function (err) {
        if (!silent) toast('Could not load bets: ' + err.message, 'error');
      })
      .then(function () {
        S.betsLoading = false;
        renderBets();
        renderBetSummary();
        renderRecent();
        renderCounts();
        if (S.state) renderOpportunities();
      });
  }

  /**
   * "Placed a bet on a pick that has left the board?": recent actionable picks that are no longer active, so a bet
   * placed on one can still be logged (the server accepts the dashboard's copy of an expired pick).
   */
  function renderRecent() {
    if (!el.recent) return;
    var st = S.state;
    var onBoard = new Set();
    if (st) {
      st.opportunities.forEach(function (o) {
        if (isOpportunity(o) && o.status === 'active') onBoard.add(o.id);
      });
    }
    var items = Array.from(S.recent.values())
      .filter(function (e) {
        return !onBoard.has(e.o.id);
      })
      .reverse();
    var sig = JSON.stringify(
      items.map(function (e) {
        return [e.o.id, e.seenAt, loggedText(e.o.id)];
      })
    );
    if (sig === S.recentSig) return;
    S.recentSig = sig;
    el.recent.hidden = items.length === 0;
    setText(el.recentCount, items.length ? String(items.length) : '');
    el.recentList.replaceChildren();
    items.forEach(function (e) {
      var o = e.o;
      var logged = loggedText(o.id);
      var log = button('btn btn-small btn-secondary', [icon('check-square'), h('span', null, logged ? 'Log again' : 'Log it')], 'recent-log');
      log.setAttribute('data-id', o.id);
      log.setAttribute('aria-label', 'Log a bet on ' + o.pick + ', ' + o.eventName);
      el.recentList.appendChild(
        h('li', 'recent-item', [
          h('div', 'recent-main', [
            h('strong', 'recent-pick', o.pick),
            h('span', 'recent-ctx', [h('span', 'league-tag', o.league), o.isLive ? h('span', 'live-tag', 'LIVE') : null, h('span', null, o.eventName)]),
          ]),
          h('span', 'recent-meta', [
            (o.type === 'arb' ? 'Arb · DK ' : 'DK ') + fmtAmerican(o.dkAmerican),
            ' · last seen ',
            rel(h('span'), e.seenAt, 'ago'),
            logged ? ' · logged ' + logged : '',
          ]),
          log,
        ])
      );
    });
  }

  function onRecentAction(e) {
    var target = e.target.closest('[data-action="recent-log"]');
    if (!target) return;
    var entry = S.recent.get(target.getAttribute('data-id'));
    if (entry) openPlace(findOpp(entry.o.id) || entry.o);
  }

  function renderBetSummary() {
    var s = S.summary;
    var box = el.betSummary;
    var tiles = [
      ['Bets', s ? fmtCount(s.totalBets) : '—', ''],
      ['Pending', s ? fmtCount(s.pending) : '—', ''],
      ['Staked', s ? fmtMoney(s.staked) : '—', ''],
      ['Profit', s ? fmtMoney(s.profit, { signed: true }) : '—', s && s.profit > 0 ? 'pos' : s && s.profit < 0 ? 'neg' : ''],
      ['ROI', s && isNum(s.roiPct) ? fmtPct(s.roiPct) : '—', s && s.roiPct > 0 ? 'pos' : s && s.roiPct < 0 ? 'neg' : ''],
      ['Avg EV', s && isNum(s.avgEvPct) ? fmtPct(s.avgEvPct) : '—', ''],
      ['Avg CLV', s && isNum(s.avgClvPct) ? fmtPct(s.avgClvPct) : '—', s && s.avgClvPct > 0 ? 'pos' : s && s.avgClvPct < 0 ? 'neg' : '', clvCoverage(s)],
      ['Staked today', s ? fmtMoney(s.stakedToday) : '—', ''],
    ];
    var sig = JSON.stringify(tiles);
    if (box.getAttribute('data-sig') === sig) return;
    box.setAttribute('data-sig', sig);
    box.replaceChildren();
    tiles.forEach(function (t) {
      box.appendChild(h('div', 'tile', [h('dt', null, t[0]), h('dd', t[2] || null, t[1]), t[3] ? h('p', 'tile-sub', t[3]) : null]));
    });
  }

  /** "on 12 bets · 3 without a closing line": avg CLV only covers bets whose closing line was captured. */
  function clvCoverage(s) {
    if (!s || !isNum(s.clvBets)) return '';
    var parts = [];
    if (s.clvBets > 0) parts.push('on ' + fmtCount(s.clvBets) + (s.clvBets === 1 ? ' bet' : ' bets'));
    if (isNum(s.clvMissing) && s.clvMissing > 0) parts.push(fmtCount(s.clvMissing) + ' without a closing line');
    return parts.join(' · ');
  }

  function resultPill(b) {
    var r = b.result;
    var label = r === 'pending' ? 'Pending' : r.charAt(0).toUpperCase() + r.slice(1);
    var txt = r === 'won' || r === 'lost' || r === 'push' ? label + ' ' + fmtMoney(b.profit, { signed: true }) : label;
    return h('span', 'result result-' + r, txt);
  }

  function settleButtons(b) {
    return h(
      'div',
      'settle',
      [
        ['won', 'Won'],
        ['lost', 'Lost'],
        ['push', 'Push'],
        ['void', 'Void'],
      ].map(function (r) {
        var btn = button('btn btn-small settle-' + r[0], r[1], 'settle');
        btn.setAttribute('data-result', r[0]);
        btn.setAttribute('data-bet', b.id);
        btn.setAttribute('aria-label', 'Mark ' + b.pick + ' as ' + r[1].toLowerCase());
        return btn;
      })
    );
  }

  function td(label, content, cls) {
    var cell = h('td', cls || null, content);
    cell.setAttribute('data-label', label);
    return cell;
  }

  function renderBets() {
    var box = el.betList;
    box.replaceChildren();
    if (S.bets === null) {
      box.appendChild(h('p', 'empty-text', S.betsLoading ? 'Loading your bets…' : 'Bets load when you open this tab.'));
      return;
    }
    var list = S.bets.filter(function (b) {
      if (!b || typeof b.id !== 'string') return false;
      if (S.betFilter === 'pending') return b.result === 'pending';
      if (S.betFilter === 'settled') return b.result !== 'pending';
      return true;
    });
    if (list.length === 0) {
      var empty = h('div', 'empty-state');
      empty.appendChild(h('div', 'empty-icon', icon('check-square')));
      if (S.bets.length === 0) {
        empty.appendChild(h('h2', 'empty-title', 'No bets logged yet'));
        empty.appendChild(h('p', 'empty-text', 'After you place a bet in DraftKings, tap “I placed it” on the card. Your results, ROI and closing line value (CLV) are tracked here.'));
      } else {
        empty.appendChild(h('h2', 'empty-title', S.betFilter === 'pending' ? 'Nothing pending' : 'No settled bets yet'));
      }
      box.appendChild(empty);
      return;
    }
    var now = serverNow();
    var table = h('table', 'bets-table');
    var head = h(
      'tr',
      null,
      ['Placed', 'Pick', 'Odds', 'Stake', 'EV', 'CLV', 'Result', ''].map(function (t, i) {
        var th = h('th', i >= 2 && i <= 5 ? 'num' : null, t || h('span', 'sr-only', 'Actions'));
        th.scope = 'col';
        return th;
      })
    );
    table.appendChild(h('thead', null, head));
    var tbody = h('tbody');
    list.forEach(function (b) {
      var clv = isNum(b.closingFairProb) && isNum(b.decimalTaken) ? b.closingFairProb * b.decimalTaken - 1 : null;
      var pickCell = [
        h('strong', 'bet-pick', b.pick),
        h('span', 'bet-event', [h('span', 'league-tag', b.league), b.wasLive ? h('span', 'live-tag', 'LIVE') : null, h('span', null, b.eventName)]),
      ];
      if (b.notes) pickCell.push(h('span', 'bet-notes', b.notes));
      var editing = S.editBets.has(b.id);
      var actions;
      if (b.result === 'pending' || editing) {
        actions = settleButtons(b);
        if (editing) {
          var cancel = button('btn btn-small btn-ghost', 'Cancel', 'settle-cancel');
          cancel.setAttribute('data-bet', b.id);
          actions.appendChild(cancel);
        }
      } else {
        var change = button('btn btn-small btn-ghost', 'Change', 'settle-edit');
        change.setAttribute('data-bet', b.id);
        change.setAttribute('aria-label', 'Change result of ' + b.pick);
        actions = change;
      }
      var row = h('tr', 'bet-row bet-' + b.result, [
        td('Placed', h('span', 'bet-time', fmtClock(b.placedAt, now)), 'c-placed'),
        td('Pick', pickCell, 'bet-pick-cell'),
        td('Odds', fmtAmerican(b.americanTaken), 'num c-odds'),
        td('Stake', fmtMoney(b.stake), 'num c-stake'),
        td('EV', fmtPct(b.evPctAtPlace), 'num c-ev'),
        clvCell(b, clv),
        td('Result', resultPill(b), 'c-result'),
        td('', actions, 'bet-actions'),
      ]);
      tbody.appendChild(row);
    });
    table.appendChild(tbody);
    box.appendChild(h('div', 'table-wrap', table));
  }

  function clvCell(b, clv) {
    var cell = td('CLV', clv === null ? '—' : (b.closingApprox ? '≈' : '') + fmtPct(clv), 'num c-clv' + (clv === null ? '' : clv >= 0 ? ' pos' : ' neg'));
    if (b.closingApprox) cell.title = 'Approximate: the sharp line closed on a different number and was converted to yours';
    else if (clv === null && !b.wasLive && b.result !== 'void' && isNum(b.startTime) && b.startTime <= serverNow()) {
      cell.title = 'No closing line was captured for this bet';
    }
    return cell;
  }

  function onBetAction(e) {
    var target = e.target.closest('[data-action]');
    if (!target) return;
    var action = target.getAttribute('data-action');
    var id = target.getAttribute('data-bet');
    if (!id) return;
    if (action === 'settle-edit') {
      S.editBets.add(id);
      renderBets();
    } else if (action === 'settle-cancel') {
      S.editBets.delete(id);
      renderBets();
    } else if (action === 'settle') {
      settleBet(id, target.getAttribute('data-result'), target);
    }
  }

  function settleBet(id, result, btn) {
    var group = btn.parentNode;
    Array.prototype.forEach.call(group.querySelectorAll('button'), function (b) {
      b.disabled = true;
    });
    api('POST', 'api/bets/' + encodeURIComponent(id) + '/settle', { result: result })
      .then(function (rec) {
        if (Array.isArray(S.bets) && rec && typeof rec === 'object') {
          for (var i = 0; i < S.bets.length; i++) if (S.bets[i].id === id) S.bets[i] = rec;
        }
        S.editBets.delete(id);
        var profit = rec && isNum(rec.profit) ? ' ' + fmtMoney(rec.profit, { signed: true }) : '';
        toast('Marked ' + result + ': ' + (rec && rec.pick ? rec.pick : 'bet') + profit);
        renderBets();
        loadBets(true);
      })
      .catch(function (err) {
        toast(err.message, 'error');
        Array.prototype.forEach.call(group.querySelectorAll('button'), function (b) {
          b.disabled = false;
        });
      });
  }

  // ------------------------------------------------------------------------------------------- alerts

  function renderAlertsBtn() {
    el.btnAlerts.setAttribute('aria-pressed', String(S.alertsOn));
    el.btnAlerts.classList.toggle('is-on', S.alertsOn);
    el.alertsIcon.setAttribute('href', S.alertsOn ? '#i-bell' : '#i-bell-off');
    setText(el.alertsLabel, S.alertsOn ? 'Alerts on' : 'Enable alerts');
    el.btnAlerts.title = S.alertsOn ? 'Alerts on — click to turn off' : 'Enable sound + browser notifications for new high and critical picks';
  }

  function unlockAudio() {
    try {
      var Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx) return null;
      if (!S.audio) S.audio = new Ctx();
      if (S.audio.state === 'suspended') S.audio.resume().catch(function () {});
      return S.audio;
    } catch (e) {
      return null;
    }
  }

  function beep(kind) {
    var ctx = S.audio;
    if (!ctx || ctx.state !== 'running') return;
    try {
      var tones = kind === 'critical' ? [[988, 0], [1319, 0.15], [988, 0.3], [1319, 0.45]] : kind === 'high' ? [[880, 0], [1175, 0.16]] : [[880, 0]];
      var t0 = ctx.currentTime + 0.01;
      tones.forEach(function (tone) {
        var osc = ctx.createOscillator();
        var gain = ctx.createGain();
        var t = t0 + tone[1];
        osc.type = 'sine';
        osc.frequency.setValueAtTime(tone[0], t);
        gain.gain.setValueAtTime(0.0001, t);
        gain.gain.exponentialRampToValueAtTime(0.22, t + 0.012);
        gain.gain.exponentialRampToValueAtTime(0.0001, t + 0.12);
        osc.connect(gain);
        gain.connect(ctx.destination);
        osc.start(t);
        osc.stop(t + 0.13);
      });
    } catch (e) {
      /* audio is best effort */
    }
  }

  function notifySupported() {
    return typeof window.Notification === 'function' && window.isSecureContext;
  }

  function toggleAlerts() {
    if (S.alertsOn) {
      S.alertsOn = false;
      lsSet('alerts', false);
      renderAlertsBtn();
      toast('Alerts off', 'info', 2000);
      return;
    }
    S.alertsOn = true;
    lsSet('alerts', true);
    renderAlertsBtn();
    var ctx = unlockAudio();
    if (ctx) {
      Promise.resolve(ctx.state === 'running' ? null : ctx.resume()).then(
        function () {
          beep('test');
        },
        function () {}
      );
    }
    if (!notifySupported()) {
      toast('Sound alerts on. Browser notifications need HTTPS or localhost.', 'info');
      return;
    }
    if (window.Notification.permission === 'granted') {
      toast('Alerts on — sound + notification for new high and critical picks');
      return;
    }
    if (window.Notification.permission === 'denied') {
      toast('Sound alerts on. Notifications are blocked in your browser settings.', 'info');
      return;
    }
    try {
      Promise.resolve(window.Notification.requestPermission()).then(
        function (p) {
          toast(p === 'granted' ? 'Alerts on — sound + notification for new high and critical picks' : 'Sound alerts on (notifications not allowed)', p === 'granted' ? 'ok' : 'info');
        },
        function () {}
      );
    } catch (e) {
      toast('Sound alerts on', 'info');
    }
  }

  function notifyTitle(o) {
    if (o.type === 'arb') return (o.isLive ? 'LIVE ' : '') + 'ARB ' + fmtPct(o.arb ? o.arb.profitPct : o.evPct) + ' · ' + o.league;
    return (o.isLive ? 'LIVE ' : '') + verdictLabel(o.verdict) + ' · ' + o.league;
  }

  function notifyBody(o) {
    if (o.type === 'arb') return o.pick + ' @ ' + fmtAmericanAscii(o.dkAmerican) + ' at DraftKings + other leg elsewhere · ' + o.eventName;
    return (
      o.pick + ' @ ' + fmtAmericanAscii(o.dkAmerican) + ' (take ≥ ' + fmtAmericanAscii(o.minAcceptableAmerican) + ') · EV ' + fmtPct(o.evPct) + ' · stake ' + fmtMoney(o.stake) + '\n' + o.eventName
    );
  }

  function showNotification(o) {
    if (!notifySupported() || window.Notification.permission !== 'granted') return;
    try {
      var n = new window.Notification(notifyTitle(o), { body: notifyBody(o), tag: o.id, icon: 'favicon.svg', silent: true });
      n.onclick = function () {
        try {
          window.focus();
        } catch (e) {
          /* ignore */
        }
        focusCard(o.id);
        n.close();
      };
    } catch (e) {
      /* e.g. Android Chrome only allows notifications from a service worker */
    }
  }

  function focusCard(id) {
    var c = S.cards.get(id);
    if (!c) return;
    selectTab(c.opp && c.opp.type === 'arb' ? 'arbs' : 'opps');
    c.el.scrollIntoView({ block: 'center', behavior: prefersReducedMotion() ? 'auto' : 'smooth' });
  }

  function prefersReducedMotion() {
    return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  }

  function checkAlerts() {
    var parts = S.parts;
    if (!parts || !S.state) return;
    var fresh = selectAlerts(parts.live.concat(parts.pre, parts.arbs), S.alerted, S.primed, MAX_ALERT_IDS);
    S.primed = true;
    if (!fresh.length || !S.alertsOn) return;
    var critical = fresh.some(function (o) {
      return o.urgency === 'critical';
    });
    beep(critical ? 'critical' : 'high');
    fresh.slice(0, 3).forEach(showNotification);
  }

  // ------------------------------------------------------------------------------------------- connection

  function connectStream() {
    window.clearTimeout(S.reconnectTimer);
    if (S.es) {
      S.es.close();
      S.es = null;
    }
    if (typeof window.EventSource !== 'function') {
      setConn('reconnecting');
      startPolling();
      return;
    }
    var es;
    try {
      es = new window.EventSource('api/stream');
    } catch (e) {
      setConn('reconnecting');
      startPolling();
      scheduleReconnect();
      return;
    }
    S.es = es;
    es.addEventListener('state', function (ev) {
      if (S.es !== es) return;
      var data;
      try {
        data = JSON.parse(ev.data);
      } catch (e) {
        return;
      }
      S.reconnectDelay = 2000;
      stopPolling();
      if (S.conn !== 'live') setConn('live');
      applyState(data);
      if (S.conn === 'live') renderConn();
    });
    es.addEventListener('error', function () {
      if (S.es !== es) return;
      setConn('reconnecting');
      startPolling();
      renderBanners();
      if (es.readyState === 2) {
        es.close();
        S.es = null;
        scheduleReconnect();
      }
    });
    window.clearTimeout(S.firstStateTimer);
    S.firstStateTimer = window.setTimeout(function () {
      if (!S.state) pollOnce();
    }, 5000);
  }

  function scheduleReconnect() {
    window.clearTimeout(S.reconnectTimer);
    S.reconnectTimer = window.setTimeout(connectStream, S.reconnectDelay);
    S.reconnectDelay = Math.min(30000, S.reconnectDelay * 2);
  }

  function startPolling() {
    if (S.pollTimer) return;
    S.pollTimer = window.setInterval(pollOnce, POLL_MS);
    pollOnce();
  }

  function stopPolling() {
    if (!S.pollTimer) return;
    window.clearInterval(S.pollTimer);
    S.pollTimer = null;
  }

  function pollOnce() {
    return api('GET', 'api/state').then(applyState, function () {
      /* still reconnecting; the banner already says so */
    });
  }

  // ------------------------------------------------------------------------------------------- tabs

  function selectTab(name) {
    var tabs = ['opps', 'arbs', 'bets'];
    if (tabs.indexOf(name) < 0) name = 'opps';
    S.tab = name;
    lsSet('tab', name);
    tabs.forEach(function (t) {
      var tab = $('tab-' + t);
      var panel = $('panel-' + t);
      var on = t === name;
      tab.setAttribute('aria-selected', String(on));
      tab.tabIndex = on ? 0 : -1;
      panel.hidden = !on;
    });
    el.filters.hidden = name === 'bets';
    if (name === 'bets') {
      renderBets();
      loadBets(false);
    }
  }

  function onTabKey(e) {
    var tabs = Array.prototype.slice.call(document.querySelectorAll('[role=tab]'));
    var i = tabs.indexOf(document.activeElement);
    if (i < 0) return;
    var next = null;
    if (e.key === 'ArrowRight') next = tabs[(i + 1) % tabs.length];
    else if (e.key === 'ArrowLeft') next = tabs[(i - 1 + tabs.length) % tabs.length];
    else if (e.key === 'Home') next = tabs[0];
    else if (e.key === 'End') next = tabs[tabs.length - 1];
    if (!next) return;
    e.preventDefault();
    next.focus();
    selectTab(next.getAttribute('data-tab'));
  }

  // ------------------------------------------------------------------------------------------- theme

  function renderThemeBtn() {
    var t = document.documentElement.getAttribute('data-theme') === 'light' ? 'light' : 'dark';
    el.themeIcon.setAttribute('href', t === 'light' ? '#i-moon' : '#i-sun');
    el.btnTheme.setAttribute('aria-label', t === 'light' ? 'Switch to dark theme' : 'Switch to light theme');
    el.btnTheme.title = t === 'light' ? 'Dark theme' : 'Light theme';
    var meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute('content', t === 'light' ? '#f3f5f8' : '#0a0e13');
  }

  function toggleTheme() {
    var t = document.documentElement.getAttribute('data-theme') === 'light' ? 'dark' : 'light';
    applyTheme(t);
    lsSet('theme', t);
    renderThemeBtn();
  }

  // ------------------------------------------------------------------------------------------- tick

  function tick() {
    var now = serverNow();
    var rels = document.querySelectorAll('[data-rel]');
    for (var i = 0; i < rels.length; i++) renderRel(rels[i], now);
    S.cards.forEach(function (c) {
      updateCountdown(c, now);
    });
    renderConn();
    if (S.conn === 'live' && S.lastStateAt && Date.now() - S.lastStateAt > FORCE_RECONNECT_MS && Date.now() - S.lastForcedReconnect > FORCE_RECONNECT_MS) {
      S.lastForcedReconnect = Date.now();
      setConn('reconnecting');
      startPolling();
      connectStream();
    }
  }

  // ------------------------------------------------------------------------------------------- init

  function bindDialog(dlg, closeOnBackdrop) {
    dlg.addEventListener('click', function (e) {
      var closer = e.target.closest('[data-close]');
      if (closer && dlg.contains(closer)) {
        dlg.close();
        return;
      }
      if (closeOnBackdrop && e.target === dlg) dlg.close();
    });
  }

  function init() {
    [
      ['conn', 'conn'],
      ['connText', 'conn-text'],
      ['demoBadge', 'demo-badge'],
      ['feedBtn', 'feed-btn'],
      ['feedDot', 'feed-dot'],
      ['feedText', 'feed-text'],
      ['statUpdated', 'stat-updated'],
      ['statLive', 'stat-live'],
      ['statBankroll', 'stat-bankroll'],
      ['statLeft', 'stat-left'],
      ['statLeftOf', 'stat-left-of'],
      ['btnAlerts', 'btn-alerts'],
      ['alertsIcon', 'alerts-icon'],
      ['alertsLabel', 'alerts-label'],
      ['btnSettings', 'btn-settings'],
      ['btnHealth', 'btn-health'],
      ['btnTheme', 'btn-theme'],
      ['themeIcon', 'theme-icon'],
      ['countOpps', 'count-opps'],
      ['countArbs', 'count-arbs'],
      ['countBets', 'count-bets'],
      ['banners', 'banners'],
      ['filters', 'filters'],
      ['chips', 'league-chips'],
      ['fLive', 'f-live'],
      ['fMinEv', 'f-minev'],
      ['fMinEvOut', 'f-minev-out'],
      ['fWatch', 'f-watch'],
      ['emptyOpps', 'empty-opps'],
      ['secLive', 'sec-live'],
      ['gridLive', 'grid-live'],
      ['emptyLive', 'empty-live'],
      ['nLive', 'n-live'],
      ['arbNotice', 'arb-notice'],
      ['arbNoticeText', 'arb-notice-text'],
      ['arbNoticeBtn', 'arb-notice-btn'],
      ['secPre', 'sec-pre'],
      ['gridPre', 'grid-pre'],
      ['emptyPre', 'empty-pre'],
      ['nPre', 'n-pre'],
      ['secWatch', 'sec-watch'],
      ['gridWatch', 'grid-watch'],
      ['nWatch', 'n-watch'],
      ['watchToggle', 'watch-toggle'],
      ['watchToggleText', 'watch-toggle-text'],
      ['gridArbs', 'grid-arbs'],
      ['emptyArbs', 'empty-arbs'],
      ['betSummary', 'bet-summary'],
      ['betList', 'bet-list'],
      ['recent', 'recent-picks'],
      ['recentCount', 'recent-count'],
      ['recentList', 'recent-list'],
      ['betsRefresh', 'bets-refresh'],
      ['dlgPlace', 'dlg-place'],
      ['placeForm', 'place-form'],
      ['placeKicker', 'place-kicker'],
      ['placePick', 'place-pick'],
      ['placeLive', 'place-live'],
      ['placeNote', 'place-note'],
      ['placeOdds', 'place-odds'],
      ['placeSign', 'place-sign'],
      ['placeStake', 'place-stake'],
      ['placeCalc', 'place-calc'],
      ['placeNotes', 'place-notes'],
      ['placeError', 'place-error'],
      ['placeSubmit', 'place-submit'],
      ['dlgSettings', 'dlg-settings'],
      ['settingsForm', 'settings-form'],
      ['sBankroll', 's-bankroll'],
      ['sKelly', 's-kelly'],
      ['sMaxPct', 's-maxpct'],
      ['sMaxAbs', 's-maxabs'],
      ['sDaily', 's-daily'],
      ['sMinPre', 's-minpre'],
      ['sMinLive', 's-minlive'],
      ['sWatch', 's-watch'],
      ['sLeagues', 's-leagues'],
      ['sLeaguesAll', 's-leagues-all'],
      ['sLeaguesNone', 's-leagues-none'],
      ['sArbs', 's-arbs'],
      ['settingsError', 'settings-error'],
      ['settingsSubmit', 'settings-submit'],
      ['dlgHealth', 'dlg-health'],
      ['healthBody', 'health-body'],
      ['healthSub', 'health-sub'],
      ['toasts', 'toasts'],
    ].forEach(function (pair) {
      el[pair[0]] = $(pair[1]);
    });

    renderThemeBtn();
    renderAlertsBtn();
    syncFilterControls();
    renderConn();

    el.btnTheme.addEventListener('click', toggleTheme);
    el.btnAlerts.addEventListener('click', toggleAlerts);
    el.btnSettings.addEventListener('click', openSettings);
    el.btnHealth.addEventListener('click', openHealth);
    el.feedBtn.addEventListener('click', openHealth);

    document.querySelector('[role=tablist]').addEventListener('keydown', onTabKey);
    Array.prototype.forEach.call(document.querySelectorAll('[role=tab]'), function (tab) {
      tab.addEventListener('click', function () {
        selectTab(tab.getAttribute('data-tab'));
      });
    });
    el.arbNoticeBtn.addEventListener('click', function () {
      selectTab('arbs');
      window.scrollTo(0, 0);
    });

    // Delegated card/chip/empty-state actions.
    document.getElementById('main').addEventListener('click', onCardAction);
    el.chips.addEventListener('click', onCardAction);

    el.fLive.addEventListener('change', function () {
      S.filters.liveOnly = el.fLive.checked;
      onFiltersChanged();
    });
    el.fWatch.addEventListener('change', function () {
      S.filters.showWatch = el.fWatch.checked;
      onFiltersChanged();
    });
    el.fMinEv.addEventListener('input', function () {
      S.filters.minEv = clamp(Number(el.fMinEv.value) || 0, 0, 10);
      onFiltersChanged();
    });
    el.watchToggle.addEventListener('click', function () {
      S.filters.showWatch = !S.filters.showWatch;
      onFiltersChanged();
    });

    // Bets tab
    el.betList.addEventListener('click', onBetAction);
    el.recentList.addEventListener('click', onRecentAction);
    el.betsRefresh.addEventListener('click', function () {
      loadBets(false);
    });
    Array.prototype.forEach.call(document.querySelectorAll('[data-betfilter]'), function (seg) {
      seg.addEventListener('click', function () {
        S.betFilter = seg.getAttribute('data-betfilter');
        Array.prototype.forEach.call(document.querySelectorAll('[data-betfilter]'), function (s) {
          s.setAttribute('aria-pressed', String(s === seg));
        });
        renderBets();
      });
    });

    // Dialogs
    bindDialog(el.dlgPlace, false);
    bindDialog(el.dlgSettings, false);
    bindDialog(el.dlgHealth, true);
    el.dlgPlace.addEventListener('close', function () {
      S.place = null;
    });
    el.placeForm.addEventListener('submit', submitPlace);
    el.placeOdds.addEventListener('input', updatePlaceCalc);
    el.placeStake.addEventListener('input', updatePlaceCalc);
    el.placeSign.addEventListener('click', function () {
      var v = el.placeOdds.value.trim().replace(/^[−]/, '-');
      if (v.charAt(0) === '-') v = '+' + v.slice(1);
      else if (v.charAt(0) === '+') v = '-' + v.slice(1);
      else if (v) v = '-' + v;
      el.placeOdds.value = v;
      updatePlaceCalc();
      el.placeOdds.focus();
    });
    el.settingsForm.addEventListener('submit', submitSettings);
    el.sLeaguesAll.addEventListener('click', function () {
      setAllLeagues(true);
    });
    el.sLeaguesNone.addEventListener('click', function () {
      setAllLeagues(false);
    });

    // Audio can only start after a user gesture; re-arm it on the first interaction after a reload.
    if (S.alertsOn) {
      var arm = function () {
        unlockAudio();
        document.removeEventListener('pointerdown', arm, true);
        document.removeEventListener('keydown', arm, true);
      };
      document.addEventListener('pointerdown', arm, true);
      document.addEventListener('keydown', arm, true);
    }

    document.addEventListener('visibilitychange', function () {
      if (document.visibilityState !== 'visible') return;
      if (!S.es || S.es.readyState === 2) connectStream();
      else if (S.lastStateAt && Date.now() - S.lastStateAt > STALL_MS) pollOnce();
    });

    selectTab(lsGet('tab', 'opps'));
    renderOpportunities();
    renderCounts();
    renderBetSummary();
    connectStream();
    loadBets(true);
    window.setInterval(tick, 1000);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();

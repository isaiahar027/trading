import type { AppConfig } from '../config';
import type { MarketKind, Opportunity, Side } from '../types';
import { formatAmerican } from '../util/odds';
import { createLogger } from '../util/logger';

/**
 * Optional phone/desktop alerts for new actionable opportunities: Discord webhook, ntfy topic, Telegram bot.
 * notify() never throws, sends at most 5 messages per call, and applies a per-opportunity cooldown.
 * Webhook URLs and bot tokens are secrets: they are never logged (errors are scrubbed before logging).
 */

const log = createLogger('notifier');

export interface NotifierDeps {
  fetchImpl?: typeof fetch;
  now?: () => number;
}

type Channel = 'discord' | 'ntfy' | 'telegram';

const MAX_PER_CALL = 5;
const REQUEST_TIMEOUT_MS = 8000;
const COOLDOWN_CAP = 2000;
const DISCORD_MAX_CHARS = 2000;
const TELEGRAM_MAX_CHARS = 4096;
const NTFY_MAX_BODY_CHARS = 3500;
const MAX_HEADER_CHARS = 250;

const VERDICT_LABEL: Record<Opportunity['verdict'], string> = {
  BET_NOW: 'BET NOW',
  BET: 'BET',
  WATCH: 'WATCH',
};

const BOOK_NAMES: Record<string, string> = {
  draftkings: 'DraftKings',
  fanduel: 'FanDuel',
  betmgm: 'BetMGM',
  williamhill_us: 'Caesars',
  betrivers: 'BetRivers',
  fanatics: 'Fanatics',
  bovada: 'Bovada',
  pinnacle: 'Pinnacle',
  betonlineag: 'BetOnline',
  lowvig: 'LowVig',
  espnbet: 'ESPN BET',
  hardrockbet: 'Hard Rock Bet',
  ballybet: 'Bally Bet',
  betparx: 'betPARX',
  mybookieag: 'MyBookie',
};

function bookName(key: string): string {
  return BOOK_NAMES[key] ?? key;
}

function truncate(s: string, max: number): string {
  return s.length > max ? `${s.slice(0, max - 1)}…` : s;
}

function signedPct(fraction: number, digits = 1): string {
  const v = Number.isFinite(fraction) ? fraction * 100 : 0;
  const s = v.toFixed(digits);
  return v >= 0 ? `+${s}%` : `${s}%`;
}

function dollars(n: number): string {
  return `$${Number.isFinite(n) ? Math.max(0, Math.round(n)) : 0}`;
}

/** Arb legs are split to the cent: "$18.38" (or "$18" when whole). */
function legDollars(n: number): string {
  const cents = Number.isFinite(n) ? Math.max(0, Math.round(n * 100)) : 0;
  return cents % 100 === 0 ? `$${cents / 100}` : `$${Math.floor(cents / 100)}.${String(cents % 100).padStart(2, '0')}`;
}

function american(n: number): string {
  return Number.isFinite(n) ? formatAmerican(n) : '?';
}

function signedLine(line: number): string {
  if (line === 0) return 'PK';
  return line > 0 ? `+${line}` : `${line}`;
}

/** Leg label for arb legs: "Knicks +3.5", "Under 224.5", "Draw", "Celtics ML". */
function legLabel(kind: MarketKind, side: Side, line: number | null, home: string, away: string): string {
  if (side === 'draw') return 'Draw';
  if (side === 'over' || side === 'under') {
    const word = side === 'over' ? 'Over' : 'Under';
    return line === null ? word : `${word} ${line}`;
  }
  const team = side === 'home' ? home : away;
  if (kind === 'moneyline' || line === null) return `${team} ML`;
  return `${team} ${signedLine(line)}`;
}

/** HTTP header values must be Latin-1 and single-line; non-ASCII text is sent as an RFC 2047 encoded-word. */
function headerValue(s: string): string {
  // eslint-disable-next-line no-control-regex
  const clean = truncate(s.replace(/[\u0000-\u001f\u007f]+/g, ' ').trim(), MAX_HEADER_CHARS);
  // eslint-disable-next-line no-control-regex
  if (/^[ -~]*$/.test(clean)) return clean;
  return `=?UTF-8?B?${Buffer.from(clean, 'utf8').toString('base64')}?=`;
}

function safeHttpsUrl(u: string | null): string | null {
  if (!u) return null;
  try {
    const parsed = new URL(u);
    return parsed.protocol === 'https:' ? parsed.href : null;
  } catch {
    return null;
  }
}

class ChannelError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'ChannelError';
  }
}

export class Notifier {
  private readonly cfg: AppConfig['notify'];
  private readonly fetchImpl: typeof fetch;
  private readonly now: () => number;
  /** opportunity id -> last send attempt (insertion order == time order; entries are re-inserted on update). */
  private readonly lastSent = new Map<string, number>();
  private readonly secrets: string[];

  constructor(cfg: AppConfig['notify'], deps: NotifierDeps = {}) {
    this.cfg = { ...cfg };
    this.fetchImpl = deps.fetchImpl ?? ((input, init) => fetch(input, init));
    this.now = deps.now ?? Date.now;
    this.secrets = [cfg.discordWebhookUrl, cfg.ntfyUrl, cfg.telegramBotToken]
      .map((s) => (s ?? '').trim())
      .filter((s) => s.length >= 4)
      .sort((a, b) => b.length - a.length);
  }

  get enabled(): boolean {
    return this.channels().length > 0;
  }

  formatMessage(o: Opportunity): { title: string; body: string } {
    const live = o.isLive ? 'LIVE ' : '';
    if (o.type === 'arb' && o.arb && o.arb.legs.length > 0) {
      const head = `${live}ARB`;
      const legs = o.arb.legs
        .map((l) => `${legLabel(o.kind, l.side, l.line, o.home, o.away)} @ ${american(l.american)} ${bookName(l.book)} ${legDollars(l.stake)}`)
        .join(' + ');
      const title = `${head} ${signedPct(o.arb.profitPct)}: ${o.pick} @ ${american(o.dkAmerican)} + other book`;
      const body = [head, o.league, legs, `profit ${signedPct(o.arb.profitPct)}`, `total ${dollars(o.arb.totalStake)}`, o.eventName].join(' · ');
      return { title, body };
    }
    const head = `${live}${VERDICT_LABEL[o.verdict] ?? o.verdict}`;
    const price = american(o.dkAmerican);
    const title = `${head}: ${o.pick} @ ${price}`;
    const body = [
      head,
      o.league,
      `${o.pick} @ ${price} (take ≥ ${american(o.minAcceptableAmerican)})`,
      `EV ${signedPct(o.evPct)}`,
      `stake ${dollars(o.stake)}`,
      o.eventName,
    ].join(' · ');
    return { title, body };
  }

  /**
   * Sends alerts for the given opportunities to every configured channel. Returns how many opportunities were
   * delivered to at least one channel. Skips ids still in cooldown and 'gone' items; at most 5 per call. Never throws.
   */
  async notify(opps: Opportunity[]): Promise<number> {
    try {
      const channels = this.channels();
      if (channels.length === 0 || !Array.isArray(opps) || opps.length === 0) return 0;
      const now = this.now();
      this.pruneCooldown(now);
      const cooldownMs = Math.max(0, this.cfg.cooldownSec) * 1000;

      const seen = new Set<string>();
      const ordered = opps
        .map((o, i) => ({ o, i }))
        .sort((a, b) => (b.o.urgencyScore ?? 0) - (a.o.urgencyScore ?? 0) || a.i - b.i)
        .map((x) => x.o);
      const batch: Opportunity[] = [];
      for (const o of ordered) {
        if (batch.length >= MAX_PER_CALL) break;
        if (!o || typeof o.id !== 'string' || o.status === 'gone' || seen.has(o.id)) continue;
        seen.add(o.id);
        const last = this.lastSent.get(o.id);
        if (last !== undefined && now - last < cooldownMs) continue;
        batch.push(o);
      }
      if (batch.length === 0) return 0;

      // Record the attempt up front so overlapping calls cannot double-send and a failing channel is not hammered.
      for (const o of batch) {
        this.lastSent.delete(o.id);
        this.lastSent.set(o.id, now);
      }
      this.enforceCap();

      const messages = batch.map((o) => ({ id: o.id, msg: this.formatMessage(o), opp: o }));
      // Channels run in parallel; within a channel messages go out in order so chats read top-down.
      const perChannel = await Promise.all(
        channels.map(async (ch) => {
          const delivered = new Set<string>();
          for (const m of messages) {
            if (await this.sendOne(ch, m.opp, m.msg)) delivered.add(m.id);
          }
          return delivered;
        }),
      );
      let count = 0;
      for (const m of messages) if (perChannel.some((d) => d.has(m.id))) count++;
      return count;
    } catch (err) {
      log.error('notify failed unexpectedly', { error: this.scrub(err) });
      return 0;
    }
  }

  private channels(): Channel[] {
    const out: Channel[] = [];
    if (this.cfg.discordWebhookUrl.trim() !== '') out.push('discord');
    if (this.cfg.ntfyUrl.trim() !== '') out.push('ntfy');
    if (this.cfg.telegramBotToken.trim() !== '' && this.cfg.telegramChatId.trim() !== '') out.push('telegram');
    return out;
  }

  private async sendOne(ch: Channel, o: Opportunity, msg: { title: string; body: string }): Promise<boolean> {
    const link = safeHttpsUrl(o.dkUrl);
    let url: string;
    let init: RequestInit;
    if (ch === 'discord') {
      const content = truncate(`**${msg.title}**\n${msg.body}${link ? `\n<${link}>` : ''}`, DISCORD_MAX_CHARS);
      url = this.cfg.discordWebhookUrl.trim();
      init = {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content, allowed_mentions: { parse: [] } }),
      };
    } else if (ch === 'ntfy') {
      const headers: Record<string, string> = {
        'Content-Type': 'text/plain; charset=utf-8',
        Title: headerValue(msg.title),
        Priority: o.urgency === 'critical' ? '5' : o.urgency === 'high' ? '4' : '3',
        Tags: o.isLive ? 'rotating_light' : 'moneybag',
      };
      if (link) headers.Click = link;
      url = this.cfg.ntfyUrl.trim();
      init = { method: 'POST', headers, body: truncate(msg.body, NTFY_MAX_BODY_CHARS) };
    } else {
      const text = truncate(`${msg.title}\n${msg.body}${link ? `\n${link}` : ''}`, TELEGRAM_MAX_CHARS);
      url = `https://api.telegram.org/bot${this.cfg.telegramBotToken.trim()}/sendMessage`;
      init = {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ chat_id: this.cfg.telegramChatId.trim(), text, disable_web_page_preview: true }),
      };
    }

    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    timer.unref?.();
    try {
      const res = await this.fetchImpl(url, { ...init, signal: controller.signal });
      const status = res.status;
      try {
        await res.body?.cancel();
      } catch {
        // response body is irrelevant; ignore errors discarding it
      }
      if (!res.ok) throw new ChannelError(`HTTP ${status}`);
      return true;
    } catch (err) {
      const reason = controller.signal.aborted ? `timed out after ${REQUEST_TIMEOUT_MS / 1000}s` : this.scrub(err);
      log.warn(`${ch} notification failed`, { opportunity: o.id, reason });
      return false;
    } finally {
      clearTimeout(timer);
    }
  }

  /** Error text without secrets: strips URLs and every configured webhook URL / token. */
  private scrub(err: unknown): string {
    let s: string;
    if (err instanceof Error) {
      const cause = (err as Error & { cause?: unknown }).cause;
      const causeCode = cause && typeof cause === 'object' && 'code' in cause ? String((cause as { code: unknown }).code) : '';
      s = `${err.name}: ${err.message}${causeCode ? ` (${causeCode})` : ''}`;
    } else {
      s = String(err);
    }
    for (const secret of this.secrets) s = s.split(secret).join('[redacted]');
    s = s.replace(/https?:\/\/\S+/gi, '[url]').replace(/bot\d+:[A-Za-z0-9_-]+/g, 'bot[redacted]');
    return truncate(s, 300);
  }

  private pruneCooldown(now: number): void {
    const cooldownMs = Math.max(0, this.cfg.cooldownSec) * 1000;
    for (const [id, at] of this.lastSent) {
      if (now - at < cooldownMs) break;
      this.lastSent.delete(id);
    }
    this.enforceCap();
  }

  private enforceCap(): void {
    while (this.lastSent.size > COOLDOWN_CAP) {
      const oldest = this.lastSent.keys().next().value;
      if (oldest === undefined) break;
      this.lastSent.delete(oldest);
    }
  }
}

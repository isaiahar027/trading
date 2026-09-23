import { afterEach, describe, expect, it, vi } from 'vitest';
import type { AppConfig } from '../src/config';
import { Notifier } from '../src/server/notifier';
import type { Opportunity } from '../src/types';

type NotifyCfg = AppConfig['notify'];

const DISCORD = 'https://discord.com/api/webhooks/123456/SECRET-discord-token-abc';
const NTFY = 'https://ntfy.sh/secret-topic-xyz';
const TG_TOKEN = '987654:SECRET-telegram-token';
const TG_CHAT = '-100200300';

function cfg(overrides: Partial<NotifyCfg> = {}): NotifyCfg {
  return {
    discordWebhookUrl: '',
    ntfyUrl: '',
    telegramBotToken: '',
    telegramChatId: '',
    minUrgency: 'critical',
    cooldownSec: 300,
    ...overrides,
  };
}

function opp(overrides: Partial<Opportunity> = {}): Opportunity {
  return {
    id: 'odds-api:e1|ev|spread|home|-3.5',
    type: 'ev',
    eventId: 'odds-api:e1',
    league: 'NBA',
    eventName: 'Knicks @ Celtics',
    home: 'Celtics',
    away: 'Knicks',
    startTime: 1_000,
    isLive: true,
    kind: 'spread',
    side: 'home',
    line: -3.5,
    pick: 'Celtics -3.5',
    dkDecimal: 2.1,
    dkAmerican: 110,
    fairProb: 0.5,
    fairDecimal: 2,
    fairAmerican: 100,
    evPct: 0.052,
    minAcceptableAmerican: 104,
    kellyFraction: 0.038,
    stake: 38,
    confidence: 0.8,
    urgency: 'critical',
    urgencyScore: 90,
    verdict: 'BET_NOW',
    reasons: ['x'],
    sharpSource: 'pinnacle',
    sharpAgeSec: 5,
    dkAgeSec: 5,
    staleLine: false,
    firstSeen: 0,
    lastSeen: 0,
    status: 'active',
    expiresInSec: 45,
    dkUrl: 'https://sportsbook.draftkings.com/event/123',
    ...overrides,
  };
}

interface Call {
  url: string;
  init: RequestInit;
}

function okFetch(calls: Call[], status = 200): typeof fetch {
  return (async (input: string | URL | Request, init?: RequestInit) => {
    calls.push({ url: String(input), init: init ?? {} });
    return new Response(status === 204 ? null : 'ok', { status });
  }) as typeof fetch;
}

function headersOf(c: Call): Record<string, string> {
  return c.init.headers as Record<string, string>;
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe('Notifier.enabled', () => {
  it('is false with nothing configured and notify is a no-op', async () => {
    const fetchImpl = vi.fn();
    const n = new Notifier(cfg(), { fetchImpl: fetchImpl as unknown as typeof fetch });
    expect(n.enabled).toBe(false);
    expect(await n.notify([opp()])).toBe(0);
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it('needs both telegram token and chat id', () => {
    expect(new Notifier(cfg({ telegramBotToken: TG_TOKEN })).enabled).toBe(false);
    expect(new Notifier(cfg({ telegramChatId: TG_CHAT })).enabled).toBe(false);
    expect(new Notifier(cfg({ telegramBotToken: TG_TOKEN, telegramChatId: TG_CHAT })).enabled).toBe(true);
    expect(new Notifier(cfg({ discordWebhookUrl: DISCORD })).enabled).toBe(true);
    expect(new Notifier(cfg({ ntfyUrl: NTFY })).enabled).toBe(true);
  });
});

describe('Notifier.formatMessage', () => {
  const n = new Notifier(cfg());

  it('matches the spec example for a live BET NOW', () => {
    const m = n.formatMessage(opp());
    expect(m.body).toBe('LIVE BET NOW · NBA · Celtics -3.5 @ +110 (take ≥ +104) · EV +5.2% · stake $38 · Knicks @ Celtics');
    expect(m.title).toBe('LIVE BET NOW: Celtics -3.5 @ +110');
  });

  it('formats a pre-game BET with negative odds', () => {
    const m = n.formatMessage(
      opp({
        isLive: false,
        verdict: 'BET',
        league: 'NFL',
        pick: 'Chiefs ML',
        dkAmerican: -120,
        minAcceptableAmerican: -125,
        evPct: 0.02,
        stake: 24.6,
        eventName: 'Bills @ Chiefs',
      }),
    );
    expect(m.body).toBe('BET · NFL · Chiefs ML @ -120 (take ≥ -125) · EV +2.0% · stake $25 · Bills @ Chiefs');
    expect(m.title).toBe('BET: Chiefs ML @ -120');
  });

  it('formats a WATCH with no stake', () => {
    const m = n.formatMessage(opp({ isLive: false, verdict: 'WATCH', stake: 0, evPct: 0.008 }));
    expect(m.body).toContain('WATCH · NBA');
    expect(m.body).toContain('EV +0.8%');
    expect(m.body).toContain('stake $0');
  });

  it('formats an arb with both legs and books', () => {
    const m = n.formatMessage(
      opp({
        type: 'arb',
        isLive: false,
        verdict: 'BET',
        arb: {
          profitPct: 0.012,
          totalStake: 100,
          legs: [
            { book: 'draftkings', side: 'home', line: -3.5, decimal: 2.1, american: 110, stake: 49 },
            { book: 'fanduel', side: 'away', line: 3.5, decimal: 2.02, american: 102, stake: 51 },
          ],
        },
      }),
    );
    expect(m.body).toBe(
      'ARB · NBA · Celtics -3.5 @ +110 DraftKings $49 + Knicks +3.5 @ +102 FanDuel $51 · profit +1.2% · total $100 · Knicks @ Celtics',
    );
    expect(m.title).toContain('ARB +1.2%');
  });
});

describe('Notifier channels', () => {
  it('Discord: POST JSON {content} to the webhook', async () => {
    const calls: Call[] = [];
    const n = new Notifier(cfg({ discordWebhookUrl: DISCORD }), { fetchImpl: okFetch(calls, 204), now: () => 0 });
    expect(await n.notify([opp()])).toBe(1);
    expect(calls).toHaveLength(1);
    const c = calls[0];
    expect(c.url).toBe(DISCORD);
    expect(c.init.method).toBe('POST');
    expect(headersOf(c)['Content-Type']).toBe('application/json');
    expect(c.init.signal).toBeInstanceOf(AbortSignal);
    const body = JSON.parse(String(c.init.body)) as { content: string; allowed_mentions: unknown };
    expect(body.content).toContain('LIVE BET NOW: Celtics -3.5 @ +110');
    expect(body.content).toContain('LIVE BET NOW · NBA · Celtics -3.5 @ +110 (take ≥ +104)');
    expect(body.content).toContain('https://sportsbook.draftkings.com/event/123');
    expect(body.allowed_mentions).toEqual({ parse: [] });
    expect(body.content.length).toBeLessThanOrEqual(2000);
  });

  it('ntfy: POST text with Title, Priority, Tags and Click headers', async () => {
    const calls: Call[] = [];
    const n = new Notifier(cfg({ ntfyUrl: NTFY, cooldownSec: 0 }), { fetchImpl: okFetch(calls), now: () => 0 });
    await n.notify([opp({ id: 'a', urgency: 'critical', isLive: true })]);
    await n.notify([opp({ id: 'b', urgency: 'high', isLive: false, dkUrl: null })]);
    await n.notify([opp({ id: 'c', urgency: 'medium', isLive: false })]);
    expect(calls).toHaveLength(3);
    expect(calls.every((c) => c.url === NTFY && c.init.method === 'POST')).toBe(true);

    const [a, b, c] = calls.map(headersOf);
    expect(a.Title).toBe('LIVE BET NOW: Celtics -3.5 @ +110');
    expect(a.Priority).toBe('5');
    expect(a.Tags).toBe('rotating_light');
    expect(a.Click).toBe('https://sportsbook.draftkings.com/event/123');
    expect(b.Priority).toBe('4');
    expect(b.Tags).toBe('moneybag');
    expect(b.Click).toBeUndefined();
    expect(c.Priority).toBe('3');
    expect(String(calls[0].init.body)).toBe(
      'LIVE BET NOW · NBA · Celtics -3.5 @ +110 (take ≥ +104) · EV +5.2% · stake $38 · Knicks @ Celtics',
    );
  });

  it('ntfy: non-ASCII titles are RFC 2047 encoded and newlines stripped so headers stay valid', async () => {
    const calls: Call[] = [];
    const n = new Notifier(cfg({ ntfyUrl: NTFY }), { fetchImpl: okFetch(calls), now: () => 0 });
    await n.notify([opp({ pick: 'Beşiktaş\nML' })]);
    const title = headersOf(calls[0]).Title;
    expect(title).toMatch(/^=\?UTF-8\?B\?[A-Za-z0-9+/=]+\?=$/);
    const decoded = Buffer.from(title.slice('=?UTF-8?B?'.length, -2), 'base64').toString('utf8');
    expect(decoded).toBe('LIVE BET NOW: Beşiktaş ML @ +110');
    // Real fetch would reject a non-Latin-1 header; make sure the value is constructible.
    expect(() => new Headers({ Title: title })).not.toThrow();
  });

  it('ignores non-https deep links', async () => {
    const calls: Call[] = [];
    const n = new Notifier(cfg({ ntfyUrl: NTFY }), { fetchImpl: okFetch(calls), now: () => 0 });
    await n.notify([opp({ dkUrl: 'javascript:alert(1)' })]);
    expect(headersOf(calls[0]).Click).toBeUndefined();
  });

  it('Telegram: POST JSON {chat_id, text, disable_web_page_preview} to the bot API', async () => {
    const calls: Call[] = [];
    const n = new Notifier(cfg({ telegramBotToken: TG_TOKEN, telegramChatId: TG_CHAT }), {
      fetchImpl: okFetch(calls),
      now: () => 0,
    });
    expect(await n.notify([opp()])).toBe(1);
    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe(`https://api.telegram.org/bot${TG_TOKEN}/sendMessage`);
    expect(calls[0].init.method).toBe('POST');
    expect(headersOf(calls[0])['Content-Type']).toBe('application/json');
    const body = JSON.parse(String(calls[0].init.body)) as Record<string, unknown>;
    expect(body.chat_id).toBe(TG_CHAT);
    expect(body.disable_web_page_preview).toBe(true);
    expect(body.text).toContain('LIVE BET NOW · NBA · Celtics -3.5 @ +110');
    expect(Object.keys(body).sort()).toEqual(['chat_id', 'disable_web_page_preview', 'text']);
  });

  it('sends one message per opportunity to every configured channel and counts opportunities', async () => {
    const calls: Call[] = [];
    const n = new Notifier(
      cfg({ discordWebhookUrl: DISCORD, ntfyUrl: NTFY, telegramBotToken: TG_TOKEN, telegramChatId: TG_CHAT }),
      { fetchImpl: okFetch(calls), now: () => 0 },
    );
    expect(await n.notify([opp({ id: 'a' }), opp({ id: 'b' })])).toBe(2);
    expect(calls).toHaveLength(6);
  });
});

describe('Notifier limits', () => {
  it('applies a per-id cooldown', async () => {
    const calls: Call[] = [];
    let t = 1_000_000;
    const n = new Notifier(cfg({ discordWebhookUrl: DISCORD, cooldownSec: 300 }), { fetchImpl: okFetch(calls), now: () => t });
    expect(await n.notify([opp({ id: 'a' })])).toBe(1);
    t += 299_999;
    expect(await n.notify([opp({ id: 'a' })])).toBe(0);
    expect(await n.notify([opp({ id: 'b' })])).toBe(1);
    t += 1;
    expect(await n.notify([opp({ id: 'a' })])).toBe(1);
    expect(calls).toHaveLength(3);
  });

  it('sends at most 5 per call, most urgent first, and de-duplicates ids', async () => {
    const calls: Call[] = [];
    const n = new Notifier(cfg({ discordWebhookUrl: DISCORD }), { fetchImpl: okFetch(calls), now: () => 0 });
    const list = Array.from({ length: 8 }, (_, i) => opp({ id: `o${i}`, urgencyScore: i * 10, pick: `Pick ${i}` }));
    list.push(opp({ id: 'o7', urgencyScore: 70, pick: 'dup' }));
    expect(await n.notify(list)).toBe(5);
    expect(calls).toHaveLength(5);
    const picks = calls.map((c) => (JSON.parse(String(c.init.body)) as { content: string }).content.split('\n')[0]);
    expect(picks).toEqual([7, 6, 5, 4, 3].map((i) => `**LIVE BET NOW: Pick ${i} @ +110**`));

    // The rest go out on the next call; the sent ones are cooling down.
    expect(await n.notify(list)).toBe(3);
    expect(calls).toHaveLength(8);
  });

  it('skips opportunities that are already gone', async () => {
    const calls: Call[] = [];
    const n = new Notifier(cfg({ discordWebhookUrl: DISCORD }), { fetchImpl: okFetch(calls), now: () => 0 });
    expect(await n.notify([opp({ status: 'gone' })])).toBe(0);
    expect(calls).toHaveLength(0);
  });

  it('keeps the cooldown map bounded', async () => {
    const calls: Call[] = [];
    let t = 0;
    const n = new Notifier(cfg({ discordWebhookUrl: DISCORD, cooldownSec: 1e6 }), { fetchImpl: okFetch(calls), now: () => t });
    for (let i = 0; i < 450; i++) {
      t += 1;
      await n.notify(Array.from({ length: 5 }, (_, k) => opp({ id: `id-${i}-${k}` })));
    }
    expect(calls).toHaveLength(2250);
    const size = (n as unknown as { lastSent: Map<string, number> }).lastSent.size;
    expect(size).toBeLessThanOrEqual(2000);
    // The newest ids are still cooling down.
    expect(await n.notify([opp({ id: 'id-449-4' })])).toBe(0);
  });

  it('prunes expired cooldown entries', async () => {
    const calls: Call[] = [];
    let t = 0;
    const n = new Notifier(cfg({ discordWebhookUrl: DISCORD, cooldownSec: 10 }), { fetchImpl: okFetch(calls), now: () => t });
    await n.notify([opp({ id: 'a' }), opp({ id: 'b' })]);
    t = 20_000;
    await n.notify([opp({ id: 'c' })]);
    const map = (n as unknown as { lastSent: Map<string, number> }).lastSent;
    expect(Array.from(map.keys())).toEqual(['c']);
  });
});

describe('Notifier failures', () => {
  it('never throws when fetch rejects or returns an error status', async () => {
    const rejecting = (async () => {
      throw new Error('boom');
    }) as unknown as typeof fetch;
    const n1 = new Notifier(cfg({ discordWebhookUrl: DISCORD }), { fetchImpl: rejecting, now: () => 0 });
    await expect(n1.notify([opp()])).resolves.toBe(0);

    const calls: Call[] = [];
    const n2 = new Notifier(cfg({ ntfyUrl: NTFY }), { fetchImpl: okFetch(calls, 500), now: () => 0 });
    await expect(n2.notify([opp()])).resolves.toBe(0);

    const throwingSync = (() => {
      throw new TypeError('sync failure');
    }) as unknown as typeof fetch;
    const n3 = new Notifier(cfg({ ntfyUrl: NTFY }), { fetchImpl: throwingSync, now: () => 0 });
    await expect(n3.notify([opp()])).resolves.toBe(0);

    await expect(n3.notify(null as unknown as Opportunity[])).resolves.toBe(0);
    await expect(n3.notify([null as unknown as Opportunity])).resolves.toBe(0);
  });

  it('counts an opportunity as sent when at least one channel succeeds', async () => {
    const calls: Call[] = [];
    const fetchImpl = (async (input: string | URL | Request, init?: RequestInit) => {
      calls.push({ url: String(input), init: init ?? {} });
      if (String(input) === DISCORD) return new Response('nope', { status: 429 });
      return new Response('ok', { status: 200 });
    }) as typeof fetch;
    const n = new Notifier(cfg({ discordWebhookUrl: DISCORD, ntfyUrl: NTFY }), { fetchImpl, now: () => 0 });
    expect(await n.notify([opp()])).toBe(1);
    expect(calls).toHaveLength(2);
  });

  it('aborts a hanging request after 8 seconds', async () => {
    vi.useFakeTimers();
    let signal: AbortSignal | undefined;
    const hanging = ((_input: string | URL | Request, init?: RequestInit) =>
      new Promise<Response>((_resolve, reject) => {
        signal = init?.signal ?? undefined;
        signal?.addEventListener('abort', () => reject(new DOMException('This operation was aborted', 'AbortError')));
      })) as typeof fetch;
    const n = new Notifier(cfg({ discordWebhookUrl: DISCORD }), { fetchImpl: hanging, now: () => 0 });
    const p = n.notify([opp()]);
    await vi.advanceTimersByTimeAsync(7_999);
    expect(signal?.aborted).toBe(false);
    await vi.advanceTimersByTimeAsync(1);
    expect(signal?.aborted).toBe(true);
    await expect(p).resolves.toBe(0);
  });

  it('never logs webhook URLs, ntfy topics or bot tokens', async () => {
    const out: string[] = [];
    vi.spyOn(process.stderr, 'write').mockImplementation((chunk: string | Uint8Array) => {
      out.push(String(chunk));
      return true;
    });
    vi.spyOn(process.stdout, 'write').mockImplementation((chunk: string | Uint8Array) => {
      out.push(String(chunk));
      return true;
    });
    // Move the wall clock past the logger's repeat throttle so this test's warnings are actually written.
    const realNow = Date.now();
    vi.spyOn(Date, 'now').mockReturnValue(realNow + 3_600_000);

    const leaky = (async (input: string | URL | Request) => {
      const url = String(input);
      const err = new TypeError(`Failed to parse URL from ${url}`);
      (err as Error & { cause?: unknown }).cause = { code: 'ERR_INVALID_URL', input: url };
      throw err;
    }) as typeof fetch;
    const n = new Notifier(
      cfg({ discordWebhookUrl: DISCORD, ntfyUrl: NTFY, telegramBotToken: TG_TOKEN, telegramChatId: TG_CHAT }),
      { fetchImpl: leaky, now: () => 0 },
    );
    expect(await n.notify([opp()])).toBe(0);

    const logged = out.join('');
    expect(logged).toContain('discord notification failed');
    expect(logged).toContain('ntfy notification failed');
    expect(logged).toContain('telegram notification failed');
    expect(logged).toContain('ERR_INVALID_URL');
    for (const secret of [DISCORD, NTFY, TG_TOKEN, 'SECRET', 'secret-topic', '123456/']) {
      expect(logged).not.toContain(secret);
    }
  });
});

import { getEventListeners } from 'node:events';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { AbortError, backoffDelay, retry, sleep } from '../src/util/retry';

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe('AbortError', () => {
  it('is an Error named AbortError', () => {
    const e = new AbortError();
    expect(e).toBeInstanceOf(Error);
    expect(e.name).toBe('AbortError');
    expect(e.message).toBe('Aborted');
    expect(new AbortError('stop').message).toBe('stop');
  });
});

describe('backoffDelay', () => {
  it('grows exponentially without jitter and is capped at maxMs', () => {
    expect(backoffDelay(1, 100, 10_000, 2, false)).toBe(100);
    expect(backoffDelay(2, 100, 10_000, 2, false)).toBe(200);
    expect(backoffDelay(3, 100, 10_000, 2, false)).toBe(400);
    expect(backoffDelay(5, 100, 10_000, 2, false)).toBe(1600);
    expect(backoffDelay(20, 100, 10_000, 2, false)).toBe(10_000);
    expect(backoffDelay(3, 100, 10_000, 3, false)).toBe(900);
  });

  it('treats attempt <= 1 as the base delay', () => {
    expect(backoffDelay(0, 100, 10_000, 2, false)).toBe(100);
    expect(backoffDelay(-5, 100, 10_000, 2, false)).toBe(100);
  });

  it('never overflows for huge attempt numbers', () => {
    expect(backoffDelay(10_000, 100, 5_000, 2, false)).toBe(5_000);
    const j = backoffDelay(10_000, 100, 5_000);
    expect(j).toBeGreaterThanOrEqual(2_500);
    expect(j).toBeLessThanOrEqual(5_000);
  });

  it('full jitter stays within [exp/2, exp]', () => {
    const random = vi.spyOn(Math, 'random');
    random.mockReturnValue(0);
    expect(backoffDelay(3, 100, 10_000)).toBe(200);
    random.mockReturnValue(0.999999);
    expect(backoffDelay(3, 100, 10_000)).toBe(400);
    random.mockReturnValue(0.5);
    expect(backoffDelay(3, 100, 10_000)).toBe(300);
    random.mockRestore();

    for (let attempt = 1; attempt <= 12; attempt++) {
      const exp = Math.min(8_000, 250 * 2 ** (attempt - 1));
      for (let i = 0; i < 50; i++) {
        const d = backoffDelay(attempt, 250, 8_000);
        expect(d).toBeGreaterThanOrEqual(Math.floor(exp / 2));
        expect(d).toBeLessThanOrEqual(exp);
      }
    }
  });
});

describe('sleep', () => {
  it('resolves after the given time', async () => {
    vi.useFakeTimers();
    let done = false;
    const p = sleep(1000).then(() => {
      done = true;
    });
    await vi.advanceTimersByTimeAsync(999);
    expect(done).toBe(false);
    await vi.advanceTimersByTimeAsync(1);
    await p;
    expect(done).toBe(true);
  });

  it('treats negative durations as 0', async () => {
    vi.useFakeTimers();
    const p = sleep(-50);
    await vi.advanceTimersByTimeAsync(0);
    await expect(p).resolves.toBeUndefined();
  });

  it('rejects immediately when the signal is already aborted', async () => {
    const ac = new AbortController();
    ac.abort();
    await expect(sleep(10_000, ac.signal)).rejects.toBeInstanceOf(AbortError);
  });

  it('rejects with AbortError when aborted mid-sleep and clears its listener', async () => {
    vi.useFakeTimers();
    const ac = new AbortController();
    const p = sleep(10_000, ac.signal);
    expect(getEventListeners(ac.signal, 'abort')).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(100);
    ac.abort();
    await expect(p).rejects.toBeInstanceOf(AbortError);
    expect(getEventListeners(ac.signal, 'abort')).toHaveLength(0);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('removes its abort listener after resolving (no leak on long-lived signals)', async () => {
    vi.useFakeTimers();
    const ac = new AbortController();
    const ps = [sleep(10, ac.signal), sleep(20, ac.signal), sleep(30, ac.signal)];
    expect(getEventListeners(ac.signal, 'abort')).toHaveLength(3);
    await vi.advanceTimersByTimeAsync(30);
    await Promise.all(ps);
    expect(getEventListeners(ac.signal, 'abort')).toHaveLength(0);
  });
});

describe('retry', () => {
  it('returns the first success without retrying', async () => {
    const fn = vi.fn(async (attempt: number) => `ok${attempt}`);
    await expect(retry(fn, { retries: 3, baseMs: 1, maxMs: 1 })).resolves.toBe('ok1');
    expect(fn).toHaveBeenCalledTimes(1);
  });

  it('retries until success and passes 1-based attempt numbers', async () => {
    const attempts: number[] = [];
    const result = await retry(
      async (attempt) => {
        attempts.push(attempt);
        if (attempt < 3) throw new Error(`fail ${attempt}`);
        return 'done';
      },
      { retries: 2, baseMs: 1, maxMs: 1, jitter: false },
    );
    expect(result).toBe('done');
    expect(attempts).toEqual([1, 2, 3]);
  });

  it('makes retries + 1 attempts and then throws the last error', async () => {
    let calls = 0;
    await expect(
      retry(
        async () => {
          calls++;
          throw new Error(`boom ${calls}`);
        },
        { retries: 3, baseMs: 1, maxMs: 1, jitter: false },
      ),
    ).rejects.toThrow('boom 4');
    expect(calls).toBe(4);
  });

  it('makes exactly one attempt with retries: 0', async () => {
    const fn = vi.fn(async () => {
      throw new Error('once');
    });
    await expect(retry(fn, { retries: 0, baseMs: 1, maxMs: 1 })).rejects.toThrow('once');
    expect(fn).toHaveBeenCalledTimes(1);
  });

  it('stops immediately when shouldRetry returns false', async () => {
    const seen: Array<[string, number]> = [];
    let calls = 0;
    await expect(
      retry(
        async () => {
          calls++;
          throw new Error(calls === 1 ? 'transient' : 'fatal');
        },
        {
          retries: 5,
          baseMs: 1,
          maxMs: 1,
          shouldRetry: (err, attempt) => {
            seen.push([(err as Error).message, attempt]);
            return (err as Error).message === 'transient';
          },
        },
      ),
    ).rejects.toThrow('fatal');
    expect(calls).toBe(2);
    expect(seen).toEqual([
      ['transient', 1],
      ['fatal', 2],
    ]);
  });

  it('uses exponential backoff delays and reports them via onRetry', async () => {
    vi.useFakeTimers();
    const delays: number[] = [];
    let calls = 0;
    const p = retry(
      async () => {
        calls++;
        if (calls <= 3) throw new Error('again');
        return calls;
      },
      { retries: 3, baseMs: 100, maxMs: 250, jitter: false, onRetry: (_e, _a, d) => delays.push(d) },
    );
    await vi.advanceTimersByTimeAsync(100);
    expect(calls).toBe(2);
    await vi.advanceTimersByTimeAsync(200);
    expect(calls).toBe(3);
    await vi.advanceTimersByTimeAsync(250);
    await expect(p).resolves.toBe(4);
    expect(delays).toEqual([100, 200, 250]);
  });

  it('honours delayFor (capped at maxMs) and falls back to backoff when it returns null', async () => {
    vi.useFakeTimers();
    const delays: number[] = [];
    let calls = 0;
    const p = retry(
      async () => {
        calls++;
        if (calls <= 3) throw new Error(String(calls));
        return 'ok';
      },
      {
        retries: 3,
        baseMs: 10,
        maxMs: 5_000,
        jitter: false,
        delayFor: (_err, attempt) => (attempt === 1 ? 1234 : attempt === 2 ? 99_999 : null),
        onRetry: (_e, _a, d) => delays.push(d),
      },
    );
    await vi.advanceTimersByTimeAsync(1234 + 5000 + 40);
    await expect(p).resolves.toBe('ok');
    expect(delays).toEqual([1234, 5000, 40]);
  });

  it('never retries an AbortError thrown by fn', async () => {
    let calls = 0;
    await expect(
      retry(
        async () => {
          calls++;
          throw new AbortError();
        },
        { retries: 5, baseMs: 1, maxMs: 1 },
      ),
    ).rejects.toBeInstanceOf(AbortError);
    expect(calls).toBe(1);
  });

  it('does not call fn when the signal is already aborted', async () => {
    const ac = new AbortController();
    ac.abort();
    const fn = vi.fn(async () => 1);
    await expect(retry(fn, { retries: 3, baseMs: 1, maxMs: 1, signal: ac.signal })).rejects.toBeInstanceOf(AbortError);
    expect(fn).not.toHaveBeenCalled();
  });

  it('aborts during the backoff sleep', async () => {
    vi.useFakeTimers();
    const ac = new AbortController();
    let calls = 0;
    const p = retry(
      async () => {
        calls++;
        throw new Error('down');
      },
      { retries: 10, baseMs: 1_000, maxMs: 1_000, jitter: false, signal: ac.signal },
    );
    const assertion = expect(p).rejects.toBeInstanceOf(AbortError);
    await vi.advanceTimersByTimeAsync(500);
    ac.abort();
    await assertion;
    expect(calls).toBe(1);
  });

  it('rethrows the original error (not AbortError) when aborted while fn was running', async () => {
    const ac = new AbortController();
    let calls = 0;
    await expect(
      retry(
        async () => {
          calls++;
          ac.abort();
          throw new Error('in-flight failure');
        },
        { retries: 5, baseMs: 1, maxMs: 1, signal: ac.signal },
      ),
    ).rejects.toThrow('in-flight failure');
    expect(calls).toBe(1);
  });
});

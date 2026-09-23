/**
 * Retry with exponential backoff + full jitter, and abortable sleep.
 */

export class AbortError extends Error {
  constructor(message = 'Aborted') {
    super(message);
    this.name = 'AbortError';
  }
}

/** Sleeps for `ms`, rejecting with AbortError if the signal fires. The timer never keeps the process alive. */
export function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new AbortError());
      return;
    }
    const onAbort = () => {
      clearTimeout(timer);
      reject(new AbortError());
    };
    const timer = setTimeout(() => {
      signal?.removeEventListener('abort', onAbort);
      resolve();
    }, Math.max(0, ms));
    timer.unref?.();
    signal?.addEventListener('abort', onAbort, { once: true });
  });
}

/**
 * Delay before retry number `attempt` (1-based). Full jitter keeps many clients from retrying in lockstep.
 */
export function backoffDelay(attempt: number, baseMs: number, maxMs: number, factor = 2, jitter = true): number {
  const exp = Math.min(maxMs, baseMs * Math.pow(factor, Math.max(0, attempt - 1)));
  if (!jitter) return exp;
  return Math.round(exp / 2 + Math.random() * (exp / 2));
}

export interface RetryOptions {
  /** Number of retries after the first attempt. */
  retries: number;
  baseMs: number;
  maxMs: number;
  factor?: number;
  jitter?: boolean;
  signal?: AbortSignal;
  /** Return false to stop retrying and rethrow immediately. */
  shouldRetry?: (err: unknown, attempt: number) => boolean;
  /** Override the computed delay (e.g. honour Retry-After). Return null to use the default. */
  delayFor?: (err: unknown, attempt: number) => number | null;
  onRetry?: (err: unknown, attempt: number, delayMs: number) => void;
}

export async function retry<T>(fn: (attempt: number) => Promise<T>, opts: RetryOptions): Promise<T> {
  let attempt = 0;
  for (;;) {
    if (opts.signal?.aborted) throw new AbortError();
    try {
      return await fn(attempt + 1);
    } catch (err) {
      attempt++;
      if (err instanceof AbortError || opts.signal?.aborted) throw err;
      if (attempt > opts.retries) throw err;
      if (opts.shouldRetry && !opts.shouldRetry(err, attempt)) throw err;
      const custom = opts.delayFor?.(err, attempt);
      const delay =
        custom !== null && custom !== undefined
          ? Math.min(custom, opts.maxMs)
          : backoffDelay(attempt, opts.baseMs, opts.maxMs, opts.factor ?? 2, opts.jitter ?? true);
      opts.onRetry?.(err, attempt, delay);
      await sleep(delay, opts.signal);
    }
  }
}

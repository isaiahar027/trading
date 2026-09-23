/**
 * Minimal structured logger. One line per entry, safe for `docker logs`.
 * Repeated identical warnings/errors are throttled so a flapping feed cannot flood the disk.
 */

export type Level = 'debug' | 'info' | 'warn' | 'error';

const ORDER: Record<Level, number> = { debug: 10, info: 20, warn: 30, error: 40 };

let minLevel: Level = (process.env.LOG_LEVEL as Level) in ORDER ? (process.env.LOG_LEVEL as Level) : 'info';

const THROTTLE_MS = 60_000;
const MAX_THROTTLE_KEYS = 500;
const lastLogged = new Map<string, { at: number; suppressed: number }>();

export function setLogLevel(level: Level): void {
  if (level in ORDER) minLevel = level;
}

export function getLogLevel(): Level {
  return minLevel;
}

function fmtMeta(meta: unknown): string {
  if (meta === undefined) return '';
  if (meta instanceof Error) return ` ${meta.name}: ${meta.message}`;
  try {
    const s = JSON.stringify(meta, (_k, v) => (v instanceof Error ? `${v.name}: ${v.message}` : v));
    return s && s !== '{}' ? ` ${s.length > 2000 ? `${s.slice(0, 2000)}…` : s}` : '';
  } catch {
    return ' [unserializable meta]';
  }
}

function write(level: Level, mod: string, msg: string, meta?: unknown): void {
  if (ORDER[level] < ORDER[minLevel]) return;
  const line = `${new Date().toISOString()} ${level.toUpperCase().padEnd(5)} [${mod}] ${msg}${fmtMeta(meta)}`;
  if (level === 'error' || level === 'warn') process.stderr.write(`${line}\n`);
  else process.stdout.write(`${line}\n`);
}

function throttled(level: Level, mod: string, msg: string, meta?: unknown): void {
  const key = `${level}|${mod}|${msg}`;
  const now = Date.now();
  const prev = lastLogged.get(key);
  if (prev && now - prev.at < THROTTLE_MS) {
    prev.suppressed++;
    return;
  }
  const suffix = prev && prev.suppressed > 0 ? ` (repeated ${prev.suppressed}x in last ${Math.round((now - prev.at) / 1000)}s)` : '';
  if (lastLogged.size >= MAX_THROTTLE_KEYS && !prev) {
    const oldest = lastLogged.keys().next().value;
    if (oldest !== undefined) lastLogged.delete(oldest);
  }
  lastLogged.delete(key);
  lastLogged.set(key, { at: now, suppressed: 0 });
  write(level, mod, msg + suffix, meta);
}

export interface Logger {
  debug(msg: string, meta?: unknown): void;
  info(msg: string, meta?: unknown): void;
  warn(msg: string, meta?: unknown): void;
  error(msg: string, meta?: unknown): void;
}

export function createLogger(mod: string): Logger {
  return {
    debug: (msg, meta) => write('debug', mod, msg, meta),
    info: (msg, meta) => write('info', mod, msg, meta),
    warn: (msg, meta) => throttled('warn', mod, msg, meta),
    error: (msg, meta) => throttled('error', mod, msg, meta),
  };
}

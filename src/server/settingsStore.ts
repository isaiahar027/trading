import * as crypto from 'node:crypto';
import * as fs from 'node:fs';
import * as path from 'node:path';
import type { LeagueKey, RuntimeSettings } from '../types';
import { createLogger } from '../util/logger';

/**
 * Runtime settings edited from the dashboard and persisted to DATA_DIR/settings.json.
 *
 *  - load(): lenient. Every valid field in the file overrides the default; bad fields are skipped with a warning.
 *    A missing, unreadable or corrupt file falls back to the defaults. Never throws.
 *  - update(patch): strict. Any unknown key or invalid value rejects the whole patch with a ValidationError
 *    (clear, user-facing message) and nothing changes. Valid patches are written atomically (tmp file + rename).
 */

const log = createLogger('settings');

/** Thrown for invalid user input. HTTP layers map it to 400 with `message` as the error text. */
export class ValidationError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'ValidationError';
  }
}

type NumericKey =
  | 'bankroll'
  | 'kellyMultiplier'
  | 'maxStakePct'
  | 'maxStakeAbs'
  | 'maxDailyExposurePct'
  | 'minEvPrematch'
  | 'minEvLive'
  | 'watchEv';

interface NumericRule {
  min: number;
  max: number;
  label: string;
}

const NUMERIC_RULES: Record<NumericKey, NumericRule> = {
  bankroll: { min: 1, max: 1e8, label: 'Bankroll' },
  kellyMultiplier: { min: 0.01, max: 1, label: 'Kelly multiplier' },
  maxStakePct: { min: 0.001, max: 1, label: 'Max stake % of bankroll' },
  maxStakeAbs: { min: 1, max: 1e7, label: 'Max stake $' },
  maxDailyExposurePct: { min: 0.001, max: 1, label: 'Max daily exposure %' },
  minEvPrematch: { min: 0, max: 0.5, label: 'Min EV (pre-game)' },
  minEvLive: { min: 0, max: 0.5, label: 'Min EV (live)' },
  watchEv: { min: 0, max: 0.5, label: 'Watch EV' },
};

const NUMERIC_KEYS = Object.keys(NUMERIC_RULES) as NumericKey[];

const ALL_KEYS: ReadonlyArray<keyof RuntimeSettings> = [...NUMERIC_KEYS, 'enabledLeagues', 'showArbs'];

/** Hard cap on how much of a settings file we are willing to parse. */
const MAX_FILE_BYTES = 256 * 1024;

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

function copySettings(s: RuntimeSettings): RuntimeSettings {
  return { ...s, enabledLeagues: s.enabledLeagues.slice() };
}

function clamp(n: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, n));
}

function describeValue(v: unknown): string {
  if (typeof v === 'string') return `"${v.length > 40 ? `${v.slice(0, 40)}…` : v}"`;
  if (Array.isArray(v)) return 'a list';
  if (v === null) return 'null';
  if (typeof v === 'object') return 'an object';
  return String(v);
}

export class SettingsStore {
  private readonly file: string;
  private readonly defaults: RuntimeSettings;
  private readonly known: LeagueKey[];
  private readonly knownByUpper: Map<string, LeagueKey>;
  private current: RuntimeSettings;

  constructor(file: string, defaults: RuntimeSettings, knownLeagues: LeagueKey[]) {
    this.file = path.resolve(file);
    this.known = Array.from(new Set(knownLeagues));
    this.knownByUpper = new Map(this.known.map((k) => [k.toUpperCase(), k]));
    this.defaults = this.normalize(copySettings(defaults), 'defaults');
    this.current = copySettings(this.defaults);
  }

  /** Reads the settings file and merges its valid fields over the defaults. Never throws. */
  load(): RuntimeSettings {
    const merged = copySettings(this.defaults);
    let raw: string | null = null;
    try {
      const st = fs.statSync(this.file);
      if (!st.isFile()) {
        log.warn('settings path is not a regular file; using defaults', { file: this.file });
      } else if (st.size > MAX_FILE_BYTES) {
        log.warn('settings file is too large; using defaults', { file: this.file, bytes: st.size });
      } else {
        raw = fs.readFileSync(this.file, 'utf8');
      }
    } catch (err) {
      const code = (err as NodeJS.ErrnoException).code;
      if (code === 'ENOENT') log.info('no saved settings yet; using defaults', { file: this.file });
      else log.warn('could not read settings file; using defaults', { file: this.file, error: (err as Error).message });
    }

    if (raw !== null) {
      let parsed: unknown;
      let ok = true;
      try {
        parsed = JSON.parse(raw);
      } catch (err) {
        ok = false;
        log.warn('settings file is not valid JSON; using defaults', { file: this.file, error: (err as Error).message });
      }
      if (ok && !isPlainObject(parsed)) {
        ok = false;
        log.warn('settings file does not contain a JSON object; using defaults', { file: this.file });
      }
      if (ok && isPlainObject(parsed)) {
        for (const [key, value] of Object.entries(parsed)) {
          if (!(ALL_KEYS as readonly string[]).includes(key)) {
            log.warn('ignoring unknown key in settings file', { key });
            continue;
          }
          try {
            this.applyField(merged, key as keyof RuntimeSettings, value);
          } catch (err) {
            log.warn('ignoring invalid value in settings file', { key, error: (err as Error).message });
          }
        }
      }
    }

    this.current = this.normalize(merged, 'settings file');
    return copySettings(this.current);
  }

  /** Current settings (a copy; mutating it has no effect on the store). */
  get(): RuntimeSettings {
    return copySettings(this.current);
  }

  /**
   * Validates a partial patch strictly and persists the merged result atomically.
   * Throws ValidationError for bad input (nothing is changed) or a plain Error when the file cannot be written.
   */
  update(patch: unknown): RuntimeSettings {
    if (!isPlainObject(patch)) throw new ValidationError('Settings must be a JSON object');
    const keys = Object.keys(patch);
    const unknown = keys.filter((k) => !(ALL_KEYS as readonly string[]).includes(k));
    if (unknown.length > 0) {
      throw new ValidationError(
        `Unknown setting${unknown.length > 1 ? 's' : ''}: ${unknown.map((k) => `"${k.slice(0, 40)}"`).join(', ')}`,
      );
    }

    const next = copySettings(this.current);
    for (const key of keys) this.applyField(next, key as keyof RuntimeSettings, patch[key]);

    const floor = Math.min(next.minEvPrematch, next.minEvLive);
    if (next.watchEv > floor) {
      throw new ValidationError(
        `Watch EV (${next.watchEv}) must not exceed the lower of Min EV pre-game (${next.minEvPrematch}) and Min EV live (${next.minEvLive})`,
      );
    }

    if (keys.length === 0) return copySettings(this.current);

    this.persist(next);
    this.current = next;
    return copySettings(this.current);
  }

  /** Validates one field and writes it into `target`. Throws ValidationError on bad input. */
  private applyField(target: RuntimeSettings, key: keyof RuntimeSettings, value: unknown): void {
    if (key === 'enabledLeagues') {
      target.enabledLeagues = this.validateLeagues(value);
      return;
    }
    if (key === 'showArbs') {
      if (typeof value !== 'boolean') throw new ValidationError(`showArbs must be true or false, got ${describeValue(value)}`);
      target.showArbs = value;
      return;
    }
    const rule = NUMERIC_RULES[key];
    if (typeof value !== 'number' || !Number.isFinite(value)) {
      throw new ValidationError(`${rule.label} (${key}) must be a number, got ${describeValue(value)}`);
    }
    if (value < rule.min || value > rule.max) {
      throw new ValidationError(`${rule.label} (${key}) must be between ${rule.min} and ${rule.max}, got ${value}`);
    }
    target[key] = value;
  }

  private validateLeagues(value: unknown): LeagueKey[] {
    if (!Array.isArray(value)) {
      throw new ValidationError(`enabledLeagues must be a list of league keys, got ${describeValue(value)}`);
    }
    if (value.length === 0) throw new ValidationError('enabledLeagues must contain at least one league');
    if (value.length > 500) throw new ValidationError('enabledLeagues has too many entries');
    const out: LeagueKey[] = [];
    for (const item of value) {
      if (typeof item !== 'string') {
        throw new ValidationError(`enabledLeagues entries must be league keys, got ${describeValue(item)}`);
      }
      const canonical = this.knownByUpper.get(item.trim().toUpperCase());
      if (canonical === undefined) {
        throw new ValidationError(
          `Unknown league ${describeValue(item)}. Known leagues: ${this.known.join(', ')}`,
        );
      }
      if (!out.includes(canonical)) out.push(canonical);
    }
    return out;
  }

  /**
   * Makes a settings object internally consistent without throwing (used for defaults and the loaded file):
   * clamps numbers into range, keeps only known leagues, and lowers watchEv to the smaller min EV.
   */
  private normalize(s: RuntimeSettings, origin: string): RuntimeSettings {
    const out = copySettings(s);
    for (const key of NUMERIC_KEYS) {
      const rule = NUMERIC_RULES[key];
      const v = out[key];
      if (typeof v !== 'number' || !Number.isFinite(v)) {
        log.warn(`${origin}: ${key} is not a number; using the minimum`, { key, min: rule.min });
        out[key] = rule.min;
      } else if (v < rule.min || v > rule.max) {
        const fixed = clamp(v, rule.min, rule.max);
        log.warn(`${origin}: ${key} out of range; clamped`, { key, value: v, clamped: fixed });
        out[key] = fixed;
      }
    }
    const leagues: LeagueKey[] = [];
    for (const l of Array.isArray(out.enabledLeagues) ? out.enabledLeagues : []) {
      const canonical = typeof l === 'string' ? this.knownByUpper.get(l.toUpperCase()) : undefined;
      if (canonical !== undefined && !leagues.includes(canonical)) leagues.push(canonical);
    }
    if (leagues.length === 0 && this.known.length > 0) {
      log.warn(`${origin}: no known enabled leagues; enabling all known leagues`);
      leagues.push(...this.known);
    }
    out.enabledLeagues = leagues;
    if (typeof out.showArbs !== 'boolean') out.showArbs = Boolean(out.showArbs);
    const floor = Math.min(out.minEvPrematch, out.minEvLive);
    if (out.watchEv > floor) {
      log.warn(`${origin}: watchEv above min EV; lowered`, { watchEv: out.watchEv, lowered: floor });
      out.watchEv = floor;
    }
    return out;
  }

  /** Atomic write: temp file in the same directory, fsync, then rename over the target. */
  private persist(s: RuntimeSettings): void {
    const dir = path.dirname(this.file);
    fs.mkdirSync(dir, { recursive: true });
    const tmp = path.join(dir, `.${path.basename(this.file)}.${process.pid}.${crypto.randomBytes(6).toString('hex')}.tmp`);
    const ordered: Record<string, unknown> = {};
    for (const key of ALL_KEYS) ordered[key] = s[key];
    try {
      fs.writeFileSync(tmp, `${JSON.stringify(ordered, null, 2)}\n`, { encoding: 'utf8', mode: 0o600, flush: true });
      fs.renameSync(tmp, this.file);
    } catch (err) {
      try {
        fs.rmSync(tmp, { force: true });
      } catch {
        // best effort cleanup; the original file is untouched either way
      }
      log.error('failed to save settings', { file: this.file, error: (err as Error).message });
      throw new Error(`Could not save settings: ${(err as Error).message}`);
    }
  }
}

import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { SettingsStore, ValidationError } from '../src/server/settingsStore';
import type { RuntimeSettings } from '../src/types';

const DEFAULTS: RuntimeSettings = {
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

const KNOWN = ['NFL', 'NBA', 'MLB', 'NHL', 'EPL'];

let dir: string;
let file: string;

beforeEach(() => {
  dir = fs.mkdtempSync(path.join(os.tmpdir(), 'settings-test-'));
  file = path.join(dir, 'settings.json');
});

afterEach(() => {
  fs.rmSync(dir, { recursive: true, force: true });
});

function store(defaults: RuntimeSettings = DEFAULTS, f: string = file): SettingsStore {
  return new SettingsStore(f, defaults, KNOWN);
}

function writeFile(content: string): void {
  fs.writeFileSync(file, content);
}

function expectValidation(fn: () => unknown, pattern?: RegExp): void {
  let caught: unknown;
  try {
    fn();
  } catch (err) {
    caught = err;
  }
  expect(caught).toBeInstanceOf(ValidationError);
  if (pattern) expect((caught as Error).message).toMatch(pattern);
}

describe('SettingsStore.load', () => {
  it('returns the defaults when there is no file', () => {
    const s = store();
    expect(s.load()).toEqual(DEFAULTS);
    expect(s.get()).toEqual(DEFAULTS);
    expect(fs.existsSync(file)).toBe(false);
  });

  it('merges valid fields from the file over the defaults', () => {
    writeFile(JSON.stringify({ bankroll: 5000, showArbs: false, enabledLeagues: ['EPL', 'MLB'] }));
    const loaded = store().load();
    expect(loaded).toEqual({ ...DEFAULTS, bankroll: 5000, showArbs: false, enabledLeagues: ['EPL', 'MLB'] });
  });

  it('skips invalid and unknown fields in the file but keeps the valid ones', () => {
    writeFile(
      JSON.stringify({
        bankroll: -5,
        kellyMultiplier: 'lots',
        maxStakeAbs: 250,
        enabledLeagues: ['NBA', 'XFL'],
        surprise: 1,
        minEvLive: 0.04,
      }),
    );
    const loaded = store().load();
    expect(loaded).toEqual({ ...DEFAULTS, maxStakeAbs: 250, minEvLive: 0.04 });
  });

  it('falls back to defaults on a corrupt file and never throws', () => {
    writeFile('{"bankroll": 5000,,, oops');
    const s = store();
    expect(() => s.load()).not.toThrow();
    expect(s.get()).toEqual(DEFAULTS);

    writeFile('[1,2,3]');
    expect(store().load()).toEqual(DEFAULTS);

    writeFile('');
    expect(store().load()).toEqual(DEFAULTS);

    writeFile('null');
    expect(store().load()).toEqual(DEFAULTS);
  });

  it('falls back to defaults when the path is a directory', () => {
    fs.mkdirSync(file);
    expect(store().load()).toEqual(DEFAULTS);
  });

  it('lowers watchEv when the merged file values would put it above the min EV', () => {
    writeFile(JSON.stringify({ minEvPrematch: 0.001 }));
    const loaded = store().load();
    expect(loaded.minEvPrematch).toBe(0.001);
    expect(loaded.watchEv).toBe(0.001);
  });

  it('normalizes inconsistent defaults instead of failing', () => {
    const s = store({ ...DEFAULTS, watchEv: 0.05, minEvPrematch: 0.9, enabledLeagues: ['nba', 'XFL', 'NBA'] });
    const got = s.get();
    expect(got.minEvPrematch).toBe(0.5);
    expect(got.watchEv).toBe(0.03);
    expect(got.enabledLeagues).toEqual(['NBA']);
    // A later unrelated patch is still accepted.
    expect(s.update({ bankroll: 2000 }).bankroll).toBe(2000);
  });
});

describe('SettingsStore.get', () => {
  it('returns a copy that cannot mutate the store', () => {
    const s = store();
    s.load();
    const a = s.get();
    a.bankroll = 1;
    a.enabledLeagues.push('EPL');
    expect(s.get()).toEqual(DEFAULTS);
  });

  it('does not mutate the defaults object passed in', () => {
    const defaults = { ...DEFAULTS, enabledLeagues: DEFAULTS.enabledLeagues.slice() };
    const s = store(defaults);
    s.load();
    s.update({ enabledLeagues: ['MLB'], bankroll: 42 });
    expect(defaults).toEqual(DEFAULTS);
  });
});

describe('SettingsStore.update', () => {
  it('applies a partial patch, persists it, and survives a reload', () => {
    const s = store();
    s.load();
    const next = s.update({ bankroll: 2500, kellyMultiplier: 0.5, enabledLeagues: ['NHL'] });
    expect(next).toEqual({ ...DEFAULTS, bankroll: 2500, kellyMultiplier: 0.5, enabledLeagues: ['NHL'] });
    expect(s.get()).toEqual(next);

    const onDisk = JSON.parse(fs.readFileSync(file, 'utf8')) as RuntimeSettings;
    expect(onDisk).toEqual(next);
    expect(store().load()).toEqual(next);
  });

  it('writes atomically and leaves no temp files behind', () => {
    const s = store();
    s.load();
    s.update({ bankroll: 1234 });
    s.update({ showArbs: false });
    expect(fs.readdirSync(dir)).toEqual(['settings.json']);
    expect(JSON.parse(fs.readFileSync(file, 'utf8'))).toMatchObject({ bankroll: 1234, showArbs: false });
  });

  it('creates the parent directory when needed', () => {
    const nested = path.join(dir, 'a', 'b', 'settings.json');
    const s = store(DEFAULTS, nested);
    s.load();
    s.update({ bankroll: 10 });
    expect(JSON.parse(fs.readFileSync(nested, 'utf8')).bankroll).toBe(10);
  });

  it('throws a plain Error (not ValidationError) and keeps state when the file cannot be written', () => {
    fs.mkdirSync(file);
    fs.writeFileSync(path.join(file, 'blocker'), 'x');
    const s = store();
    s.load();
    let caught: unknown;
    try {
      s.update({ bankroll: 777 });
    } catch (err) {
      caught = err;
    }
    expect(caught).toBeInstanceOf(Error);
    expect(caught).not.toBeInstanceOf(ValidationError);
    expect(s.get().bankroll).toBe(1000);
    expect(fs.readdirSync(dir)).toEqual(['settings.json']);
  });

  it('rejects unknown keys and changes nothing', () => {
    const s = store();
    s.load();
    expectValidation(() => s.update({ bankroll: 2000, autoBet: true }), /Unknown setting.*autoBet/);
    expect(s.get()).toEqual(DEFAULTS);
    expect(fs.existsSync(file)).toBe(false);
  });

  it('rejects a __proto__ key', () => {
    const s = store();
    s.load();
    expectValidation(() => s.update(JSON.parse('{"__proto__": {"bankroll": 5}}')), /Unknown setting/);
    expect(s.get()).toEqual(DEFAULTS);
  });

  it('rejects non-object patches', () => {
    const s = store();
    s.load();
    for (const bad of [null, undefined, 5, 'bankroll=5', [], [{ bankroll: 5 }]]) {
      expectValidation(() => s.update(bad), /JSON object/);
    }
  });

  it.each([
    ['bankroll', 0],
    ['bankroll', 1e8 + 1],
    ['bankroll', '1000'],
    ['bankroll', Number.NaN],
    ['bankroll', Number.POSITIVE_INFINITY],
    ['kellyMultiplier', 0.005],
    ['kellyMultiplier', 1.5],
    ['maxStakePct', 0],
    ['maxStakePct', 1.01],
    ['maxStakeAbs', 0.5],
    ['maxStakeAbs', 1e7 + 1],
    ['maxDailyExposurePct', 0.0005],
    ['maxDailyExposurePct', 2],
    ['minEvPrematch', -0.01],
    ['minEvPrematch', 0.51],
    ['minEvLive', -1],
    ['minEvLive', 0.6],
    ['watchEv', -0.001],
    ['watchEv', null],
    ['showArbs', 'true'],
    ['showArbs', 1],
    ['enabledLeagues', []],
    ['enabledLeagues', 'NBA'],
    ['enabledLeagues', ['NBA', 'XFL']],
    ['enabledLeagues', ['NBA', 7]],
  ])('rejects %s = %j', (key, value) => {
    const s = store();
    s.load();
    expectValidation(() => s.update({ [key]: value }), new RegExp(key === 'enabledLeagues' ? 'enabledLeagues|league' : key));
    expect(s.get()).toEqual(DEFAULTS);
  });

  it('accepts the inclusive range boundaries', () => {
    const s = store();
    s.load();
    const next = s.update({
      bankroll: 1e8,
      kellyMultiplier: 0.01,
      maxStakePct: 1,
      maxStakeAbs: 1,
      maxDailyExposurePct: 0.001,
      minEvPrematch: 0.5,
      minEvLive: 0,
      watchEv: 0,
    });
    expect(next).toMatchObject({ bankroll: 1e8, kellyMultiplier: 0.01, maxStakePct: 1, maxStakeAbs: 1, minEvLive: 0, watchEv: 0 });
  });

  it('enforces watchEv <= min(minEvPrematch, minEvLive) against the merged result', () => {
    const s = store();
    s.load();
    expectValidation(() => s.update({ watchEv: 0.021 }), /Watch EV/);
    expectValidation(() => s.update({ minEvPrematch: 0.001 }), /Watch EV/);
    expectValidation(() => s.update({ minEvLive: 0.004 }), /Watch EV/);
    expect(s.get()).toEqual(DEFAULTS);

    expect(s.update({ watchEv: 0.02 }).watchEv).toBe(0.02);
    const both = s.update({ minEvLive: 0.001, watchEv: 0.001 });
    expect(both).toMatchObject({ minEvLive: 0.001, watchEv: 0.001, minEvPrematch: 0.02 });
  });

  it('maps league keys case-insensitively to the known keys and de-duplicates them', () => {
    const s = store();
    s.load();
    expect(s.update({ enabledLeagues: ['epl', 'NBA', 'nba'] }).enabledLeagues).toEqual(['EPL', 'NBA']);
  });

  it('treats an empty patch as a no-op without writing', () => {
    const s = store();
    s.load();
    expect(s.update({})).toEqual(DEFAULTS);
    expect(fs.existsSync(file)).toBe(false);
  });

  it('reports every unknown key in the message', () => {
    const s = store();
    s.load();
    expectValidation(() => s.update({ foo: 1, bar: 2 }), /"foo".*"bar"/);
  });
});

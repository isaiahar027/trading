import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { BetJournal, UnknownBetError } from '../src/server/betJournal';
import type { PlaceBetInput } from '../src/server/betJournal';
import { ValidationError } from '../src/server/settingsStore';
import type { BetRecord } from '../src/types';

let dir: string;
let file: string;
let clock: number;
const now = (): number => clock;

beforeEach(() => {
  dir = fs.mkdtempSync(path.join(os.tmpdir(), 'journal-test-'));
  file = path.join(dir, 'bets.jsonl');
  clock = Date.UTC(2026, 8, 23, 18, 0, 0);
});

afterEach(() => {
  fs.rmSync(dir, { recursive: true, force: true });
});

function input(overrides: Partial<PlaceBetInput> = {}): PlaceBetInput {
  return {
    opportunityId: 'odds-api:e1|ev|spread|home|-3.5',
    eventId: 'odds-api:e1',
    league: 'NBA',
    eventName: 'Knicks @ Celtics',
    startTime: Date.UTC(2026, 8, 24, 0, 0, 0),
    pick: 'Celtics -3.5',
    kind: 'spread',
    side: 'home',
    line: -3.5,
    wasLive: false,
    americanTaken: 110,
    stake: 50,
    fairProbAtPlace: 0.5,
    ...overrides,
  };
}

function journal(opts: { maxInMemory?: number } = {}): BetJournal {
  return new BetJournal(file, { now, ...opts }).load();
}

function fileLines(): string[] {
  return fs
    .readFileSync(file, 'utf8')
    .split('\n')
    .filter((l) => l.trim() !== '');
}

function expectThrows(fn: () => unknown, cls: new (...args: never[]) => Error, pattern?: RegExp): void {
  let caught: unknown;
  try {
    fn();
  } catch (err) {
    caught = err;
  }
  expect(caught).toBeInstanceOf(cls);
  if (pattern) expect((caught as Error).message).toMatch(pattern);
}

describe('BetJournal.place', () => {
  it('records the bet with derived numbers and appends one line synchronously', () => {
    const j = journal();
    const rec = j.place(input({ notes: '  quick one  ' }));
    expect(rec.id).toMatch(/^[0-9a-f-]{36}$/);
    expect(rec).toMatchObject({
      placedAt: clock,
      americanTaken: 110,
      stake: 50,
      result: 'pending',
      settledAt: null,
      profit: null,
      closingFairProb: null,
      notes: 'quick one',
      pick: 'Celtics -3.5',
      line: -3.5,
    });
    expect(rec.decimalTaken).toBeCloseTo(2.1, 12);
    expect(rec.evPctAtPlace).toBeCloseTo(0.05, 12);

    const lines = fileLines();
    expect(lines).toHaveLength(1);
    expect(JSON.parse(lines[0])).toEqual(rec);
  });

  it('handles negative American odds', () => {
    const rec = journal().place(input({ americanTaken: -120, fairProbAtPlace: 0.56 }));
    expect(rec.decimalTaken).toBeCloseTo(1 + 100 / 120, 12);
    expect(rec.evPctAtPlace).toBeCloseTo(0.56 * (1 + 100 / 120) - 1, 12);
  });

  it('gives every bet a unique id', () => {
    const j = journal();
    const ids = new Set(Array.from({ length: 20 }, () => j.place(input()).id));
    expect(ids.size).toBe(20);
  });

  it.each([
    ['stake 0', { stake: 0 }, /Stake/],
    ['negative stake', { stake: -5 }, /Stake/],
    ['NaN stake', { stake: Number.NaN }, /Stake/],
    ['infinite stake', { stake: Number.POSITIVE_INFINITY }, /Stake/],
    ['|american| < 100', { americanTaken: 99 }, /American/],
    ['american -50', { americanTaken: -50 }, /American/],
    ['NaN american', { americanTaken: Number.NaN }, /American/],
    ['fair prob 0', { fairProbAtPlace: 0 }, /probability/],
    ['fair prob 1', { fairProbAtPlace: 1 }, /probability/],
    ['fair prob 1.2', { fairProbAtPlace: 1.2 }, /probability/],
    ['bad kind', { kind: 'props' as never }, /kind/],
    ['bad side', { side: 'left' as never }, /Side/],
    ['bad line', { line: Number.NaN }, /Line/],
    ['empty pick', { pick: '   ' }, /pick/],
    ['non-string event', { eventId: 5 as never }, /eventId/],
    ['huge notes', { notes: 'x'.repeat(1001) }, /notes/],
  ])('rejects %s with a ValidationError and writes nothing', (_label, patch, pattern) => {
    const j = journal();
    expectThrows(() => j.place(input(patch as Partial<PlaceBetInput>)), ValidationError, pattern);
    expect(j.list()).toHaveLength(0);
    expect(fs.existsSync(file)).toBe(false);
  });
});

describe('BetJournal.settle', () => {
  it.each([
    [110, 50, 55],
    [150, 100, 150],
    [-110, 110, 100],
    [-120, 60, 50],
    [-250, 25, 10],
    [100, 10, 10],
  ])('won at %d with stake %d profits %d', (american, stake, profit) => {
    const j = journal();
    const b = j.place(input({ americanTaken: american, stake }));
    clock += 3_600_000;
    const s = j.settle(b.id, 'won');
    expect(s.result).toBe('won');
    expect(s.settledAt).toBe(clock);
    expect(s.profit).toBeCloseTo(profit, 2);
  });

  it('rounds winnings to cents', () => {
    const j = journal();
    const b = j.place(input({ americanTaken: -110, stake: 100 }));
    expect(j.settle(b.id, 'won').profit).toBe(90.91);
  });

  it('lost costs the stake, push and void return zero', () => {
    const j = journal();
    const a = j.place(input({ stake: 40, americanTaken: -150 }));
    const b = j.place(input({ stake: 30, americanTaken: 200 }));
    const c = j.place(input({ stake: 20 }));
    expect(j.settle(a.id, 'lost').profit).toBe(-40);
    expect(j.settle(b.id, 'push').profit).toBe(0);
    expect(j.settle(c.id, 'void').profit).toBe(0);
  });

  it('can re-settle a bet to correct a mistake (last line wins on reload)', () => {
    const j = journal();
    const b = j.place(input({ stake: 10, americanTaken: 200 }));
    j.settle(b.id, 'lost');
    j.settle(b.id, 'won');
    expect(j.get(b.id)?.profit).toBe(20);
    expect(fileLines()).toHaveLength(3);
    expect(journal().get(b.id)).toMatchObject({ result: 'won', profit: 20 });
  });

  it('throws for an unknown id and for an invalid result', () => {
    const j = journal();
    const b = j.place(input());
    expectThrows(() => j.settle('nope', 'won'), UnknownBetError, /Unknown bet/);
    expectThrows(() => j.settle(b.id, 'pending' as never), ValidationError, /Result/);
    expectThrows(() => j.settle(b.id, 'maybe' as never), ValidationError);
    expect(j.get(b.id)?.result).toBe('pending');
  });
});

describe('BetJournal.updateClosing', () => {
  it('stores the closing fair probability and persists it', () => {
    const j = journal();
    const b = j.place(input());
    const u = j.updateClosing(b.id, 0.52);
    expect(u.closingFairProb).toBe(0.52);
    expect(journal().get(b.id)?.closingFairProb).toBe(0.52);
  });

  it('validates the probability and the id', () => {
    const j = journal();
    const b = j.place(input());
    expectThrows(() => j.updateClosing(b.id, 0), ValidationError);
    expectThrows(() => j.updateClosing(b.id, 1), ValidationError);
    expectThrows(() => j.updateClosing(b.id, Number.NaN), ValidationError);
    expectThrows(() => j.updateClosing('missing', 0.5), Error, /Unknown bet/);
  });
});

describe('BetJournal.load', () => {
  it('starts empty when the file does not exist', () => {
    const j = journal();
    expect(j.list()).toEqual([]);
    expect(j.summary(clock).totalBets).toBe(0);
  });

  it('reloads the latest state of every bet (last line per id wins)', () => {
    const j = journal();
    const a = j.place(input({ stake: 10 }));
    clock += 1000;
    const b = j.place(input({ stake: 20 }));
    j.settle(a.id, 'won');
    j.updateClosing(b.id, 0.55);
    j.settle(b.id, 'lost');

    const r = journal();
    expect(r.get(a.id)).toEqual(j.get(a.id));
    expect(r.get(b.id)).toEqual(j.get(b.id));
    expect(r.get(b.id)).toMatchObject({ result: 'lost', closingFairProb: 0.55, profit: -20 });
    expect(r.list().map((x) => x.id)).toEqual([b.id, a.id]);
  });

  it('skips corrupt lines and keeps the valid ones', () => {
    const j = journal();
    const a = j.place(input({ stake: 11 }));
    const b = j.place(input({ stake: 12 }));
    const valid = fs.readFileSync(file, 'utf8');
    const incomplete = { ...JSON.parse(fileLines()[0]), stake: 'lots' };
    fs.writeFileSync(
      file,
      [
        'not json at all',
        valid.trim().split('\n')[0],
        '{"id": "x", "stake": 5}',
        JSON.stringify(incomplete),
        '[1,2,3]',
        '',
        '   ',
        valid.trim().split('\n')[1],
        '{"id":"trunc',
      ].join('\n'),
    );
    const r = journal();
    expect(r.list()).toHaveLength(2);
    expect(r.get(a.id)?.stake).toBe(11);
    expect(r.get(b.id)?.stake).toBe(12);
  });

  it('terminates a truncated last line so the next append lands on its own line', () => {
    const j = journal();
    const a = j.place(input());
    fs.appendFileSync(file, '{"id":"half-written",');
    const r = journal();
    const b = r.place(input({ stake: 7 }));
    const again = journal();
    expect(again.get(a.id)).toBeDefined();
    expect(again.get(b.id)?.stake).toBe(7);
  });

  it('skips a single absurdly long line without buffering it all', () => {
    const j = journal();
    const a = j.place(input());
    fs.appendFileSync(file, `${'x'.repeat(200_000)}\n`);
    const b = new BetJournal(file, { now }).load().place(input({ stake: 9 }));
    const r = journal();
    expect(r.get(a.id)).toBeDefined();
    expect(r.get(b.id)?.stake).toBe(9);
  });

  it('compacts when lines exceed 2 × bets + 50, keeping the latest state', () => {
    const j = journal();
    const a = j.place(input({ stake: 10, americanTaken: 200 }));
    const b = j.place(input({ stake: 20 }));
    for (let i = 0; i < 26; i++) j.settle(a.id, i % 2 === 0 ? 'lost' : 'won');
    for (let i = 0; i < 26; i++) j.updateClosing(b.id, 0.4 + i / 100);
    // 2 + 26 + 26 = 54 lines = 2 × 2 + 50 -> not above the threshold yet
    expect(fileLines()).toHaveLength(54);
    journal();
    expect(fileLines()).toHaveLength(54);

    j.settle(a.id, 'won');
    expect(fileLines()).toHaveLength(55);
    const r = journal();
    const lines = fileLines();
    expect(lines).toHaveLength(2);
    expect(lines.map((l) => (JSON.parse(l) as BetRecord).id)).toEqual([a.id, b.id]);
    expect(r.get(a.id)).toMatchObject({ result: 'won', profit: 20 });
    expect(r.get(b.id)?.closingFairProb).toBeCloseTo(0.65, 12);
    expect(fs.existsSync(`${file}.bak`)).toBe(false);
    expect(fs.readdirSync(dir).filter((f) => f.endsWith('.tmp'))).toEqual([]);

    // Appends keep working after compaction.
    const c = r.place(input({ stake: 5 }));
    expect(journal().get(c.id)?.stake).toBe(5);
  });

  it('compaction drops corrupt lines but keeps a .bak copy of the original', () => {
    const j = journal();
    const a = j.place(input());
    fs.appendFileSync(file, `${Array.from({ length: 60 }, (_, i) => `garbage ${i}`).join('\n')}\n`);
    const r = journal();
    expect(r.get(a.id)).toBeDefined();
    expect(fileLines()).toHaveLength(1);
    expect(fs.readFileSync(`${file}.bak`, 'utf8')).toContain('garbage 59');
  });
});

describe('BetJournal queries', () => {
  it('list is newest first with a limit', () => {
    const j = journal();
    const ids: string[] = [];
    for (let i = 0; i < 5; i++) {
      ids.push(j.place(input({ stake: i + 1 })).id);
      clock += 60_000;
    }
    expect(j.list().map((b) => b.id)).toEqual(ids.slice().reverse());
    expect(j.list(2).map((b) => b.id)).toEqual([ids[4], ids[3]]);
    expect(j.list(0)).toEqual([]);
    expect(j.list(Number.NaN)).toHaveLength(5);
  });

  it('get/list return copies', () => {
    const j = journal();
    const b = j.place(input());
    const got = j.get(b.id);
    if (!got) throw new Error('missing');
    got.stake = 999;
    j.list()[0].result = 'won';
    expect(j.get(b.id)).toMatchObject({ stake: 50, result: 'pending' });
  });

  it('pendingWithoutClosing only lists pending bets without a closing probability', () => {
    const j = journal();
    const a = j.place(input());
    clock += 1;
    const b = j.place(input());
    clock += 1;
    const c = j.place(input());
    clock += 1;
    const d = j.place(input());
    j.updateClosing(b.id, 0.5);
    j.settle(c.id, 'won');
    expect(j.pendingWithoutClosing().map((x) => x.id)).toEqual([a.id, d.id]);
  });

  it('stakedBetween is half-open and excludes void bets', () => {
    const j = journal();
    const t0 = clock;
    j.place(input({ stake: 10 }));
    clock = t0 + 1000;
    const v = j.place(input({ stake: 20 }));
    clock = t0 + 2000;
    j.place(input({ stake: 30 }));
    j.settle(v.id, 'void');
    expect(j.stakedBetween(t0, t0 + 2000)).toBe(10);
    expect(j.stakedBetween(t0, t0 + 2001)).toBe(40);
    expect(j.stakedBetween(t0 + 1, t0 + 3000)).toBe(30);
  });

  it('stakedToday follows the local calendar day across midnight', () => {
    const j = journal();
    const lateDay1 = new Date(2026, 8, 23, 23, 30, 0).getTime();
    const earlyDay2 = new Date(2026, 8, 24, 0, 30, 0).getTime();
    const day2Noon = new Date(2026, 8, 24, 12, 0, 0).getTime();
    const midnight = new Date(2026, 8, 24, 0, 0, 0).getTime();

    clock = lateDay1;
    j.place(input({ stake: 25 }));
    clock = earlyDay2;
    j.place(input({ stake: 40 }));
    const lost = j.place(input({ stake: 15 }));
    j.settle(lost.id, 'lost');
    const voided = j.place(input({ stake: 100 }));
    j.settle(voided.id, 'void');

    expect(j.stakedToday(new Date(2026, 8, 23, 23, 59, 59).getTime())).toBe(25);
    expect(j.stakedToday(midnight - 1)).toBe(25);
    expect(j.stakedToday(midnight)).toBe(55);
    expect(j.stakedToday(day2Noon)).toBe(55);
    expect(j.stakedToday(new Date(2026, 8, 25, 0, 0, 1).getTime())).toBe(0);
    expect(j.summary(day2Noon).stakedToday).toBe(55);
  });

  it('stakedToday covers a full 25-hour local day at a DST change', () => {
    const prevTz = process.env.TZ;
    process.env.TZ = 'America/New_York';
    try {
      const j = journal();
      // 2026-11-01 in New York: clocks fall back at 02:00, so the day is 25 hours long.
      const dayStart = new Date(2026, 10, 1, 0, 0, 0).getTime();
      const nextDay = new Date(2026, 10, 2, 0, 0, 0).getTime();
      expect(nextDay - dayStart).toBe(25 * 3_600_000);
      clock = nextDay - 30 * 60_000; // 23:30 local, 24.5 h after midnight
      j.place(input({ stake: 12 }));
      clock = nextDay + 60_000;
      j.place(input({ stake: 8 }));
      expect(j.stakedToday(dayStart + 3_600_000)).toBe(12);
      expect(j.stakedToday(nextDay)).toBe(8);
    } finally {
      if (prevTz === undefined) delete process.env.TZ;
      else process.env.TZ = prevTz;
    }
  });
});

describe('BetJournal.summary', () => {
  it('is empty-safe', () => {
    expect(journal().summary(clock)).toEqual({
      totalBets: 0,
      pending: 0,
      staked: 0,
      profit: 0,
      roiPct: null,
      avgEvPct: null,
      avgClvPct: null,
      stakedToday: 0,
    });
  });

  it('computes ROI, average EV and average CLV', () => {
    const j = journal();
    // won +150, stake 100 -> +150
    const a = j.place(input({ americanTaken: 150, stake: 100, fairProbAtPlace: 0.42 }));
    // lost -110, stake 110 -> -110
    const b = j.place(input({ americanTaken: -110, stake: 110, fairProbAtPlace: 0.55 }));
    // push +100 stake 50 -> 0
    const c = j.place(input({ americanTaken: 100, stake: 50, fairProbAtPlace: 0.52 }));
    // void stake 30 -> excluded from staked and ROI denominator
    const d = j.place(input({ americanTaken: 200, stake: 30, fairProbAtPlace: 0.35 }));
    // pending stake 20
    const e = j.place(input({ americanTaken: -105, stake: 20, fairProbAtPlace: 0.53 }));
    j.settle(a.id, 'won');
    j.settle(b.id, 'lost');
    j.settle(c.id, 'push');
    j.settle(d.id, 'void');
    j.updateClosing(a.id, 0.44);
    j.updateClosing(e.id, 0.5);

    const s = j.summary(clock);
    expect(s.totalBets).toBe(5);
    expect(s.pending).toBe(1);
    expect(s.staked).toBe(280);
    expect(s.profit).toBe(40);
    expect(s.roiPct).toBeCloseTo(40 / 260, 12);
    const evs = [0.42 * 2.5 - 1, 0.55 * (1 + 100 / 110) - 1, 0.52 * 2 - 1, 0.35 * 3 - 1, 0.53 * (1 + 100 / 105) - 1];
    expect(s.avgEvPct).toBeCloseTo(evs.reduce((x, y) => x + y, 0) / 5, 12);
    const clvs = [0.44 * 2.5 - 1, 0.5 * (1 + 100 / 105) - 1];
    expect(s.avgClvPct).toBeCloseTo((clvs[0] + clvs[1]) / 2, 12);
    expect(s.stakedToday).toBe(280);
  });

  it('roiPct is null when nothing is settled (only pending or void)', () => {
    const j = journal();
    j.place(input());
    const v = j.place(input());
    j.settle(v.id, 'void');
    expect(j.summary(clock).roiPct).toBeNull();
    expect(j.summary(clock).avgClvPct).toBeNull();
  });
});

describe('BetJournal memory cap', () => {
  it('evicts the oldest settled bets from memory, keeps them on disk and keeps the summary exact', () => {
    const j = journal({ maxInMemory: 10 });
    const ids: string[] = [];
    for (let i = 0; i < 10; i++) {
      ids.push(j.place(input({ stake: 10, americanTaken: 100 })).id);
      clock += 1000;
    }
    for (let i = 0; i < 6; i++) j.settle(ids[i], i % 2 === 0 ? 'won' : 'lost');
    j.updateClosing(ids[0], 0.55);
    const before = j.summary(clock);

    ids.push(j.place(input({ stake: 10, americanTaken: 100 })).id);
    expect(j.size()).toBeLessThanOrEqual(10);
    // Oldest settled bets went first; pending ones are never evicted.
    expect(j.get(ids[0])).toBeUndefined();
    for (let i = 6; i < 11; i++) expect(j.get(ids[i])).toBeDefined();

    const after = j.summary(clock);
    expect(after.totalBets).toBe(11);
    expect(after.staked).toBe(before.staked + 10);
    expect(after.profit).toBe(before.profit);
    expect(after.roiPct).toBe(before.roiPct);
    expect(after.avgClvPct).toBe(before.avgClvPct);
    expect(after.pending).toBe(before.pending + 1);

    // Still on disk: a journal with a bigger cap sees everything.
    const full = journal();
    expect(full.list(100)).toHaveLength(11);
    expect(full.get(ids[0])?.closingFairProb).toBe(0.55);

    // Evicted bets are unknown to settle/updateClosing.
    expect(() => j.settle(ids[0], 'won')).toThrow(/Unknown bet/);

    // load() applies the same cap.
    const capped = journal({ maxInMemory: 10 });
    expect(capped.size()).toBeLessThanOrEqual(10);
    expect(capped.summary(clock)).toEqual(after);
  });
});

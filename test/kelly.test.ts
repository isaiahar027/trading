import { describe, expect, it } from 'vitest';
import { fullKelly, suggestStake } from '../src/engine/kelly';
import type { StakeInput } from '../src/engine/kelly';

/** prob 0.55 at even money: full Kelly 0.1; quarter Kelly on $1000 = $25. */
function base(overrides: Partial<StakeInput> = {}): StakeInput {
  return {
    prob: 0.55,
    decimal: 2.0,
    bankroll: 1000,
    kellyMultiplier: 0.25,
    confidence: 1,
    maxStakePct: 0.05,
    maxStakeAbs: 500,
    remainingDailyExposure: 1000,
    ...overrides,
  };
}

describe('fullKelly', () => {
  it('is (p·d − 1)/(d − 1)', () => {
    expect(fullKelly(0.55, 2.0)).toBeCloseTo(0.1, 12);
    expect(fullKelly(0.5, 2.1)).toBeCloseTo(0.05 / 1.1, 12);
    expect(fullKelly(0.25, 5)).toBeCloseTo(0.0625, 12);
    expect(fullKelly(0.6, 1 + 100 / 110)).toBeCloseTo((0.6 * (1 + 100 / 110) - 1) / (100 / 110), 12);
    expect(fullKelly(1, 3)).toBe(1);
  });

  it('is 0 with no edge or a negative edge', () => {
    expect(fullKelly(0.5, 2)).toBe(0);
    expect(fullKelly(0.4, 2)).toBe(0);
    expect(fullKelly(0, 10)).toBe(0);
  });

  it('is 0 for invalid inputs', () => {
    expect(fullKelly(Number.NaN, 2)).toBe(0);
    expect(fullKelly(0.5, Number.NaN)).toBe(0);
    expect(fullKelly(0.5, 1)).toBe(0);
    expect(fullKelly(0.5, 0.5)).toBe(0);
    expect(fullKelly(-0.1, 3)).toBe(0);
    expect(fullKelly(1.2, 2)).toBe(0);
    expect(fullKelly(0.5, Number.POSITIVE_INFINITY)).toBe(0);
    expect(fullKelly(Number.POSITIVE_INFINITY, 2)).toBe(0);
  });
});

describe('suggestStake', () => {
  it('applies bankroll × fullKelly × multiplier × confidence', () => {
    const r = suggestStake(base());
    expect(r).toEqual({ fraction: 0.025, stake: 25, cappedBy: 'none', fullKelly: r.fullKelly });
    expect(r.fullKelly).toBeCloseTo(0.1, 12);
  });

  it('scales by confidence and clamps it to [0, 1]', () => {
    expect(suggestStake(base({ confidence: 0.5 })).stake).toBe(12); // 12.5 floored
    expect(suggestStake(base({ confidence: 0.8 })).stake).toBe(20);
    expect(suggestStake(base({ confidence: 2 })).stake).toBe(25);
    const zero = suggestStake(base({ confidence: -1 }));
    expect(zero.stake).toBe(0);
    expect(zero.cappedBy).toBe('minimum');
  });

  it('caps at bankroll × maxStakePct', () => {
    const r = suggestStake(base({ maxStakePct: 0.01 }));
    expect(r.stake).toBe(10);
    expect(r.cappedBy).toBe('maxStakePct');
    expect(r.fraction).toBeCloseTo(0.01, 12);
  });

  it('caps at maxStakeAbs', () => {
    const r = suggestStake(base({ maxStakeAbs: 7 }));
    expect(r.stake).toBe(7);
    expect(r.cappedBy).toBe('maxStakeAbs');
  });

  it('caps at the remaining daily exposure', () => {
    const r = suggestStake(base({ remainingDailyExposure: 15 }));
    expect(r.stake).toBe(15);
    expect(r.cappedBy).toBe('dailyExposure');
  });

  it('returns stake 0 labelled dailyExposure when the daily exposure is used up', () => {
    for (const remaining of [0, -50, 0.4]) {
      const r = suggestStake(base({ remainingDailyExposure: remaining }));
      expect(r.stake).toBe(0);
      expect(r.fraction).toBe(0);
      expect(r.cappedBy).toBe('dailyExposure');
      expect(r.fullKelly).toBeCloseTo(0.1, 12);
    }
  });

  it('records the tightest cap; ties go to the earlier cap (pct, abs, daily)', () => {
    expect(suggestStake(base({ maxStakePct: 0.02, maxStakeAbs: 15, remainingDailyExposure: 18 })).cappedBy).toBe('maxStakeAbs');
    expect(suggestStake(base({ maxStakePct: 0.02, maxStakeAbs: 25, remainingDailyExposure: 12 })).cappedBy).toBe('dailyExposure');
    expect(suggestStake(base({ maxStakePct: 0.01, maxStakeAbs: 15, remainingDailyExposure: 18 })).cappedBy).toBe('maxStakePct');
    const tie = suggestStake(base({ maxStakePct: 0.01, maxStakeAbs: 10, remainingDailyExposure: 10 }));
    expect(tie.stake).toBe(10);
    expect(tie.cappedBy).toBe('maxStakePct');
  });

  it('floors to whole dollars', () => {
    const r = suggestStake(base({ bankroll: 1036 })); // 25.9
    expect(r.stake).toBe(25);
    expect(r.cappedBy).toBe('none');
    expect(r.fraction).toBeCloseTo(25 / 1036, 12);
  });

  it('does not lose a dollar to float noise (100 × 0.29 = 28.999999999999996)', () => {
    expect(100 * 0.29).toBeLessThan(29);
    const r = suggestStake(base({ prob: 0.9, bankroll: 100, kellyMultiplier: 1, maxStakePct: 0.29, maxStakeAbs: 1e6 }));
    expect(r.stake).toBe(29);
    expect(r.cappedBy).toBe('maxStakePct');
  });

  it('returns 0 labelled minimum when the Kelly amount is under $1', () => {
    const r = suggestStake(base({ bankroll: 30 })); // 0.75
    expect(r).toEqual({ fraction: 0, stake: 0, cappedBy: 'minimum', fullKelly: r.fullKelly });
    expect(r.fullKelly).toBeCloseTo(0.1, 12);
    expect(suggestStake(base({ bankroll: 40 })).stake).toBe(1); // exactly $1 is allowed
  });

  it('returns 0 labelled noEdge when full Kelly is 0', () => {
    for (const prob of [0.5, 0.45, 0]) {
      expect(suggestStake(base({ prob }))).toEqual({ fraction: 0, stake: 0, cappedBy: 'noEdge', fullKelly: 0 });
    }
  });

  it('never suggests a stake for NaN / negative / invalid inputs', () => {
    const invalid: Array<Partial<StakeInput>> = [
      { prob: Number.NaN },
      { decimal: Number.NaN },
      { decimal: 1 },
      { decimal: -3 },
      { prob: -0.5 },
      { prob: 1.5 },
      { bankroll: Number.NaN },
      { bankroll: -100 },
      { bankroll: 0 },
      { bankroll: Number.POSITIVE_INFINITY },
      { kellyMultiplier: Number.NaN },
      { kellyMultiplier: -0.25 },
      { kellyMultiplier: Number.POSITIVE_INFINITY },
      { confidence: Number.NaN },
      { maxStakePct: Number.NaN },
      { maxStakePct: -0.05 },
      { maxStakeAbs: Number.NaN },
      { maxStakeAbs: -10 },
      { remainingDailyExposure: Number.NaN },
      { remainingDailyExposure: -1 },
    ];
    for (const o of invalid) {
      const r = suggestStake(base(o));
      expect(r.stake, JSON.stringify(o, (_k, v) => (typeof v === 'number' && !Number.isFinite(v) ? String(v) : v))).toBe(0);
      expect(r.fraction).toBe(0);
      expect(Number.isFinite(r.fullKelly)).toBe(true);
    }
  });

  it('labels invalid caps as the cap that zeroed the stake', () => {
    expect(suggestStake(base({ maxStakePct: Number.NaN })).cappedBy).toBe('maxStakePct');
    expect(suggestStake(base({ maxStakeAbs: -10 })).cappedBy).toBe('maxStakeAbs');
    expect(suggestStake(base({ remainingDailyExposure: Number.NaN })).cappedBy).toBe('dailyExposure');
  });

  it('treats +Infinity limits as "no limit"', () => {
    const r = suggestStake(base({ maxStakePct: 1, maxStakeAbs: Number.POSITIVE_INFINITY, remainingDailyExposure: Number.POSITIVE_INFINITY }));
    expect(r.stake).toBe(25);
    expect(r.cappedBy).toBe('none');
  });

  it('holds its invariants across many inputs', () => {
    let seed = 42;
    const rand = () => {
      seed = (seed * 1664525 + 1013904223) >>> 0;
      return seed / 4294967296;
    };
    for (let i = 0; i < 2000; i++) {
      const input: StakeInput = {
        prob: 0.05 + rand() * 0.9,
        decimal: 1.05 + rand() * 12,
        bankroll: 10 + rand() * 50_000,
        kellyMultiplier: 0.01 + rand() * 0.99,
        confidence: rand() * 1.2 - 0.1,
        maxStakePct: 0.001 + rand() * 0.2,
        maxStakeAbs: 1 + rand() * 2000,
        remainingDailyExposure: rand() * 3000 - 200,
      };
      const r = suggestStake(input);
      expect(Number.isInteger(r.stake)).toBe(true);
      expect(r.stake).toBeGreaterThanOrEqual(0);
      expect(r.stake).toBeLessThanOrEqual(input.bankroll * input.maxStakePct + 1e-6);
      expect(r.stake).toBeLessThanOrEqual(input.maxStakeAbs + 1e-6);
      expect(r.stake).toBeLessThanOrEqual(Math.max(0, input.remainingDailyExposure) + 1e-6);
      expect(r.fraction).toBeCloseTo(r.stake / input.bankroll, 12);
      expect(r.fullKelly).toBeCloseTo(fullKelly(input.prob, input.decimal), 12);
      if (r.fullKelly === 0) expect(r.cappedBy).toBe('noEdge');
      if (r.cappedBy === 'none') expect(r.stake).toBeGreaterThanOrEqual(1);
    }
  });
});

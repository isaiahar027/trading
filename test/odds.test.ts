import { describe, expect, it } from 'vitest';
import {
  americanToDecimal,
  decimalToAmerican,
  decimalToProb,
  expectedValue,
  formatAmerican,
  parseAmerican,
  parseDecimal,
  probToAmerican,
  probToDecimal,
  roundOdds,
} from '../src/util/odds';

describe('parseAmerican', () => {
  it('parses signed strings and numbers', () => {
    expect(parseAmerican('+120')).toBe(120);
    expect(parseAmerican('120')).toBe(120);
    expect(parseAmerican('-110')).toBe(-110);
    expect(parseAmerican(' -110 ')).toBe(-110);
    expect(parseAmerican('- 110')).toBe(-110);
    expect(parseAmerican('+100.5')).toBe(100.5);
    expect(parseAmerican(-150)).toBe(-150);
    expect(parseAmerican(250)).toBe(250);
  });

  it('accepts the unicode minus and dash variants books display', () => {
    expect(parseAmerican('−150')).toBe(-150); // − MINUS SIGN (DraftKings)
    expect(parseAmerican('–125')).toBe(-125); // – EN DASH
    expect(parseAmerican('—200')).toBe(-200); // — EM DASH
    expect(parseAmerican('‒105')).toBe(-105); // ‒ FIGURE DASH
    expect(parseAmerican('－300')).toBe(-300); // － FULLWIDTH HYPHEN-MINUS
    expect(parseAmerican('﹣115')).toBe(-115); // ﹣ SMALL HYPHEN-MINUS
    // The global regex must not keep state between calls.
    expect(parseAmerican('−150')).toBe(-150);
  });

  it('maps EVEN / EV / EVENS / PK to +100, case-insensitively', () => {
    for (const s of ['EVEN', 'even', 'Even', 'EV', 'ev', 'EVENS', 'PK', 'pk', ' even ']) {
      expect(parseAmerican(s), s).toBe(100);
    }
  });

  it('rejects invalid input', () => {
    for (const s of ['', '   ', 'abc', '+', '-', '−', '99', '-99', '+50', '0', '1e3', '12a', '++110', '+-110', '1.5.5', 'Infinity']) {
      expect(parseAmerican(s), JSON.stringify(s)).toBeNull();
    }
    expect(parseAmerican(null)).toBeNull();
    expect(parseAmerican(undefined)).toBeNull();
    expect(parseAmerican(50)).toBeNull();
    expect(parseAmerican(-99.9)).toBeNull();
    expect(parseAmerican(0)).toBeNull();
    expect(parseAmerican(Number.NaN)).toBeNull();
    expect(parseAmerican(Number.POSITIVE_INFINITY)).toBeNull();
  });
});

describe('parseDecimal', () => {
  it('parses numbers and numeric strings > 1', () => {
    expect(parseDecimal(1.91)).toBe(1.91);
    expect(parseDecimal('1.91')).toBe(1.91);
    expect(parseDecimal(' 2.5 ')).toBe(2.5);
    expect(parseDecimal(1.0001)).toBe(1.0001);
  });

  it('rejects <= 1, non-finite and garbage', () => {
    for (const v of [1, 0.5, 0, -2, '1', 'abc', '', Number.NaN, Number.POSITIVE_INFINITY, 'Infinity', null, undefined]) {
      expect(parseDecimal(v as string | number | null | undefined), String(v)).toBeNull();
    }
  });
});

describe('americanToDecimal / decimalToAmerican', () => {
  it('converts known prices', () => {
    expect(americanToDecimal(100)).toBe(2);
    expect(americanToDecimal(-100)).toBe(2);
    expect(americanToDecimal(150)).toBe(2.5);
    expect(americanToDecimal(-200)).toBe(1.5);
    expect(americanToDecimal(-110)).toBeCloseTo(1.909090909, 9);
    expect(americanToDecimal(-250)).toBeCloseTo(1.4, 12);

    expect(decimalToAmerican(2)).toBe(100);
    expect(decimalToAmerican(2.5)).toBe(150);
    expect(decimalToAmerican(1.5)).toBe(-200);
    expect(decimalToAmerican(1.91)).toBe(-110);
    expect(decimalToAmerican(1.909090909)).toBe(-110);
    expect(decimalToAmerican(11)).toBe(1000);
  });

  it('round-trips every whole American price from ±100 to ±2000 (−100 ≡ +100)', () => {
    for (let a = 100; a <= 2000; a++) {
      expect(decimalToAmerican(americanToDecimal(a))).toBe(a);
      if (a > 100) expect(decimalToAmerican(americanToDecimal(-a))).toBe(-a);
    }
    expect(decimalToAmerican(americanToDecimal(-100))).toBe(100);
  });

  it('round-trips decimal prices within display precision', () => {
    for (let d = 1.01; d < 20; d += 0.01) {
      const back = americanToDecimal(decimalToAmerican(d));
      // American odds are whole numbers, so the round-trip error is bounded by one American point.
      const tol = d >= 2 ? 0.005 + 1e-9 : ((d - 1) * (d - 1)) / 200 + 1e-9;
      expect(Math.abs(back - d), `d=${d}`).toBeLessThanOrEqual(tol * 1.01 + 1e-9);
    }
  });

  it('throws RangeError for invalid prices', () => {
    for (const a of [0, 50, -99, 99.99, Number.NaN, Number.POSITIVE_INFINITY, Number.NEGATIVE_INFINITY]) {
      expect(() => americanToDecimal(a), String(a)).toThrow(RangeError);
    }
    for (const d of [1, 0.5, 0, -2, Number.NaN, Number.POSITIVE_INFINITY]) {
      expect(() => decimalToAmerican(d), String(d)).toThrow(RangeError);
    }
  });
});

describe('probability conversions', () => {
  it('converts between decimal odds and probability', () => {
    expect(decimalToProb(2)).toBe(0.5);
    expect(decimalToProb(4)).toBe(0.25);
    expect(probToDecimal(0.5)).toBe(2);
    expect(probToDecimal(0.25)).toBe(4);
    expect(probToAmerican(0.5)).toBe(100);
    expect(probToAmerican(0.25)).toBe(300);
    expect(probToAmerican(110 / 210)).toBe(-110);
    expect(probToAmerican(2 / 3)).toBe(-200);
  });

  it('throws RangeError outside (0,1) / for decimal <= 1', () => {
    for (const d of [1, 0.9, Number.NaN, Number.POSITIVE_INFINITY]) expect(() => decimalToProb(d)).toThrow(RangeError);
    for (const p of [0, 1, -0.1, 1.1, Number.NaN]) {
      expect(() => probToDecimal(p)).toThrow(RangeError);
      expect(() => probToAmerican(p)).toThrow(RangeError);
    }
  });
});

describe('formatAmerican', () => {
  it('adds a + for positive prices and rounds', () => {
    expect(formatAmerican(120)).toBe('+120');
    expect(formatAmerican(100)).toBe('+100');
    expect(formatAmerican(-150)).toBe('-150');
    expect(formatAmerican(-110.4)).toBe('-110');
    expect(formatAmerican(104.6)).toBe('+105');
  });
});

describe('expectedValue / roundOdds', () => {
  it('computes EV per unit staked', () => {
    expect(expectedValue(0.5, 2.1)).toBeCloseTo(0.05, 12);
    expect(expectedValue(0.5, 1.9)).toBeCloseTo(-0.05, 12);
    expect(expectedValue(0.25, 4)).toBeCloseTo(0, 12);
  });

  it('rounds to 4 decimal places', () => {
    expect(roundOdds(1.909090909)).toBe(1.9091);
    expect(roundOdds(2.00004)).toBe(2);
    expect(roundOdds(2.00005)).toBe(2.0001);
    expect(roundOdds(0.1 + 0.2)).toBe(0.3);
  });
});

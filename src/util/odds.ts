/**
 * Odds conversion helpers. Decimal odds are the internal representation.
 */

const MINUS_CHARS = /[−‒–—﹣－]/g; // DraftKings uses U+2212 "−" in display odds

/**
 * Parses American odds from a number or a display string such as "+120", "−150" (unicode minus),
 * "-110", "EVEN", "EV", "PK". Returns null when the input is not a valid American price.
 */
export function parseAmerican(input: string | number | null | undefined): number | null {
  if (input === null || input === undefined) return null;
  if (typeof input === 'number') {
    return Number.isFinite(input) && Math.abs(input) >= 100 ? input : null;
  }
  const s = input.replace(MINUS_CHARS, '-').replace(/\s+/g, '').toUpperCase();
  if (s === '') return null;
  if (s === 'EVEN' || s === 'EV' || s === 'EVENS' || s === 'PK') return 100;
  if (!/^[+-]?\d+(\.\d+)?$/.test(s)) return null;
  const n = Number(s);
  return Number.isFinite(n) && Math.abs(n) >= 100 ? n : null;
}

/** Parses a decimal odds value that may be a string. Returns null unless finite and > 1. */
export function parseDecimal(input: string | number | null | undefined): number | null {
  if (input === null || input === undefined) return null;
  const n = typeof input === 'number' ? input : Number(String(input).trim());
  return Number.isFinite(n) && n > 1 ? n : null;
}

export function americanToDecimal(american: number): number {
  if (!Number.isFinite(american) || Math.abs(american) < 100) {
    throw new RangeError(`Invalid American odds: ${american}`);
  }
  return american > 0 ? 1 + american / 100 : 1 + 100 / Math.abs(american);
}

/** Converts decimal odds to American, rounded to the nearest whole number (as books display). */
export function decimalToAmerican(decimal: number): number {
  if (!Number.isFinite(decimal) || decimal <= 1) {
    throw new RangeError(`Invalid decimal odds: ${decimal}`);
  }
  if (decimal >= 2) return Math.round((decimal - 1) * 100);
  return Math.round(-100 / (decimal - 1));
}

export function decimalToProb(decimal: number): number {
  if (!Number.isFinite(decimal) || decimal <= 1) throw new RangeError(`Invalid decimal odds: ${decimal}`);
  return 1 / decimal;
}

export function probToDecimal(prob: number): number {
  if (!Number.isFinite(prob) || prob <= 0 || prob >= 1) throw new RangeError(`Invalid probability: ${prob}`);
  return 1 / prob;
}

export function probToAmerican(prob: number): number {
  return decimalToAmerican(probToDecimal(prob));
}

/** "+120" / "-150" / "+100" */
export function formatAmerican(american: number): string {
  const r = Math.round(american);
  return r > 0 ? `+${r}` : `${r}`;
}

/** Expected value per unit staked at `decimal` odds when the true win probability is `prob`. */
export function expectedValue(prob: number, decimal: number): number {
  return prob * decimal - 1;
}

/** Rounds a decimal price to 4 places to avoid float noise when comparing prices. */
export function roundOdds(decimal: number): number {
  return Math.round(decimal * 10000) / 10000;
}

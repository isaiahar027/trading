/**
 * Margin ("vig") removal: turns a book's decimal prices for one market into fair probabilities.
 *
 * Notation: d_i = decimal price of outcome i, q_i = 1/d_i (implied probability), B = Σ q_i (the "booksum").
 * B − 1 is the overround. A market with B <= 1 carries no margin; every method then returns the
 * multiplicative (proportional) normalisation so outputs still sum to 1.
 *
 * - Multiplicative: p_i = q_i / B. Spreads the margin proportionally to each price.
 * - Additive:       p_i = q_i − (B − 1) / n. Spreads the margin equally; can go negative on longshots,
 *                   in which case we fall back to multiplicative.
 * - Power:          p_i = q_i^k with k >= 1 chosen so that Σ q_i^k = 1. Removes relatively more margin from
 *                   longshots (favourite–longshot bias). k is found by bisection.
 * - Shin:           models the margin as protection against a fraction z of insiders. For a given z,
 *                   p_i(z) = (sqrt(z² + 4(1 − z) q_i² / B) − z) / (2(1 − z)); z in (0, 1) is the root of
 *                   Σ p_i(z) = 1, found by bisection. Also favours the favourite over the longshot.
 * - Worst case:     per outcome, the minimum probability over the four methods above. It is deliberately
 *                   NOT renormalised: it is the most conservative estimate for EV on each side.
 */
import type { DevigMethod } from '../config';

/** Convergence tolerance for bisection (relative on the root, absolute on the residual). */
const TOLERANCE = 1e-12;
/** Hard cap on bisection iterations; 200 halvings is far beyond double precision. */
const MAX_BISECTION_ITERATIONS = 200;
/** Hard cap on doublings of the power exponent's upper bracket (k up to 2^65). */
const MAX_BRACKET_DOUBLINGS = 64;

function validate(decimals: number[]): void {
  if (!Array.isArray(decimals) || decimals.length < 2) {
    throw new RangeError(`devig needs at least 2 prices, got ${Array.isArray(decimals) ? decimals.length : typeof decimals}`);
  }
  for (const d of decimals) {
    if (typeof d !== 'number' || !Number.isFinite(d) || d <= 1) {
      throw new RangeError(`Invalid decimal odds for devig: ${String(d)}`);
    }
  }
}

function implied(decimals: number[]): number[] {
  return decimals.map((d) => 1 / d);
}

function sum(xs: number[]): number {
  let s = 0;
  for (const x of xs) s += x;
  return s;
}

/** Divides by the sum so the result sums to 1 up to float rounding. */
function normalise(xs: number[]): number[] {
  const s = sum(xs);
  return xs.map((x) => x / s);
}

/** True when every value is a finite probability strictly inside (0, 1). */
function allOpenUnit(xs: number[]): boolean {
  return xs.every((x) => Number.isFinite(x) && x > 0 && x < 1);
}

/** Σ 1/d − 1. Positive for a normal bookmaker market; <= 0 means no margin (or an arb across books). */
export function overround(decimals: number[]): number {
  validate(decimals);
  return sum(implied(decimals)) - 1;
}

export function devigMultiplicative(decimals: number[]): number[] {
  validate(decimals);
  return normalise(implied(decimals));
}

export function devigAdditive(decimals: number[]): number[] {
  validate(decimals);
  const q = implied(decimals);
  const booksum = sum(q);
  if (booksum <= 1) return normalise(q);
  const shift = (booksum - 1) / q.length;
  const p = q.map((x) => x - shift);
  if (p.some((x) => !(x > 0))) return normalise(q);
  const out = normalise(p);
  return allOpenUnit(out) ? out : normalise(q);
}

/**
 * Power method. f(k) = Σ q_i^k is strictly decreasing in k because every q_i is in (0, 1),
 * f(1) = B > 1 and f(k) → 0, so a unique root k* > 1 exists. We bracket it by doubling, then bisect.
 */
export function devigPower(decimals: number[]): number[] {
  validate(decimals);
  const q = implied(decimals);
  const booksum = sum(q);
  if (booksum <= 1) return normalise(q);

  const f = (k: number): number => {
    let s = 0;
    for (const x of q) s += Math.pow(x, k);
    return s;
  };

  let lo = 1;
  let hi = 2;
  let doublings = 0;
  while (f(hi) > 1) {
    if (++doublings > MAX_BRACKET_DOUBLINGS) return normalise(q);
    lo = hi;
    hi *= 2;
  }

  for (let i = 0; i < MAX_BISECTION_ITERATIONS; i++) {
    const mid = (lo + hi) / 2;
    if (mid <= lo || mid >= hi) break; // bracket collapsed to adjacent doubles
    const residual = f(mid) - 1;
    if (Math.abs(residual) <= TOLERANCE) {
      lo = mid;
      hi = mid;
      break;
    }
    if (residual > 0) lo = mid;
    else hi = mid;
    if (hi - lo <= TOLERANCE * hi) break;
  }

  const k = (lo + hi) / 2;
  const out = normalise(q.map((x) => Math.pow(x, k)));
  // Extreme inputs (a near-certain favourite next to a tiny longshot) can underflow q^k to 0.
  return allOpenUnit(out) ? out : normalise(q);
}

/**
 * Shin's method. We use the algebraically equivalent, cancellation-free form
 *   p_i(z) = 2 c_i / (sqrt(z² + 4(1 − z) c_i) + z),  c_i = q_i² / B,
 * which is finite on the whole closed interval [0, 1]. Its denominator is strictly increasing in z
 * (because c_i < 1), so S(z) = Σ p_i(z) is strictly decreasing with S(0) = sqrt(B) > 1 and
 * S(1) = Σ q_i² / B < 1. The unique root z* in (0, 1) is found by bisection.
 */
export function devigShin(decimals: number[]): number[] {
  validate(decimals);
  const q = implied(decimals);
  const booksum = sum(q);
  if (booksum <= 1) return normalise(q);

  const c = q.map((x) => (x * x) / booksum);
  const probsAt = (z: number): number[] => c.map((ci) => (2 * ci) / (Math.sqrt(z * z + 4 * (1 - z) * ci) + z));

  let lo = 0;
  let hi = 1;
  for (let i = 0; i < MAX_BISECTION_ITERATIONS; i++) {
    const mid = (lo + hi) / 2;
    if (mid <= lo || mid >= hi) break;
    const residual = sum(probsAt(mid)) - 1;
    if (Math.abs(residual) <= TOLERANCE) {
      lo = mid;
      hi = mid;
      break;
    }
    if (residual > 0) lo = mid;
    else hi = mid;
    if (hi - lo <= TOLERANCE) break;
  }

  const out = normalise(probsAt((lo + hi) / 2));
  return allOpenUnit(out) ? out : normalise(q);
}

/** Per-outcome minimum over multiplicative, additive, power and Shin. Not renormalised (sums to <= 1). */
export function devigWorstCase(decimals: number[]): number[] {
  validate(decimals);
  const methods = [devigMultiplicative(decimals), devigAdditive(decimals), devigPower(decimals), devigShin(decimals)];
  return decimals.map((_d, i) => Math.min(...methods.map((m) => m[i])));
}

export function devig(decimals: number[], method: DevigMethod): number[] {
  switch (method) {
    case 'multiplicative':
      return devigMultiplicative(decimals);
    case 'additive':
      return devigAdditive(decimals);
    case 'power':
      return devigPower(decimals);
    case 'shin':
      return devigShin(decimals);
    case 'worst':
      return devigWorstCase(decimals);
    default: {
      const unknown: never = method;
      throw new RangeError(`Unknown devig method: ${String(unknown)}`);
    }
  }
}

/**
 * Kelly stake sizing.
 *
 * For a bet at decimal price d with win probability p, the growth-optimal fraction of bankroll is
 *   f* = (p·d − 1) / (d − 1)   (edge divided by net odds),
 * clamped at 0 when there is no edge. We then scale it down (fractional Kelly × model confidence) and
 * apply hard caps, because p is an estimate and full Kelly on an over-estimated edge is ruinous.
 */

export type StakeCap = 'none' | 'maxStakePct' | 'maxStakeAbs' | 'dailyExposure' | 'minimum' | 'noEdge';

export interface StakeInput {
  prob: number;
  decimal: number;
  bankroll: number;
  kellyMultiplier: number;
  confidence: number;
  maxStakePct: number;
  maxStakeAbs: number;
  remainingDailyExposure: number;
}

export interface StakeResult {
  /** stake / bankroll (0 when the stake is 0). */
  fraction: number;
  /** Suggested stake in whole dollars. */
  stake: number;
  /** Which rule determined the final stake. */
  cappedBy: StakeCap;
  /** The unscaled full-Kelly fraction for (prob, decimal). */
  fullKelly: number;
}

/** Smallest stake worth suggesting, in dollars. */
const MIN_STAKE = 1;
/**
 * Slack added before flooring so float noise such as 0.29 * 100 = 28.999999999999996 floors to 28.99… → 29,
 * not 28. Far below one cent, so it never rounds a genuinely smaller amount up.
 */
const FLOOR_EPSILON = 1e-9;

/** max(0, (p·d − 1)/(d − 1)). Returns 0 for any invalid input (non-finite, p outside [0, 1], d <= 1). */
export function fullKelly(prob: number, decimal: number): number {
  if (!Number.isFinite(prob) || !Number.isFinite(decimal)) return 0;
  if (prob <= 0 || prob > 1 || decimal <= 1) return 0;
  const f = (prob * decimal - 1) / (decimal - 1);
  return Number.isFinite(f) && f > 0 ? Math.min(1, f) : 0;
}

/** Finite and positive, else 0. */
function nonNegativeFinite(x: number): number {
  return Number.isFinite(x) && x > 0 ? x : 0;
}

/** >= 0 and not NaN (so +Infinity means "no limit"), else 0. */
function nonNegativeLimit(x: number): number {
  return typeof x === 'number' && !Number.isNaN(x) && x > 0 ? x : 0;
}

/**
 * raw = bankroll × fullKelly × kellyMultiplier × clamp(confidence, 0, 1), then capped by
 * bankroll × maxStakePct, maxStakeAbs and max(0, remainingDailyExposure) (the tightest cap is recorded;
 * on a tie the earlier cap in that order wins). The result is floored to whole dollars.
 *
 * Labels: 'noEdge' when full Kelly is 0; 'minimum' when no cap bound and the Kelly amount is under $1;
 * otherwise the binding cap (kept even when that cap pushes the stake to $0, e.g. daily exposure used
 * up, so the UI can say why) or 'none'. Any NaN/negative/invalid input yields a $0 stake.
 */
export function suggestStake(input: StakeInput): StakeResult {
  const fk = fullKelly(input.prob, input.decimal);
  if (fk === 0) return { fraction: 0, stake: 0, cappedBy: 'noEdge', fullKelly: 0 };

  const bankroll = nonNegativeFinite(input.bankroll);
  if (bankroll === 0) return { fraction: 0, stake: 0, cappedBy: 'minimum', fullKelly: fk };

  const multiplier = nonNegativeFinite(input.kellyMultiplier);
  const confidence = Number.isNaN(input.confidence) ? 0 : Math.min(1, Math.max(0, input.confidence));
  const raw = bankroll * fk * multiplier * confidence;

  const caps: Array<[Exclude<StakeCap, 'none' | 'minimum' | 'noEdge'>, number]> = [
    ['maxStakePct', bankroll * nonNegativeLimit(input.maxStakePct)],
    ['maxStakeAbs', nonNegativeLimit(input.maxStakeAbs)],
    ['dailyExposure', nonNegativeLimit(input.remainingDailyExposure)],
  ];

  let amount = Number.isFinite(raw) ? raw : 0;
  let cappedBy: StakeCap = 'none';
  for (const [name, cap] of caps) {
    if (cap < amount) {
      amount = cap;
      cappedBy = name;
    }
  }

  let stake = Math.floor(amount + FLOOR_EPSILON);
  if (!Number.isFinite(stake) || stake < MIN_STAKE) {
    stake = 0;
    if (cappedBy === 'none') cappedBy = 'minimum';
  }

  return { fraction: stake / bankroll, stake, cappedBy, fullKelly: fk };
}

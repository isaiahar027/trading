import * as crypto from 'node:crypto';
import * as fs from 'node:fs';
import * as path from 'node:path';
import type { BetRecord, BetResult, BetSummary, LeagueKey, MarketKind, Side } from '../types';
import { americanToDecimal } from '../util/odds';
import { createLogger } from '../util/logger';
import { ValidationError } from './settingsStore';

/**
 * Append-only bet journal (JSON Lines). Every line is a complete BetRecord snapshot; the last line for an id wins.
 * place/settle/updateClosing append synchronously (with fsync) before the change is acknowledged, so a crash never
 * loses a bet the user was told was saved. load() compacts the file when superseded/corrupt lines pile up.
 */

const log = createLogger('journal');

export interface PlaceBetInput {
  opportunityId: string;
  eventId: string;
  league: LeagueKey;
  eventName: string;
  startTime: number;
  pick: string;
  kind: MarketKind;
  side: Side;
  line: number | null;
  wasLive: boolean;
  americanTaken: number;
  stake: number;
  fairProbAtPlace: number;
  notes?: string;
}

/** Thrown when an id is not in the journal (settled bets evicted from memory also count as unknown). */
export class UnknownBetError extends Error {
  constructor(id: string) {
    super(`Unknown bet id: ${id.length > 80 ? `${id.slice(0, 80)}…` : id}`);
    this.name = 'UnknownBetError';
  }
}

export interface BetJournalOptions {
  now?: () => number;
  /** Max bets held in memory (default 20 000). Oldest settled bets are evicted first; they stay on disk. */
  maxInMemory?: number;
}

const DEFAULT_MAX_IN_MEMORY = 20_000;
const DEFAULT_LIST_LIMIT = 500;
const MAX_NOTES_LENGTH = 1000;
const MAX_TEXT_LENGTH = 300;
const MAX_ID_LENGTH = 400;
/** Lines longer than this are treated as corrupt instead of being buffered without bound. */
const MAX_LINE_BYTES = 64 * 1024;
const READ_CHUNK_BYTES = 64 * 1024;

const KINDS: readonly MarketKind[] = ['moneyline', 'spread', 'total'];
const SIDES: readonly Side[] = ['home', 'away', 'draw', 'over', 'under'];
const RESULTS: readonly BetResult[] = ['pending', 'won', 'lost', 'push', 'void'];
const SETTLED_RESULTS: readonly Exclude<BetResult, 'pending'>[] = ['won', 'lost', 'push', 'void'];

function isObj(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

function isFiniteNum(v: unknown): v is number {
  return typeof v === 'number' && Number.isFinite(v);
}

function roundCents(n: number): number {
  const r = Math.round(n * 100) / 100;
  return Object.is(r, -0) ? 0 : r;
}

function profitFor(result: Exclude<BetResult, 'pending'>, stake: number, decimal: number): number {
  if (result === 'won') return roundCents(stake * (decimal - 1));
  if (result === 'lost') return roundCents(-stake);
  return 0;
}

/** Structural check of one parsed journal line. Returns null when it is not a usable BetRecord. */
function parseRecord(v: unknown): BetRecord | null {
  if (!isObj(v)) return null;
  const {
    id, placedAt, opportunityId, eventId, league, eventName, startTime, pick, kind, side, line, wasLive,
    americanTaken, decimalTaken, stake, fairProbAtPlace, evPctAtPlace, closingFairProb, result, settledAt, profit, notes,
  } = v;
  if (typeof id !== 'string' || id === '' || id.length > MAX_ID_LENGTH) return null;
  if (!isFiniteNum(placedAt) || !isFiniteNum(startTime)) return null;
  if (typeof opportunityId !== 'string' || typeof eventId !== 'string' || typeof league !== 'string') return null;
  if (typeof eventName !== 'string' || typeof pick !== 'string') return null;
  if (typeof kind !== 'string' || !(KINDS as readonly string[]).includes(kind)) return null;
  if (typeof side !== 'string' || !(SIDES as readonly string[]).includes(side)) return null;
  if (line !== null && !isFiniteNum(line)) return null;
  if (typeof wasLive !== 'boolean') return null;
  if (!isFiniteNum(americanTaken) || Math.abs(americanTaken) < 100) return null;
  if (!isFiniteNum(decimalTaken) || decimalTaken <= 1) return null;
  if (!isFiniteNum(stake) || stake <= 0) return null;
  if (!isFiniteNum(fairProbAtPlace) || fairProbAtPlace <= 0 || fairProbAtPlace >= 1) return null;
  if (!isFiniteNum(evPctAtPlace)) return null;
  if (closingFairProb !== null && (!isFiniteNum(closingFairProb) || closingFairProb <= 0 || closingFairProb >= 1)) return null;
  if (typeof result !== 'string' || !(RESULTS as readonly string[]).includes(result)) return null;
  if (settledAt !== null && !isFiniteNum(settledAt)) return null;
  if (profit !== null && !isFiniteNum(profit)) return null;
  if (notes !== undefined && typeof notes !== 'string') return null;
  const rec: BetRecord = {
    id,
    placedAt,
    opportunityId,
    eventId,
    league,
    eventName,
    startTime,
    pick,
    kind: kind as MarketKind,
    side: side as Side,
    line: line as number | null,
    wasLive,
    americanTaken,
    decimalTaken,
    stake,
    fairProbAtPlace,
    evPctAtPlace,
    closingFairProb: closingFairProb as number | null,
    result: result as BetResult,
    settledAt: settledAt as number | null,
    profit: profit as number | null,
  };
  if (typeof notes === 'string') rec.notes = notes;
  return rec;
}

function requireText(field: string, v: unknown, max = MAX_TEXT_LENGTH, allowEmpty = false): string {
  if (typeof v !== 'string') throw new ValidationError(`${field} must be a string`);
  const s = v.trim();
  if (!allowEmpty && s === '') throw new ValidationError(`${field} must not be empty`);
  if (s.length > max) throw new ValidationError(`${field} is too long (max ${max} characters)`);
  return s;
}

/** Ascending by placedAt. Array#sort is stable, so ties keep journal (insertion) order. */
function byPlacedAt(a: BetRecord, b: BetRecord): number {
  return a.placedAt - b.placedAt;
}

/** Running totals for settled bets evicted from memory, so the summary stays exact. */
interface Archived {
  count: number;
  staked: number;
  settledStake: number;
  profit: number;
  evSum: number;
  clvSum: number;
  clvCount: number;
}

function emptyArchive(): Archived {
  return { count: 0, staked: 0, settledStake: 0, profit: 0, evSum: 0, clvSum: 0, clvCount: 0 };
}

function clvOf(b: BetRecord): number | null {
  return b.closingFairProb === null ? null : b.closingFairProb * b.decimalTaken - 1;
}

export class BetJournal {
  private readonly file: string;
  private readonly now: () => number;
  private readonly maxInMemory: number;
  /** id -> latest record, in journal (insertion) order. */
  private bets = new Map<string, BetRecord>();
  private archived: Archived = emptyArchive();
  /** Non-empty lines currently in the file (for diagnostics). */
  private lineCount = 0;

  constructor(file: string, opts: BetJournalOptions = {}) {
    this.file = path.resolve(file);
    this.now = opts.now ?? Date.now;
    const cap = opts.maxInMemory;
    this.maxInMemory = cap !== undefined && Number.isFinite(cap) && cap >= 1 ? Math.floor(cap) : DEFAULT_MAX_IN_MEMORY;
  }

  /**
   * Reads the JSONL file. Corrupt lines are skipped (one warning with the count); the last valid line per id wins.
   * Rewrites the file compactly when it has more than 2 × bets + 50 lines. Missing file = empty journal.
   */
  load(): this {
    const all = new Map<string, BetRecord>();
    let lines = 0;
    let corrupt = 0;
    let firstCorruptLine = 0;
    let endsWithNewline = true;
    let size = 0;

    let fd: number | null = null;
    try {
      fd = fs.openSync(this.file, 'r');
    } catch (err) {
      if ((err as NodeJS.ErrnoException).code !== 'ENOENT') {
        log.error('could not open bet journal; starting empty in memory', { file: this.file, error: (err as Error).message });
      }
    }

    if (fd !== null) {
      try {
        const buf = Buffer.allocUnsafe(READ_CHUNK_BYTES);
        let pending: Buffer[] = [];
        let pendingBytes = 0;
        let oversized = false;
        let lineNo = 0;
        const handleLine = (bytes: Buffer): void => {
          lineNo++;
          const text = bytes.toString('utf8').trim();
          if (text === '') return;
          lines++;
          let rec: BetRecord | null = null;
          try {
            rec = parseRecord(JSON.parse(text));
          } catch {
            rec = null;
          }
          if (rec === null) {
            corrupt++;
            if (firstCorruptLine === 0) firstCorruptLine = lineNo;
            return;
          }
          // Map#set on an existing key keeps its first-seen position, so ties on placedAt stay in journal order.
          all.set(rec.id, rec);
        };
        for (;;) {
          const n = fs.readSync(fd, buf, 0, buf.length, null);
          if (n <= 0) break;
          size += n;
          endsWithNewline = buf[n - 1] === 0x0a;
          let start = 0;
          for (let i = 0; i < n; i++) {
            if (buf[i] !== 0x0a) continue;
            if (oversized) {
              lineNo++;
              lines++;
              corrupt++;
              if (firstCorruptLine === 0) firstCorruptLine = lineNo;
              oversized = false;
            } else {
              pending.push(buf.subarray(start, i));
              handleLine(Buffer.concat(pending));
            }
            pending = [];
            pendingBytes = 0;
            start = i + 1;
          }
          if (start < n && !oversized) {
            const rest = Buffer.from(buf.subarray(start, n));
            pendingBytes += rest.length;
            if (pendingBytes > MAX_LINE_BYTES) {
              oversized = true;
              pending = [];
              pendingBytes = 0;
            } else {
              pending.push(rest);
            }
          }
        }
        if (oversized) {
          lineNo++;
          lines++;
          corrupt++;
          if (firstCorruptLine === 0) firstCorruptLine = lineNo;
        } else if (pendingBytes > 0) {
          handleLine(Buffer.concat(pending));
        }
      } catch (err) {
        log.error('error while reading bet journal; loaded what was readable', { file: this.file, error: (err as Error).message });
      } finally {
        fs.closeSync(fd);
      }
    }

    if (corrupt > 0) {
      log.warn('skipped corrupt lines in bet journal', { file: this.file, corrupt, firstCorruptLine });
    }

    const sorted = Array.from(all.values()).sort(byPlacedAt);
    this.lineCount = lines;

    if (lines > 2 * sorted.length + 50) {
      this.compact(sorted, corrupt > 0);
    } else if (size > 0 && !endsWithNewline) {
      // A crash mid-append can leave a partial last line; terminate it so the next append starts on a fresh line.
      try {
        fs.appendFileSync(this.file, '\n', { flush: true });
      } catch (err) {
        log.error('could not repair the end of the bet journal', { file: this.file, error: (err as Error).message });
      }
    }

    this.bets = new Map(sorted.map((b) => [b.id, b]));
    this.archived = emptyArchive();
    this.evictIfNeeded();
    return this;
  }

  /** Records a bet the user says they placed. Validates input; throws ValidationError on bad values. */
  place(input: PlaceBetInput): BetRecord {
    if (!isObj(input)) throw new ValidationError('Bet must be an object');
    const stake = input.stake;
    if (!isFiniteNum(stake) || stake <= 0) throw new ValidationError('Stake must be a positive number');
    if (stake > 1e8) throw new ValidationError('Stake is unrealistically large');
    const american = input.americanTaken;
    if (!isFiniteNum(american) || Math.abs(american) < 100) {
      throw new ValidationError('American odds must be a number like +110 or -120 (absolute value at least 100)');
    }
    const fairProb = input.fairProbAtPlace;
    if (!isFiniteNum(fairProb) || fairProb <= 0 || fairProb >= 1) {
      throw new ValidationError('Fair probability must be between 0 and 1 (exclusive)');
    }
    if (typeof input.kind !== 'string' || !(KINDS as readonly string[]).includes(input.kind)) {
      throw new ValidationError(`Market kind must be one of ${KINDS.join(', ')}`);
    }
    if (typeof input.side !== 'string' || !(SIDES as readonly string[]).includes(input.side)) {
      throw new ValidationError(`Side must be one of ${SIDES.join(', ')}`);
    }
    if (input.line !== null && !isFiniteNum(input.line)) throw new ValidationError('Line must be a number or null');
    if (!isFiniteNum(input.startTime)) throw new ValidationError('Start time must be a timestamp');
    if (typeof input.wasLive !== 'boolean') throw new ValidationError('wasLive must be true or false');
    const opportunityId = requireText('opportunityId', input.opportunityId, MAX_ID_LENGTH);
    const eventId = requireText('eventId', input.eventId, MAX_ID_LENGTH);
    const league = requireText('league', input.league);
    const eventName = requireText('eventName', input.eventName);
    const pick = requireText('pick', input.pick);
    const notes = input.notes === undefined ? undefined : requireText('notes', input.notes, MAX_NOTES_LENGTH, true);

    const decimalTaken = americanToDecimal(american);
    const rec: BetRecord = {
      id: crypto.randomUUID(),
      placedAt: this.now(),
      opportunityId,
      eventId,
      league,
      eventName,
      startTime: input.startTime,
      pick,
      kind: input.kind,
      side: input.side,
      line: input.line,
      wasLive: input.wasLive,
      americanTaken: american,
      decimalTaken,
      stake,
      fairProbAtPlace: fairProb,
      evPctAtPlace: fairProb * decimalTaken - 1,
      closingFairProb: null,
      result: 'pending',
      settledAt: null,
      profit: null,
    };
    if (notes !== undefined && notes !== '') rec.notes = notes;

    this.append(rec);
    this.bets.set(rec.id, rec);
    this.evictIfNeeded();
    return { ...rec };
  }

  /** Settles (or re-settles, to correct a mistake) a bet. won: stake × (dec − 1); lost: −stake; push/void: 0. */
  settle(id: string, result: Exclude<BetResult, 'pending'>): BetRecord {
    if (!(SETTLED_RESULTS as readonly unknown[]).includes(result)) {
      throw new ValidationError(`Result must be one of ${SETTLED_RESULTS.join(', ')}`);
    }
    const prev = this.require(id);
    const next: BetRecord = {
      ...prev,
      result,
      settledAt: this.now(),
      profit: profitFor(result, prev.stake, prev.decimalTaken),
    };
    this.append(next);
    this.bets.set(id, next);
    return { ...next };
  }

  /** Stores the closing fair probability used for closing-line value. */
  updateClosing(id: string, closingFairProb: number): BetRecord {
    if (!isFiniteNum(closingFairProb) || closingFairProb <= 0 || closingFairProb >= 1) {
      throw new ValidationError('Closing fair probability must be between 0 and 1 (exclusive)');
    }
    const prev = this.require(id);
    const next: BetRecord = { ...prev, closingFairProb };
    this.append(next);
    this.bets.set(id, next);
    return { ...next };
  }

  get(id: string): BetRecord | undefined {
    const b = this.bets.get(id);
    return b ? { ...b } : undefined;
  }

  /** Newest first. */
  list(limit: number = DEFAULT_LIST_LIMIT): BetRecord[] {
    const n = Number.isFinite(limit) ? Math.max(0, Math.floor(limit)) : DEFAULT_LIST_LIMIT;
    if (n === 0) return [];
    return Array.from(this.bets.values())
      .reverse()
      .sort((a, b) => byPlacedAt(b, a))
      .slice(0, n)
      .map((b) => ({ ...b }));
  }

  /** Pending bets that have no closing fair probability yet (oldest first). */
  pendingWithoutClosing(): BetRecord[] {
    const out: BetRecord[] = [];
    for (const b of this.bets.values()) {
      if (b.result === 'pending' && b.closingFairProb === null) out.push({ ...b });
    }
    return out.sort(byPlacedAt);
  }

  /** Total stake of bets placed in [from, to), excluding voided bets. */
  stakedBetween(from: number, to: number): number {
    let total = 0;
    for (const b of this.bets.values()) {
      if (b.result !== 'void' && b.placedAt >= from && b.placedAt < to) total += b.stake;
    }
    return roundCents(total);
  }

  /** Stake placed during the server's local calendar day containing `now` (process TZ, DST-safe). */
  stakedToday(now: number): number {
    const start = new Date(now);
    start.setHours(0, 0, 0, 0);
    const end = new Date(start.getTime());
    end.setDate(end.getDate() + 1);
    return this.stakedBetween(start.getTime(), end.getTime());
  }

  /**
   * roiPct = profit / settled stake (won/lost/push; void excluded), avgEvPct = mean EV at placement over all bets,
   * avgClvPct = mean(closingFairProb × decimalTaken − 1) over bets with a closing probability.
   * Percent fields are fractions (0.034 = +3.4%), matching Opportunity.evPct. `staked` excludes voided bets.
   */
  summary(now: number): BetSummary {
    const a = this.archived;
    let totalBets = a.count;
    let pending = 0;
    let staked = a.staked;
    let settledStake = a.settledStake;
    let profit = a.profit;
    let evSum = a.evSum;
    let clvSum = a.clvSum;
    let clvCount = a.clvCount;
    for (const b of this.bets.values()) {
      totalBets++;
      evSum += b.evPctAtPlace;
      if (b.result !== 'void') staked += b.stake;
      if (b.result === 'pending') pending++;
      if (b.result === 'won' || b.result === 'lost' || b.result === 'push') settledStake += b.stake;
      if (b.profit !== null) profit += b.profit;
      const clv = clvOf(b);
      if (clv !== null) {
        clvSum += clv;
        clvCount++;
      }
    }
    return {
      totalBets,
      pending,
      staked: roundCents(staked),
      profit: roundCents(profit),
      roiPct: settledStake > 0 ? profit / settledStake : null,
      avgEvPct: totalBets > 0 ? evSum / totalBets : null,
      avgClvPct: clvCount > 0 ? clvSum / clvCount : null,
      stakedToday: this.stakedToday(now),
    };
  }

  /** Number of bets currently held in memory. */
  size(): number {
    return this.bets.size;
  }

  private require(id: string): BetRecord {
    const b = typeof id === 'string' ? this.bets.get(id) : undefined;
    if (!b) throw new UnknownBetError(String(id));
    return b;
  }

  /** Synchronous, fsync'ed append of one full record. Throws (and nothing changes in memory) if the disk write fails. */
  private append(rec: BetRecord): void {
    const line = `${JSON.stringify(rec)}\n`;
    try {
      fs.mkdirSync(path.dirname(this.file), { recursive: true });
      fs.appendFileSync(this.file, line, { encoding: 'utf8', mode: 0o600, flush: true });
      this.lineCount++;
    } catch (err) {
      log.error('failed to write to bet journal', { file: this.file, error: (err as Error).message });
      throw new Error(`Could not save bet: ${(err as Error).message}`);
    }
  }

  /** Rewrites the file with one line per bet (atomic: tmp + fsync + rename). Keeps a .bak when lines were corrupt. */
  private compact(sorted: BetRecord[], hadCorrupt: boolean): void {
    const dir = path.dirname(this.file);
    const tmp = path.join(dir, `.${path.basename(this.file)}.${process.pid}.${crypto.randomBytes(6).toString('hex')}.tmp`);
    try {
      if (hadCorrupt) fs.copyFileSync(this.file, `${this.file}.bak`);
      const fd = fs.openSync(tmp, 'w', 0o600);
      try {
        let chunk = '';
        for (const b of sorted) {
          chunk += `${JSON.stringify(b)}\n`;
          if (chunk.length >= 1 << 20) {
            fs.writeSync(fd, chunk);
            chunk = '';
          }
        }
        if (chunk !== '') fs.writeSync(fd, chunk);
        fs.fsyncSync(fd);
      } finally {
        fs.closeSync(fd);
      }
      fs.renameSync(tmp, this.file);
      log.info('compacted bet journal', { file: this.file, linesBefore: this.lineCount, bets: sorted.length });
      this.lineCount = sorted.length;
    } catch (err) {
      try {
        fs.rmSync(tmp, { force: true });
      } catch {
        // best effort; the original journal is untouched
      }
      log.error('bet journal compaction failed; keeping the original file', { file: this.file, error: (err as Error).message });
    }
  }

  /** Evicts the oldest settled bets from memory (down to 95% of the cap) once the cap is exceeded. */
  private evictIfNeeded(): void {
    if (this.bets.size <= this.maxInMemory) return;
    const target = Math.max(1, Math.floor(this.maxInMemory * 0.95));
    const settled = Array.from(this.bets.values())
      .filter((b) => b.result !== 'pending')
      .sort(byPlacedAt);
    let excess = this.bets.size - target;
    for (const b of settled) {
      if (excess <= 0) break;
      this.bets.delete(b.id);
      const a = this.archived;
      a.count++;
      a.evSum += b.evPctAtPlace;
      if (b.result !== 'void') a.staked += b.stake;
      if (b.result !== 'void') a.settledStake += b.stake;
      if (b.profit !== null) a.profit += b.profit;
      const clv = clvOf(b);
      if (clv !== null) {
        a.clvSum += clv;
        a.clvCount++;
      }
      excess--;
    }
  }
}

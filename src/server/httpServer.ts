import * as crypto from 'node:crypto';
import * as fs from 'node:fs';
import * as http from 'node:http';
import * as path from 'node:path';
import type { BetJournal, PlaceBetInput } from './betJournal';
import type { SettingsStore } from './settingsStore';
import type { BetResult, DashboardState, Opportunity, RuntimeSettings } from '../types';
import { createLogger } from '../util/logger';

/**
 * Dashboard HTTP server (node:http only).
 *
 *  - `GET /healthz` is the only unauthenticated route. Everything else requires HTTP Basic auth when a password is set.
 *  - Static files come from a fixed whitelist; request paths are matched verbatim (never decoded or joined), so
 *    traversal attempts such as `/../x` or `/%2e%2e/x` simply do not match and get a 404.
 *  - `GET /api/stream` is a Server-Sent Events feed of `event: state` frames. Slow clients are conflated (they get the
 *    newest state once their socket drains) and dropped when they stay backed up for too long or buffer too much.
 *  - JSON bodies: application/json only, 32 KB max, and a present Origin header must match Host (CSRF guard).
 *
 * Validation errors thrown by the settings store / bet journal are recognised by error name ('ValidationError',
 * 'UnknownBetError') so this module only needs type imports and tests can use small fakes.
 */

const log = createLogger('http');

export interface ServerDeps {
  host: string;
  port: number;
  user: string;
  password: string;
  publicDir: string;
  getState: () => DashboardState;
  settings: Pick<SettingsStore, 'get' | 'update'>;
  journal: Pick<BetJournal, 'place' | 'settle' | 'list' | 'summary'>;
  findOpportunity: (id: string) => Opportunity | undefined;
  onSettingsChanged?: (s: RuntimeSettings) => void;
  now?: () => number;
  /** SSE heartbeat period in ms (default 15 000). */
  heartbeatMs?: number;
  /** Max concurrent SSE clients (default 25); further stream requests get 503. */
  maxSseClients?: number;
  /** A backed-up SSE client holding more unsent bytes than this is dropped (default 4 MiB). */
  sseMaxBufferBytes?: number;
  /** A SSE client whose socket stays backed up longer than this is dropped (default 60 000 ms). */
  sseStallMs?: number;
}

const MAX_BODY_BYTES = 32 * 1024;
/** Oversized bodies are read and discarded up to this size so the client reliably sees the 413 response. */
const MAX_DRAIN_BYTES = 1024 * 1024;
const DEFAULT_HEARTBEAT_MS = 15_000;
const DEFAULT_MAX_SSE_CLIENTS = 25;
const DEFAULT_SSE_MAX_BUFFER_BYTES = 4 * 1024 * 1024;
const DEFAULT_SSE_STALL_MS = 60_000;
const SSE_RETRY_MS = 5_000;
const DEFAULT_BET_LIST_LIMIT = 500;
const MAX_BET_LIST_LIMIT = 5_000;
const MAX_AUTH_HEADER_LENGTH = 4_096;
const MAX_OPPORTUNITY_ID_LENGTH = 400;
const MAX_BET_ID_LENGTH = 128;
const MAX_ERROR_MESSAGE_LENGTH = 500;
/** Static files larger than this are served but not kept in memory. */
const MAX_CACHED_FILE_BYTES = 2 * 1024 * 1024;
const STOP_GRACE_MS = 2_000;

const CONTENT_SECURITY_POLICY =
  "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; " +
  "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'";

const SECURITY_HEADERS: ReadonlyArray<readonly [string, string]> = [
  ['X-Content-Type-Options', 'nosniff'],
  ['X-Frame-Options', 'DENY'],
  ['Referrer-Policy', 'no-referrer'],
  ['Content-Security-Policy', CONTENT_SECURITY_POLICY],
];

interface StaticAsset {
  file: string;
  type: string;
}

const INDEX_ASSET: StaticAsset = { file: 'index.html', type: 'text/html; charset=utf-8' };

/** The only files ever read from publicDir. Keys are matched against the raw (undecoded) request path. */
const STATIC_ASSETS: ReadonlyMap<string, StaticAsset> = new Map([
  ['/', INDEX_ASSET],
  ['/index.html', INDEX_ASSET],
  ['/app.js', { file: 'app.js', type: 'text/javascript; charset=utf-8' }],
  ['/styles.css', { file: 'styles.css', type: 'text/css; charset=utf-8' }],
  ['/favicon.svg', { file: 'favicon.svg', type: 'image/svg+xml' }],
]);

const SETTLE_RESULTS: ReadonlyArray<Exclude<BetResult, 'pending'>> = ['won', 'lost', 'push', 'void'];
const SETTLE_PATH = /^\/api\/bets\/([^/]+)\/settle$/;
const BET_ID_PATTERN = /^[A-Za-z0-9._:-]+$/;

interface SseClient {
  res: http.ServerResponse;
  /** When the socket was first seen backed up (write returned false / needs drain); null when flowing. */
  backedUpSince: number | null;
  /** A newer state arrived while backed up; send the latest frame on 'drain'. */
  pending: boolean;
}

interface CachedFile {
  mtimeMs: number;
  ctimeMs: number;
  size: number;
  body: Buffer;
}

type BodyResult = { kind: 'ok'; text: string } | { kind: 'too-large'; drained: boolean } | { kind: 'aborted' };

type JsonBodyResult = { ok: true; value: unknown } | { ok: false };

function noop(): void {
  /* intentionally empty: errors on aborted client sockets are expected and handled via 'close' */
}

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

function isNamedError(err: unknown, name: string): err is Error {
  return err instanceof Error && (err.name === name || err.constructor.name === name);
}

function errorMessage(err: Error): string {
  const msg = err.message || 'Invalid request';
  return msg.length > MAX_ERROR_MESSAGE_LENGTH ? `${msg.slice(0, MAX_ERROR_MESSAGE_LENGTH)}…` : msg;
}

function describeError(err: unknown): string {
  if (err instanceof Error) return `${err.name}: ${err.message}`;
  return String(err);
}

/** Accepts JSON numbers and numeric strings (HTML form inputs); anything else -> null. */
function toFiniteNumber(v: unknown): number | null {
  if (typeof v === 'number') return Number.isFinite(v) ? v : null;
  if (typeof v === 'string' && v.trim() !== '') {
    const n = Number(v.trim());
    return Number.isFinite(n) ? n : null;
  }
  return null;
}

function sha256(s: string): Buffer {
  return crypto.createHash('sha256').update(s, 'utf8').digest();
}

/** Constant-time string comparison (hashing first makes the lengths equal). */
function safeEqual(a: string, b: string): boolean {
  return crypto.timingSafeEqual(sha256(a), sha256(b));
}

function sendJson(res: http.ServerResponse, status: number, body: unknown, headers?: Record<string, string>): void {
  const buf = Buffer.from(JSON.stringify(body), 'utf8');
  res.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'Cache-Control': 'no-store',
    'Content-Length': String(buf.length),
    ...headers,
  });
  res.end(buf);
}

function sendError(res: http.ServerResponse, status: number, error: string, headers?: Record<string, string>): void {
  sendJson(res, status, { error }, headers);
}

function methodNotAllowed(res: http.ServerResponse, allow: string): void {
  sendError(res, 405, 'method not allowed', { Allow: allow });
}

function stateFrame(state: DashboardState): string {
  // JSON.stringify never emits raw CR/LF, so the payload is always a single SSE data line.
  return `event: state\ndata: ${JSON.stringify(state)}\n\n`;
}

/** A present Origin header must name the same host[:port] as the Host header. */
function originAllowed(req: http.IncomingMessage): boolean {
  const origin = req.headers.origin;
  if (origin === undefined) return true;
  const host = req.headers.host;
  if (typeof host !== 'string' || host === '') return false;
  try {
    const o = new URL(origin);
    if (o.protocol !== 'http:' && o.protocol !== 'https:') return false;
    const h = new URL(`${o.protocol}//${host}`);
    return o.host.toLowerCase() === h.host.toLowerCase();
  } catch {
    return false;
  }
}

function contentTypeOf(req: http.IncomingMessage): string {
  const raw = req.headers['content-type'];
  if (typeof raw !== 'string') return '';
  return raw.split(';')[0].trim().toLowerCase();
}

/**
 * Reads a request body with a hard size limit. Bodies over `limit` are discarded while being read (memory stays
 * bounded); reading stops altogether past `drainLimit`.
 */
function readBody(req: http.IncomingMessage, limit: number, drainLimit: number): Promise<BodyResult> {
  return new Promise((resolve) => {
    const chunks: Buffer[] = [];
    let total = 0;
    let settled = false;

    const finish = (result: BodyResult): void => {
      if (settled) return;
      settled = true;
      req.off('data', onData);
      req.off('end', onEnd);
      req.off('error', onError);
      req.off('close', onClose);
      resolve(result);
    };
    const onData = (chunk: Buffer): void => {
      total += chunk.length;
      if (total <= limit) {
        chunks.push(chunk);
        return;
      }
      chunks.length = 0;
      if (total > drainLimit) {
        req.pause();
        finish({ kind: 'too-large', drained: false });
      }
    };
    const onEnd = (): void => {
      if (total > limit) finish({ kind: 'too-large', drained: true });
      else finish({ kind: 'ok', text: Buffer.concat(chunks, total).toString('utf8') });
    };
    const onError = (): void => finish({ kind: 'aborted' });
    const onClose = (): void => {
      if (!req.complete) finish({ kind: 'aborted' });
    };

    req.on('data', onData);
    req.on('end', onEnd);
    req.on('error', onError);
    req.on('close', onClose);
  });
}

export class DashboardServer {
  private readonly deps: ServerDeps;
  private readonly now: () => number;
  private readonly publicDir: string;
  private readonly heartbeatMs: number;
  private readonly maxSseClients: number;
  private readonly sseMaxBufferBytes: number;
  private readonly sseStallMs: number;
  private readonly clients = new Set<SseClient>();
  /** Bounded by the static whitelist (at most 4 entries). */
  private readonly staticCache = new Map<string, CachedFile>();
  private server: http.Server | null = null;
  private heartbeat: NodeJS.Timeout | null = null;
  private stopping: Promise<void> | null = null;
  private startedAt: number;
  /** Most recent broadcast frame, used to catch up clients that were backed up. */
  private latestFrame: string | null = null;

  constructor(deps: ServerDeps) {
    this.deps = deps;
    this.now = deps.now ?? Date.now;
    this.publicDir = path.resolve(deps.publicDir);
    this.heartbeatMs = positiveOr(deps.heartbeatMs, DEFAULT_HEARTBEAT_MS);
    this.maxSseClients = Math.floor(positiveOr(deps.maxSseClients, DEFAULT_MAX_SSE_CLIENTS));
    this.sseMaxBufferBytes = positiveOr(deps.sseMaxBufferBytes, DEFAULT_SSE_MAX_BUFFER_BYTES);
    this.sseStallMs = positiveOr(deps.sseStallMs, DEFAULT_SSE_STALL_MS);
    this.startedAt = this.now();
  }

  start(): Promise<{ port: number }> {
    if (this.server) return Promise.reject(new Error('DashboardServer already started'));
    const server = http.createServer((req, res) => this.onRequest(req, res));
    // requestTimeout only covers receiving the request (headers + body). A GET /api/stream request is complete as
    // soon as its headers arrive, so long-lived SSE responses are not affected by it. No idle-socket timeout
    // (server.timeout = 0): SSE heartbeats keep proxies happy and keepAliveTimeout reaps idle keep-alive sockets.
    server.headersTimeout = 15_000;
    server.requestTimeout = 30_000;
    server.keepAliveTimeout = 5_000;
    server.timeout = 0;
    server.maxHeadersCount = 100;
    server.maxConnections = 512;
    server.on('clientError', (err: NodeJS.ErrnoException, socket) => {
      if (err.code === 'ECONNRESET' || !socket.writable) {
        socket.destroy();
        return;
      }
      const status =
        err.code === 'ERR_HTTP_REQUEST_TIMEOUT'
          ? '408 Request Timeout'
          : err.code === 'HPE_HEADER_OVERFLOW'
            ? '431 Request Header Fields Too Large'
            : '400 Bad Request';
      socket.end(`HTTP/1.1 ${status}\r\nConnection: close\r\nContent-Length: 0\r\n\r\n`);
    });
    this.server = server;

    return new Promise((resolve, reject) => {
      const onError = (err: Error): void => {
        server.off('listening', onListening);
        this.server = null;
        reject(err);
      };
      const onListening = (): void => {
        server.off('error', onError);
        server.on('error', (err) => log.error('http server error', { error: describeError(err) }));
        const addr = server.address();
        const port = addr !== null && typeof addr === 'object' ? addr.port : this.deps.port;
        this.startedAt = this.now();
        this.heartbeat = setInterval(() => this.tickHeartbeat(), this.heartbeatMs);
        this.heartbeat.unref();
        log.info(`dashboard listening on http://${this.deps.host}:${port}`, { auth: this.deps.password !== '' });
        resolve({ port });
      };
      server.once('error', onError);
      server.once('listening', onListening);
      server.listen(this.deps.port, this.deps.host);
    });
  }

  broadcast(state: DashboardState): void {
    if (this.clients.size === 0) {
      this.latestFrame = null;
      return;
    }
    let frame: string;
    try {
      frame = stateFrame(state);
    } catch (err) {
      log.error('could not serialize dashboard state', { error: describeError(err) });
      return;
    }
    this.latestFrame = frame;
    const now = this.now();
    for (const client of [...this.clients]) {
      if (!this.isOpen(client)) continue;
      if (client.res.writableNeedDrain) {
        if (client.backedUpSince === null) client.backedUpSince = now;
        if (client.res.writableLength > this.sseMaxBufferBytes || now - client.backedUpSince > this.sseStallMs) {
          this.dropClient(client, 'socket buffer backed up');
        } else {
          client.pending = true;
        }
        continue;
      }
      this.writeTo(client, frame);
    }
  }

  clientCount(): number {
    return this.clients.size;
  }

  stop(): Promise<void> {
    if (this.stopping) return this.stopping;
    if (this.heartbeat) {
      clearInterval(this.heartbeat);
      this.heartbeat = null;
    }
    for (const client of this.clients) {
      try {
        client.res.end();
        client.res.socket?.end();
      } catch {
        client.res.destroy();
      }
    }
    this.clients.clear();
    this.latestFrame = null;
    const server = this.server;
    if (!server) return Promise.resolve();
    this.stopping = new Promise<void>((resolve) => {
      const force = setTimeout(() => server.closeAllConnections(), STOP_GRACE_MS);
      force.unref();
      server.close(() => {
        clearTimeout(force);
        this.server = null;
        this.stopping = null;
        resolve();
      });
      server.closeIdleConnections();
    });
    return this.stopping;
  }

  // ---------------------------------------------------------------------------------------------------------------
  // Request handling

  private onRequest(req: http.IncomingMessage, res: http.ServerResponse): void {
    req.on('error', noop);
    res.on('error', noop);
    this.handle(req, res).catch((err: unknown) => {
      log.error('request handler failed', { method: req.method, path: pathOf(req.url), error: describeError(err) });
      if (res.headersSent) {
        res.destroy();
        return;
      }
      try {
        sendError(res, 500, 'internal');
      } catch {
        res.destroy();
      }
    });
  }

  private async handle(req: http.IncomingMessage, res: http.ServerResponse): Promise<void> {
    for (const [name, value] of SECURITY_HEADERS) res.setHeader(name, value);
    const method = (req.method ?? 'GET').toUpperCase();
    const rawUrl = req.url ?? '/';
    const qIndex = rawUrl.indexOf('?');
    const pathname = qIndex >= 0 ? rawUrl.slice(0, qIndex) : rawUrl;
    const query = new URLSearchParams(qIndex >= 0 ? rawUrl.slice(qIndex + 1) : '');

    if (pathname === '/healthz') {
      if (method !== 'GET' && method !== 'HEAD') return methodNotAllowed(res, 'GET, HEAD');
      const uptimeSec = Math.max(0, Math.round((this.now() - this.startedAt) / 1000));
      return sendJson(res, 200, { ok: true, uptimeSec });
    }

    if (!this.isAuthorized(req)) {
      return sendError(res, 401, 'unauthorized', { 'WWW-Authenticate': 'Basic realm="Odds Hub", charset="UTF-8"' });
    }

    if (method !== 'GET' && method !== 'HEAD' && method !== 'OPTIONS' && !originAllowed(req)) {
      return sendError(res, 403, 'cross-origin request rejected');
    }

    const asset = STATIC_ASSETS.get(pathname);
    if (asset) {
      if (method !== 'GET' && method !== 'HEAD') return methodNotAllowed(res, 'GET, HEAD');
      return this.serveStatic(res, asset);
    }

    switch (pathname) {
      case '/api/state':
        if (method !== 'GET' && method !== 'HEAD') return methodNotAllowed(res, 'GET, HEAD');
        return sendJson(res, 200, this.deps.getState());
      case '/api/stream':
        if (method !== 'GET') return methodNotAllowed(res, 'GET');
        return this.openStream(req, res);
      case '/api/bets':
        if (method === 'GET' || method === 'HEAD') return this.listBets(res, query);
        if (method === 'POST') return this.placeBet(req, res);
        return methodNotAllowed(res, 'GET, HEAD, POST');
      case '/api/settings':
        if (method === 'GET' || method === 'HEAD') return sendJson(res, 200, this.deps.settings.get());
        if (method === 'PUT') return this.updateSettings(req, res);
        return methodNotAllowed(res, 'GET, HEAD, PUT');
      default:
        break;
    }

    const settle = SETTLE_PATH.exec(pathname);
    if (settle) {
      if (method !== 'POST') return methodNotAllowed(res, 'POST');
      return this.settleBet(req, res, settle[1]);
    }

    return sendError(res, 404, 'not found');
  }

  private isAuthorized(req: http.IncomingMessage): boolean {
    if (this.deps.password === '') return true;
    const header = req.headers.authorization;
    if (typeof header !== 'string' || header.length > MAX_AUTH_HEADER_LENGTH) return false;
    const m = /^Basic[ \t]+([A-Za-z0-9+/]+={0,2})[ \t]*$/i.exec(header);
    if (!m) return false;
    const decoded = Buffer.from(m[1], 'base64').toString('utf8');
    const colon = decoded.indexOf(':');
    if (colon < 0) return false;
    // Evaluate both comparisons so timing does not reveal which part was wrong.
    const userOk = safeEqual(decoded.slice(0, colon), this.deps.user);
    const passOk = safeEqual(decoded.slice(colon + 1), this.deps.password);
    return userOk && passOk;
  }

  private async serveStatic(res: http.ServerResponse, asset: StaticAsset): Promise<void> {
    const full = path.join(this.publicDir, asset.file);
    let body: Buffer;
    try {
      const st = await fs.promises.stat(full);
      if (!st.isFile()) return sendError(res, 404, 'not found');
      const cached = this.staticCache.get(asset.file);
      if (cached && cached.mtimeMs === st.mtimeMs && cached.ctimeMs === st.ctimeMs && cached.size === st.size) {
        body = cached.body;
      } else {
        body = await fs.promises.readFile(full);
        if (body.length <= MAX_CACHED_FILE_BYTES) {
          this.staticCache.set(asset.file, { mtimeMs: st.mtimeMs, ctimeMs: st.ctimeMs, size: st.size, body });
        } else {
          this.staticCache.delete(asset.file);
        }
      }
    } catch (err) {
      const code = (err as NodeJS.ErrnoException).code;
      if (code === 'ENOENT' || code === 'ENOTDIR' || code === 'EISDIR') return sendError(res, 404, 'not found');
      throw err;
    }
    res.writeHead(200, {
      'Content-Type': asset.type,
      'Content-Length': String(body.length),
      'Cache-Control': 'no-cache',
    });
    res.end(body);
  }

  // ---------------------------------------------------------------------------------------------------------------
  // Server-Sent Events

  private openStream(req: http.IncomingMessage, res: http.ServerResponse): void {
    if (this.clients.size >= this.maxSseClients) {
      return sendError(res, 503, 'too many live connections', { 'Retry-After': '10' });
    }
    const initial = stateFrame(this.deps.getState());
    req.socket.setNoDelay(true);
    req.socket.setKeepAlive(true, 30_000);
    res.writeHead(200, {
      'Content-Type': 'text/event-stream; charset=utf-8',
      'Cache-Control': 'no-cache, no-transform',
      Connection: 'keep-alive',
      'X-Accel-Buffering': 'no',
    });
    const client: SseClient = { res, backedUpSince: null, pending: false };
    this.clients.add(client);
    res.on('close', () => {
      this.clients.delete(client);
    });
    res.on('drain', () => this.onDrain(client));
    this.writeTo(client, `retry: ${SSE_RETRY_MS}\n\n${initial}`);
  }

  private isOpen(client: SseClient): boolean {
    const { res } = client;
    if (res.destroyed || res.writableEnded) {
      this.clients.delete(client);
      return false;
    }
    return true;
  }

  private writeTo(client: SseClient, data: string): void {
    if (!this.isOpen(client)) return;
    let flowing: boolean;
    try {
      flowing = client.res.write(data);
    } catch (err) {
      this.dropClient(client, `write failed: ${describeError(err)}`);
      return;
    }
    if (!flowing && client.backedUpSince === null) client.backedUpSince = this.now();
  }

  private onDrain(client: SseClient): void {
    client.backedUpSince = null;
    if (client.pending && this.latestFrame !== null) {
      client.pending = false;
      this.writeTo(client, this.latestFrame);
    }
  }

  private dropClient(client: SseClient, reason: string): void {
    this.clients.delete(client);
    log.debug(`dropping SSE client: ${reason}`, { buffered: client.res.writableLength });
    client.res.destroy();
  }

  private tickHeartbeat(): void {
    try {
      const now = this.now();
      for (const client of [...this.clients]) {
        if (!this.isOpen(client)) continue;
        if (client.res.writableNeedDrain) {
          if (client.backedUpSince === null) client.backedUpSince = now;
          else if (now - client.backedUpSince > this.sseStallMs) this.dropClient(client, 'stalled');
          continue;
        }
        this.writeTo(client, ': ping\n\n');
      }
    } catch (err) {
      log.error('SSE heartbeat failed', { error: describeError(err) });
    }
  }

  // ---------------------------------------------------------------------------------------------------------------
  // JSON API

  private async readJson(req: http.IncomingMessage, res: http.ServerResponse): Promise<JsonBodyResult> {
    if (contentTypeOf(req) !== 'application/json') {
      sendError(res, 415, 'Content-Type must be application/json', { Connection: 'close' });
      return { ok: false };
    }
    const declared = Number(req.headers['content-length']);
    if (Number.isFinite(declared) && declared > MAX_DRAIN_BYTES) {
      sendError(res, 413, `Request body too large (max ${MAX_BODY_BYTES / 1024} KB)`, { Connection: 'close' });
      return { ok: false };
    }
    const body = await readBody(req, MAX_BODY_BYTES, MAX_DRAIN_BYTES);
    if (body.kind === 'aborted') {
      if (!res.writableEnded) res.destroy();
      return { ok: false };
    }
    if (body.kind === 'too-large') {
      sendError(res, 413, `Request body too large (max ${MAX_BODY_BYTES / 1024} KB)`, { Connection: 'close' });
      return { ok: false };
    }
    try {
      return { ok: true, value: JSON.parse(body.text) as unknown };
    } catch {
      sendError(res, 400, 'Invalid JSON body');
      return { ok: false };
    }
  }

  private listBets(res: http.ServerResponse, query: URLSearchParams): void {
    let limit = DEFAULT_BET_LIST_LIMIT;
    const raw = query.get('limit');
    if (raw !== null && raw.trim() !== '') {
      const n = Number(raw);
      if (Number.isInteger(n) && n >= 1) limit = Math.min(n, MAX_BET_LIST_LIMIT);
    }
    const bets = this.deps.journal.list(limit);
    const summary = this.deps.journal.summary(this.now());
    sendJson(res, 200, { bets, summary });
  }

  private async placeBet(req: http.IncomingMessage, res: http.ServerResponse): Promise<void> {
    const parsed = await this.readJson(req, res);
    if (!parsed.ok) return;
    const body = parsed.value;
    if (!isPlainObject(body)) return sendError(res, 400, 'Body must be a JSON object');

    const opportunityId = body.opportunityId;
    if (typeof opportunityId !== 'string' || opportunityId.trim() === '' || opportunityId.length > MAX_OPPORTUNITY_ID_LENGTH) {
      return sendError(res, 400, 'opportunityId is required');
    }
    const stake = toFiniteNumber(body.stake);
    if (stake === null || stake <= 0) return sendError(res, 400, 'Stake must be a positive number');
    const americanTaken = toFiniteNumber(body.americanTaken);
    if (americanTaken === null || Math.abs(americanTaken) < 100) {
      return sendError(res, 400, 'American odds must be a number like +110 or -120 (absolute value at least 100)');
    }
    const notes = body.notes;
    if (notes !== undefined && notes !== null && typeof notes !== 'string') {
      return sendError(res, 400, 'Notes must be text');
    }

    const opp = this.deps.findOpportunity(opportunityId);
    if (!opp) return sendError(res, 404, 'Opportunity not found (it may have expired)');

    const input: PlaceBetInput = {
      opportunityId: opp.id,
      eventId: opp.eventId,
      league: opp.league,
      eventName: opp.eventName,
      startTime: opp.startTime,
      pick: opp.pick,
      kind: opp.kind,
      side: opp.side,
      line: opp.line,
      wasLive: opp.isLive,
      americanTaken,
      stake,
      fairProbAtPlace: opp.fairProb,
    };
    if (typeof notes === 'string' && notes.trim() !== '') input.notes = notes.trim();

    try {
      const record = this.deps.journal.place(input);
      return sendJson(res, 201, record);
    } catch (err) {
      if (isNamedError(err, 'ValidationError')) return sendError(res, 400, errorMessage(err));
      throw err;
    }
  }

  private async settleBet(req: http.IncomingMessage, res: http.ServerResponse, rawId: string): Promise<void> {
    let id: string;
    try {
      id = decodeURIComponent(rawId);
    } catch {
      return sendError(res, 404, 'Unknown bet');
    }
    if (id.length === 0 || id.length > MAX_BET_ID_LENGTH || !BET_ID_PATTERN.test(id)) {
      return sendError(res, 404, 'Unknown bet');
    }
    const parsed = await this.readJson(req, res);
    if (!parsed.ok) return;
    const body = parsed.value;
    if (!isPlainObject(body)) return sendError(res, 400, 'Body must be a JSON object');
    const result = body.result;
    const settled = SETTLE_RESULTS.find((r) => r === result);
    if (settled === undefined) return sendError(res, 400, `Result must be one of ${SETTLE_RESULTS.join(', ')}`);

    try {
      const record = this.deps.journal.settle(id, settled);
      return sendJson(res, 200, record);
    } catch (err) {
      if (isNamedError(err, 'UnknownBetError')) return sendError(res, 404, errorMessage(err));
      if (isNamedError(err, 'ValidationError')) return sendError(res, 400, errorMessage(err));
      throw err;
    }
  }

  private async updateSettings(req: http.IncomingMessage, res: http.ServerResponse): Promise<void> {
    const parsed = await this.readJson(req, res);
    if (!parsed.ok) return;
    if (!isPlainObject(parsed.value)) return sendError(res, 400, 'Settings must be a JSON object');

    let updated: RuntimeSettings;
    try {
      updated = this.deps.settings.update(parsed.value);
    } catch (err) {
      if (isNamedError(err, 'ValidationError')) return sendError(res, 400, errorMessage(err));
      throw err;
    }
    if (this.deps.onSettingsChanged) {
      try {
        this.deps.onSettingsChanged(updated);
      } catch (err) {
        // The new settings are already persisted; report success and log the listener failure.
        log.error('onSettingsChanged listener failed', { error: describeError(err) });
      }
    }
    return sendJson(res, 200, updated);
  }
}

function positiveOr(v: number | undefined, def: number): number {
  return typeof v === 'number' && Number.isFinite(v) && v > 0 ? v : def;
}

function pathOf(url: string | undefined): string {
  if (!url) return '/';
  const q = url.indexOf('?');
  const p = q >= 0 ? url.slice(0, q) : url;
  return p.length > 200 ? `${p.slice(0, 200)}…` : p;
}

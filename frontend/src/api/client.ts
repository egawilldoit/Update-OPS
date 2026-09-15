// Typed fetch client for all /api/v1 endpoints.
// - Same-origin requests with credentials (Cloudflare Access cookie).
// - CSRF: GET /session returns csrf_token; POSTs send it as X-CSRF-Token
//   with Content-Type: application/json (backend rejects mismatches with 403).
// - Error envelope: {code,message,details,request_id} per CONTRACTS section 4.
// - Disconnected: network failure throws DisconnectedError so the UI shows a
//   "disconnected" banner and never renders cached green as current.
// - Internal request deadlines throw RequestTimeoutError. Caller
//   cancellation remains AbortError.
// - 429: backend carries a Retry-After header (read vs mutation rate buckets
//   are separate server-side). ApiError.retryAfterMs preserves it; callers
//   honor Retry-After then exponential backoff (max 30s) and surface
//   reconnect state. Never busy-spin on 429.
// - JobView.final_log_seq: int, -1 unknown until durable flush (main agent).
//   UI drains logs until (terminal && next_after >= final_log_seq &&
//   !has_more); see app.tsx R20 drain.

export interface ErrorBody {
  code: string;
  message: string;
  details: string;
  request_id: string;
}

export class ApiError extends Error {
  status: number;
  body: ErrorBody;
  /** Retry-After in ms when status is 429 and the header parsed; else null. */
  retryAfterMs: number | null;
  constructor(status: number, body: ErrorBody, retryAfterMs: number | null = null) {
    super(body.message || `request failed (${status})`);
    this.status = status;
    this.body = body;
    this.retryAfterMs = retryAfterMs;
  }
}

export class DisconnectedError extends Error {
  constructor() {
    super("API unreachable");
    this.name = "DisconnectedError";
  }
}

export class RequestTimeoutError extends Error {
  constructor() {
    super("Request timed out");
    this.name = "RequestTimeoutError";
  }
}

export interface ToolCard {
  id: string;
  installed_version: string;
  available_target: string;
  channel: string;
  last_success: string;
  last_attempt: string;
  health: "healthy" | "degraded" | "unhealthy" | "unknown" | "stale";
  health_detail: string;
  checked_at: string;
  discovery_error: string;
  install_identity: string;
  /** Last successful observation timestamp (never overwritten by a failed check). */
  last_success_at?: string;
  /** Latest check attempt timestamp, success or failure. */
  attempted_at?: string;
  /** Latest failed attempt reason; last-good observation fields stay intact. */
  attempt_error?: string;
}

export type ToolCheck = ToolCard & { probe_pending?: boolean; probe_request_id?: string };

export interface ProbeView {
  request_id: string;
  tool_id: string;
  state: string;
  pending: boolean;
  status: string;
  observation_applied: boolean;
}

export interface PlanView {
  id: string;
  tool_id: string;
  target: string;
  target_mode: "exact" | "native_latest";
  channel: string;
  fingerprint: string;
  services: string[];
  backup_scope: Record<string, string>;
  activity_state: "idle" | "busy" | "unknown";
  activity_evidence: string;
  required_space_bytes: number;
  steps: string[];
  expires_at: string;
  restart_impact: string;
}

export interface JobView {
  id: string;
  tool_id: string;
  plan_id: string;
  state: string;
  step: string;
  before_version: string;
  after_version: string;
  created_at: string;
  started_at: string;
  finished_at: string;
  exit_code: number;
  error_code: string;
  error_detail: string;
  runner_unit: string;
  recovery_required: boolean;
  backup_summary: string;
  /** Final durable log seq; -1 unknown until durable flush (main agent). */
  final_log_seq: number;
  replayed?: boolean;
}

export interface CheckView {
  name: string;
  result: "pass" | "fail" | "unknown" | "not_applicable";
  mandatory: boolean;
  summary: string;
  created_at: string;
}

export interface JobDetail extends JobView {
  checks: CheckView[];
}

export interface LogRecord {
  seq: number;
  ts: string;
  stream: "stdout" | "stderr" | "event";
  line: string;
}

export interface LogPage {
  records: LogRecord[];
  next_after: number;
  truncated: boolean;
  has_more?: boolean;
}

export interface HistoryPage {
  jobs: JobView[];
  next_cursor: string;
}

export interface SessionView {
  email: string;
  csrf_token: string;
}

export interface HealthView {
  api: "ok" | "degraded" | "down";
  database: "ok" | "down";
  worker: "ok" | "stale" | "down";
  recovery_required: boolean;
  checked_at: string;
  /** Optional: API builds that report the maintenance drain flag directly. */
  drain?: boolean;
  /** Optional alias for the maintenance drain flag. */
  maintenance?: boolean;
}

const BASE = "/api/v1";

/** AbortController timeout for every request (R21): 15s. */
export const REQUEST_TIMEOUT_MS = 15000;
/** Backend owner plan probe deadline in milliseconds (backend: 30s). */
export const BACKEND_OWNER_PLAN_TIMEOUT_MS = 30_000;
/** Plan requests must outlive the backend owner probe deadline. */
export const PLAN_REQUEST_TIMEOUT_MS = BACKEND_OWNER_PLAN_TIMEOUT_MS + 5_000;
/** Backoff ceiling for 429/network retries (R21): 30s. */
export const MAX_BACKOFF_MS = 30000;

let csrfToken = "";
let idemCounter = 0;

export function getCsrfToken(): string {
  return csrfToken;
}

export function setCsrfToken(t: string): void {
  csrfToken = t;
}

export function newIdempotencyKey(): string {
  idemCounter += 1;
  const rand =
    typeof crypto !== "undefined" && "randomUUID" in crypto
      ? crypto.randomUUID()
      : `${Date.now()}-${Math.floor(Math.random() * 1e9)}`;
  return `${rand}-${idemCounter}`;
}

function parseErrorBody(text: string): ErrorBody {
  try {
    const o = JSON.parse(text) as Partial<ErrorBody>;
    return {
      code: typeof o.code === "string" ? o.code : "unavailable",
      message: typeof o.message === "string" ? o.message : "request failed",
      details: typeof o.details === "string" ? o.details : "",
      request_id: typeof o.request_id === "string" ? o.request_id : "",
    };
  } catch {
    return { code: "unavailable", message: "request failed", details: "", request_id: "" };
  }
}

/** Parse a Retry-After header (delay-seconds or HTTP-date) to ms; null when absent/invalid. */
export function parseRetryAfterMs(value: string | null): number | null {
  if (!value) return null;
  const v = value.trim();
  if (!v) return null;
  if (/^\d+$/.test(v)) {
    const secs = parseInt(v, 10);
    if (Number.isFinite(secs) && secs >= 0) return Math.min(secs * 1000, MAX_BACKOFF_MS);
    return null;
  }
  const t = Date.parse(v);
  if (!Number.isNaN(t)) {
    const delta = t - Date.now();
    if (delta <= 0) return 0;
    return Math.min(delta, MAX_BACKOFF_MS);
  }
  return null;
}

/** Exponential backoff honoring Retry-After first, capped at 30s (R21). */
export function backoffMs(attempt: number, retryAfterMs: number | null): number {
  const safeAttempt = Math.max(0, Math.min(10, Math.floor(attempt) || 0));
  const exp = Math.min(MAX_BACKOFF_MS, 1000 * 2 ** safeAttempt);
  if (retryAfterMs !== null && retryAfterMs >= 0) {
    return Math.min(MAX_BACKOFF_MS, Math.max(retryAfterMs, exp));
  }
  return exp;
}

function abortError(): DOMException {
  return new DOMException("Request cancelled", "AbortError");
}

async function req<T>(
  path: string,
  init?: RequestInit,
  timeoutMs = REQUEST_TIMEOUT_MS,
): Promise<T> {
  const externalSignal = init?.signal;
  const controller = new AbortController();
  const abort = () => controller.abort();
  let timedOut = false;
  externalSignal?.addEventListener("abort", abort, { once: true });
  if (externalSignal?.aborted) controller.abort();
  const timer = window.setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);
  try {
    const res = await fetch(`${BASE}${path}`, {
      credentials: "same-origin",
      ...init,
      signal: controller.signal,
    });
    if (!res.ok) {
      const text = await res.text().catch(() => "");
      if (externalSignal?.aborted) throw abortError();
      if (timedOut) throw new RequestTimeoutError();
      const retryAfterMs = res.status === 429 ? parseRetryAfterMs(res.headers.get("Retry-After")) : null;
      throw new ApiError(res.status, parseErrorBody(text), retryAfterMs);
    }
    const body: unknown = await res.json();
    if (externalSignal?.aborted) throw abortError();
    if (timedOut) throw new RequestTimeoutError();
    return body as T;
  } catch (error) {
    if (externalSignal?.aborted) throw abortError();
    if (error instanceof ApiError) throw error;
    if (error instanceof RequestTimeoutError || timedOut) {
      throw error instanceof RequestTimeoutError ? error : new RequestTimeoutError();
    }
    throw new DisconnectedError();
  } finally {
    window.clearTimeout(timer);
    externalSignal?.removeEventListener("abort", abort);
  }
}

function mutationHeaders(extra?: Record<string, string>): Record<string, string> {
  return {
    "Content-Type": "application/json",
    "X-CSRF-Token": csrfToken,
    ...(extra ?? {}),
  };
}

export const api = {
  async getSession(): Promise<SessionView> {
    const s = await req<SessionView>("/session");
    setCsrfToken(s.csrf_token);
    return s;
  },
  getTools(signal?: AbortSignal): Promise<ToolCard[]> {
    return req<ToolCard[]>("/tools", { signal });
  },
  checkTool(toolId: string, force = false, signal?: AbortSignal): Promise<ToolCheck> {
    const suffix = force ? "?force=1" : "";
    return req<ToolCheck>(`/tools/${encodeURIComponent(toolId)}/check${suffix}`, {
      signal,
      method: "POST",
      headers: mutationHeaders(),
      body: JSON.stringify({}),
    });
  },
  getProbe(requestId: string, signal?: AbortSignal): Promise<ProbeView> {
    return req<ProbeView>(`/probes/${encodeURIComponent(requestId)}`, { signal });
  },
  createPlan(toolId: string, signal?: AbortSignal): Promise<PlanView> {
    // Never send ack at plan time: unknown activity is recorded on the
    // plan (201) and acknowledged at POST /jobs.
    return req<PlanView>(`/tools/${encodeURIComponent(toolId)}/plans`, {
      signal,
      method: "POST",
      headers: mutationHeaders(),
      body: JSON.stringify({}),
    }, PLAN_REQUEST_TIMEOUT_MS);
  },
  createJob(planId: string, activityAck: boolean, idempotencyKey: string): Promise<JobView> {
    return req<JobView>("/jobs", {
      method: "POST",
      headers: mutationHeaders({ "Idempotency-Key": idempotencyKey }),
      body: JSON.stringify({ plan_id: planId, activity_ack: activityAck }),
    });
  },
  listJobs(cursor = "", limit = 25): Promise<HistoryPage> {
    const q = new URLSearchParams();
    if (cursor) q.set("cursor", cursor);
    q.set("limit", String(limit));
    return req<HistoryPage>(`/jobs?${q.toString()}`);
  },
  /** Global active-job poll for update gating (R21): nonterminal only, independent of history/detail. */
  listActiveJobs(limit = 25): Promise<HistoryPage> {
    const q = new URLSearchParams();
    q.set("active", "true");
    q.set("limit", String(limit));
    return req<HistoryPage>(`/jobs?${q.toString()}`);
  },
  getJob(jobId: string): Promise<JobDetail> {
    return req<JobDetail>(`/jobs/${encodeURIComponent(jobId)}`);
  },
  getLogs(jobId: string, after = 0, limit = 200): Promise<LogPage> {
    const q = new URLSearchParams();
    q.set("after", String(after));
    q.set("limit", String(limit));
    return req<LogPage>(`/jobs/${encodeURIComponent(jobId)}/logs?${q.toString()}`);
  },
  getHealth(): Promise<HealthView> {
    return req<HealthView>("/health");
  },
};

/** Poll helper: runs fn every intervalMs until aborted. Returns a stopper. */
export function poll(fn: () => void, intervalMs: number): () => void {
  const t = window.setInterval(() => {
    try {
      fn();
    } catch {
      // Poll failures surface through the caller's own error state.
    }
  }, intervalMs);
  return () => window.clearInterval(t);
}

/** Health older than 5 minutes is labeled stale (SPEC section 11). */
export function isStale(checkedAt: string, nowMs = Date.now(), staleAfterMs = 5 * 60 * 1000): boolean {
  if (!checkedAt) return true;
  const t = Date.parse(checkedAt);
  if (Number.isNaN(t)) return true;
  return nowMs - t > staleAfterMs;
}

export function elapsed(startIso: string, endIso: string, nowMs = Date.now()): string {
  const s = Date.parse(startIso);
  if (Number.isNaN(s)) return "—";
  const e = endIso ? Date.parse(endIso) : nowMs;
  const end = Number.isNaN(e) ? nowMs : e;
  const secs = Math.max(0, Math.floor((end - s) / 1000));
  const m = Math.floor(secs / 60);
  const r = secs % 60;
  if (m <= 0) return `${r}s`;
  const h = Math.floor(m / 60);
  if (h <= 0) return `${m}m ${r}s`;
  return `${h}h ${m % 60}m`;
}

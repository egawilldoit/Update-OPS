// Typed fetch client for all 9 /api/v1 endpoints.
// - Same-origin requests with credentials (Cloudflare Access cookie).
// - CSRF: GET /session returns csrf_token; POSTs send it as X-CSRF-Token
//   with Content-Type: application/json (backend rejects mismatches with 403).
// - Error envelope: {code,message,details,request_id} per CONTRACTS section 4.
// - Disconnected: network failure throws DisconnectedError so the UI shows a
//   "disconnected" banner and never renders cached green as current.

export interface ErrorBody {
  code: string;
  message: string;
  details: string;
  request_id: string;
}

export class ApiError extends Error {
  status: number;
  body: ErrorBody;
  constructor(status: number, body: ErrorBody) {
    super(body.message || `request failed (${status})`);
    this.status = status;
    this.body = body;
  }
}

export class DisconnectedError extends Error {
  constructor() {
    super("API unreachable");
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
}

const BASE = "/api/v1";

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

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${BASE}${path}`, {
      credentials: "same-origin",
      ...init,
    });
  } catch {
    throw new DisconnectedError();
  }
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new ApiError(res.status, parseErrorBody(text));
  }
  return (await res.json()) as T;
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
  getTools(): Promise<ToolCard[]> {
    return req<ToolCard[]>("/tools");
  },
  checkTool(toolId: string): Promise<ToolCard> {
    return req<ToolCard>(`/tools/${encodeURIComponent(toolId)}/check`, {
      method: "POST",
      headers: mutationHeaders(),
      body: JSON.stringify({}),
    });
  },
  createPlan(toolId: string): Promise<PlanView> {
    return req<PlanView>(`/tools/${encodeURIComponent(toolId)}/plans`, {
      method: "POST",
      headers: mutationHeaders(),
      body: JSON.stringify({}),
    });
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

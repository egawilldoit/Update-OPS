import { ApiError, DisconnectedError, isStale, type HealthView, type JobView, type ToolCard } from "./api/client";
import type { CheckState } from "./useToolChecks";

export const TOOL_NAMES: Record<string, string> = {
  hermes: "Hermes",
  opencode: "OpenCode",
  codex: "Codex",
  t3: "T3",
};

export const TOOL_ORDER = ["hermes", "opencode", "codex", "t3"];

export const FAILED_JOB_STATES = ["blocked", "failed", "health_failed", "interrupted"];

export function toolName(id: string): string {
  return TOOL_NAMES[id] ?? id;
}

export type MaintenanceState =
  | { kind: "unknown" }
  | { kind: "clear" }
  | { kind: "active"; source: "health" | "api"; evidence: string };

export function maintenanceFromHealth(health: HealthView | null, previous: MaintenanceState): MaintenanceState {
  if (!health) return previous;
  const flags: Array<boolean | undefined> = [health.drain, health.maintenance];
  for (const flag of flags) {
    if (typeof flag === "boolean") {
      return flag
        ? { kind: "active", source: "health", evidence: health.checked_at }
        : { kind: "clear" };
    }
  }
  return previous;
}

export function maintenanceFromError(error: unknown): MaintenanceState | null {
  if (error instanceof ApiError && error.status === 503 && error.body.code === "maintenance") {
    return { kind: "active", source: "api", evidence: error.body.message };
  }
  return null;
}

export function maintenanceLabel(state: MaintenanceState): string {
  if (state.kind === "active") return "maintenance";
  if (state.kind === "clear") return "normal";
  return "unknown";
}

export function maintenanceDetail(state: MaintenanceState): string {
  if (state.kind === "active") {
    return state.source === "health"
      ? "Drain flag reported by the API — new plans and updates are refused until it clears."
      : "The API refused a request because the console is drained for maintenance.";
  }
  if (state.kind === "clear") return "No maintenance drain is active.";
  return "This API build does not report the drain flag; maintenance is detected when a plan or update is refused.";
}

export type Tone = "ok" | "warn" | "bad" | "neutral";

export interface OutcomeView {
  label: string;
  tone: Tone;
  sentence: string;
}

export function jobOutcome(state: string): OutcomeView {
  switch (state) {
    case "succeeded":
      return { label: "Succeeded", tone: "ok", sentence: "The update completed and verification passed." };
    case "failed":
      return { label: "Failed", tone: "bad", sentence: "The installer reported a failure. The previous version may remain installed." };
    case "health_failed":
      return { label: "Health check failed", tone: "bad", sentence: "The installer finished but a required health check did not pass." };
    case "blocked":
      return { label: "Blocked", tone: "warn", sentence: "A precondition stopped the update before it was applied." };
    case "interrupted":
      return { label: "Interrupted", tone: "warn", sentence: "The update did not finish. Check the tool state before retrying." };
    case "accepted":
      return { label: "Queued", tone: "neutral", sentence: "The job is accepted and waiting for the worker." };
    case "preflight":
      return { label: "Preflight checks", tone: "neutral", sentence: "Required preflight checks are running." };
    case "backup":
      return { label: "Backing up", tone: "neutral", sentence: "Backup and snapshot steps are running." };
    case "updating":
      return { label: "Updating", tone: "neutral", sentence: "The tool is being updated now." };
    case "verifying":
      return { label: "Verifying", tone: "neutral", sentence: "The update finished; health and version checks are running." };
    default:
      return { label: state || "Unknown", tone: "neutral", sentence: "State not reported by this API build." };
  }
}

export function lastFailureByTool(jobs: JobView[]): Record<string, JobView> {
  const out: Record<string, JobView> = {};
  for (const job of jobs) {
    if (!FAILED_JOB_STATES.includes(job.state)) continue;
    const previous = out[job.tool_id];
    const at = job.finished_at || job.created_at || "";
    const previousAt = previous ? previous.finished_at || previous.created_at || "" : "";
    if (!previous || at > previousAt) out[job.tool_id] = job;
  }
  return out;
}

export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "";
  const gb = bytes / 1024 ** 3;
  if (gb >= 1) return `${gb.toFixed(1)} GB`;
  const mb = bytes / 1024 ** 2;
  if (mb >= 1) return `${Math.round(mb)} MB`;
  return `${Math.round(bytes)} B`;
}

function requiredBytesFromDetail(detail: string): number {
  const match = /required\s+(\d+)/i.exec(detail || "");
  if (!match) return 0;
  const parsed = Number(match[1]);
  return Number.isFinite(parsed) ? parsed : 0;
}

export interface FailureView {
  title: string;
  message: string;
  action: string;
  code: string;
}

export function describeFailure(code: string, message = "", detail = ""): FailureView {
  const normalized = (code || "").trim();
  const serverMessage = (message || "").trim();
  const serverDetail = (detail || "").trim();
  switch (normalized) {
    case "maintenance":
      return {
        title: "Maintenance mode",
        message: serverMessage || "New update plans are disabled while the console is drained for maintenance.",
        action: "Wait for maintenance to finish. Read-only checks, health, and history remain available.",
        code: normalized,
      };
    case "recovery_required":
      return {
        title: "Recovery required",
        message: serverMessage || "A previous update may have left the tool in a partial state.",
        action: "Reconcile over SSH, then run Check again before planning another update.",
        code: normalized,
      };
    case "manual_prerequisite_required":
      return {
        title: "Manual prerequisite",
        message: serverMessage || "This tool must be stopped manually before an update plan can be created.",
        action: `Stop the tool manually, then create the plan again.${serverDetail ? ` Restart impact: ${serverDetail}` : ""}`,
        code: normalized,
      };
    case "disk_blocked":
    case "insufficient_space": {
      const required = requiredBytesFromDetail(serverDetail);
      return {
        title: "Insufficient disk space",
        message: required > 0
          ? `Requires ~${formatBytes(required)} available on the affected filesystem.`
          : serverDetail || serverMessage || "The filesystem does not have enough free space for this update.",
        action: "Free disk space, then create the plan again.",
        code: normalized,
      };
    }
    case "activity_blocked":
    case "busy":
      return {
        title: "Tool is busy",
        message: serverMessage || "The tool reports active work, so a new update is blocked for now.",
        action: "Wait for the active work to finish, then create the plan again.",
        code: normalized,
      };
    case "worker_unavailable":
      return {
        title: "Worker unavailable",
        message: serverMessage || "The update worker is not ready, so no job was accepted.",
        action: "Wait for the worker to come back, then retry.",
        code: normalized,
      };
    case "stale_plan":
      return {
        title: "Observation is stale",
        message: serverMessage || "The last check is too old or incomplete for a safe plan.",
        action: "Run Check again, then create the plan.",
        code: normalized,
      };
    case "fingerprint_changed":
      return {
        title: "Installation changed",
        message: serverMessage || "The installed tool changed since it was checked.",
        action: "Run Check again, then create a fresh plan.",
        code: normalized,
      };
    case "config_changed":
      return {
        title: "Configuration changed",
        message: serverMessage || "The console configuration changed since the plan was created.",
        action: "Create a fresh plan from the current state.",
        code: normalized,
      };
    case "rate_limited":
      return {
        title: "Rate limited",
        message: serverMessage || "Too many requests right now.",
        action: "Wait a few seconds, then retry.",
        code: normalized,
      };
    case "unavailable":
      return {
        title: "Service unavailable",
        message: serverMessage || "The console could not complete the request.",
        action: "Retry in a moment; check the control plane if this persists.",
        code: normalized,
      };
    case "not_found":
      return {
        title: "Not found",
        message: serverMessage || "The requested item does not exist.",
        action: "Return to the dashboard and run Check again.",
        code: normalized,
      };
    case "unauthenticated":
    case "forbidden":
      return {
        title: "Session ended",
        message: serverMessage || "The console session is no longer valid.",
        action: "Reload the page and sign in again.",
        code: normalized,
      };
    default:
      return {
        title: "Update blocked",
        message: serverMessage || serverDetail || "The request was refused.",
        action: "Review the details, then retry.",
        code: normalized || "unknown",
      };
  }
}

export function describeJobFailure(job: JobView): FailureView {
  const detail = job.error_detail || "";
  const code = job.error_code || (job.state === "health_failed" ? "health_check_failed" : "");
  if (!code) {
    return {
      title: jobOutcome(job.state).label,
      message: jobOutcome(job.state).sentence,
      action: "Open the job for logs and checks.",
      code: "",
    };
  }
  const view = describeFailure(code, detail ? "" : jobOutcome(job.state).sentence, detail);
  if (job.state === "health_failed") {
    return { ...view, title: "Health check failed", message: detail || view.message };
  }
  return view;
}

export function planErrorFromException(error: unknown): FailureView {
  if (error instanceof DisconnectedError) {
    return {
      title: "Disconnected",
      message: "The API is unreachable, so the plan could not be created.",
      action: "Restore the connection and retry. Cached values are not shown as current.",
      code: "disconnected",
    };
  }
  if (error instanceof ApiError) {
    return describeFailure(error.body.code, error.body.message, error.body.details);
  }
  return {
    title: "Unexpected error",
    message: error instanceof Error ? error.message : "The plan request failed.",
    action: "Retry the plan; if it persists, check the control plane.",
    code: "unexpected",
  };
}

function humanAttemptError(reason: string): string {
  const value = (reason || "").trim();
  if (!value) return "";
  if (value.includes("probe_result_too_large")) {
    return "The check result was too large to store, so the tool was not re-verified. Last known values are retained.";
  }
  if (/timed out/i.test(value)) {
    return "The check timed out before the tool responded. Last known values are retained.";
  }
  if (/unusable/i.test(value)) {
    return "The latest check returned no usable observation. Last known values are retained.";
  }
  return value.charAt(0).toUpperCase() + value.slice(1) + (value.endsWith(".") ? "" : ".");
}

export type CheckKind = "idle" | "checking" | "pending" | "failed" | "never";

export interface CheckView {
  kind: CheckKind;
  label: string;
  detail: string;
  alert: boolean;
  code: string;
  at: string;
}

export function checkPresentation(card: ToolCard, checkState: CheckState | undefined, now = Date.now()): CheckView {
  if (checkState?.kind === "checking") {
    return {
      kind: "checking",
      label: "Checking…",
      detail: "The request was sent to the console. Waiting for the tool response.",
      alert: false,
      code: "",
      at: "",
    };
  }
  if (checkState?.kind === "pending") {
    return {
      kind: "pending",
      label: "Checking…",
      detail: "Result will continue in the background. You can leave this page; polling stays scoped to this tool.",
      alert: false,
      code: "",
      at: "",
    };
  }
  if (checkState?.kind === "error") {
    return {
      kind: "failed",
      label: "Check failed",
      detail: checkState.message,
      alert: true,
      code: checkState.message,
      at: "",
    };
  }
  const attemptError = card.attempt_error || "";
  const attemptedAt = card.attempted_at || "";
  const lastGood = card.checked_at || card.last_success_at || "";
  if (attemptError && (!lastGood || attemptedAt > lastGood)) {
    return {
      kind: "failed",
      label: "Check failed",
      detail: `Last known installed version ${card.installed_version || "unknown"} retained. Latest attempt: ${humanAttemptError(attemptError)}`,
      alert: false,
      code: attemptError,
      at: attemptedAt,
    };
  }
  if (attemptedAt && (!lastGood || attemptedAt > lastGood)) {
    return {
      kind: "idle",
      label: "Check succeeded",
      detail: `Latest attempt completed ${formatTimestamp(attemptedAt)}.`,
      alert: false,
      code: "",
      at: attemptedAt,
    };
  }
  if (!lastGood) {
    return {
      kind: "never",
      label: "Never checked",
      detail: "Run Check again to discover the installed version and health.",
      alert: false,
      code: "",
      at: attemptedAt,
    };
  }
  if (isStale(lastGood, now)) {
    return {
      kind: "idle",
      label: "Check stale",
      detail: "The last observation is older than 5 minutes. Run Check again for current values.",
      alert: false,
      code: "",
      at: lastGood,
    };
  }
  return {
    kind: "idle",
    label: "Check fresh",
    detail: `Last successful check ${formatTimestamp(lastGood)}.`,
    alert: false,
    code: "",
    at: lastGood,
  };
}

export function formatTimestamp(iso: string): string {
  if (!iso) return "never";
  return iso;
}

export function versionSummary(card: ToolCard): string {
  const installed = card.installed_version || "";
  const target = card.available_target || "";
  if (!installed && !target) return "Version unknown — run Check again.";
  if (!target) return `Installed ${installed || "unknown"} — no update target discovered.`;
  if (!installed) return `Target ${target} discovered — installed version unknown.`;
  if (installed === target) return `Up to date — installed ${installed}.`;
  return `Update available — ${installed} → ${target}.`;
}

export interface ToolView {
  id: string;
  name: string;
  installed: string;
  target: string;
  channel: string;
  health: string;
  healthDetail: string;
  stale: boolean;
  summary: string;
  check: CheckView;
  lastSuccess: string;
  lastFailure?: { at: string; outcome: OutcomeView; reason: FailureView };
  eligibility: { state: "ready" | "blocked" | "needs-check"; label: string; reason: string; action: string };
}

export function buildToolView(options: {
  card: ToolCard;
  checkState?: CheckState;
  updateBlocked: boolean;
  blockedReason: string;
  lastFailure?: JobView;
  disconnected?: boolean;
  now?: number;
}): ToolView {
  const { card, checkState, updateBlocked, blockedReason, lastFailure, disconnected } = options;
  const now = options.now ?? Date.now();
  const forcedStale = disconnected === true;
  const stale = forcedStale || isStale(card.checked_at || card.last_success_at || "", now);
  const health = forcedStale ? "stale" : stale && card.health !== "stale" ? "stale" : card.health || "unknown";
  const check = checkPresentation(card, checkState, now);
  const failure = lastFailure
    ? {
        at: lastFailure.finished_at || lastFailure.created_at || "",
        outcome: jobOutcome(lastFailure.state),
        reason: describeJobFailure(lastFailure),
      }
    : undefined;
  let eligibility: ToolView["eligibility"];
  if (forcedStale) {
    eligibility = {
      state: "blocked",
      label: "Status not current",
      reason: "Disconnected — the API is unreachable, so no update should be planned.",
      action: "Reconnect and run Check again before planning.",
    };
  } else if (updateBlocked) {
    eligibility = {
      state: "blocked",
      label: "Update blocked",
      reason: blockedReason || "Updates are disabled right now.",
      action: "Resolve the blocking condition, then plan the update.",
    };
  } else if (!card.installed_version || !card.available_target) {
    eligibility = {
      state: "needs-check",
      label: "Check required",
      reason: !card.installed_version
        ? "The installed version has not been observed."
        : "No update target has been discovered yet.",
      action: "Run Check again, then plan the update.",
    };
  } else if (check.kind === "failed" || check.kind === "never") {
    eligibility = {
      state: "needs-check",
      label: "Check required",
      reason: "The latest check did not produce a fresh observation.",
      action: "Run Check again so the plan binds to current state.",
    };
  } else {
    eligibility = {
      state: "ready",
      label: "Update allowed",
      reason: "No active job, recovery flag, or maintenance drain blocks a new plan.",
      action: stale
        ? "Check again first so the plan binds to a current observation."
        : `Plan update to ${card.available_target}.`,
    };
  }
  return {
    id: card.id,
    name: toolName(card.id),
    installed: card.installed_version || "unknown",
    target: card.available_target || "unknown",
    channel: card.channel || "—",
    health,
    healthDetail: forcedStale
      ? "stale — disconnected: cached health is not shown as current."
      : card.health_detail || "No health detail reported by the latest observation.",
    stale,
    summary: versionSummary(card),
    check,
    lastSuccess: card.last_success || "",
    lastFailure: failure,
    eligibility,
  };
}

export function globalUpdateGate(options: {
  maintenance: MaintenanceState;
  recoveryRequired: boolean;
  activeJob?: JobView;
  disconnected: boolean;
}): { blocked: boolean; reason: string; action: string } {
  const { maintenance, recoveryRequired, activeJob, disconnected } = options;
  if (maintenance.kind === "active") {
    return {
      blocked: true,
      reason: "Maintenance mode — new update plans are disabled while the console is drained.",
      action: "Wait for maintenance to finish. Read-only checks, health, and history remain available.",
    };
  }
  if (recoveryRequired) {
    return {
      blocked: true,
      reason: "Recovery required — a previous update may have left a tool in a partial state.",
      action: "Reconcile over SSH, then run Check again.",
    };
  }
  if (activeJob) {
    const outcome = jobOutcome(activeJob.state);
    return {
      blocked: true,
      reason: `Update in progress — ${toolName(activeJob.tool_id)} is currently ${outcome.label.toLowerCase()}.`,
      action: "Wait for the active job to finish, then plan the next update.",
    };
  }
  if (disconnected) {
    return {
      blocked: true,
      reason: "Disconnected — status is not current, so planning an update is disabled.",
      action: "Restore the connection, then check again.",
    };
  }
  return { blocked: false, reason: "", action: "Select a tool card to plan its update." };
}

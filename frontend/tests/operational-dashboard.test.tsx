// @vitest-environment jsdom
import { readFileSync } from "node:fs";
import path from "node:path";
import React from "react";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { App } from "../src/app";
import type { JobView, PlanView, ToolCard } from "../src/api/client";

const NOW = "2026-09-14T12:00:00.000Z";

function card(id: string, overrides: Partial<ToolCard> = {}): ToolCard {
  return {
    id,
    installed_version: "1.0.0",
    available_target: "1.1.0",
    channel: "stable",
    last_success: "",
    last_attempt: "",
    health: "healthy",
    health_detail: "",
    checked_at: NOW,
    discovery_error: "",
    install_identity: id,
    ...overrides,
  };
}

function job(id: string, overrides: Partial<JobView> = {}): JobView {
  return {
    id,
    tool_id: "hermes",
    plan_id: "plan-1",
    state: "succeeded",
    step: "verifying",
    before_version: "1.0.0",
    after_version: "1.1.0",
    created_at: "2026-09-10T09:00:00Z",
    started_at: "2026-09-10T09:00:00Z",
    finished_at: "2026-09-10T09:05:00Z",
    exit_code: 0,
    error_code: "",
    error_detail: "",
    runner_unit: "unit",
    recovery_required: false,
    backup_summary: "",
    final_log_seq: 10,
    ...overrides,
  };
}

function plan(id: string): PlanView {
  return {
    id,
    tool_id: id,
    target: `${id}-target`,
    target_mode: "exact",
    channel: "stable",
    fingerprint: "f",
    services: [],
    backup_scope: {},
    activity_state: "idle",
    activity_evidence: "",
    required_space_bytes: 8700000000,
    steps: ["preflight", "backup", "updating", "verifying"],
    expires_at: "2026-09-14T12:05:00.000Z",
    restart_impact: "",
  };
}

interface Pending {
  path: string;
  signal: AbortSignal | null | undefined;
  resolve: (value: Response) => void;
}

let pending: Pending[];
let tools: ToolCard[];
let jobs: JobView[];
let activeJobs: JobView[];
let health: Record<string, unknown>;
let probeBody: Record<string, unknown>;

function fetchStub(input: string, init?: RequestInit): Promise<Response> {
  let body: unknown;
  if (input.endsWith("/session")) body = { email: "owner@example.test", csrf_token: "fixture" };
  else if (input.endsWith("/health")) body = health;
  else if (input.endsWith("/tools")) body = tools;
  else if (input.includes("active=true")) body = { jobs: activeJobs, next_cursor: "" };
  else if (input.includes("/jobs?")) body = { jobs, next_cursor: "" };
  else {
    return new Promise<Response>((resolve) => pending.push({ path: input, signal: init?.signal, resolve }));
  }
  return Promise.resolve(new Response(JSON.stringify(body)));
}

async function boot(): Promise<void> {
  render(<App />);
  await act(async () => {});
}

async function tick(ms: number): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

async function respond(path: string, body: unknown, status = 200): Promise<void> {
  const index = pending.findIndex((p) => p.path === path);
  expect(index, `pending ${path}`).toBeGreaterThanOrEqual(0);
  const [request] = pending.splice(index, 1);
  await act(async () => {
    request.resolve(new Response(JSON.stringify(body), { status }));
  });
  await act(async () => {});
}

const checkPath = (id: string) => `/api/v1/tools/${id}/check?force=1`;
const planPath = (id: string) => `/api/v1/tools/${id}/plans`;
const hermesCard = () => within(screen.getByRole("article", { name: "Hermes status card" }));
const fact = (label: string): HTMLElement => {
  const dt = hermesCard().getByText(label);
  const row = dt.closest("div");
  if (!row) throw new Error(`no fact row for ${label}`);
  return row;
};

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date(NOW));
  window.localStorage.clear();
  pending = [];
  probeBody = { request_id: "p1", tool_id: "hermes", state: "running", pending: true, status: "", observation_applied: false };
  health = { api: "ok", database: "ok", worker: "ok", recovery_required: false, checked_at: NOW };
  tools = [card("hermes"), card("opencode"), card("codex"), card("t3")];
  jobs = [];
  activeJobs = [];
  vi.stubGlobal("fetch", vi.fn(fetchStub));
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

it("shows a prominent maintenance banner and disables planning while drained", async () => {
  health = { ...health, drain: true };
  await boot();
  const banner = screen.getByRole("status", { name: "Maintenance mode" });
  expect(within(banner).getByText(/New update plans are disabled/)).toBeTruthy();
  expect(within(banner).getByText(/Read-only checks, health, and history remain available/)).toBeTruthy();
  expect(screen.queryByRole("alert")).toBeNull();
  expect(within(screen.getByRole("region", { name: "Update gate" })).getByText("Update blocked")).toBeTruthy();
  for (const name of ["Hermes", "OpenCode", "Codex", "T3"]) {
    expect(screen.getByRole("button", { name: `Preview update for ${name}` }).hasAttribute("disabled")).toBe(true);
    expect(screen.getByRole("button", { name: `Check ${name} again` }).hasAttribute("disabled")).toBe(false);
  }
});

it("detects maintenance from a refused plan request and keeps the banner", async () => {
  await boot();
  fireEvent.click(screen.getByRole("button", { name: "Preview update for Hermes" }));
  await respond(planPath("hermes"), { code: "maintenance", message: "console is drained for maintenance; new plans are refused" }, 503);
  expect(screen.getByRole("status", { name: "Maintenance mode" })).toBeTruthy();
  expect(screen.getAllByText(/Maintenance mode/).length).toBeGreaterThan(0);
  expect(screen.getAllByText(/Wait for maintenance to finish/).length).toBeGreaterThan(0);
  fireEvent.click(screen.getByRole("button", { name: "Overview" }));
  expect(within(screen.getByRole("region", { name: "Update gate" })).getByText("Update blocked")).toBeTruthy();
  expect(screen.getByRole("button", { name: "Preview update for Hermes" }).hasAttribute("disabled")).toBe(true);
});

it("keeps health and last known version when the latest check failed", async () => {
  tools = [
    card("hermes", {
      installed_version: "0.21.1",
      available_target: "0.22.0",
      health: "healthy",
      checked_at: "2026-09-14T11:56:00Z",
      last_success_at: "2026-09-14T11:56:00Z",
      attempted_at: "2026-09-14T11:59:00Z",
      attempt_error: "probe_result_too_large",
    }),
  ];
  await boot();
  const view = hermesCard();
  expect(view.getByText("0.21.1")).toBeTruthy();
  expect(view.getByText("healthy")).toBeTruthy();
  expect(view.getByText("Check failed")).toBeTruthy();
  expect(view.getByText(/too large to store/)).toBeTruthy();
  expect(view.getByText(/Last known installed version 0.21.1 retained/)).toBeTruthy();
  expect(within(fact("Last successful check")).getByText("2026-09-14T11:56:00Z")).toBeTruthy();
  expect(within(fact("Latest check")).getByText(/Check failed/)).toBeTruthy();
});

it("keeps a pending check visually distinct from a running update job", async () => {
  activeJobs = [
    job("job-77777777", {
      tool_id: "opencode",
      state: "updating",
      created_at: NOW,
      started_at: NOW,
      finished_at: "",
      final_log_seq: -1,
    }),
  ];
  await boot();
  fireEvent.click(screen.getByRole("button", { name: "Check Hermes again" }));
  await respond(checkPath("hermes"), { ...card("hermes"), probe_pending: true, probe_request_id: "p1" });
  const hermes = hermesCard();
  expect(hermes.getByText(/Result will continue in the background/)).toBeTruthy();
  expect(hermes.getByText(/You can leave this page; polling stays scoped to this tool/)).toBeTruthy();
  expect(hermes.queryByText(/Updating/)).toBeNull();
  const operation = screen.getByRole("region", { name: "Active update operation" });
  expect(within(operation).getAllByText(/Updating/).length).toBeGreaterThan(0);
  expect(screen.getByRole("button", { name: "History" }).hasAttribute("disabled")).toBe(false);
  await tick(1000);
  await respond("/api/v1/probes/p1", { ...probeBody, pending: false, state: "completed", status: "ok", observation_applied: true });
  expect(hermesCard().queryByText(/Result will continue in the background/)).toBeNull();
});

it("explains the T3 manual prerequisite instead of dumping the code", async () => {
  await boot();
  fireEvent.click(screen.getByRole("button", { name: "Preview update for T3" }));
  await respond(planPath("t3"), {
    code: "manual_prerequisite_required",
    message: "T3 must be stopped manually before an update plan can be created",
    details: "managed user service t3-agent pinned to 1.3.0",
  }, 409);
  expect(screen.getByText(/Manual prerequisite/)).toBeTruthy();
  expect(screen.getByText(/T3 must be stopped manually before an update plan can be created/)).toBeTruthy();
  expect(screen.getByText(/Stop the tool manually, then create the plan again/)).toBeTruthy();
  const details = screen.getByText("Diagnostic code").closest("details");
  expect(details?.textContent).toContain("manual_prerequisite_required");
});

it("shows a human reason for a blocked update from history", async () => {
  jobs = [
    job("job-blocked-1", {
      state: "blocked",
      error_code: "disk_blocked",
      error_detail: "disk_blocked: / free 1000000000 < required 8700000000",
      finished_at: "2026-09-13T10:00:00Z",
    }),
  ];
  await boot();
  expect(hermesCard().getByText(/Blocked — Insufficient disk space/)).toBeTruthy();
  expect(hermesCard().getByText(/Requires ~8.1 GB available/)).toBeTruthy();
});

it("blocks planning globally when recovery is required", async () => {
  health = { ...health, recovery_required: true, drain: false };
  await boot();
  expect(screen.getAllByText(/Recovery required/).length).toBeGreaterThan(0);
  expect(screen.getAllByText(/SSH/).length).toBeGreaterThan(0);
  expect(within(screen.getByRole("region", { name: "Update gate" })).getByText("Update blocked")).toBeTruthy();
  expect(screen.getByRole("button", { name: "Preview update for Hermes" }).hasAttribute("disabled")).toBe(true);
});

it("displays the last successful update separately from the last failure", async () => {
  tools = [card("hermes", { installed_version: "2.0.0", available_target: "2.0.0", last_success: "2026-09-10T09:00:00Z" })];
  jobs = [
    job("job-ok-1", { before_version: "1.9.0", after_version: "2.0.0", created_at: "2026-09-10T09:00:00Z", finished_at: "2026-09-10T09:05:00Z" }),
    job("job-fail-1", {
      state: "failed",
      before_version: "1.9.0",
      after_version: "",
      created_at: "2026-09-12T08:00:00Z",
      started_at: "2026-09-12T08:00:00Z",
      finished_at: "2026-09-12T08:01:00Z",
      error_code: "",
      error_detail: "",
    }),
  ];
  await boot();
  expect(within(fact("Last successful update")).getByText("2026-09-10T09:00:00Z")).toBeTruthy();
  expect(within(fact("Last failed attempt")).getByText(/Failed: The installer reported a failure/)).toBeTruthy();
  expect(hermesCard().getByText(/Up to date — installed 2.0.0/)).toBeTruthy();
});

it("labels stale observations without hiding the last confirmed values", async () => {
  tools = [
    card("hermes", {
      installed_version: "3.4.5",
      checked_at: "2026-09-14T11:00:00Z",
      last_success_at: "2026-09-14T11:00:00Z",
      health: "healthy",
    }),
  ];
  await boot();
  const view = hermesCard();
  expect(view.getByText("stale")).toBeTruthy();
  expect(view.getByText("3.4.5")).toBeTruthy();
  expect(view.getByText(/Observation is stale/)).toBeTruthy();
  expect(view.getByText(/last confirmed state, not current/)).toBeTruthy();
});

it("keeps every key action keyboard reachable and ships responsive styles", async () => {
  await boot();
  for (const name of ["Hermes", "OpenCode", "Codex", "T3"]) {
    expect(screen.getByRole("button", { name: `Check ${name} again` })).toBeTruthy();
    expect(screen.getByRole("button", { name: `Preview update for ${name}` })).toBeTruthy();
  }
  expect(screen.getByRole("link", { name: "Skip to main content" })).toBeTruthy();
  expect(screen.getByRole("navigation", { name: "Primary" })).toBeTruthy();
  const css = readFileSync(path.join(process.cwd(), "src", "styles", "app.css"), "utf8");
  expect(css).toContain("@media (max-width: 460px)");
  expect(css).toContain("@media (prefers-reduced-motion: reduce)");
  expect(css).toContain(":focus-visible");
  expect(css).toContain(".tool-actions button");
});

it("does not mutate anything when only reviewing a plan", async () => {
  await boot();
  fireEvent.click(screen.getByRole("button", { name: "Preview update for Hermes" }));
  await respond(planPath("hermes"), plan("hermes"));
  expect(screen.getByText(/hermes-target/)).toBeTruthy();
  expect(screen.getByText(/Requires ~8.1 GB free/)).toBeTruthy();
  expect(pending.filter((p) => p.path === "/api/v1/jobs")).toHaveLength(0);
  expect(screen.getByRole("button", { name: "Start update for Hermes" })).toBeTruthy();
});

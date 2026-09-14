// @vitest-environment jsdom
import React from "react";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { App } from "../src/app";
import type { PlanView, ToolCard } from "../src/api/client";

function card(id: string, version = "1"): ToolCard {
  return { id, installed_version: version, available_target: "2", channel: "stable", last_success: "", last_attempt: "", health: "healthy", health_detail: "", checked_at: new Date().toISOString(), discovery_error: "", install_identity: id };
}
function plan(id: string): PlanView {
  return { id, tool_id: id, target: `${id}-target`, target_mode: "exact", channel: "stable", fingerprint: "f", services: [], backup_scope: {}, activity_state: "idle", activity_evidence: "", required_space_bytes: 0, steps: [], expires_at: "later", restart_impact: "" };
}
interface Pending { path: string; signal: AbortSignal | null | undefined; resolve: (value: Response) => void }
let pending: Pending[];
let tools: ToolCard[];
let jobs: unknown[];
function respond(path: string, body: unknown, status = 200): Promise<void> {
  const index = pending.findIndex((p) => p.path === path);
  expect(index, `pending ${path}`).toBeGreaterThanOrEqual(0);
  const [request] = pending.splice(index, 1);
  if (status === 200 && path.includes("/check") && typeof body === "object" && body !== null && "id" in body && "installed_version" in body) {
    tools = tools.map(c => c.id === body.id ? { ...c, installed_version: String(body.installed_version) } : c);
  }
  return act(async () => { request.resolve(new Response(JSON.stringify(body), { status })); });
}
const check = (id: string) => `/api/v1/tools/${id}/check?force=1`;
const plans = (id: string) => `/api/v1/tools/${id}/plans`;
const clickCheck = (id: string) => fireEvent.click(screen.getByRole("button", { name: `Check ${id === "hermes" ? "Hermes" : "OpenCode"} again` }));
const clickPlan = (id: string) => fireEvent.click(screen.getByRole("button", { name: `Preview update for ${id === "hermes" ? "Hermes" : "OpenCode"}` }));
const overview = () => fireEvent.click(screen.getByRole("button", { name: "Overview" }));
const checking = (name: string) => screen.getByRole("button", { name: `Check ${name} again` }).hasAttribute("disabled");
async function boot() { render(<App />); await act(async () => {}); }
async function tick(ms = 1000) { await act(async () => { await vi.advanceTimersByTimeAsync(ms); }); }
const probe = (id: string, overrides = {}) => ({ request_id: id, tool_id: "hermes", op: "check", state: "running", pending: true, status: "", observation_applied: false, result: {}, ...overrides });
beforeEach(() => {
  vi.useFakeTimers(); pending = []; tools = [card("hermes"), card("opencode")]; jobs = []; localStorage.clear();
  vi.stubGlobal("fetch", vi.fn((input: string, init?: RequestInit) => {
    let body: unknown;
    if (input.endsWith("/session")) body = { email: "owner@example.test", csrf_token: "fixture" };
    else if (input.endsWith("/health")) body = { api: "ok", database: "ok", worker: "ok", recovery_required: false, checked_at: "" };
    else if (input.endsWith("/tools")) body = tools;
    else if (input.includes("/jobs?")) body = { jobs, next_cursor: "" };
    else return new Promise<Response>((resolve) => pending.push({ path: input, signal: init?.signal, resolve }));
    return Promise.resolve(new Response(JSON.stringify(body)));
  }));
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); });
it("checks different tools independently and handles B then A", async () => {
  await boot(); clickCheck("hermes"); clickCheck("opencode");
  expect(checking("Hermes")).toBe(true); expect(checking("OpenCode")).toBe(true);
  await respond(check("opencode"), card("opencode", "B"));
  expect(checking("Hermes")).toBe(true); expect(checking("OpenCode")).toBe(false);
  await respond(check("hermes"), card("hermes", "A"));
  expect(screen.getByText("A")).toBeTruthy(); expect(screen.getByText("B")).toBeTruthy();
});
it("ignores plan A after plan B succeeds", async () => {
  await boot(); clickPlan("opencode"); overview(); clickPlan("hermes");
  await respond(plans("hermes"), plan("hermes")); await respond(plans("opencode"), plan("opencode"));
  expect(screen.queryByText(/opencode-target/)).toBeNull(); expect(screen.getByText(/hermes-target/)).toBeTruthy();
});
it("continues pending probe until observation is applied", async () => {
  await boot(); clickCheck("hermes");
  await respond(check("hermes"), { ...card("hermes"), probe_pending: true, probe_request_id: "p1" });
  expect(checking("Hermes")).toBe(true);
  await tick(); await respond("/api/v1/probes/p1", probe("p1", { pending: false, state: "completed", status: "ok" }));
  expect(checking("Hermes")).toBe(true);
  tools = [card("hermes", "fresh"), card("opencode")];
  await tick(2000); await respond("/api/v1/probes/p1", probe("p1", { pending: false, state: "completed", status: "ok", observation_applied: true }));
  expect(checking("Hermes")).toBe(false); expect(screen.getByText("fresh")).toBeTruthy();
  await tick(30000); expect(pending.filter(p => p.path.includes("/probes/"))).toHaveLength(0);
});
it("suppresses same-tool double clicks", async () => {
  await boot(); clickCheck("hermes"); clickCheck("hermes");
  expect(pending.filter(p => p.path === check("hermes"))).toHaveLength(1);
  await respond(check("hermes"), card("hermes", "new")); expect(screen.getByText("new")).toBeTruthy();
});
it("ignores a late failed plan after another tool succeeds", async () => {
  await boot(); clickPlan("opencode"); overview(); clickPlan("hermes");
  await respond(plans("hermes"), plan("hermes"));
  await respond(plans("opencode"), { code: "late", message: "old failure" }, 409);
  expect(screen.queryByText(/old failure/)).toBeNull(); expect(screen.getByText(/hermes-target/)).toBeTruthy();
});
it("invalidates a plan immediately when navigating away", async () => {
  await boot(); clickPlan("opencode"); const request = pending[0]; overview();
  expect(request.signal?.aborted).toBe(true);
  await respond(plans("opencode"), plan("opencode"));
  expect(screen.queryByText(/opencode-target/)).toBeNull();
  clickPlan("opencode"); expect(screen.getByText(/Loading server-generated/)).toBeTruthy();
});
it("repeated plan for the same tool owns only its newest response", async () => {
  await boot(); clickPlan("hermes"); overview(); clickPlan("hermes");
  const [old, latest] = pending.splice(0);
  await act(async () => { latest.resolve(new Response(JSON.stringify({ ...plan("hermes"), target: "latest" }))); });
  await act(async () => { old.resolve(new Response(JSON.stringify({ ...plan("hermes"), target: "obsolete" }))); });
  expect(screen.getByText(/latest/)).toBeTruthy(); expect(screen.queryByText(/obsolete/)).toBeNull();
});
it("probe failure is a check error and preserves observed health", async () => {
  await boot(); clickCheck("hermes");
  await respond(check("hermes"), { ...card("hermes"), probe_pending: true, probe_request_id: "p1" });
  await tick(); await respond("/api/v1/probes/p1", probe("p1", { pending: false, state: "completed", status: "error", observation_applied: true }));
  expect(screen.getByRole("alert").textContent).toContain("Probe error");
  expect(within(screen.getByRole("article", { name: "Hermes status card" })).getByText("healthy")).toBeTruthy();
  expect(checking("Hermes")).toBe(false); await tick(30000);
  expect(pending.filter(p => p.path.includes("/probes/"))).toHaveLength(0);
});
it("keeps several pending polls serial and bounded", async () => {
  await boot(); clickCheck("hermes");
  await respond(check("hermes"), { ...card("hermes"), probe_pending: true, probe_request_id: "p1" });
  for (const interval of [1000, 2000, 4000, 8000, 16000, 30000, 30000]) {
    await tick(interval - 1); expect(pending.filter(p => p.path.includes("/probes/"))).toHaveLength(0);
    await tick(1); expect(pending.filter(p => p.path.includes("/probes/"))).toHaveLength(1);
    await respond("/api/v1/probes/p1", probe("p1")); expect(checking("Hermes")).toBe(true);
  }
});
it("unmount aborts in-flight polling and prevents more requests", async () => {
  const app = render(<App />); await act(async () => {}); clickCheck("hermes");
  await respond(check("hermes"), { ...card("hermes"), probe_pending: true, probe_request_id: "p1" });
  await tick(); const request = pending[0]; app.unmount(); expect(request.signal?.aborted).toBe(true);
  await respond("/api/v1/probes/p1", probe("p1")); await tick(60000); expect(pending).toHaveLength(0);
});
it("session expiry cancels polling for every tool", async () => {
  await boot(); clickCheck("hermes"); clickCheck("opencode");
  await respond(check("hermes"), { ...card("hermes"), probe_pending: true, probe_request_id: "p1" });
  await respond(check("opencode"), { ...card("opencode"), probe_pending: true, probe_request_id: "p2" });
  await tick(); const other = pending.find(p => p.path.endsWith("p2"));
  await respond("/api/v1/probes/p1", { code: "unauthenticated", message: "Session expired" }, 401);
  expect(other?.signal?.aborted).toBe(true); expect(screen.getByText(/Session ended/)).toBeTruthy();
  await respond("/api/v1/probes/p2", probe("p2")); await tick(60000); expect(pending).toHaveLength(0);
});
it("pagehide used by external logout cancels scheduled polling", async () => {
  await boot(); clickCheck("hermes");
  await respond(check("hermes"), { ...card("hermes"), probe_pending: true, probe_request_id: "p1" });
  act(() => window.dispatchEvent(new Event("pagehide"))); await tick(60000); expect(pending).toHaveLength(0);
});
it("a check cannot overwrite active-job gating or another tool card", async () => {
  await boot(); clickCheck("hermes");
  jobs = [{ id: "job-1", tool_id: "opencode", state: "updating" }];
  tools = [card("hermes", "job-refresh"), card("opencode", "other-refresh")];
  await tick(5000);
  const [request] = pending.splice(0);
  await act(async () => { request.resolve(new Response(JSON.stringify(card("hermes", "old-check")))); });
  expect(screen.getByText("job-refresh")).toBeTruthy(); expect(screen.getByText("1")).toBeTruthy();
  expect(screen.queryByText("old-check")).toBeNull();
  expect(screen.getByRole("button", { name: "Preview update for Hermes" }).hasAttribute("disabled")).toBe(true);
});
it("accepts legacy immediate cards without probe fields", async () => {
  await boot(); clickCheck("hermes"); await respond(check("hermes"), card("hermes", "legacy"));
  expect(screen.getByText("legacy")).toBeTruthy(); expect(checking("Hermes")).toBe(false);
  await tick(1000); expect(pending).toHaveLength(0);
});
it("honors a rate-limited probe read then completes", async () => {
  await boot(); clickCheck("hermes");
  await respond(check("hermes"), { ...card("hermes"), probe_pending: true, probe_request_id: "p1" });
  await tick(); const [request] = pending.splice(0);
  await act(async () => { request.resolve(new Response(JSON.stringify({ code: "rate_limited", message: "wait" }), { status: 429, headers: { "Retry-After": "5" } })); });
  await tick(4999); expect(pending).toHaveLength(0); await tick(1);
  await respond("/api/v1/probes/p1", probe("p1", { pending: false, state: "completed", status: "ok", observation_applied: true }));
  expect(checking("Hermes")).toBe(false);
});

// @vitest-environment jsdom
import React from "react";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { App } from "../src/app";
import {
  ApiError,
  BACKEND_OWNER_PLAN_TIMEOUT_MS,
  DisconnectedError,
  PLAN_REQUEST_TIMEOUT_MS,
  RequestTimeoutError,
  REQUEST_TIMEOUT_MS,
  api,
  type PlanView,
  type ToolCard,
} from "../src/api/client";

function abortError(): DOMException {
  return new DOMException("aborted", "AbortError");
}

function hangingFetch(_input: string, init?: RequestInit): Promise<Response> {
  return new Promise((_resolve, reject) => {
    const signal = init?.signal;
    if (signal?.aborted) {
      reject(abortError());
      return;
    }
    signal?.addEventListener("abort", () => reject(abortError()), { once: true });
  });
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function plan(toolId = "hermes"): PlanView {
  return {
    id: "plan-1",
    tool_id: toolId,
    target: "2.0.0",
    target_mode: "exact",
    channel: "stable",
    fingerprint: "fp",
    services: [],
    backup_scope: {},
    activity_state: "idle",
    activity_evidence: "",
    required_space_bytes: 0,
    steps: ["preflight"],
    expires_at: "later",
    restart_impact: "none",
  };
}

function card(id: string): ToolCard {
  return {
    id,
    installed_version: "1.0.0",
    available_target: "2.0.0",
    channel: "stable",
    last_success: "",
    last_attempt: "",
    health: "healthy",
    health_detail: "",
    checked_at: new Date().toISOString(),
    discovery_error: "",
    install_identity: id,
  };
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

it("classifies a network failure as disconnected", async () => {
  vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("network down")));

  await expect(api.getTools()).rejects.toBeInstanceOf(DisconnectedError);
});

it("classifies an ordinary internal 15-second deadline as a request timeout", async () => {
  vi.stubGlobal("fetch", vi.fn(hangingFetch));

  const request = api.getTools();
  const result = request.catch(error => error);
  await act(async () => {
    await vi.advanceTimersByTimeAsync(REQUEST_TIMEOUT_MS);
  });

  const error = await result;
  expect(error).toBeInstanceOf(RequestTimeoutError);
  expect(error).not.toBeInstanceOf(DisconnectedError);
});

it("preserves external AbortController cancellation as AbortError", async () => {
  vi.stubGlobal("fetch", vi.fn(hangingFetch));
  const owner = new AbortController();
  const request = api.getTools(owner.signal);

  owner.abort();

  await expect(request).rejects.toMatchObject({ name: "AbortError" });
  await expect(request).rejects.not.toBeInstanceOf(RequestTimeoutError);
});

it("keeps plan requests alive beyond the ordinary 15-second deadline", async () => {
  let resolveResponse: (response: Response) => void = () => {};
  const response = new Promise<Response>(resolve => { resolveResponse = resolve; });
  vi.stubGlobal("fetch", vi.fn(() => response));

  let settled = false;
  const request = api.createPlan("hermes").then(() => { settled = true; });
  await act(async () => {
    await vi.advanceTimersByTimeAsync(REQUEST_TIMEOUT_MS + 1);
  });
  expect(settled).toBe(false);

  resolveResponse(jsonResponse(plan()));
  await request;
  expect(settled).toBe(true);
});

it("accepts a plan response before the plan-specific deadline", async () => {
  let resolveResponse: (response: Response) => void = () => {};
  const response = new Promise<Response>(resolve => { resolveResponse = resolve; });
  vi.stubGlobal("fetch", vi.fn(() => response));

  const request = api.createPlan("hermes");
  await act(async () => {
    await vi.advanceTimersByTimeAsync(PLAN_REQUEST_TIMEOUT_MS - 1);
  });
  resolveResponse(jsonResponse(plan()));

  await expect(request).resolves.toMatchObject({ id: "plan-1" });
});

it("classifies a plan past its deadline as timeout, not disconnected", async () => {
  vi.stubGlobal("fetch", vi.fn(hangingFetch));

  const request = api.createPlan("hermes");
  const result = request.catch(error => error);
  await act(async () => {
    await vi.advanceTimersByTimeAsync(PLAN_REQUEST_TIMEOUT_MS);
  });

  const error = await result;
  expect(error).toBeInstanceOf(RequestTimeoutError);
  expect(error).not.toBeInstanceOf(DisconnectedError);
});

it("keeps 409 and 503 responses server-classified", async () => {
  for (const status of [409, 503]) {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(jsonResponse({
      code: status === 409 ? "busy" : "unavailable",
      message: `server ${status}`,
      details: "",
      request_id: "rid",
    }, status))));

    const error = await api.createPlan("hermes").catch(value => value);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).not.toBeInstanceOf(RequestTimeoutError);
    expect(error).not.toBeInstanceOf(DisconnectedError);
    if (!(error instanceof ApiError)) throw new Error("expected ApiError");
    expect(error.status).toBe(status);
  }
});

it("keeps the frontend plan deadline above the backend owner-plan deadline", () => {
  expect(REQUEST_TIMEOUT_MS).toBe(15_000);
  expect(BACKEND_OWNER_PLAN_TIMEOUT_MS).toBe(30_000);
  expect(PLAN_REQUEST_TIMEOUT_MS).toBeGreaterThan(BACKEND_OWNER_PLAN_TIMEOUT_MS);
});

it("shows a plan timeout without marking a healthy dashboard disconnected", async () => {
  let healthCalls = 0;
  let toolsCalls = 0;
  const dashboardFetch = vi.fn((input: string, init?: RequestInit): Promise<Response> => {
    if (input.endsWith("/session")) return Promise.resolve(jsonResponse({ email: "owner@example.test", csrf_token: "csrf" }));
    if (input.endsWith("/health")) {
      healthCalls += 1;
      return Promise.resolve(jsonResponse({ api: "ok", database: "ok", worker: "ok", recovery_required: false, checked_at: new Date().toISOString() }));
    }
    if (input.endsWith("/tools")) {
      toolsCalls += 1;
      return Promise.resolve(jsonResponse([card("hermes")]));
    }
    if (input.includes("/jobs?")) return Promise.resolve(jsonResponse({ jobs: [], next_cursor: "" }));
    if (input.endsWith("/tools/hermes/plans")) return hangingFetch(input, init);
    return Promise.reject(new Error(`unexpected request: ${input}`));
  });
  vi.stubGlobal("fetch", dashboardFetch);

  render(<App />);
  await act(async () => {});
  fireEvent.click(screen.getByRole("button", { name: "Preview update for Hermes" }));
  await act(async () => {
    await vi.advanceTimersByTimeAsync(PLAN_REQUEST_TIMEOUT_MS);
  });

  expect(screen.getByText("Plan request timed out")).toBeTruthy();
  const planAlert = screen.getByRole("alert");
  expect(planAlert.textContent).toContain("The plan probe did not finish before the browser deadline.");
  expect(planAlert.textContent).toContain("Retry the plan. The control plane may still be healthy.");
  expect(screen.queryByText(/The API is unreachable/)).toBeNull();
  expect(screen.getByRole("status", { name: "Connection: Connected" })).toBeTruthy();
  expect(healthCalls).toBeGreaterThan(1);
  expect(toolsCalls).toBeGreaterThan(1);
});

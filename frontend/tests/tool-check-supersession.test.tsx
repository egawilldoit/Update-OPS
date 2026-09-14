// @vitest-environment jsdom
import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { api, type ToolCard, type ToolCheck, type ProbeView } from "../src/api/client";
import { useToolChecks } from "../src/useToolChecks";

function deferred<T>() {
  let resolve: (value: T) => void = () => {};
  const promise = new Promise<T>(r => { resolve = r; });
  return { promise, resolve };
}
const card: ToolCard = { id: "hermes", installed_version: "fresh", available_target: "", channel: "", last_success: "", last_attempt: "", health: "unknown", health_detail: "", checked_at: "", discovery_error: "", install_identity: "" };
beforeEach(() => { vi.useFakeTimers(); vi.spyOn(api, "getTools").mockResolvedValue([card]); });
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers(); });
it("ignores old A after a newer A even when transport ignores abort", async () => {
  const old = deferred<ToolCheck>(); const newer = deferred<ToolCheck>();
  vi.spyOn(api, "checkTool").mockReturnValueOnce(old.promise).mockReturnValueOnce(newer.promise);
  const onCard = vi.fn(); const { result } = renderHook(() => useToolChecks({ onCard, onError: vi.fn() }));
  act(() => { void result.current.check("hermes"); });
  act(() => { void result.current.check("hermes", true); });
  await act(async () => { newer.resolve(card); });
  await act(async () => { old.resolve({ ...card, installed_version: "obsolete" }); });
  expect(onCard).toHaveBeenCalledTimes(1); expect(onCard).toHaveBeenCalledWith(card);
  expect(result.current.states.hermes.kind).toBe("idle");
});
it("late poll response cannot finish or refresh a superseding check", async () => {
  const oldPoll = deferred<ProbeView>(); const newer = deferred<ToolCheck>();
  vi.spyOn(api, "checkTool").mockResolvedValueOnce({ ...card, probe_pending: true, probe_request_id: "old" }).mockReturnValueOnce(newer.promise);
  vi.spyOn(api, "getProbe").mockReturnValue(oldPoll.promise);
  const onCard = vi.fn(); const { result } = renderHook(() => useToolChecks({ onCard, onError: vi.fn() }));
  await act(async () => { void result.current.check("hermes"); });
  await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
  act(() => { void result.current.check("hermes", true); });
  await act(async () => { oldPoll.resolve({ request_id: "old", tool_id: "hermes", pending: false, status: "ok", state: "completed", observation_applied: true }); });
  expect(onCard).not.toHaveBeenCalled(); expect(result.current.states.hermes.kind).toBe("checking");
  await act(async () => { newer.resolve(card); }); expect(onCard).toHaveBeenCalledTimes(1);
  await act(async () => { await vi.advanceTimersByTimeAsync(60000); }); expect(api.getProbe).toHaveBeenCalledTimes(1);
});
it("superseding a waiting poll cancels its timer", async () => {
  vi.spyOn(api, "checkTool").mockResolvedValueOnce({ ...card, probe_pending: true, probe_request_id: "old" }).mockResolvedValue(card);
  const poll = vi.spyOn(api, "getProbe");
  const { result } = renderHook(() => useToolChecks({ onCard: vi.fn(), onError: vi.fn() }));
  await act(async () => { void result.current.check("hermes"); });
  await act(async () => { void result.current.check("hermes", true); });
  await act(async () => { await vi.advanceTimersByTimeAsync(60000); }); expect(poll).not.toHaveBeenCalled();
});
it("keeps HTTP cancellation attached until the response body is read", async () => {
  const body = deferred<ToolCard[]>();
  let transportSignal: AbortSignal | null | undefined;
  vi.stubGlobal("fetch", vi.fn((_input: string, init?: RequestInit) => {
    transportSignal = init?.signal;
    return Promise.resolve({ ok: true, json: () => body.promise });
  }));
  vi.mocked(api.getTools).mockRestore();
  const controller = new AbortController();
  const request = api.getTools(controller.signal);
  await Promise.resolve();
  controller.abort();
  expect(transportSignal?.aborted).toBe(true);
  body.resolve([card]);
  await expect(request).rejects.toMatchObject({ name: "AbortError" });
  vi.unstubAllGlobals();
});

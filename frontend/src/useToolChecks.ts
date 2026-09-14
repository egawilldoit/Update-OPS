import React from "react";
import { api, ApiError, backoffMs, type ToolCard } from "./api/client";

export type CheckState =
  | { kind: "checking" }
  | { kind: "pending"; requestId: string }
  | { kind: "idle" }
  | { kind: "error"; message: string };

function delay(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const abort = () => {
      window.clearTimeout(timer);
      reject(new DOMException("Request cancelled", "AbortError"));
    };
    const timer = window.setTimeout(() => {
      signal.removeEventListener("abort", abort);
      resolve();
    }, ms);
    signal.addEventListener("abort", abort, { once: true });
    if (signal.aborted) abort();
  });
}

export function useToolChecks({ onCard, onError }: {
  onCard: (card: ToolCard) => void;
  onError: (error: unknown) => void;
}) {
  const [states, setStates] = React.useState<Record<string, CheckState>>({});
  const requests = React.useRef(new Map<string, AbortController>());
  const enabled = React.useRef(true);
  const cancel = React.useCallback(() => {
    enabled.current = false;
    for (const request of requests.current.values()) request.abort();
    requests.current.clear();
  }, []);
  React.useEffect(() => {
    enabled.current = true;
    window.addEventListener("pagehide", cancel);
    return () => { cancel(); window.removeEventListener("pagehide", cancel); };
  }, [cancel]);

  async function check(toolId: string, supersede = false): Promise<void> {
    if (!enabled.current) return;
    const previous = requests.current.get(toolId);
    if (previous && !supersede) return;
    previous?.abort();
    const request = new AbortController();
    requests.current.set(toolId, request);
    const current = () => enabled.current && requests.current.get(toolId) === request;
    const update = (state: CheckState) => {
      if (current()) setStates(prev => ({ ...prev, [toolId]: state }));
    };
    update({ kind: "checking" });
    try {
      const response = await api.checkTool(toolId, true, request.signal);
      if (!current()) return;
      if (response.probe_pending) {
        const requestId = response.probe_request_id;
        if (!requestId) throw new Error("Pending probe has no request ID");
        update({ kind: "pending", requestId });
        let attempt = 0;
        let retryAfter: number | null = null;
        while (current()) {
          await delay(backoffMs(attempt++, retryAfter), request.signal);
          if (!current()) return;
          try {
            const probe = await api.getProbe(requestId, request.signal);
            if (!current()) return;
            if (probe.request_id !== requestId || probe.tool_id !== toolId) {
              throw new Error("Probe response identity mismatch");
            }
            retryAfter = null;
            if (probe.pending) continue;
            if (probe.status !== "ok") throw new Error(`Probe ${probe.status || probe.state}; tool health unchanged`);
            if (probe.observation_applied) break;
          } catch (error) {
            if (error instanceof ApiError && error.status === 429) {
              retryAfter = error.retryAfterMs;
              continue;
            }
            throw error;
          }
        }
      }
      // Fetch the authoritative card after completion; the POST snapshot may predate a job refresh.
      const cards = await api.getTools(request.signal);
      if (!current()) return;
      const card = cards.find(c => c.id === toolId);
      if (!card) throw new Error("Checked tool missing from refresh");
      onCard(card);
      update({ kind: "idle" });
    } catch (error) {
      if (!current()) return;
      update({ kind: "error", message: error instanceof Error ? error.message : "Check failed" });
      onError(error);
    } finally {
      if (current()) requests.current.delete(toolId);
    }
  }
  return { states, check, cancel };
}

import React from "react";
import {
  ApiError,
  DisconnectedError,
  api,
  newIdempotencyKey,
  type HealthView,
  type JobDetail as JobDetailT,
  type JobView,
  type LogRecord,
  type PlanView,
  type ToolCard as Card,
} from "./api/client";
import { HealthPanel } from "./components/HealthPanel";
import { History } from "./views/History";
import { JobDetail } from "./views/JobDetail";
import { Overview } from "./views/Overview";
import { PlanPreview } from "./views/PlanPreview";

type View = { kind: "overview" } | { kind: "plan"; toolId: string } | { kind: "job"; jobId: string } | { kind: "history" };

const NONTERMINAL = ["accepted", "preflight", "backup", "updating", "verifying"];

function errText(e: unknown): string {
  if (e instanceof DisconnectedError) return "disconnected";
  if (e instanceof ApiError) return `${e.body.code}: ${e.body.message}`;
  return "unexpected error";
}

export function App(): React.ReactElement {
  const [email, setEmail] = React.useState("");
  const [disconnected, setDisconnected] = React.useState(false);
  const [bootError, setBootError] = React.useState("");
  const [health, setHealth] = React.useState<HealthView | null>(null);
  const [cards, setCards] = React.useState<Card[]>([]);
  const [view, setView] = React.useState<View>({ kind: "overview" });
  const [checkingId, setCheckingId] = React.useState("");
  const [planningId, setPlanningId] = React.useState("");
  const [plan, setPlan] = React.useState<PlanView | null>(null);
  const [planLoading, setPlanLoading] = React.useState(false);
  const [planError, setPlanError] = React.useState("");
  const [ack, setAck] = React.useState(false);
  const [starting, setStarting] = React.useState(false);
  const [idemKey, setIdemKey] = React.useState("");
  const [job, setJob] = React.useState<JobDetailT | null>(null);
  const [logs, setLogs] = React.useState<LogRecord[]>([]);
  const [logsAfter, setLogsAfter] = React.useState(0);
  const [truncated, setTruncated] = React.useState(false);
  const [history, setHistory] = React.useState<JobView[]>([]);
  const [nextCursor, setNextCursor] = React.useState("");
  const [historyLoading, setHistoryLoading] = React.useState(false);
  const [notice, setNotice] = React.useState("");

  function markConnected(): void {
    setDisconnected(false);
  }
  function markMaybeDisconnected(e: unknown): void {
    if (e instanceof DisconnectedError) setDisconnected(true);
  }

  const loadHealth = React.useCallback(async () => {
    try {
      const h = await api.getHealth();
      markConnected();
      setHealth(h);
    } catch (e) {
      markMaybeDisconnected(e);
    }
  }, []);

  const loadTools = React.useCallback(async () => {
    try {
      const t = await api.getTools();
      markConnected();
      setCards(t);
    } catch (e) {
      markMaybeDisconnected(e);
    }
  }, []);

  const loadHistory = React.useCallback(async (cursor = "") => {
    setHistoryLoading(true);
    try {
      const page = await api.listJobs(cursor, 25);
      markConnected();
      setHistory((prev) => (cursor ? [...prev, ...page.jobs] : page.jobs));
      setNextCursor(page.next_cursor);
    } catch (e) {
      markMaybeDisconnected(e);
      setNotice(errText(e));
    } finally {
      setHistoryLoading(false);
    }
  }, []);

  // Boot: session first (establishes CSRF), then reads.
  React.useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const s = await api.getSession();
        if (cancelled) return;
        setEmail(s.email);
        markConnected();
        await Promise.all([loadHealth(), loadTools(), loadHistory()]);
      } catch (e) {
        if (cancelled) return;
        if (e instanceof DisconnectedError) {
          setDisconnected(true);
          setBootError("API unreachable — showing disconnected banner, nothing cached as current.");
        } else {
          setBootError(errText(e));
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [loadHealth, loadTools, loadHistory]);

  // Poll health + tools every 30s; job/log polling is handled per-view below.
  React.useEffect(() => {
    const t = window.setInterval(() => {
      void loadHealth();
      void loadTools();
    }, 30000);
    return () => window.clearInterval(t);
  }, [loadHealth, loadTools]);

  const activeJob = history.find((j) => NONTERMINAL.includes(j.state));
  const recoveryRequired = health?.recovery_required === true;
  const updateDisabled = Boolean(activeJob) || recoveryRequired;
  const disableReason = recoveryRequired
    ? "Updates blocked: recovery required — clear via SSH reconcile first."
    : activeJob
      ? `Updates blocked: job ${activeJob.id.slice(0, 8)} (${activeJob.tool_id}, ${activeJob.state}) is active.`
      : "";

  async function handleCheck(toolId: string): Promise<void> {
    setCheckingId(toolId);
    setNotice("");
    try {
      const c = await api.checkTool(toolId);
      markConnected();
      setCards((prev) => prev.map((x) => (x.id === toolId ? c : x)));
    } catch (e) {
      markMaybeDisconnected(e);
      setNotice(errText(e));
    } finally {
      setCheckingId("");
    }
  }

  async function handlePlan(toolId: string): Promise<void> {
    setView({ kind: "plan", toolId });
    setPlanningId(toolId);
    setPlanLoading(true);
    setPlanError("");
    setPlan(null);
    setAck(false);
    try {
      const p = await api.createPlan(toolId);
      markConnected();
      setPlan(p);
      setIdemKey(newIdempotencyKey());
    } catch (e) {
      markMaybeDisconnected(e);
      setPlanError(errText(e));
    } finally {
      setPlanLoading(false);
      setPlanningId("");
    }
  }

  async function handleStart(): Promise<void> {
    if (!plan) return;
    setStarting(true);
    setNotice("");
    try {
      const j = await api.createJob(plan.id, ack, idemKey || newIdempotencyKey());
      markConnected();
      setView({ kind: "job", jobId: j.id });
      setJob(null);
      setLogs([]);
      setLogsAfter(0);
      await loadHistory();
    } catch (e) {
      markMaybeDisconnected(e);
      setNotice(errText(e));
    } finally {
      setStarting(false);
    }
  }

  // Job detail + log polling: logs every 2s while nonterminal (SPEC section 9).
  const jobId = view.kind === "job" ? view.jobId : "";
  React.useEffect(() => {
    if (!jobId) return;
    let cancelled = false;
    let logAfter = 0;
    let timer = 0;
    async function fetchOnce(): Promise<boolean> {
      try {
        const [j, page] = await Promise.all([api.getJob(jobId), api.getLogs(jobId, logAfter, 200)]);
        if (cancelled) return true;
        markConnected();
        setJob(j);
        if (page.records.length > 0) {
          logAfter = page.next_after;
          setLogsAfter(page.next_after);
          setLogs((prev) => {
            const seen = new Set(prev.map((r) => r.seq));
            return [...prev, ...page.records.filter((r) => !seen.has(r.seq))].sort((a, b) => a.seq - b.seq);
          });
        }
        setTruncated(page.truncated);
        return !NONTERMINAL.includes(j.state);
      } catch (e) {
        if (!cancelled) markMaybeDisconnected(e);
        return false;
      }
    }
    setJob(null);
    setLogs([]);
    setTruncated(false);
    void fetchOnce().then((done) => {
      if (cancelled || done) return;
      timer = window.setInterval(() => {
        void fetchOnce().then((d) => {
          if (d && timer) window.clearInterval(timer);
        });
      }, 2000);
    });
    return () => {
      cancelled = true;
      if (timer) window.clearInterval(timer);
    };
  }, [jobId]);

  function openJob(jobIdToOpen: string): void {
    setView({ kind: "job", jobId: jobIdToOpen });
  }

  return (
    <div className="app">
      <a className="skip-link" href="#main">
        Skip to main content
      </a>
      <header className="app-header">
        <div>
          <h1>EGA Update Console</h1>
          <p className="owner-line">{email ? `Owner: ${email}` : "Private owner console"}</p>
        </div>
        <nav aria-label="Primary">
          <button
            type="button"
            onClick={() => setView({ kind: "overview" })}
            aria-current={view.kind === "overview" ? "page" : undefined}
          >
            Overview
          </button>
          <button
            type="button"
            onClick={() => {
              setView({ kind: "history" });
              void loadHistory();
            }}
            aria-current={view.kind === "history" ? "page" : undefined}
          >
            History
          </button>
        </nav>
      </header>

      {disconnected ? (
        <p className="banner banner-disconnected" role="alert">
          Disconnected — API unreachable. Status is not current; cached values are never shown as green.
        </p>
      ) : null}
      {bootError && !disconnected ? (
        <p className="banner banner-error" role="alert">
          {bootError}
        </p>
      ) : null}
      {notice ? (
        <p className="banner banner-error" role="alert">
          {notice}
        </p>
      ) : null}

      <main id="main">
        <HealthPanel health={health} />
        {view.kind === "overview" ? (
          <Overview
            cards={cards}
            updateDisabled={updateDisabled}
            disableReason={disableReason}
            checkingId={checkingId}
            planningId={planningId}
            onCheck={(t) => void handleCheck(t)}
            onPlan={(t) => void handlePlan(t)}
          />
        ) : null}
        {view.kind === "plan" ? (
          <PlanPreview
            toolId={view.toolId}
            plan={plan}
            loading={planLoading}
            error={planError}
            ack={ack}
            onAckChange={setAck}
            onStart={() => void handleStart()}
            starting={starting}
            blockedReason={disableReason}
          />
        ) : null}
        {view.kind === "job" ? <JobDetail job={job} logs={logs} truncated={truncated} /> : null}
        {view.kind === "history" ? (
          <History
            jobs={history}
            nextCursor={nextCursor}
            loading={historyLoading}
            onNext={() => void loadHistory(nextCursor)}
            onRefresh={() => void loadHistory()}
            onOpen={openJob}
          />
        ) : null}
        {view.kind === "job" ? (
          <p className="hint">
            Log cursor: after={logsAfter}. Active logs refresh every 2 seconds; reopening restores ordered history
            without duplicates.
          </p>
        ) : null}
      </main>
    </div>
  );
}

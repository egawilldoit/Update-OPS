import React from "react";
import {
  ApiError,
  DisconnectedError,
  api,
  backoffMs,
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
const TERMINAL = ["succeeded", "blocked", "failed", "health_failed", "interrupted"];

const ROUTE_JOB_KEY = "ega-update:selected-job";
const ROUTE_VIEW_KEY = "ega-update:view";

function errText(e: unknown): string {
  if (e instanceof DisconnectedError) return "disconnected";
  if (e instanceof ApiError) return `${e.body.code}: ${e.body.message}`;
  return "unexpected error";
}

function isTerminal(state: string): boolean {
  return TERMINAL.includes(state);
}

export function App(): React.ReactElement {
  const [email, setEmail] = React.useState("");
  const [disconnected, setDisconnected] = React.useState(false);
  const [reconnect, setReconnect] = React.useState("");
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
  const [hasMore, setHasMore] = React.useState(false);
  const [finalLogSeq, setFinalLogSeq] = React.useState(-1);
  const [history, setHistory] = React.useState<JobView[]>([]);
  const [activeJobs, setActiveJobs] = React.useState<JobView[]>([]);
  const [nextCursor, setNextCursor] = React.useState("");
  const [historyLoading, setHistoryLoading] = React.useState(false);
  const [notice, setNotice] = React.useState("");

  // R21: per-bucket backoff state (reads vs mutations stay independent;
  // server enforces separate read/mutation buckets). Each bucket tracks its
  // own attempt count and next-allowed timestamp; 429 honors Retry-After.
  const readBackoff = React.useRef({ attempt: 0, untilMs: 0 });
  const mutationBackoff = React.useRef({ attempt: 0, untilMs: 0 });
  const jobPollBackoff = React.useRef({ attempt: 0, untilMs: 0 });
  // R21: serialize per-job polls — never overlap fetches for the same job.
  const jobInflight = React.useRef(false);
  // R21: plan generation IDs per tool — ignore stale plan responses.
  const planGen = React.useRef<Record<string, number>>({});

  function markConnected(): void {
    setDisconnected(false);
  }
  function markMaybeDisconnected(e: unknown): void {
    if (e instanceof DisconnectedError) setDisconnected(true);
  }

  function noteRateLimited(bucket: "read" | "mutation" | "job", e: ApiError): void {
    const ref = bucket === "mutation" ? mutationBackoff : bucket === "job" ? jobPollBackoff : readBackoff;
    const waitMs = backoffMs(ref.current.attempt, e.retryAfterMs);
    ref.current.attempt += 1;
    ref.current.untilMs = Date.now() + waitMs;
    const secs = Math.max(1, Math.ceil(waitMs / 1000));
    setReconnect(`Rate limited — retrying in ${secs}s (Retry-After honored, backoff max 30s).`);
    window.setTimeout(() => setReconnect(""), waitMs);
  }

  function clearBucket(bucket: "read" | "mutation" | "job"): void {
    const ref = bucket === "mutation" ? mutationBackoff : bucket === "job" ? jobPollBackoff : readBackoff;
    ref.current.attempt = 0;
    ref.current.untilMs = 0;
  }

  function readThrottled(): boolean {
    return Date.now() < readBackoff.current.untilMs;
  }

  const loadHealth = React.useCallback(async () => {
    if (readThrottled()) return;
    try {
      const h = await api.getHealth();
      markConnected();
      clearBucket("read");
      setReconnect("");
      setHealth(h);
    } catch (e) {
      if (e instanceof ApiError && e.status === 429) {
        noteRateLimited("read", e);
        return;
      }
      markMaybeDisconnected(e);
    }
  }, []);

  const loadTools = React.useCallback(async () => {
    if (readThrottled()) return;
    try {
      const t = await api.getTools();
      markConnected();
      clearBucket("read");
      setCards(t);
    } catch (e) {
      if (e instanceof ApiError && e.status === 429) {
        noteRateLimited("read", e);
        return;
      }
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
      if (e instanceof ApiError && e.status === 429) {
        noteRateLimited("read", e);
        setNotice(`rate_limited: honoring Retry-After before history retry`);
        return;
      }
      markMaybeDisconnected(e);
      setNotice(errText(e));
    } finally {
      setHistoryLoading(false);
    }
  }, []);

  // R21: global active-job poll for update gating — GET /jobs?active=true
  // every 5s, independent of history/detail views.
  const loadActive = React.useCallback(async () => {
    if (readThrottled()) return;
    try {
      const page = await api.listActiveJobs(25);
      markConnected();
      clearBucket("read");
      setActiveJobs(page.jobs);
    } catch (e) {
      if (e instanceof ApiError && e.status === 429) {
        noteRateLimited("read", e);
        return;
      }
      markMaybeDisconnected(e);
    }
  }, []);

  // Boot: session first (establishes CSRF), then reads. Restores the
  // persisted job route after session recovery (R21).
  React.useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const s = await api.getSession();
        if (cancelled) return;
        setEmail(s.email);
        markConnected();
        await Promise.all([loadHealth(), loadTools(), loadHistory(), loadActive()]);
        if (cancelled) return;
        try {
          const savedJob = window.localStorage.getItem(ROUTE_JOB_KEY) || "";
          const savedView = window.localStorage.getItem(ROUTE_VIEW_KEY) || "";
          if (savedJob) {
            setView({ kind: "job", jobId: savedJob });
          } else if (savedView === "history") {
            setView({ kind: "history" });
          }
        } catch {
          // localStorage unavailable — stay on overview.
        }
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
  }, [loadHealth, loadTools, loadHistory, loadActive]);

  // Poll health + tools every 30s; active-job gate polls every 5s globally.
  React.useEffect(() => {
    const t = window.setInterval(() => {
      void loadHealth();
      void loadTools();
    }, 30000);
    return () => window.clearInterval(t);
  }, [loadHealth, loadTools]);

  React.useEffect(() => {
    void loadActive();
    const t = window.setInterval(() => {
      void loadActive();
    }, 5000);
    return () => window.clearInterval(t);
  }, [loadActive]);

  // R21: persist the selected job route; restore after session recovery.
  React.useEffect(() => {
    try {
      if (view.kind === "job") {
        window.localStorage.setItem(ROUTE_JOB_KEY, view.jobId);
        window.localStorage.setItem(ROUTE_VIEW_KEY, "job");
      } else {
        window.localStorage.removeItem(ROUTE_JOB_KEY);
        window.localStorage.setItem(ROUTE_VIEW_KEY, view.kind);
      }
    } catch {
      // Storage full/blocked — route persistence is best-effort.
    }
  }, [view]);

  // Gating comes from the global active poll, never from history/detail.
  const activeJob = activeJobs.find((j) => NONTERMINAL.includes(j.state));
  const recoveryRequired = health?.recovery_required === true;
  const updateDisabled = Boolean(activeJob) || recoveryRequired;
  const disableReason = recoveryRequired
    ? "Updates blocked: recovery required — clear via SSH reconcile first."
    : activeJob
      ? `Updates blocked: job ${activeJob.id.slice(0, 8)} (${activeJob.tool_id}, ${activeJob.state}) is active.`
      : "";

  async function handleCheck(toolId: string): Promise<void> {
    // R19: explicit Check again forces fresh bounded probes (?force=1);
    // cached 15-min discovery results are never served for manual refresh.
    if (Date.now() < mutationBackoff.current.untilMs) {
      setNotice("Rate limited — Check again is backing off (Retry-After honored, max 30s).");
      return;
    }
    setCheckingId(toolId);
    setNotice("");
    try {
      const c = await api.checkTool(toolId, true);
      markConnected();
      clearBucket("mutation");
      setReconnect("");
      setCards((prev) => prev.map((x) => (x.id === toolId ? c : x)));
    } catch (e) {
      if (e instanceof ApiError && e.status === 429) {
        noteRateLimited("mutation", e);
        setNotice("rate_limited: Check again backing off (Retry-After honored, max 30s).");
        return;
      }
      markMaybeDisconnected(e);
      setNotice(errText(e));
    } finally {
      setCheckingId("");
    }
  }

  async function handlePlan(toolId: string): Promise<void> {
    if (Date.now() < mutationBackoff.current.untilMs) {
      setPlanError("Rate limited — plan retry backing off (Retry-After honored, max 30s).");
      return;
    }
    // R21: plan generation IDs per tool — ignore stale responses.
    const gen = (planGen.current[toolId] || 0) + 1;
    planGen.current[toolId] = gen;
    setView({ kind: "plan", toolId });
    setPlanningId(toolId);
    setPlanLoading(true);
    setPlanError("");
    setPlan(null);
    setAck(false);
    try {
      const p = await api.createPlan(toolId);
      if (planGen.current[toolId] !== gen) return;
      markConnected();
      clearBucket("mutation");
      setReconnect("");
      setPlan(p);
      setIdemKey(newIdempotencyKey());
    } catch (e) {
      if (planGen.current[toolId] !== gen) return;
      if (e instanceof ApiError && e.status === 429) {
        noteRateLimited("mutation", e);
        setPlanError("rate_limited: plan retry backing off (Retry-After honored, max 30s).");
        return;
      }
      markMaybeDisconnected(e);
      setPlanError(errText(e));
    } finally {
      if (planGen.current[toolId] === gen) {
        setPlanLoading(false);
        setPlanningId("");
      }
    }
  }

  async function handleStart(): Promise<void> {
    if (!plan) return;
    if (Date.now() < mutationBackoff.current.untilMs) {
      setNotice("Rate limited — Start update is backing off (Retry-After honored, max 30s).");
      return;
    }
    setStarting(true);
    setNotice("");
    try {
      const j = await api.createJob(plan.id, ack, idemKey || newIdempotencyKey());
      markConnected();
      clearBucket("mutation");
      setReconnect("");
      setView({ kind: "job", jobId: j.id });
      setJob(null);
      setLogs([]);
      setLogsAfter(0);
      setFinalLogSeq(typeof j.final_log_seq === "number" ? j.final_log_seq : -1);
      setHasMore(false);
      setTruncated(false);
      await loadHistory();
      await loadActive();
    } catch (e) {
      if (e instanceof ApiError && e.status === 429) {
        noteRateLimited("mutation", e);
        setNotice("rate_limited: Start update backing off (Retry-After honored, max 30s).");
        return;
      }
      markMaybeDisconnected(e);
      setNotice(errText(e));
    } finally {
      setStarting(false);
    }
  }

  // R20 + R21: job detail + log polling.
  // - Drain until (terminal && next_after >= final_log_seq && !has_more);
  //   final_log_seq -1 means unknown until durable flush — then !has_more
  //   plus terminal is the drain signal.
  // - Reopening a finished job resumes from cursor 0 to full drain.
  // - Per-job polls are serialized (no overlapping fetches); every fetch
  //   uses the 15s AbortController timeout in client.ts; 429 honors
  //   Retry-After with exponential backoff (max 30s) and reconnect state.
  const jobId = view.kind === "job" ? view.jobId : "";
  const loadMoreRef = React.useRef<() => void>(() => {});
  React.useEffect(() => {
    if (!jobId) return;
    let cancelled = false;
    let logAfter = 0;
    let timer = 0;
    let done = false;
    function mergePolledJob(j: JobDetailT): void {
      const { checks: _omit, ...lite } = j as JobDetailT & { checks?: unknown };
      const entry = lite as JobView;
      setHistory((prev) => {
        const idx = prev.findIndex((h) => h.id === entry.id);
        if (idx === -1) return [entry, ...prev];
        return prev.map((h) => (h.id === entry.id ? { ...h, ...entry } : h));
      });
    }
    function drained(j: JobDetailT, nextAfter: number, more: boolean): boolean {
      if (!isTerminal(j.state)) return false;
      if (more) return false;
      const fls = typeof j.final_log_seq === "number" ? j.final_log_seq : -1;
      if (fls >= 0 && nextAfter < fls) return false;
      return true;
    }
    async function fetchOnce(): Promise<boolean> {
      // Serialize: skip this tick when the previous fetch is still in flight.
      if (jobInflight.current) return done;
      if (Date.now() < jobPollBackoff.current.untilMs) return done;
      jobInflight.current = true;
      try {
        const [j, page] = await Promise.all([api.getJob(jobId), api.getLogs(jobId, logAfter, 200)]);
        if (cancelled) return true;
        markConnected();
        clearBucket("job");
        setJob(j);
        mergePolledJob(j);
        const fls = typeof j.final_log_seq === "number" ? j.final_log_seq : -1;
        setFinalLogSeq(fls);
        if (page.records.length > 0) {
          logAfter = page.next_after;
          setLogsAfter(page.next_after);
          setLogs((prev) => {
            const seen = new Set(prev.map((r) => r.seq));
            return [...prev, ...page.records.filter((r) => !seen.has(r.seq))].sort((a, b) => a.seq - b.seq);
          });
        } else {
          // Advance the cursor even on empty pages so next_after tracks the
          // server cursor (needed for the drain comparison).
          logAfter = page.next_after;
          setLogsAfter(page.next_after);
        }
        setTruncated(page.truncated);
        const more = page.has_more === true;
        setHasMore(more);
        const isDrained = drained(j, page.next_after, more);
        done = isDrained;
        if (isDrained) {
          void loadHistory();
          void loadActive();
        }
        return isDrained;
      } catch (e) {
        if (cancelled) return false;
        if (e instanceof ApiError && e.status === 429) {
          noteRateLimited("job", e);
          return false;
        }
        markMaybeDisconnected(e);
        return false;
      } finally {
        jobInflight.current = false;
      }
    }
    loadMoreRef.current = () => {
      void fetchOnce().then((d) => {
        if (d && timer) window.clearInterval(timer);
      });
    };
    // Reopening always resumes from cursor 0 to full drain (R20).
    setJob(null);
    setLogs([]);
    setLogsAfter(0);
    setFinalLogSeq(-1);
    setTruncated(false);
    setHasMore(false);
    done = false;
    void fetchOnce().then((isDrained) => {
      if (cancelled || isDrained) return;
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
  }, [jobId, loadHistory, loadActive]);

  function handleLoadMore(): void {
    loadMoreRef.current();
  }

  function openJob(jobIdToOpen: string): void {
    setView({ kind: "job", jobId: jobIdToOpen });
  }

  const terminal = job !== null && isTerminal(job.state);
  const pendingTail =
    terminal && (hasMore || (finalLogSeq >= 0 && logsAfter < finalLogSeq));

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
      {reconnect ? (
        <p className="banner banner-error" role="status">
          {reconnect}
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
        <HealthPanel health={health} disconnected={disconnected} />
        {view.kind === "overview" ? (
          <Overview
            cards={cards}
            updateDisabled={updateDisabled}
            disableReason={disableReason}
            checkingId={checkingId}
            planningId={planningId}
            disconnected={disconnected}
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
        {view.kind === "job" ? (
          <JobDetail
            job={job}
            logs={logs}
            truncated={truncated}
            hasMore={hasMore}
            finalLogSeq={finalLogSeq}
            nextAfter={logsAfter}
            pendingTail={pendingTail}
            onLoadMore={handleLoadMore}
          />
        ) : null}
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
            Log cursor: after={logsAfter}
            {finalLogSeq >= 0 ? ` final=${finalLogSeq}` : " final=unknown"}. Active logs refresh every 2
            seconds until terminal drain; reopening resumes from 0 to full drain without duplicates.
            {pendingTail ? " Pending tail: terminal state reached but log cursor is behind — draining." : ""}
          </p>
        ) : null}
      </main>
    </div>
  );
}

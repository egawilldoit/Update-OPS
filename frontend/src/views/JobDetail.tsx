import React from "react";
import { elapsed, type JobDetail as Detail, type LogRecord } from "../api/client";
import { LogViewer } from "../components/LogViewer";
import { StatusBadge } from "../components/StatusBadge";

export function JobDetail({
  job,
  logs,
  truncated,
  hasMore,
  finalLogSeq,
  nextAfter,
  pendingTail,
  onLoadMore,
}: {
  job: Detail | null;
  logs: LogRecord[];
  truncated: boolean;
  hasMore?: boolean;
  finalLogSeq?: number;
  nextAfter?: number;
  pendingTail?: boolean;
  onLoadMore?: () => void;
}): React.ReactElement {
  const [nowMs, setNowMs] = React.useState(Date.now());
  React.useEffect(() => {
    const t = window.setInterval(() => setNowMs(Date.now()), 1000);
    return () => window.clearInterval(t);
  }, []);
  if (!job) {
    return (
      <section aria-label="Job detail">
        <h2>Job</h2>
        <p className="hint">No job selected.</p>
      </section>
    );
  }
  const terminal = ["succeeded", "blocked", "failed", "health_failed", "interrupted"].includes(job.state);
  const fls = typeof finalLogSeq === "number" ? finalLogSeq : typeof job.final_log_seq === "number" ? job.final_log_seq : -1;
  const cursor = typeof nextAfter === "number" ? nextAfter : 0;
  const tail = pendingTail === true || (terminal && (hasMore === true || (fls >= 0 && cursor < fls)));
  return (
    <section aria-label={`Job ${job.id}`}>
      <h2>
        Job — {job.tool_id} <StatusBadge status={job.state} />
      </h2>
      <dl className="plan-facts">
        <div>
          <dt>Step</dt>
          <dd>{job.step || job.state} (real step name — no percentage is shown)</dd>
        </div>
        <div>
          <dt>Elapsed</dt>
          <dd>{elapsed(job.started_at || job.created_at, terminal ? job.finished_at : "", nowMs)}</dd>
        </div>
        <div>
          <dt>Before → after</dt>
          <dd>
            {job.before_version || "unknown"} → {job.after_version || "pending"}
          </dd>
        </div>
        <div>
          <dt>Created / started / finished</dt>
          <dd>
            {job.created_at || "—"} / {job.started_at || "—"} / {job.finished_at || "—"}
          </dd>
        </div>
        {job.error_code ? (
          <div>
            <dt>Error</dt>
            <dd>
              {job.error_code}
              {job.error_detail ? ` — ${job.error_detail}` : ""}
            </dd>
          </div>
        ) : null}
        {job.backup_summary ? (
          <div>
            <dt>Backup</dt>
            <dd>{job.backup_summary}</dd>
          </div>
        ) : null}
        <div>
          <dt>Runner</dt>
          <dd>{job.runner_unit || "—"}</dd>
        </div>
        <div>
          <dt>Log cursor</dt>
          <dd>
            after={cursor}
            {fls >= 0 ? ` final=${fls}` : " final=unknown (durable flush pending)"}
          </dd>
        </div>
      </dl>
      {tail ? (
        <p className="warn" role="status">
          Pending tail: job is {job.state} but the log cursor ({cursor}
          {fls >= 0 ? `/${fls}` : ""}) is behind — draining remaining pages.
        </p>
      ) : null}
      {job.checks && job.checks.length > 0 ? (
        <section aria-label="Health checks">
          <h3>Checks (installation and health reported independently)</h3>
          <ul className="check-list">
            {job.checks.map((c, i) => (
              <li key={`${c.name}-${i}`}>
                <StatusBadge status={c.result} /> <strong>{c.name}</strong>
                {c.mandatory ? " (mandatory)" : " (optional)"}
                {c.summary ? ` — ${c.summary}` : ""}
              </li>
            ))}
          </ul>
        </section>
      ) : null}
      <LogViewer
        records={logs}
        truncated={truncated}
        hasMore={hasMore === true}
        finalLogSeq={fls}
        nextAfter={cursor}
        pendingTail={tail}
        onLoadMore={onLoadMore}
      />
    </section>
  );
}

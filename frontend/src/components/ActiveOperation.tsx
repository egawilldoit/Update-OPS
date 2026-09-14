import React from "react";
import { elapsed, type JobView } from "../api/client";
import { jobOutcome, toolName } from "../operational";
import { StatusBadge } from "./StatusBadge";

export function ActiveOperation({
  job,
  onOpen,
}: {
  job: JobView;
  onOpen: (jobId: string) => void;
}): React.ReactElement {
  const [nowMs, setNowMs] = React.useState(() => Date.now());
  React.useEffect(() => {
    const t = window.setInterval(() => setNowMs(Date.now()), 1000);
    return () => window.clearInterval(t);
  }, []);
  const outcome = jobOutcome(job.state);
  return (
    <section className="active-op" aria-label="Active update operation">
      <header className="section-head">
        <h2>Active operation — {toolName(job.tool_id)}</h2>
        <StatusBadge status={job.state} label={outcome.label} />
      </header>
      <p className="hint">{outcome.sentence}</p>
      <dl className="op-facts">
        <div>
          <dt>Job</dt>
          <dd className="mono">{job.id}</dd>
        </div>
        <div>
          <dt>Step</dt>
          <dd>{job.step || outcome.label}</dd>
        </div>
        <div>
          <dt>Elapsed</dt>
          <dd>{elapsed(job.started_at || job.created_at, "", nowMs)}</dd>
        </div>
        <div>
          <dt>Version</dt>
          <dd className="mono">
            {job.before_version || "unknown"} → {job.after_version || "pending"}
          </dd>
        </div>
      </dl>
      {job.recovery_required ? (
        <p className="error" role="alert">
          Recovery required — this job may have left the tool in a partial state. Reconcile over SSH before any
          new update.
        </p>
      ) : null}
      <div className="row-actions">
        <button type="button" onClick={() => onOpen(job.id)} aria-label={`Open active job ${job.id}`}>
          Open job details
        </button>
      </div>
    </section>
  );
}

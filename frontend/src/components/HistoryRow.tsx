import React from "react";
import { elapsed, type JobView } from "../api/client";
import { jobOutcome, toolName } from "../operational";
import { StatusBadge } from "./StatusBadge";

export function HistoryRow({
  job,
  onOpen,
}: {
  job: JobView;
  onOpen: (jobId: string) => void;
}): React.ReactElement {
  const outcome = jobOutcome(job.state);
  const target = job.after_version || "…";
  return (
    <li>
      <button
        type="button"
        className="history-item"
        onClick={() => onOpen(job.id)}
        aria-label={`Open job ${job.id}`}
      >
        <StatusBadge status={job.state} label={outcome.label} />
        <span className="history-main">
          <strong>{toolName(job.tool_id)}</strong>{" "}
          <span className="mono">
            {job.before_version || "?"} → {target}
          </span>
        </span>
        <span className="history-meta mono">{job.created_at || "—"}</span>
        <span className="history-meta">{elapsed(job.started_at || job.created_at, job.finished_at, Date.now())}</span>
      </button>
    </li>
  );
}

import React from "react";
import type { JobView } from "../api/client";
import { StatusBadge } from "../components/StatusBadge";

export function History({
  jobs,
  nextCursor,
  loading,
  onNext,
  onRefresh,
  onOpen,
}: {
  jobs: JobView[];
  nextCursor: string;
  loading: boolean;
  onNext: () => void;
  onRefresh: () => void;
  onOpen: (jobId: string) => void;
}): React.ReactElement {
  return (
    <section aria-label="Update history">
      <h2>History</h2>
      <div className="row-actions">
        <button type="button" onClick={onRefresh} disabled={loading} aria-label="Refresh history">
          {loading ? "Loading…" : "Refresh"}
        </button>
        {nextCursor ? (
          <button type="button" onClick={onNext} disabled={loading} aria-label="Load older jobs">
            Older
          </button>
        ) : null}
      </div>
      {jobs.length === 0 ? (
        <p className="hint">No jobs yet.</p>
      ) : (
        <ol className="history-list">
          {jobs.map((j) => (
            <li key={j.id}>
              <button type="button" className="history-item" onClick={() => onOpen(j.id)} aria-label={`Open job ${j.id}`}>
                <StatusBadge status={j.state} />
                <span className="history-main">
                  {j.tool_id} — {j.before_version || "?"} → {j.after_version || "…"}
                </span>
                <span className="history-meta">{j.created_at || ""}</span>
              </button>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

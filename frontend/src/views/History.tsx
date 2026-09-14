import React from "react";
import type { JobView } from "../api/client";
import { HistoryRow } from "../components/HistoryRow";

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
      <header className="section-head">
        <h2>Update history</h2>
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
      </header>
      <p className="hint">
        Outcomes are separated: succeeded, failed, blocked, interrupted, and health-check failures. Open a row
        for logs and check results from the existing job APIs.
      </p>
      {jobs.length === 0 ? (
        <p className="hint">No jobs yet.</p>
      ) : (
        <ol className="history-list">
          {jobs.map((j) => (
            <HistoryRow key={j.id} job={j} onOpen={onOpen} />
          ))}
        </ol>
      )}
    </section>
  );
}

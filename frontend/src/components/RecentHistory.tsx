import React from "react";
import type { JobView } from "../api/client";
import { HistoryRow } from "./HistoryRow";

export function RecentHistory({
  jobs,
  onOpen,
  onViewAll,
  limit = 5,
}: {
  jobs: JobView[];
  onOpen: (jobId: string) => void;
  onViewAll: () => void;
  limit?: number;
}): React.ReactElement {
  return (
    <section aria-label="Recent update attempts">
      <header className="section-head">
        <h2>Recent history</h2>
        <button type="button" onClick={onViewAll} aria-label="View full history">
          View full history
        </button>
      </header>
      {jobs.length === 0 ? (
        <p className="hint">No update attempts recorded yet.</p>
      ) : (
        <ol className="history-list history-compact">
          {jobs.slice(0, limit).map((job) => (
            <HistoryRow key={job.id} job={job} onOpen={onOpen} />
          ))}
        </ol>
      )}
    </section>
  );
}

import React from "react";
import type { LogRecord } from "../api/client";

export function LogViewer({
  records,
  truncated,
  hasMore,
  finalLogSeq,
  nextAfter,
  pendingTail,
  onLoadMore,
}: {
  records: LogRecord[];
  truncated: boolean;
  hasMore?: boolean;
  finalLogSeq?: number;
  nextAfter?: number;
  pendingTail?: boolean;
  onLoadMore?: () => void;
}): React.ReactElement {
  const [follow, setFollow] = React.useState(true);
  const [copied, setCopied] = React.useState(false);
  const boxRef = React.useRef<HTMLDivElement>(null);

  React.useEffect(() => {
    if (follow && boxRef.current) {
      boxRef.current.scrollTop = boxRef.current.scrollHeight;
    }
  }, [records.length, follow]);

  async function copyPlainText(): Promise<void> {
    const text = records.map((r) => `[${r.seq}] ${r.stream}: ${r.line}`).join("\n");
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      setCopied(false);
    }
  }

  const fls = typeof finalLogSeq === "number" ? finalLogSeq : -1;
  const cursor = typeof nextAfter === "number" ? nextAfter : 0;
  // R29 retention gap (API out of scope): when the server reports no more
  // pages but the cursor sits behind the durable final seq, records were
  // removed by retention — surface a gap note instead of silent completeness.
  const showGap = hasMore !== true && fls >= 0 && cursor < fls;
  const more = hasMore === true;

  return (
    <section className="log-viewer" aria-label="Job logs">
      <div className="log-toolbar">
        <button
          type="button"
          onClick={() => setFollow((f) => !f)}
          aria-pressed={follow}
          aria-label={follow ? "Pause log auto-follow" : "Resume log auto-follow"}
        >
          {follow ? "Pause follow" : "Follow"}
        </button>
        <button type="button" onClick={() => void copyPlainText()} aria-label="Copy logs as plain text">
          {copied ? "Copied" : "Copy as text"}
        </button>
        {more && onLoadMore ? (
          <button type="button" onClick={onLoadMore} aria-label="Load more log records">
            Load More
          </button>
        ) : null}
        <span className="hint" role="note">
          {records.length} records
          {more ? " — More records available" : ""}
          {truncated ? " — truncated, retention limits apply" : ""}
          {fls >= 0 ? ` — cursor ${cursor}/${fls}` : ""}
        </span>
      </div>
      {more ? (
        <p className="hint" role="note">
          More records available — additional pages exist beyond this view. Auto-drain continues while the
          job view is open; use Load More to fetch the next page immediately.
        </p>
      ) : null}
      {pendingTail === true ? (
        <p className="warn" role="status">
          Pending tail: terminal state reached but the log cursor is behind the durable final seq —
          draining remaining pages.
        </p>
      ) : null}
      {showGap ? (
        <p className="warn" role="note">
          Retention gap: log cursor {cursor} is behind final seq {fls} with no further pages — records
          in between were removed by retention (see RUNBOOK retention/tombstone ops). Shown records are
          ordered but not complete across the gap.
        </p>
      ) : null}
      {truncated ? (
        <p className="warn" role="note">
          Log output was truncated (retention limits). Shown records are ordered and complete up to the cap.
        </p>
      ) : null}
      {/* Rendered as text nodes only — never HTML (SPEC section 4). */}
      <div ref={boxRef} className="log-body" tabIndex={0} aria-label="Log output" role="log" aria-live="off">
        {records.length === 0 ? (
          <p className="hint">No log records yet.</p>
        ) : (
          records.map((r) => (
            <div key={r.seq} className={`log-line log-${r.stream}`}>
              <span className="log-seq">[{r.seq}]</span> <span className="log-stream">{r.stream}</span>{" "}
              <span className="log-text">{r.line}</span>
            </div>
          ))
        )}
      </div>
    </section>
  );
}

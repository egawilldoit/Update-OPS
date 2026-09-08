import React from "react";
import type { LogRecord } from "../api/client";

export function LogViewer({
  records,
  truncated,
}: {
  records: LogRecord[];
  truncated: boolean;
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
        <span className="hint" role="note">
          {records.length} records{truncated ? " — truncated, retention limits apply" : ""}
        </span>
      </div>
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

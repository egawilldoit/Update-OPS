import React from "react";
import { isStale, type HealthView } from "../api/client";
import { StatusBadge } from "./StatusBadge";

export function HealthPanel({ health }: { health: HealthView | null }): React.ReactElement {
  if (!health) {
    return (
      <section className="health-panel" aria-label="Service health">
        <h2>Health</h2>
        <p className="hint">Loading…</p>
      </section>
    );
  }
  const stale = isStale(health.checked_at);
  return (
    <section className="health-panel" aria-label="Service health">
      <h2>Health</h2>
      <ul className="health-list">
        <li>
          API <StatusBadge status={health.api} />
        </li>
        <li>
          Database <StatusBadge status={health.database} />
        </li>
        <li>
          Worker <StatusBadge status={health.worker} />
        </li>
      </ul>
      <p className="health-meta">
        Checked {health.checked_at || "never"}
        {stale ? " — labeled stale (older than 5 min)" : ""}
      </p>
      {health.recovery_required ? (
        <p className="warn" role="alert">
          Recovery required — new updates are blocked until SSH reconcile clears it.
        </p>
      ) : null}
    </section>
  );
}

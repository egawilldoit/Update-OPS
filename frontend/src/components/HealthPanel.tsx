import React from "react";
import { isStale, type HealthView } from "../api/client";
import { StatusBadge } from "./StatusBadge";

export function HealthPanel({
  health,
  disconnected,
}: {
  health: HealthView | null;
  disconnected?: boolean;
}): React.ReactElement {
  const forcedStale = disconnected === true;
  if (!health) {
    return (
      <section className="health-panel" aria-label="Service health">
        <h2>Health</h2>
        <p className="hint">{forcedStale ? "stale — disconnected" : "Loading…"}</p>
      </section>
    );
  }
  const stale = isStale(health.checked_at) || forcedStale;
  // Disconnected forces stale presentation regardless of cached values:
  // greyed, no green, explicit "stale — disconnected" label.
  const apiStatus = forcedStale ? "stale" : health.api;
  const dbStatus = forcedStale ? "stale" : health.database;
  const workerStatus = forcedStale ? "stale" : health.worker;
  return (
    <section className={forcedStale ? "health-panel health-stale" : "health-panel"} aria-label="Service health">
      <h2>Health</h2>
      <ul className="health-list">
        <li>
          API <StatusBadge status={apiStatus} />
        </li>
        <li>
          Database <StatusBadge status={dbStatus} />
        </li>
        <li>
          Worker <StatusBadge status={workerStatus} />
        </li>
      </ul>
      <p className="health-meta">
        Checked {health.checked_at || "never"}
        {forcedStale ? " — stale — disconnected" : stale ? " — labeled stale (older than 5 min)" : ""}
      </p>
      {forcedStale ? (
        <p className="warn" role="note">
          stale — disconnected: cached values are never shown as green.
        </p>
      ) : null}
      {health.recovery_required && !forcedStale ? (
        <p className="warn" role="alert">
          Recovery required — new updates are blocked until SSH reconcile clears it.
        </p>
      ) : null}
    </section>
  );
}

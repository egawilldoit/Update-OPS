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
  // R21: stale health is NEVER green. When the observation is older than
  // 5 min (or the API is unreachable), every badge renders the stale style
  // with explicit stale text — cached green is never shown as current.
  const apiStatus = stale ? "stale" : health.api;
  const dbStatus = stale ? "stale" : health.database;
  const workerStatus = stale ? "stale" : health.worker;
  return (
    <section className={stale ? "health-panel health-stale" : "health-panel"} aria-label="Service health">
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
      {stale && !forcedStale ? (
        <p className="warn" role="note">
          stale: health observation is older than 5 min — values are not current.
        </p>
      ) : null}
      {forcedStale ? (
        <p className="warn" role="note">
          stale — disconnected: cached values are never shown as green.
        </p>
      ) : null}
      {health.recovery_required && !stale ? (
        <p className="warn" role="alert">
          Recovery required — new updates are blocked until SSH reconcile clears it.
        </p>
      ) : null}
      {health.recovery_required && stale ? (
        <p className="warn" role="alert">
          Recovery flag is set (last known) — verify over SSH; display is stale.
        </p>
      ) : null}
    </section>
  );
}

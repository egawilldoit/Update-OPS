import React from "react";
import { isStale, type HealthView } from "../api/client";
import { maintenanceDetail, maintenanceLabel, type MaintenanceState } from "../operational";
import { StatusBadge } from "./StatusBadge";

function overallStatus(
  health: HealthView,
  stale: boolean,
  maintenance: MaintenanceState,
): { status: string; label: string } {
  if (stale) return { status: "stale", label: "stale" };
  if (health.api === "down" || health.database === "down" || health.worker === "down") {
    return { status: "degraded", label: "degraded" };
  }
  if (health.api === "degraded" || health.worker === "stale" || health.recovery_required) {
    return { status: "degraded", label: "degraded" };
  }
  if (maintenance.kind === "active") return { status: "degraded", label: "maintenance" };
  return { status: "ok", label: "operational" };
}

export function HealthPanel({
  health,
  disconnected,
  maintenance,
}: {
  health: HealthView | null;
  disconnected?: boolean;
  maintenance: MaintenanceState;
}): React.ReactElement {
  const forcedStale = disconnected === true;
  if (!health) {
    return (
      <section className="health-panel" aria-label="Control plane health">
        <header className="section-head">
          <h2>Control plane</h2>
          <StatusBadge status={forcedStale ? "stale" : "unknown"} label={forcedStale ? "stale" : "loading"} />
        </header>
        <p className="hint">
          {forcedStale ? "stale — disconnected: cached control-plane values are not current." : "Loading…"}
        </p>
      </section>
    );
  }
  const stale = isStale(health.checked_at) || forcedStale;
  const apiStatus = stale ? "stale" : health.api;
  const dbStatus = stale ? "stale" : health.database;
  const workerStatus = stale ? "stale" : health.worker;
  const overall = overallStatus(health, stale, maintenance);
  return (
    <section className={stale ? "health-panel health-stale" : "health-panel"} aria-label="Control plane health">
      <header className="section-head">
        <h2>Control plane</h2>
        <StatusBadge status={overall.status} label={overall.label} />
      </header>
      <ul className="health-list">
        <li>
          <span className="health-key">API</span> <StatusBadge status={apiStatus} />
        </li>
        <li>
          <span className="health-key">Database</span> <StatusBadge status={dbStatus} />
        </li>
        <li>
          <span className="health-key">Worker</span> <StatusBadge status={workerStatus} />
        </li>
        <li>
          <span className="health-key">Maintenance</span>{" "}
          <StatusBadge status={maintenanceLabel(maintenance)} />
        </li>
      </ul>
      <p className="health-meta">
        Checked {health.checked_at || "never"}
        {forcedStale ? " — stale — disconnected" : stale ? " — labeled stale (older than 5 min)" : ""}
      </p>
      <p className="hint">{maintenanceDetail(maintenance)}</p>
      {stale && !forcedStale ? (
        <p className="warn" role="note">
          stale: control-plane observation is older than 5 min — values are not current.
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

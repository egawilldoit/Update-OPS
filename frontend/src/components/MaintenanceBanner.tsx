import React from "react";
import type { MaintenanceState } from "../operational";

export function MaintenanceBanner({ state }: { state: MaintenanceState }): React.ReactElement | null {
  if (state.kind !== "active") return null;
  return (
    <section className="banner banner-maintenance" role="status" aria-live="polite" aria-label="Maintenance mode">
      <strong>Maintenance mode</strong> — New update plans are disabled while the console is drained for
      maintenance. Read-only checks, health, and history remain available.
      {state.source === "api" ? " Detected from a request the API refused during maintenance." : ""}
    </section>
  );
}

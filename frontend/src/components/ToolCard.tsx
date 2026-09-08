import React from "react";
import { isStale, type ToolCard as Card } from "../api/client";
import { StatusBadge } from "./StatusBadge";

const DISPLAY: Record<string, string> = {
  hermes: "Hermes",
  opencode: "OpenCode",
  codex: "Codex",
  t3: "T3",
};

export function ToolCard({
  card,
  updateDisabled,
  disableReason,
  onCheck,
  onPlan,
  checking,
  planning,
  disconnected,
}: {
  card: Card;
  updateDisabled: boolean;
  disableReason: string;
  onCheck: () => void;
  onPlan: () => void;
  checking: boolean;
  planning: boolean;
  disconnected?: boolean;
}): React.ReactElement {
  const stale = isStale(card.checked_at);
  // Disconnected forces stale presentation regardless of cached values:
  // never render cached green as current while the API is unreachable.
  const forcedStale = disconnected === true;
  const healthLabel = forcedStale ? "stale" : stale && card.health !== "stale" ? "stale" : card.health;
  const name = DISPLAY[card.id] ?? card.id;
  return (
    <article
      className={forcedStale ? "tool-card tool-card-stale" : "tool-card"}
      aria-label={`${name} status card${forcedStale ? " (stale — disconnected)" : ""}`}
    >
      <header className="tool-card-head">
        <h2>{name}</h2>
        <StatusBadge status={healthLabel} />
      </header>
      <dl className="tool-facts">
        <div>
          <dt>Installed</dt>
          <dd>{card.installed_version || "unknown"}</dd>
        </div>
        <div>
          <dt>Available target</dt>
          <dd>{card.available_target || "unknown"}</dd>
        </div>
        <div>
          <dt>Channel</dt>
          <dd>{card.channel || "—"}</dd>
        </div>
        <div>
          <dt>Last success</dt>
          <dd>{card.last_success || "never"}</dd>
        </div>
        <div>
          <dt>Last attempt</dt>
          <dd>{card.last_attempt || "never"}</dd>
        </div>
        <div>
          <dt>Last checked</dt>
          <dd>
            {card.checked_at || "never"}
            {forcedStale ? " (stale — disconnected)" : stale ? " (stale)" : ""}
          </dd>
        </div>
        {forcedStale ? (
          <div>
            <dt>Status</dt>
            <dd>stale — disconnected</dd>
          </div>
        ) : null}
        {card.health_detail ? (
          <div>
            <dt>Health detail</dt>
            <dd>{card.health_detail}</dd>
          </div>
        ) : null}
        {card.discovery_error ? (
          <div>
            <dt>Discovery</dt>
            <dd>unknown — {card.discovery_error}</dd>
          </div>
        ) : null}
      </dl>
      <div className="tool-actions">
        <button type="button" onClick={onCheck} disabled={checking} aria-label={`Check ${name} again`}>
          {checking ? "Checking…" : "Check again"}
        </button>
        <button
          type="button"
          onClick={onPlan}
          disabled={updateDisabled || planning}
          aria-label={`Preview update for ${name}`}
          title={updateDisabled ? disableReason : `Preview update for ${name}`}
        >
          {planning ? "Loading…" : "Update…"}
        </button>
      </div>
      {updateDisabled && disableReason ? (
        <p className="hint" role="note">
          {disableReason}
        </p>
      ) : null}
    </article>
  );
}

import React from "react";
import type { JobView, ToolCard as Card } from "../api/client";
import type { CheckState } from "../useToolChecks";
import { buildToolView, formatTimestamp } from "../operational";
import { StatusBadge } from "./StatusBadge";

export function ToolCard({
  card,
  updateBlocked,
  blockedReason,
  checkState,
  lastFailure,
  planning,
  disconnected,
  onCheck,
  onPlan,
}: {
  card: Card;
  updateBlocked: boolean;
  blockedReason: string;
  checkState?: CheckState;
  lastFailure?: JobView;
  planning: boolean;
  disconnected?: boolean;
  onCheck: () => void;
  onPlan: () => void;
}): React.ReactElement {
  const view = buildToolView({ card, checkState, updateBlocked, blockedReason, lastFailure, disconnected });
  const busy = checkState?.kind === "checking" || checkState?.kind === "pending";
  const failure = view.lastFailure;
  const blocked = view.eligibility.state === "blocked";
  return (
    <article
      className={view.stale ? "tool-card tool-card-stale" : "tool-card"}
      aria-label={`${view.name} status card${disconnected ? " (stale — disconnected)" : ""}`}
    >
      <header className="tool-card-head">
        <div className="tool-card-title">
          <h2>{view.name}</h2>
          <p className="tool-card-summary">{view.summary}</p>
        </div>
        <StatusBadge status={view.health} />
      </header>

      <dl className="tool-facts">
        <div>
          <dt>Installed</dt>
          <dd className="mono">{view.installed}</dd>
        </div>
        <div>
          <dt>Available target</dt>
          <dd className="mono">{view.target}</dd>
        </div>
        <div>
          <dt>Channel</dt>
          <dd>{view.channel}</dd>
        </div>
        <div>
          <dt>Health</dt>
          <dd>{view.healthDetail}</dd>
        </div>
        <div>
          <dt>Last successful check</dt>
          <dd className="mono">
            {formatTimestamp(card.checked_at || card.last_success_at || "")}
            {view.stale ? " (stale)" : ""}
          </dd>
        </div>
        <div>
          <dt>Latest check</dt>
          <dd>
            {view.check.label}
            {view.check.at ? <span className="meta"> {formatTimestamp(view.check.at)}</span> : null}
          </dd>
        </div>
        <div>
          <dt>Last successful update</dt>
          <dd className="mono">{formatTimestamp(view.lastSuccess)}</dd>
        </div>
        {failure ? (
          <div>
            <dt>Last failed attempt</dt>
            <dd>
              {failure.outcome.label}
              {failure.reason.title && failure.reason.title !== failure.outcome.label
                ? ` — ${failure.reason.title}`
                : ""}
              : {failure.reason.message}
              {failure.at ? <span className="meta"> ({formatTimestamp(failure.at)})</span> : null}
            </dd>
          </div>
        ) : null}
      </dl>

      {view.check.kind === "pending" ? (
        <p className="check-note" role="status">
          {view.check.label} {view.check.detail}
        </p>
      ) : null}
      {view.check.kind === "checking" ? (
        <p className="check-note" role="status">
          {view.check.label} {view.check.detail}
        </p>
      ) : null}
      {view.check.kind === "failed" && view.check.alert ? (
        <p className="error" role="alert">
          Check failed: {view.check.detail}
        </p>
      ) : null}
      {view.check.kind === "failed" && !view.check.alert ? (
        <p className="warn" role="note">
          {view.check.detail}
        </p>
      ) : null}
      {view.stale && view.check.kind !== "failed" ? (
        <p className="warn" role="note">
          Observation is stale — the values above are the last confirmed state, not current.
        </p>
      ) : null}

      <div className="tool-actions">
        <button type="button" onClick={onCheck} disabled={busy} aria-label={`Check ${view.name} again`}>
          {view.check.kind === "checking" || view.check.kind === "pending" ? "Checking…" : "Check again"}
        </button>
        <button
          type="button"
          onClick={onPlan}
          disabled={updateBlocked || planning}
          aria-label={`Preview update for ${view.name}`}
          title={updateBlocked ? blockedReason : `Preview update for ${view.name}`}
        >
          {planning ? "Loading…" : "Plan update"}
        </button>
      </div>

      <p className={blocked ? "tool-gate tool-gate-blocked" : "tool-gate"} role="note">
        <strong>{view.eligibility.label}</strong> — {view.eligibility.reason} {view.eligibility.action}
      </p>
    </article>
  );
}

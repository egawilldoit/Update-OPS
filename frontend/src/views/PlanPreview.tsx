import React from "react";
import type { PlanView } from "../api/client";
import { StatusBadge } from "../components/StatusBadge";

const DISPLAY: Record<string, string> = {
  hermes: "Hermes",
  opencode: "OpenCode",
  codex: "Codex",
  t3: "T3",
};

export function PlanPreview({
  toolId,
  plan,
  loading,
  error,
  ack,
  onAckChange,
  onStart,
  starting,
  blockedReason,
}: {
  toolId: string;
  plan: PlanView | null;
  loading: boolean;
  error: string;
  ack: boolean;
  onAckChange: (v: boolean) => void;
  onStart: () => void;
  starting: boolean;
  blockedReason: string;
}): React.ReactElement {
  const name = DISPLAY[toolId] ?? toolId;
  if (loading) {
    return (
      <section aria-label={`Update preview for ${name}`}>
        <h2>Update preview — {name}</h2>
        <p className="hint">Loading server-generated plan…</p>
      </section>
    );
  }
  if (error) {
    return (
      <section aria-label={`Update preview for ${name}`}>
        <h2>Update preview — {name}</h2>
        <p className="error" role="alert">
          {error}
        </p>
      </section>
    );
  }
  if (!plan) {
    return (
      <section aria-label={`Update preview for ${name}`}>
        <h2>Update preview — {name}</h2>
        <p className="hint">No plan loaded.</p>
      </section>
    );
  }
  const busy = plan.activity_state === "busy";
  const unknown = plan.activity_state === "unknown";
  const blocked = busy || blockedReason !== "";
  return (
    <section aria-label={`Update preview for ${name}`}>
      <h2>Update preview — {name}</h2>
      <dl className="plan-facts">
        <div>
          <dt>Target</dt>
          <dd>
            {plan.target} ({plan.target_mode}
            {plan.target_mode === "native_latest" ? " — main/latest-tracking installer" : ""})
          </dd>
        </div>
        <div>
          <dt>Channel</dt>
          <dd>{plan.channel || "—"}</dd>
        </div>
        <div>
          <dt>Affected services</dt>
          <dd>{plan.services.length > 0 ? plan.services.join(", ") : "none declared"}</dd>
        </div>
        <div>
          <dt>Backup scope</dt>
          <dd>
            {Object.keys(plan.backup_scope).length > 0 ? (
              <ul>
                {Object.entries(plan.backup_scope).map(([k, v]) => (
                  <li key={k}>
                    {k}: {v}
                  </li>
                ))}
              </ul>
            ) : (
              "none declared — required backup capability is checked at execution"
            )}
          </dd>
        </div>
        <div>
          <dt>Activity</dt>
          <dd>
            <StatusBadge status={plan.activity_state} /> {plan.activity_evidence || "no evidence"}
          </dd>
        </div>
        <div>
          <dt>Restart impact</dt>
          <dd>{plan.restart_impact || "not declared"}</dd>
        </div>
        <div>
          <dt>Plan expires</dt>
          <dd>{plan.expires_at} (5-minute server-owned preview)</dd>
        </div>
        <div>
          <dt>Steps</dt>
          <dd>{plan.steps.length > 0 ? plan.steps.join(" → ") : "preflight → backup → updating → verifying"}</dd>
        </div>
      </dl>
      {busy ? (
        <p className="error" role="alert">
          Blocked: tool reports active work ({plan.activity_evidence || "busy"}). Execution is disabled until idle.
        </p>
      ) : null}
      {unknown ? (
        <label className="ack-row">
          <input
            type="checkbox"
            checked={ack}
            onChange={(e) => onAckChange(e.target.checked)}
            aria-label="Acknowledge possible interruption from unknown activity"
          />
          <span>
            Activity is unknown — I acknowledge this update may interrupt unseen work. (Unchecked by default; the
            acknowledgment is recorded on the job.)
          </span>
        </label>
      ) : null}
      {blockedReason && !busy ? (
        <p className="warn" role="note">
          {blockedReason}
        </p>
      ) : null}
      <div className="row-actions">
        <button
          type="button"
          onClick={onStart}
          disabled={blocked || (unknown && !ack) || starting}
          aria-label={`Start update for ${name}`}
        >
          {starting ? "Starting…" : "Start update"}
        </button>
      </div>
      <p className="hint">
        A supported backup is not a promise of rollback — scope and location are shown to the owner only.
      </p>
    </section>
  );
}

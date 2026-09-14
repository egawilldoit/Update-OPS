import React from "react";
import type { PlanView, ToolCard as Card } from "../api/client";
import { formatBytes, toolName, type FailureView, type MaintenanceState } from "../operational";
import { StatusBadge } from "../components/StatusBadge";

function targetModeSentence(mode: string): string {
  if (mode === "exact") return "Pinned to one exact version.";
  if (mode === "native_latest") return "Tracks the installer's current native build.";
  return "Target mode not declared.";
}

export function PlanPreview({
  toolId,
  current,
  plan,
  loading,
  error,
  ack,
  onAckChange,
  onStart,
  starting,
  blockedReason,
  maintenance,
  recoveryRequired,
}: {
  toolId: string;
  current?: Card;
  plan: PlanView | null;
  loading: boolean;
  error: FailureView | null;
  ack: boolean;
  onAckChange: (v: boolean) => void;
  onStart: () => void;
  starting: boolean;
  blockedReason: string;
  maintenance: MaintenanceState;
  recoveryRequired: boolean;
}): React.ReactElement {
  const name = toolName(toolId);
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
        <div className="plan-error" role="alert">
          <p className="error">
            <strong>{error.title}</strong> — {error.message}
          </p>
          <p className="hint">Next: {error.action}</p>
        </div>
        <details className="diagnostics">
          <summary>Diagnostic code</summary>
          <p className="mono">{error.code}</p>
        </details>
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
  const space = formatBytes(plan.required_space_bytes);
  return (
    <section aria-label={`Update preview for ${name}`}>
      <h2>Update preview — {name}</h2>
      <p className="hint">
        Review the contract below. Nothing is installed until you confirm. The plan is a single-use, 5-minute
        preview bound to the current configuration.
      </p>
      <dl className="plan-facts">
        <div>
          <dt>Current installed</dt>
          <dd className="mono">{current?.installed_version || "unknown"}</dd>
        </div>
        <div>
          <dt>Target</dt>
          <dd>
            <span className="mono">{plan.target}</span> — {targetModeSentence(plan.target_mode)}
          </dd>
        </div>
        <div>
          <dt>Channel / method</dt>
          <dd>{plan.channel || "—"}</dd>
        </div>
        <div>
          <dt>Required steps</dt>
          <dd>
            {plan.steps.length > 0
              ? plan.steps.join(" → ")
              : "preflight → backup → updating → verifying"}
          </dd>
        </div>
        <div>
          <dt>Affected services</dt>
          <dd>{plan.services.length > 0 ? plan.services.join(", ") : "none declared"}</dd>
        </div>
        <div>
          <dt>Backup and recovery</dt>
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
          <dt>Disk requirement</dt>
          <dd>{space ? `Requires ~${space} free on the affected filesystem.` : "No extra space requirement reported."}</dd>
        </div>
        <div>
          <dt>Activity right now</dt>
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
          <dd>
            <span className="mono">{plan.expires_at}</span> — single use; a retry needs a fresh preview.
          </dd>
        </div>
      </dl>
      <p className="hint">
        A supported backup is not a promise of rollback — scope and location are shown to the owner only.
      </p>
      {maintenance.kind === "active" ? (
        <p className="warn" role="note">
          Maintenance mode — even with a valid plan, the console refuses to start updates while drained.
        </p>
      ) : null}
      {recoveryRequired ? (
        <p className="error" role="alert">
          Recovery required — a previous update may have left a tool in a partial state. Reconcile over SSH
          before starting anything.
        </p>
      ) : null}
      {busy ? (
        <p className="error" role="alert">
          Blocked: the tool reports active work ({plan.activity_evidence || "busy"}). Execution is disabled until
          it is idle.
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
            The console cannot see whether {name} is actively working. Until you acknowledge, the server refuses
            to start the job. Acknowledge only if you accept a possible interruption of unseen work. (Unchecked by
            default; the acknowledgment is recorded on the job.)
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
          {starting ? "Starting…" : "Confirm and start update"}
        </button>
      </div>
    </section>
  );
}

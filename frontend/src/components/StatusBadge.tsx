import React from "react";

const CLASS_FOR: Record<string, string> = {
  healthy: "badge badge-healthy",
  degraded: "badge badge-degraded",
  unhealthy: "badge badge-unhealthy",
  unknown: "badge badge-unknown",
  stale: "badge badge-stale",
  succeeded: "badge badge-healthy",
  failed: "badge badge-unhealthy",
  health_failed: "badge badge-unhealthy",
  blocked: "badge badge-degraded",
  interrupted: "badge badge-degraded",
  accepted: "badge badge-unknown",
  preflight: "badge badge-unknown",
  backup: "badge badge-unknown",
  updating: "badge badge-unknown",
  verifying: "badge badge-unknown",
  ok: "badge badge-healthy",
  down: "badge badge-unhealthy",
  pass: "badge badge-healthy",
  fail: "badge badge-unhealthy",
  not_applicable: "badge badge-unknown",
  idle: "badge badge-healthy",
  busy: "badge badge-degraded",
};

export function StatusBadge({ status, label }: { status: string; label?: string }): React.ReactElement {
  const cls = CLASS_FOR[status] ?? "badge badge-unknown";
  // Text label always accompanies color (PRD: text labels alongside colors).
  return (
    <span className={cls} role="status" aria-label={`status: ${label ?? status}`}>
      {label ?? status}
    </span>
  );
}

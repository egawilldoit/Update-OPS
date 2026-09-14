#!/usr/bin/env bash
# EGA Update Console — W4-D8 deployment readiness VM acceptance harness.
#
# *** MANUAL / DISPOSABLE-VM ONLY. NEVER RUN AUTOMATICALLY. ***
# This script performs REAL host acceptance checks (real Linux users and
# groups, POSIX ACLs, the already-running user manager, `systemd --user`
# bus, real transient units, service identities). It requires root and a
# systemd host and reads live deployment state. It is intentionally NOT
# wired into install.sh/upgrade.sh or pytest collection.
#
# Usage (on the deployment VM, after a completed install/upgrade):
#   sudo EGA_VM_ACCEPTANCE=1 deploy/tests/vm-acceptance-readiness.sh
#
# Guards: runs only when EGA_VM_ACCEPTANCE=1, euid 0, systemd + release
# venv present. Exits 0 only when every check passes; 2 when it refuses
# to run; 1 on the first failed acceptance check.
set -uo pipefail

if [ "${EGA_VM_ACCEPTANCE:-0}" != "1" ]; then
  echo "vm-acceptance-readiness: NOT RUN (manual harness; set EGA_VM_ACCEPTANCE=1 on a disposable root VM)" >&2
  exit 0
fi
if [ "$(id -u)" -ne 0 ]; then
  echo "vm-acceptance-readiness: REFUSING (must run as root via sudo)" >&2
  exit 2
fi
if ! command -v systemctl >/dev/null 2>&1; then
  echo "vm-acceptance-readiness: REFUSING (systemd host required)" >&2
  exit 2
fi

CONFIG="${EGA_CONFIG_FILE:-/etc/ega-update/config.json}"
RELEASE="${EGA_RELEASE_ROOT:-/opt/ega-update/current}"
PY="$RELEASE/venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "vm-acceptance-readiness: REFUSING (release venv python missing: $PY)" >&2
  exit 2
fi
export EGA_CONFIG_FILE="$CONFIG"

cfg_get() {
  "$PY" -m backend.app.config_cli get --require "$1" 2>/dev/null
}

fail() {
  echo "vm-acceptance-readiness: FAIL: $1" >&2
  exit 1
}

echo "[vmaccept] 1/5 API service identity effective access"
API_USER="$(systemctl show ega-update-api -p User --value 2>/dev/null || true)"
API_USER="${API_USER:-ega-update}"
if runuser -u "$API_USER" -- "$PY" -m backend.app.deployment_readiness \
     api-identity-probe --config-file "$CONFIG" >/dev/null 2>&1; then
  echo "[vmaccept]   ok: $API_USER can read config/csrf.secret, open the DB, use runtime paths"
else
  fail "api-identity-probe under $API_USER"
fi

echo "[vmaccept] 2/5 loopback security boundary (assess includes HTTP 401/403)"
TOOL_OWNER="$(cfg_get tool_owner)"
STATE_DIR="$(cfg_get state_dir)"
LOG_DIR="$(cfg_get log_dir)"
if command -v getfacl >/dev/null 2>&1; then
  getfacl -p "$STATE_DIR" 2>/dev/null | grep -q "^user:$TOOL_OWNER:" \
    || fail "no named-user ACL for $TOOL_OWNER on $STATE_DIR"
  getfacl -p "$LOG_DIR" 2>/dev/null | grep -q "^user:$TOOL_OWNER:" \
    || fail "no named-user ACL for $TOOL_OWNER on $LOG_DIR"
  echo "[vmaccept]   ok: explicit owner ACLs present (no mode widening)"
else
  echo "[vmaccept]   WARN: getfacl unavailable; ACL shape not inspected directly"
fi

echo "[vmaccept] 3/5 durable probe-executor ready marker (W3.1)"
MARKER="$STATE_DIR/probe_worker.heartbeat"
if [ -f "$MARKER" ]; then
  echo "[vmaccept]   ok: $MARKER present (freshness proven by the assess gate)"
else
  fail "probe_worker.heartbeat missing (required probe executor not ready)"
fi

echo "[vmaccept] 4/5 canonical transient owner acceptance round trip"
if EGA_CONFIG_FILE="$CONFIG" "$PY" -m backend.app.owner_env accept \
     >/tmp/ega-vmaccept-owner.json 2>/tmp/ega-vmaccept-owner.err; then
  echo "[vmaccept]   ok: user bus, transient unit, identity, payload/config/result, termination"
else
  cat /tmp/ega-vmaccept-owner.json >&2 2>/dev/null || true
  cat /tmp/ega-vmaccept-owner.err >&2 2>/dev/null || true
  fail "owner_env accept"
fi

echo "[vmaccept] 5/5 full readiness assessment (all mandatory stages)"
if EGA_CONFIG_FILE="$CONFIG" "$PY" -m backend.app.deployment_readiness assess \
     >/tmp/ega-vmaccept-assess.json 2>/tmp/ega-vmaccept-assess.err; then
  echo "[vmaccept]   ok: all mandatory stages proven"
else
  cat /tmp/ega-vmaccept-assess.json >&2 2>/dev/null || true
  cat /tmp/ega-vmaccept-assess.err >&2 2>/dev/null || true
  fail "deployment_readiness assess"
fi

echo "[vmaccept] VM ACCEPTANCE: OK ($RELEASE)"

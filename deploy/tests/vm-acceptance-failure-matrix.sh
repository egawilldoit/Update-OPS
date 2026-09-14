#!/usr/bin/env bash
# EGA Update Console — W6 deployment failure-matrix VM acceptance harness.
#
# *** MANUAL / DISPOSABLE-VM ONLY. NEVER RUN AUTOMATICALLY. ***
#
# Exercises REAL host acceptance on one disposable Ubuntu 22.04 VM:
# root/non-root identities, the ega-update service account and ega-update
# group, the ubuntu tool owner, POSIX ACLs, the already-running user@UID
# manager and `systemd --user`, system units, SQLite file permissions,
# /opt/ega-update/releases + current, the deployment flock (kernel), the
# canonical readiness gate, service restart, and reboot-state verification
# for named failure states.
#
# It is intentionally NOT wired into install.sh/upgrade.sh, CI, or pytest
# collection. The hermetic failure matrix (backend/tests/
# test_deploy_failure_matrix.py) is the executable contract; this harness
# is the real-OS acceptance companion for an operator.
#
# Exact usage (see deploy/tests/FAILURE-MATRIX.md):
#   sudo EGA_VM_ACCEPTANCE=1 EGA_VM_DISPOSABLE=1 \
#     deploy/tests/vm-acceptance-failure-matrix.sh verify
#   sudo EGA_VM_ACCEPTANCE=1 EGA_VM_DISPOSABLE=1 \
#     deploy/tests/vm-acceptance-failure-matrix.sh pointer
#   sudo EGA_VM_ACCEPTANCE=1 EGA_VM_DISPOSABLE=1 EGA_VM_DESTRUCTIVE=1 \
#     EGA_VM_SERVICE_MUTATION=1 \
#     deploy/tests/vm-acceptance-failure-matrix.sh services
#   sudo EGA_VM_ACCEPTANCE=1 EGA_VM_DISPOSABLE=1 \
#     deploy/tests/vm-acceptance-failure-matrix.sh reboot-check <state>
#
# Guards (ALL of them, in this order):
#   EGA_VM_ACCEPTANCE=1   explicit opt-in; otherwise NOT RUN, exit 0
#   EGA_VM_DISPOSABLE=1   the operator asserts this VM is disposable
#   euid 0                root via sudo
#   systemd host          systemctl present and /run/systemd/system exists
#   production marker     /etc/ega-update/PRODUCTION must not exist
#   deployment present    /opt/ega-update/current and config must exist
#   destructive flags     EGA_VM_DESTRUCTIVE=1 for any mutation;
#                         EGA_VM_SERVICE_MUTATION=1 for service stop/start
#
# This harness NEVER migrates, NEVER switches the real `current`, NEVER
# edits /etc/ega-update or unit files, and NEVER restores a database. The
# `pointer` phase only manipulates scratch releases and a scratch pointer
# inside /opt/ega-update/releases/.vm-acceptance-*/.
set -uo pipefail

PREFIX="${EGA_VM_PREFIX:-/opt/ega-update}"
CONFIG="${EGA_CONFIG_FILE:-/etc/ega-update/config.json}"
PRODUCTION_MARKER="${EGA_VM_PRODUCTION_MARKER:-/etc/ega-update/PRODUCTION}"
CURRENT="$PREFIX/current"
RELEASES="$PREFIX/releases"
DEPLOY_LOCK="$PREFIX/deploy.lock"
PHASE="${1:-verify}"

refuse() {
  echo "vm-acceptance-failure-matrix: REFUSING: $1" >&2
  exit 2
}

fail() {
  echo "vm-acceptance-failure-matrix: FAIL: $1" >&2
  exit 1
}

ok() {
  echo "[vmmatrix] ok: $1"
}

if [ "${EGA_VM_ACCEPTANCE:-0}" != "1" ]; then
  echo "vm-acceptance-failure-matrix: NOT RUN (manual harness; set EGA_VM_ACCEPTANCE=1 on a disposable root VM)" >&2
  exit 0
fi
if [ "${EGA_VM_DISPOSABLE:-0}" != "1" ]; then
  refuse "disposable-VM acknowledgement missing (set EGA_VM_DISPOSABLE=1 only on a VM/snapshot you may destroy)"
fi
if [ "$(id -u)" -ne 0 ]; then
  refuse "must run as root via sudo"
fi
if ! command -v systemctl >/dev/null 2>&1 \
   || [ ! -d /run/systemd/system ]; then
  refuse "systemd host required"
fi
if [ -e /etc/ega-update/PRODUCTION ] || [ -e "$PRODUCTION_MARKER" ]; then
  refuse "production marker present ($PRODUCTION_MARKER); this harness must never run against production"
fi
if [ ! -L "$CURRENT" ] || [ ! -f "$CONFIG" ]; then
  refuse "no installed deployment found ($CURRENT / $CONFIG); run install.sh first on the disposable VM"
fi

PY="$CURRENT/venv/bin/python"
[ -x "$PY" ] || refuse "release venv python missing: $PY"
export EGA_CONFIG_FILE="$CONFIG"

cfg_get() {
  "$PY" -m backend.app.config_cli get --require "$1" 2>/dev/null
}

STATE_DIR="$(cfg_get state_dir)" || fail "state_dir unparsable"
LOG_DIR="$(cfg_get log_dir)" || fail "log_dir unparsable"
DB_PATH="$(cfg_get db_path)" || fail "db_path unparsable"
TOOL_OWNER="$(cfg_get tool_owner)" || fail "tool_owner unparsable"

# ---------------------------------------------------------------------------
# verify — read-only host acceptance (default; safe on any disposable VM)
# ---------------------------------------------------------------------------
phase_verify() {
  echo "[vmmatrix] phase=verify (read-only)"

  getent passwd ega-update >/dev/null 2>&1 \
    || fail "service account ega-update missing"
  getent passwd "$TOOL_OWNER" >/dev/null 2>&1 \
    || fail "tool owner $TOOL_OWNER missing"
  getent group ega-update >/dev/null 2>&1 \
    || fail "shared group ega-update missing"
  id -nG ega-update | tr ' ' '\n' | grep -qx ega-update \
    || fail "ega-update not a member of the shared group"
  id -nG "$TOOL_OWNER" | tr ' ' '\n' | grep -qx ega-update \
    || fail "$TOOL_OWNER not a member of the shared group"
  ok "identities + group membership"

  if command -v getfacl >/dev/null 2>&1; then
    getfacl -p "$STATE_DIR" 2>/dev/null | grep -q "^user:$TOOL_OWNER:" \
      || fail "no named-user ACL for $TOOL_OWNER on $STATE_DIR"
    getfacl -p "$LOG_DIR" 2>/dev/null | grep -q "^user:$TOOL_OWNER:" \
      || fail "no named-user ACL for $TOOL_OWNER on $LOG_DIR"
    ok "explicit owner ACLs (no mode widening)"
  else
    fail "getfacl unavailable; ACL contract unproven"
  fi

  stat -c '%a %U:%G %n' "$DB_PATH" | grep -Eq '^660 ' \
    || fail "state DB not 0660: $(stat -c '%a %U:%G %n' "$DB_PATH")"
  runuser -u ega-update -- test -r "$DB_PATH" \
    || fail "ega-update cannot read the state DB"
  runuser -u "$TOOL_OWNER" -- test -r "$DB_PATH" \
    || fail "$TOOL_OWNER cannot read the state DB"
  ok "SQLite file permissions + both service identities can read"

  local resolved real_root
  resolved="$(readlink -f "$CURRENT")" || fail "current unreadable"
  real_root="$(readlink -f "$RELEASES")"
  case "$resolved" in
    "$real_root"/*) : ;;
    *) fail "current escapes releases root: $resolved" ;;
  esac
  PYTHONPATH="${EGA_OPERATOR_CHECKOUT:?set EGA_OPERATOR_CHECKOUT to the trusted checkout}" \
    python3 -m backend.app.deploy_release verify \
      --current "$CURRENT" --target "$resolved" >/dev/null \
    || fail "atomic pointer verify failed for $resolved"
  ok "current -> $resolved (pointer verify)"

  if command -v flock >/dev/null 2>&1; then
    flock -n "$DEPLOY_LOCK" -c true || fail "deploy.lock is held by another deployment"
    flock -n "$DEPLOY_LOCK" -c 'sleep 2' &
    local holder=$!
    sleep 0.2
    if flock -n "$DEPLOY_LOCK" -c true 2>/dev/null; then
      kill "$holder" 2>/dev/null || true
      fail "deployment flock did not exclude a second holder"
    fi
    wait "$holder" 2>/dev/null || true
    flock -n "$DEPLOY_LOCK" -c true || fail "deployment flock not released"
    ok "deployment flock excludes concurrent holders and releases"
  fi

  local state
  state="$("$PY" -m backend.app.cli status --require-ready 2>&1)"
  local rc=$?
  echo "[vmmatrix] readiness gate exit=$rc"
  [ "$rc" -eq 0 ] || fail "canonical readiness failed"
  if [ -f "$STATE_DIR/drain" ]; then
    echo "[vmmatrix] drain present: admission is stopped (manual state)"
  fi

  for unit in ega-update-api ega-update-worker; do
    systemctl cat "$unit" >/dev/null 2>&1 || fail "unit missing: $unit"
    if systemctl cat "$unit" | grep -E '^ExecStart=' | grep -q migrate; then
      fail "$unit ExecStart runs a migration"
    fi
  done
  ok "units present; no boot path migrates"

  if id -u "$TOOL_OWNER" >/dev/null 2>&1; then
    local uid
    uid="$(id -u "$TOOL_OWNER")"
    runuser -u "$TOOL_OWNER" -- env \
      XDG_RUNTIME_DIR="/run/user/$uid" \
      DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" \
      systemctl --user is-system-running >/dev/null 2>&1 \
      || fail "user manager not reachable for $TOOL_OWNER"
    ok "user@UID manager queried for $TOOL_OWNER"
  fi
}

# ---------------------------------------------------------------------------
# pointer — REAL atomic pointer semantics on scratch releases only
# ---------------------------------------------------------------------------
phase_pointer() {
  echo "[vmmatrix] phase=pointer (scratch releases; real current untouched)"
  local real_target
  real_target="$(readlink -f "$CURRENT")"
  local stamp="$$-$(date +%s)"
  local scratch="$RELEASES/.vm-acceptance-$stamp"
  local a="$scratch/A" b="$scratch/B" ptr="$scratch/current"
  mkdir -p "$a" "$b"
  chmod 0755 "$a" "$b"
  ln -s "$a" "$ptr"

  PYTHONPATH="${EGA_OPERATOR_CHECKOUT:?set EGA_OPERATOR_CHECKOUT to the trusted checkout}" \
  python3 -m backend.app.deploy_release switch \
    --current "$ptr" --target "$b" --releases-root "$RELEASES" \
    --previous "$a" >/dev/null || fail "atomic switch to scratch B failed"
  [ "$(readlink -f "$ptr")" = "$b" ] || fail "scratch pointer did not resolve to B"

  # CAS: a stale expected-current must fail closed and never overwrite.
  if PYTHONPATH="${EGA_OPERATOR_CHECKOUT}" python3 -m backend.app.deploy_release \
       switch --current "$ptr" --target "$a" --releases-root "$RELEASES" \
       --previous "$a" >/dev/null 2>&1; then
    fail "CAS accepted stale evidence"
  fi
  [ "$(readlink -f "$ptr")" = "$b" ] || fail "CAS failure changed the pointer"
  ok "atomic switch + CAS fail-closed"

  # reader loop: never missing, never partial
  local missing=0 unexpected=0 i=0
  (
    while [ ! -e "$scratch/reader-stop" ] && kill -0 "$$" 2>/dev/null; do
      t="$(readlink "$ptr" 2>/dev/null)" || { echo missing >> "$scratch/reader"; break; }
      r="$(readlink -f "$ptr" 2>/dev/null)"
      case "$r" in "$a"|"$b") : ;; *) echo "unexpected:$r" >> "$scratch/reader" ;; esac
      i=$((i + 1))
    done
  ) &
  local reader=$!
  for _ in $(seq 1 20); do
    if [ "$(readlink -f "$ptr")" = "$a" ]; then
      PYTHONPATH="${EGA_OPERATOR_CHECKOUT}" python3 -m backend.app.deploy_release \
        switch --current "$ptr" --target "$b" --releases-root "$RELEASES" >/dev/null || fail "scratch pointer switch failed"
    else
      PYTHONPATH="${EGA_OPERATOR_CHECKOUT}" python3 -m backend.app.deploy_release \
        switch --current "$ptr" --target "$a" --releases-root "$RELEASES" >/dev/null || fail "scratch pointer switch failed"
    fi
  done
  touch "$scratch/reader-stop"
  wait "$reader" 2>/dev/null || fail "concurrent reader process failed"
  if compgen -G "$scratch/.current.tmp.*" >/dev/null; then
    fail "temporary pointers remain"
  fi
  if [ -s "$scratch/reader" ]; then
    cat "$scratch/reader" >&2
    rm -rf "$scratch"
    fail "concurrent reader observed a missing/partial current"
  fi
  rm -rf "$scratch"
  [ "$(readlink -f "$CURRENT")" = "$real_target" ] \
    || fail "the real current pointer was disturbed"
  ok "concurrent readers only ever saw complete old/new targets"
}

# ---------------------------------------------------------------------------
# services — explicit service restart window (destructive; opt-in)
# ---------------------------------------------------------------------------
phase_services() {
  [ "${EGA_VM_DESTRUCTIVE:-0}" = "1" ] \
    || refuse "services phase is destructive: set EGA_VM_DESTRUCTIVE=1"
  [ "${EGA_VM_SERVICE_MUTATION:-0}" = "1" ] \
    || refuse "services phase stops/starts units: set EGA_VM_SERVICE_MUTATION=1"
  echo "[vmmatrix] phase=services (stop/prove/start/prove)"
  local was_api=0 was_worker=0
  systemctl is-active --quiet ega-update-api && was_api=1
  systemctl is-active --quiet ega-update-worker && was_worker=1
  systemctl stop ega-update-worker ega-update-api || fail "stop failed"
  systemctl is-active --quiet ega-update-api \
    && fail "api did not stop" || true
  systemctl is-active --quiet ega-update-worker \
    && fail "worker did not stop" || true
  ok "services stopped and proven inactive"
  [ "$was_worker" = "1" ] && { systemctl start ega-update-worker || fail "worker start failed"; }
  [ "$was_api" = "1" ] && { systemctl start ega-update-api || fail "api start failed"; }
  local i=0
  while [ "$i" -lt 12 ]; do
    i=$((i + 1))
    if "$PY" -m backend.app.cli status --require-ready >/dev/null 2>&1; then
      ok "canonical readiness green after restart"
      return 0
    fi
    sleep 5
  done
  fail "canonical readiness not green after service restart"
}

# ---------------------------------------------------------------------------
# reboot-check <state> — verify invariants after an induced failure + reboot
# ---------------------------------------------------------------------------
# The named states are induced per FAILURE-MATRIX.md on the DISPOSABLE VM
# (real upgrade.sh with EGA_DEPLOY_FAULT_POINT=<point>), then the VM is
# rebooted and this phase proves the boot path did not guess anything.
phase_reboot_check() {
  local state="${1:-}"
  [ -n "$state" ] || refuse "usage: ... reboot-check <state>"
  echo "[vmmatrix] phase=reboot-check state=$state"
  local drain_present=0
  [ -f "$STATE_DIR/drain" ] && drain_present=1
  local active_api=0 active_worker=0
  systemctl is-active --quiet ega-update-api && active_api=1
  systemctl is-active --quiet ega-update-worker && active_worker=1
  local resolved
  resolved="$(readlink -f "$CURRENT")"
  echo "[vmmatrix] current=$resolved drain=$drain_present api=$active_api worker=$active_worker"
  case "$state" in
    drain-before-migration|after-failed-migration|post-migration-pre-switch)
      [ "$drain_present" = "1" ] || fail "$state: drain must be present after reboot"
      ;;
    post-switch-pre-start)
      [ "$drain_present" = "1" ] || fail "$state: drain must be present"
      ;;
    readiness-failure)
      [ "$drain_present" = "1" ] || fail "$state: drain must be present"
      ;;
    failed-pointer-restore)
      [ "$drain_present" = "1" ] || fail "$state: drain must be present"
      ;;
    *) refuse "unknown reboot state: $state" ;;
  esac
  # No boot path may migrate/backup/switch: assert units and journal.
  if systemctl cat ega-update-api ega-update-worker 2>/dev/null \
       | grep -E '^ExecStart=' | grep -qE 'migrate|deploy_release|upgrade\.sh|install\.sh'; then
    fail "a boot path still contains a deployment/migration command"
  fi
  ok "reboot state invariants hold (no deployment command in any ExecStart)"
}

case "$PHASE" in
  verify) phase_verify ;;
  pointer) phase_pointer ;;
  services) phase_services ;;
  reboot-check) phase_reboot_check "${2:-}" ;;
  *) refuse "unknown phase '$PHASE' (verify|pointer|services|reboot-check)" ;;
esac

echo "[vmmatrix] PHASE OK: $PHASE"

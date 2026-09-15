#!/usr/bin/env bash
# EGA Update Console — shared deploy helpers (W4-D8).
#
# ONE implementation of the pre-undrain acceptance gate consumed by BOTH
# install.sh and upgrade.sh. The canonical readiness definition lives in
# backend/app/deployment_readiness.py and is exposed ONLY through
# `python -m backend.app.cli status --require-ready`; the shell never
# parses or re-implements readiness semantics — the process exit code is
# the contract (N01 pattern). The advisory loopback probe only paces
# retries while the API is still binding; it is NEVER sufficient for
# success.
#
# Sourced by install.sh/upgrade.sh after SCRIPT_DIR is resolved.

# ===== W5-D9 deployment serialization + atomic release pointer =====
#
# ONE atomic release-pointer primitive for BOTH scripts
# (backend/app/deploy_release.py, operator-side, stdlib only, run from
# the TRUSTED operator checkout — never from the candidate release).
# The shell never uses `ln -sfn` for `current`: unlink-then-create leaves
# a missing-`current` window. switch/restore create a temporary symlink
# in the SAME parent directory and commit with os.replace (rename(2)).
#
# Failure policy shared vocabulary: POINTER RESTORE (console symlink
# only), DATABASE ROLLBACK (never automatic), SERVICE RESTORATION
# (restarting previously-active units). Restoration happens ONLY when
# `validate-release.py --check-compat OLD NEW` proves the prior release
# can run the migrated DB; unknown/false compatibility stays in manual
# recovery.

# Deterministic test-only hook (W5-D9). Inert unless
# EGA_DEPLOY_FAULT_POINT names this exact point.
ega_maybe_fail() {
  local point="$1" what="${2:-}"
  [ "${EGA_DEPLOY_FAULT_POINT:-}" = "$point" ] || return 0
  echo "[deploy] FAULT INJECTED at ${point}${what:+: $what}" >&2
  return 1
}

# Delegate to the operator-checkout primitive (F06). Machine-readable
# JSON on stdout; exit codes distinguish inspect/validate/switch/verify.
# Runs from the trusted checkout in a subshell: `python3 -m` puts the CWD
# first on sys.path, so a candidate release sharing the `backend` package
# name must NEVER shadow the operator-side module (the scripts cd into
# the release dir before the switch).
ega_deploy_release() {
  (
    cd "$REPO_ROOT" || exit 1
    PYTHONPATH="$REPO_ROOT" python3 -m backend.app.deploy_release "$@"
  )
}

# Capture the exact PREVIOUS raw/resolved pointer via `inspect`
# (never a `readlink -f` guess). Prints the resolved previous target on
# stdout (empty when `current` is absent); non-zero on invalid evidence.
ega_release_previous_target() {
  local current="$1" releases_root="$2"
  ega_deploy_release inspect --current "$current" \
    --releases-root "$releases_root" --field previous_target
}

# Atomic switch/restore. $4 (optional) is the expected current target
# (compare-and-swap guard, closes the inspect->switch race).
ega_switch_release() {
  local current="$1" target="$2" releases_root="$3" previous="${4:-}"
  if [ -n "$previous" ]; then
    ega_deploy_release switch --current "$current" --target "$target" \
      --releases-root "$releases_root" --previous "$previous"
  else
    ega_deploy_release switch --current "$current" --target "$target" \
      --releases-root "$releases_root"
  fi
}

# True when `current` resolves exactly to `target` (post-switch recovery
# discriminator: "never replaced" vs "replaced then reported failure").
ega_verify_release() {
  local current="$1" target="$2"
  ega_deploy_release verify --current "$current" --target "$target" \
    >/dev/null 2>&1
}

# Existing compatibility contract (validator --check-compat OLD NEW):
# true ONLY when the prior release can run the DB migrated by NEW.
# Unknown/unreadable compatibility is FALSE (fail closed).
ega_compat_proven() {
  local new_rel="$1" prev_rel="$2" cfg="$3"
  [ -n "$prev_rel" ] || return 1
  [ -d "$prev_rel" ] || return 1
  [ -f "$new_rel/deploy/etc/validate-release.py" ] || return 1
  (
    cd "$new_rel" || exit 1
    EGA_CONFIG_FILE="$cfg" "$new_rel/venv/bin/python" \
      "$new_rel/deploy/etc/validate-release.py" --check-compat \
      "$prev_rel" "$new_rel"
  ) >/dev/null 2>&1
}

# Host-local kernel flock around the critical deployment execution,
# acquired BEFORE any maintenance mutation and released by the kernel on
# process exit (no stale-PID semantics, no SQLite lock). Fail fast when
# another operator/deployment owns it. Uses fd 9 in the calling shell.
ega_acquire_deploy_lock() {
  local lock_file="$1" label="$2" lock_dir=""
  lock_dir="$(dirname "$lock_file")"
  if [ ! -d "$lock_dir" ]; then
    echo "[$label] REFUSING: deployment lock directory missing: $lock_dir" >&2
    return 1
  fi
  if ! exec 9>"$lock_file"; then
    echo "[$label] REFUSING: cannot open deployment lock $lock_file" >&2
    return 1
  fi
  if ! flock -n 9; then
    echo "[$label] REFUSING: another deployment is active (deployment lock held: $lock_file)" >&2
    return 1
  fi
  echo "[$label] deployment lock acquired: $lock_file"
  return 0
}

# Advisory reachability code (not a gate).
ega_advisory_http_probe() {
  local port="$1"
  curl -s -o /dev/null -w '%{http_code}' \
    "http://127.0.0.1:$port/api/v1/health" 2>/dev/null || printf '000'
}

# ega_user_bus_env <owner> <repo_root>
# Canonical user-bus environment for shell `systemctl --user` calls:
# delegates to the product resolver (backend.app.owner_env bus-env ->
# systemd_user_bus). Prints one line:
#   "XDG_RUNTIME_DIR=<path> DBUS_SESSION_BUS_ADDRESS=unix:path=<path>/bus"
# Returns nonzero with empty output when the bus is unresolvable (callers
# warn; the owner transient readiness stage remains the fail-closed gate).
ega_user_bus_env() {
  local owner="$1" repo="$2" out=""
  out="$(PYTHONPATH="$repo" python3 -m backend.app.owner_env bus-env \
    --owner "$owner" 2>/dev/null)" || return 1
  [ -n "$out" ] || return 1
  printf '%s' "$out"
}

# ega_wait_for_readiness <release_dir> <config_file> <port> <report_path>
#                        <label> [iterations] [sleep_s]
# Bounded loop over the canonical readiness gate. Returns 0 only when the
# gate exits 0 (ALL mandatory stages proven). On failure the machine-
# readable report and the gate's stderr diagnosis are left at
# <report_path> / <report_path>.err for the caller's failure path.
ega_wait_for_readiness() {
  local release_dir="$1" config_file="$2" port="$3" report="$4"
  local label="$5"
  local iters="${6:-12}" nap="${7:-5}"
  local i=0 code=""
  while [ "$i" -lt "$iters" ]; do
    i=$((i + 1))
    code="$(ega_advisory_http_probe "$port")"
    if ( cd "$release_dir" && EGA_CONFIG_FILE="$config_file" \
         "$release_dir/venv/bin/python" -m backend.app.cli status --require-ready \
       ) >"$report" 2>"${report}.err"; then
      return 0
    fi
    echo "[$label] readiness pending (http=$code, $i/$iters) — failed stages:" >&2
    sed -n 's/^not ready: failed stages: //p' "${report}.err" >&2 2>/dev/null || true
    sleep "$nap"
  done
  return 1
}

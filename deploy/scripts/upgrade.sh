#!/usr/bin/env bash
# EGA Update Console — release upgrade (SPEC §12).
#
# Console deployment requires NO active update. Preserves database/log/
# backup directories. Rolls back CONSOLE CODE only when its DB schema is
# compatible; otherwise follows the migration-recovery procedure. Never
# automatically restores TOOL data (tool rollback is a deliberate manual
# action via backups, never implicit).
#
# Usage: sudo deploy/scripts/upgrade.sh --commit <sha> --release-tarball <path>
#
# Maintenance protocol (R33, W4-D8 ordering):
#  (1) write drain file; (2) cli status proves no active/unresolved
#  (bounded 120s, fail closed); (3) stop API+worker, PROVE stopped (fail
#  closed, abort, keep drain); (4) consistent DB backup; (5) stage+validate
#  (validator); (6) migrate via release venv; (7) atomic symlink switch;
#  (8) install units (+ port drop-in); (9) start; (10) bounded readiness:
#  the ONE shared pre-undrain acceptance gate
#  (deploy/scripts/lib/deploy_common.sh -> `cli status --require-ready`:
#  api_service_identity, api_security_boundary, worker_process,
#  probe_executor, owner_transient_execution); (11) release drain only on
#  success. No host/runtime mutation (including secrets.env provisioning)
#  happens before the drain + proven quiescence + proven stopped.
# Failure: preserve drain. W5-D9: the prior pointer is restored ONLY when
# the validator --check-compat OLD NEW contract PROVES the old code can
# run the migrated DB, and then ONLY through the shared atomic rename(2)
# primitive (backend/app/deploy_release.py). Messages distinguish POINTER
# RESTORE from DATABASE ROLLBACK from SERVICE RESTORATION; unknown/false
# compatibility, and any migration failure, leave the host in
# manual-recovery state (never "start same broken release" as rollback,
# never boot unproven old code against a newer DB). A host-local flock
# serializes deployments and `current` is never switched with `ln -sfn`.
#
# Python rule (R32): every python invocation runs with CWD at the release
# dir (cd "$RELEASE_DIR") under the release venv binary, with
# EGA_CONFIG_FILE exported for ALL invocations including quiescence. State
# paths are parsed from config via config_cli (backend/app/config_cli.py),
# never hardcoded and never inline python fragments. No
# heredoc-python. pip uses --require-hashes with NO fallback (R35).
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Trusted operator-checkout root (F06): bootstrap operations (config
# parse, quiescence, archive validation) run from here — never from the
# candidate release before it is validated, and never rely on an old
# installed release for new-protocol gates.
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
# Release checkout validator (N17): the tarball is NEVER trusted for its
# own validation code. Archive inspection runs from the operator's
# checkout copy before any root extraction.
VALIDATE_ARCHIVE="$SCRIPT_DIR/../etc/validate-archive.py"
# Version-independent quiescence controller (F07): proves quiescence
# from stable DB/systemd contracts without requiring any installed
# release to implement the newest protocol.
QUIESCE_CHECK="$SCRIPT_DIR/../etc/quiescence-check.py"
# Shared deployment helpers (W4-D8): ONE pre-undrain acceptance gate.
# shellcheck source=lib/deploy_common.sh
source "$SCRIPT_DIR/lib/deploy_common.sh"

COMMIT=""
TARBALL=""
PREFIX="/opt/ega-update"
ETC="/etc/ega-update"
STATE="/var/lib/ega-update"
API_USER="ega-update"
TOOL_OWNER="ubuntu"

while [ $# -gt 0 ]; do
  case "$1" in
    --commit) COMMIT="$2"; shift 2 ;;
    --release-tarball) TARBALL="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
if [ -n "$COMMIT" ] && [ -n "$TARBALL" ]; then
  :
else
  echo "usage: upgrade.sh --commit <sha> --release-tarball <path>" >&2
  exit 2
fi
# Commit must be a 40-hex release pin (R32).
case "$COMMIT" in
  *[^0-9a-f]*|"")
    echo "[upgrade] REFUSING: --commit must be a 40-hex sha (got: $COMMIT)" >&2
    exit 2
    ;;
esac
if [ "${#COMMIT}" -ne 40 ]; then
  echo "[upgrade] REFUSING: --commit must be a 40-hex sha (got length ${#COMMIT})" >&2
  exit 2
fi
if [ "$(id -u)" -eq 0 ]; then
  :
else
  echo "upgrade.sh must run as root (sudo)" >&2
  exit 2
fi
if [ -f "$TARBALL" ]; then
  :
else
  echo "[upgrade] release tarball missing: $TARBALL" >&2
  exit 2
fi

RELEASE_DIR="$PREFIX/releases/$COMMIT"
CURRENT_LINK="$PREFIX/current"
# W5-D9: PREV_RELEASE is captured via the shared atomic primitive's
# `inspect` (exact raw target, not a readlink -f guess) AFTER the
# deployment lock and trusted config parse, but BEFORE any maintenance
# mutation.
PREV_RELEASE=""
SWITCH_DONE=0
MIGRATED=0
MIGRATION_STARTED=0
LOCK_FILE="$PREFIX/deploy.lock"
if [ -e "$CURRENT_LINK" ] || [ -L "$CURRENT_LINK" ]; then
  :
else
  echo "[upgrade] REFUSING: $CURRENT_LINK is not a symlink (manual state)" >&2
  exit 1
fi

# EGA_CONFIG_FILE exported for ALL python invocations (R32), including
# quiescence/status/validator/migrate/readiness.
export EGA_CONFIG_FILE="$ETC/config.json"

# Config values come ONLY from the trusted checkout parser (F06/N16):
# PYTHONPATH=$REPO_ROOT (operator checkout), never the candidate
# release, never an old install. Existing config must parse; there are
# no silent fallbacks for an existing-but-broken config.
cfg_cli() {
  # Subshell-CWD rule (same as ega_deploy_release): `python3 -m` puts the
  # caller's CWD first on sys.path, so a stale checkout sharing the
  # `backend` package name must never shadow the trusted parser.
  (
    cd "$REPO_ROOT" || exit 1
    PYTHONPATH="$REPO_ROOT" EGA_CONFIG_FILE="$ETC/config.json" python3 -m backend.app.config_cli "$@"
  )
}

cfg_value() {
  local key="$1"
  local fallback="$2"
  local out=""
  if out="$(cfg_cli get --require "$key" 2>/dev/null)"; then
    printf '%s' "$out"
  elif [ -f "$ETC/config.json" ]; then
    # fail() is defined below; this path runs before it exists, so exit
    # directly (same fail-closed outcome, drain already or absent).
    echo "[upgrade] REFUSING: config parse failed for required key $key" >&2
    exit 1
  else
    printf '%s' "$fallback"
  fi
}

# W6: the assignments MUST propagate a cfg_value failure — cfg_value's
# `exit 1` for an existing-but-unparsable config runs in a
# command-substitution subshell, so without the explicit `|| exit 1` the
# upgrade would continue with EMPTY state/db/backup paths (no-fail-open
# contract, D8 read-only precheck).
EFFECTIVE_STATE="$(cfg_value state_dir "$STATE")" || exit 1
EFFECTIVE_DB="$(cfg_value db_path "$EFFECTIVE_STATE/state.db")" || exit 1
EFFECTIVE_BACKUPS="$(cfg_value backup_dir "$EFFECTIVE_STATE/backups")" || exit 1
EFFECTIVE_PORT="$(cfg_value listen_port "8771")" || exit 1
DRAIN="$EFFECTIVE_STATE/drain"
DRAIN_CREATED_BY_US=0
WAS_API=0
WAS_WORKER=0

fail() {
  # W5-D9 restore policy. NEVER claims a database rollback. The only
  # automatic action is an atomic POINTER RESTORE, and only when the
  # existing --check-compat contract PROVES the prior release can run the
  # DB migrated by $RELEASE_DIR. Usage: fail "message"
  echo "[upgrade] FAILED: $1" >&2
  if [ "$SWITCH_DONE" = "1" ]; then
    if ega_compat_proven "$RELEASE_DIR" "$PREV_RELEASE" "$ETC/config.json"; then
      echo "[upgrade] compatibility proven — attempting atomic POINTER RESTORE to $PREV_RELEASE (no DATABASE ROLLBACK)" >&2
      if ega_maybe_fail restore "pointer restoration" && \
         ega_switch_release "$CURRENT_LINK" "$PREV_RELEASE" "$PREFIX/releases" "$RELEASE_DIR"; then
        echo "[upgrade] POINTER RESTORE COMPLETE: $CURRENT_LINK -> $PREV_RELEASE (no DATABASE ROLLBACK; the DB migrated by $RELEASE_DIR is NOT restored)" >&2
        echo "[upgrade] NOTE (V1 boundary): installed systemd unit files are NOT reverted — daemon-reload + restart use the restored pointer's code; unit restoration is a manual decision (docs/RUNBOOK.md §7)" >&2
        systemctl daemon-reload || true
        if [ "$WAS_WORKER" = "1" ] || [ "$WAS_API" = "1" ]; then
          echo "[upgrade] SERVICE RESTORATION: restarting units active before maintenance" >&2
        fi
        # W11/B2: the failed attempt already started the candidate units,
        # so `start` is a no-op and would leave pointer=old with NEW
        # processes running. The restored pointer's code must actually be
        # loaded: restart the units that were active before maintenance.
        # (Pre-migration/migration-only failure paths keep `start` — there
        # the services were stopped by this script and hold no new code.)
        if [ "$WAS_WORKER" = "1" ]; then systemctl restart ega-update-worker || true; fi
        if [ "$WAS_API" = "1" ]; then systemctl restart ega-update-api || true; fi
      else
        echo "[upgrade] RESTORE FAILED: $CURRENT_LINK may still resolve to $RELEASE_DIR (POINTER NOT RESTORED) — MANUAL RECOVERY REQUIRED (docs/RUNBOOK.md §7)" >&2
        systemctl stop ega-update-worker ega-update-api || true
        echo "[upgrade] SERVICE STOP: api/worker stopped; drain kept" >&2
      fi
    else
      echo "[upgrade] compatibility NOT proven (schema drift/unknown) — pointer NOT restored (stays at $RELEASE_DIR); stopping services; MANUAL RECOVERY REQUIRED (docs/RUNBOOK.md §7)" >&2
      systemctl stop ega-update-worker ega-update-api || true
      echo "[upgrade] follow docs/RUNBOOK.md migration-recovery with $EFFECTIVE_BACKUPS/state-preupgrade-*.db" >&2
    fi
  elif [ "$MIGRATED" = "1" ]; then
    if ega_compat_proven "$RELEASE_DIR" "$PREV_RELEASE" "$ETC/config.json"; then
      echo "[upgrade] failure after migration, before pointer switch; compatibility proven — POINTER UNCHANGED ($PREV_RELEASE); DATABASE NOT ROLLED BACK; restarting previously-active services" >&2
      if [ "$WAS_WORKER" = "1" ]; then systemctl start ega-update-worker || true; fi
      if [ "$WAS_API" = "1" ]; then systemctl start ega-update-api || true; fi
    else
      echo "[upgrade] failure after migration; compatibility NOT proven — services NOT restarted; MANUAL RECOVERY REQUIRED (docs/RUNBOOK.md §7)" >&2
    fi
  elif [ "$MIGRATION_STARTED" = "1" ]; then
    echo "[upgrade] migration failed — database state unproven; services NOT restarted; MANUAL RECOVERY REQUIRED (docs/RUNBOOK.md §7)" >&2
  else
    # Failure before migration: prior release remains linked and the
    # DB/config were not mutated; restart what was running before.
    echo "[upgrade] failure before migration — prior release $PREV_RELEASE remains linked; previously-active services restarted" >&2
    if [ "$WAS_WORKER" = "1" ]; then systemctl start ega-update-worker || true; fi
    if [ "$WAS_API" = "1" ]; then systemctl start ega-update-api || true; fi
  fi
  echo "[upgrade] restore: drain KEPT at $DRAIN (blocking)." >&2
  echo "[upgrade] inspect, reconcile (docs/RUNBOOK.md §5-§6), then sudo rm -f $DRAIN only when healthy." >&2
  exit 1
}

# ===== DEPLOYMENT LOCK (W5-D9) =====
# One root-controlled kernel flock serializes the critical deployment
# execution BEFORE any maintenance mutation or pointer inspection (two
# operators, install vs upgrade overlap, duplicate invocation). Fail
# fast; the kernel releases the lock on process exit (no stale-PID file
# semantics, no SQLite lock).
if [ -d "$PREFIX" ]; then :; else
  echo "[upgrade] REFUSING: prefix missing for deployment lock: $PREFIX" >&2
  exit 1
fi
ega_acquire_deploy_lock "$LOCK_FILE" upgrade || exit 1

# Capture the exact PREVIOUS release pointer (W5-D9) via the shared
# atomic primitive — never `readlink -f`. Invalid pointer evidence fails
# closed BEFORE the drain and any other mutation.
if PREV_RELEASE="$(ega_release_previous_target "$CURRENT_LINK" "$PREFIX/releases")"; then
  :
else
  echo "[upgrade] REFUSING: release pointer evidence invalid for $CURRENT_LINK (fail closed before maintenance)" >&2
  exit 1
fi
if [ -z "$PREV_RELEASE" ]; then
  echo "[upgrade] REFUSING: $CURRENT_LINK resolves to no previous release" >&2
  exit 1
fi
if [ -d "$PREV_RELEASE" ]; then
  :
else
  echo "[upgrade] REFUSING: previous release dir missing: $PREV_RELEASE" >&2
  exit 1
fi

# READ-ONLY PRECHECK (W6-D8 ordering): the target release dir must not
# exist yet. Evaluated BEFORE the maintenance boundary so a repeated
# invocation (same commit) fails fast without draining admission, stopping
# services, or taking a backup. Never overwrite a staged/installed release;
# a partial dir must be removed deliberately before a retry.
if [ -e "$RELEASE_DIR" ]; then
  echo "[upgrade] REFUSING: release dir already exists: $RELEASE_DIR (use a new commit or remove the partial dir)" >&2
  exit 1
fi

echo "[upgrade] $PREV_RELEASE -> $RELEASE_DIR"
echo "[upgrade] state_dir=$EFFECTIVE_STATE db=$EFFECTIVE_DB port=$EFFECTIVE_PORT"

# (1) Admission stop FIRST: create drain before any other step so the API
#    refuses new plans/jobs while present (no drain removal on failure path).
mkdir -p "$EFFECTIVE_STATE"
if [ -f "$DRAIN" ]; then
  echo "[upgrade] drain already present at $DRAIN (admission already stopped)"
else
  touch "$DRAIN" || { echo "cannot create drain $DRAIN" >&2; exit 1; }
  DRAIN_CREATED_BY_US=1
  echo "[upgrade] drain created at $DRAIN (new plans/jobs refused)"
fi

# (2) Quiescence via the version-independent deploy controller (F07):
# `quiescence-check.py` exits 0 ONLY when proven quiescent (worker alive,
# no active/unresolved/recovery work, no live/unknown runner units, no
# active delegated operations, drain present). No installed release —
# old or candidate — participates. Bounded 120s, fail closed.
echo "[upgrade] waiting for quiescence via deploy controller (bounded 120s)..."
QUIESCED=0
for _i in $(seq 1 24); do
  if python3 "$QUIESCE_CHECK" --config "$ETC/config.json" >/tmp/ega-upgrade-status.json 2>/tmp/ega-upgrade-status.err; then
    QUIESCED=1
    break
  fi
  echo "[upgrade] not quiescent yet, waiting 5s ($_i/24) — see /tmp/ega-upgrade-status.json reasons..."
  sleep 5
done
if [ "$QUIESCED" = "1" ]; then
  echo "[upgrade] quiesced: cli status proves no active job, no unresolved runners"
else
  fail "quiescence timeout: cli status still shows active/unresolved after 120s (fail closed, drain kept)"
fi

# (3) Stop API+worker, then PROVE stopped (fail closed, abort, keep drain).
if systemctl is-active --quiet ega-update-api 2>/dev/null; then WAS_API=1; fi
if systemctl is-active --quiet ega-update-worker 2>/dev/null; then WAS_WORKER=1; fi
systemctl stop ega-update-worker ega-update-api || true
if systemctl is-active --quiet ega-update-api 2>/dev/null; then
  fail "api failed to stop (fail closed, drain kept)"
fi
if systemctl is-active --quiet ega-update-worker 2>/dev/null; then
  fail "worker failed to stop (fail closed, drain kept)"
fi
echo "[upgrade] stopped: api and worker proven inactive"

# (3b) Known-secret redaction source (S01, idempotent): older installs may
# predate secrets.env provisioning. Created EMPTY when absent so the
# structural secret-source contract holds after upgrade; never touch an
# existing owner-managed file, never write values, never print contents.
# 0640 root:ega-update: readable by the ubuntu worker and the ega-update
# API via group (see install.sh; 0600 root-owned would fail readiness).
# W4-D8: this host/runtime mutation runs AFTER the maintenance boundary
# (drain + proven quiescence + proven stopped), never before it.
if [ -e "$ETC/secrets.env" ]; then
  :
else
  : > "$ETC/secrets.env"
  chmod 0640 "$ETC/secrets.env"
  chown root:ega-update "$ETC/secrets.env"
  echo "[upgrade] created empty known-secret source $ETC/secrets.env"
fi

# (3c) Explicit effective owner-execution access (W11/B1). The running
# user@<uid>.service keeps the supplementary-group vector it had at start,
# so transient `systemd-run --user` probe/runner units inherit a vector
# WITHOUT the shared group and cannot traverse the group-owned shared
# paths. install.sh has always provisioned the named-user POSIX ACLs for
# the tool owner; upgrades of hosts that predate that model must provision
# them too — the SAME canonical idempotent mechanism (ACL only, never a
# wider chmod). Runs after drain + proven quiescence + proven stopped and
# before any service/readiness dependency on those permissions.
SHARED_GROUP="$(
  cd "$REPO_ROOT" || exit 1
  PYTHONPATH="$REPO_ROOT" EGA_CONFIG_FILE="$ETC/config.json" \
    python3 -m backend.app.config_cli get shared_group 2>/dev/null || true
)"
SHARED_GROUP="${SHARED_GROUP:-${EGA_SHARED_GROUP:-ega-update}}"
echo "[upgrade] provisioning explicit owner-execution access (owner=$TOOL_OWNER group=$SHARED_GROUP)"
# Subshell-CWD rule (same as ega_deploy_release): `python3 -m` puts the
# caller's CWD first on sys.path, so a stale checkout sharing the
# `backend` package name would silently shadow the provisioning CLI
# (observed on the real VM: an old CLI-less module exited 0 doing
# nothing). Pinning CWD to the trusted checkout is REQUIRED here.
if (
    cd "$REPO_ROOT" || exit 1
    PYTHONPATH="$REPO_ROOT" python3 -m backend.app.owner_env provision \
      --owner "$TOOL_OWNER" --group "$SHARED_GROUP" \
      --state-dir "$EFFECTIVE_STATE" --log-dir "$EFFECTIVE_STATE/logs" \
      --backup-dir "$EFFECTIVE_BACKUPS" --config-dir "$ETC" \
      --config-file "$ETC/config.json" --secrets-file "$ETC/secrets.env" \
      --inventory-file "$ETC/inventory.json" \
      >/tmp/ega-upgrade-owner-access.json 2>/tmp/ega-upgrade-owner-access.err
  ); then
  echo "[upgrade] owner-execution access provisioned (ACLs; no permission widening)"
else
  cat /tmp/ega-upgrade-owner-access.json >&2 2>/dev/null || true
  cat /tmp/ega-upgrade-owner-access.err >&2 2>/dev/null || true
  fail "owner-execution effective access could not be provisioned (drain kept)"
fi

# (4) Consistent DB backup (SQLite backup API via the release db tool —
# never bare cp of a live DB, never inline python). Writers stopped above.
TS="$(date -u +%Y%m%dT%H%M%SZ)"
PREV_VENV="$CURRENT_LINK/venv/bin/python"
cd "$PREV_RELEASE" 2>/dev/null || cd /tmp
ega_maybe_fail backup "pre-upgrade DB backup" || fail "pre-upgrade DB backup failed (fault injected)"
EGA_CONFIG_FILE="$ETC/config.json" "$PREV_VENV" -m backend.app.db backup "$EFFECTIVE_DB" "$EFFECTIVE_BACKUPS/state-preupgrade-$TS.db" \
  || fail "pre-upgrade DB backup failed"
chmod 0600 "$EFFECTIVE_BACKUPS/state-preupgrade-$TS.db"
chown "$API_USER":"$API_USER" "$EFFECTIVE_BACKUPS/state-preupgrade-$TS.db" || true
chmod 0600 "$EFFECTIVE_BACKUPS/state-preupgrade-$TS.db"
echo "[upgrade] backup: $EFFECTIVE_BACKUPS/state-preupgrade-$TS.db"

# (5) Stage the new release dir (immutable, root-owned), write MANIFEST via
# sha256sum, install venv with --require-hashes (NO fallback), then validate.
# The release-dir precheck above already refused an existing dir before the
# maintenance boundary.
mkdir -p "$RELEASE_DIR" || fail "cannot create $RELEASE_DIR"
# F15: immutable staging — copy the operator tarball into a
# root-controlled staging dir, digest it, validate the STAGED copy,
# re-verify the digest, then extract exactly that file (no TOCTOU).
STAGE_DIR="$(mktemp -d /var/tmp/ega-stage-XXXXXXXX)" || fail "cannot create staging dir"
chmod 0700 "$STAGE_DIR"
STAGED_ARCHIVE="$STAGE_DIR/release.tar.gz"
ega_maybe_fail stage "candidate staging" || { rm -rf "$STAGE_DIR"; fail "candidate staging failed"; }
cp -p "$TARBALL" "$STAGED_ARCHIVE" || fail "cannot stage tarball"
CANDIDATE_SHA256="$(sha256sum "$STAGED_ARCHIVE" | awk '{print $1}')"
[ -n "$CANDIDATE_SHA256" ] || fail "cannot digest staged archive"
echo "[upgrade] candidate archive sha256: $CANDIDATE_SHA256"
# N17: validate EVERY archive member BEFORE any root extraction, using
# the checkout's validator — never code from the unvalidated tarball.
if python3 "$VALIDATE_ARCHIVE" --archive "$STAGED_ARCHIVE" --dest "$RELEASE_DIR"; then
  echo "[upgrade] archive validation ok"
else
  rm -rf "$STAGE_DIR"
  fail "archive validation blocked"
fi
if [ "$(sha256sum "$STAGED_ARCHIVE" | awk '{print $1}')" != "$CANDIDATE_SHA256" ]; then
  rm -rf "$STAGE_DIR"
  fail "staged archive changed after validation"
fi
tar -xzf "$STAGED_ARCHIVE" -C "$RELEASE_DIR" || { rm -rf "$STAGE_DIR"; fail "tarball extraction failed"; }
printf '%s  %s\n' "$CANDIDATE_SHA256" "$COMMIT" > "$RELEASE_DIR/CANDIDATE_SHA256"
chmod 0644 "$RELEASE_DIR/CANDIDATE_SHA256"
rm -rf "$STAGE_DIR"
chown -R root:root "$RELEASE_DIR"
# Build output mapping: frontend/vite.config.ts outDir is
# ../backend/app/static and backend/app/main.py serves backend/app/static,
# so the gate checks backend/app/static/index.html (not frontend/dist/).
if [ -f "$RELEASE_DIR/backend/app/static/index.html" ]; then
  :
else
  fail "tarball missing built frontend (backend/app/static/index.html)"
fi
# Dedicated app venv (baseline Python >=3.10,<3.14; shared runtimes untouched).
command -v python3 >/dev/null 2>&1 || fail "python3 (>=3.10,<3.14) required"
python3 -c 'import sys; raise SystemExit(0 if (3, 10) <= sys.version_info < (3, 14) else 1)' \
  || fail "python3 >=3.10,<3.14 required (got: $(python3 --version 2>&1))"
ega_maybe_fail venv "release venv/build preparation" || fail "venv creation failed"
python3 -m venv "$RELEASE_DIR/venv" || fail "venv creation failed"
cd "$RELEASE_DIR"
EGA_CONFIG_FILE="$ETC/config.json" "$RELEASE_DIR/venv/bin/pip" install --require-hashes -r "$RELEASE_DIR/backend/requirements.txt" \
  || fail "requirements install failed (pip --require-hashes REQUIRED; missing hashes file is blocked — generate hashes during authorized release prep)"
# Stage-time MANIFEST (validator verifies it; validator never writes it).
cd "$RELEASE_DIR"
find backend -type f -exec sha256sum {} + | sort > "$RELEASE_DIR/MANIFEST" \
  || fail "manifest staging failed"
echo "[upgrade] staged MANIFEST with $(wc -l < "$RELEASE_DIR/MANIFEST") backend files"
# Validate BEFORE migrate/switch (validator runs under the new release venv,
# CWD at the release root, EGA_CONFIG_FILE exported).
cd "$RELEASE_DIR"
ega_maybe_fail validate "release validation" || fail "stage validation blocked (fault injected)"
if EGA_CONFIG_FILE="$ETC/config.json" "$RELEASE_DIR/venv/bin/python" "$RELEASE_DIR/deploy/etc/validate-release.py" --release "$RELEASE_DIR" --config "$ETC/config.json"; then
  echo "[upgrade] stage validation ok"
else
  fail "stage validation blocked (drain kept; prior release still linked)"
fi

# (6) Migrate via the release module entrypoint (N04: only the controlled
# deploy procedure migrates; services validate only). CWD at release root.
# MIGRATION_STARTED is set BEFORE the attempt: a failure never restarts
# the old code against a possibly-migrated DB (fail closed, manual
# recovery); MIGRATED is set only after success.
cd "$RELEASE_DIR"
MIGRATION_STARTED=1
ega_maybe_fail migrate "migration" || fail "migration failed (fault injected; see migration-recovery in RUNBOOK; drain kept)"
if EGA_CONFIG_FILE="$ETC/config.json" "$RELEASE_DIR/venv/bin/python" -m backend.app.db migrate; then
  echo "[upgrade] migrate ok"
  MIGRATED=1
else
  fail "migration failed (see migration-recovery in RUNBOOK; drain kept)"
fi

# (7) Atomic release pointer switch (W5-D9) — only after
# stage+validate+migrate. ONE shared rename(2)-based primitive
# (backend/app/deploy_release.py via deploy_common.sh): a temporary
# symlink in the same parent directory is committed with os.replace, so
# no reader ever observes a missing `current` and a failed preparation
# leaves the prior pointer untouched. The exact previous target captured
# by `inspect` above is passed as a compare-and-swap guard.
ega_maybe_fail pre-switch "before pointer switch" || fail "release switch aborted before the pointer change (fault injected)"
if ega_switch_release "$CURRENT_LINK" "$RELEASE_DIR" "$PREFIX/releases" "$PREV_RELEASE"; then
  SWITCH_DONE=1
else
  # Distinguish "never replaced" from "replaced then reported failure"
  # (post-replace verification mismatch): the independent verify call
  # decides the recovery policy, always erring toward post-switch.
  if ega_verify_release "$CURRENT_LINK" "$RELEASE_DIR"; then
    SWITCH_DONE=1
  fi
  fail "atomic release switch failed for $RELEASE_DIR (see diagnosis above)"
fi
ega_maybe_fail post-switch "after pointer switch" || fail "post-switch failure (fault injected)"

# (8) Install units (+ R35 port drop-in rendered from config via
# config_cli — an unparsable port blocks instead of a wrong default).
# W5-D9 boundary: systemd unit files are release-versioned artifacts;
# they are overwritten here and are NOT reverted by the automatic
# pointer restore (see fail(): documented V1 recovery boundary).
ega_maybe_fail units "unit installation/reload" || fail "unit installation failed (fault injected)"
EFFECTIVE_PORT_NOW="$(cfg_cli get --require listen_port 2>/dev/null)" || fail "listen_port unparsable in $ETC/config.json"
mkdir -p /etc/systemd/system/ega-update-api.service.d
printf '[Service]\nEnvironment=EGA_LISTEN_PORT=%s\n' "$EFFECTIVE_PORT_NOW" > /etc/systemd/system/ega-update-api.service.d/10-port.conf || fail "port drop-in write failed"
chmod 0644 /etc/systemd/system/ega-update-api.service.d/10-port.conf
echo "[upgrade] rendered port drop-in 10-port.conf with EGA_LISTEN_PORT=$EFFECTIVE_PORT_NOW"
cp "$CURRENT_LINK/systemd/"*.service /etc/systemd/system/ || fail "unit copy failed"
if [ -f "$CURRENT_LINK/systemd/user/ega-update-runner@.service" ]; then
  mkdir -p "/home/$TOOL_OWNER/.config/systemd/user"
  cp "$CURRENT_LINK/systemd/user/ega-update-runner@.service" "/home/$TOOL_OWNER/.config/systemd/user/" || fail "user unit copy failed"
  chown -R "$TOOL_OWNER:$TOOL_OWNER" "/home/$TOOL_OWNER/.config/systemd/user" || true
  # W11/B3: `systemctl --user` needs the resolved user-bus environment;
  # a bare `su` has none ("Failed to connect to bus"), leaving the copied
  # user runner unit unreloaded. Resolution is canonical (owner_env
  # bus-env); a WARN is non-fatal because the owner_transient_execution
  # readiness stage remains the fail-closed gate.
  USER_BUS_ENV="$(ega_user_bus_env "$TOOL_OWNER" "$REPO_ROOT" || true)"
  if [ -n "$USER_BUS_ENV" ] \
     && su -s /bin/bash "$TOOL_OWNER" -c "env $USER_BUS_ENV systemctl --user daemon-reload"; then
    echo "[upgrade] user manager reloaded for $TOOL_OWNER (user units)"
  else
    echo "[upgrade] WARN: user-manager daemon-reload failed; user runner unit may be stale" >&2
  fi
fi
systemctl daemon-reload || fail "daemon-reload failed"

# (9) Start console services (DB/logs/backups preserved — outside releases).
# Distinct API/worker start points give a precise failure diagnosis
# (matching install.sh) and the same atomic pointer policy on failure.
ega_maybe_fail api-start "api start" || fail "api start failed (fault injected)"
systemctl restart ega-update-api || fail "api restart failed"
ega_maybe_fail worker-start "worker start" || fail "worker start failed (fault injected)"
systemctl restart ega-update-worker || fail "worker restart failed"
systemctl restart cloudflared-ega-update || fail "service restart failed"

# (10) Bounded readiness: ONE canonical acceptance gate shared with
# install.sh (deploy/scripts/lib/deploy_common.sh) proves ALL mandatory
# stages through `cli status --require-ready` (api_service_identity,
# api_security_boundary, worker_process, probe_executor,
# owner_transient_execution). The shell consumes only the gate exit code;
# failed stages are named in the machine-readable report and stderr.
echo "[upgrade] waiting for readiness (bounded 60s)..."
if ega_wait_for_readiness "$RELEASE_DIR" "$ETC/config.json" \
    "$EFFECTIVE_PORT_NOW" /tmp/ega-upgrade-ready.json upgrade; then
  :
else
  fail "readiness failed after 60s (mandatory stages unproven; drain kept)"
fi
if systemctl is-active --quiet ega-update-api; then
  :
else
  fail "api failed to stay active — see rollback below"
fi
if systemctl is-active --quiet ega-update-worker; then
  :
else
  fail "worker failed to stay active — see rollback below"
fi
echo "[upgrade] readiness ok (all mandatory stages proven)"

# (11) Success path ONLY: remove drain to re-admit plans/jobs — but only
# when this run created it. A pre-existing drain (manual maintenance) is
# left for its owner to clear.
if [ "$DRAIN_CREATED_BY_US" = "1" ]; then
  rm -f "$DRAIN"
  echo "[upgrade] drain removed (admitting); done: $COMMIT"
else
  echo "[upgrade] done: $COMMIT (pre-existing drain kept at $DRAIN)"
fi
echo ""
echo "ROLLBACK (console code POINTER only — never a database rollback, never"
echo "          auto-restores tool data):"
echo "  If the validator --check-compat passes for the prior release:"
echo "    sudo env PYTHONPATH=$REPO_ROOT python3 -m backend.app.deploy_release switch \\"
echo "      --current $CURRENT_LINK --target $PREV_RELEASE --releases-root $PREFIX/releases"
echo "    sudo systemctl daemon-reload && sudo systemctl restart ega-update-api ega-update-worker"
echo "  (the atomic primitive is the ONLY supported pointer switch: never replace"
echo "   current with a manual unlink+create, which leaves a missing-current window)"
echo "  Else (schema changed / migrate failed): follow the migration-recovery"
echo "  procedure in docs/RUNBOOK.md — restore $EFFECTIVE_BACKUPS/state-preupgrade-$TS.db"
echo "  via the documented sqlite3 recovery steps, then re-point current."
echo "  Verify compat first (trusted checkout validator, candidate release code):"
echo "    sudo PYTHONPATH=$REPO_ROOT python3 $REPO_ROOT/deploy/etc/validate-release.py --check-compat $PREV_RELEASE $RELEASE_DIR"
echo "  Tool data is NEVER restored automatically; use the per-job backup in"
echo "  $EFFECTIVE_BACKUPS/<job-id>/ only via an explicit manual procedure."

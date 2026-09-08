#!/usr/bin/env bash
# EGA Update Console — pinned release install (SPEC §12).
#
# Installs ONE pinned console release WITHOUT touching managed tool state:
# creates the ega-update account, lays out /opt/ega-update/releases/<commit>/,
# links `current`, provisions /etc/ega-update + /var/lib/ega-update, builds a
# dedicated Python 3.10 venv, migrates SQLite after a consistent backup,
# installs/enables console systemd units, and validates loopback binding +
# Access JWT configuration IN CODE (checks below, run by this script — the
# script itself is not executed during implementation).
#
# Usage: sudo deploy/scripts/install.sh --commit <sha> --release-tarball <path>
#        [--config deploy/etc/config.example.json]
#
# Safety: never modifies tool installations, homes, alternate binaries,
# service units of managed tools, or tool data dirs. Tool-affecting steps
# require an explicit console job, never the installer.
#
# Maintenance protocol (R33): on an EXISTING deploy (state.db present) this
# script follows the same 11-step protocol as upgrade.sh — (1) drain,
# (2) cli-status quiescence, (3) stop + prove stopped, (4) consistent backup,
# (5) stage + validate, (6) migrate, (7) atomic switch, (8) units,
# (9) start, (10) readiness, (11) undrain only on success. Failures keep the
# drain and restore the PRIOR symlink only when the validator compat check
# passes; otherwise the host is left in manual-recovery state (never
# "start same broken release" as rollback).
#
# Python rule (R32): every python invocation below runs with CWD set to the
# release dir (cd "$RELEASE_DIR") under the release venv binary
# ("$RELEASE_DIR/venv/bin/python"), with EGA_CONFIG_FILE exported for ALL
# invocations including quiescence. State paths (state/db/log/backup/drain)
# are parsed from config via config_cli (backend/app/config_cli.py), never
# hardcoded and never inline python fragments. No
# heredoc-python blocks. pip uses --require-hashes with NO fallback (R35):
# a hashless requirements file blocks with a message instead of installing
# unverified code.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Trusted operator-checkout root (F06): bootstrap operations (config
# parse, quiescence, archive validation) run from here — never from the
# candidate release before it is validated, and never from an old
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

COMMIT=""
TARBALL=""
CONFIG_SRC="deploy/etc/config.example.json"
PREFIX="/opt/ega-update"
ETC="/etc/ega-update"
STATE="/var/lib/ega-update"
API_USER="ega-update"
TOOL_OWNER="ubuntu"

while [ $# -gt 0 ]; do
  case "$1" in
    --commit) COMMIT="$2"; shift 2 ;;
    --release-tarball) TARBALL="$2"; shift 2 ;;
    --config) CONFIG_SRC="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [ -z "$COMMIT" ] || [ -z "$TARBALL" ]; then
  echo "usage: install.sh --commit <sha> --release-tarball <path> [--config <path>]" >&2
  exit 2
fi
# Commit must be a 40-hex release pin (R32); refuse short/branch names.
case "$COMMIT" in
  *[^0-9a-f]*|"")
    echo "[install] REFUSING: --commit must be a 40-hex sha (got: $COMMIT)" >&2
    exit 2
    ;;
esac
if [ "${#COMMIT}" -ne 40 ]; then
  echo "[install] REFUSING: --commit must be a 40-hex sha (got length ${#COMMIT})" >&2
  exit 2
fi
if [ "$(id -u)" -ne 0 ]; then
  echo "install.sh must run as root (sudo)" >&2
  exit 2
fi
if [ -f "$TARBALL" ]; then
  :
else
  echo "[install] release tarball missing: $TARBALL" >&2
  exit 2
fi

RELEASE_DIR="$PREFIX/releases/$COMMIT"
CURRENT_LINK="$PREFIX/current"
PREV_RELEASE="$(readlink -f "$CURRENT_LINK" 2>/dev/null || echo '')"
DRAIN_CREATED_BY_US=0
WAS_API=0
WAS_WORKER=0
# F08: existing-deployment detection happens AFTER trusted config parse
# (see below where EFFECTIVE_DB resolves); never from a hardcoded path
# before configuration is known.
EXISTING_DEPLOY=0

# Config value helper (N16/F06): exactly one parser —
# `python -m backend.app.config_cli` from the TRUSTED CHECKOUT
# (PYTHONPATH=$REPO_ROOT; stdlib-only so it runs before any venv or
# release exists). Never the candidate release, never an old install.
# Fresh installs (no config file yet) fall back to documented defaults;
# an EXISTING but unparsable config is a hard failure, never silent.
cfg_value() {
  local key="$1"
  local fallback="$2"
  local out=""
  if out="$(PYTHONPATH="$REPO_ROOT" EGA_CONFIG_FILE="$ETC/config.json" python3 -m backend.app.config_cli get --require "$key" 2>/dev/null)"; then
    printf '%s' "$out"
  elif [ -f "$ETC/config.json" ]; then
    echo "[install] REFUSING: config parse failed for required key $key" >&2
    fail_keep_drain "config parse failed for $key"
  else
    printf '%s' "$fallback"
  fi
}

fail_keep_drain() {
  echo "[install] FAILED: $1" >&2
  echo "[install] drain KEPT (blocking admission) for manual review." >&2
  echo "[install] inspect, reconcile (docs/RUNBOOK.md), then sudo rm -f <state_dir>/drain only when healthy." >&2
  exit 1
}

echo "[install] pinned release: $COMMIT (existing deploy resolved after config parse below)"

# 1. Dedicated non-root API account (no login shell, no tool ownership).
if id "$API_USER" >/dev/null 2>&1; then
  :
else
  useradd --system --no-create-home --shell /usr/sbin/nologin "$API_USER"
  echo "[install] created user $API_USER"
fi
id "$TOOL_OWNER" >/dev/null 2>&1 || { echo "tool-owner account $TOOL_OWNER missing" >&2; exit 1; }

# 2. Release layout: immutable release dir + `current` symlink (staged but
#    NOT switched until stage+validate+migrate succeed — see step 7).
mkdir -p "$PREFIX/releases"
if [ -e "$RELEASE_DIR" ]; then
  echo "[install] release dir already exists: $RELEASE_DIR (refusing to overwrite)" >&2
  exit 1
fi
mkdir -p "$RELEASE_DIR"
# F15: immutable staging — the operator-supplied tarball path may live in
# a writable location, so copy it into a root-controlled staging dir,
# digest it, validate the STAGED copy, re-verify the digest, then extract
# exactly that staged file. Validation and extraction never independently
# reopen a mutable path (no TOCTOU window).
STAGE_DIR="$(mktemp -d /var/tmp/ega-stage-XXXXXXXX)" || { echo "[install] cannot create staging dir" >&2; exit 1; }
chmod 0700 "$STAGE_DIR"
STAGED_ARCHIVE="$STAGE_DIR/release.tar.gz"
cleanup_stage() { rm -rf "$STAGE_DIR"; }
cp -p "$TARBALL" "$STAGED_ARCHIVE" || { echo "[install] cannot stage tarball" >&2; cleanup_stage; exit 1; }
CANDIDATE_SHA256="$(sha256sum "$STAGED_ARCHIVE" | awk '{print $1}')"
if [ -z "$CANDIDATE_SHA256" ]; then echo "[install] cannot digest staged archive" >&2; cleanup_stage; exit 1; fi
echo "[install] candidate archive sha256: $CANDIDATE_SHA256"
# N17: validate EVERY archive member (traversal/links/devices/modes +
# expected top-levels) BEFORE any root extraction, using the checkout's
# validator — never code from the unvalidated tarball.
if python3 "$VALIDATE_ARCHIVE" --archive "$STAGED_ARCHIVE" --dest "$RELEASE_DIR"; then
  echo "[install] archive validation ok"
else
  echo "[install] REFUSING: archive validation blocked" >&2
  cleanup_stage
  exit 1
fi
if [ "$(sha256sum "$STAGED_ARCHIVE" | awk '{print $1}')" != "$CANDIDATE_SHA256" ]; then
  echo "[install] REFUSING: staged archive changed after validation" >&2
  cleanup_stage
  exit 1
fi
tar -xzf "$STAGED_ARCHIVE" -C "$RELEASE_DIR"
printf '%s  %s\n' "$CANDIDATE_SHA256" "$COMMIT" > "$RELEASE_DIR/CANDIDATE_SHA256"
chmod 0644 "$RELEASE_DIR/CANDIDATE_SHA256"
cleanup_stage
chown -R root:root "$RELEASE_DIR"
chmod -R a-w "$RELEASE_DIR" || true

# Frontend ships already built in the release tarball (no npm build here).
# Build output mapping: frontend/vite.config.ts outDir is
# ../backend/app/static and backend/app/main.py serves backend/app/static,
# so the gate checks backend/app/static/index.html (not frontend/dist/).
if [ -f "$RELEASE_DIR/backend/app/static/index.html" ]; then
  :
else
  echo "[install] release tarball missing built frontend (backend/app/static/index.html)" >&2
  exit 1
fi

# 3. Config + secrets dirs. Secrets 0600, shared config 0640.
# Traversal: /etc/ega-update is root:ega-update 0750 so both service accounts
# traverse via group; files stay group-readable (0640) and secrets 0600.
# The cloudflared subdir is root:ega-update 0750 (traversable by the
# ega-update user via group); never 0700 root-only when the tunnel runs as
# ega-update.
getent group ega-update >/dev/null 2>&1 || groupadd -r ega-update
usermod -aG ega-update "$API_USER" || true
usermod -aG ega-update "$TOOL_OWNER" || true
mkdir -p "$ETC" "$ETC/cloudflared"
chown root:ega-update "$ETC"
chmod 0750 "$ETC"
chown root:ega-update "$ETC/cloudflared"
chmod 0750 "$ETC/cloudflared"
if [ -f "$ETC/config.json" ]; then
  :
else
  cp "$CONFIG_SRC" "$ETC/config.json"
  chmod 0640 "$ETC/config.json"
  chown root:"$API_USER" "$ETC/config.json"
  echo "[install] wrote $ETC/config.json from $CONFIG_SRC — EDIT before starting services"
fi
touch "$ETC/api.env" "$ETC/worker.env"
chmod 0640 "$ETC/api.env" "$ETC/worker.env"
chown root:ega-update "$ETC/api.env" "$ETC/worker.env"
# Secret files (created by owner, never by installer with real values):
for f in "$ETC/csrf.secret" "$ETC/tunnel.env"; do
  [ -e "$f" ] || { touch "$f"; chmod 0600 "$f"; chown root:"$API_USER" "$f"; }
done
# Known-secret redaction source (S01): provisioned EMPTY when absent so a
# fresh install satisfies the structural secret-source contract without
# undocumented manual file creation. Empty means "no additional known
# secrets" (valid); a configured source that later becomes missing,
# unreadable, or malformed still raises SecretSourceError (G05/H06
# unchanged). Never overwritten, never filled with values, never
# printed. 0640 root:ega-update: the ubuntu worker AND the ega-update
# API both read it via group (both are members; $ETC is 0750 group
# traversable). Never world-readable, never owner-only (services are
# not root — 0600 root-owned would fail readiness for both readers).
if [ -e "$ETC/secrets.env" ]; then
  :
else
  : > "$ETC/secrets.env"
  chmod 0640 "$ETC/secrets.env"
  chown root:ega-update "$ETC/secrets.env"
  echo "[install] created empty known-secret source $ETC/secrets.env"
fi
# Cloudflared tunnel config: generate ONLY with an explicit placeholder
# hostname (fail-closed). The tunnel service validates non-placeholder
# before routing; see docs/RUNBOOK.md. Never invent a real hostname here.
if [ -f "$ETC/cloudflared/config.yml" ]; then
  :
else
  cat > "$ETC/cloudflared/config.yml" <<'YML'
# EGA Update Console — tunnel ingress (placeholder only; owner MUST replace).
# Fail-closed: cloudflared-ega-update.service refuses to route while the
# hostname below is still a placeholder (see RUNBOOK §1).
tunnel: CHANGEME-tunnel-id
credentials-file: /etc/ega-update/cloudflared/credentials.json
ingress:
  - hostname: CHANGEME-update-console.example.invalid
    service: http://127.0.0.1:8771
    originRequest:
      noTLSVerify: false
  - service: http_status:404
YML
  chmod 0640 "$ETC/cloudflared/config.yml"
  chown root:ega-update "$ETC/cloudflared/config.yml"
  echo "[install] wrote placeholder $ETC/cloudflared/config.yml — REPLACE hostname/tunnel before enabling routing"
fi

# EGA_CONFIG_FILE is exported for ALL python invocations below (R32),
# including quiescence/status/validator/migrate/readiness.
export EGA_CONFIG_FILE="$ETC/config.json"

# Resolve state paths from config (N16/R32: never hardcoded for backup/drain).
EFFECTIVE_STATE="$(cfg_value state_dir "$STATE")"
EFFECTIVE_DB="$(cfg_value db_path "$EFFECTIVE_STATE/state.db")"
EFFECTIVE_BACKUPS="$(cfg_value backup_dir "$EFFECTIVE_STATE/backups")"
DRAIN="$EFFECTIVE_STATE/drain"
echo "[install] state_dir=$EFFECTIVE_STATE db=$EFFECTIVE_DB backups=$EFFECTIVE_BACKUPS"
# F08: existing deployment = the CONFIGURED DB path exists (determined
# only now, after trusted config parse — never a hardcoded path before
# configuration is known).
if [ -f "$EFFECTIVE_DB" ]; then
  EXISTING_DEPLOY=1
fi
echo "[install] existing_deploy=$EXISTING_DEPLOY (from configured db path)"

# Drain-file hooks: <state_dir>/drain blocks new plans/jobs (API refuses
# while present). No drain by default on fresh installs; existing deploys
# drain FIRST per the maintenance protocol below.
echo "[install] drain hooks: fresh default absent ($DRAIN absent = admitting)"

# 4. Persistent state dirs OUTSIDE releases, shared by the two service
#    accounts only. Both accounts share joint group ega-update with group
#    read/write (0770 dirs, 0660 DB files). Never widen beyond the two
#    accounts (no o+rw, no 0777).
getent group ega-update >/dev/null 2>&1 || groupadd -r ega-update
usermod -aG ega-update "$API_USER" || true
usermod -aG ega-update "$TOOL_OWNER" || true
mkdir -p "$EFFECTIVE_STATE/logs" "$EFFECTIVE_BACKUPS"
chown "$API_USER:ega-update" "$EFFECTIVE_STATE"
chown "$API_USER:ega-update" "$EFFECTIVE_STATE/logs"
chown "$TOOL_OWNER:ega-update" "$EFFECTIVE_BACKUPS"
chmod 0770 "$EFFECTIVE_STATE" "$EFFECTIVE_STATE/logs" "$EFFECTIVE_BACKUPS"
# Drain file: absent by default (admitting). Never create here on fresh.
# Admission stop: sudo touch "$EFFECTIVE_STATE/drain"; re-admit: sudo rm -f "$EFFECTIVE_STATE/drain".

# 4b. User-manager linger: job runners launch via `systemd-run --user` as
#     ubuntu and T3 user services run under the ubuntu user manager, so the
#     manager must exist at boot even with no login session.
if [ "$(loginctl show-user "$TOOL_OWNER" -p Linger --value 2>/dev/null || echo no)" = "yes" ]; then
  echo "[install] linger already enabled for $TOOL_OWNER"
else
  loginctl enable-linger "$TOOL_OWNER"
  echo "[install] enabled linger for $TOOL_OWNER (user manager at boot for --user job units)"
fi

# Existing-deploy maintenance gate (R33 steps 1-3, F07/F09): drain FIRST,
# prove quiescence via the version-independent deploy controller (bounded
# 120s, fail closed — never requires any installed release to implement
# new protocol, never stops services on unproven state), then stop
# services and PROVE stopped. Fresh installs skip to venv/stage.
if [ "$EXISTING_DEPLOY" = "1" ]; then
  echo "[install] existing deploy detected — entering maintenance protocol"
  mkdir -p "$EFFECTIVE_STATE"
  if [ -f "$DRAIN" ]; then
    echo "[install] drain already present at $DRAIN (admission already stopped)"
  else
    touch "$DRAIN" || { echo "cannot create drain $DRAIN" >&2; exit 1; }
    DRAIN_CREATED_BY_US=1
    echo "[install] drain created at $DRAIN (new plans/jobs refused)"
  fi
  echo "[install] waiting for quiescence via deploy controller (bounded 120s)..."
  QUIESCED=0
  for _i in $(seq 1 24); do
    if python3 "$QUIESCE_CHECK" --config "$ETC/config.json" >/tmp/ega-install-status.json 2>/tmp/ega-install-status.err; then
      QUIESCED=1
      break
    fi
    echo "[install] not quiescent yet, waiting 5s ($_i/24) — see /tmp/ega-install-status.json reasons..."
    sleep 5
  done
  if [ "$QUIESCED" = "1" ]; then
    echo "[install] quiesced: deploy controller proves no active/unresolved work, no live units, drain present"
  else
    fail_keep_drain "quiescence timeout after 120s (fail closed, drain kept)"
  fi
  if systemctl is-active --quiet ega-update-api 2>/dev/null; then WAS_API=1; fi
  if systemctl is-active --quiet ega-update-worker 2>/dev/null; then WAS_WORKER=1; fi
  systemctl stop ega-update-worker ega-update-api || true
  if systemctl is-active --quiet ega-update-api 2>/dev/null; then
    fail_keep_drain "api failed to stop (fail closed, drain kept)"
  fi
  if systemctl is-active --quiet ega-update-worker 2>/dev/null; then
    fail_keep_drain "worker failed to stop (fail closed, drain kept)"
  fi
  echo "[install] stopped: api and worker proven inactive"
fi

# 5. Dedicated app venv + pinned requirements install (baseline Python
#    >=3.10,<3.14; shared runtimes are untouched — this only creates the
#    release-local venv). --require-hashes REQUIRED (R35): no fallback to an
#    unhashed install. A hashless requirements file blocks here with a
#    message (generate hashes during authorized release prep).
command -v python3 >/dev/null 2>&1 || { echo "python3 (>=3.10,<3.14) required" >&2; exit 1; }
python3 -c 'import sys; raise SystemExit(0 if (3, 10) <= sys.version_info < (3, 14) else 1)' \
  || { echo "python3 >=3.10,<3.14 required (got: $(python3 --version 2>&1))" >&2; exit 1; }
python3 -m venv "$RELEASE_DIR/venv" || fail_keep_drain "venv creation failed"
cd "$RELEASE_DIR"
EGA_CONFIG_FILE="$ETC/config.json" "$RELEASE_DIR/venv/bin/pip" install --require-hashes -r "$RELEASE_DIR/backend/requirements.txt" \
  || fail_keep_drain "requirements install failed (pip --require-hashes REQUIRED; missing hashes file is blocked — generate hashes during authorized release prep, never fabricate them)"

# 5b. Stage-time MANIFEST (R32): sha256 of backend/** recorded now; the
# validator verifies it before any switch (validator never writes it).
cd "$RELEASE_DIR"
find backend -type f -exec sha256sum {} + | sort > "$RELEASE_DIR/MANIFEST" \
  || fail_keep_drain "manifest staging failed"
echo "[install] staged MANIFEST with $(wc -l < "$RELEASE_DIR/MANIFEST") backend files"

# 5c. Stage validation (R32/R35): validator runs under the release venv
# with CWD at the release root and EGA_CONFIG_FILE exported. Any block
# aborts before the symlink switch (drain kept on existing deploys).
cd "$RELEASE_DIR"
if EGA_CONFIG_FILE="$ETC/config.json" "$RELEASE_DIR/venv/bin/python" "$RELEASE_DIR/deploy/etc/validate-release.py" --release "$RELEASE_DIR" --config "$ETC/config.json"; then
  echo "[install] stage validation ok"
else
  fail_keep_drain "stage validation blocked (see validator reason above)"
fi

# 6. Consistent SQLite backup BEFORE migrate (existing deploys only).
#    Writers are already quiesced + stopped above; use the SQLite backup API
#    so WAL content is included — never a bare `cp` of a live DB file.
#    State paths come from config (R32), not hardcoded $STATE.
if [ "$EXISTING_DEPLOY" = "1" ] && [ -f "$EFFECTIVE_DB" ]; then
  TS="$(date -u +%Y%m%dT%H%M%SZ)"
  cd "$RELEASE_DIR"
  EGA_CONFIG_FILE="$ETC/config.json" "$RELEASE_DIR/venv/bin/python" -m backend.app.db backup "$EFFECTIVE_DB" "$EFFECTIVE_BACKUPS/state-preinstall-$TS.db" \
    || fail_keep_drain "pre-install DB backup failed"
  chmod 0600 "$EFFECTIVE_BACKUPS"/state-preinstall-*.db
  chown "$API_USER":"$API_USER" "$EFFECTIVE_BACKUPS"/state-preinstall-*.db || true
fi

# 6b. Migrate via the release module entrypoint (N04: only the controlled
# deploy procedure migrates; services validate only). CWD at release root.
cd "$RELEASE_DIR"
if EGA_CONFIG_FILE="$ETC/config.json" "$RELEASE_DIR/venv/bin/python" -m backend.app.db migrate; then
  echo "[install] migrate ok"
else
  fail_keep_drain "migration failed (drain kept; see RUNBOOK migration-recovery)"
fi
# Joint-group DB ownership so both API (ega-update) and worker (ubuntu)
# read/write state.db/WAL/SHM; 0660 keeps it restricted to the two accounts.
# Secrets stay 0600 root:API_USER (unchanged, see §3).
cd "$RELEASE_DIR"
DB_PATH="$(EGA_CONFIG_FILE="$ETC/config.json" "$RELEASE_DIR/venv/bin/python" -m backend.app.config_cli get --require db_path 2>/dev/null || printf '%s' "$EFFECTIVE_DB")"
for dbf in "$DB_PATH" "$DB_PATH-wal" "$DB_PATH-shm" "$DB_PATH-journal"; do
  if [ -e "$dbf" ]; then chown "$API_USER:ega-update" "$dbf" || true; chmod 0660 "$dbf" || true; fi
done

# 7. Atomic symlink switch (R33 step 7) — only after stage+validate+migrate.
ln -sfn "$RELEASE_DIR" "$CURRENT_LINK" || fail_keep_drain "cannot flip current symlink"

# 7b. Port drop-in (R35): render the effective listen port from config so
# the unit never diverges from config defaults. config_cli fails closed;
# an unparsable port blocks here instead of rendering a wrong default.
EFFECTIVE_PORT="$(EGA_CONFIG_FILE="$ETC/config.json" "$RELEASE_DIR/venv/bin/python" -m backend.app.config_cli get --require listen_port 2>/dev/null)" || fail_keep_drain "listen_port unparsable in $ETC/config.json"
mkdir -p /etc/systemd/system/ega-update-api.service.d
printf '[Service]\nEnvironment=EGA_LISTEN_PORT=%s\n' "$EFFECTIVE_PORT" > /etc/systemd/system/ega-update-api.service.d/10-port.conf
chmod 0644 /etc/systemd/system/ega-update-api.service.d/10-port.conf
echo "[install] rendered port drop-in 10-port.conf with EGA_LISTEN_PORT=$EFFECTIVE_PORT"

# 8. Systemd units: daemon-reload + enable + start order (api, worker, cloudflared).
# Job units launch via `systemd-run --user` as ubuntu (see RUNBOOK --user
# launch model); inspect them with `systemctl --user` as ubuntu (with
# XDG_RUNTIME_DIR set, e.g. via `sudo -u ubuntu -i`), never the system
# manager. Runner templates document the transient unit shape only.
cp "$CURRENT_LINK/systemd/ega-update-api.service" /etc/systemd/system/ || fail_keep_drain "unit copy failed (api)"
cp "$CURRENT_LINK/systemd/ega-update-worker.service" /etc/systemd/system/ || fail_keep_drain "unit copy failed (worker)"
cp "$CURRENT_LINK/systemd/ega-update-runner@.service" /etc/systemd/system/ || fail_keep_drain "unit copy failed (runner)"
cp "$CURRENT_LINK/systemd/cloudflared-ega-update.service" /etc/systemd/system/ || fail_keep_drain "unit copy failed (tunnel)"
if [ -f "$CURRENT_LINK/systemd/user/ega-update-runner@.service" ]; then
  mkdir -p "/home/$TOOL_OWNER/.config/systemd/user"
  cp "$CURRENT_LINK/systemd/user/ega-update-runner@.service" "/home/$TOOL_OWNER/.config/systemd/user/" || fail_keep_drain "user unit copy failed"
  chown -R "$TOOL_OWNER:$TOOL_OWNER" "/home/$TOOL_OWNER/.config/systemd/user"
  su -s /bin/bash "$TOOL_OWNER" -c 'systemctl --user daemon-reload' || true
fi
systemctl daemon-reload || fail_keep_drain "daemon-reload failed"
systemctl enable ega-update-api ega-update-worker cloudflared-ega-update || fail_keep_drain "unit enable failed"

# 9. Start console services.
systemctl start ega-update-api || fail_keep_drain "api start failed"
systemctl start ega-update-worker || fail_keep_drain "worker start failed"
systemctl start cloudflared-ega-update || echo "[install] WARN: tunnel failed to start (placeholders expected pre-config) — API/worker unaffected" >&2

# 10. Bounded readiness (R33 step 10): API health via curl localhost plus
# worker heartbeat freshness via cli status. Both must pass before undrain.
echo "[install] waiting for readiness (bounded 60s)..."
READY=0
for _i in $(seq 1 12); do
  HTTP_CODE="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$EFFECTIVE_PORT/api/v1/health" 2>/dev/null || printf '000')"
  if [ "$HTTP_CODE" = "401" ] || [ "$HTTP_CODE" = "403" ] || [ "$HTTP_CODE" = "200" ]; then
    cd "$RELEASE_DIR"
    if EGA_CONFIG_FILE="$ETC/config.json" "$RELEASE_DIR/venv/bin/python" -m backend.app.cli status --require-ready >/tmp/ega-install-ready.json 2>/dev/null; then
      READY=1
      break
    fi
  fi
  echo "[install] readiness pending (http=$HTTP_CODE, $_i/12)..."
  sleep 5
done
if [ "$READY" = "1" ]; then
  echo "[install] readiness ok (api http=$HTTP_CODE, worker heartbeat fresh)"
else
  # Rollback rule (R33): restore PRIOR symlink ONLY when compat passes;
  # else leave manual-recovery state. Never restart the broken release.
  if [ -n "$PREV_RELEASE" ] && [ -d "$PREV_RELEASE" ] && [ "$PREV_RELEASE" != "$RELEASE_DIR" ]; then
    cd "$RELEASE_DIR"
    if EGA_CONFIG_FILE="$ETC/config.json" "$RELEASE_DIR/venv/bin/python" "$RELEASE_DIR/deploy/etc/validate-release.py" --check-compat "$PREV_RELEASE" "$RELEASE_DIR" >/dev/null 2>&1; then
      echo "[install] readiness failed — compat ok, restoring prior release $PREV_RELEASE" >&2
      ln -sfn "$PREV_RELEASE" "$CURRENT_LINK"
      systemctl daemon-reload || true
      systemctl restart ega-update-api ega-update-worker || true
      fail_keep_drain "readiness failed; prior release restored (drain kept for review)"
    fi
    echo "[install] readiness failed — schema drift: MANUAL RECOVERY (prior symlink NOT restored; drain kept)" >&2
    fail_keep_drain "readiness failed with schema drift; see RUNBOOK migration-recovery"
  fi
  fail_keep_drain "readiness failed after 60s (drain kept)"
fi

# 9b. Localhost-binding + Access JWT validation checks (implemented here;
#    executed only when this script runs on the VM, never during code review).
echo "[check] localhost binding:"
(ss -ltnp 2>/dev/null | grep -E "127\\.0\\.0\\.1:$EFFECTIVE_PORT" \
  || echo "WARN: nothing on 127.0.0.1:$EFFECTIVE_PORT — check EGA_LISTEN_PORT and api.env")
cd "$RELEASE_DIR"
LISTEN_HOST="$(EGA_CONFIG_FILE=$ETC/config.json "$RELEASE_DIR/venv/bin/python" -m backend.app.config_cli get --require listen_host 2>/dev/null)" || fail_keep_drain "listen_host unparsable in $ETC/config.json"
if [ "$LISTEN_HOST" = "127.0.0.1" ]; then
  :
else
  echo "REFUSING: listen_host=$LISTEN_HOST is not 127.0.0.1" >&2
  fail_keep_drain "listen_host is not loopback"
fi
echo "[check] Access JWT config present:"
cd "$RELEASE_DIR"
ACCESS_CHECK="$(EGA_CONFIG_FILE=$ETC/config.json "$RELEASE_DIR/venv/bin/python" -m backend.app.config_cli json 2>/dev/null)" || fail_keep_drain "Access config unreadable"
for _k in team_domain audience public_origin; do
  case "$ACCESS_CHECK" in
    *"\"$_k\": \"\""*|*"\"$_k\": \"CHANGEME"*)
      fail_keep_drain "Access config incomplete: $_k placeholder/missing" ;;
  esac
done
for _k in owner_emails csrf_secret; do
  _v="$(EGA_CONFIG_FILE=$ETC/config.json "$RELEASE_DIR/venv/bin/python" -m backend.app.config_cli get "$_k" 2>/dev/null)" || fail_keep_drain "Access config unreadable: $_k"
  case "$_v" in
    ""|"CHANGEME"*)
      fail_keep_drain "Access config incomplete: $_k placeholder/missing" ;;
  esac
done
echo "[check] Access config ok"

# 10. Conflicting updaterSchedule handling — RECORD-ONLY by default.
#     Detect cron entries, systemd timers, and tool self-update flags that
#     could race the console lock. Disable ONLY confirmed-conflicting ones,
#     AFTER saving prior config for restoration. Never blanket-disable.
echo "[check] conflicting automation scan (record-only):"
CONFLICT_DIR="$EFFECTIVE_STATE/conflicting-automation"
mkdir -p "$CONFLICT_DIR"
chmod 0700 "$CONFLICT_DIR"
{
  echo "## crontab ($TOOL_OWNER)"; crontab -u "$TOOL_OWNER" -l 2>/dev/null || echo "(none)";
  echo "## crontab (root)"; crontab -l 2>/dev/null || echo "(none)";
  echo "## systemd timers"; systemctl list-timers --all --no-pager 2>/dev/null || true;
} > "$CONFLICT_DIR/scan-$(date -u +%Y%m%dT%H%M%SZ).txt"
echo "  scan saved to $CONFLICT_DIR/. Review it; disable ONLY confirmed-conflicting"
echo "  entries (e.g. a nightly tool updater timer) with:"
echo "    systemctl cat <timer> > $CONFLICT_DIR/<timer>.unit.bak"
echo "    sudo systemctl disable --now <timer>"
echo "  and record the restoration command in $CONFLICT_DIR/README."

# 11. Release drain only on success (R33 step 11). A pre-existing drain
# (manual maintenance) is left for its owner to clear.
if [ "$DRAIN_CREATED_BY_US" = "1" ]; then
  rm -f "$DRAIN"
  echo "[install] drain removed (admitting); done: $COMMIT"
else
  echo "[install] done: $COMMIT (tool state untouched)"
fi

# Lingering / user-services: job runners launch via `systemd-run --user` as
# ubuntu and T3 user services run under the ubuntu user manager, so
# `loginctl enable-linger ubuntu` is REQUIRED (see §4b with idempotent
# check). Without linger the user manager (and all --user job units) dies
# when the last SSH session closes. The worker itself remains a system unit
# under User=ubuntu; only job execution uses the user manager.

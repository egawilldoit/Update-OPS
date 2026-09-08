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
# Maintenance protocol (R33, 11 steps):
#  (1) write drain file; (2) cli status proves no active/unresolved
#  (bounded 120s, fail closed); (3) stop API+worker, PROVE stopped (fail
#  closed, abort, keep drain); (4) consistent DB backup; (5) stage+validate
#  (validator); (6) migrate via release venv; (7) atomic symlink switch;
#  (8) install units (+ port drop-in); (9) start; (10) bounded API+worker
#  readiness (health endpoint via curl localhost + heartbeat freshness via
#  cli status); (11) release drain only on success.
# Failure: preserve drain, restore PRIOR release symlink ONLY if schema
# compat check passes (validator --check-compat OLD NEW), else leave
# manual-recovery state with a clear message. Never "start same broken
# release" as rollback.
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

COMMIT=""
TARBALL=""
PREFIX="/opt/ega-update"
ETC="/etc/ega-update"
STATE="/var/lib/ega-update"
API_USER="ega-update"

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
PREV_RELEASE="$(readlink -f "$CURRENT_LINK" 2>/dev/null || echo '')"
if [ -z "$PREV_RELEASE" ] || [ -L "$CURRENT_LINK" ]; then
  :
else
  echo "[upgrade] REFUSING: $CURRENT_LINK is not a symlink (manual state)" >&2
  exit 1
fi

# EGA_CONFIG_FILE exported for ALL python invocations (R32), including
# quiescence/status/validator/migrate/readiness.
export EGA_CONFIG_FILE="$ETC/config.json"

# Known-secret redaction source (S01, idempotent): older installs may
# predate secrets.env provisioning. Create EMPTY when absent so the
# structural secret-source contract holds after upgrade; never touch an
# existing owner-managed file, never write values, never print contents.
# 0640 root:ega-update: readable by the ubuntu worker and the ega-update
# API via group (see install.sh; 0600 root-owned would fail readiness).
if [ -e "$ETC/secrets.env" ]; then
  :
else
  : > "$ETC/secrets.env"
  chmod 0640 "$ETC/secrets.env"
  chown root:ega-update "$ETC/secrets.env"
  echo "[upgrade] created empty known-secret source $ETC/secrets.env"
fi

# Config values come ONLY from the trusted checkout parser (F06/N16):
# PYTHONPATH=$REPO_ROOT (operator checkout), never the candidate
# release, never an old install. Existing config must parse; there are
# no silent fallbacks for an existing-but-broken config.
cfg_cli() {
  PYTHONPATH="$REPO_ROOT" EGA_CONFIG_FILE="$ETC/config.json" python3 -m backend.app.config_cli "$@"
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

EFFECTIVE_STATE="$(cfg_value state_dir "$STATE")"
EFFECTIVE_DB="$(cfg_value db_path "$EFFECTIVE_STATE/state.db")"
EFFECTIVE_BACKUPS="$(cfg_value backup_dir "$EFFECTIVE_STATE/backups")"
EFFECTIVE_PORT="$(cfg_value listen_port "8771")"
DRAIN="$EFFECTIVE_STATE/drain"
DRAIN_CREATED_BY_US=0
WAS_API=0
WAS_WORKER=0

echo "[upgrade] $PREV_RELEASE -> $RELEASE_DIR"
echo "[upgrade] state_dir=$EFFECTIVE_STATE db=$EFFECTIVE_DB port=$EFFECTIVE_PORT"

fail() {
  # Explicit restore path on failure: the drain is KEPT (still blocking
  # admission) for manual review. The PRIOR release symlink is restored
  # ONLY when the validator compat check passes; otherwise the host stays
  # in manual-recovery state. Never restarts the just-failed release.
  # Usage: fail "message"
  echo "[upgrade] FAILED: $1" >&2
  if [ -n "$PREV_RELEASE" ] && [ -d "$PREV_RELEASE" ] && [ "$(readlink -f "$CURRENT_LINK" 2>/dev/null || echo '')" = "$RELEASE_DIR" ]; then
    cd "$RELEASE_DIR"
    if EGA_CONFIG_FILE="$ETC/config.json" "$RELEASE_DIR/venv/bin/python" "$RELEASE_DIR/deploy/etc/validate-release.py" --check-compat "$PREV_RELEASE" "$RELEASE_DIR" >/dev/null 2>&1; then
      echo "[upgrade] compat ok — restoring prior release symlink $PREV_RELEASE" >&2
      ln -sfn "$PREV_RELEASE" "$CURRENT_LINK" || true
      systemctl daemon-reload || true
      if [ "$WAS_WORKER" = "1" ]; then systemctl start ega-update-worker || true; fi
      if [ "$WAS_API" = "1" ]; then systemctl start ega-update-api || true; fi
    else
      echo "[upgrade] schema drift — MANUAL RECOVERY (prior symlink NOT restored; services NOT restarted)" >&2
      echo "[upgrade] follow docs/RUNBOOK.md migration-recovery with $EFFECTIVE_BACKUPS/state-preupgrade-*.db" >&2
    fi
  else
    # Failure before the switch: prior symlink untouched; restart what was
    # running before (explicit restore, no bare set -e).
    if [ "$WAS_WORKER" = "1" ]; then systemctl start ega-update-worker || true; fi
    if [ "$WAS_API" = "1" ]; then systemctl start ega-update-api || true; fi
  fi
  echo "[upgrade] restore: drain KEPT at $DRAIN (blocking)." >&2
  echo "[upgrade] inspect, reconcile (docs/RUNBOOK.md §5-§6), then sudo rm -f $DRAIN only when healthy." >&2
  exit 1
}

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

# (4) Consistent DB backup (SQLite backup API via the release db tool —
# never bare cp of a live DB, never inline python). Writers stopped above.
TS="$(date -u +%Y%m%dT%H%M%SZ)"
PREV_VENV="$CURRENT_LINK/venv/bin/python"
cd "$PREV_RELEASE" 2>/dev/null || cd /tmp
EGA_CONFIG_FILE="$ETC/config.json" "$PREV_VENV" -m backend.app.db backup "$EFFECTIVE_DB" "$EFFECTIVE_BACKUPS/state-preupgrade-$TS.db" \
  || fail "pre-upgrade DB backup failed"
chmod 0600 "$EFFECTIVE_BACKUPS/state-preupgrade-$TS.db"
chown "$API_USER":"$API_USER" "$EFFECTIVE_BACKUPS/state-preupgrade-$TS.db" || true
chmod 0600 "$EFFECTIVE_BACKUPS/state-preupgrade-$TS.db"
echo "[upgrade] backup: $EFFECTIVE_BACKUPS/state-preupgrade-$TS.db"

# (5) Stage the new release dir (immutable, root-owned), write MANIFEST via
# sha256sum, install venv with --require-hashes (NO fallback), then validate.
if [ -e "$RELEASE_DIR" ]; then fail "release dir exists: $RELEASE_DIR"; fi
mkdir -p "$RELEASE_DIR" || fail "cannot create $RELEASE_DIR"
# F15: immutable staging — copy the operator tarball into a
# root-controlled staging dir, digest it, validate the STAGED copy,
# re-verify the digest, then extract exactly that file (no TOCTOU).
STAGE_DIR="$(mktemp -d /var/tmp/ega-stage-XXXXXXXX)" || fail "cannot create staging dir"
chmod 0700 "$STAGE_DIR"
STAGED_ARCHIVE="$STAGE_DIR/release.tar.gz"
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
if EGA_CONFIG_FILE="$ETC/config.json" "$RELEASE_DIR/venv/bin/python" "$RELEASE_DIR/deploy/etc/validate-release.py" --release "$RELEASE_DIR" --config "$ETC/config.json"; then
  echo "[upgrade] stage validation ok"
else
  fail "stage validation blocked (drain kept; prior release still linked)"
fi

# (6) Migrate via the release module entrypoint (N04: only the controlled
# deploy procedure migrates; services validate only). CWD at release root.
cd "$RELEASE_DIR"
if EGA_CONFIG_FILE="$ETC/config.json" "$RELEASE_DIR/venv/bin/python" -m backend.app.db migrate; then
  echo "[upgrade] migrate ok"
else
  fail "migration failed (see migration-recovery in RUNBOOK; drain kept)"
fi

# (7) Atomic symlink switch — only after stage+validate+migrate.
ln -sfn "$RELEASE_DIR" "$CURRENT_LINK" || fail "cannot flip current symlink"

# (8) Install units (+ R35 port drop-in rendered from config via
# config_cli — an unparsable port blocks instead of a wrong default).
EFFECTIVE_PORT_NOW="$(cfg_cli get --require listen_port 2>/dev/null)" || fail "listen_port unparsable in $ETC/config.json"
mkdir -p /etc/systemd/system/ega-update-api.service.d
printf '[Service]\nEnvironment=EGA_LISTEN_PORT=%s\n' "$EFFECTIVE_PORT_NOW" > /etc/systemd/system/ega-update-api.service.d/10-port.conf || fail "port drop-in write failed"
chmod 0644 /etc/systemd/system/ega-update-api.service.d/10-port.conf
echo "[upgrade] rendered port drop-in 10-port.conf with EGA_LISTEN_PORT=$EFFECTIVE_PORT_NOW"
cp "$CURRENT_LINK/systemd/"*.service /etc/systemd/system/ || fail "unit copy failed"
if [ -f "$CURRENT_LINK/systemd/user/ega-update-runner@.service" ]; then
  mkdir -p /home/ubuntu/.config/systemd/user
  cp "$CURRENT_LINK/systemd/user/ega-update-runner@.service" /home/ubuntu/.config/systemd/user/ || fail "user unit copy failed"
  chown -R ubuntu:ubuntu /home/ubuntu/.config/systemd/user || true
  su -s /bin/bash ubuntu -c 'systemctl --user daemon-reload' || true
fi
systemctl daemon-reload || fail "daemon-reload failed"

# (9) Start console services (DB/logs/backups preserved — outside releases).
systemctl restart ega-update-api ega-update-worker cloudflared-ega-update || fail "service restart failed"

# (10) Bounded readiness: API health via curl localhost + worker heartbeat
# freshness via cli status. Both must pass before undrain.
echo "[upgrade] waiting for readiness (bounded 60s)..."
READY=0
for _i in $(seq 1 12); do
  HTTP_CODE="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$EFFECTIVE_PORT_NOW/api/v1/health" 2>/dev/null || printf '000')"
  if [ "$HTTP_CODE" = "401" ] || [ "$HTTP_CODE" = "403" ] || [ "$HTTP_CODE" = "200" ]; then
    cd "$RELEASE_DIR"
    if EGA_CONFIG_FILE="$ETC/config.json" "$RELEASE_DIR/venv/bin/python" -m backend.app.cli status --require-ready >/tmp/ega-upgrade-ready.json 2>/dev/null; then
      READY=1
      break
    fi
  fi
  echo "[upgrade] readiness pending (http=$HTTP_CODE, $_i/12)..."
  sleep 5
done
if [ "$READY" = "1" ]; then
  :
else
  fail "readiness failed after 60s (api/worker not healthy; drain kept)"
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
echo "[upgrade] readiness ok (api http=$HTTP_CODE, worker heartbeat fresh)"

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
echo "ROLLBACK (console code only — never auto-restores tool data):"
echo "  If the validator --check-compat passes for the prior release:"
echo "    sudo ln -sfn $PREV_RELEASE $CURRENT_LINK && sudo systemctl restart ega-update-api ega-update-worker"
echo "  Else (schema changed / migrate failed): follow the migration-recovery"
echo "  procedure in docs/RUNBOOK.md — restore $EFFECTIVE_BACKUPS/state-preupgrade-$TS.db"
echo "  via the documented sqlite3 recovery steps, then re-point current."
echo "  Verify compat first with the release venv:"
echo "    sudo /opt/ega-update/current/venv/bin/python /opt/ega-update/current/deploy/etc/validate-release.py --check-compat $PREV_RELEASE $RELEASE_DIR"
echo "  Tool data is NEVER restored automatically; use the per-job backup in"
echo "  $EFFECTIVE_BACKUPS/<job-id>/ only via an explicit manual procedure."

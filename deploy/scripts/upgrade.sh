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
# paths are parsed from config via python -c JSON, never hardcoded. No
# heredoc-python. pip uses --require-hashes with NO fallback (R35).
set -uo pipefail

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

cfg_value() {
  local file="$1"
  local key="$2"
  local fallback="$3"
  python3 -c 'import json,sys; f=sys.argv[1]; k=sys.argv[2]; d=sys.argv[3]; try:
    data=json.load(open(f,encoding="utf-8"))
    except Exception: print(d); raise SystemExit(0)
 v=data.get(k,""); print(v if isinstance(v,str) and v else d)' \
    "$file" "$key" "$fallback" 2>/dev/null || printf '%s' "$fallback"
}

EFFECTIVE_STATE="$(cfg_value "$ETC/config.json" state_dir "$STATE")"
EFFECTIVE_DB="$(cfg_value "$ETC/config.json" db_path "$EFFECTIVE_STATE/state.db")"
EFFECTIVE_BACKUPS="$(cfg_value "$ETC/config.json" backup_dir "$EFFECTIVE_STATE/backups")"
EFFECTIVE_PORT="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1],encoding="utf-8")).get("listen_port",8771))' "$ETC/config.json" 2>/dev/null || printf '8771')"
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

# (2) Quiescence via cli status (R33): the CLI proves no active job and no
# unresolved runners. Bounded 120s, fail closed. CWD is the release staging
# area once available, else the current release; EGA_CONFIG_FILE exported.
echo "[upgrade] waiting for quiescence via cli status (bounded 120s)..."
STATUS_PY="$CURRENT_LINK/venv/bin/python"
STATUS_CWD="$PREV_RELEASE"
if [ -z "$STATUS_CWD" ] || [ -x "$STATUS_PY" ]; then
  :
else
  STATUS_PY="python3"
  STATUS_CWD="/tmp"
fi
QUIESCED=0
for _i in $(seq 1 24); do
  cd "$STATUS_CWD" 2>/dev/null || cd /tmp
  if EGA_CONFIG_FILE="$ETC/config.json" "$STATUS_PY" -m backend.app.cli status --wait-secs 5 >/tmp/ega-upgrade-status.json 2>/tmp/ega-upgrade-status.err; then
    if python3 -c 'import json,sys; d=json.load(open("/tmp/ega-upgrade-status.json")); raise SystemExit(0 if (not d.get("active_job") and not d.get("unresolved_runners")) else 1)' 2>/dev/null; then
      QUIESCED=1
      break
    fi
    if python3 -c 'import json,sys; d=json.load(open("/tmp/ega-upgrade-status.json")); raise SystemExit(0 if d.get("unresolved_runners") else 1)' 2>/dev/null; then
      echo "[upgrade] unresolved runners present — run reconcile first (drain kept)" >&2
      fail "unresolved runners present"
    fi
    if python3 -c 'import json,sys; d=json.load(open("/tmp/ega-upgrade-status.json")); raise SystemExit(0 if d.get("active_job") else 1)' 2>/dev/null; then
      echo "[upgrade] active job still present, waiting 5s ($_i/24)..."
      sleep 5
    else
      echo "[upgrade] active job still present, waiting 5s ($_i/24)..."
      sleep 5
    fi
  else
    echo "[upgrade] status gate unavailable, waiting 5s ($_i/24)..."
    sleep 5
  fi
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

# (4) Consistent DB backup (SQLite backup API — never bare cp of a live DB).
# State paths come from config (R32). Writers are stopped above.
TS="$(date -u +%Y%m%dT%H%M%SZ)"
PREV_VENV="$CURRENT_LINK/venv/bin/python"
cd "$PREV_RELEASE" 2>/dev/null || cd /tmp
EGA_CONFIG_FILE="$ETC/config.json" "$PREV_VENV" -c 'import sqlite3,sys; src, dst = sys.argv[1], sys.argv[2]; s = sqlite3.connect(src, timeout=10.0); d = sqlite3.connect(dst, timeout=10.0); s.backup(d); s.close(); d.close(); print("backup ok:", dst)' "$EFFECTIVE_DB" "$EFFECTIVE_BACKUPS/state-preupgrade-$TS.db" \
  || fail "pre-upgrade DB backup failed"
chmod 0600 "$EFFECTIVE_BACKUPS/state-preupgrade-$TS.db"
SCHEMA_BEFORE="$(cd "$PREV_RELEASE" 2>/dev/null && EGA_CONFIG_FILE="$ETC/config.json" "$PREV_VENV" -c 'import sqlite3; print(sqlite3.connect(sys.argv[1]).execute("SELECT value FROM schema_meta WHERE key=\x27version\x27").fetchone())' "$EFFECTIVE_DB" 2>/dev/null || printf 'unknown')"
echo "[upgrade] schema before: $SCHEMA_BEFORE; backup: $EFFECTIVE_BACKUPS/state-preupgrade-$TS.db"

# (5) Stage the new release dir (immutable, root-owned), write MANIFEST via
# sha256sum, install venv with --require-hashes (NO fallback), then validate.
if [ -e "$RELEASE_DIR" ]; then fail "release dir exists: $RELEASE_DIR"; fi
mkdir -p "$RELEASE_DIR" || fail "cannot create $RELEASE_DIR"
tar -xzf "$TARBALL" -C "$RELEASE_DIR" || fail "tarball extraction failed"
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

# (6) Migrate against the preserved DB via the NEW release venv (CWD set).
cd "$RELEASE_DIR"
if EGA_CONFIG_FILE="$ETC/config.json" "$RELEASE_DIR/venv/bin/python" -c 'from backend.app.db import connect, migrate; from backend.app.config import load_settings; s = load_settings(); conn = connect(s.db_path); v = migrate(conn); print("migrate ok, schema version:", v)'; then
  echo "[upgrade] migrate ok"
else
  fail "migration failed (see migration-recovery in RUNBOOK; drain kept)"
fi

# (7) Atomic symlink switch — only after stage+validate+migrate.
ln -sfn "$RELEASE_DIR" "$CURRENT_LINK" || fail "cannot flip current symlink"

# (8) Install units (+ R35 port drop-in rendered from config).
EFFECTIVE_PORT_NOW="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1],encoding="utf-8")).get("listen_port",8771))' "$ETC/config.json" 2>/dev/null || printf '8771')"
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
    if EGA_CONFIG_FILE="$ETC/config.json" "$RELEASE_DIR/venv/bin/python" -m backend.app.cli status --wait-secs 5 >/tmp/ega-upgrade-ready.json 2>/dev/null; then
      if python3 -c 'import json,sys; d=json.load(open("/tmp/ega-upgrade-ready.json")); raise SystemExit(0 if d.get("worker_alive") else 1)' 2>/dev/null; then
        READY=1
        break
      fi
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
echo "  If the new release's schema version == $SCHEMA_BEFORE (compatible):"
echo "    sudo ln -sfn $PREV_RELEASE $CURRENT_LINK && sudo systemctl restart ega-update-api ega-update-worker"
echo "  Else (schema changed / migrate failed): follow the migration-recovery"
echo "  procedure in docs/RUNBOOK.md — restore $EFFECTIVE_BACKUPS/state-preupgrade-$TS.db"
echo "  via the documented sqlite3 recovery steps, then re-point current."
echo "  Verify compat first with the release venv:"
echo "    sudo /opt/ega-update/current/venv/bin/python /opt/ega-update/current/deploy/etc/validate-release.py --check-compat $PREV_RELEASE $RELEASE_DIR"
echo "  Tool data is NEVER restored automatically; use the per-job backup in"
echo "  $EFFECTIVE_BACKUPS/<job-id>/ only via an explicit manual procedure."

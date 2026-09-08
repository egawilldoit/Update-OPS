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
# Admission/drain: the drain file <state_dir>/drain is created FIRST so the
# API refuses new plans/jobs during the upgrade; quiescence is awaited
# (bounded wait, fail closed) before services stop. The drain is removed
# ONLY on the success path; failures keep the drain and restart
# previously-running services (explicit restore, no bare `set -e`).
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
[ -n "$COMMIT" ] && [ -n "$TARBALL" ] || { echo "usage: upgrade.sh --commit <sha> --release-tarball <path>" >&2; exit 2; }
[ "$(id -u)" -eq 0 ] || { echo "upgrade.sh must run as root (sudo)" >&2; exit 2; }

RELEASE_DIR="$PREFIX/releases/$COMMIT"
CURRENT_LINK="$PREFIX/current"
PREV_RELEASE="$(readlink -f "$CURRENT_LINK" || echo '')"
DRAIN="$STATE/drain"
DRAIN_CREATED_BY_US=0
WAS_API=0
WAS_WORKER=0

echo "[upgrade] $PREV_RELEASE -> $RELEASE_DIR"

fail() {
  # Explicit restore path on failure: restart previously-running services;
  # the drain is KEPT (still blocking admission) for manual review.
  # Usage: fail "message"
  echo "[upgrade] FAILED: $1" >&2
  if [ "$WAS_WORKER" = "1" ]; then systemctl start ega-update-worker || true; fi
  if [ "$WAS_API" = "1" ]; then systemctl start ega-update-api || true; fi
  echo "[upgrade] restore: previously-running services restarted; drain KEPT at $DRAIN (blocking)." >&2
  echo "[upgrade] inspect, reconcile (docs/RUNBOOK.md §5-§6), then sudo rm -f $DRAIN only when healthy." >&2
  exit 1
}

# 0. Admission stop FIRST: create drain before any other step so the API
#    refuses new plans/jobs while present (no drain removal on failure path).
mkdir -p "$STATE"
if [ -f "$DRAIN" ]; then
  echo "[upgrade] drain already present at $DRAIN (admission already stopped)"
else
  touch "$DRAIN" || { echo "cannot create drain $DRAIN" >&2; exit 1; }
  DRAIN_CREATED_BY_US=1
  echo "[upgrade] drain created at $DRAIN (new plans/jobs refused)"
fi

# 1. Quiescence: wait (bounded, fail closed) for no nonterminal jobs and no
#    recovery block AFTER the drain is in place. New admissions are already
#    refused; in-flight jobs must drain before we stop services.
echo "[upgrade] waiting for quiescence (bounded 120s)..."
QUIESCE_DEADLINE=24
QUIESCED=0
for _i in $(seq 1 $QUIESCE_DEADLINE); do
  if "$CURRENT_LINK/venv/bin/python" - <<'PY'
import sys
from backend.app.db import connect
from backend.app.config import load_settings
from backend.app.jobs import NONTERMINAL, recovery_blocked
s = load_settings()
conn = connect(s.db_path)
active = conn.execute(
    "SELECT id FROM jobs WHERE state IN (?,?,?,?,?)",
    NONTERMINAL).fetchall()
if active:
    sys.exit(10)
if recovery_blocked(conn):
    sys.exit(11)
sys.exit(0)
PY
  then
    QUIESCED=1
    break
  else
    _code=$?
    if [ "$_code" = "11" ]; then
      echo "[upgrade] recovery block set — run reconcile first (drain kept)" >&2
      fail "recovery_required is set"
    fi
    echo "[upgrade] active job still present, waiting 5s ($_i/$QUIESCE_DEADLINE)..."
    sleep 5
  fi
done
[ "$QUIESCED" = "1" ] || fail "quiescence timeout: nonterminal job(s) still present after 120s (fail closed, drain kept)"
echo "[upgrade] quiesced: no nonterminal jobs, no recovery block"

# 2. Consistent DB backup (SQLite backup API — never bare cp of a live DB).
# Record previously-running services BEFORE stopping so fail() restores them.
if systemctl is-active --quiet ega-update-api 2>/dev/null; then WAS_API=1; fi
if systemctl is-active --quiet ega-update-worker 2>/dev/null; then WAS_WORKER=1; fi
systemctl stop ega-update-worker ega-update-api || true
TS="$(date -u +%Y%m%dT%H%M%SZ)"
PREV_VENV="$CURRENT_LINK/venv/bin/python"
"$PREV_VENV" - "$STATE/state.db" "$STATE/backups/state-preupgrade-$TS.db" <<'PY'
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
s = sqlite3.connect(src, timeout=10.0)
d = sqlite3.connect(dst, timeout=10.0)
with d:
    s.backup(d)
s.close(); d.close()
print("backup ok:", dst)
PY
[ "$?" -eq 0 ] || fail "pre-upgrade DB backup failed"
chmod 0600 "$STATE/backups/state-preupgrade-$TS.db"
SCHEMA_BEFORE="$("$PREV_VENV" -c "import sqlite3; print(sqlite3.connect('$STATE/state.db').execute(\"SELECT value FROM schema_meta WHERE key='version'\").fetchone())" 2>/dev/null || echo unknown)"
echo "[upgrade] schema before: $SCHEMA_BEFORE; backup: $STATE/backups/state-preupgrade-$TS.db"

# 3. Install new release dir (immutable, root-owned).
[ -e "$RELEASE_DIR" ] && fail "release dir exists: $RELEASE_DIR"
mkdir -p "$RELEASE_DIR" || fail "cannot create $RELEASE_DIR"
tar -xzf "$TARBALL" -C "$RELEASE_DIR" || fail "tarball extraction failed"
chown -R root:root "$RELEASE_DIR"
# Build output mapping: frontend/vite.config.ts outDir is
# ../backend/app/static and backend/app/main.py serves backend/app/static,
# so the gate checks backend/app/static/index.html (not frontend/dist/).
[ -f "$RELEASE_DIR/backend/app/static/index.html" ] || fail "tarball missing built frontend (backend/app/static/index.html)"
# Dedicated app venv (baseline Python >=3.10,<3.14; shared runtimes untouched).
command -v python3 >/dev/null 2>&1 || fail "python3 (>=3.10,<3.14) required"
python3 -c 'import sys; raise SystemExit(0 if (3, 10) <= sys.version_info < (3, 14) else 1)' \
  || fail "python3 >=3.10,<3.14 required (got: $(python3 --version 2>&1))"
python3 -m venv "$RELEASE_DIR/venv" || fail "venv creation failed"
"$RELEASE_DIR/venv/bin/pip" install --require-hashes -r "$RELEASE_DIR/backend/requirements.txt" 2>/dev/null \
  || "$RELEASE_DIR/venv/bin/pip" install -r "$RELEASE_DIR/backend/requirements.txt" \
  || fail "requirements install failed"

# 4. Migrate against the preserved DB, then flip `current`.
EGA_CONFIG_FILE="$ETC/config.json" "$RELEASE_DIR/venv/bin/python" - <<'PY'
from backend.app.db import connect, migrate
from backend.app.config import load_settings
s = load_settings()
conn = connect(s.db_path)
v = migrate(conn)
print("migrate ok, schema version:", v)
PY
[ "$?" -eq 0 ] || fail "migration failed (see migration-recovery in RUNBOOK; drain kept)"
ln -sfn "$RELEASE_DIR" "$CURRENT_LINK" || fail "cannot flip current symlink"

# 5. Restart console services (DB/logs/backups preserved — they live outside releases).
cp "$CURRENT_LINK/systemd/"*.service /etc/systemd/system/ || fail "unit copy failed"
if [ -f "$CURRENT_LINK/systemd/user/ega-update-runner@.service" ]; then
  mkdir -p /home/ubuntu/.config/systemd/user
  cp "$CURRENT_LINK/systemd/user/ega-update-runner@.service" /home/ubuntu/.config/systemd/user/ || fail "user unit copy failed"
  chown -R ubuntu:ubuntu /home/ubuntu/.config/systemd/user || true
  su -s /bin/bash ubuntu -c 'systemctl --user daemon-reload' || true
fi
systemctl daemon-reload || fail "daemon-reload failed"
systemctl restart ega-update-api ega-update-worker cloudflared-ega-update || fail "service restart failed"
systemctl is-active --quiet ega-update-api || fail "api failed to start — see rollback below"
systemctl is-active --quiet ega-update-worker || fail "worker failed to start — see rollback below"

# Success path ONLY: remove drain to re-admit plans/jobs — but only when
# this run created it. A pre-existing drain (manual maintenance) is left for
# its owner to clear.
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
echo "  procedure in docs/RUNBOOK.md — restore $STATE/backups/state-preupgrade-$TS.db"
echo "  via the documented sqlite3 recovery steps, then re-point current."
echo "  Tool data is NEVER restored automatically; use the per-job backup in"
echo "  $STATE/backups/<job-id>/ only via an explicit manual procedure."

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
set -euo pipefail

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

echo "[upgrade] $PREV_RELEASE -> $RELEASE_DIR"

# 1. Require no active job and no recovery block (fail otherwise).
"$CURRENT_LINK/venv/bin/python" - <<'PY'
import sys
from backend.app.db import connect
from backend.app.config import load_settings
from backend.app.jobs import NONTERMINAL, recovery_blocked
s = load_settings()
conn = connect(s.db_path)
active = conn.execute(
    "SELECT id, tool_id, state FROM jobs WHERE state IN (?,?,?,?,?)",
    NONTERMINAL).fetchall()
if active:
    print("REFUSING: active job(s): %s" % [dict(r) for r in active], file=sys.stderr)
    sys.exit(1)
if recovery_blocked(conn):
    print("REFUSING: recovery_required is set — run reconcile first", file=sys.stderr)
    sys.exit(1)
print("no active job, no recovery block")
PY

# 2. Consistent DB backup (SQLite backup API — never bare cp of a live DB).
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
chmod 0600 "$STATE/backups/state-preupgrade-$TS.db"
SCHEMA_BEFORE="$("$PREV_VENV" -c "import sqlite3; print(sqlite3.connect('$STATE/state.db').execute(\"SELECT value FROM schema_meta WHERE key='version'\").fetchone())" 2>/dev/null || echo unknown)"
echo "[upgrade] schema before: $SCHEMA_BEFORE; backup: $STATE/backups/state-preupgrade-$TS.db"

# 3. Install new release dir (immutable, root-owned).
[ -e "$RELEASE_DIR" ] && { echo "release dir exists: $RELEASE_DIR" >&2; exit 1; }
mkdir -p "$RELEASE_DIR"
tar -xzf "$TARBALL" -C "$RELEASE_DIR"
chown -R root:root "$RELEASE_DIR"
# Build output mapping: frontend/vite.config.ts outDir is
# ../backend/app/static and backend/app/main.py serves backend/app/static,
# so the gate checks backend/app/static/index.html (not frontend/dist/).
[ -f "$RELEASE_DIR/backend/app/static/index.html" ] || { echo "tarball missing built frontend (backend/app/static/index.html)" >&2; exit 1; }
# Dedicated app venv (baseline Python >=3.10,<3.14; shared runtimes untouched).
command -v python3 >/dev/null 2>&1 || { echo "python3 (>=3.10,<3.14) required" >&2; exit 1; }
python3 -c 'import sys; raise SystemExit(0 if (3, 10) <= sys.version_info < (3, 14) else 1)' \
  || { echo "python3 >=3.10,<3.14 required (got: $(python3 --version 2>&1))" >&2; exit 1; }
python3 -m venv "$RELEASE_DIR/venv"
"$RELEASE_DIR/venv/bin/pip" install --require-hashes -r "$RELEASE_DIR/backend/requirements.txt" 2>/dev/null \
  || "$RELEASE_DIR/venv/bin/pip" install -r "$RELEASE_DIR/backend/requirements.txt"

# 4. Migrate against the preserved DB, then flip `current`.
EGA_CONFIG_FILE="$ETC/config.json" "$RELEASE_DIR/venv/bin/python" - <<'PY'
from backend.app.db import connect, migrate
from backend.app.config import load_settings
s = load_settings()
conn = connect(s.db_path)
v = migrate(conn)
print("migrate ok, schema version:", v)
PY
ln -sfn "$RELEASE_DIR" "$CURRENT_LINK"

# 5. Restart console services (DB/logs/backups preserved — they live outside releases).
cp "$CURRENT_LINK/systemd/"*.service /etc/systemd/system/
systemctl daemon-reload
systemctl restart ega-update-api ega-update-worker cloudflared-ega-update
systemctl is-active --quiet ega-update-api || { echo "api failed to start — see rollback below" >&2; exit 1; }
systemctl is-active --quiet ega-update-worker || { echo "worker failed to start — see rollback below" >&2; exit 1; }

echo "[upgrade] done: $COMMIT"
echo ""
echo "ROLLBACK (console code only — never auto-restores tool data):"
echo "  If the new release's schema version == $SCHEMA_BEFORE (compatible):"
echo "    sudo ln -sfn $PREV_RELEASE $CURRENT_LINK && sudo systemctl restart ega-update-api ega-update-worker"
echo "  Else (schema changed / migrate failed): follow the migration-recovery"
echo "  procedure in docs/RUNBOOK.md — restore $STATE/backups/state-preupgrade-$TS.db"
echo "  via the documented sqlite3 recovery steps, then re-point current."
echo "  Tool data is NEVER restored automatically; use the per-job backup in"
echo "  $STATE/backups/<job-id>/ only via an explicit manual procedure."

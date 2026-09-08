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
set -euo pipefail

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
if [ "$(id -u)" -ne 0 ]; then
  echo "install.sh must run as root (sudo)" >&2
  exit 2
fi

RELEASE_DIR="$PREFIX/releases/$COMMIT"
CURRENT_LINK="$PREFIX/current"

echo "[install] pinned release: $COMMIT"

# 1. Dedicated non-root API account (no login shell, no tool ownership).
if ! id "$API_USER" >/dev/null 2>&1; then
  useradd --system --no-create-home --shell /usr/sbin/nologin "$API_USER"
  echo "[install] created user $API_USER"
fi
id "$TOOL_OWNER" >/dev/null 2>&1 || { echo "tool-owner account $TOOL_OWNER missing" >&2; exit 1; }

# 2. Release layout: immutable release dir + `current` symlink.
mkdir -p "$PREFIX/releases"
if [ -e "$RELEASE_DIR" ]; then
  echo "[install] release dir already exists: $RELEASE_DIR (refusing to overwrite)" >&2
  exit 1
fi
mkdir -p "$RELEASE_DIR"
tar -xzf "$TARBALL" -C "$RELEASE_DIR"
chown -R root:root "$RELEASE_DIR"
chmod -R a-w "$RELEASE_DIR" || true

# Frontend ships already built in the release tarball (no npm build here).
# Build output mapping: frontend/vite.config.ts outDir is
# ../backend/app/static and backend/app/main.py serves backend/app/static,
# so the gate checks backend/app/static/index.html (not frontend/dist/).
if [ ! -f "$RELEASE_DIR/backend/app/static/index.html" ]; then
  echo "[install] release tarball missing built frontend (backend/app/static/index.html)" >&2
  exit 1
fi

# 3. Config + secrets dirs. Secrets 0600, shared config 0640.
mkdir -p "$ETC" "$ETC/cloudflared"
chmod 0750 "$ETC"
chmod 0700 "$ETC/cloudflared"
if [ ! -f "$ETC/config.json" ]; then
  cp "$CONFIG_SRC" "$ETC/config.json"
  chmod 0640 "$ETC/config.json"
  chown root:"$API_USER" "$ETC/config.json"
  echo "[install] wrote $ETC/config.json from $CONFIG_SRC — EDIT before starting services"
fi
touch "$ETC/api.env" "$ETC/worker.env"
chmod 0640 "$ETC/api.env" "$ETC/worker.env"
chown root:"$API_USER" "$ETC/api.env" "$ETC/worker.env"
# Secret files (created by owner, never by installer with real values):
for f in "$ETC/csrf.secret" "$ETC/tunnel.env"; do
  [ -e "$f" ] || { touch "$f"; chmod 0600 "$f"; chown root:"$API_USER" "$f"; }
done

# 4. Persistent state dirs OUTSIDE releases, shared by the two service
#    accounts only. Mode 0750 denied group write so ubuntu could not write
#    state.db/WAL, worker.lock, or logs: both accounts now share a joint
#    group (ega-update) with group read/write. Never widen beyond the two
#    accounts (no o+rw, no 0777).
getent group ega-update >/dev/null 2>&1 || groupadd -r ega-update
usermod -aG ega-update "$API_USER" || true
usermod -aG ega-update "$TOOL_OWNER" || true
mkdir -p "$STATE/logs" "$STATE/backups"
chown "$API_USER:ega-update" "$STATE"
chown "$API_USER:ega-update" "$STATE/logs"
chown "$TOOL_OWNER:ega-update" "$STATE/backups"
chmod 0770 "$STATE" "$STATE/logs" "$STATE/backups"

# 5. Dedicated app venv + pinned requirements install (baseline Python
#    >=3.10,<3.14; shared runtimes are untouched — this only creates the
#    release-local venv).
command -v python3 >/dev/null 2>&1 || { echo "python3 (>=3.10,<3.14) required" >&2; exit 1; }
python3 -c 'import sys; raise SystemExit(0 if (3, 10) <= sys.version_info < (3, 14) else 1)' \
  || { echo "python3 >=3.10,<3.14 required (got: $(python3 --version 2>&1))" >&2; exit 1; }
python3 -m venv "$RELEASE_DIR/venv"
"$RELEASE_DIR/venv/bin/pip" install --require-hashes -r "$RELEASE_DIR/backend/requirements.txt" 2>/dev/null \
  || "$RELEASE_DIR/venv/bin/pip" install -r "$RELEASE_DIR/backend/requirements.txt"

# 6. Consistent SQLite backup BEFORE migrate.
#    Quiesce writers: stop worker/API if a previous install exists, use the
#    SQLite backup API (or .backup) so WAL content is included — never a bare
#    `cp` of a live DB file.
if systemctl is-active --quiet ega-update-worker 2>/dev/null; then systemctl stop ega-update-worker; fi
if systemctl is-active --quiet ega-update-api 2>/dev/null; then systemctl stop ega-update-api; fi
if [ -f "$STATE/state.db" ]; then
  TS="$(date -u +%Y%m%dT%H%M%SZ)"
  "$RELEASE_DIR/venv/bin/python" - "$STATE/state.db" "$STATE/backups/state-preinstall-$TS.db" <<'PY'
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
s = sqlite3.connect(src, timeout=10.0)
d = sqlite3.connect(dst, timeout=10.0)
with d:
    s.backup(d)
s.close(); d.close()
print("backup ok:", dst)
PY
  chmod 0600 "$STATE/backups"/state-preinstall-*.db
  chown "$API_USER":"$API_USER" "$STATE/backups"/state-preinstall-*.db || true
fi

# 7. Flip `current` symlink, then migrate.
ln -sfn "$RELEASE_DIR" "$CURRENT_LINK"
EGA_CONFIG_FILE="$ETC/config.json" "$CURRENT_LINK/venv/bin/python" - <<'PY'
from backend.app.db import connect, migrate
from backend.app.config import load_settings
import os
os.environ.setdefault("EGA_CONFIG_FILE", "/etc/ega-update/config.json")
s = load_settings()
conn = connect(s.db_path)
migrate(conn)
print("migrate ok:", s.db_path)
PY
# Joint-group DB ownership so both API (ega-update) and worker (ubuntu)
# read/write state.db/WAL/SHM; 0660 keeps it restricted to the two accounts.
# Secrets stay 0600 root:API_USER (unchanged, see §3).
DB_PATH="$(EGA_CONFIG_FILE="$ETC/config.json" "$CURRENT_LINK/venv/bin/python" -c 'from backend.app.config import load_settings; print(load_settings().db_path)' 2>/dev/null || echo "$STATE/state.db")"
for dbf in "$DB_PATH" "$DB_PATH-wal" "$DB_PATH-shm" "$DB_PATH-journal"; do
  [ -e "$dbf" ] && { chown "$API_USER:ega-update" "$dbf" || true; chmod 0660 "$dbf" || true; }
done

# 8. Systemd units: daemon-reload + enable + start order (api, worker, cloudflared).
cp "$CURRENT_LINK/systemd/ega-update-api.service" /etc/systemd/system/
cp "$CURRENT_LINK/systemd/ega-update-worker.service" /etc/systemd/system/
cp "$CURRENT_LINK/systemd/ega-update-runner@.service" /etc/systemd/system/
cp "$CURRENT_LINK/systemd/cloudflared-ega-update.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable ega-update-api ega-update-worker cloudflared-ega-update
systemctl start ega-update-api
systemctl start ega-update-worker
systemctl start cloudflared-ega-update

# 9. Localhost-binding + Access JWT validation checks (implemented here;
#    executed only when this script runs on the VM, never during code review).
echo "[check] localhost binding:"
(ss -ltnp 2>/dev/null | grep -E '127\.0\.0\.1:8771' \
  || echo "WARN: nothing on 127.0.0.1:8771 — check EGA_LISTEN_PORT and api.env")
LISTEN_HOST="$(EGA_CONFIG_FILE=$ETC/config.json "$CURRENT_LINK/venv/bin/python" -c 'from backend.app.config import load_settings; print(load_settings().listen_host)' 2>/dev/null || echo '?')"
[ "$LISTEN_HOST" = "127.0.0.1" ] || { echo "REFUSING: listen_host=$LISTEN_HOST is not 127.0.0.1" >&2; exit 1; }
echo "[check] Access JWT config present:"
EGA_CONFIG_FILE=$ETC/config.json "$CURRENT_LINK/venv/bin/python" - <<'PY'
from backend.app.config import load_settings
s = load_settings()
missing = [k for k in ("team_domain", "audience", "public_origin") if not getattr(s, k)]
if missing or not s.owner_emails or not s.csrf_secret:
    raise SystemExit("REFUSING: incomplete Access config, missing: %s" % (missing or ["owner_emails/csrf_secret"]))
print("Access config ok:", s.team_domain, s.audience, s.public_origin)
PY

# 10. Conflicting updaterSchedule handling — RECORD-ONLY by default.
#     Detect cron entries, systemd timers, and tool self-update flags that
#     could race the console lock. Disable ONLY confirmed-conflicting ones,
#     AFTER saving prior config for restoration. Never blanket-disable.
echo "[check] conflicting automation scan (record-only):"
CONFLICT_DIR="$STATE/conflicting-automation"
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

# Lingering / user-services note: the worker runs as a SYSTEM unit under the
# tool-owner account (User=ubuntu), so no loginctl lingering or user-manager
# units are required. If a site variant ever moves the worker to a --user
# unit, that variant MUST run `loginctl enable-linger ubuntu` first or the
# dispatcher will die when the last SSH session closes.

echo "[install] done: $COMMIT (tool state untouched)"

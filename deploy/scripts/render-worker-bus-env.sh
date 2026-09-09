#!/usr/bin/env bash
# Render the Update-OPS worker user-bus drop-in.
#
# System units with User= inherit no XDG_RUNTIME_DIR /
# DBUS_SESSION_BUS_ADDRESS, yet every user-scope proof and every
# --user launch (systemd-run probes/runners, systemctl --user
# queries/kills, phase-scope gates) requires them. This script derives
# the tool owner's UID and renders a deterministic drop-in so the
# installed worker unit carries an explicit bus address. The generic
# worker template stays UID-free; worker.env may still override.
#
# Usage: render-worker-bus-env.sh <dest-file> <tool-owner>
# Fails closed (nonzero, no file written) on unresolvable owner/UID.
set -uo pipefail

DEST="${1:-}"
OWNER="${2:-}"

if [ -z "$DEST" ] || [ -z "$OWNER" ]; then
  echo "usage: render-worker-bus-env.sh <dest-file> <tool-owner>" >&2
  exit 2
fi

TOOL_UID="$(id -u "$OWNER" 2>/dev/null)" || {
  echo "render-worker-bus-env: tool owner unresolvable: $OWNER" >&2
  exit 1
}
case "$TOOL_UID" in
  ''|*[!0-9]*)
    echo "render-worker-bus-env: invalid uid for $OWNER: $TOOL_UID" >&2
    exit 1
    ;;
esac

TMP_DEST="$(mktemp "$(dirname "$DEST")/.bus-env-XXXXXX")" || {
  echo "render-worker-bus-env: cannot stage $DEST" >&2
  exit 1
}
cleanup_tmp() { rm -f "$TMP_DEST"; }
trap cleanup_tmp EXIT
printf '[Service]\nEnvironment=XDG_RUNTIME_DIR=/run/user/%s\nEnvironment=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/%s/bus\n' \
  "$TOOL_UID" "$TOOL_UID" > "$TMP_DEST" || exit 1
chmod 0644 "$TMP_DEST" || exit 1
mv "$TMP_DEST" "$DEST" || exit 1
trap - EXIT
echo "rendered $DEST for $OWNER (uid $TOOL_UID)"

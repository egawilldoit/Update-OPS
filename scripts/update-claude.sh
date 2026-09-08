#!/usr/bin/env bash
# update-claude.sh — thin wrapper: Claude (disabled read-only inventory).
# Only inspect is meaningful; plan/apply/verify report BLOCKED_INSTALL_OWNERSHIP.
# usage: update-claude.sh <inspect|plan|apply|verify>
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
. "${SCRIPT_DIR}/common.sh"
if ! common_reject_banned_args "$@"; then
    exit 2
fi
exec "${SCRIPT_DIR}/agent-update" "${1:-}" claude "${@:2}"

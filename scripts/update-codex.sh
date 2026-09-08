#!/usr/bin/env bash
# update-codex.sh — thin wrapper: Codex via the agent-update dispatcher.
# usage: update-codex.sh <inspect|plan|apply|verify> [--plan-id <id> --job-id <uuid>]
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
. "${SCRIPT_DIR}/common.sh"
if ! common_reject_banned_args "$@"; then
    exit 2
fi
exec "${SCRIPT_DIR}/agent-update" "${1:-}" codex "${@:2}"

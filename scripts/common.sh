#!/usr/bin/env bash
# common.sh — shared strict-mode and discovery-guard helpers for Update-OPS
# per-tool update scripts (subagent B owned).
#
# Sourced by scripts/agent-update and scripts/update-<tool>.sh only.
# Sets strict mode, resolves the repo root, validates tool ids, rejects
# banned bulk/force arguments, and guards locking expectations:
#   - read-only paths (inspect/plan/verify) never take the exclusive lock;
#   - mutating paths (apply) require locking support or block.
set -euo pipefail

COMMON_SH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${COMMON_SH_DIR}/.." && pwd)"

VALID_TOOLS="hermes opencode codex t3 claude"
FOUR_TOOLS="hermes opencode codex t3"

common_is_valid_tool() {
    local tool="${1:-}"
    local candidate
    for candidate in $VALID_TOOLS; do
        if [ "$candidate" = "$tool" ]; then
            return 0
        fi
    done
    return 1
}

common_is_four_tool() {
    local tool="${1:-}"
    local candidate
    for candidate in $FOUR_TOOLS; do
        if [ "$candidate" = "$tool" ]; then
            return 0
        fi
    done
    return 1
}

# Reject bulk/force/generic-command arguments (never update-all in V1).
common_reject_banned_args() {
    local arg
    for arg in "$@"; do
        case "$arg" in
            all|force|command|--force|--all|--command|-a|-f|update-all|update_all)
                echo "agent-update: rejected banned argument '$arg'" >&2
                return 2
                ;;
        esac
    done
    return 0
}

# Mutating paths require locking support; missing flock blocks mutation.
common_require_flock() {
    if ! command -v flock >/dev/null 2>&1; then
        if ! python3 -c 'import fcntl' >/dev/null 2>&1; then
            echo "agent-update: locking unavailable (no flock, no fcntl); mutation blocked" >&2
            return 3
        fi
    fi
    return 0
}

common_repo_root() {
    printf '%s' "$REPO_ROOT"
}

common_usage() {
    cat >&2 <<'USAGE'
usage: agent-update <inspect|plan|apply|verify> <tool> [--plan-id <id> --job-id <uuid>]
  tools: hermes opencode codex t3 (claude: disabled read-only inventory)
  inspect/plan/verify are read-only: never install, download, restart, or modify config.
  apply requires a trusted server plan (--plan-id) and job (--job-id); rejects stale
  plans, changed fingerprints, unvalidated targets, unknown flags, concurrent jobs.
USAGE
}

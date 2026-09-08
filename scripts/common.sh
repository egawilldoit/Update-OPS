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
# Positive-form checks only (no negated-condition status capture).
common_require_flock() {
    if command -v flock >/dev/null 2>&1; then
        return 0
    fi
    if python3 -c 'import fcntl' >/dev/null 2>&1; then
        return 0
    fi
    echo "agent-update: locking unavailable (no flock, no fcntl); mutation blocked" >&2
    return 3
}

common_repo_root() {
    printf '%s' "$REPO_ROOT"
}

# Release resolution (UI/DEPLOY owned, R12/R13): script dir -> release root.
# Deployed layouts run from $RELEASE/scripts (RELEASE = /opt/ega-update/
# releases/<commit> or the /opt/ega-update/current pointer); checkouts run
# from <repo>/scripts (RELEASE = repo root). The pointer wins when present
# so wrappers always exec the staged release venv, never a CWD python.
common_release_root() {
    if [ -L "/opt/ega-update/current" ]; then
        readlink -f "/opt/ega-update/current"
        return 0
    fi
    if [ -d "/opt/ega-update/current/scripts" ]; then
        printf '%s' "/opt/ega-update/current"
        return 0
    fi
    if [ -d "/opt/ega-update/current" ]; then
        printf '%s' "/opt/ega-update/current"
        return 0
    fi
    printf '%s' "$REPO_ROOT"
}

# Release venv python for thin wrappers (R12): $RELEASE/venv/bin/python.
common_release_python() {
    local release="${1:-}"
    if [ -z "$release" ]; then
        release="$(common_release_root)"
    fi
    printf '%s' "$release/venv/bin/python"
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

#!/usr/bin/env bash
# update-claude.sh — thin wrapper: Claude (disabled read-only inventory) via backend.app.cli.
# Only inspect is meaningful; plan/apply/verify report BLOCKED_INSTALL_OWNERSHIP via the CLI.
# usage: update-claude.sh <inspect|plan|apply|verify>
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
. "${SCRIPT_DIR}/common.sh"
ACTION="${1:-}"
if [ "$#" -ge 1 ]; then
    shift 1
fi
if [ -z "$ACTION" ]; then
    echo "update-claude.sh: missing action <inspect|plan|apply|verify>" >&2
    exit 2
fi
case "$ACTION" in
    inspect|plan|apply|verify) ;;
    *)
        echo "update-claude.sh: unknown action '$ACTION'" >&2
        exit 2
        ;;
esac
if common_reject_banned_args "$ACTION" "$@"; then
    :
else
    exit 2
fi
PLAN_ID=""
JOB_ID=""
while [ "$#" -gt 0 ]; do
    case "${1:-}" in
        --plan-id)
            if [ "$#" -lt 2 ] || [ -z "${2:-}" ]; then
                echo "update-claude.sh: --plan-id requires a value" >&2
                exit 2
            fi
            PLAN_ID="$2"
            shift 2
            ;;
        --job-id)
            if [ "$#" -lt 2 ] || [ -z "${2:-}" ]; then
                echo "update-claude.sh: --job-id requires a value" >&2
                exit 2
            fi
            JOB_ID="$2"
            shift 2
            ;;
        --*)
            echo "update-claude.sh: unknown flag '$1'" >&2
            exit 2
            ;;
        *)
            echo "update-claude.sh: unexpected argument '$1'" >&2
            exit 2
            ;;
    esac
done
case "$PLAN_ID$JOB_ID" in
    *"/"*|*"\"*|*\.\.*)
        echo "update-claude.sh: IDs must not contain path characters" >&2
        exit 2
        ;;
esac
RELEASE="$(common_release_root)"
CLI_PY="$(common_release_python "$RELEASE")"
CLI_ARGS=()
if [ -n "$PLAN_ID" ]; then
    CLI_ARGS+=("--plan-id" "$PLAN_ID")
fi
if [ -n "$JOB_ID" ]; then
    CLI_ARGS+=("--job-id" "$JOB_ID")
fi
exec "$CLI_PY" -m backend.app.cli "$ACTION" --tool claude "${CLI_ARGS[@]}"

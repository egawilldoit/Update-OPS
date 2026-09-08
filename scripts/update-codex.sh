#!/usr/bin/env bash
# update-codex.sh — thin wrapper: Codex via backend.app.cli (UI/DEPLOY owned).
# usage: update-codex.sh <inspect|plan|apply|verify> [--plan-id <id> --job-id <uuid> [--ack|--no-ack] [--idempotency-key K] [--wait-secs N]]
# Thin wrapper only (R12/R13): resolves the release root and execs the
# release venv CLI so exits 0/2/3/4/5/6 propagate bit-identically.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
. "${SCRIPT_DIR}/common.sh"
ACTION="${1:-}"
if [ "$#" -ge 1 ]; then
    shift 1
fi
if [ -z "$ACTION" ]; then
    echo "update-codex.sh: missing action <inspect|plan|apply|verify>" >&2
    exit 2
fi
case "$ACTION" in
    inspect|plan|apply|verify) ;;
    *)
        echo "update-codex.sh: unknown action '$ACTION'" >&2
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
WANT_ACK=""
IDEM_KEY=""
WAIT_SECS=""
while [ "$#" -gt 0 ]; do
    case "${1:-}" in
        --plan-id)
            if [ "$#" -lt 2 ] || [ -z "${2:-}" ]; then
                echo "update-codex.sh: --plan-id requires a value" >&2
                exit 2
            fi
            PLAN_ID="$2"
            shift 2
            ;;
        --job-id)
            if [ "$#" -lt 2 ] || [ -z "${2:-}" ]; then
                echo "update-codex.sh: --job-id requires a value" >&2
                exit 2
            fi
            JOB_ID="$2"
            shift 2
            ;;
        --ack)
            WANT_ACK="--ack"
            shift 1
            ;;
        --no-ack)
            WANT_ACK="--no-ack"
            shift 1
            ;;
        --idempotency-key)
            if [ "$#" -lt 2 ] || [ -z "${2:-}" ]; then
                echo "update-codex.sh: --idempotency-key requires a value" >&2
                exit 2
            fi
            IDEM_KEY="$2"
            shift 2
            ;;
        --wait-secs)
            if [ "$#" -lt 2 ] || [ -z "${2:-}" ]; then
                echo "update-codex.sh: --wait-secs requires a value" >&2
                exit 2
            fi
            WAIT_SECS="$2"
            shift 2
            ;;
        --*)
            echo "update-codex.sh: unknown flag '$1'" >&2
            exit 2
            ;;
        *)
            echo "update-codex.sh: unexpected argument '$1'" >&2
            exit 2
            ;;
    esac
done
case "$PLAN_ID$JOB_ID$IDEM_KEY" in
    *"/"*|*"\"*|*\.\.*)
        echo "update-codex.sh: IDs must not contain path characters" >&2
        exit 2
        ;;
esac
case "$ACTION" in
    inspect|plan|verify)
        if [ -n "$PLAN_ID" ] || [ -n "$JOB_ID" ]; then
            echo "update-codex.sh: $ACTION takes no --plan-id/--job-id" >&2
            exit 2
        fi
        ;;
    apply)
        if [ -z "$PLAN_ID" ] || [ -z "$JOB_ID" ]; then
            echo "update-codex.sh: apply requires --plan-id <id> --job-id <uuid>" >&2
            exit 2
        fi
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
if [ -n "$WANT_ACK" ]; then
    CLI_ARGS+=("$WANT_ACK")
fi
if [ -n "$IDEM_KEY" ]; then
    CLI_ARGS+=("--idempotency-key" "$IDEM_KEY")
fi
if [ -n "$WAIT_SECS" ]; then
    CLI_ARGS+=("--wait-secs" "$WAIT_SECS")
fi
exec "$CLI_PY" -m backend.app.cli "$ACTION" --tool codex "${CLI_ARGS[@]}"

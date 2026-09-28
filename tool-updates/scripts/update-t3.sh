#!/usr/bin/env bash

set -Eeuo pipefail
umask 077

# Source-only helper: Update-OPS owns backup retention; T3 owns its runtime,
# service-state, and native update lifecycle.
# shellcheck source=/home/ubuntu/tool-updates/scripts/update-ops-backup-retention.sh
source /home/ubuntu/tool-updates/scripts/update-ops-backup-retention.sh

readonly USER_NAME="ubuntu"
readonly T3_HOME="/home/ubuntu/.t3"
readonly T3_BIN="/home/ubuntu/.local/bin/t3"
readonly VERSIONS_DIR="${T3_HOME}/runtime/versions"
readonly STATE_FILE="${T3_HOME}/runtime/service-state.json"
readonly DB_FILE="${T3_HOME}/userdata/state.sqlite"
readonly OPENCODE_CACHE_FILE="${T3_HOME}/caches/opencode.json"
readonly SERVICE="t3code.service"
readonly UNIT_FILE="/home/ubuntu/.config/systemd/user/t3code.service"
readonly HEALTH_URL="http://127.0.0.1:3773/"
readonly OPENCODE_BIN="/home/ubuntu/.opencode/bin/opencode"
readonly STATE_ROOT="/home/ubuntu/.local/state/tool-updates"
readonly BACKUP_ROOT="${STATE_ROOT}/backups/t3"
readonly T3_RETENTION_STATE_DIR="${STATE_ROOT}/t3"
readonly ROLLBACK_METADATA="${T3_RETENTION_STATE_DIR}/rollback-version"
readonly LOG_DIR="${STATE_ROOT}/logs/t3"
readonly RUN_ROOT="${STATE_ROOT}/runs/t3"
readonly LOCK_FILE="${STATE_ROOT}/locks/t3.lock"
readonly START_TIMEOUT="${START_TIMEOUT:-90}"
readonly PROVIDER_WARMUP_SECONDS=60
readonly PROVIDER_REFRESH_DELAY=15
readonly PROVIDER_RETRY_INTERVAL=5

MODE="${1:-update}"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RUN_DIR="${RUN_ROOT}/${TIMESTAMP}-$$"
LOG_FILE="${LOG_DIR}/${TIMESTAMP}-$$.log"
BACKUP_DIR="${BACKUP_ROOT}/${TIMESTAMP}"

BEFORE_VERSION=""
AFTER_VERSION=""
BEFORE_CLI_VERSION=""
BEFORE_STATE_VERSION=""
BEFORE_LAUNCHER_TARGET=""
ACTIVE_VERSION=""
BEFORE_EXECSTART=""
BEFORE_SERVICE_STATE=""
BEFORE_HTTP_CODE=""
BEFORE_MAIN_PID=""
BEFORE_MAIN_EXECUTABLE=""
BEFORE_PROCESS_PATHS=""
BEFORE_RUNTIME_LIST=""
AFTER_LAUNCHER_TARGET=""
AFTER_STATE_VERSION=""
AFTER_EXECSTART=""
AFTER_SERVICE_STATE=""
AFTER_HTTP_CODE=""
AFTER_MAIN_PID=""
AFTER_MAIN_EXECUTABLE=""
PROVIDER_HEALTH="unknown"
PROVIDER_CACHE_STATUS="unknown"
PROVIDER_CACHE_MESSAGE=""
PROVIDER_REFRESH_ATTEMPTED=0
PROVIDER_REFRESH_RESULT="not-attempted"
PROVIDER_CLI_STATUS="not-checked"
PROVIDER_CLI_VERSION=""
PREFLIGHT_CLI_VERSION=""
T3_LAUNCHER_TARGET=""
UPDATE_ATTEMPTED=0
RECOVERY_ATTEMPTED=0
RECOVERY_TARGET_VERSION=""
RECOVERY_TARGET_RUNTIME=""
VALIDATION_PASSED=0
FAILURE_REPORTED=0
DELETED_COUNT=0
DELETED_RUNTIMES=""
RECLAIMED_BYTES=0
ROLLBACK_VERSION=""
ROLLBACK_SOURCE="none"

log() {
    printf '[%s] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"
}

die() {
    log "ERROR: $*"
    if [[ ("${UPDATE_ATTEMPTED:-0}" == 1 || "${RECOVERY_ATTEMPTED:-0}" == 1) &&
          "${VALIDATION_PASSED:-0}" != 1 &&
          "${FAILURE_REPORTED:-0}" != 1 ]]; then
        log "cleanup skipped because validation failed"
        report_failure_state || true
        FAILURE_REPORTED=1
    fi
    exit 1
}

usage() {
    cat <<'EOF'
Usage:
  update-t3.sh [update]
  update-t3.sh --check
  update-t3.sh --help

Environment overrides:
  START_TIMEOUT
EOF
}

need_cmd() {
    command -v "$1" >/dev/null 2>&1 || die "Required command not found: $1"
}

setup_user_bus() {
    export HOME="/home/ubuntu"
    export USER="ubuntu"
    export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
    export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=${XDG_RUNTIME_DIR}/bus}"
}

state_active_version() {
    python3 - "$STATE_FILE" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as state_file:
    state = json.load(state_file)

active_version = state.get("activeVersion")
if not isinstance(active_version, str) or not active_version:
    raise SystemExit("activeVersion is missing")

print(active_version)
PY
}

cli_version() {
    local raw version
    raw="$("$T3_BIN" --version 2>&1)" || return 1
    version="$(printf '%s\n' "$raw" | awk '$1 == "t3" && $2 ~ /^v/ { sub(/^v/, "", $2); print $2; exit }')"
    [[ "$version" =~ ^[0-9][0-9A-Za-z._-]*$ ]] || return 1
    printf '%s\n' "$version"
}

launcher_target() {
    readlink -f "$T3_BIN" 2>/dev/null || true
}

path_under_versions() {
    [[ "$1" == "${VERSIONS_DIR}/"* ]]
}

service_execstart() {
    systemctl --user show "$SERVICE" -p ExecStart --value 2>/dev/null || true
}

service_main_pid() {
    systemctl --user show "$SERVICE" -p MainPID --value 2>/dev/null || true
}

service_main_executable() {
    local pid="${1:-}"
    [[ "$pid" =~ ^[1-9][0-9]*$ ]] || return 1
    readlink -f "/proc/${pid}/exe" 2>/dev/null
}

runtime_versions() {
    find "$VERSIONS_DIR" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' \
        2>/dev/null | sort -V
}

process_path_for_pid() {
    local pid="$1" exe cmd cwd
    exe="$(readlink -f "/proc/${pid}/exe" 2>/dev/null || true)"
    cmd="$(tr '\0' ' ' <"/proc/${pid}/cmdline" 2>/dev/null || true)"
    cwd="$(readlink -f "/proc/${pid}/cwd" 2>/dev/null || true)"
    printf 'PID=%s EXE=%s CWD=%s CMD=%s\n' "$pid" "$exe" "$cwd" "$cmd"
}

running_t3_processes() {
    local proc pid exe cmd cwd
    for proc in /proc/[0-9]*; do
        [[ -d "$proc" ]] || continue
        pid="${proc##*/}"
        exe="$(readlink -f "/proc/${pid}/exe" 2>/dev/null || true)"
        cmd="$(tr '\0' ' ' <"/proc/${pid}/cmdline" 2>/dev/null || true)"
        cwd="$(readlink -f "/proc/${pid}/cwd" 2>/dev/null || true)"
        if [[ "$exe" == "${T3_HOME}/runtime/versions/"* ||
              "$cmd" == *"${T3_HOME}/runtime/versions/"* ||
              "$cwd" == "${T3_HOME}/runtime/versions/"* ]]; then
            printf 'PID=%s EXE=%s CWD=%s CMD=%s\n' "$pid" "$exe" "$cwd" "$cmd"
        fi
    done
}

running_process_uses_version() {
    local version="$1" proc pid exe cmd cwd prefix
    prefix="${VERSIONS_DIR}/${version}/"
    for proc in /proc/[0-9]*; do
        [[ -d "$proc" ]] || continue
        pid="${proc##*/}"
        exe="$(readlink -f "/proc/${pid}/exe" 2>/dev/null || true)"
        cmd="$(tr '\0' ' ' <"/proc/${pid}/cmdline" 2>/dev/null || true)"
        cwd="$(readlink -f "/proc/${pid}/cwd" 2>/dev/null || true)"
        if [[ "$exe" == "$prefix"* || "$cmd" == *"$prefix"* || "$cwd" == "$prefix"* ]]; then
            return 0
        fi
    done
    return 1
}

health_code() {
    curl --max-time 10 -sS -o /dev/null -w '%{http_code}' "$HEALTH_URL" 2>/dev/null || true
}

health_pass() {
    [[ "$(health_code)" == "200" ]]
}

opencode_cache_ready() {
    [[ -f "$OPENCODE_CACHE_FILE" ]] || return 1
    jq -e '(.status == "ready") and (.version | type == "string") and (.version | length > 0)' \
        "$OPENCODE_CACHE_FILE" >/dev/null 2>&1
}

opencode_cache_status() {
    [[ -f "$OPENCODE_CACHE_FILE" ]] || {
        printf '%s\n' "missing"
        return 0
    }
    jq -r '.status // "unknown"' "$OPENCODE_CACHE_FILE" 2>/dev/null || printf '%s\n' "unknown"
}

opencode_cache_message() {
    [[ -f "$OPENCODE_CACHE_FILE" ]] || {
        printf '%s\n' "cache missing"
        return 0
    }
    jq -r '.message // ""' "$OPENCODE_CACHE_FILE" 2>/dev/null || true
}

provider_cache_is_warming() {
    local status="$1"
    local message="$2"

    case "$status" in
        missing|unknown|pending|warming|initializing|loading)
            return 0
            ;;
    esac
    [[ "$message" == *"timed out"* ||
       "$message" == *"timeout"* ||
       "$message" == *"version probe"* ]]
}

log_opencode_cache() {
    local snapshot
    if [[ ! -f "$OPENCODE_CACHE_FILE" ]]; then
        log "OpenCode provider cache: missing"
        return 0
    fi
    snapshot="$(jq -r '[.status // "unknown", .version // "unknown", .checkedAt // "unknown", .message // ""] | @tsv' \
        "$OPENCODE_CACHE_FILE" 2>/dev/null || true)"
    log "OpenCode provider cache: ${snapshot:-unreadable}"
}

refresh_opencode_provider() {
    local session_json session_id session_token ticket

    session_json="$("$T3_BIN" auth session issue --ttl 2m --label tool-updates-opencode-refresh --json)" || {
        log "OpenCode refresh: could not issue temporary local session"
        return 1
    }
    session_id="$(printf '%s' "$session_json" | jq -r '.sessionId // empty')"
    session_token="$(printf '%s' "$session_json" | jq -r '.token // empty')"
    if [[ -z "$session_id" || -z "$session_token" ]]; then
        log "OpenCode refresh: temporary session response was invalid"
        return 1
    fi

    ticket="$(curl --max-time 10 -fsS -X POST \
        -H "Authorization: Bearer ${session_token}" \
        "$HEALTH_URL/api/auth/websocket-ticket" | jq -r '.ticket // empty')" || ticket=""

    if [[ -z "$ticket" ]]; then
        log "OpenCode refresh: could not obtain local WebSocket ticket"
        "$T3_BIN" auth session revoke "$session_id" >/dev/null 2>&1 || true
        return 1
    fi

    export T3_WS_TICKET="$ticket"
    if ! node - <<'NODE'
const ticket = process.env.T3_WS_TICKET;
const url = `ws://127.0.0.1:3773/ws?wsTicket=${encodeURIComponent(ticket)}&clientSurface=desktop&clientAppVersion=tool-updates&connectionMethod=direct`;
const ws = new WebSocket(url);
let settled = false;
const finish = (code) => {
  if (settled) return;
  settled = true;
  clearTimeout(timer);
  ws.close();
  process.exit(code);
};
const timer = setTimeout(() => finish(1), 15000);
ws.addEventListener("open", () => {
  ws.send(JSON.stringify({
    _tag: "Request",
    id: 1,
    tag: "server.refreshProviders",
    payload: { refreshModels: false },
    headers: [],
  }));
});
ws.addEventListener("message", (event) => {
  try {
    const message = JSON.parse(String(event.data));
    if (message._tag !== "Exit" || message.requestId !== 1) return;
    finish(message.exit?._tag === "Success" ? 0 : 1);
  } catch {
    finish(1);
  }
});
ws.addEventListener("error", () => finish(1));
ws.addEventListener("close", () => { if (!settled) finish(1); });
NODE
    then
        log "OpenCode refresh RPC completed"
    else
        log "OpenCode refresh RPC failed or timed out"
        "$T3_BIN" auth session revoke "$session_id" >/dev/null 2>&1 || true
        unset T3_WS_TICKET
        return 1
    fi

    "$T3_BIN" auth session revoke "$session_id" >/dev/null 2>&1 || true
    unset T3_WS_TICKET
    opencode_cache_ready
}

ensure_opencode_provider() {
    local deadline refresh_deadline status message

    deadline=$((SECONDS + PROVIDER_WARMUP_SECONDS))
    refresh_deadline=$((SECONDS + PROVIDER_REFRESH_DELAY))
    PROVIDER_HEALTH="warming"
    PROVIDER_CACHE_STATUS="unknown"
    PROVIDER_CACHE_MESSAGE=""
    PROVIDER_REFRESH_ATTEMPTED=0
    PROVIDER_REFRESH_RESULT="not-attempted"
    PROVIDER_CLI_STATUS="not-checked"
    PROVIDER_CLI_VERSION=""

    while (( SECONDS <= deadline )); do
        log_opencode_cache
        status="$(opencode_cache_status)"
        message="$(opencode_cache_message)"
        PROVIDER_CACHE_STATUS="$status"
        PROVIDER_CACHE_MESSAGE="$message"
        if opencode_cache_ready; then
            PROVIDER_HEALTH="healthy"
            log "OpenCode provider readiness: HEALTHY"
            return 0
        fi

        if (( PROVIDER_REFRESH_ATTEMPTED == 0 && SECONDS >= refresh_deadline )); then
            PROVIDER_REFRESH_ATTEMPTED=1
            log "OpenCode provider remains unavailable after initial warm-up; attempting one refresh"
            if refresh_opencode_provider; then
                PROVIDER_REFRESH_RESULT="success"
            else
                PROVIDER_REFRESH_RESULT="failed"
            fi
            continue
        fi

        if provider_cache_is_warming "$status" "$message"; then
            log "OpenCode provider readiness: WARMING status=${status} message=${message:-none}"
        else
            log "OpenCode provider readiness: ${status:-unknown}; waiting for bounded warm-up"
        fi
        (( SECONDS >= deadline )) && break
        sleep "$PROVIDER_RETRY_INTERVAL"
    done

    log "OpenCode provider warm-up window expired after ${PROVIDER_WARMUP_SECONDS}s"
    if [[ ! -x "$OPENCODE_BIN" ]]; then
        PROVIDER_CLI_STATUS="unhealthy"
        PROVIDER_HEALTH="hard-failure"
        log "OpenCode CLI verification: FAIL (missing or non-executable ${OPENCODE_BIN})"
    elif PROVIDER_CLI_VERSION="$(timeout --signal=TERM 10s "$OPENCODE_BIN" --version 2>&1)"; then
        PROVIDER_CLI_STATUS="healthy"
        log "OpenCode CLI verification: PASS (${PROVIDER_CLI_VERSION//$'\n'/ })"
        PROVIDER_HEALTH="degraded"
        log "T3 core update is healthy; OpenCode provider readiness remains degraded."
    else
        PROVIDER_CLI_STATUS="unhealthy"
        PROVIDER_HEALTH="hard-failure"
        log "OpenCode CLI verification: FAIL (version probe did not complete)"
    fi
    return 0
}

pending_update_present() {
    python3 - "$STATE_FILE" <<'PY'
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as state_file:
        state = json.load(state_file)
except (OSError, ValueError):
    raise SystemExit(0)

if not isinstance(state, dict):
    raise SystemExit(0)

update = state.get("update")
status = update.get("status") if isinstance(update, dict) else None
pending = (
    state.get("pending") is True
    or state.get("updatePending") is True
    or state.get("pendingUpdate") is True
    or isinstance(state.get("pendingVersion"), str) and bool(state["pendingVersion"])
    or status in {"pending", "committed", "failed"}
)
raise SystemExit(0 if pending else 1)
PY
}

runtime_target_is_safe() {
    local version="$1" versions_root version_dir target resolved_dir resolved_target
    [[ "$version" =~ ^[0-9][0-9A-Za-z._-]*$ ]] || return 1
    versions_root="$(readlink -f "$VERSIONS_DIR" 2>/dev/null || true)"
    [[ "$versions_root" == "$VERSIONS_DIR" ]] || return 1
    version_dir="${VERSIONS_DIR}/${version}"
    target="${version_dir}/t3"
    [[ -d "$version_dir" && ! -L "$version_dir" && -f "$target" && -x "$target" ]] || return 1
    resolved_dir="$(readlink -f "$version_dir" 2>/dev/null || true)"
    resolved_target="$(readlink -f "$target" 2>/dev/null || true)"
    [[ "$resolved_dir" == "${versions_root}/${version}" &&
       "$resolved_target" == "${resolved_dir}/"* ]]
}

service_execstart_matches() {
    local version="$1" target execstart="${2:-}"
    target="${VERSIONS_DIR}/${version}/t3"
    [[ "$execstart" == *"path=${target} ; argv[]=${target} __service-launcher ;"* ]]
}

no_conflicting_t3_runtime() {
    local version="$1" allowed_root proc pid exe cmd cwd
    allowed_root="${VERSIONS_DIR}/${version}/"
    for proc in /proc/[0-9]*; do
        [[ -d "$proc" ]] || continue
        pid="${proc##*/}"
        exe="$(readlink -f "/proc/${pid}/exe" 2>/dev/null || true)"
        cmd="$(tr '\0' ' ' <"/proc/${pid}/cmdline" 2>/dev/null || true)"
        cwd="$(readlink -f "/proc/${pid}/cwd" 2>/dev/null || true)"
        if [[ "$exe" == "${VERSIONS_DIR}/"* ||
              "$cmd" == *"${VERSIONS_DIR}/"* ||
              "$cwd" == "${VERSIONS_DIR}/"* ]]; then
            if [[ "$exe" == "$allowed_root"* ||
                  "$cmd" == *"$allowed_root"* ||
                  "$cwd" == "$allowed_root"* ]]; then
                continue
            fi
            return 1
        fi
    done
    return 0
}

wait_for_recovered_service() {
    local deadline=$((SECONDS + START_TIMEOUT)) state main_pid main_executable execstart code
    while ((SECONDS < deadline)); do
        state="$(systemctl --user is-active "$SERVICE" 2>&1 || true)"
        main_pid="$(service_main_pid)"
        execstart="$(service_execstart)"
        if [[ "$state" == active && "$main_pid" =~ ^[1-9][0-9]*$ ]]; then
            service_execstart_matches "$RECOVERY_TARGET_VERSION" "$execstart" || return 1
            main_executable="$(service_main_executable "$main_pid" 2>/dev/null || true)"
            if [[ -n "$main_executable" &&
                  "$main_executable" != "${RECOVERY_TARGET_RUNTIME}/t3" ]]; then
                return 1
            fi
            if [[ "$main_executable" == "${RECOVERY_TARGET_RUNTIME}/t3" ]] &&
                no_conflicting_t3_runtime "$RECOVERY_TARGET_VERSION" &&
                [[ "$(health_code)" == 200 ]]; then
                return 0
            fi
        fi
        sleep 1
    done
    return 1
}

preflight() {
    [[ -e "$T3_BIN" ]] || die "Managed T3 launcher is missing: ${T3_BIN}"
    [[ -x "$T3_BIN" ]] || die "Managed T3 launcher is not executable: ${T3_BIN}"
    [[ -f "$STATE_FILE" ]] || die "T3 service state is missing: ${STATE_FILE}"
    [[ -d "$VERSIONS_DIR" ]] || die "T3 runtime directory is missing: ${VERSIONS_DIR}"

    T3_LAUNCHER_TARGET="$(launcher_target)"
    [[ -n "$T3_LAUNCHER_TARGET" &&
       -f "$T3_LAUNCHER_TARGET" &&
       -x "$T3_LAUNCHER_TARGET" ]] ||
        die "Managed T3 launcher target is not a usable executable: ${T3_LAUNCHER_TARGET:-missing}"
    path_under_versions "$T3_LAUNCHER_TARGET" ||
        die "Managed T3 launcher resolves outside ${VERSIONS_DIR}: ${T3_LAUNCHER_TARGET}"

    [[ "$START_TIMEOUT" =~ ^[1-9][0-9]*$ ]] ||
        die "START_TIMEOUT must be a positive integer: ${START_TIMEOUT}"
    if ! PREFLIGHT_CLI_VERSION="$(cli_version)"; then
        die "Managed T3 launcher --version failed: ${T3_BIN}"
    fi
}

collect_current_state() {
    BEFORE_CLI_VERSION="$PREFLIGHT_CLI_VERSION"
    BEFORE_STATE_VERSION="$(state_active_version 2>/dev/null || true)"
    BEFORE_VERSION="$BEFORE_STATE_VERSION"
    BEFORE_LAUNCHER_TARGET="$(launcher_target)"
    BEFORE_EXECSTART="$(service_execstart)"
    BEFORE_SERVICE_STATE="$(systemctl --user is-active "$SERVICE" 2>&1 || true)"
    BEFORE_HTTP_CODE="$(health_code)"
    BEFORE_MAIN_PID="$(service_main_pid)"
    BEFORE_MAIN_EXECUTABLE="$(service_main_executable "$BEFORE_MAIN_PID" 2>/dev/null || true)"
    BEFORE_PROCESS_PATHS="$(running_t3_processes || true)"
    BEFORE_RUNTIME_LIST="$(runtime_versions || true)"
}

log_current_state() {
    log "CLI version: ${BEFORE_CLI_VERSION:-unknown}"
    log "service-state.json activeVersion: ${BEFORE_STATE_VERSION:-unknown}"
    log "Resolved launcher: ${BEFORE_LAUNCHER_TARGET:-unknown}"
    log "Service state: ${BEFORE_SERVICE_STATE:-unknown}"
    log "systemd ExecStart: ${BEFORE_EXECSTART:-unknown}"
    log "MainPID: ${BEFORE_MAIN_PID:-unknown}"
    log "Main executable: ${BEFORE_MAIN_EXECUTABLE:-unknown}"
    log "HTTP health: ${BEFORE_HTTP_CODE:-000}"
    log "Running T3 process paths:"
    if [[ -n "$BEFORE_PROCESS_PATHS" ]]; then
        printf '%s\n' "$BEFORE_PROCESS_PATHS"
    else
        log "  none found"
    fi
    log "Installed runtime versions:"
    if [[ -n "$BEFORE_RUNTIME_LIST" ]]; then
        printf '%s\n' "$BEFORE_RUNTIME_LIST"
    else
        log "  none found"
    fi
}

validate_current_state() {
    local expected mismatch=0

    [[ -n "$BEFORE_CLI_VERSION" ]] || die "Could not read CLI version from ${T3_BIN}"
    [[ -n "$BEFORE_STATE_VERSION" ]] || die "Could not read activeVersion from ${STATE_FILE}"
    if [[ "$BEFORE_CLI_VERSION" != "$BEFORE_STATE_VERSION" ]]; then
        log "Repairable version drift: CLI=${BEFORE_CLI_VERSION} service=${BEFORE_STATE_VERSION}"
        mismatch=1
    fi

    BEFORE_VERSION="$BEFORE_STATE_VERSION"
    expected="${VERSIONS_DIR}/${BEFORE_VERSION}/t3"
    runtime_target_is_safe "$BEFORE_VERSION" ||
        die "Active service runtime is not a usable executable: ${expected}"
    if [[ "$BEFORE_LAUNCHER_TARGET" != "$expected" ]]; then
        log "Repairable launcher drift: ${BEFORE_LAUNCHER_TARGET:-unknown} -> ${expected}"
    fi
    if [[ "$BEFORE_SERVICE_STATE" != active ]]; then
        RECOVERY_ATTEMPTED=1
        RECOVERY_TARGET_VERSION="$BEFORE_STATE_VERSION"
        RECOVERY_TARGET_RUNTIME="${VERSIONS_DIR}/${RECOVERY_TARGET_VERSION}"
        [[ "$BEFORE_SERVICE_STATE" == inactive ]] ||
            die "T3 service state ${BEFORE_SERVICE_STATE:-unknown} is not eligible for automatic recovery"
        (( mismatch == 1 )) ||
            die "T3 service is inactive without repairable CLI/service-state version drift"
        [[ "$BEFORE_MAIN_PID" == 0 ]] ||
            die "Inactive T3 service reports nonzero MainPID: ${BEFORE_MAIN_PID:-unknown}"
        service_execstart_matches "$RECOVERY_TARGET_VERSION" "$BEFORE_EXECSTART" ||
            die "Inactive T3 systemd ExecStart does not exactly target ${expected}: ${BEFORE_EXECSTART:-unknown}"
        if pending_update_present; then
            die "A failed or pending native T3 update marker remains in service-state.json"
        fi
        if [[ -n "$BEFORE_PROCESS_PATHS" ]]; then
            die "Inactive T3 service has a live T3 runtime process; refusing automatic start"
        fi
        log "Repairable inactive T3 service detected"
        log "Recovery target version: ${RECOVERY_TARGET_VERSION}"
        log "Recovery target runtime: ${RECOVERY_TARGET_RUNTIME}"
        log "Automatic recovery is safe: CLI version differs from valid service-state activeVersion; the target runtime and executable resolve inside the canonical runtime root; ExecStart names the same target; MainPID is zero; no pending/failed update marker or live T3 process exists."
        if ! systemctl --user start "$SERVICE"; then
            die "Could not start ${SERVICE} for validated repairable-state recovery"
        fi
        if ! wait_for_recovered_service; then
            die "Recovered T3 service did not pass bounded process, ExecStart, conflict, and HTTP health verification"
        fi
        log "Automatic T3 service recovery verified; continuing native reconciliation"
        collect_current_state
        log_current_state
    fi

    [[ "$BEFORE_SERVICE_STATE" == active ]] ||
        die "T3 service is not active before update: ${BEFORE_SERVICE_STATE:-unknown}"
    [[ "$BEFORE_HTTP_CODE" == 200 ]] ||
        die "T3 health endpoint returned HTTP ${BEFORE_HTTP_CODE:-000} before update"
    service_execstart_matches "$BEFORE_STATE_VERSION" "$BEFORE_EXECSTART" ||
        die "systemd ExecStart does not reference active runtime ${BEFORE_VERSION}: ${BEFORE_EXECSTART:-unknown}"
    [[ "$BEFORE_MAIN_EXECUTABLE" == "$expected" ]] ||
        die "T3 MainPID executable does not reference active runtime: ${BEFORE_MAIN_EXECUTABLE:-unknown}"
    running_process_uses_version "$BEFORE_VERSION" ||
        die "No running T3 process references active runtime ${BEFORE_VERSION}"
    if pending_update_present; then
        die "A failed or pending native T3 update marker remains in service-state.json"
    fi
    no_conflicting_t3_runtime "$BEFORE_STATE_VERSION" ||
        die "A live T3 process is running from a conflicting runtime"
}

record_before_state() {
    collect_current_state
    log_current_state
    validate_current_state
}

report_failure_state() {
    local cli state launcher service_state execstart http_code main_pid main_executable processes
    cli="$(cli_version 2>/dev/null || true)"
    state="$(state_active_version 2>/dev/null || true)"
    launcher="$(launcher_target)"
    service_state="$(systemctl --user is-active "$SERVICE" 2>&1 || true)"
    execstart="$(service_execstart)"
    http_code="$(health_code)"
    main_pid="$(service_main_pid)"
    main_executable="$(service_main_executable "$main_pid" 2>/dev/null || true)"
    processes="$(running_t3_processes || true)"
    log "Failure state: CLI=${cli:-unknown} activeVersion=${state:-unknown} launcher=${launcher:-unknown} service=${service_state:-unknown} ExecStart=${execstart:-unknown} MainPID=${main_pid:-unknown} MainExe=${main_executable:-unknown} HTTP=${http_code:-000}"
    if [[ "${RECOVERY_ATTEMPTED:-0}" == 1 ]]; then
        log "Recovery target: version=${RECOVERY_TARGET_VERSION:-unknown} runtime=${RECOVERY_TARGET_RUNTIME:-unknown}"
        log "Recovery diagnostics: service-state activeVersion=${state:-unknown}; CLI version=${cli:-unknown}; launcher=${launcher:-unknown}; ExecStart=${execstart:-unknown}; MainPID=${main_pid:-unknown}; HTTP=${http_code:-000}"
        systemctl --user status "$SERVICE" --no-pager -l || true
        journalctl --user -u "$SERVICE" -n 40 --no-pager || true
    fi
    if [[ -n "$BEFORE_VERSION" &&
          "$cli" == "$BEFORE_VERSION" &&
          "$state" == "$BEFORE_VERSION" &&
          "$launcher" == "${VERSIONS_DIR}/${BEFORE_VERSION}/t3" &&
          "$service_state" == active &&
          "$execstart" == *"${VERSIONS_DIR}/${BEFORE_VERSION}/t3"* &&
          "$main_executable" == "${VERSIONS_DIR}/${BEFORE_VERSION}/t3" &&
          "$http_code" == 200 ]]; then
        log "Previous T3 runtime remains healthy: ${BEFORE_VERSION}"
    fi
    log "Failure running T3 process paths:"
    if [[ -n "$processes" ]]; then
        printf '%s\n' "$processes"
    else
        log "  none found"
    fi
}

backup_t3_state() {
    mkdir -p "$BACKUP_DIR"
    chmod 700 "$BACKUP_DIR"
    log "Creating restricted backup: ${BACKUP_DIR}"

    if [[ -f "$DB_FILE" ]]; then
        if command -v sqlite3 >/dev/null 2>&1; then
            sqlite3 "$DB_FILE" ".timeout 10000" \
                ".backup '${BACKUP_DIR}/state.sqlite'"
            local quick_check
            quick_check="$(sqlite3 "${BACKUP_DIR}/state.sqlite" 'PRAGMA quick_check;' 2>&1)"
            [[ "$quick_check" == "ok" ]] ||
                die "T3 SQLite backup quick_check failed: ${quick_check}"
            log "SQLite backup verified with PRAGMA quick_check"
        else
            log "sqlite3 is unavailable; copying quiescent database"
            cp -a "$DB_FILE" "${BACKUP_DIR}/state.sqlite"
        fi
        for sidecar in "${DB_FILE}-wal" "${DB_FILE}-shm"; do
            [[ -e "$sidecar" ]] && cp -a "$sidecar" "${BACKUP_DIR}/"
        done
    fi

    [[ -f "$STATE_FILE" ]] && cp -a "$STATE_FILE" "${BACKUP_DIR}/service-state.json"
    [[ -f "$UNIT_FILE" ]] && cp -a "$UNIT_FILE" "${BACKUP_DIR}/t3code.service"
    {
        printf 'created_at=%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
        printf 'old_version=%s\n' "$BEFORE_VERSION"
        printf 'cli_version=%s\n' "$BEFORE_CLI_VERSION"
        printf 'launcher_target=%s\n' "$BEFORE_LAUNCHER_TARGET"
        printf 'update_command=%s\n' "$T3_BIN update --channel nightly --yes"
        printf 'service_state=%s\n' "$BEFORE_SERVICE_STATE"
        printf 'execstart=%s\n' "$BEFORE_EXECSTART"
    } >"${BACKUP_DIR}/metadata"
    chmod -R go-rwx "$BACKUP_DIR"
    update_ops_mark_directory_verified "$BACKUP_DIR" t3 "${TIMESTAMP}-$$" ||
        die "Could not mark validated T3 backup"
    log "Backup complete"
}

verify_runtime_once() {
    local expected cli_version_now state_version service_state execstart code main_pid main_executable

    cli_version_now="$(cli_version 2>/dev/null || true)"
    [[ -n "$cli_version_now" ]] || return 1
    AFTER_VERSION="$cli_version_now"
    expected="${VERSIONS_DIR}/${AFTER_VERSION}/t3"

    AFTER_LAUNCHER_TARGET="$(launcher_target)"
    AFTER_STATE_VERSION="$(state_active_version 2>/dev/null || true)"
    AFTER_SERVICE_STATE="$(systemctl --user is-active "$SERVICE" 2>/dev/null || true)"
    AFTER_EXECSTART="$(service_execstart)"
    AFTER_MAIN_PID="$(service_main_pid)"
    AFTER_MAIN_EXECUTABLE="$(service_main_executable "$AFTER_MAIN_PID" 2>/dev/null || true)"
    AFTER_HTTP_CODE="$(health_code)"

    [[ -f "$expected" && -x "$expected" ]] || return 1
    [[ "$AFTER_LAUNCHER_TARGET" == "$expected" ]] || return 1
    [[ "$AFTER_STATE_VERSION" == "$AFTER_VERSION" ]] || return 1
    [[ "$AFTER_SERVICE_STATE" == active ]] || return 1
    [[ "$AFTER_EXECSTART" == *"$expected"* ]] || return 1
    [[ "$AFTER_MAIN_PID" =~ ^[1-9][0-9]*$ ]] || return 1
    [[ "$AFTER_MAIN_EXECUTABLE" == "$expected" ]] || return 1
    running_process_uses_version "$AFTER_VERSION" || return 1
    [[ "$AFTER_HTTP_CODE" == 200 ]] || return 1
    if pending_update_present; then
        return 1
    fi

    state_version="$AFTER_STATE_VERSION"
    service_state="$AFTER_SERVICE_STATE"
    execstart="$AFTER_EXECSTART"
    code="$AFTER_HTTP_CODE"
    main_pid="$AFTER_MAIN_PID"
    main_executable="$AFTER_MAIN_EXECUTABLE"
    log "Verification: CLI=${AFTER_VERSION} launcher=${AFTER_LAUNCHER_TARGET} state=${state_version} service=${service_state} ExecStart=${execstart} MainPID=${main_pid} MainExe=${main_executable} health=HTTP ${code} pending=none"
    return 0
}

verify_runtime() {
    local deadline
    deadline=$((SECONDS + START_TIMEOUT))
    while (( SECONDS <= deadline )); do
        if verify_runtime_once; then
            ACTIVE_VERSION="$AFTER_VERSION"
            return 0
        fi
        (( SECONDS >= deadline )) && break
        sleep 2
    done
    return 1
}

runtime_size() {
    local dir="$1" bytes
    bytes="$(du -sb "$dir" 2>/dev/null | awk '{print $1}')"
    printf '%s\n' "${bytes:-0}"
}

runtime_directory_is_valid() {
    local version="$1"
    [[ "$version" =~ ^[0-9][0-9A-Za-z._-]*$ ]] || return 1
    [[ -d "${VERSIONS_DIR}/${version}" && ! -L "${VERSIONS_DIR}/${version}" ]] || return 1
    [[ -x "${VERSIONS_DIR}/${version}/t3" ]]
}

recorded_rollback_version() {
    local candidate=""
    [[ -f "$ROLLBACK_METADATA" && ! -L "$ROLLBACK_METADATA" ]] || return 1
    IFS= read -r candidate <"$ROLLBACK_METADATA" || true
    runtime_directory_is_valid "$candidate" || return 1
    [[ "$candidate" != "$ACTIVE_VERSION" ]] || return 1
    printf '%s\n' "$candidate"
}

select_rollback_runtime() {
    local candidate=""
    local -a candidates=()

    ROLLBACK_VERSION=""
    ROLLBACK_SOURCE="none"

    if [[ "$BEFORE_VERSION" != "$AFTER_VERSION" ]] &&
        runtime_directory_is_valid "$BEFORE_VERSION"; then
        ROLLBACK_VERSION="$BEFORE_VERSION"
        ROLLBACK_SOURCE="pre-update"
    else
        candidate="$(recorded_rollback_version 2>/dev/null || true)"
        if [[ -n "$candidate" && "$candidate" != "$ACTIVE_VERSION" ]]; then
            ROLLBACK_VERSION="$candidate"
            ROLLBACK_SOURCE="metadata"
        elif [[ "$BEFORE_VERSION" == "$AFTER_VERSION" ]]; then
            mapfile -t candidates < <(
                runtime_versions |
                    sort -Vr
            )
            for candidate in "${candidates[@]}"; do
                [[ "$candidate" == "$ACTIVE_VERSION" ]] && continue
                if runtime_directory_is_valid "$candidate"; then
                    ROLLBACK_VERSION="$candidate"
                    ROLLBACK_SOURCE="existing-unrecorded"
                    log "Retaining existing T3 runtime ${candidate} as the newest non-active candidate; it is not labeled known-good without metadata"
                    break
                fi
            done
        fi
    fi

    if [[ -z "$ROLLBACK_VERSION" ]]; then
        log "No local T3 rollback runtime currently available; next successful version transition will retain the current active runtime."
    else
        log "Selected T3 rollback runtime: ${ROLLBACK_VERSION} (source=${ROLLBACK_SOURCE})"
    fi
}

record_successful_rollback() {
    local temporary=""

    [[ "$BEFORE_VERSION" != "$AFTER_VERSION" ]] || return 0
    [[ "$ROLLBACK_SOURCE" == pre-update || "$ROLLBACK_SOURCE" == metadata ]] || return 0
    runtime_directory_is_valid "$ROLLBACK_VERSION" || return 0
    [[ "$ROLLBACK_VERSION" != "$ACTIVE_VERSION" ]] || return 0

    mkdir -p "$T3_RETENTION_STATE_DIR"
    chmod 700 "$T3_RETENTION_STATE_DIR"
    temporary="${ROLLBACK_METADATA}.tmp.$$"
    printf '%s\n' "$ROLLBACK_VERSION" >"$temporary"
    chmod 600 "$temporary"
    mv -f -- "$temporary" "$ROLLBACK_METADATA"
    log "Recorded T3 rollback runtime: ${ROLLBACK_VERSION}"
}

cleanup_old_runtimes() {
    local -a dirs=() remove_dirs=()
    local dir dir_real version size execstart state_version expected launcher main_pid main_executable versions_root_real
    local reclaimed=0

    versions_root_real="$(readlink -f "$VERSIONS_DIR" 2>/dev/null || true)"
    [[ "$versions_root_real" == "$VERSIONS_DIR" ]] ||
        die "Refusing cleanup: runtime versions root resolves outside ${VERSIONS_DIR}"
    state_version="$(state_active_version 2>/dev/null || true)"
    [[ "$state_version" == "$ACTIVE_VERSION" ]] ||
        die "Refusing cleanup: activeVersion=${state_version:-missing}, expected ${ACTIVE_VERSION}"
    expected="${VERSIONS_DIR}/${ACTIVE_VERSION}/t3"
    execstart="$(service_execstart)"
    [[ "$execstart" == *"$expected"* ]] ||
        die "Refusing cleanup: systemd ExecStart does not reference ${ACTIVE_VERSION}"
    launcher="$(launcher_target)"
    [[ "$launcher" == "$expected" ]] ||
        die "Refusing cleanup: launcher does not reference ${ACTIVE_VERSION}"
    main_pid="$(service_main_pid)"
    [[ "$main_pid" =~ ^[1-9][0-9]*$ ]] ||
        die "Refusing cleanup: T3 MainPID is unavailable"
    main_executable="$(service_main_executable "$main_pid" 2>/dev/null || true)"
    [[ "$main_executable" == "$expected" ]] ||
        die "Refusing cleanup: T3 MainPID executable is not ${ACTIVE_VERSION}"

    select_rollback_runtime

    mapfile -d '' -t dirs < <(find "$VERSIONS_DIR" -mindepth 1 -maxdepth 1 -type d -print0)
    for dir in "${dirs[@]}"; do
        [[ "${dir%/*}" == "$VERSIONS_DIR" && ! -L "$dir" ]] ||
            die "Refusing cleanup outside runtime directory: ${dir}"
        dir_real="$(readlink -f "$dir" 2>/dev/null || true)"
        [[ "$dir_real" == "${versions_root_real}/"* && "$dir_real" == "$dir" ]] ||
            die "Refusing cleanup of runtime path outside ${VERSIONS_DIR}: ${dir}"
        version="${dir##*/}"
        [[ "$version" == "$ACTIVE_VERSION" ]] && continue
        [[ "$version" == "$ROLLBACK_VERSION" ]] && continue
        [[ -n "$version" && "$version" != */* ]] ||
            die "Refusing cleanup of invalid runtime path: ${dir}"
        [[ "$version" != "$state_version" ]] ||
            die "Refusing to delete activeVersion ${version}"
        [[ "$execstart" != *"${VERSIONS_DIR}/${version}/"* ]] ||
            die "Refusing to delete ${version}: systemd ExecStart references it"
        [[ "$launcher" != "${VERSIONS_DIR}/${version}/t3" ]] ||
            die "Refusing to delete ${version}: launcher references it"
        [[ "$main_executable" != "${VERSIONS_DIR}/${version}/t3" ]] ||
            die "Refusing to delete ${version}: MainPID executable references it"
        if running_process_uses_version "$version"; then
            die "Refusing to delete ${version}: a running T3 process references it"
        fi
        remove_dirs+=("$dir")
    done

    for dir in "${remove_dirs[@]}"; do
        version="${dir##*/}"
        size="$(runtime_size "$dir")"
        log "Removing old T3 runtime ${version} (${size} bytes)"
        rm -rf -- "$dir"
        reclaimed=$((reclaimed + size))
        DELETED_COUNT=$((DELETED_COUNT + 1))
        if [[ -n "$DELETED_RUNTIMES" ]]; then
            DELETED_RUNTIMES+=" $version"
        else
            DELETED_RUNTIMES="$version"
        fi
    done
    RECLAIMED_BYTES="$reclaimed"
    log "Disk reclaimed: ${reclaimed} bytes"
}

report_final() {
    log "T3 active runtime: ${ACTIVE_VERSION}"
    if [[ "$BEFORE_VERSION" == "$AFTER_VERSION" ]]; then
        log "T3 already latest: ${AFTER_VERSION}"
    else
        log "T3: ${BEFORE_VERSION} -> ${AFTER_VERSION}"
    fi
    log "Service: $(systemctl --user is-active "$SERVICE" 2>/dev/null || true)"
    log "Health: HTTP $(health_code)"
    log "Old runtimes removed: ${DELETED_COUNT}"
    if [[ -n "$DELETED_RUNTIMES" ]]; then
        log "Deleted runtime versions: ${DELETED_RUNTIMES}"
    fi
    if [[ -n "$ROLLBACK_VERSION" ]]; then
        if [[ "$ROLLBACK_SOURCE" == existing-unrecorded ]]; then
            log "T3 rollback runtime: ${ROLLBACK_VERSION} (existing candidate; provenance unrecorded)"
        else
            log "T3 rollback runtime: ${ROLLBACK_VERSION}"
        fi
    else
        log "T3 rollback runtime: none"
        log "Next successful version transition will establish one."
    fi
    log "Disk reclaimed: ${RECLAIMED_BYTES} bytes"
    log "Remaining runtime versions:"
    runtime_versions | sed 's/^/  /'
    log "Final free disk: $(df -h / | awk 'NR == 2 { print $4 }')"
    update_ops_backup_inventory t3 "$BACKUP_ROOT" dir
    log "T3 core verification: PASS"
    log "T3 core: version=${AFTER_VERSION} launcher=${AFTER_LAUNCHER_TARGET} state=${AFTER_STATE_VERSION} service=${AFTER_SERVICE_STATE} ExecStart=${AFTER_EXECSTART} MainPID=${AFTER_MAIN_PID} MainExe=${AFTER_MAIN_EXECUTABLE} HTTP=${AFTER_HTTP_CODE} pending=none"
    log "OpenCode provider: cached_status=${PROVIDER_CACHE_STATUS} refresh_attempted=${PROVIDER_REFRESH_ATTEMPTED} refresh_result=${PROVIDER_REFRESH_RESULT} final=${PROVIDER_HEALTH} cli=${PROVIDER_CLI_STATUS} message=${PROVIDER_CACHE_MESSAGE:-none}"
    case "$PROVIDER_HEALTH" in
        healthy)
            log "OpenCode provider readiness: HEALTHY"
            log "T3 NIGHTLY UPDATE: PASS"
            ;;
        degraded)
            log "OpenCode provider readiness: DEGRADED"
            log "Provider advisory: health refresh timed out after restart"
            log "T3 NIGHTLY UPDATE: PASS WITH ADVISORY"
            ;;
        hard-failure)
            log "OpenCode provider readiness: HARD-FAILURE"
            log "INTEGRATION HEALTH: FAIL"
            log "T3 NIGHTLY UPDATE: PASS"
            ;;
    esac
    log "UPDATE SUCCESS"
}

removable_runtime_versions() {
    local current="${1:-}" version
    while IFS= read -r version; do
        [[ -n "$version" ]] || continue
        [[ "$version" == "$current" ]] && continue
        printf '%s\n' "$version"
    done < <(runtime_versions || true)
    return 0
}

check_mode() {
    local expected candidates healthy=0

    collect_current_state
    log "CLI version: ${BEFORE_CLI_VERSION:-unknown}"
    log "Resolved launcher: ${BEFORE_LAUNCHER_TARGET:-unknown}"
    log "service-state.json activeVersion: ${BEFORE_STATE_VERSION:-unknown}"
    log "systemd status: ${BEFORE_SERVICE_STATE:-unknown}"
    log "systemd ExecStart: ${BEFORE_EXECSTART:-unknown}"
    log "MainPID: ${BEFORE_MAIN_PID:-unknown}"
    log "Main executable: ${BEFORE_MAIN_EXECUTABLE:-unknown}"
    log "HTTP health: ${BEFORE_HTTP_CODE:-000}"
    log "Active process version/path:"
    if [[ -n "$BEFORE_PROCESS_PATHS" ]]; then
        printf '%s\n' "$BEFORE_PROCESS_PATHS"
    else
        log "  none found"
    fi
    log "Installed runtime versions:"
    if [[ -n "$BEFORE_RUNTIME_LIST" ]]; then
        printf '%s\n' "$BEFORE_RUNTIME_LIST" | sed 's/^/  /'
    else
        log "  none found"
    fi
    candidates="$(removable_runtime_versions "$BEFORE_VERSION")"
    log "Runtimes that would be removable after a successful real update:"
    if [[ -n "$candidates" ]]; then
        printf '%s\n' "$candidates" | sed 's/^/  /'
    else
        log "  none"
    fi

    expected="${VERSIONS_DIR}/${BEFORE_VERSION}/t3"
    [[ -n "$BEFORE_CLI_VERSION" && "$BEFORE_CLI_VERSION" == "$BEFORE_STATE_VERSION" ]] || healthy=1
    [[ "$BEFORE_LAUNCHER_TARGET" == "$expected" && -x "$expected" ]] || healthy=1
    [[ "$BEFORE_SERVICE_STATE" == active ]] || healthy=1
    [[ "$BEFORE_EXECSTART" == *"$expected"* ]] || healthy=1
    [[ "$BEFORE_MAIN_EXECUTABLE" == "$expected" ]] || healthy=1
    [[ "$BEFORE_HTTP_CODE" == 200 ]] || healthy=1
    log_opencode_cache
    opencode_cache_ready || healthy=1
    running_process_uses_version "$BEFORE_VERSION" || healthy=1
    if pending_update_present; then
        log "Pending update marker: present"
        healthy=1
    else
        log "Pending update marker: none"
    fi
    update_ops_backup_inventory t3 "$BACKUP_ROOT" dir

    if (( healthy == 0 )); then
        log "CHECK COMPLETE"
        return 0
    fi
    log "CHECK FAILED"
    return 1
}

main() {
    case "$MODE" in
        --help|-h) usage; exit 0 ;;
        --check|check) MODE=check ;;
        update) ;;
        *) usage; die "Unknown mode: ${MODE}" ;;
    esac

    [[ "$(id -un)" == "$USER_NAME" ]] || die "Run this updater as ${USER_NAME}"
    ((EUID != 0)) || die "Do not run this updater as root"
    need_cmd awk
    need_cmd curl
    need_cmd df
    need_cmd du
    need_cmd find
    need_cmd flock
    need_cmd jq
    need_cmd node
    need_cmd python3
    need_cmd readlink
    need_cmd rm
    need_cmd sort
    need_cmd stat
    need_cmd systemctl
    need_cmd tee
    need_cmd timeout
    need_cmd mv

    setup_user_bus
    preflight
    if [[ "$MODE" == check ]]; then
        if check_mode; then
            exit 0
        fi
        exit 1
    fi

    mkdir -p "$STATE_ROOT" "$BACKUP_ROOT" "$LOG_DIR" "$RUN_DIR" "${STATE_ROOT}/locks" "$T3_RETENTION_STATE_DIR"
    chmod 700 "$T3_RETENTION_STATE_DIR"
    exec 9>"$LOCK_FILE"
    flock -n 9 || die "Another T3 updater is already running"
    exec > >(tee -a "$LOG_FILE") 2>&1

    log "T3 nightly update started"
    log "Log: ${LOG_FILE}"
    record_before_state
    backup_t3_state

    UPDATE_ATTEMPTED=1
    log "Running native T3 nightly updater: ${T3_BIN} update --channel nightly --yes"
    if ! (exec 9>&-; "$T3_BIN" update --channel nightly --yes); then
        die "Native T3 updater exited nonzero"
    fi

    if ! verify_runtime; then
        die "Post-update verification failed within ${START_TIMEOUT}s"
    fi
    # Provider readiness is advisory/integration health; core T3 validity
    # has already passed and controls cleanup and the wrapper exit status.
    ensure_opencode_provider
   VALIDATION_PASSED=1
    cleanup_old_runtimes
    if ! record_successful_rollback; then
        log "T3 rollback metadata: WARN could not record the selected rollback runtime; runtime retention remains active+one"
    fi
    case "$PROVIDER_HEALTH" in
        healthy|degraded)
            if ! update_ops_backup_retention t3 "$BACKUP_ROOT" "$BACKUP_DIR" dir; then
                log "Backup retention: WARN T3 backup pruning did not complete; core T3 update remains successful"
            fi
            ;;
        *)
            log "Backup retention: WARN T3 pruning skipped because provider integration health is ${PROVIDER_HEALTH}; core T3 update remains successful"
            ;;
    esac
    report_final
}

main "$@"

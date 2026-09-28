#!/usr/bin/env bash

set -Eeuo pipefail
umask 077

# Source-only helper: Update-OPS owns backup retention; OpenCode owns its
# native update and runtime lifecycle.
# shellcheck source=/home/ubuntu/tool-updates/scripts/update-ops-backup-retention.sh
source /home/ubuntu/tool-updates/scripts/update-ops-backup-retention.sh

readonly MANAGED_USER="ubuntu"
readonly OPENCODE_BIN="/home/ubuntu/.opencode/bin/opencode"
readonly STATE_ROOT="/home/ubuntu/.local/state/tool-updates"
readonly LOG_DIR="${STATE_ROOT}/logs/opencode"
readonly BACKUP_ROOT="${STATE_ROOT}/backups/opencode"
readonly RUN_ROOT="${STATE_ROOT}/runs/opencode"
readonly LOCK_FILE="${STATE_ROOT}/locks/opencode.lock"
readonly T3_SERVICE="t3code.service"
readonly T3_HEALTH_URL="http://127.0.0.1:3773/"
readonly STOP_TIMEOUT="${STOP_TIMEOUT:-30}"
readonly START_TIMEOUT="${START_TIMEOUT:-45}"
readonly UPDATE_TIMEOUT="${UPDATE_TIMEOUT:-600}"
readonly UPDATE_FALLBACK_TIMEOUT="${UPDATE_FALLBACK_TIMEOUT:-600}"
readonly UPDATE_HEARTBEAT_SECONDS="${UPDATE_HEARTBEAT_SECONDS:-30}"
readonly UPDATE_IDLE_DIAGNOSTIC_SECONDS="${UPDATE_IDLE_DIAGNOSTIC_SECONDS:-60}"
readonly UPDATE_STALL_FALLBACK_SECONDS="${UPDATE_STALL_FALLBACK_SECONDS:-120}"
readonly UPDATE_TERM_GRACE_SECONDS="${UPDATE_TERM_GRACE_SECONDS:-10}"
readonly MIN_FREE_BYTES="${MIN_FREE_BYTES:-4294967296}"

MODE="${1:-update}"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RUN_DIR="${RUN_ROOT}/${TIMESTAMP}-$$"
LOG_FILE="${LOG_DIR}/${TIMESTAMP}-$$.log"
BACKUP_DIR="${BACKUP_ROOT}/${TIMESTAMP}"

BEFORE_VERSION=""
AFTER_VERSION=""
OPENCODE_REAL=""
T3_RESTART_PENDING=0
STANDALONE_RESTART_PENDING=0
NATIVE_PID=""
NATIVE_PGID=""
NATIVE_FAILURE_REASON=""
UPDATE_OUTPUT_FIFO=""
UPDATE_OUTPUT_COUNT=""
UPDATE_OUTPUT_READER_PID=""
UPDATE_LATEST_TARGET=""
OWNED_CLEANUP_FAILED=0

declare -a STANDALONE_STATE_DIRS=()
declare -A T3_OWNED_PIDS=()

log() {
    printf '[%s] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"
}

die() {
    log "ERROR: $*"
    exit 1
}

usage() {
    cat <<'EOF'
Usage:
  update-opencode.sh [update]
  update-opencode.sh --check
  update-opencode.sh --help

Environment overrides:
  MIN_FREE_BYTES STOP_TIMEOUT START_TIMEOUT UPDATE_TIMEOUT UPDATE_FALLBACK_TIMEOUT
  UPDATE_HEARTBEAT_SECONDS UPDATE_IDLE_DIAGNOSTIC_SECONDS
  UPDATE_STALL_FALLBACK_SECONDS UPDATE_TERM_GRACE_SECONDS
EOF
}

need_cmd() {
    command -v "$1" >/dev/null 2>&1 || die "Required command not found: $1"
}

is_number() {
    [[ "${1:-}" =~ ^[0-9]+$ ]]
}

current_version() {
    local output
    output="$(timeout --signal=TERM 10s "$OPENCODE_BIN" --version 2>&1)" || return 1
    printf '%s\n' "$output" | head -n 1 | tr -d '\r'
}

free_bytes() {
    df -PB1 /home/ubuntu | awk 'NR == 2 { print $4 }'
}

setup_user_bus() {
    export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
    export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=${XDG_RUNTIME_DIR}/bus}"
}

proc_exe() {
    readlink -f "/proc/$1/exe" 2>/dev/null || true
}

proc_cmd() {
    tr '\0' ' ' <"/proc/$1/cmdline" 2>/dev/null || true
}

is_managed_pid() {
    local pid="$1"
    [[ -e "/proc/${pid}" ]] &&
        [[ "$(proc_exe "$pid")" == "$OPENCODE_REAL" ]]
}

list_managed_pids() {
    local proc pid
    for proc in /proc/[0-9]*; do
        [[ -d "$proc" ]] || continue
        pid="${proc##*/}"
        is_managed_pid "$pid" && printf '%s\n' "$pid"
    done | sort -n
}

parent_pid() {
    awk '/^PPid:/ { print $2 }' "/proc/$1/status" 2>/dev/null || true
}

is_child_of_managed() {
    local pid="$1" parent
    parent="$(parent_pid "$pid")"
    is_number "$parent" && is_managed_pid "$parent"
}

is_t3_process() {
    local pid="$1" exe cmd
    exe="$(proc_exe "$pid")"
    cmd="$(proc_cmd "$pid")"
    [[ "$exe" == */.t3/runtime/versions/*/t3 ]] ||
        [[ "$cmd" == *"/.t3/runtime/versions/"*"/t3 "* ]]
}

has_t3_ancestor() {
    local pid="$1" parent depth=0
    while ((pid > 1 && depth < 64)); do
        is_t3_process "$pid" && return 0
        parent="$(parent_pid "$pid")"
        is_number "$parent" || return 1
        ((parent > 1)) || return 1
        pid="$parent"
        ((depth += 1))
    done
    return 1
}

assert_safe_invocation_tree() {
    local pid="$$" parent depth=0 cmd exe
    while ((pid > 1 && depth < 64)); do
        exe="$(proc_exe "$pid")"
        cmd="$(proc_cmd "$pid")"
        if [[ "$exe" == "$OPENCODE_REAL" ]]; then
            die "Updater is running under OpenCode. Use a normal SSH shell."
        fi
        if is_t3_process "$pid"; then
            die "Updater is running under T3. Use a normal SSH shell."
        fi
        parent="$(parent_pid "$pid")"
        is_number "$parent" || break
        ((parent > 1)) || break
        pid="$parent"
        ((depth += 1))
    done
}

t3_is_active() {
    systemctl --user is-active --quiet "$T3_SERVICE"
}

show_processes() {
    local -a pids=()
    local pid parent cwd cmd
    mapfile -t pids < <(list_managed_pids)
    if ((${#pids[@]} == 0)); then
        log "Managed OpenCode processes: none"
        return
    fi
    log "Managed OpenCode processes: ${#pids[@]}"
    for pid in "${pids[@]}"; do
        parent="$(parent_pid "$pid")"
        cwd="$(readlink -f "/proc/${pid}/cwd" 2>/dev/null || printf '?')"
        cmd="$(proc_cmd "$pid")"
        printf '  PID=%s PPID=%s CWD=%s CMD=%s\n' "$pid" "$parent" "$cwd" "$cmd"
        has_t3_ancestor "$pid" && printf '  ownership=T3\n'
    done
}

record_t3_owned_pids() {
    local pid
    T3_OWNED_PIDS=()
    while read -r pid; do
        [[ -n "$pid" ]] || continue
        has_t3_ancestor "$pid" && T3_OWNED_PIDS["$pid"]=1
    done < <(list_managed_pids)
}

record_standalone_roots() {
    local -a pids=()
    local pid proc_dir cwd
    mapfile -t pids < <(list_managed_pids)
    for pid in "${pids[@]}"; do
        [[ -n "${T3_OWNED_PIDS[$pid]:-}" ]] && continue
        is_child_of_managed "$pid" && continue
        proc_dir="${RUN_DIR}/process-${pid}"
        mkdir -p "$proc_dir"
        cwd="$(readlink -f "/proc/${pid}/cwd" 2>/dev/null || true)"
        [[ -n "$cwd" ]] || die "Could not determine cwd for PID ${pid}"
        printf '%s\n' "$pid" >"${proc_dir}/old-pid"
        printf '%s\n' "$cwd" >"${proc_dir}/cwd"
        cat "/proc/${pid}/cmdline" >"${proc_dir}/cmdline" ||
            die "Could not record command line for PID ${pid}"
        if ! cat "/proc/${pid}/environ" >"${proc_dir}/environ"; then
            : >"${proc_dir}/environ"
            log "WARNING: could not record environment for PID ${pid}"
        fi
        STANDALONE_STATE_DIRS+=("$proc_dir")
    done
    if ((${#STANDALONE_STATE_DIRS[@]})); then
        STANDALONE_RESTART_PENDING=1
        log "Recorded ${#STANDALONE_STATE_DIRS[@]} standalone root process(es)"
    fi
}

wait_for_no_managed() {
    local deadline=$((SECONDS + STOP_TIMEOUT))
    local -a pids=()
    while ((SECONDS < deadline)); do
        mapfile -t pids < <(list_managed_pids)
        ((${#pids[@]} == 0)) && return 0
        sleep 1
    done
    mapfile -t pids < <(list_managed_pids)
    ((${#pids[@]} == 0)) && return 0
    log "OpenCode still running after ${STOP_TIMEOUT}s: ${pids[*]}"
    return 1
}

stop_runtime() {
    local pid
    if t3_is_active; then
        record_t3_owned_pids
        if ((${#T3_OWNED_PIDS[@]})); then
            T3_RESTART_PENDING=1
            log "Stopping ${T3_SERVICE} because it owns ${#T3_OWNED_PIDS[@]} managed OpenCode process(es)"
            systemctl --user stop "$T3_SERVICE"
            t3_is_active && die "T3 service did not stop"
        else
            log "Leaving ${T3_SERVICE} active; it owns no managed OpenCode process"
        fi
    else
        log "T3 was not active"
    fi
    sleep 2
    record_standalone_roots
    while read -r pid; do
        [[ -n "$pid" ]] || continue
        [[ -n "${T3_OWNED_PIDS[$pid]:-}" ]] && continue
        is_child_of_managed "$pid" && continue
        log "Sending SIGTERM to OpenCode PID ${pid}"
        kill -TERM "$pid"
    done < <(list_managed_pids)
    if ! wait_for_no_managed; then
        die "OpenCode did not stop cleanly; refusing automatic SIGKILL"
    fi
    log "OpenCode runtime stopped"
}

check_disk_space() {
    local free
    free="$(free_bytes)"
    is_number "$free" || die "Could not determine free disk space"
    log "Free disk: ${free} bytes"
    ((free >= MIN_FREE_BYTES)) ||
        die "Free disk is below required minimum (${MIN_FREE_BYTES} bytes)"
}

backup_opencode() {
    local config_dir="/home/ubuntu/.config/opencode"
    local data_dir="/home/ubuntu/.local/share/opencode"
    local db="${data_dir}/opencode.db"
    local file quick_check
    mkdir -p "$BACKUP_DIR"
    log "Creating backup: ${BACKUP_DIR}"
    if [[ -d "$config_dir" ]]; then
        if ! tar -C "/home/ubuntu/.config" -czf \
            "${BACKUP_DIR}/config-opencode.tar.gz" opencode; then
            die "OpenCode config backup creation failed"
        fi
        tar -tzf "${BACKUP_DIR}/config-opencode.tar.gz" >/dev/null ||
            die "OpenCode config backup archive validation failed"
    fi
    if [[ -f "$db" ]]; then
        if command -v sqlite3 >/dev/null 2>&1; then
            if ! sqlite3 "$db" ".timeout 10000" \
                ".backup '${BACKUP_DIR}/opencode.db'"; then
                die "OpenCode SQLite online backup command failed"
            fi
            if ! quick_check="$(sqlite3 "${BACKUP_DIR}/opencode.db" \
                'PRAGMA quick_check;' 2>&1)"; then
                die "OpenCode SQLite backup quick_check command failed: ${quick_check}"
            fi
            [[ "$quick_check" == "ok" ]] ||
                die "SQLite backup quick_check failed: ${quick_check}"
            log "SQLite online backup verified"
        else
            log "sqlite3 unavailable; copying quiescent database"
            cp -a "$db" "${BACKUP_DIR}/"
            cmp -s "$db" "${BACKUP_DIR}/opencode.db" ||
                die "OpenCode quiescent SQLite copy verification failed"
        fi
        for file in "$db-wal" "$db-shm" "$db-journal"; do
            [[ -e "$file" ]] && cp -a "$file" "${BACKUP_DIR}/"
        done
    fi
    for file in "${data_dir}/auth.json" "${data_dir}/mcp-auth.json"; do
        [[ -f "$file" ]] && cp -a "$file" "${BACKUP_DIR}/"
    done
    {
        printf 'created_at=%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
        printf 'managed_binary=%s\n' "$OPENCODE_BIN"
        printf 'before_version=%s\n' "$BEFORE_VERSION"
    } >"${BACKUP_DIR}/metadata"
    chmod -R go-rwx "$BACKUP_DIR"
    update_ops_mark_directory_verified "$BACKUP_DIR" opencode "${TIMESTAMP}-$$" ||
        die "Could not mark validated OpenCode backup"
    log "Backup complete"
}

output_counter_value() {
    [[ -f "$UPDATE_OUTPUT_COUNT" ]] && cat "$UPDATE_OUTPUT_COUNT" || printf '0\n'
}

owned_group_has_live_process() {
    local group="$1"
    ps -eo pgid=,stat= | awk -v wanted="$group" '$1 == wanted && $2 !~ /Z/ { found = 1 } END { exit !found }'
}

owned_group_pids() {
    local group="$1"
    ps -eo pid=,pgid= | awk -v wanted="$group" '$2 == wanted { print $1 }'
}

native_group_has_close_wait() {
    local pid
    while IFS= read -r pid; do
        [[ -n "$pid" ]] || continue
        if ss -tnp state close-wait 2>/dev/null | grep -Fq "pid=${pid},"; then
            return 0
        fi
    done < <(owned_group_pids "$NATIVE_PGID")
    return 1
}

native_group_has_curl() {
    local pid command_name
    while IFS= read -r pid; do
        [[ -n "$pid" ]] || continue
        command_name="$(ps -o comm= -p "$pid" 2>/dev/null | tr -d '[:space:]')"
        [[ "$command_name" == curl ]] && return 0
    done < <(owned_group_pids "$NATIVE_PGID")
    return 1
}

log_owned_process_tree() {
    local group="$1" pid
    log "Owned updater process tree (PID PPID PGID SID ELAPSED STATE COMMAND):"
    while IFS= read -r pid; do
        [[ -n "$pid" ]] || continue
        ps -o pid=,ppid=,pgid=,sid=,etime=,stat=,comm= -p "$pid" 2>/dev/null || true
    done < <(owned_group_pids "$group")
}

log_owned_network_sockets() {
    local group="$1" pid sockets
    log "Owned updater TCP sockets:"
    while IFS= read -r pid; do
        [[ -n "$pid" ]] || continue
        sockets="$(ss -tinp 2>/dev/null | awk -v needle="pid=${pid}," 'index($0, needle) { print; if (getline > 0) print }')"
        [[ -n "$sockets" ]] && printf '%s\n' "$sockets"
    done < <(owned_group_pids "$group")
}

stop_owned_updater_group() {
    local group="$1" deadline
    owned_group_has_live_process "$group" || return 0
    log "Sending SIGTERM to updater process group ${group}"
    kill -TERM -- "-${group}" 2>/dev/null || true
    deadline=$((SECONDS + UPDATE_TERM_GRACE_SECONDS))
    while ((SECONDS < deadline)) && owned_group_has_live_process "$group"; do
        sleep 1
    done
    if owned_group_has_live_process "$group"; then
        log "Updater process group ${group} did not stop after SIGTERM; sending SIGKILL as last resort"
        kill -KILL -- "-${group}" 2>/dev/null || true
        deadline=$((SECONDS + 5))
        while ((SECONDS < deadline)) && owned_group_has_live_process "$group"; do
            sleep 1
        done
    fi
    if owned_group_has_live_process "$group"; then
        log "ERROR: owned updater process group ${group} remains alive after SIGKILL"
        return 1
    fi
    return 0
}

stop_unverified_updater_pid() {
    local pid="$1" deadline state
    state="$(ps -o stat= -p "$pid" 2>/dev/null | tr -d '[:space:]')"
    [[ -n "$state" && "$state" != Z* ]] || {
        wait "$pid" 2>/dev/null || true
        return 0
    }
    log "Sending SIGTERM to directly launched updater PID ${pid}; process group ownership was not verified"
    kill -TERM "$pid" 2>/dev/null || true
    deadline=$((SECONDS + UPDATE_TERM_GRACE_SECONDS))
    while ((SECONDS < deadline)); do
        state="$(ps -o stat= -p "$pid" 2>/dev/null | tr -d '[:space:]')"
        [[ -n "$state" && "$state" != Z* ]] || break
        sleep 1
    done
    state="$(ps -o stat= -p "$pid" 2>/dev/null | tr -d '[:space:]')"
    if [[ -n "$state" && "$state" != Z* ]]; then
        log "Updater PID ${pid} did not stop after SIGTERM; sending SIGKILL as last resort"
        kill -KILL "$pid" 2>/dev/null || true
        deadline=$((SECONDS + 5))
        while ((SECONDS < deadline)); do
            state="$(ps -o stat= -p "$pid" 2>/dev/null | tr -d '[:space:]')"
            [[ -n "$state" && "$state" != Z* ]] || break
            sleep 1
        done
    fi
    state="$(ps -o stat= -p "$pid" 2>/dev/null | tr -d '[:space:]')"
    if [[ -n "$state" && "$state" != Z* ]]; then
        log "ERROR: directly launched updater PID ${pid} remains alive after SIGKILL"
        return 1
    fi
    wait "$pid" 2>/dev/null || true
    return 0
}

start_output_counter() {
    UPDATE_OUTPUT_FIFO="${RUN_DIR}/updater-output.fifo"
    UPDATE_OUTPUT_COUNT="${RUN_DIR}/updater-output-bytes"
    mkfifo -m 600 "$UPDATE_OUTPUT_FIFO"
    python3 -c 'import os, sys
path = sys.argv[1]
total = 0
while True:
    chunk = sys.stdin.buffer.read(65536)
    if not chunk:
        break
    total += len(chunk)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="ascii") as counter:
        counter.write(str(total))
    os.replace(temporary, path)' "$UPDATE_OUTPUT_COUNT" <"$UPDATE_OUTPUT_FIFO" &
    UPDATE_OUTPUT_READER_PID=$!
}

finish_output_counter() {
    if [[ -n "$UPDATE_OUTPUT_READER_PID" ]]; then
        wait "$UPDATE_OUTPUT_READER_PID" 2>/dev/null || true
    fi
    [[ -z "$UPDATE_OUTPUT_FIFO" ]] || rm -f -- "$UPDATE_OUTPUT_FIFO"
    UPDATE_OUTPUT_READER_PID=""
}

run_owned_update_command() {
    local phase="$1" timeout_seconds="$2" detect_native_stall="$3"
    shift 3
    local started=$SECONDS last_progress=$SECONDS last_heartbeat=$SECONDS
    local last_bytes=0 current_bytes elapsed_seconds idle_seconds
    local close_wait_since=0 last_diagnostic=0 pgid rc local_state

    start_output_counter
    log "${phase}: launching in a dedicated process group"
    setsid env HOME="/home/ubuntu" PATH="${OPENCODE_BIN%/*}:$PATH" "$@" >"$UPDATE_OUTPUT_FIFO" 2>&1 &
    NATIVE_PID=$!
    NATIVE_PGID=""
    for _ in 1 2 3 4 5; do
        pgid="$(ps -o pgid= -p "$NATIVE_PID" 2>/dev/null | tr -d '[:space:]')"
        [[ -n "$pgid" ]] && break
        sleep 0.1
    done
    if [[ -z "$pgid" ]]; then
        local_state="$(ps -o stat= -p "$NATIVE_PID" 2>/dev/null | tr -d '[:space:]')"
        if [[ -z "$local_state" || "$local_state" == Z* ]]; then
            if wait "$NATIVE_PID"; then rc=0; else rc=$?; fi
            log "${phase}: updater exited before its process group could be sampled (PID=${NATIVE_PID} exit_code=${rc})"
            finish_output_counter
            return "$rc"
        fi
        NATIVE_FAILURE_REASON="could not establish native updater process group"
        log "ERROR: ${NATIVE_FAILURE_REASON} (PID=${NATIVE_PID}); terminating only the directly launched updater and blocking fallback"
        stop_unverified_updater_pid "$NATIVE_PID" || true
        OWNED_CLEANUP_FAILED=1
        finish_output_counter
        return 1
    fi
    if [[ "$pgid" != "$NATIVE_PID" ]]; then
        log "ERROR: setsid did not establish an owned process group (PID=${NATIVE_PID} PGID=${pgid})"
        NATIVE_FAILURE_REASON="setsid process group ownership did not match the launched updater"
        stop_unverified_updater_pid "$NATIVE_PID" || OWNED_CLEANUP_FAILED=1
        OWNED_CLEANUP_FAILED=1
        finish_output_counter
        return 1
    fi
    NATIVE_PGID="$pgid"
    log "${phase}: native PID=${NATIVE_PID} PGID=${NATIVE_PGID}; hard timeout=${timeout_seconds}s"

    while owned_group_has_live_process "$NATIVE_PGID"; do
        current_bytes="$(output_counter_value)"
        if [[ "$current_bytes" =~ ^[0-9]+$ ]] && ((current_bytes > last_bytes)); then
            last_bytes="$current_bytes"
            last_progress=$SECONDS
            close_wait_since=0
        fi
        elapsed_seconds=$((SECONDS - started))
        idle_seconds=$((SECONDS - last_progress))
        if ((SECONDS - last_heartbeat >= UPDATE_HEARTBEAT_SECONDS)); then
            log "${phase} heartbeat: PID=${NATIVE_PID} elapsed=${elapsed_seconds}s no_output=${idle_seconds}s output_bytes=${last_bytes}"
            last_heartbeat=$SECONDS
        fi
        if ((idle_seconds >= UPDATE_IDLE_DIAGNOSTIC_SECONDS &&
             idle_seconds - last_diagnostic >= UPDATE_IDLE_DIAGNOSTIC_SECONDS)); then
            log "${phase}: no observable updater output for ${idle_seconds}s; collecting process and socket diagnostics"
            log_owned_process_tree "$NATIVE_PGID"
            log_owned_network_sockets "$NATIVE_PGID"
            last_diagnostic=$idle_seconds
        fi
        if [[ "$detect_native_stall" == true ]] &&
            ((idle_seconds >= UPDATE_STALL_FALLBACK_SECONDS)) &&
            native_group_has_close_wait &&
            ! native_group_has_curl; then
            if ((close_wait_since == 0)); then
                close_wait_since=$SECONDS
            elif ((SECONDS - close_wait_since >= UPDATE_IDLE_DIAGNOSTIC_SECONDS)); then
                NATIVE_FAILURE_REASON="persistent CLOSE-WAIT socket with no observable progress"
                log "${phase}: ${NATIVE_FAILURE_REASON}; stopping only the owned updater process group"
                log_owned_process_tree "$NATIVE_PGID"
                log_owned_network_sockets "$NATIVE_PGID"
                if ! stop_owned_updater_group "$NATIVE_PGID"; then
                    OWNED_CLEANUP_FAILED=1
                    finish_output_counter
                    return 1
                fi
                wait "$NATIVE_PID" 2>/dev/null || true
                finish_output_counter
                return 124
            fi
        else
            close_wait_since=0
        fi
        if ((elapsed_seconds >= timeout_seconds)); then
            NATIVE_FAILURE_REASON="total hard timeout after ${timeout_seconds}s"
            log "${phase}: ${NATIVE_FAILURE_REASON}; stopping only the owned updater process group"
            log_owned_process_tree "$NATIVE_PGID"
            log_owned_network_sockets "$NATIVE_PGID"
            if ! stop_owned_updater_group "$NATIVE_PGID"; then
                OWNED_CLEANUP_FAILED=1
                finish_output_counter
                return 1
            fi
            wait "$NATIVE_PID" 2>/dev/null || true
            finish_output_counter
            return 124
        fi
        sleep 1
    done

    if wait "$NATIVE_PID"; then
        rc=0
    else
        rc=$?
    fi
    if owned_group_has_live_process "$NATIVE_PGID"; then
        log "${phase}: updater parent exited while owned child processes remain"
        log_owned_process_tree "$NATIVE_PGID"
        if ! stop_owned_updater_group "$NATIVE_PGID"; then
            OWNED_CLEANUP_FAILED=1
            finish_output_counter
            return 1
        fi
    fi
    finish_output_counter
    return "$rc"
}

official_latest_version() {
    local response tag
    response="$(curl --fail --silent --show-error --location --connect-timeout 10 --max-time 20 \
        https://api.github.com/repos/anomalyco/opencode/releases/latest)" || return 1
    tag="$(printf '%s' "$response" | jq -r '.tag_name // empty')"
    tag="${tag#v}"
    [[ "$tag" =~ ^[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.-]+)?$ ]] || return 1
    printf '%s\n' "$tag"
}

run_official_fallback() {
    local target="$1" installer="${RUN_DIR}/opencode-install.sh" rc
    log "Official fallback: downloading https://opencode.ai/install with TLS verification"
    curl --fail --silent --show-error --location --connect-timeout 10 --max-time 30 \
        https://opencode.ai/install --output "$installer" || return $?
    chmod 600 "$installer"
    bash -n "$installer" || {
        log "ERROR: downloaded official installer failed bash syntax validation"
        return 1
    }
    log "Official fallback: installing upstream release ${target} into ${OPENCODE_BIN%/*}"
    if run_owned_update_command "OpenCode official fallback" "$UPDATE_FALLBACK_TIMEOUT" false \
        bash "$installer" --version "$target" --no-modify-path; then
        return 0
    else
        rc=$?
        log "Official fallback failed with exit code ${rc}"
        return "$rc"
    fi
}

perform_update() {
    local native_rc target current
    NATIVE_FAILURE_REASON=""
    log "Update phase: native OpenCode updater, method=curl"
    log "Native updater output is counted but not persisted; secrets cannot enter the wrapper log"
    if run_owned_update_command "OpenCode native updater" "$UPDATE_TIMEOUT" true \
        "$OPENCODE_BIN" upgrade --method curl --print-logs --log-level INFO; then
        log "Native updater exited successfully; verifying managed version against the official release target"
        current="$(current_version 2>/dev/null || true)"
        target="$(official_latest_version 2>/dev/null || true)"
        if [[ -n "$current" && -n "$target" && "$current" == "$target" ]]; then
            UPDATE_LATEST_TARGET="$target"
            log "Native updater installed verified official target ${target}"
            return 0
        fi
        if [[ -n "$current" && "$current" != "$BEFORE_VERSION" && -z "$target" ]]; then
            log "Official latest lookup unavailable; native updater changed managed version to healthy ${current}"
            return 0
        fi
        if [[ -n "$target" && "$current" == "$BEFORE_VERSION" &&
              "$target" != "$BEFORE_VERSION" ]]; then
            NATIVE_FAILURE_REASON="native updater returned success without installing verified official latest"
            UPDATE_LATEST_TARGET="$target"
        else
            NATIVE_FAILURE_REASON="native updater exited without verifying the managed version"
        fi
        native_rc=1
        log "${NATIVE_FAILURE_REASON}: before=${BEFORE_VERSION} after=${current:-unhealthy} official_latest=${target:-unavailable}"
    else
        native_rc=$?
    fi

    log "Native updater failed: exit_code=${native_rc} reason=${NATIVE_FAILURE_REASON:-quick nonzero exit}"
    if [[ "$NATIVE_FAILURE_REASON" != "persistent CLOSE-WAIT socket with no observable progress" &&
          "$NATIVE_FAILURE_REASON" != "total hard timeout after ${UPDATE_TIMEOUT}s" &&
          "$NATIVE_FAILURE_REASON" != "native updater returned success without installing verified official latest" ]]; then
        current="$(current_version 2>/dev/null || true)"
        if [[ "$current" == "$BEFORE_VERSION" ]]; then
            log "OPENCODE UPDATE: FAILED; managed binary remains healthy at ${BEFORE_VERSION}"
        fi
        return "$native_rc"
    fi
    if ((OWNED_CLEANUP_FAILED)); then
        log "OPENCODE UPDATE: FAILED; owned updater process cleanup could not be verified; official fallback is blocked"
        return "$native_rc"
    fi

    current="$(current_version 2>/dev/null || true)"
    if [[ ! -x "$OPENCODE_BIN" || "$current" != "$BEFORE_VERSION" ]]; then
        log "Fallback blocked: managed binary did not remain healthy at the original version (${BEFORE_VERSION})"
        log "OPENCODE UPDATE: FAILED; native updater rc=${native_rc}; managed version=${current:-unhealthy}"
        return "$native_rc"
    fi
    target="$(official_latest_version 2>/dev/null || true)"
    if [[ -z "$target" || "$target" == "$BEFORE_VERSION" ]]; then
        log "Fallback blocked: official latest release target could not be verified or is unchanged"
        log "OPENCODE UPDATE: FAILED; managed binary remains healthy at ${BEFORE_VERSION}"
        return "$native_rc"
    fi
    UPDATE_LATEST_TARGET="$target"
    log "Fallback trigger verified: native updater ${NATIVE_FAILURE_REASON}; managed binary is still healthy at ${BEFORE_VERSION}; official latest target=${target}"
    if ! run_official_fallback "$target"; then
        log "OPENCODE UPDATE: FAILED; native updater rc=${native_rc}; managed version remains $(current_version 2>/dev/null || printf 'unhealthy')"
        return "$native_rc"
    fi
    AFTER_VERSION="$(current_version 2>/dev/null || true)"
    if [[ "$AFTER_VERSION" != "$target" ]]; then
        log "OPENCODE UPDATE: FAILED; official fallback target=${target}, managed version=${AFTER_VERSION:-unhealthy}"
        return "$native_rc"
    fi
    log "Official fallback verified: ${BEFORE_VERSION} -> ${AFTER_VERSION} at ${OPENCODE_BIN}"
    return 0
}

verify_update() {
    [[ -x "$OPENCODE_BIN" ]] || die "Managed binary disappeared after update"
    OPENCODE_REAL="$(readlink -f "$OPENCODE_BIN")"
    [[ -n "$OPENCODE_REAL" && "$OPENCODE_REAL" == "${OPENCODE_BIN%/*}/"* ]] ||
        die "Managed binary resolves outside ${OPENCODE_BIN%/*}: ${OPENCODE_REAL:-unresolved}"
    AFTER_VERSION="$(current_version)"
    [[ -n "$AFTER_VERSION" ]] || die "Post-update --version failed"
    if [[ -n "$UPDATE_LATEST_TARGET" && "$AFTER_VERSION" != "$UPDATE_LATEST_TARGET" ]]; then
        die "Managed OpenCode version ${AFTER_VERSION} does not match verified target ${UPDATE_LATEST_TARGET}"
    fi
    log "Version before: ${BEFORE_VERSION}"
    log "Version after:  ${AFTER_VERSION}"
}

restart_standalone() {
    local dir cwd restart_log new_pid
    local -a cmd=() envv=()
    ((STANDALONE_RESTART_PENDING)) || return 0
    if ! "$OPENCODE_BIN" --version >/dev/null 2>&1; then
        log "ERROR: OpenCode is unhealthy; standalone runtime was not restarted"
        return 1
    fi
    for dir in "${STANDALONE_STATE_DIRS[@]}"; do
        cwd="$(<"${dir}/cwd")"
        mapfile -d '' -t cmd <"${dir}/cmdline" || true
        mapfile -d '' -t envv <"${dir}/environ" || true
        if ((${#cmd[@]} == 0)); then
            log "ERROR: recorded OpenCode command is empty"
            return 1
        fi
        cmd[0]="$OPENCODE_BIN"
        restart_log="${LOG_DIR}/${TIMESTAMP}-standalone-restart.log"
        log "Restarting standalone OpenCode in cwd=${cwd}"
        (
            cd "$cwd"
            if ((${#envv[@]})); then
                nohup env -i "${envv[@]}" "${cmd[@]}" \
                    >>"$restart_log" 2>&1 </dev/null &
            else
                nohup "${cmd[@]}" >>"$restart_log" 2>&1 </dev/null &
            fi
            printf '%s\n' "$!" >"${dir}/new-pid"
        )
        new_pid="$(<"${dir}/new-pid")"
        sleep 2
        if ! kill -0 "$new_pid" 2>/dev/null &&
            [[ -z "$(list_managed_pids)" ]]; then
            log "ERROR: standalone OpenCode restart failed"
            return 1
        fi
    done
    STANDALONE_RESTART_PENDING=0
}

wait_for_t3_health() {
    local deadline=$((SECONDS + START_TIMEOUT))
    while ((SECONDS < deadline)); do
        if systemctl --user is-active --quiet "$T3_SERVICE"; then
            if ! command -v curl >/dev/null 2>&1; then
                return 0
            fi
            curl --fail --silent --show-error --max-time 5 \
                "$T3_HEALTH_URL" >/dev/null 2>&1 && return 0
        fi
        sleep 1
    done
    return 1
}

restart_t3() {
    ((T3_RESTART_PENDING)) || return 0
    log "Starting ${T3_SERVICE}"
    systemctl --user start "$T3_SERVICE"
    wait_for_t3_health || {
        log "T3 post-start health verification failed"
        systemctl --user status "$T3_SERVICE" --no-pager -l || true
        return 1
    }
    T3_RESTART_PENDING=0
    log "T3 restarted and healthy"
}

restore_runtime_after_failure() {
    local rc="$1"
    log "Update failed with exit code ${rc}"
    ((STANDALONE_RESTART_PENDING)) && restart_standalone || true
    ((T3_RESTART_PENDING)) && restart_t3 || true
}

on_exit() {
    local rc=$?
    trap - EXIT
    if ((rc != 0)); then
        restore_runtime_after_failure "$rc"
    fi
    exit "$rc"
}

check_mode() {
    local path alt_bin alt_version
    log "Managed binary: ${OPENCODE_BIN}"
    log "Resolved binary: ${OPENCODE_REAL}"
    log "Managed version: ${BEFORE_VERSION}"
    path="$(command -v opencode 2>/dev/null || true)"
    if [[ -n "$path" ]]; then
        log "PATH opencode: ${path}"
    else
        log "PATH opencode: not found"
    fi
    if [[ -n "$path" && "$(readlink -f "$path")" != "$OPENCODE_REAL" ]]; then
        log "WARNING: PATH resolves to a different OpenCode installation"
    fi
    alt_bin="/home/ubuntu/.nvm/versions/node/v24.18.0/bin/opencode"
    if [[ -x "$alt_bin" ]]; then
        alt_version="$("$alt_bin" --version 2>/dev/null | head -n 1 || true)"
        log "Informational only: npm OpenCode ${alt_bin} ${alt_version}"
    fi
    if t3_is_active; then
        log "T3 service: active"
    else
        log "T3 service: inactive"
    fi
    show_processes
    log "Free disk: $(free_bytes) bytes"
    update_ops_backup_inventory opencode "$BACKUP_ROOT" dir
    log "CHECK COMPLETE"
}

main() {
    case "$MODE" in
        --help|-h) usage; exit 0 ;;
        --check|check) MODE=check ;;
        update) ;;
        *) usage; die "Unknown mode: ${MODE}" ;;
    esac
    [[ "$(id -un)" == "$MANAGED_USER" ]] || die "Run as ${MANAGED_USER}"
    ((EUID != 0)) || die "Do not run as root"
    need_cmd awk
    need_cmd df
    need_cmd du
    need_cmd find
    need_cmd flock
    need_cmd readlink
    need_cmd sort
    need_cmd ss
    need_cmd setsid
    need_cmd stat
    need_cmd systemctl
    need_cmd tar
    need_cmd timeout
    need_cmd ps
    need_cmd python3
    need_cmd jq
    [[ -x "$OPENCODE_BIN" ]] || die "Managed binary not found: ${OPENCODE_BIN}"
    for duration in "$UPDATE_TIMEOUT" "$UPDATE_FALLBACK_TIMEOUT" \
        "$UPDATE_HEARTBEAT_SECONDS" "$UPDATE_IDLE_DIAGNOSTIC_SECONDS" \
        "$UPDATE_STALL_FALLBACK_SECONDS" "$UPDATE_TERM_GRACE_SECONDS"; do
        if ! is_number "$duration" || ((duration <= 0)); then
            die "Update timing values must be positive integers"
        fi
    done
    OPENCODE_REAL="$(readlink -f "$OPENCODE_BIN")"
    [[ -n "$OPENCODE_REAL" && "$OPENCODE_REAL" == "${OPENCODE_BIN%/*}/"* ]] ||
        die "Managed binary resolves outside ${OPENCODE_BIN%/*}: ${OPENCODE_REAL:-unresolved}"
    BEFORE_VERSION="$(current_version)"
    [[ -n "$BEFORE_VERSION" ]] || die "Could not determine current version"
    setup_user_bus
    assert_safe_invocation_tree
    if [[ "$MODE" == check ]]; then
        check_mode
        exit 0
    fi
    mkdir -p "$STATE_ROOT" "$LOG_DIR" "$BACKUP_ROOT" "$RUN_DIR" "${STATE_ROOT}/locks"
    exec 9>"$LOCK_FILE"
    flock -n 9 || die "Another OpenCode updater is already running"
    exec > >(tee -a "$LOG_FILE") 2>&1
    trap on_exit EXIT
    log "OpenCode update started"
    log "Managed binary: ${OPENCODE_BIN}"
    log "Current version: ${BEFORE_VERSION}"
    log "Native update hard timeout: ${UPDATE_TIMEOUT}s; official fallback hard timeout: ${UPDATE_FALLBACK_TIMEOUT}s; idle diagnostics: ${UPDATE_IDLE_DIAGNOSTIC_SECONDS}s; heartbeat: ${UPDATE_HEARTBEAT_SECONDS}s"
    log "Log: ${LOG_FILE}"
    check_disk_space
    stop_runtime
    check_disk_space
    backup_opencode
    perform_update
    verify_update
    restart_standalone
    restart_t3
    if ! update_ops_backup_retention opencode "$BACKUP_ROOT" "$BACKUP_DIR" dir; then
        log "Backup retention: WARN OpenCode backup pruning did not complete; application update remains successful"
    fi
    log "OpenCode: ${BEFORE_VERSION} -> ${AFTER_VERSION}"
    log "UPDATE SUCCESS"
}

main "$@"

#!/usr/bin/env bash

# Source-only helper.  Update-OPS owns backup retention; tool wrappers own
# backup creation and decide when a successful lifecycle permits pruning.

update_ops_retention_log() {
    if declare -F log >/dev/null 2>&1; then
        log "$*"
    else
        printf '%s\n' "$*"
    fi
}

update_ops_backup_marker_valid() {
    local marker="$1"
    local tool="$2"
    local owner expected_owner line has_tool=0 has_verified_at=0

    [[ -f "$marker" && ! -L "$marker" ]] || return 1
    owner="$(stat -c '%u' -- "$marker" 2>/dev/null || true)"
    expected_owner="$(id -u)"
    [[ "$owner" == "$expected_owner" ]] || return 1
    [[ "$(stat -c '%a' -- "$marker" 2>/dev/null || true)" == 600 ]] || return 1

    while IFS= read -r line; do
        case "$line" in
            "tool=${tool}") has_tool=1 ;;
            verified_at=*) has_verified_at=1 ;;
        esac
    done <"$marker"

    ((has_tool == 1 && has_verified_at == 1))
}

update_ops_mark_directory_verified() {
    local backup_dir="$1"
    local tool="$2"
    local run_id="$3"
    local marker="${backup_dir}/.update-ops-verified"

    [[ -d "$backup_dir" && ! -L "$backup_dir" ]] || return 1
    {
        printf 'tool=%s\n' "$tool"
        printf 'verified_at=%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
        printf 'run_id=%s\n' "$run_id"
    } >"$marker"
    chmod 600 "$marker"
    update_ops_backup_marker_valid "$marker" "$tool"
}

update_ops_mark_archive_verified() {
    local archive="$1"
    local tool="$2"
    local run_id="$3"
    local marker="${archive}.verified"

    [[ -f "$archive" && ! -L "$archive" ]] || return 1
    {
        printf 'tool=%s\n' "$tool"
        printf 'verified_at=%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
        printf 'run_id=%s\n' "$run_id"
    } >"$marker"
    chmod 600 "$marker"
    update_ops_backup_marker_valid "$marker" "$tool"
}

update_ops_backup_item_is_safe() {
    local root_real="$1"
    local item="$2"
    local kind="$3"
    local resolved marker marker_resolved owner expected_owner

    resolved="$(readlink -f -- "$item" 2>/dev/null || true)"
    [[ -n "$resolved" && "$resolved" == "${root_real}/"* ]] || return 1
    owner="$(stat -c '%u' -- "$item" 2>/dev/null || true)"
    expected_owner="$(id -u)"
    [[ "$owner" == "$expected_owner" ]] || return 1

    case "$kind" in
        dir)
            [[ -d "$item" && ! -L "$item" ]] || return 1
            marker="${item}/.update-ops-verified"
            [[ "$(readlink -f -- "$marker" 2>/dev/null || true)" == "${resolved}/.update-ops-verified" ]] || return 1
            ;;
        archive)
            [[ -f "$item" && ! -L "$item" ]] || return 1
            marker="${item}.verified"
            marker_resolved="$(readlink -f -- "$marker" 2>/dev/null || true)"
            [[ -n "$marker_resolved" && "$marker_resolved" == "${root_real}/"* ]] || return 1
            ;;
        *)
            return 1
            ;;
    esac

    update_ops_backup_marker_valid "$marker" "${UPDATE_OPS_RETENTION_TOOL:-unknown}"
}

update_ops_backup_sort_key() {
    local kind="$1"
    local path="$2"
    local name="${path##*/}"
    local mtime=""

    case "$kind:$name" in
        dir:[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]T[0-9][0-9][0-9][0-9][0-9][0-9]Z)
            printf '%s\n' "$name"
            return 0
            ;;
        archive:hermes-pre-update-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]T[0-9][0-9][0-9][0-9][0-9][0-9]Z.zip)
            printf '%s\n' "$name"
            return 0
            ;;
    esac

    mtime="$(stat -c '%Y' -- "$path" 2>/dev/null || printf '0')"
    printf '%020d:%s\n' "$mtime" "$name"
}

update_ops_backup_inventory() {
    local tool="$1"
    local root="$2"
    local kind="$3"
    local path name marker sort_key record
    local root_bytes=0 verified=0 legacy=0 unknown=0
    local idx
    local -a items=() records=() sorted_records=()

    if [[ ! -d "$root" ]]; then
        update_ops_retention_log "Backup inventory ${tool}: root unavailable (${root})"
        return 0
    fi

    root_bytes="$(du -sb -- "$root" 2>/dev/null | awk '{print $1}')"
    case "$kind" in
        dir)
            mapfile -d '' -t items < <(find "$root" -mindepth 1 -maxdepth 1 -type d -print0)
            ;;
        archive)
            mapfile -d '' -t items < <(find "$root" -mindepth 1 -maxdepth 1 -type f -name '*.zip' -print0)
            ;;
        *)
            update_ops_retention_log "Backup inventory ${tool}: unknown backup kind ${kind}"
            return 1
            ;;
    esac

    for path in "${items[@]}"; do
        case "$kind" in
            dir) marker="${path}/.update-ops-verified" ;;
            archive) marker="${path}.verified" ;;
        esac
        if update_ops_backup_marker_valid "$marker" "$tool"; then
            verified=$((verified + 1))
            name="${path##*/}"
            sort_key="$(update_ops_backup_sort_key "$kind" "$path")"
            records+=("${sort_key}"$'\t'"${name}")
        elif [[ -e "$path" ]]; then
            legacy=$((legacy + 1))
        else
            unknown=$((unknown + 1))
        fi
    done

    update_ops_retention_log "Backup inventory ${tool}: items=${#items[@]} verified=${verified} legacy-unmarked=${legacy} unknown=${unknown} bytes=${root_bytes:-0}"
    update_ops_retention_log "Backup inventory ${tool}: legacy-unmarked artifacts are read-only and not retention candidates"
    if ((${#records[@]} > 3)); then
        mapfile -t sorted_records < <(printf '%s\n' "${records[@]}" | LC_ALL=C sort -r -k1,1)
        update_ops_retention_log "Backup inventory ${tool}: would-delete verified candidates under policy:"
        for ((idx=3; idx<${#sorted_records[@]}; idx++)); do
            record="${sorted_records[idx]}"
            update_ops_retention_log "  ${record#*$'\t'}"
        done
    else
        update_ops_retention_log "Backup inventory ${tool}: would-delete verified candidates under policy: none"
    fi
    return 0
}

update_ops_backup_retention() {
    local tool="$1"
    local root="$2"
    local current_path="$3"
    local kind="$4"
    local root_real current_real path name marker record resolved size sort_key
    local before_bytes after_bytes verified=0 legacy=0 removed=0 reclaimed=0 failure=0
    local idx
    local -a items=() records=() sorted_records=()

    UPDATE_OPS_RETENTION_TOOL="$tool"

    [[ -d "$root" ]] || {
        update_ops_retention_log "Backup retention ${tool}: WARN root unavailable (${root})"
        return 1
    }
    root_real="$(readlink -f -- "$root" 2>/dev/null || true)"
    [[ -n "$root_real" ]] || {
        update_ops_retention_log "Backup retention ${tool}: WARN could not resolve backup root"
        return 1
    }
    current_real=""
    if [[ -e "$current_path" ]]; then
        current_real="$(readlink -f -- "$current_path" 2>/dev/null || true)"
    fi
    before_bytes="$(du -sb -- "$root" 2>/dev/null | awk '{print $1}')"

    case "$kind" in
        dir)
            mapfile -d '' -t items < <(find "$root" -mindepth 1 -maxdepth 1 -type d -print0)
            ;;
        archive)
            mapfile -d '' -t items < <(find "$root" -mindepth 1 -maxdepth 1 -type f -name '*.zip' -print0)
            ;;
        *)
            update_ops_retention_log "Backup retention ${tool}: WARN unknown backup kind ${kind}"
            return 1
            ;;
    esac

    for path in "${items[@]}"; do
        name="${path##*/}"
        case "$kind" in
            dir) marker="${path}/.update-ops-verified" ;;
            archive) marker="${path}.verified" ;;
        esac
        if update_ops_backup_marker_valid "$marker" "$tool"; then
            verified=$((verified + 1))
            sort_key="$(update_ops_backup_sort_key "$kind" "$path")"
            records+=("${sort_key}"$'\t'"${path}")
        else
            legacy=$((legacy + 1))
        fi
    done

    if ((${#records[@]} > 0)); then
        mapfile -t sorted_records < <(printf '%s\n' "${records[@]}" | LC_ALL=C sort -r -k1,1)
    fi

    update_ops_retention_log "Backup retention ${tool}: verified=${verified} legacy-unmarked=${legacy} keeping newest 3 verified backups"
    if ((verified < 4)); then
        update_ops_retention_log "Backup retention ${tool}: nothing eligible for removal"
        return 0
    fi

    for ((idx=3; idx<${#sorted_records[@]}; idx++)); do
        record="${sorted_records[idx]}"
        path="${record#*$'\t'}"
        resolved="$(readlink -f -- "$path" 2>/dev/null || true)"
        [[ -n "$resolved" && "$resolved" == "${root_real}/"* ]] || {
            update_ops_retention_log "Backup retention ${tool}: WARN refusing unsafe candidate ${path}"
            failure=1
            continue
        }
        [[ -n "$current_real" && "$resolved" == "$current_real" ]] && {
            update_ops_retention_log "Backup retention ${tool}: preserving current run backup ${path}"
            continue
        }
        if ! update_ops_backup_item_is_safe "$root_real" "$path" "$kind"; then
            update_ops_retention_log "Backup retention ${tool}: WARN refusing unproven candidate ${path}"
            failure=1
            continue
        fi
        size="$(du -sb -- "$path" 2>/dev/null | awk '{print $1}')"
        case "$kind" in
            dir)
                marker="${path}/.update-ops-verified"
                if ! rm -rf -- "$path"; then
                    update_ops_retention_log "Backup retention ${tool}: WARN failed to remove ${path}"
                    failure=1
                    continue
                fi
                ;;
            archive)
                marker="${path}.verified"
                if ! rm -f -- "$path" "$marker"; then
                    update_ops_retention_log "Backup retention ${tool}: WARN failed to remove ${path} and marker"
                    failure=1
                    continue
                fi
                ;;
        esac
        removed=$((removed + 1))
        reclaimed=$((reclaimed + ${size:-0}))
        update_ops_retention_log "Backup retention ${tool}: removed verified backup ${path} (${size:-0} bytes)"
    done

    after_bytes="$(du -sb -- "$root" 2>/dev/null | awk '{print $1}')"
    update_ops_retention_log "Backup retention ${tool}: retained newest 3 verified backups; removed=${removed} bytes_reclaimed=${reclaimed} root_before=${before_bytes:-0} root_after=${after_bytes:-0}"
    ((failure == 0))
}

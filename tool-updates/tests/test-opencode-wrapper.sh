#!/usr/bin/env bash

set -Eeuo pipefail

SOURCE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
readonly SOURCE_ROOT
readonly OPENCODE_SOURCE="${SOURCE_ROOT}/tool-updates/scripts/update-opencode.sh"
readonly HELPER_SOURCE="${SOURCE_ROOT}/tool-updates/scripts/update-ops-backup-retention.sh"
ROOT="$(mktemp -d /tmp/update-ops-opencode-test.XXXXXX)"
readonly ROOT
readonly OLD_VERSION="1.18.32"
readonly NEW_VERSION="1.18.33"

cleanup() {
    local file pid
    for file in "${ROOT}"/cases/*/service-pid "${ROOT}"/cases/*/unrelated-pid; do
        [[ -f "$file" ]] || continue
        pid="$(<"$file")"
        kill -TERM -- "-${pid}" 2>/dev/null || true
        kill -TERM "$pid" 2>/dev/null || true
    done
    rm -rf -- "$ROOT"
}
trap cleanup EXIT

mkdir -p "${ROOT}/mocks"
cat >"${ROOT}/fake-app.c" <<'C'
#define _GNU_SOURCE
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static int read_version(char *out) {
    const char *path = getenv("OC_VERSION_FILE");
    FILE *file = path ? fopen(path, "r") : NULL;
    if (!file) return 1;
    int result = fscanf(file, "%127s", out) == 1 ? 0 : 1;
    fclose(file);
    return result;
}

static int write_version(const char *version) {
    const char *path = getenv("OC_VERSION_FILE");
    FILE *file = path ? fopen(path, "w") : NULL;
    if (!file) return 1;
    fprintf(file, "%s\n", version);
    return fclose(file) ? 1 : 0;
}

int main(int argc, char **argv) {
    if (argc > 1 && strcmp(argv[1], "--version") == 0) {
        char version[128];
        if (read_version(version)) return 2;
        printf("%s\n", version);
        return 0;
    }
    if (argc > 1 && (strcmp(argv[1], "--service-session") == 0 || strcmp(argv[1], "unrelated") == 0)) {
        for (;;) pause();
    }
    if (argc > 1 && strcmp(argv[1], "upgrade") == 0) {
        const char *called = getenv("OC_NATIVE_CALLED_FILE");
        if (called) close(open(called, O_CREAT | O_WRONLY, 0600));
        const char *mode = getenv("OC_NATIVE_MODE");
        if (mode && strcmp(mode, "quick-fail") == 0) return 42;
        if (mode && strcmp(mode, "success") == 0) return write_version("1.18.33");
        if (mode && strcmp(mode, "hang") == 0) {
            const char *pid_path = getenv("OC_UNRELATED_PID_FILE");
            if (pid_path) {
                pid_t child = fork();
                if (child == 0) {
                    setsid();
                    close(STDOUT_FILENO);
                    close(STDERR_FILENO);
                    execl("/proc/self/exe", "opencode", "unrelated", (char *)NULL);
                    _exit(127);
                }
                if (child > 0) {
                    FILE *file = fopen(pid_path, "w");
                    if (file) { fprintf(file, "%d\n", child); fclose(file); }
                }
            }
            for (;;) pause();
        }
        return 0;
    }
    return 0;
}
C

cat >"${ROOT}/fake-t3.c" <<'C'
#define _GNU_SOURCE
#include <signal.h>
#include <stdlib.h>
#include <unistd.h>

int main(int argc, char **argv) {
    if (argc > 1 && argv[1][0] == '_' && argv[1][1] == '_') {
        const char *opencode = getenv("OPENCODE_BIN");
        pid_t child = fork();
        if (child == 0) {
            execl(opencode, opencode, "--service-session", (char *)NULL);
            _exit(127);
        }
        for (;;) pause();
    }
    return 0;
}
C

cat >"${ROOT}/mocks/systemctl" <<'MOCK'
#!/usr/bin/env bash
set -euo pipefail
root="$FIXTURE_ROOT"
[[ "$1" == --user ]] || exit 90
shift
case "$1" in
    is-active)
        if [[ -f "$root/service-active" ]]; then exit 0; else exit 3; fi
        ;;
    stop)
        if [[ -f "$root/service-pid" ]]; then
            pid="$(<"$root/service-pid")"
            kill -TERM -- "-${pid}" 2>/dev/null || true
            kill -TERM "$pid" 2>/dev/null || true
            for _ in 1 2 3 4 5; do
                kill -0 "$pid" 2>/dev/null || break
                sleep 0.2
            done
        fi
        rm -f "$root/service-active" "$root/service-pid"
        ;;
    start)
        FIXTURE_ROOT="$root" OPENCODE_BIN="$TEST_OPENCODE_BIN" \
            setsid "$TEST_T3_BIN" __service-launcher >/dev/null 2>&1 &
        pid=$!
        printf '%s\n' "$pid" >"$root/service-pid"
        touch "$root/service-active"
        ;;
    status)
        printf 'mock t3code.service status\n'
        ;;
    *) exit 91 ;;
esac
MOCK

cat >"${ROOT}/mocks/curl" <<'MOCK'
#!/usr/bin/env bash
set -euo pipefail
url=""
for argument in "$@"; do
    case "$argument" in
        http://*|https://*) url="$argument" ;;
    esac
done
if [[ "$url" == https://api.github.com/repos/anomalyco/opencode/releases/latest ]]; then
    printf '{"tag_name":"v1.18.33"}\n'
    exit 0
fi
if [[ "$url" == https://opencode.ai/install ]]; then
    output=""
    while (($#)); do
        if [[ "$1" == --output ]]; then output="$2"; shift 2; else shift; fi
    done
    [[ -n "$output" ]] || exit 92
    cat >"$output" <<'INSTALLER'
#!/usr/bin/env bash
set -euo pipefail
[[ "${OC_INSTALLER_FAIL:-0}" != 1 ]] || exit 44
while (($#)); do
    if [[ "$1" == --version ]]; then printf '%s\n' "$2" >"$OC_VERSION_FILE"; exit 0; fi
    shift
done
exit 45
INSTALLER
    exit 0
fi
if [[ "$url" == http://127.0.0.1:3773/ ]]; then exit 0; fi
exit 93
MOCK

cat >"${ROOT}/mocks/sqlite3" <<'MOCK'
#!/usr/bin/env bash
exit 1
MOCK
chmod +x "${ROOT}/mocks/"*

make_case() {
    local name="$1" case_root
    case_root="${ROOT}/cases/${name}"
    mkdir -p "${case_root}/home/ubuntu/.opencode/bin" \
        "${case_root}/home/ubuntu/.t3/runtime/versions/test" \
        "${case_root}/home/ubuntu/.local/state/tool-updates" \
        "${case_root}/home/ubuntu/.local/share/opencode" \
        "${case_root}/home/ubuntu/.config"
    gcc -O2 "${ROOT}/fake-app.c" -o "${case_root}/home/ubuntu/.opencode/bin/opencode"
    gcc -O2 "${ROOT}/fake-t3.c" -o "${case_root}/home/ubuntu/.t3/runtime/versions/test/t3"
    cp "$HELPER_SOURCE" "${case_root}/helper.sh"
    python3 - "$OPENCODE_SOURCE" "${case_root}/update-opencode.sh" "$case_root" <<'PY'
from pathlib import Path
import sys
source, destination, root = map(Path, sys.argv[1:])
text = source.read_text()
replacements = {
    "/home/ubuntu/.opencode/bin/opencode": str(root / "home/ubuntu/.opencode/bin/opencode"),
    "/home/ubuntu/.local/state/tool-updates": str(root / "home/ubuntu/.local/state/tool-updates"),
    "/home/ubuntu/.local/share/opencode": str(root / "home/ubuntu/.local/share/opencode"),
    "/home/ubuntu/.config/opencode": str(root / "home/ubuntu/.config/opencode"),
    "/home/ubuntu/tool-updates/scripts/update-ops-backup-retention.sh": str(root / "helper.sh"),
}
for old, new in replacements.items():
    text = text.replace(old, new)
destination.write_text(text)
destination.chmod(0o700)
PY
    printf '%s\n' "$OLD_VERSION" >"${case_root}/version"
    FIXTURE_CASE_ROOT="$case_root"
}

run_wrapper() {
    local case_root="$1" output="$2" mode="$3" timeout_seconds="${4:-3}" status=0
    FIXTURE_ROOT="$case_root" \
        OC_VERSION_FILE="${case_root}/version" \
        OC_NATIVE_CALLED_FILE="${case_root}/native-called" \
        OC_NATIVE_MODE="$mode" \
        OC_UNRELATED_PID_FILE="${case_root}/unrelated-pid" \
        TEST_OPENCODE_BIN="${case_root}/home/ubuntu/.opencode/bin/opencode" \
        TEST_T3_BIN="${case_root}/home/ubuntu/.t3/runtime/versions/test/t3" \
        OPENCODE_BIN="${case_root}/home/ubuntu/.opencode/bin/opencode" \
        PATH="${ROOT}/mocks:$PATH" \
        MIN_FREE_BYTES=1 STOP_TIMEOUT=2 START_TIMEOUT=2 \
        UPDATE_TIMEOUT="$timeout_seconds" UPDATE_FALLBACK_TIMEOUT=3 \
        UPDATE_HEARTBEAT_SECONDS=1 UPDATE_IDLE_DIAGNOSTIC_SECONDS=1 \
        UPDATE_STALL_FALLBACK_SECONDS=1 UPDATE_TERM_GRACE_SECONDS=1 \
        bash "${case_root}/update-opencode.sh" update >"$output" 2>&1 || status=$?
    return "$status"
}

assert_contains() {
    grep -Fq -- "$2" "$1" || { cat "$1" >&2; printf 'missing expected log: %s\n' "$2" >&2; exit 1; }
}

start_t3() {
    local case_root="$1" pid
    FIXTURE_ROOT="$case_root" \
        TEST_OPENCODE_BIN="${case_root}/home/ubuntu/.opencode/bin/opencode" \
        TEST_T3_BIN="${case_root}/home/ubuntu/.t3/runtime/versions/test/t3" \
        OPENCODE_BIN="${case_root}/home/ubuntu/.opencode/bin/opencode" \
        setsid "${case_root}/home/ubuntu/.t3/runtime/versions/test/t3" __service-launcher \
            >/dev/null 2>&1 &
    pid=$!
    printf '%s\n' "$pid" >"${case_root}/service-pid"
    touch "${case_root}/service-active"
    sleep 0.2
}

run_success_case() {
    local name="$1" mode="$2" initial="$3" active="$4" case_root output status
    make_case "$name"
    case_root="$FIXTURE_CASE_ROOT"
    printf '%s\n' "$initial" >"${case_root}/version"
    output="${case_root}/output.log"
    [[ "$active" == yes ]] && start_t3 "$case_root"
    if run_wrapper "$case_root" "$output" "$mode"; then
        status=0
    else
        status=$?
        cat "$output" >&2
        printf 'expected success for %s, got rc=%s\n' "$name" "$status" >&2
        exit 1
    fi
    assert_contains "$output" "UPDATE SUCCESS"
    [[ "$(<"${case_root}/version")" == "$NEW_VERSION" ]]
    if [[ "$active" == yes ]]; then
        [[ -f "${case_root}/service-active" ]]
        assert_contains "$output" "T3 restarted and healthy"
    else
        [[ ! -f "${case_root}/service-active" ]]
    fi
    printf 'PASS %s\n' "$name"
}

run_failure_case() {
    local name="$1" mode="$2" expected="$3" installer_fail="${4:-0}"
    local case_root output status
    make_case "$name"
    case_root="$FIXTURE_CASE_ROOT"
    output="${case_root}/output.log"
    start_t3 "$case_root"
    if [[ "$name" == sqlite-failure ]]; then
        printf 'invalid sqlite fixture\n' >"${case_root}/home/ubuntu/.local/share/opencode/opencode.db"
    fi
    if OC_INSTALLER_FAIL="$installer_fail" run_wrapper "$case_root" "$output" "$mode" 2; then
        printf 'expected failure for %s\n' "$name" >&2
        exit 1
    else
        status=$?
    fi
    ((status != 0))
    assert_contains "$output" "$expected"
    [[ "$(<"${case_root}/version")" == "$OLD_VERSION" ]]
    [[ -f "${case_root}/service-active" ]]
    assert_contains "$output" "T3 restarted and healthy"
    if [[ "$name" == sqlite-failure ]]; then
        [[ ! -e "${case_root}/native-called" ]]
    fi
    if [[ "$name" == quick-failure ]]; then
        if grep -Fq "Official fallback" "$output"; then
            printf 'quick native failure unexpectedly invoked fallback\n' >&2
            exit 1
        fi
    fi
    printf 'PASS %s (failure preserved, T3 restored)\n' "$name"
}

run_success_case already-latest noop "$NEW_VERSION" no
run_success_case native-update success "$OLD_VERSION" yes
run_success_case inactive-t3 success "$OLD_VERSION" no
run_failure_case quick-failure quick-fail "OPENCODE UPDATE: FAILED; managed binary remains healthy at ${OLD_VERSION}"
run_failure_case timeout-fallback-failure hang "OPENCODE UPDATE: FAILED; native updater rc=124" 1
run_failure_case sqlite-failure success "SQLite"

make_case timeout-fallback
case_root="$FIXTURE_CASE_ROOT"
output="${case_root}/output.log"
if run_wrapper "$case_root" "$output" hang 2; then
    :
else
    cat "$output" >&2
    exit 1
fi
assert_contains "$output" "total hard timeout after 2s"
assert_contains "$output" "Official fallback verified: ${OLD_VERSION} -> ${NEW_VERSION}"
[[ -f "${case_root}/unrelated-pid" ]]
unrelated_pid="$(<"${case_root}/unrelated-pid")"
kill -0 "$unrelated_pid"
printf 'PASS timeout-fallback (owned updater stopped; unrelated OpenCode process survived)\n'
kill -TERM "$unrelated_pid" 2>/dev/null || true

printf 'All isolated OpenCode wrapper cases passed.\n'

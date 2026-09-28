#!/usr/bin/env bash

set -Eeuo pipefail

SOURCE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
readonly SOURCE_ROOT
readonly T3_SOURCE="${SOURCE_ROOT}/tool-updates/scripts/update-t3.sh"
readonly HELPER_SOURCE="${SOURCE_ROOT}/tool-updates/scripts/update-ops-backup-retention.sh"
readonly OLD_VERSION="0.0.43-nightly.20260927.2365"
readonly ACTIVE_VERSION="0.0.43-nightly.20260928.2375"
ROOT="$(mktemp -d /tmp/update-ops-t3-test.XXXXXX)"
readonly ROOT

cleanup() {
    local file pid
    for file in "${ROOT}"/cases/*/mainpid "${ROOT}"/cases/*/conflict-pid; do
        [[ -f "$file" ]] || continue
        pid="$(<"$file")"
        kill -TERM -- "-${pid}" 2>/dev/null || true
        kill -TERM "$pid" 2>/dev/null || true
    done
    rm -rf -- "$ROOT"
}
trap cleanup EXIT

mkdir -p "${ROOT}/mocks"
cat >"${ROOT}/fake-t3.c" <<'C'
#define _GNU_SOURCE
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static const char *self_version(void) {
    static char path[4096];
    if (readlink("/proc/self/exe", path, sizeof(path) - 1) < 0) return "unknown";
    char *slash = strrchr(path, '/');
    if (!slash) return "unknown";
    *slash = '\0';
    slash = strrchr(path, '/');
    return slash ? slash + 1 : "unknown";
}

int main(int argc, char **argv) {
    if (argc > 1 && strcmp(argv[1], "--version") == 0) {
        printf("t3 v%s\n", self_version());
        return 0;
    }
    if (argc > 1 && strcmp(argv[1], "__service-launcher") == 0) {
        for (;;) pause();
    }
    if (argc > 1 && strcmp(argv[1], "update") == 0) {
        const char *root = getenv("FIXTURE_ROOT");
        const char *version = getenv("FIXTURE_TARGET_VERSION");
        if (getenv("FIXTURE_NATIVE_FAIL")) return 33;
        if (!root || !version) return 34;
        char target[4096], launcher[4096], state[4096], temporary[8192];
        snprintf(target, sizeof(target), "%s/home/ubuntu/.t3/runtime/versions/%s/t3", root, version);
        snprintf(launcher, sizeof(launcher), "%s/home/ubuntu/.local/bin/t3", root);
        snprintf(state, sizeof(state), "%s/home/ubuntu/.t3/runtime/service-state.json", root);
        snprintf(temporary, sizeof(temporary), "%s.tmp", launcher);
        unlink(temporary);
        if (symlink(target, temporary) || rename(temporary, launcher)) return 35;
        FILE *file = fopen(state, "w");
        if (!file) return 36;
        fprintf(file, "{\"activeVersion\":\"%s\"}\n", version);
        return fclose(file) ? 37 : 0;
    }
    return 0;
}
C

cat >"${ROOT}/mocks/systemctl" <<'MOCK'
#!/usr/bin/env bash
set -euo pipefail
root="$FIXTURE_ROOT"
if [[ "$1" != --user ]]; then exit 90; fi
shift
case "$1" in
    is-active)
        shift
        quiet=0
        [[ "${1:-}" == --quiet ]] && { quiet=1; shift; }
        if [[ -f "$root/service-active" ]]; then
            ((quiet)) || printf 'active\n'
            exit 0
        fi
        ((quiet)) || printf 'inactive\n'
        exit 3
        ;;
    show)
        shift
        [[ "$1" == t3code.service && "$2" == -p ]] || exit 91
        case "$3" in
            ExecStart) cat "$root/execstart" ;;
            MainPID)
                if [[ -f "$root/service-active" ]]; then cat "$root/mainpid"; else printf '0\n'; fi
                ;;
            *) exit 92 ;;
        esac
        ;;
    start)
        [[ ! -f "$root/start-fails" ]] || exit 94
        runtime="$(cat "$root/start-runtime" 2>/dev/null || cat "$root/exec-target")"
        setsid "$runtime" __service-launcher >/dev/null 2>&1 &
        pid=$!
        printf '%s\n' "$pid" >"$root/mainpid"
        touch "$root/service-active"
        ;;
    stop)
        if [[ -f "$root/mainpid" ]]; then
            pid="$(<"$root/mainpid")"
            kill -TERM -- "-${pid}" 2>/dev/null || true
            kill -TERM "$pid" 2>/dev/null || true
        fi
        rm -f "$root/service-active" "$root/mainpid"
        ;;
    status)
        printf 'mock t3code.service status\n'
        ;;
    *) exit 93 ;;
esac
MOCK

cat >"${ROOT}/mocks/journalctl" <<'MOCK'
#!/usr/bin/env bash
printf 'mock recent t3code.service journal\n'
MOCK

cat >"${ROOT}/mocks/curl" <<'MOCK'
#!/usr/bin/env bash
if [[ "${FIXTURE_HTTP_STATUS:-200}" == 200 ]]; then printf '200'; else printf '%s' "$FIXTURE_HTTP_STATUS"; fi
MOCK
chmod +x "${ROOT}/mocks/"*

make_case() {
    local name="$1" case_root versions version target exec_target
    case_root="${ROOT}/cases/${name}"
    versions="${case_root}/home/ubuntu/.t3/runtime/versions"
    mkdir -p "$versions" "${case_root}/home/ubuntu/.local/bin" \
        "${case_root}/home/ubuntu/.t3/caches" "${case_root}/home/ubuntu/.config/systemd/user" \
        "${case_root}/home/ubuntu/.local/state/tool-updates" \
        "${case_root}/home/ubuntu/.opencode/bin"
    for version in 0.0.43-nightly.20260925.2344 "$OLD_VERSION" "$ACTIVE_VERSION"; do
        mkdir -p "${versions}/${version}"
        gcc -O2 "${ROOT}/fake-t3.c" -o "${versions}/${version}/t3"
    done
    cp "$HELPER_SOURCE" "${case_root}/helper.sh"
    python3 - "$T3_SOURCE" "${case_root}/update-t3.sh" "$case_root" <<'PY'
from pathlib import Path
import sys
source, destination, root = map(Path, sys.argv[1:])
text = source.read_text()
replacements = {
    "/home/ubuntu/.t3": str(root / "home/ubuntu/.t3"),
    "/home/ubuntu/.local/bin/t3": str(root / "home/ubuntu/.local/bin/t3"),
    "/home/ubuntu/.opencode/bin/opencode": str(root / "home/ubuntu/.opencode/bin/opencode"),
    "/home/ubuntu/.local/state/tool-updates": str(root / "home/ubuntu/.local/state/tool-updates"),
    "/home/ubuntu/tool-updates/scripts/update-ops-backup-retention.sh": str(root / "helper.sh"),
}
for old, new in replacements.items():
    text = text.replace(old, new)
destination.write_text(text)
destination.chmod(0o700)
PY
    cp /bin/true "${case_root}/home/ubuntu/.opencode/bin/opencode"
    printf '{"status":"ready","version":"test"}\n' >"${case_root}/home/ubuntu/.t3/caches/opencode.json"
    printf '{"activeVersion":"%s"}\n' "$ACTIVE_VERSION" >"${case_root}/home/ubuntu/.t3/runtime/service-state.json"
    ln -s "${versions}/${OLD_VERSION}/t3" "${case_root}/home/ubuntu/.local/bin/t3"
    target="${versions}/${ACTIVE_VERSION}/t3"
    exec_target="{ path=${target} ; argv[]=${target} __service-launcher ; ignore_errors=no ; }"
    printf '%s\n' "$target" >"${case_root}/exec-target"
    printf '%s\n' "$exec_target" >"${case_root}/execstart"
    printf '%s\n' "$target" >"${case_root}/start-runtime"
    FIXTURE_CASE_ROOT="$case_root"
}

run_update() {
    local case_root="$1" output="$2" status=0
    FIXTURE_ROOT="$case_root" FIXTURE_TARGET_VERSION="$ACTIVE_VERSION" \
        FIXTURE_HTTP_STATUS="${FIXTURE_HTTP_STATUS:-200}" START_TIMEOUT=2 \
        PATH="${ROOT}/mocks:$PATH" bash "${case_root}/update-t3.sh" update >"$output" 2>&1 || status=$?
    return "$status"
}

assert_contains() {
    grep -Fq -- "$2" "$1" || { cat "$1" >&2; printf 'missing expected log: %s\n' "$2" >&2; exit 1; }
}

run_pass_case() {
    local name="$1" drift="$2" inactive="$3" case_root output pid
    make_case "$name"
    case_root="$FIXTURE_CASE_ROOT"
    output="${case_root}/output.log"
    if [[ "$drift" == latest ]]; then
        ln -sfn "${case_root}/home/ubuntu/.t3/runtime/versions/${ACTIVE_VERSION}/t3" \
            "${case_root}/home/ubuntu/.local/bin/t3"
    fi
    if [[ "$drift" == active || "$drift" == latest ]]; then
        FIXTURE_ROOT="$case_root" "${case_root}/home/ubuntu/.t3/runtime/versions/${ACTIVE_VERSION}/t3" \
            __service-launcher >/dev/null 2>&1 &
        pid=$!
        printf '%s\n' "$pid" >"${case_root}/mainpid"
        touch "${case_root}/service-active"
    fi
    if [[ "$inactive" == yes ]]; then
        if ! run_update "$case_root" "$output"; then cat "$output" >&2; return 1; fi
        assert_contains "$output" "Repairable inactive T3 service detected"
        assert_contains "$output" "Automatic T3 service recovery verified"
    else
        if ! run_update "$case_root" "$output"; then cat "$output" >&2; return 1; fi
    fi
    assert_contains "$output" "T3 core verification: PASS"
    [[ "$(readlink -f "${case_root}/home/ubuntu/.local/bin/t3")" == \
        "${case_root}/home/ubuntu/.t3/runtime/versions/${ACTIVE_VERSION}/t3" ]]
    [[ -d "${case_root}/home/ubuntu/.t3/runtime/versions/${ACTIVE_VERSION}" ]]
    [[ -d "${case_root}/home/ubuntu/.t3/runtime/versions/${OLD_VERSION}" ]]
    [[ ! -d "${case_root}/home/ubuntu/.t3/runtime/versions/0.0.43-nightly.20260925.2344" ]]
    printf 'PASS %s\n' "$name"
}

run_fail_case() {
    local name="$1" expected="$2" case_root output status
    make_case "$name"
    case_root="$FIXTURE_CASE_ROOT"
    output="${case_root}/output.log"
    case "$name" in
        missing-runtime)
            rm -rf "${case_root}/home/ubuntu/.t3/runtime/versions/${ACTIVE_VERSION}"
            ;;
        unsafe-symlink)
            mkdir -p "${case_root}/outside-runtime"
            gcc -O2 "${ROOT}/fake-t3.c" -o "${case_root}/outside-runtime/t3"
            rm -rf "${case_root}/home/ubuntu/.t3/runtime/versions/${ACTIVE_VERSION}"
            ln -s "${case_root}/outside-runtime" \
                "${case_root}/home/ubuntu/.t3/runtime/versions/${ACTIVE_VERSION}"
            ;;
        wrong-execstart)
            printf '{ path=%s ; argv[]=%s __service-launcher ; }\n' \
                "${case_root}/home/ubuntu/.t3/runtime/versions/${OLD_VERSION}/t3" \
                "${case_root}/home/ubuntu/.t3/runtime/versions/${OLD_VERSION}/t3" >"${case_root}/execstart"
            ;;
        conflicting-process)
            setsid "${case_root}/home/ubuntu/.t3/runtime/versions/${OLD_VERSION}/t3" __service-launcher \
                >/dev/null 2>&1 &
            printf '%s\n' "$!" >"${case_root}/conflict-pid"
            sleep 0.1
            ;;
        failed-update-marker)
            printf '{"activeVersion":"%s","update":{"status":"failed"}}\n' "$ACTIVE_VERSION" \
                >"${case_root}/home/ubuntu/.t3/runtime/service-state.json"
            ;;
        start-failure)
            touch "${case_root}/start-fails"
            ;;
        http-failure)
            FIXTURE_HTTP_STATUS=503
            ;;
        wrong-main-executable)
            printf '%s\n' "${case_root}/home/ubuntu/.t3/runtime/versions/${OLD_VERSION}/t3" >"${case_root}/start-runtime"
            ;;
    esac
    cp "${case_root}/home/ubuntu/.t3/runtime/service-state.json" "${case_root}/state.before"
    if run_update "$case_root" "$output"; then
        printf 'expected hard failure for %s\n' "$name" >&2
        exit 1
    else
        status=$?
    fi
    ((status != 0))
    [[ -s "$output" ]] || { printf 'missing updater diagnostics for %s\n' "$name" >&2; exit 1; }
    assert_contains "$output" "$expected"
    if [[ "$name" == http-failure || "$name" == wrong-main-executable ]]; then
        assert_contains "$output" "mock recent t3code.service journal"
    fi
    if [[ "$name" == start-failure ]]; then
        assert_contains "$output" "mock recent t3code.service journal"
        assert_contains "$output" "MainPID=0"
        assert_contains "$output" "Could not start t3code.service"
    fi
    cmp -s "${case_root}/state.before" "${case_root}/home/ubuntu/.t3/runtime/service-state.json" || {
        printf 'test fixture service state changed before native updater for %s\n' "$name" >&2
        exit 1
    }
    if [[ -f "${case_root}/service-active" && "$name" != wrong-main-executable && "$name" != http-failure ]]; then
        printf 'unexpected service start in %s\n' "$name" >&2
        exit 1
    fi
    FIXTURE_HTTP_STATUS=200
    printf 'PASS %s (hard failure)\n' "$name"
}

run_pass_case already-latest latest no
run_pass_case active-drift active no
run_pass_case inactive-drift inactive yes
run_fail_case missing-runtime "not a usable executable"
run_fail_case unsafe-symlink "not a usable executable"
run_fail_case wrong-execstart "systemd ExecStart does not exactly target"
run_fail_case conflicting-process "Inactive T3 service has a live T3 runtime process"
run_fail_case failed-update-marker "failed or pending native T3 update marker"
run_fail_case start-failure "Could not start t3code.service"
run_fail_case http-failure "did not pass bounded process, ExecStart, conflict, and HTTP health verification"
run_fail_case wrong-main-executable "did not pass bounded process, ExecStart, conflict, and HTTP health verification"

printf 'All isolated T3 recovery cases passed.\n'

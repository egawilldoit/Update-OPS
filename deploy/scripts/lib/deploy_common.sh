#!/usr/bin/env bash
# EGA Update Console — shared deploy helpers (W4-D8).
#
# ONE implementation of the pre-undrain acceptance gate consumed by BOTH
# install.sh and upgrade.sh. The canonical readiness definition lives in
# backend/app/deployment_readiness.py and is exposed ONLY through
# `python -m backend.app.cli status --require-ready`; the shell never
# parses or re-implements readiness semantics — the process exit code is
# the contract (N01 pattern). The advisory loopback probe only paces
# retries while the API is still binding; it is NEVER sufficient for
# success.
#
# Sourced by install.sh/upgrade.sh after SCRIPT_DIR is resolved.

# Advisory reachability code (not a gate).
ega_advisory_http_probe() {
  local port="$1"
  curl -s -o /dev/null -w '%{http_code}' \
    "http://127.0.0.1:$port/api/v1/health" 2>/dev/null || printf '000'
}

# ega_wait_for_readiness <release_dir> <config_file> <port> <report_path>
#                        <label> [iterations] [sleep_s]
# Bounded loop over the canonical readiness gate. Returns 0 only when the
# gate exits 0 (ALL mandatory stages proven). On failure the machine-
# readable report and the gate's stderr diagnosis are left at
# <report_path> / <report_path>.err for the caller's failure path.
ega_wait_for_readiness() {
  local release_dir="$1" config_file="$2" port="$3" report="$4"
  local label="$5"
  local iters="${6:-12}" nap="${7:-5}"
  local i=0 code=""
  while [ "$i" -lt "$iters" ]; do
    i=$((i + 1))
    code="$(ega_advisory_http_probe "$port")"
    if ( cd "$release_dir" && EGA_CONFIG_FILE="$config_file" \
         "$release_dir/venv/bin/python" -m backend.app.cli status --require-ready \
       ) >"$report" 2>"${report}.err"; then
      return 0
    fi
    echo "[$label] readiness pending (http=$code, $i/$iters) — failed stages:" >&2
    sed -n 's/^not ready: failed stages: //p' "${report}.err" >&2 2>/dev/null || true
    sleep "$nap"
  done
  return 1
}

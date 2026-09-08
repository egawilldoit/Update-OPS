#!/bin/sh
# Fake installer for future fault-injection runs (offline, no network).
# Emits fixed version strings; nonzero-exit mode via FAKE_TOOL_FAIL env.
# Usage:
#   fake-tool.sh --version        -> prints FAKE_TOOL_VERSION (default 9.9.9)
#   fake-tool.sh upgrade TARGET   -> prints upgrade line, exit 0
#   FAKE_TOOL_FAIL=1 ...          -> prints error to stderr, exit 4
#   FAKE_TOOL_VERSION=1.2.3 ...   -> overrides the reported version
set -eu
VERSION="${FAKE_TOOL_VERSION:-9.9.9}"
FAIL="${FAKE_TOOL_FAIL:-0}"
case "${1:-}" in
  --version|-v|version)
    if [ "$FAIL" != "0" ]; then
      echo "fake-tool: version probe failed" >&2
      exit 4
    fi
    echo "fake-tool $VERSION"
    ;;
  upgrade)
    TARGET="${2:-unknown}"
    if [ "$FAIL" != "0" ]; then
      echo "fake-tool: upgrade to $TARGET failed (injected fault)" >&2
      exit 4
    fi
    echo "fake-tool upgraded to $TARGET (from $VERSION)"
    ;;
  --help|-h|"")
    echo "usage: fake-tool.sh [--version|upgrade TARGET]"
    ;;
  *)
    echo "fake-tool: invalid request: $1" >&2
    exit 2
    ;;
esac

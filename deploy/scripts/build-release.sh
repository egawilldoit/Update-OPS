#!/usr/bin/env bash
# Canonical byte-reproducible release packaging (W10 audit fix).
#
# Usage:
#   deploy/scripts/build-release.sh <commit-sha> <output-dir>
#   deploy/scripts/build-release.sh --verify <artifact.tar.gz>
#
# Produces <output-dir>/ega-update-<commit>.tar.gz (install.sh /
# upgrade.sh --release-tarball input) plus the external
# <artifact>.sha256. The implementation (deploy/scripts/build_release.py)
# stages the exact `git archive <sha>` tree — never the dirty working
# directory — builds the frontend with lockfile-resolved `npm ci
# --prefer-offline` + `npm run build` inside that staging tree, writes
# RELEASE_IDENTITY.json / MANIFEST / MANIFEST.sha256, and emits the
# deterministic tar.gz (normalized owners, mtimes = SOURCE_DATE_EPOCH,
# sorted names, `gzip -n`). It then self-verifies fail-closed; a
# nonzero exit means the artifact was not produced/verified.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

if ! command -v python3 >/dev/null 2>&1; then
  echo "[build-release] REFUSING: python3 (>=3.10) is required" >&2
  exit 2
fi
if ! python3 -c 'import sys; raise SystemExit(0 if (3, 10) <= sys.version_info < (3, 14) else 1)'; then
  echo "[build-release] REFUSING: python3 >=3.10,<3.14 required (got: $(python3 --version 2>&1))" >&2
  exit 2
fi

exec python3 "$SCRIPT_DIR/build_release.py" --repo "$REPO_ROOT" "$@"

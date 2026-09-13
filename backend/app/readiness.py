"""Startup/readiness validation (R34). Python 3.10 compatible.

Placeholders fail readiness: no usable secret or identity value may remain
CHANGEME/empty where required. Per-service secret files are validated for
readability by the calling identity (os.access as current euid at service
startup; the deploy validator checks ownership/modes as root).

Roles: api | worker | runner | deploy.
"""
from __future__ import annotations

import os
from typing import List

PLACEHOLDER_PREFIXES = ("CHANGEME", "REPLACE-ME", "EXAMPLE", "TODO")

# D2 classification: every check validate_startup() currently performs is
# FATAL. The service either cannot authenticate/authorize (placeholder
# team_domain/audience/owner_emails/public_origin), cannot mint CSRF tokens
# (unresolved csrf_secret), cannot redact/durably record (unreadable
# secrets_file), cannot trust installation facts (unparsable inventory),
# cannot launch an owner process (missing executable / unresolvable
# release), or cannot read its own config (deploy role's EGA_CONFIG_FILE).
# None is advisory, so all land in `violations` and raise ReadinessError.
# The `warnings` channel is preserved for genuinely non-fatal advisories;
# it is currently always empty. There is no "report-only" mode: a caller
# that silently ignores fatal violations would serve unsafe traffic.


class ReadinessError(Exception):
    """Startup must fail; carries the list of violations."""

    def __init__(self, violations):
        # type: (List[str]) -> None
        super(ReadinessError, self).__init__(
            "readiness failed: %s" % "; ".join(violations[:10]))
        self.violations = list(violations)


def is_placeholder(value):
    # type: (object) -> bool
    try:
        text = str(value or "").strip()
    except Exception:
        return True
    if not text:
        return True
    upper = text.upper()
    return any(upper.startswith(prefix) for prefix in PLACEHOLDER_PREFIXES)


def check_readable(path, label, violations):
    # type: (str, str, list) -> None
    if not path:
        violations.append("%s: no path configured" % label)
        return
    if is_placeholder(path):
        violations.append("%s: placeholder path" % label)
        return
    if not os.path.isfile(path):
        violations.append("%s: missing file %s" % (label, path))
        return
    if not os.access(path, os.R_OK):
        violations.append("%s: unreadable by uid=%d: %s"
                          % (label, _euid(), path))


def _euid():
    # type: () -> int
    try:
        return os.geteuid()
    except Exception:
        return -1


def validate_startup(role="api", settings=None):
    # type: (str, object) -> List[str]
    """Validate readiness for role.

    Every violation this function detects is FATAL (see module-level
    classification note) and raises ReadinessError carrying the full
    `violations` list, so API/worker startup fails closed. Returns the
    non-fatal `warnings` list (currently always empty) on success.
    Messages name fields/paths only; secret CONTENTS are never included.
    """
    try:
        from .config import settings as _defaults
    except Exception:
        _defaults = None
    s = settings if settings is not None else _defaults
    violations = []  # type: List[str]
    warnings = []  # type: List[str]
    if s is None:
        raise ReadinessError(["settings unavailable"])
    if role in ("api", "deploy"):
        if is_placeholder(getattr(s, "team_domain", "")):
            violations.append("team_domain is placeholder/missing")
        if is_placeholder(getattr(s, "audience", "")):
            violations.append("audience is placeholder/missing")
        if not list(getattr(s, "owner_emails", []) or []):
            violations.append("owner_emails empty")
        if is_placeholder(getattr(s, "public_origin", "")):
            violations.append("public_origin is placeholder/missing")
    if role == "api":
        secret = str(getattr(s, "csrf_secret", "") or "")
        if is_placeholder(secret):
            # csrf.secret file fallback lives in config loading; at this
            # point an empty/placeholder value means the API cannot mint
            # CSRF tokens safely.
            violations.append("csrf_secret unresolved "
                              "(secret file unreadable or placeholder)")
    if role in ("worker", "runner", "deploy"):
        secrets_file = str(getattr(s, "secrets_file", "") or "")
        if not secrets_file or is_placeholder(secrets_file):
            violations.append("secrets_file is placeholder/missing")
        else:
            check_readable(secrets_file, "secrets_file", violations)
    if role in ("worker", "deploy"):
        try:
            from .inventory import load_inventory
            load_inventory()
        except ValueError as exc:
            violations.append("inventory: %s" % exc)
    if role in ("worker", "runner", "deploy"):
        try:
            from .owner_env import resolved_paths, validate_executables
            paths = resolved_paths(s)
            ok, missing = validate_executables(paths)
            if not ok:
                violations.append("required executables missing: %s"
                                  % ", ".join(missing))
        except ValueError as exc:
            violations.append("release: %s" % exc)
        except Exception as exc:
            violations.append("owner env: %s" % exc)
    if role == "deploy":
        cfg = os.environ.get("EGA_CONFIG_FILE", "") or \
            "/etc/ega-update/config.json"
        check_readable(cfg, "EGA_CONFIG_FILE", violations)
    if violations:
        raise ReadinessError(violations)
    return warnings

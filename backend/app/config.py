"""Central configuration. Python 3.10 compatible.

Values come from the environment (prefix ``EGA_``) and optionally from a JSON
file pointed at by ``EGA_CONFIG_FILE`` (owner-managed ``/etc/ega-update/``).
Secrets stay outside shared storage; this module never logs secret values.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple


def _env(name, default=""):
    # type: (str, str) -> str
    return os.environ.get(name, default)


def _env_int(name, default):
    # type: (str, int) -> int
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _coerce_int(value, default):
    # type: (object, int) -> int
    """Coerce a JSON-file value to int without dropping non-strings.

    Accepts ints (excluding bool) and numeric strings. Anything else
    falls back to the default so a malformed file key never crashes
    startup and never silently becomes zero.
    """
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        try:
            return int(value)
        except (TypeError, ValueError, OverflowError):
            return default
    if isinstance(value, str):
        try:
            return int(value.strip())
        except (TypeError, ValueError):
            return default
    return default


@dataclass
class Settings:
    # Cloudflare Access
    team_domain: str = ""
    audience: str = ""
    owner_emails: List[str] = field(default_factory=list)
    jwks_cache_ttl_s: int = 600
    # Web / CSRF
    public_origin: str = ""
    listen_host: str = "127.0.0.1"
    listen_port: int = 8771
    csrf_secret: str = ""
    csrf_secret_file: str = "/etc/ega-update/csrf.secret"
    # State
    state_dir: str = "/var/lib/ega-update"
    db_path: str = "/var/lib/ega-update/state.db"
    log_dir: str = "/var/lib/ega-update/logs"
    backup_dir: str = "/var/lib/ega-update/backups"
    # Limits
    plan_ttl_s: int = 300
    worker_claim_s: int = 10
    log_poll_s: int = 2
    per_job_log_cap_bytes: int = 20 * 1024 * 1024
    total_log_cap_bytes: int = 500 * 1024 * 1024
    completed_log_retention_days: int = 30
    metadata_retention_days: int = 90
    disk_floor_bytes: int = 3 * 1024 * 1024 * 1024
    reserve_bytes: int = 1 * 1024 * 1024 * 1024
    rate_limit_per_min: int = 60
    body_limit_bytes: int = 256 * 1024
    # Tool owner / runner
    tool_owner: str = "ubuntu"
    node_path: str = "/home/ubuntu/.nvm/versions/node/v24.18.0/bin/node"
    npm_path: str = "/home/ubuntu/.nvm/versions/node/v24.18.0/bin/npm"
    npx_path: str = "/home/ubuntu/.nvm/versions/node/v24.18.0/bin/npx"
    # Secrets file (one secret per line; never logged)
    secrets_file: str = "/etc/ega-update/secrets.env"
    # Inventory file (deploy/etc/inventory.json content installed here).
    inventory_file: str = "/etc/ega-update/inventory.json"
    # Deploy-owned passthroughs (mirror deploy/etc/config.example.json).
    # service_units: console + inventoried units (deploy-managed).
    # adapter_timeouts_s: per-step ceilings overlaying ADAPTER_TIMEOUT_DEFAULTS.
    service_units: Dict[str, Any] = field(default_factory=dict)
    adapter_timeouts_s: Dict[str, Any] = field(default_factory=dict)


def load_settings():
    # type: () -> Settings
    cfg_file = os.environ.get("EGA_CONFIG_FILE", "")
    data = {}
    if cfg_file and os.path.exists(cfg_file):
        with open(cfg_file, "r", encoding="utf-8") as fh:
            loaded = json.load(fh)
            if isinstance(loaded, dict):
                data = loaded

    def pick(env_name, file_key, default=""):
        # type: (str, str, str) -> str
        if env_name in os.environ:
            return os.environ[env_name]
        val = data.get(file_key, default)
        return val if isinstance(val, str) else default

    def pick_int(env_name, file_key, default):
        # type: (str, str, int) -> int
        if env_name in os.environ:
            try:
                return int(str(os.environ[env_name]).strip())
            except (TypeError, ValueError):
                return default
        if file_key in data:
            return _coerce_int(data.get(file_key), default)
        return default

    # Env wins over file for the owner allow-list. Env is comma-separated;
    # the file may hold either a list or a comma-separated string.
    if "EGA_OWNER_EMAILS" in os.environ:
        owners = [e.strip() for e in
                  str(os.environ["EGA_OWNER_EMAILS"]).split(",") if e.strip()]
    elif isinstance(data.get("owner_emails"), list):
        owners = [str(x).strip() for x in data["owner_emails"] if str(x).strip()]
    else:
        raw = data.get("owner_emails", "")
        owners = [e.strip() for e in str(raw).split(",") if e.strip()]

    s = Settings()
    s.team_domain = pick("EGA_TEAM_DOMAIN", "team_domain")
    s.audience = pick("EGA_AUDIENCE", "audience")
    s.owner_emails = owners
    s.jwks_cache_ttl_s = pick_int(
        "EGA_JWKS_CACHE_TTL_S", "jwks_cache_ttl_s", s.jwks_cache_ttl_s)
    s.public_origin = pick("EGA_PUBLIC_ORIGIN", "public_origin")
    s.listen_host = pick("EGA_LISTEN_HOST", "listen_host") or s.listen_host
    s.listen_port = pick_int("EGA_LISTEN_PORT", "listen_port", s.listen_port)
    s.csrf_secret = pick("EGA_CSRF_SECRET", "csrf_secret")
    s.csrf_secret_file = pick(
        "EGA_CSRF_SECRET_FILE", "csrf_secret_file",
        s.csrf_secret_file) or s.csrf_secret_file
    # csrf.secret file fallback: when csrf_secret is empty, read the first
    # non-empty line of csrf_secret_file (default
    # /etc/ega-update/csrf.secret). Never logs values; missing/unreadable
    # file leaves the secret empty (fail-closed downstream).
    if not s.csrf_secret:
        _csrf_path = s.csrf_secret_file or "/etc/ega-update/csrf.secret"
        try:
            if isinstance(_csrf_path, str) and _csrf_path:
                with open(_csrf_path, "r", encoding="utf-8",
                          errors="replace") as _fh:
                    for _line in _fh.read().splitlines():
                        _stripped = _line.strip()
                        if _stripped:
                            s.csrf_secret = _stripped
                            break
        except OSError:
            pass
        except Exception:
            pass
    s.state_dir = pick("EGA_STATE_DIR", "state_dir") or s.state_dir
    s.db_path = pick("EGA_DB_PATH", "db_path") or s.db_path
    s.log_dir = pick("EGA_LOG_DIR", "log_dir") or s.log_dir
    s.backup_dir = pick("EGA_BACKUP_DIR", "backup_dir") or s.backup_dir
    s.plan_ttl_s = pick_int("EGA_PLAN_TTL_S", "plan_ttl_s", s.plan_ttl_s)
    s.worker_claim_s = pick_int(
        "EGA_WORKER_CLAIM_S", "worker_claim_s", s.worker_claim_s)
    s.log_poll_s = pick_int("EGA_LOG_POLL_S", "log_poll_s", s.log_poll_s)
    s.per_job_log_cap_bytes = pick_int(
        "EGA_PER_JOB_LOG_CAP_BYTES", "per_job_log_cap_bytes",
        s.per_job_log_cap_bytes)
    s.total_log_cap_bytes = pick_int(
        "EGA_TOTAL_LOG_CAP_BYTES", "total_log_cap_bytes",
        s.total_log_cap_bytes)
    s.completed_log_retention_days = pick_int(
        "EGA_COMPLETED_LOG_RETENTION_DAYS", "completed_log_retention_days",
        s.completed_log_retention_days)
    s.metadata_retention_days = pick_int(
        "EGA_METADATA_RETENTION_DAYS", "metadata_retention_days",
        s.metadata_retention_days)
    s.disk_floor_bytes = pick_int(
        "EGA_DISK_FLOOR_BYTES", "disk_floor_bytes", s.disk_floor_bytes)
    s.reserve_bytes = pick_int(
        "EGA_RESERVE_BYTES", "reserve_bytes", s.reserve_bytes)
    s.rate_limit_per_min = pick_int(
        "EGA_RATE_LIMIT_PER_MIN", "rate_limit_per_min",
        s.rate_limit_per_min)
    s.body_limit_bytes = pick_int(
        "EGA_BODY_LIMIT_BYTES", "body_limit_bytes", s.body_limit_bytes)
    s.tool_owner = pick("EGA_TOOL_OWNER", "tool_owner") or s.tool_owner
    s.node_path = pick("EGA_NODE_PATH", "node_path") or s.node_path
    s.npm_path = pick("EGA_NPM_PATH", "npm_path") or s.npm_path
    s.npx_path = pick("EGA_NPX_PATH", "npx_path") or s.npx_path
    s.secrets_file = pick(
        "EGA_SECRETS_FILE", "secrets_file", s.secrets_file) or s.secrets_file
    s.inventory_file = pick(
        "EGA_INVENTORY_FILE", "inventory_file",
        s.inventory_file) or s.inventory_file
    # service_units dict passthrough (file key service_units; optional JSON
    # object in EGA_SERVICE_UNITS wins). Unparsable shapes yield {}.
    _units = {}  # type: Dict[str, Any]
    try:
        if "EGA_SERVICE_UNITS" in os.environ:
            _parsed_units = json.loads(
                str(os.environ["EGA_SERVICE_UNITS"] or "{}"))
            if isinstance(_parsed_units, dict):
                _units = dict(_parsed_units)
        elif isinstance(data.get("service_units"), dict):
            _units = dict(data["service_units"])
    except Exception:
        _units = {}
    s.service_units = _units
    # adapter_timeouts_s dict passthrough (file key adapter_timeouts_s;
    # optional JSON object in EGA_ADAPTER_TIMEOUTS_S wins). Values stay raw
    # here; get_adapter_timeouts() coerces ints over defaults.
    _timeouts = {}  # type: Dict[str, Any]
    try:
        if "EGA_ADAPTER_TIMEOUTS_S" in os.environ:
            _parsed_to = json.loads(
                str(os.environ["EGA_ADAPTER_TIMEOUTS_S"] or "{}"))
            if isinstance(_parsed_to, dict):
                _timeouts = dict(_parsed_to)
        elif isinstance(data.get("adapter_timeouts_s"), dict):
            _timeouts = dict(data["adapter_timeouts_s"])
    except Exception:
        _timeouts = {}
    s.adapter_timeouts_s = _timeouts
    return s


def get_adapter_timeouts(s=None):
    # type: (object) -> Dict[str, int]
    """Merge file adapter_timeouts_s over ADAPTER_TIMEOUT_DEFAULTS.

    Returns ints for preflight/backup/updating/verifying plus any extra
    file keys (e.g. stop_grace_s). Malformed values fall back to the
    default per key; missing overlay yields defaults. Never raises.
    Python 3.10 compatible.
    """
    try:
        from .adapters.base import ADAPTER_TIMEOUT_DEFAULTS as _defaults
        _base = dict(_defaults)
    except Exception:
        _base = {"preflight": 120, "backup": 600, "updating": 1800,
                 "verifying": 300}
    try:
        _obj = s if s is not None else settings
        _overlay = getattr(_obj, "adapter_timeouts_s", {}) or {}
    except Exception:
        _overlay = {}
    out = {}  # type: Dict[str, int]
    try:
        for _k, _v in _base.items():
            try:
                _def = int(_v)
            except Exception:
                continue
            if isinstance(_overlay, dict) and _k in _overlay:
                out[_k] = _coerce_int(_overlay.get(_k), _def)
            else:
                out[_k] = _def
        if isinstance(_overlay, dict):
            for _k, _v in _overlay.items():
                if _k in out:
                    continue
                try:
                    _coerced = _coerce_int(_v, -1)
                except Exception:
                    continue
                if isinstance(_coerced, int) and _coerced >= 0:
                    out[str(_k)] = _coerced
    except Exception:
        pass
    if not out:
        try:
            out = {str(_k): int(_v) for _k, _v in _base.items()}
        except Exception:
            out = {"preflight": 120, "backup": 600, "updating": 1800,
                   "verifying": 300}
    return out


class SecretSourceError(Exception):
    """A CONFIGURED secret source failed to load (G05).

    Distinct from 'no secrets configured' (empty secrets_file, which
    validly yields ()). Durable evidence paths must fail closed on
    this error — never degrade to an empty secret set and persist.
    """


def load_secret_values(s):
    # type: (object) -> Tuple[str, ...]
    """Return known secret values for redaction (never logs values).

    Strict (G05): values parsed with the single shared parser
    (sanitize.parse_secrets_content). NO source configured to THIS
    helper (attribute missing, None, or "") means no secrets by
    design and yields ``()``. A CONFIGURED path that is missing,
    unreadable, non-string, or unparsable raises SecretSourceError —
    callers on durable evidence paths must fail closed instead of
    persisting with zero secrets. The V1 deployment default
    (``/etc/ega-update/secrets.env``) lives in Settings/config
    loading, not here: normal Settings always carry a configured
    path, so deployed worker/API paths stay fail-closed. Callers
    pass the values to redaction only, never to logs or error
    details.
    """
    try:
        path = getattr(s, "secrets_file", "")
    except Exception as exc:
        raise SecretSourceError("settings unreadable: %s" % exc)
    if path is None or path == "":
        return ()
    if not isinstance(path, str):
        raise SecretSourceError(
            "secret source is not a path: %r" % (type(path).__name__,))
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            content = fh.read()
    except OSError as exc:
        raise SecretSourceError(
            "secret file unreadable: %s" % str(exc)[:200])
    except Exception as exc:
        raise SecretSourceError(
            "secret source failed: %s" % str(exc)[:200])
    try:
        from .sanitize import parse_secrets_content
        return parse_secrets_content(content)
    except Exception as exc:
        raise SecretSourceError("secret parse failed: %s" % exc)


settings = load_settings()

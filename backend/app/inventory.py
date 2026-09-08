"""Inventory + configuration identity contract (R15/R34, SC-01).

Inventory (deploy/etc/inventory.json, validated against
deploy/etc/inventory.schema.json) is the only source of real installation
facts. Adapters read per-tool inventory via get_tool_inventory(); missing
inventory is {} and callers fail closed (never invent facts).

config_identity() binds the effective configuration + inventory subset into
a stable hash stored on every plan; execution re-checks it and blocks on
drift instead of silently mixing saved fields with fresh defaults.
Python 3.10 compatible.
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Dict

KNOWN_TOOLS = ("hermes", "opencode", "codex", "t3")

DEFAULT_INVENTORY_PATH = "/etc/ega-update/inventory.json"

_inventory_cache = {"path": "", "mtime": 0.0, "data": {}}  # type: dict


def inventory_path(settings=None):
    # type: (object) -> str
    try:
        if settings is not None:
            val = getattr(settings, "inventory_file", "") or ""
            if val:
                return str(val)
    except Exception:
        pass
    try:
        return os.environ.get("EGA_INVENTORY_FILE", "") or \
            DEFAULT_INVENTORY_PATH
    except Exception:
        return DEFAULT_INVENTORY_PATH


def load_inventory(path=""):
    # type: (str) -> Dict[str, Any]
    """Load + structurally validate inventory. Raises ValueError."""
    target = path or inventory_path()
    try:
        with open(target, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        raise ValueError("inventory missing: %s" % target)
    except Exception as exc:
        raise ValueError("inventory unreadable: %s" % exc)
    if not isinstance(data, dict):
        raise ValueError("inventory must be an object")
    tools = data.get("tools", {})
    if not isinstance(tools, dict):
        raise ValueError("inventory.tools must be an object")
    norm = {"tools": {}}  # type: Dict[str, Any]
    for tool_id, entry in tools.items():
        if tool_id not in KNOWN_TOOLS:
            continue
        if not isinstance(entry, dict):
            raise ValueError("inventory.tools.%s must be an object"
                             % tool_id)
        norm["tools"][tool_id] = dict(entry)
    for key in ("generated_at", "generated_by", "vm_notes"):
        if key in data:
            norm[key] = data[key]
    return norm


def get_inventory():
    # type: () -> Dict[str, Any]
    """Cached inventory; {} when unavailable (callers fail closed)."""
    path = inventory_path()
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    if _inventory_cache.get("path") == path and \
            _inventory_cache.get("mtime") == mtime and \
            isinstance(_inventory_cache.get("data"), dict):
        return _inventory_cache["data"]  # type: ignore[return-value]
    try:
        data = load_inventory(path)
    except ValueError:
        return {}
    _inventory_cache["path"] = path
    _inventory_cache["mtime"] = mtime
    _inventory_cache["data"] = data
    return data


def get_tool_inventory(tool_id):
    # type: (str) -> Dict[str, Any]
    """Frozen contract consumed by adapters. {} means uninventoried."""
    try:
        tools = get_inventory().get("tools", {})
        entry = tools.get(tool_id, {})
        return dict(entry) if isinstance(entry, dict) else {}
    except Exception:
        return {}


def config_identity(settings=None, inventory=None):
    # type: (object, object) -> str
    """Stable hash of effective config + inventory subset (plan binding)."""
    try:
        from .config import settings as _defaults
    except Exception:
        _defaults = None
    s = settings if settings is not None else _defaults
    inv = inventory if inventory is not None else get_inventory()
    try:
        subset = {
            "tool_owner": getattr(s, "tool_owner", ""),
            "node_path": getattr(s, "node_path", ""),
            "npm_path": getattr(s, "npm_path", ""),
            "npx_path": getattr(s, "npx_path", ""),
            "service_units": getattr(s, "service_units", {}) or {},
            "adapter_timeouts_s": getattr(s, "adapter_timeouts_s", {})
            or {},
            "disk_floor_bytes": getattr(s, "disk_floor_bytes", 0),
            "reserve_bytes": getattr(s, "reserve_bytes", 0),
            "inventory_tools": (inv or {}).get("tools", {}),
        }
        raw = json.dumps(subset, sort_keys=True, default=str).encode(
            "utf-8")
        return hashlib.sha256(raw).hexdigest()
    except Exception:
        return ""

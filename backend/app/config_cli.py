"""Release-local config reader for deploy scripts (N16).

Usage:
  <release>/venv/bin/python -m backend.app.config_cli get <key>
  <release>/venv/bin/python -m backend.app.config_cli get --require <key>
  <release>/venv/bin/python -m backend.app.config_cli json

Exactly one config parser for deployment (no inline `python -c` config
fragments in shell). EGA_CONFIG_FILE is required. `--require` (or the
json command) makes an empty value exit 3; plain `get` prints whatever
is configured (possibly empty) and only fails when the config itself
is unloadable. Shell callers must fail explicitly on empty required
values — never silent defaults. Exits: 0 ok, 2 usage, 3 blocked.

Python 3.10 compatible. Read-only: never writes, never migrates.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

REQUIRED_KEYS = ("state_dir", "db_path", "log_dir", "backup_dir",
                 "listen_port", "inventory_file")

EXIT_OK = 0
EXIT_INVALID = 2
EXIT_BLOCKED = 3


def _load():
    # type: () -> object
    cfg = os.environ.get("EGA_CONFIG_FILE", "")
    if not cfg:
        raise ValueError("EGA_CONFIG_FILE is not set")
    if not os.path.isfile(cfg):
        raise ValueError("config file missing: %s" % cfg)
    from .config import load_settings
    return load_settings()


def _value(settings, key):
    # type: (object, str) -> str
    if not isinstance(key, str) or not key or "/" in key or " " in key:
        raise ValueError("unknown config key: %s" % (key,))
    if not hasattr(settings, key):
        # Typo'd keys fail closed instead of yielding silent empties (N16).
        raise ValueError("unknown config key: %s" % key)
    try:
        value = getattr(settings, key, "")
    except Exception:
        raise ValueError("config key unreadable: %s" % key)
    if key == "listen_port":
        try:
            port = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise ValueError("listen_port invalid: %r" % (value,))
        if not 1 <= port <= 65535:
            raise ValueError("listen_port out of range: %r" % (value,))
        return str(port)
    if isinstance(value, (list, tuple)):
        return ",".join(str(v) for v in value)
    if isinstance(value, dict):
        import json as _json
        try:
            return _json.dumps(value, sort_keys=True)
        except Exception:
            raise ValueError("config key unserializable: %s" % key)
    return str(value or "").strip()


def _as_json(settings):
    # type: (object) -> dict
    out = {}
    for key in REQUIRED_KEYS:
        value = _value(settings, key)
        if not value:
            raise ValueError("%s is empty" % key)
        out[key] = value
    return out


def main(argv=None):
    # type: (object) -> int
    ap = argparse.ArgumentParser(description="Update-OPS config reader")
    sub = ap.add_subparsers(dest="command", required=True)
    p_get = sub.add_parser("get")
    p_get.add_argument("--require", action="store_true",
                       help="exit 3 when the value is empty")
    p_get.add_argument("key")
    sub.add_parser("json")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0) if isinstance(exc.code, int) \
            else EXIT_INVALID
    try:
        settings = _load()
    except ValueError as exc:
        sys.stderr.write("config_cli: %s\n" % exc)
        return EXIT_BLOCKED
    except Exception as exc:
        sys.stderr.write("config_cli: config load crashed: %s\n" % exc)
        return EXIT_BLOCKED
    try:
        if args.command == "get":
            value = _value(settings, args.key)
            if getattr(args, "require", False) and not value:
                sys.stderr.write("config_cli: %s is empty\n" % args.key)
                return EXIT_BLOCKED
            sys.stdout.write(value + "\n")
        elif args.command == "json":
            sys.stdout.write(json.dumps(_as_json(settings),
                                        sort_keys=True) + "\n")
        else:
            return EXIT_INVALID
        return EXIT_OK
    except ValueError as exc:
        sys.stderr.write("config_cli: %s\n" % exc)
        return EXIT_BLOCKED
    except Exception as exc:
        sys.stderr.write("config_cli: crashed: %s\n" % exc)
        return EXIT_BLOCKED


if __name__ == "__main__":
    raise SystemExit(main())

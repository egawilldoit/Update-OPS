#!/usr/bin/env python3
"""EGA Update Console — atomic release pointer primitive (W5-D9).

Operator-side ONLY: invoked by ``deploy/scripts/install.sh`` and
``deploy/scripts/upgrade.sh`` (through the shared wrapper in
``deploy/scripts/lib/deploy_common.sh``) and never imported by the web
application or the worker.

Why: ``ln -sfn`` performs unlink-then-create, so a reader can observe
``current`` missing between the two syscalls and a crash can leave the
host without a pointer. ``switch`` creates a temporary symlink in the
SAME parent directory as ``current`` and commits it with ``os.replace``
(rename(2)): a concurrent reader always observes the old or the new
target, never a missing pointer. Restoration uses EXACTLY this primitive.

Usage (machine-readable JSON on stdout, diagnosis on stderr):
  deploy_release inspect --current PATH --releases-root DIR [--field NAME]
  deploy_release switch  --current PATH --target DIR [--releases-root DIR]
                         [--previous PATH]
  deploy_release verify  --current PATH --target PATH

Exit codes:
  0 ok / 2 usage / 3 inspect failure / 4 target validation failure /
  5 switch failure / 6 verify failure

Guard rails: `current` must be absent or a symlink whose target resolves
inside the releases root (no regular files/directories, no ``..``
escapes); the candidate must be an absolute, existing, real (non-symlink)
directory inside the releases root with no group/other write bits
(immutable-release contract). The full release content validation remains
``deploy/etc/validate-release.py`` — this helper only enforces the
filesystem invariants needed for safe pointer manipulation.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import stat
import sys

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_INSPECT = 3
EXIT_TARGET = 4
EXIT_SWITCH = 5
EXIT_VERIFY = 6

# Test-only deterministic fault hook (inert unless the environment names
# this exact point). The same variable is used by the shell hook in
# deploy/scripts/lib/deploy_common.sh.
_FAULT_ENV = "EGA_DEPLOY_FAULT_POINT"
_FAULT_PREPARE = "pointer-switch-prep"


class _Reject(Exception):
    def __init__(self, reason):
        # type: (str) -> None
        super().__init__(reason)
        self.reason = reason


def _abspath(path):
    # type: (str) -> str
    return os.path.abspath(os.path.expanduser(str(path)))


def _inside(path_abs, root_real):
    # type: (str, str) -> bool
    try:
        return os.path.commonpath([os.path.realpath(path_abs), root_real]) \
            == root_real
    except ValueError:
        return False


def _emit(payload, field=None):
    # type: (dict, object) -> None
    if field is not None:
        value = payload.get(field)
        if isinstance(value, bool):
            sys.stdout.write("true\n" if value else "false\n")
        elif value is None:
            sys.stdout.write("\n")
        else:
            sys.stdout.write("%s\n" % (value,))
    else:
        sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    sys.stdout.flush()


def _fail(command, reason, exit_code, **extra):
    # type: (str, str, int, object) -> int
    payload = {"ok": False, "command": command, "reason": reason}
    payload.update(extra)
    _emit(payload)
    sys.stderr.write("deploy_release: %s\n" % reason)
    sys.stderr.flush()
    return exit_code


def _validate_root(releases_root):
    # type: (str) -> tuple
    root_abs = _abspath(releases_root)
    if not os.path.lexists(root_abs):
        raise _Reject("releases root missing: %s" % root_abs)
    if os.path.islink(root_abs) or not os.path.isdir(root_abs):
        raise _Reject("releases root is not a real directory: %s" % root_abs)
    return root_abs, os.path.realpath(root_abs)


def _code_version(release_dir):
    # type: (str) -> object
    if not os.path.isdir(release_dir):
        return None
    path = os.path.join(release_dir, "backend", "app", "db.py")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return None
    match = re.search(r"SCHEMA_VERSION\s*=\s*(\d+)", text)
    if match is None:
        match = re.search(r"CODE_VERSION\s*=\s*(\d+)", text)
    if match is None:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _target_type(target):
    # type: (str) -> str
    if os.path.islink(target):
        return "symlink"
    if os.path.isdir(target):
        return "directory"
    if os.path.isfile(target):
        return "file"
    if os.path.lexists(target):
        return "other"
    return "missing"


def _inspect_pointer(current_abs, root_real):
    # type: (str, str) -> dict
    """Capture the exact previous pointer evidence or raise _Reject."""
    info = {"current": current_abs}
    if not os.path.lexists(current_abs):
        info.update({
            "exists": False, "current_type": "missing",
            "previous_raw": None, "previous_target": None,
            "target_exists": False, "target_type": None,
            "code_version": None,
        })
        return info
    st = os.lstat(current_abs)
    if not stat.S_ISLNK(st.st_mode):
        if stat.S_ISREG(st.st_mode):
            kind = "file"
        elif stat.S_ISDIR(st.st_mode):
            kind = "directory"
        else:
            kind = "unexpected type"
        raise _Reject(
            "current exists but is a %s, not a symlink: %s"
            % (kind, current_abs))
    raw = os.readlink(current_abs)
    if os.path.isabs(raw):
        target = os.path.normpath(raw)
    else:
        target = os.path.normpath(
            os.path.join(os.path.dirname(current_abs), raw))
    if not _inside(target, root_real):
        raise _Reject(
            "current symlink target escapes releases root: %s -> %s "
            "(root %s)" % (current_abs, target, root_real))
    ttype = _target_type(target)
    info.update({
        "exists": True, "current_type": "symlink",
        "previous_raw": raw, "previous_target": target,
        "target_exists": ttype != "missing", "target_type": ttype,
        "code_version": _code_version(target),
    })
    return info


def _validate_target(target, root_real):
    # type: (str, str) -> tuple
    if not os.path.isabs(str(target)):
        raise _Reject("target must be an absolute path: %r" % (target,))
    target_abs = os.path.normpath(str(target))
    if not _inside(target_abs, root_real):
        raise _Reject(
            "target escapes releases root: %s (root %s)"
            % (target_abs, root_real))
    if os.path.islink(target_abs):
        raise _Reject(
            "target is a symlink, not a real release dir: %s" % target_abs)
    if not os.path.exists(target_abs):
        raise _Reject("target missing: %s" % target_abs)
    if not os.path.isdir(target_abs):
        raise _Reject("target is not a directory: %s" % target_abs)
    mode = stat.S_IMODE(os.stat(target_abs).st_mode)
    if mode & 0o022:
        raise _Reject(
            "target is writable by group/other (mode %04o; immutable-release"
            " contract): %s" % (mode, target_abs))
    return target_abs, os.path.realpath(target_abs)


def _post_switch_ok(current_abs, target_real):
    # type: (str, str) -> bool
    try:
        if not os.path.islink(current_abs):
            return False
        return os.path.realpath(current_abs) == target_real
    except OSError:
        return False


def _cleanup_tmp(tmp):
    # type: (str) -> None
    try:
        if os.path.lexists(tmp):
            os.unlink(tmp)
    except OSError:
        pass


def _fsync_parent(path):
    # type: (str) -> None
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _do_inspect(current, releases_root, field):
    # type: (str, str, object) -> int
    current_abs = _abspath(current)
    try:
        _root_abs, root_real = _validate_root(releases_root)
    except _Reject as exc:
        return _fail("inspect", exc.reason, EXIT_INSPECT,
                     current=current_abs)
    try:
        info = _inspect_pointer(current_abs, root_real)
    except _Reject as exc:
        return _fail("inspect", exc.reason, EXIT_INSPECT,
                     current=current_abs)
    payload = {"ok": True, "command": "inspect",
               "releases_root": _root_abs}
    payload.update(info)
    _emit(payload, field=field)
    return EXIT_OK


def _do_switch(current, target, releases_root, previous):
    # type: (str, str, str, str) -> int
    current_abs = _abspath(current)
    if not os.path.isabs(str(current)):
        return _fail("switch", "current must be an absolute path: %r"
                     % (current,), EXIT_SWITCH, target=str(target))
    try:
        if str(releases_root or "").strip():
            _root_abs, root_real = _validate_root(releases_root)
        else:
            root_real = os.path.realpath(
                os.path.dirname(os.path.normpath(str(target))))
    except _Reject as exc:
        return _fail("switch", exc.reason, EXIT_TARGET, stage="root",
                     current=current_abs, target=str(target))
    try:
        target_abs, target_real = _validate_target(target, root_real)
    except _Reject as exc:
        return _fail("switch", exc.reason, EXIT_TARGET, stage="target",
                     current=current_abs, target=str(target))
    try:
        pointer = _inspect_pointer(current_abs, root_real)
    except _Reject as exc:
        return _fail("switch", "current pointer invalid: %s" % exc.reason,
                     EXIT_SWITCH, stage="current", current=current_abs,
                     target=target_abs)
    if pointer["exists"] and \
            os.path.realpath(current_abs) == target_real:
        _emit({"ok": True, "command": "switch", "current": current_abs,
               "target": target_abs,
               "previous_target": pointer["previous_target"],
               "already_current": True, "status": "already_current",
               "atomic": "os.replace"})
        return EXIT_OK
    prev_arg = str(previous or "").strip()
    if prev_arg:
        if not os.path.isabs(prev_arg):
            return _fail("switch", "previous must be an absolute path: %r"
                         % (prev_arg,), EXIT_SWITCH, stage="cas",
                         current=current_abs, target=target_abs)
        if not pointer["exists"]:
            return _fail("switch",
                         "current is absent but --previous %s was supplied"
                         % os.path.normpath(prev_arg), EXIT_SWITCH,
                         stage="cas", current=current_abs,
                         target=target_abs)
        if os.path.realpath(os.path.normpath(prev_arg)) != \
                os.path.realpath(pointer["previous_target"]):
            return _fail(
                "switch",
                "current pointer changed since inspection (expected %s, "
                "found %s)" % (os.path.normpath(prev_arg),
                               pointer["previous_target"]),
                EXIT_SWITCH, stage="cas", current=current_abs,
                target=target_abs)
    parent = os.path.dirname(current_abs)
    if not os.path.isdir(parent):
        return _fail("switch", "current parent directory missing: %s"
                     % parent, EXIT_SWITCH, stage="prepare",
                     current=current_abs, target=target_abs)
    tmp = os.path.join(
        parent, ".%s.tmp.%d.%s" % (os.path.basename(current_abs),
                                   os.getpid(), secrets.token_hex(4)))
    try:
        os.symlink(target_abs, tmp)
    except OSError as exc:
        return _fail("switch", "cannot create temporary pointer: %s" % exc,
                     EXIT_SWITCH, stage="prepare", current=current_abs,
                     target=target_abs)
    try:
        if os.environ.get(_FAULT_ENV) == _FAULT_PREPARE:
            _cleanup_tmp(tmp)
            return _fail("switch",
                         "fault injected at %s (temporary pointer removed;"
                         " current untouched)" % _FAULT_PREPARE,
                         EXIT_SWITCH, stage="prepare",
                         current=current_abs, target=target_abs,
                         current_replaced=False)
        os.replace(tmp, current_abs)
    except OSError as exc:
        _cleanup_tmp(tmp)
        return _fail("switch", "atomic replace failed: %s" % exc,
                     EXIT_SWITCH, stage="replace", current=current_abs,
                     target=target_abs, current_replaced=False)
    finally:
        _cleanup_tmp(tmp)
    _fsync_parent(parent)
    if not _post_switch_ok(current_abs, target_real):
        return _fail(
            "switch",
            "post-switch verification mismatch: %s does not resolve to %s"
            % (current_abs, target_abs), EXIT_SWITCH, stage="post-verify",
            current=current_abs, target=target_abs, current_replaced=True)
    _emit({"ok": True, "command": "switch", "current": current_abs,
           "target": target_abs,
           "previous_target": pointer["previous_target"],
           "already_current": False, "status": "switched",
           "atomic": "os.replace"})
    return EXIT_OK


def _do_verify(current, target):
    # type: (str, str) -> int
    current_abs = _abspath(current)
    target_abs = _abspath(target)
    if not os.path.isabs(str(current)) or not os.path.isabs(str(target)):
        return _fail("verify", "current and target must be absolute paths",
                     EXIT_VERIFY, current=current_abs, target=target_abs)
    if not os.path.islink(current_abs):
        return _fail("verify", "current is not a symlink: %s" % current_abs,
                     EXIT_VERIFY, current=current_abs, target=target_abs)
    try:
        resolved = os.path.realpath(current_abs)
    except OSError as exc:
        return _fail("verify", "current unreadable: %s" % exc, EXIT_VERIFY,
                     current=current_abs, target=target_abs)
    if resolved != os.path.realpath(target_abs):
        return _fail("verify", "current resolves to %s, not %s"
                     % (resolved, target_abs), EXIT_VERIFY,
                     current=current_abs, target=target_abs)
    _emit({"ok": True, "command": "verify", "current": current_abs,
           "target": target_abs, "status": "verified"})
    return EXIT_OK


def main(argv=None):
    # type: (object) -> int
    ap = argparse.ArgumentParser(
        prog="deploy_release",
        description="EGA atomic release pointer primitive")
    sub = ap.add_subparsers(dest="command")
    p_inspect = sub.add_parser("inspect")
    p_inspect.add_argument("--current", required=True)
    p_inspect.add_argument("--releases-root", required=True)
    p_inspect.add_argument("--field", default=None)
    p_switch = sub.add_parser("switch")
    p_switch.add_argument("--current", required=True)
    p_switch.add_argument("--target", required=True)
    p_switch.add_argument("--releases-root", default="")
    p_switch.add_argument("--previous", default="")
    p_verify = sub.add_parser("verify")
    p_verify.add_argument("--current", required=True)
    p_verify.add_argument("--target", required=True)
    args = ap.parse_args(argv)
    if args.command is None:
        ap.print_usage(sys.stderr)
        return EXIT_USAGE
    if args.command == "inspect":
        return _do_inspect(args.current, args.releases_root, args.field)
    if args.command == "switch":
        return _do_switch(args.current, args.target, args.releases_root,
                          args.previous)
    if args.command == "verify":
        return _do_verify(args.current, args.target)
    ap.print_usage(sys.stderr)
    return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())

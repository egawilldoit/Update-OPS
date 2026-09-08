#!/usr/bin/env python3
"""Release archive validator (N17). Stdlib only, Python 3.10 compatible.

Usage:
  python3 deploy/etc/validate-archive.py --archive FILE --dest DIR

Inspects EVERY tar member BEFORE any root extraction. Rejects:
- absolute paths
- `..` traversal / normalization escaping the destination root
- symlinks or hard links resolving outside the destination root
- device files, FIFOs, sockets, other special entries
- members with unsafe modes (setuid/setgid/world-writable)

Then validates the expected top-level release structure
(backend/app, systemd/, deploy/etc/, frontend build marker optional —
the installer/upgrade gate checks the built frontend separately).

Exits: 0 archive safe (+ prints member count and top-level dirs);
2 usage; 3 blocked with reason. Never extracts anything.
"""
from __future__ import annotations

import argparse
import os
import stat
import sys
import tarfile

EXIT_OK = 0
EXIT_INVALID = 2
EXIT_BLOCKED = 3

EXPECTED_TOP_LEVELS = ("backend", "systemd", "deploy")


def _fail(reason):
    # type: (str) -> int
    sys.stderr.write("validate-archive: REFUSING: %s\n" % reason)
    return EXIT_BLOCKED


def _is_within_root(root, path):
    # type: (str, str) -> bool
    try:
        resolved = os.path.realpath(os.path.join(root, path))
    except Exception:
        return False
    try:
        common = os.path.commonpath([resolved, os.path.realpath(root)])
    except Exception:
        return False
    return common == os.path.realpath(root)


def validate(archive, dest):
    # type: (str, str) -> int
    if not os.path.isfile(archive):
        return _fail("archive missing: %s" % archive)
    try:
        root = os.path.realpath(dest)
    except Exception as exc:
        return _fail("destination unresolvable: %s" % exc)
    try:
        members = []
        with tarfile.open(archive, "r") as tf:
            members = tf.getmembers()
    except Exception as exc:
        return _fail("archive unreadable: %s" % exc)
    if not members:
        return _fail("archive is empty")
    tops = set()
    for member in members:
        name = member.name or ""
        if not name or name in (".", "./"):
            continue
        if os.path.isabs(name):
            return _fail("absolute member path: %r" % name[:200])
        normalized = os.path.normpath(name)
        if normalized.startswith("..") or os.path.isabs(normalized):
            return _fail("traversal member path: %r" % name[:200])
        if not _is_within_root(root, normalized):
            return _fail("member escapes destination: %r" % name[:200])
        if member.issym() or member.islnk():
            target = member.linkname or ""
            if os.path.isabs(target):
                return _fail("absolute link target: %r -> %r"
                             % (name[:120], target[:120]))
            base = os.path.dirname(normalized)
            resolved_target = os.path.normpath(
                os.path.join(base, target))
            if resolved_target.startswith("..") or not _is_within_root(
                    root, resolved_target):
                return _fail("link escapes destination: %r -> %r"
                             % (name[:120], target[:120]))
        if member.isdev() or member.isfifo():
            return _fail("special member rejected: %r" % name[:200])
        if member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE,
                               tarfile.DIRTYPE, tarfile.SYMTYPE,
                               tarfile.LNKTYPE):
            return _fail("unsupported member type %r: %r"
                         % (member.type, name[:200]))
        try:
            mode = member.mode or 0
        except Exception:
            mode = 0
        if mode & (stat.S_ISUID | stat.S_ISGID):
            return _fail("setuid/setgid member rejected: %r" % name[:200])
        if mode & stat.S_IWOTH:
            return _fail("world-writable member rejected: %r" % name[:200])
        top = normalized.split("/")[0]
        if top and top != ".":
            tops.add(top)
    missing = [t for t in EXPECTED_TOP_LEVELS if t not in tops]
    if missing:
        return _fail("missing expected top-level dirs: %s (have: %s)"
                     % (",".join(missing), ",".join(sorted(tops))))
    sys.stdout.write("validate-archive: ok (%d members, top=%s)\n"
                     % (len(members), ",".join(sorted(tops))))
    return EXIT_OK


def main(argv=None):
    # type: (object) -> int
    ap = argparse.ArgumentParser(description="Update-OPS archive validator")
    ap.add_argument("--archive", required=True)
    ap.add_argument("--dest", required=True)
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0) if isinstance(exc.code, int) \
            else EXIT_INVALID
    return validate(args.archive, args.dest)


if __name__ == "__main__":
    raise SystemExit(main())

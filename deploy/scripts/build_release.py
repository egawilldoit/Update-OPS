#!/usr/bin/env python3
"""Canonical release packager (W10) — see deploy/scripts/build-release.sh.

Usage:
  deploy/scripts/build-release.sh <commit-sha> <output-dir>
  deploy/scripts/build-release.sh --verify <artifact.tar.gz>

The build stages the EXACT candidate tree via `git archive <sha>` (never
the dirty working directory), builds the frontend inside the staging tree
with a lockfile-resolved `npm ci --prefer-offline` (the warm npm cache is
acceptable; missing tarballs are still fetched from the network) followed
by `npm run build`, writes release metadata, and emits a deterministic
archive:

  tar --sort=name --owner=0 --group=0 --numeric-owner \
      --mtime=@$SOURCE_DATE_EPOCH --format=gnu -cf - -T <list> | gzip -n -9

Outputs (deterministic names):
  <output-dir>/ega-update-<commit>.tar.gz
  <output-dir>/ega-update-<commit>.tar.gz.sha256

Archive layout (top level): backend/, deploy/, systemd/, frontend/,
scripts/, docs/, DOC/, README.md, plus:
  RELEASE_IDENTITY.json  commit/tree/source_branch/code_version/
                         build_recipe_version/SOURCE_DATE_EPOCH/
                         source_timestamp (no secrets)
  MANIFEST               backend-only `<sha256>  <relpath>` manifest, the
                         same file install.sh/upgrade.sh regenerate via
                         `find backend -type f -exec sha256sum {} + | sort`
                         and validate-release.py verifies
  MANIFEST.sha256        full-release manifest covering every release file
                         except itself (sorted `<sha256>  <relpath>` lines)

Self-verification is fail-closed and always runs on the produced artifact:
embedded commit == requested commit, embedded tree == `git rev-parse
<sha>^{tree}`, embedded epoch == `git show -s --format=%ct <sha>`, full
manifest verifies against extracted content (set-exact), every tracked
file matches its `git ls-tree -r <sha>` blob hash and exec bit, the built
frontend is present and covered by the manifest, the recorded external
artifact SHA-256 matches, and no member path is absolute or contains the
staging/output directory.

Python 3.10 compatible, stdlib only. Never writes inside the working tree.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile

BUILD_RECIPE_VERSION = 1
IDENTITY_NAME = "RELEASE_IDENTITY.json"
FULL_MANIFEST_NAME = "MANIFEST.sha256"
BACKEND_MANIFEST_NAME = "MANIFEST"
STATIC_REL = os.path.join("backend", "app", "static")
FRONTEND_INDEX_REL = os.path.join(STATIC_REL, "index.html")
ARTIFACT_PREFIX = "ega-update-"
HEX40_RE = re.compile(r"^[0-9a-f]{40}$")
CODE_VERSION_RE = re.compile(r"(?m)^CODE_VERSION\s*=\s*(\d+)\s*$")
PACKAGE_VERSION_RE = re.compile(r'(?m)^__version__\s*=\s*"([^"]+)"\s*$')
RESERVED_TOP_LEVEL = (IDENTITY_NAME, FULL_MANIFEST_NAME,
                      BACKEND_MANIFEST_NAME)


class PackagingError(Exception):
    """Fail-closed packaging/verification error (message already printed)."""


def _fail(message):
    # type: (str) -> "NoReturn"
    sys.stderr.write("[build-release] REFUSING: %s\n" % message)
    raise PackagingError(message)


def _log(message):
    # type: (str) -> None
    sys.stdout.write("[build-release] %s\n" % message)
    sys.stdout.flush()


def _sha256_file(path):
    # type: (str) -> str
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_blob_hash(path, object_format="sha1"):
    # type: (str, str) -> str
    # git ls-tree object ids are `sha1("blob <len>\0" + content)` hashes
    # (this repository uses sha1 object format; the build never applies
    # filters, so git archive exported the content 1:1).
    hasher = hashlib.sha1 if object_format == "sha1" else hashlib.sha256
    size = os.path.getsize(path)
    digest = hasher()
    digest.update(("blob %d\0" % size).encode("ascii"))
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _iso_utc(epoch):
    # type: (int) -> str
    return datetime.datetime.fromtimestamp(
        int(epoch), datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _base_env():
    # type: () -> "dict[str, str]"
    env = dict(os.environ)
    env.update({
        "CI": "1",
        "TZ": "UTC",
        "LANG": "C",
        "LC_ALL": "C",
        "npm_config_audit": "false",
        "npm_config_fund": "false",
        "npm_config_update_notifier": "false",
    })
    return env


def _run(cmd, cwd=None, env=None):
    # type: ("list[str]", "str | None", "dict[str, str] | None") -> None
    proc = subprocess.run(cmd, cwd=cwd, env=env or _base_env(),
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        _fail("command failed (%s): %s\n%s"
              % (" ".join(cmd[:3]), " ".join(cmd),
                 "\n".join(tail[-20:]) or "(no stderr)"))


def _git(repo, args, text=True):
    # type: (str, "list[str]", bool) -> "str | bytes"
    proc = subprocess.run(["git", "-C", repo] + args, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, env=_base_env())
    if proc.returncode != 0:
        _fail("git %s failed: %s"
              % (" ".join(args), proc.stderr.decode("utf-8", "replace").strip()))
    return proc.stdout.decode("utf-8") if text else proc.stdout


def _require_commit(repo, commit):
    # type: (str, str) -> None
    if not isinstance(commit, str) or not HEX40_RE.match(commit or ""):
        _fail("commit must be a full 40-character lowercase hex SHA "
              "(got: %r)" % (commit,))
    proc = subprocess.run(
        ["git", "-C", repo, "cat-file", "-e", commit + "^{commit}"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_base_env())
    if proc.returncode != 0:
        _fail("commit not found in this repository: %s" % commit)


def _source_branch(repo, commit):
    # type: (str, str) -> str
    out = _git(repo, ["for-each-ref", "--format=%(refname:short)",
                      "--points-at", commit, "refs/heads"])
    names = [line for line in out.splitlines() if line.strip()]
    return names[0] if len(names) == 1 else ""


def _ls_tree(repo, commit):
    # type: (str, str) -> "list[tuple[str, str, str]]"
    raw = _git(repo, ["ls-tree", "-r", "-z", commit], text=False)
    entries = []
    for record in raw.split(b"\x00"):
        if not record:
            continue
        meta, _, path = record.partition(b"\t")
        parts = meta.split(b" ")
        if len(parts) < 3:
            _fail("unparsable git ls-tree record for commit %s" % commit)
        entries.append((parts[0].decode("ascii"), parts[1].decode("ascii"),
                        parts[2].decode("ascii"),
                        path.decode("utf-8", "surrogateescape")))
    return entries


def _parse_versions(staging):
    # type: (str) -> "tuple[int, str]"
    db_text = _read_text(os.path.join(staging, "backend", "app", "db.py"))
    match = CODE_VERSION_RE.search(db_text)
    if not match:
        _fail("canonical version source backend/app/db.py:CODE_VERSION "
              "not found in the candidate tree")
    code_version = int(match.group(1))
    init_text = _read_text(os.path.join(staging, "backend", "app", "__init__.py"))
    pkg = PACKAGE_VERSION_RE.search(init_text)
    if not pkg:
        _fail("backend/app/__init__.py:__version__ not found in the "
              "candidate tree")
    return code_version, pkg.group(1)


def _read_text(path):
    # type: (str) -> str
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except OSError as exc:
        _fail("cannot read %s: %s" % (path, exc))


def _extract_git_archive(repo, commit, staging):
    # type: (str, str, str) -> None
    data = _git(repo, ["archive", "--format=tar", commit], text=False)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tf:
        for member in tf.getmembers():
            name = member.name or ""
            normalized = os.path.normpath(name)
            if (os.path.isabs(name) or normalized.startswith("..")
                    or not (member.isfile() or member.isdir())):
                _fail("git archive produced unsafe member: %r" % name[:200])
        tf.extractall(staging)


def _build_frontend(staging):
    # type: (str) -> None
    frontend = os.path.join(staging, "frontend")
    for required in ("package.json", "package-lock.json"):
        if not os.path.isfile(os.path.join(frontend, required)):
            _fail("commit does not include frontend/%s; cannot build the "
                  "frontend from the exact candidate" % required)
    _log("frontend: npm ci --prefer-offline (lockfile-resolved)")
    _run(["npm", "ci", "--prefer-offline", "--no-audit", "--no-fund"],
         cwd=frontend)
    _log("frontend: npm run build (vite outDir ../backend/app/static)")
    _run(["npm", "run", "build"], cwd=frontend)
    if not os.path.isfile(os.path.join(staging, FRONTEND_INDEX_REL)):
        _fail("frontend build did not produce %s" % FRONTEND_INDEX_REL)


def _collect_release_files(staging, repo, commit):
    # type: (str, str, str) -> "dict[str, bool]"
    release = {}
    tracked = _ls_tree(repo, commit)
    for mode, kind, _object, path in tracked:
        if kind != "blob":
            continue
        if path in RESERVED_TOP_LEVEL:
            _fail("tracked file collides with reserved release metadata: %s"
                  % path)
        if not os.path.isfile(os.path.join(staging, path)):
            _fail("staged tree is missing tracked file: %s" % path)
        release[path] = (mode == "100755")

    static_dir = os.path.join(staging, STATIC_REL)
    if not os.path.isdir(static_dir):
        _fail("built frontend directory missing after build: %s" % STATIC_REL)
    for root, _dirs, files in os.walk(static_dir):
        for name in files:
            full = os.path.join(root, name)
            rel = os.path.relpath(full, staging).replace(os.sep, "/")
            release[rel] = False

    release[IDENTITY_NAME] = False
    release[BACKEND_MANIFEST_NAME] = False
    release[FULL_MANIFEST_NAME] = False
    return release


def _normalize_modes(staging, release):
    # type: (str, "dict[str, bool]") -> None
    directories = set()
    for rel in release:
        parent = os.path.dirname(rel)
        while parent:
            directories.add(parent)
            parent = os.path.dirname(parent)
    for directory in sorted(directories):
        os.chmod(os.path.join(staging, directory), 0o755)
    for rel, executable in release.items():
        path = os.path.join(staging, rel)
        os.chmod(path, 0o755 if executable else 0o644)


def _write_metadata(staging, commit, tree, epoch, branch):
    # type: (str, str, str, int, str) -> "dict[str, object]"
    code_version, package_version = _parse_versions(staging)
    identity = {
        "SOURCE_DATE_EPOCH": int(epoch),
        "build_recipe_version": BUILD_RECIPE_VERSION,
        "code_version": code_version,
        "commit_sha": commit,
        "package_version": package_version,
        "source_branch": branch,
        "source_timestamp": _iso_utc(epoch),
        "tree_sha": tree,
    }
    with open(os.path.join(staging, IDENTITY_NAME), "w",
              encoding="utf-8") as handle:
        handle.write(json.dumps(identity, indent=2, sort_keys=True) + "\n")
    return identity


def _write_backend_manifest(staging, release):
    # type: (str, "dict[str, bool]") -> int
    lines = []
    for rel in sorted(release):
        if rel.startswith("backend/"):
            lines.append("%s  %s\n"
                         % (_sha256_file(os.path.join(staging, rel)), rel))
    with open(os.path.join(staging, BACKEND_MANIFEST_NAME), "w",
              encoding="utf-8") as handle:
        handle.writelines(lines)
    return len(lines)


def _write_full_manifest(staging, release):
    # type: (str, "dict[str, bool]") -> int
    lines = []
    for rel in sorted(release):
        if rel == FULL_MANIFEST_NAME:
            continue
        lines.append("%s  %s\n"
                     % (_sha256_file(os.path.join(staging, rel)), rel))
    with open(os.path.join(staging, FULL_MANIFEST_NAME), "w",
              encoding="utf-8") as handle:
        handle.writelines(lines)
    return len(lines)


def _tar_entries(release):
    # type: ("dict[str, bool]") -> "list[str]"
    entries = set(release)
    for rel in release:
        parent = os.path.dirname(rel)
        while parent:
            entries.add(parent)
            parent = os.path.dirname(parent)
    return sorted(entries)


def _create_artifact(staging, staging_parent, release, epoch, artifact):
    # type: (str, str, "dict[str, bool]", int, str) -> None
    list_path = os.path.join(staging_parent, "release-files.txt")
    with open(list_path, "w", encoding="utf-8") as handle:
        for entry in _tar_entries(release):
            handle.write(entry + "\n")
    tar_cmd = [
        "tar",
        "--sort=name",
        "--owner=0",
        "--group=0",
        "--numeric-owner",
        "--mtime=@%d" % int(epoch),
        "--format=gnu",
        "--no-recursion",
        "-C", staging,
        "-cf", "-",
        "-T", list_path,
    ]
    tmp_artifact = artifact + ".tmp"
    with open(tmp_artifact, "wb") as out:
        gzip_proc = subprocess.Popen(["gzip", "-n", "-9"],
                                     stdin=subprocess.PIPE, stdout=out)
        tar_proc = subprocess.Popen(tar_cmd, stdout=gzip_proc.stdin,
                                    stderr=subprocess.PIPE)
        gzip_proc.stdin.close()
        _, tar_err = tar_proc.communicate()
        gzip_rc = gzip_proc.wait()
        if tar_proc.returncode != 0:
            _fail("tar failed: %s"
                  % tar_err.decode("utf-8", "replace").strip())
        if gzip_rc != 0:
            _fail("gzip failed with exit %d" % gzip_rc)
    os.replace(tmp_artifact, artifact)


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _parse_manifest_lines(path):
    # type: (str) -> "list[tuple[str, str]]"
    entries = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.rstrip("\n")
            if not line:
                continue
            if "  " not in line:
                _fail("malformed manifest line (need '<sha256>  <path>'): %r"
                      % line[:200])
            digest, rel = line.split("  ", 1)
            if not SHA256_RE.match(digest):
                _fail("malformed manifest digest: %r" % digest[:80])
            entries.append((digest, rel))
    return entries


def verify_artifact(artifact, repo, requested_commit="", staging_dir=""):
    # type: (str, str, str, str) -> "list[str]"
    """Return a list of verification problems (empty list == verified)."""
    problems = []
    if not os.path.isfile(artifact):
        return ["artifact missing: %s" % artifact]
    artifact_name = os.path.basename(artifact)
    try:
        with tarfile.open(artifact, "r:gz") as tf:
            members = tf.getmembers()
    except Exception as exc:
        return ["artifact unreadable: %s" % exc]
    if not members:
        return ["artifact is empty"]

    names = set()
    for member in members:
        name = member.name or ""
        if not name or name in (".", "./"):
            problems.append("empty member name")
            continue
        if os.path.isabs(name) or name.startswith("/"):
            problems.append("absolute member path: %r" % name[:200])
            continue
        normalized = os.path.normpath(name)
        if normalized.startswith("..") or os.path.isabs(normalized):
            problems.append("traversal member path: %r" % name[:200])
            continue
        if staging_dir and os.path.normpath(staging_dir) in normalized:
            problems.append("member embeds staging dir: %r" % name[:200])
        # Allow only the conventional trailing slash on directory entries.
        if name.rstrip("/") != normalized:
            problems.append("non-normalized member path: %r" % name[:200])
        if member.issym() or member.islnk():
            problems.append("link member rejected: %r" % name[:200])
        elif member.isdev() or member.isfifo():
            problems.append("special member rejected: %r" % name[:200])
        elif not (member.isfile() or member.isdir()):
            problems.append("unsupported member type: %r" % name[:200])
        mode = member.mode or 0
        if mode & (stat.S_ISUID | stat.S_ISGID):
            problems.append("setuid/setgid member: %r" % name[:200])
        if mode & stat.S_IWOTH:
            problems.append("world-writable member: %r" % name[:200])
        if normalized in names:
            problems.append("duplicate member: %r" % normalized[:200])
        names.add(normalized)
    if problems:
        return problems

    extract_dir = tempfile.mkdtemp(prefix="ega-release-verify-")
    try:
        root = os.path.realpath(extract_dir)
        with tarfile.open(artifact, "r:gz") as tf:
            for member in tf.getmembers():
                target = os.path.realpath(
                    os.path.join(root, os.path.normpath(member.name)))
                if target != root and not target.startswith(root + os.sep):
                    problems.append("member escapes extraction root: %r"
                                    % member.name[:200])
                    break
                if member.isdir():
                    os.makedirs(target, exist_ok=True)
                    os.chmod(target, (member.mode or 0o755) & 0o777)
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                source = tf.extractfile(member)
                if source is None:
                    problems.append("unreadable member: %r" % member.name[:200])
                    break
                with open(target, "wb") as handle:
                    shutil.copyfileobj(source, handle)
                os.chmod(target, (member.mode or 0o644) & 0o777)
        if problems:
            return problems

        identity_path = os.path.join(extract_dir, IDENTITY_NAME)
        if not os.path.isfile(identity_path):
            return ["%s missing from archive" % IDENTITY_NAME]
        try:
            with open(identity_path, "r", encoding="utf-8") as handle:
                identity = json.load(handle)
        except Exception as exc:
            return ["%s unreadable: %s" % (IDENTITY_NAME, exc)]

        commit = str(identity.get("commit_sha", ""))
        tree = str(identity.get("tree_sha", ""))
        if not HEX40_RE.match(commit):
            problems.append("embedded commit_sha malformed: %r" % commit[:80])
        if not HEX40_RE.match(tree):
            problems.append("embedded tree_sha malformed: %r" % tree[:80])
        if requested_commit and commit != requested_commit:
            problems.append("embedded commit %s != requested commit %s"
                            % (commit, requested_commit))
        if artifact_name != "%s%s.tar.gz" % (ARTIFACT_PREFIX, commit):
            problems.append("artifact filename %s does not match embedded "
                            "commit %s" % (artifact_name, commit))
        commit_ok = False
        if not problems:
            probe = subprocess.run(
                ["git", "-C", repo, "cat-file", "-e", commit + "^{commit}"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_base_env())
            if probe.returncode != 0:
                problems.append("embedded commit not in repository: %s" % commit)
            else:
                commit_ok = True
                expected_tree = _git(
                    repo, ["rev-parse", commit + "^{tree}"]).strip()
                if tree != expected_tree:
                    problems.append("embedded tree %s != %s"
                                    % (tree, expected_tree))
                epoch_text = _git(
                    repo, ["show", "-s", "--format=%ct", commit]).strip()
                epoch = int(epoch_text)
                embedded_epoch = identity.get("SOURCE_DATE_EPOCH")
                if embedded_epoch != epoch:
                    problems.append("embedded SOURCE_DATE_EPOCH %r != %d"
                                    % (embedded_epoch, epoch))
                if identity.get("source_timestamp") != _iso_utc(epoch):
                    problems.append("embedded source_timestamp %r != %s"
                                    % (identity.get("source_timestamp"),
                                       _iso_utc(epoch)))
                if identity.get("build_recipe_version") != BUILD_RECIPE_VERSION:
                    problems.append("embedded build_recipe_version %r != %r"
                                    % (identity.get("build_recipe_version"),
                                       BUILD_RECIPE_VERSION))
                try:
                    code_version, package_version = _parse_versions(extract_dir)
                except PackagingError:
                    problems.append("cannot parse embedded version sources")
                else:
                    if identity.get("code_version") != code_version:
                        problems.append("embedded code_version %r != %d"
                                        % (identity.get("code_version"),
                                           code_version))
                    if identity.get("package_version") != package_version:
                        problems.append("embedded package_version %r != %s"
                                        % (identity.get("package_version"),
                                           package_version))
                if not isinstance(identity.get("source_branch"), str):
                    problems.append("embedded source_branch is not a string")

        manifest_path = os.path.join(extract_dir, FULL_MANIFEST_NAME)
        if not os.path.isfile(manifest_path):
            problems.append("%s missing from archive" % FULL_MANIFEST_NAME)
        else:
            covered = set()
            previous = ""
            try:
                entries = _parse_manifest_lines(manifest_path)
            except PackagingError as exc:
                return problems + ["%s: %s" % (FULL_MANIFEST_NAME, exc)]
            for digest, rel in entries:
                if rel <= previous:
                    problems.append("%s not sorted/unique at %s"
                                    % (FULL_MANIFEST_NAME, rel[:150]))
                previous = rel
                if rel in covered:
                    problems.append("%s duplicate entry %s"
                                    % (FULL_MANIFEST_NAME, rel[:150]))
                covered.add(rel)
                target = os.path.join(extract_dir, rel)
                if not os.path.isfile(target):
                    problems.append("%s references missing file %s"
                                    % (FULL_MANIFEST_NAME, rel[:150]))
                    continue
                if _sha256_file(target) != digest:
                    problems.append("%s integrity mismatch: %s"
                                    % (FULL_MANIFEST_NAME, rel[:150]))
            actual = set()
            for walk_root, _dirs, files in os.walk(extract_dir):
                for name in files:
                    full = os.path.join(walk_root, name)
                    rel = os.path.relpath(full, extract_dir).replace(os.sep, "/")
                    actual.add(rel)
            expected = actual - {FULL_MANIFEST_NAME}
            missing = sorted(expected - covered)
            extra = sorted(covered - expected)
            for rel in missing[:20]:
                problems.append("release file not covered by %s: %s"
                                % (FULL_MANIFEST_NAME, rel[:150]))
            for rel in extra[:20]:
                problems.append("%s covers unknown file: %s"
                                % (FULL_MANIFEST_NAME, rel[:150]))

        backend_manifest = os.path.join(extract_dir, BACKEND_MANIFEST_NAME)
        if not os.path.isfile(backend_manifest):
            problems.append("%s missing from archive" % BACKEND_MANIFEST_NAME)
        else:
            backend_covered = set()
            try:
                backend_entries = _parse_manifest_lines(backend_manifest)
            except PackagingError as exc:
                problems.append("%s: %s" % (BACKEND_MANIFEST_NAME, exc))
            else:
                for digest, rel in backend_entries:
                    backend_covered.add(rel)
                    target = os.path.join(extract_dir, rel)
                    if not os.path.isfile(target):
                        problems.append("%s references missing file %s"
                                        % (BACKEND_MANIFEST_NAME, rel[:150]))
                    elif _sha256_file(target) != digest:
                        problems.append("%s integrity mismatch: %s"
                                        % (BACKEND_MANIFEST_NAME, rel[:150]))
                backend_actual = {rel for rel in actual
                                  if rel.startswith("backend/")}
                if backend_covered != backend_actual:
                    problems.append("%s does not cover exactly backend/**: "
                                    "missing=%s extra=%s"
                                    % (BACKEND_MANIFEST_NAME,
                                       len(backend_actual - backend_covered),
                                       len(backend_covered - backend_actual)))

        if not os.path.isfile(os.path.join(extract_dir, FRONTEND_INDEX_REL)):
            problems.append("built frontend missing: %s" % FRONTEND_INDEX_REL)
        static_dir = os.path.join(extract_dir, STATIC_REL)
        if not os.path.isdir(static_dir):
            problems.append("built frontend directory missing: %s" % STATIC_REL)

        if commit_ok:
            object_format = _git(
                repo, ["rev-parse", "--show-object-format"]).strip()
            for mode, kind, object_id, rel in _ls_tree(repo, commit):
                if kind != "blob":
                    continue
                target = os.path.join(extract_dir, rel)
                if not os.path.isfile(target):
                    problems.append("tracked file missing from archive: %s"
                                    % rel[:150])
                    continue
                if _git_blob_hash(target, object_format) != object_id:
                    problems.append("tracked file differs from git blob: %s"
                                    % rel[:150])
                    continue
                expected_exec = (mode == "100755")
                actual_exec = bool(os.stat(target).st_mode & 0o111)
                if expected_exec != actual_exec:
                    problems.append("tracked file exec bit differs: %s"
                                    % rel[:150])

        sha_path = artifact + ".sha256"
        if not os.path.isfile(sha_path):
            problems.append("recorded artifact SHA-256 missing: %s"
                            % os.path.basename(sha_path))
        else:
            try:
                with open(sha_path, "r", encoding="utf-8") as handle:
                    recorded = handle.read().strip().split()[0]
            except Exception as exc:
                problems.append("unreadable artifact SHA-256 file: %s" % exc)
            else:
                if recorded != _sha256_file(artifact):
                    problems.append("recorded artifact SHA-256 mismatch")
        return problems
    finally:
        shutil.rmtree(extract_dir, ignore_errors=True)


def build(commit, output_dir, repo):
    # type: (str, str, str) -> int
    _require_commit(repo, commit)
    tree = _git(repo, ["rev-parse", commit + "^{tree}"]).strip()
    epoch = int(_git(repo, ["show", "-s", "--format=%ct", commit]).strip())
    branch = _source_branch(repo, commit)
    os.makedirs(output_dir, exist_ok=True)
    if not os.path.isdir(output_dir):
        _fail("output dir is not a directory: %s" % output_dir)
    artifact_name = "%s%s.tar.gz" % (ARTIFACT_PREFIX, commit)
    artifact = os.path.join(output_dir, artifact_name)
    sha_path = artifact + ".sha256"

    with tempfile.TemporaryDirectory(prefix="ega-release-build-") as stage_root:
        staging = os.path.join(stage_root, "tree")
        os.makedirs(staging)
        _log("staging exact tree of %s via git archive" % commit)
        _extract_git_archive(repo, commit, staging)
        _build_frontend(staging)
        release = _collect_release_files(staging, repo, commit)
        identity = _write_metadata(staging, commit, tree, epoch, branch)
        backend_count = _write_backend_manifest(staging, release)
        full_count = _write_full_manifest(staging, release)
        _normalize_modes(staging, release)
        _log("manifest: %d files (full) / %d backend files"
             % (full_count, backend_count))
        _create_artifact(staging, stage_root, release, epoch, artifact)

        size = os.path.getsize(artifact)
        digest = _sha256_file(artifact)
        tmp_sha = sha_path + ".tmp"
        with open(tmp_sha, "w", encoding="utf-8") as handle:
            handle.write("%s  %s\n" % (digest, artifact_name))
        os.replace(tmp_sha, sha_path)

        problems = verify_artifact(artifact, repo, requested_commit=commit,
                                   staging_dir=staging)
        if problems:
            # Fail closed: never leave an unverified artifact in place.
            for problem in problems[:40]:
                sys.stderr.write("[build-release] VERIFY FAILED: %s\n" % problem)
            try:
                os.remove(artifact)
                os.remove(sha_path)
            except OSError:
                pass
            _fail("self-verification failed (%d problem(s))" % len(problems))
        _log("artifact: %s" % artifact)
        _log("sha256: %s" % digest)
        _log("size: %d bytes" % size)
        _log("identity: commit=%s tree=%s branch=%r code_version=%s "
             "SOURCE_DATE_EPOCH=%s"
             % (identity["commit_sha"], identity["tree_sha"],
                identity["source_branch"], identity["code_version"],
                identity["SOURCE_DATE_EPOCH"]))
        _log("verified: full manifest, backend manifest, tracked blob hashes "
             "and exec bits, built frontend, artifact SHA-256, member paths")
    return 0


def verify_cli(artifact, repo):
    # type: (str, str) -> int
    problems = verify_artifact(artifact, repo)
    if problems:
        for problem in problems[:40]:
            sys.stderr.write("[build-release] VERIFY FAILED: %s\n" % problem)
        _fail("artifact verification failed (%d problem(s))"
              % len(problems))
    _log("verify ok: %s (sha256 %s)"
         % (artifact, _sha256_file(artifact)))
    return 0


def main(argv=None):
    # type: ("list[str] | None") -> int
    default_repo = os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))
    parser = argparse.ArgumentParser(
        prog="build-release.sh",
        description="Canonical byte-reproducible EGA release packager")
    parser.add_argument("--repo", default=default_repo,
                        help="git repository root (default: script checkout)")
    parser.add_argument("--verify", metavar="ARTIFACT", default="",
                        help="verify an existing artifact instead of building")
    parser.add_argument("commit", nargs="?", default="",
                        help="full 40-hex commit SHA to package")
    parser.add_argument("output_dir", nargs="?", default="",
                        help="directory for ega-update-<sha>.tar.gz")
    args = parser.parse_args(argv)
    try:
        if args.verify:
            if args.commit or args.output_dir:
                parser.error("--verify takes no positional arguments")
            return verify_cli(args.verify, args.repo)
        if not args.commit or not args.output_dir:
            parser.error("usage: build-release.sh <commit-sha> <output-dir>")
        return build(args.commit, args.output_dir, args.repo)
    except PackagingError:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

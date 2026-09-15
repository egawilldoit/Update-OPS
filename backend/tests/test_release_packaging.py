"""W10 packaging tests: canonical byte-reproducible release artifact.

Runs the REAL `deploy/scripts/build-release.sh` against this repository's
committed HEAD (never the dirty working directory): same-commit builds must
be byte-identical, junk/untracked/secret files present in the worktree must
never enter the archive, the full manifest must cover every release file,
tampering must fail verification, and the existing archive validators must
keep refusing hostile archives. Hermetic (temp dirs only), no production
access.
"""
from __future__ import annotations

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

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
BUILD_SH = os.path.join(REPO_ROOT, "deploy", "scripts", "build-release.sh")
VALIDATE_ARCHIVE_PY = os.path.join(REPO_ROOT, "deploy", "etc",
                                   "validate-archive.py")
VALIDATE_RELEASE_PY = os.path.join(REPO_ROOT, "deploy", "etc",
                                   "validate-release.py")
CONFIG_EXAMPLE = os.path.join(REPO_ROOT, "deploy", "etc", "config.example.json")

JUNK_FILES = (
    ".packaging-junk-untracked.txt",
    ".runtime-secret.env",
    os.path.join(".packaging-junk-dir", "nested-leak.txt"),
    os.path.join("backend", "tests", "__packaging_junk__.txt"),
    os.path.join("backend", "app", "static", "__worktree_only_leak__.html"),
    os.path.join("frontend", "node_modules", "__packaging_junk__.txt"),
)


class BuildResult(object):
    def __init__(self, commit, out_dir, proc):
        self.commit = commit
        self.out_dir = out_dir
        self.proc = proc
        self.artifact = os.path.join(out_dir, "ega-update-%s.tar.gz" % commit)
        self.sidecar = self.artifact + ".sha256"

    @property
    def sha256(self):
        return _sha256_path(self.artifact)

    @property
    def size(self):
        return os.path.getsize(self.artifact)


def _sha256_path(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git(*args):
    proc = subprocess.run(["git", "-C", REPO_ROOT] + list(args),
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    return proc.stdout.decode("utf-8")


def _run(cmd, timeout=900):
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          cwd=REPO_ROOT, timeout=timeout)


def _build(commit, out_dir):
    proc = _run([BUILD_SH, commit, out_dir])
    return BuildResult(commit, out_dir, proc)


def _expected_branch(commit):
    names = [line for line in _git(
        "for-each-ref", "--format=%(refname:short)", "--points-at", commit,
        "refs/heads").splitlines() if line.strip()]
    return names[0] if len(names) == 1 else ""


def _tar_members(path):
    with tarfile.open(path, "r:gz") as tf:
        return tf.getmembers()


def _extract(artifact, dest):
    with tarfile.open(artifact, "r:gz") as tf:
        for member in tf.getmembers():
            target = os.path.join(dest, member.name)
            if member.isdir():
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            source = tf.extractfile(member)
            with open(target, "wb") as handle:
                handle.write(source.read())
            os.chmod(target, member.mode & 0o777)
    return dest


def _member_names(artifact):
    return [m.name.rstrip("/") for m in _tar_members(artifact)]


def _read_identity(artifact):
    with tarfile.open(artifact, "r:gz") as tf:
        handle = tf.extractfile("RELEASE_IDENTITY.json")
        return json.loads(handle.read().decode("utf-8"))


def _read_manifest(artifact, name):
    with tarfile.open(artifact, "r:gz") as tf:
        handle = tf.extractfile(name)
        entries = []
        for line in handle.read().decode("utf-8").splitlines():
            if not line.strip():
                continue
            digest, rel = line.split("  ", 1)
            entries.append((digest, rel))
        return entries


def _provision_worktree_junk(junk_payload, secret_payload):
    created = []
    for rel in JUNK_FILES:
        path = os.path.join(REPO_ROOT, rel)
        if os.path.exists(path):
            continue
        os.makedirs(os.path.dirname(path) or REPO_ROOT, exist_ok=True)
        payload = secret_payload if rel == ".runtime-secret.env" \
            else junk_payload
        with open(path, "wb") as handle:
            handle.write(payload + b"\n")
        if rel == ".runtime-secret.env":
            os.chmod(path, 0o600)
        created.append(path)
    return created


def _remove_worktree_junk(created):
    for path in created:
        try:
            os.remove(path)
        except OSError:
            pass
    for rel in (os.path.join(".packaging-junk-dir",),
                os.path.join("backend", "app", "static"),
                os.path.join("frontend", "node_modules")):
        path = os.path.join(REPO_ROOT, rel)
        try:
            if os.path.isdir(path) and not os.listdir(path):
                os.rmdir(path)
        except OSError:
            pass


@pytest.fixture(scope="session")
def canonical_builds(tmp_path_factory):
    head = _git("rev-parse", "HEAD").strip()
    out_one = str(tmp_path_factory.mktemp("pkg-out-one"))
    out_two = str(tmp_path_factory.mktemp("pkg-out-two"))
    junk_payload = b"EGA-JUNK-" + os.urandom(20).hex().encode("ascii")
    secret_payload = b"EGA-SECRET-" + os.urandom(20).hex().encode("ascii")
    junk = _provision_worktree_junk(junk_payload, secret_payload)
    try:
        first = _build(head, out_one)
        second = _build(head, out_two)
    finally:
        _remove_worktree_junk(junk)
    assert first.proc.returncode == 0, \
        "build 1 failed: %s" % first.proc.stderr.decode("utf-8", "replace")
    assert second.proc.returncode == 0, \
        "build 2 failed: %s" % second.proc.stderr.decode("utf-8", "replace")
    assert os.path.isfile(first.artifact) and os.path.isfile(first.sidecar)
    assert os.path.isfile(second.artifact) and os.path.isfile(second.sidecar)
    return {"head": head, "first": first, "second": second,
            "junk": list(JUNK_FILES), "junk_payload": junk_payload,
            "secret_payload": secret_payload}


@pytest.fixture(scope="session")
def older_build(tmp_path_factory):
    older = _git("rev-parse", "HEAD~1").strip()
    out_dir = str(tmp_path_factory.mktemp("pkg-out-older"))
    result = _build(older, out_dir)
    assert result.proc.returncode == 0, \
        "older-commit build failed: %s" % result.proc.stderr.decode(
            "utf-8", "replace")
    return result


def test_canonical_artifact_layout_and_identity(canonical_builds):
    artifact = canonical_builds["first"].artifact
    head = canonical_builds["head"]
    names = set(_member_names(artifact))
    for required in ("RELEASE_IDENTITY.json", "MANIFEST", "MANIFEST.sha256",
                     "backend/app/main.py", "backend/app/static/index.html",
                     "deploy/scripts/install.sh", "deploy/etc/validate-archive.py",
                     "deploy/etc/validate-release.py", "systemd/ega-update-api.service",
                     "frontend/package.json", "frontend/package-lock.json",
                     "scripts/agent-update"):
        assert required in names, required
    for forbidden in ("node_modules", "__pycache__", ".pytest_cache",
                      "backend/app/static/__worktree_only_leak__.html"):
        assert not any(part == forbidden or forbidden in name
                       for name in names for part in name.split("/")), forbidden

    identity = _read_identity(artifact)
    assert identity["commit_sha"] == head
    assert identity["tree_sha"] == _git("rev-parse", "%s^{tree}" % head).strip()
    assert identity["SOURCE_DATE_EPOCH"] == int(
        _git("show", "-s", "--format=%ct", head).strip())
    timestamp = identity["source_timestamp"]
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", timestamp)
    assert identity["build_recipe_version"] == 1
    assert identity["source_branch"] == _expected_branch(head)
    db_text = _git("show", "%s:backend/app/db.py" % head)
    assert identity["code_version"] == int(
        re.search(r"(?m)^CODE_VERSION\s*=\s*(\d+)", db_text).group(1))
    init_text = _git("show", "%s:backend/app/__init__.py" % head)
    assert identity["package_version"] == re.search(
        r'(?m)^__version__\s*=\s*"([^"]+)"', init_text).group(1)
    blob = json.dumps(identity, sort_keys=True).lower()
    for needle in ("secret", "token", "password", "credential"):
        assert needle not in blob, needle

    sidecar = open(canonical_builds["first"].sidecar).read().split()
    assert sidecar[0] == canonical_builds["first"].sha256
    assert sidecar[1] == os.path.basename(artifact)


def test_same_commit_builds_byte_identically(canonical_builds):
    first = canonical_builds["first"]
    second = canonical_builds["second"]
    assert first.size == second.size
    assert first.sha256 == second.sha256
    with open(first.artifact, "rb") as handle_a, \
            open(second.artifact, "rb") as handle_b:
        assert handle_a.read() == handle_b.read()


def test_dirty_worktree_and_untracked_files_cannot_leak(canonical_builds):
    artifact = canonical_builds["first"].artifact
    names = _member_names(artifact)
    for junk_rel in canonical_builds["junk"]:
        junk_unix = junk_rel.replace(os.sep, "/")
        assert junk_unix not in names
        assert os.path.basename(junk_unix) not in [
            os.path.basename(name) for name in names]
    junk_payload = canonical_builds["junk_payload"]
    secret_payload = canonical_builds["secret_payload"]
    with tarfile.open(artifact, "r:gz") as tf:
        for member in tf.getmembers():
            if not member.isfile():
                continue
            payload = tf.extractfile(member).read()
            assert junk_payload not in payload, member.name
            assert secret_payload not in payload, member.name


def test_secret_outside_git_cannot_enter(canonical_builds):
    artifact = canonical_builds["first"].artifact
    names = _member_names(artifact)
    assert ".runtime-secret.env" not in names
    assert not any("runtime-secret" in name for name in names)
    secret_payload = canonical_builds["secret_payload"]
    with tarfile.open(artifact, "r:gz") as tf:
        assert ".runtime-secret.env" not in tf.getnames()
        for member in tf.getmembers():
            if not member.isfile():
                continue
            assert secret_payload not in tf.extractfile(member).read(), \
                member.name


def test_no_absolute_or_staging_paths(canonical_builds):
    artifact = canonical_builds["first"].artifact
    out_dir = canonical_builds["first"].out_dir
    absolute_prefixes = (out_dir.encode(), REPO_ROOT.encode())
    staging_prefixes = (b"/tmp/" + b"ega-release-build-",
                        b"/tmp/" + b"ega-release-verify-")
    with tarfile.open(artifact, "r:gz") as tf:
        for member in tf.getmembers():
            name = member.name
            assert not os.path.isabs(name), name
            assert not name.startswith("/"), name
            assert ".." not in name.split("/"), name
            for needle in (out_dir, REPO_ROOT):
                assert needle not in name, (name, needle)
        for member in tf.getmembers():
            if not member.isfile():
                continue
            payload = tf.extractfile(member).read()
            for needle in absolute_prefixes + staging_prefixes:
                assert needle not in payload, (member.name, needle)
            if member.name.startswith("backend/app/static/"):
                assert b"/tmp/" not in payload, member.name
                assert b"/home/" not in payload, member.name


def test_manifest_covers_frontend_backend_deploy(canonical_builds):
    artifact = canonical_builds["first"].artifact
    with tempfile.TemporaryDirectory(prefix="pkg-manifest-") as dest:
        _extract(artifact, dest)
        covered = {rel: digest
                   for digest, rel in _read_manifest(artifact, "MANIFEST.sha256")}
        assert list(covered) == sorted(covered)
        for required in ("backend/app/main.py",
                         "backend/app/static/index.html",
                         "frontend/src/app.tsx",
                         "frontend/package-lock.json",
                         "deploy/scripts/install.sh",
                         "deploy/etc/validate-archive.py",
                         "deploy/etc/validate-release.py",
                         "systemd/ega-update-api.service",
                         "RELEASE_IDENTITY.json", "MANIFEST"):
            assert required in covered, required
        assert any(path.startswith("backend/app/static/assets/")
                   for path in covered)
        assert "MANIFEST.sha256" not in covered
        actual = set()
        for root, _dirs, files in os.walk(dest):
            for name in files:
                rel = os.path.relpath(os.path.join(root, name), dest)
                actual.add(rel.replace(os.sep, "/"))
        assert set(covered) == actual - {"MANIFEST.sha256"}
        for rel, digest in covered.items():
            assert re.match(r"^[0-9a-f]{64}$", digest), rel
            assert _sha256_path(os.path.join(dest, rel)) == digest, rel

        backend = {rel: digest
                   for digest, rel in _read_manifest(artifact, "MANIFEST")}
        assert all(rel.startswith("backend/") for rel in backend)
        assert "backend/app/static/index.html" in backend
        assert set(backend) == {rel for rel in actual if rel.startswith("backend/")}
        for rel, digest in backend.items():
            assert _sha256_path(os.path.join(dest, rel)) == digest, rel


def test_verify_accepts_pristine_artifact(canonical_builds):
    proc = _run([BUILD_SH, "--verify", canonical_builds["first"].artifact])
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    assert b"verify ok" in proc.stdout


def _rebuild_tampered(artifact, dest, mutate):
    _extract(artifact, dest)
    mutate(dest)
    tampered = os.path.join(dest, os.path.basename(artifact))
    with tarfile.open(tampered, "w:gz") as tf:
        for root, dirs, files in os.walk(dest):
            dirs.sort()
            for name in sorted(files):
                full = os.path.join(root, name)
                rel = os.path.relpath(full, dest)
                if rel == os.path.basename(artifact):
                    continue
                with open(full, "rb") as handle:
                    data = handle.read()
                info = tarfile.TarInfo(rel.replace(os.sep, "/"))
                info.size = len(data)
                info.mode = stat.S_IMODE(os.stat(full).st_mode)
                tf.addfile(info, io.BytesIO(data))
    with open(tampered + ".sha256", "w", encoding="utf-8") as handle:
        handle.write("%s  %s\n" % (_sha256_path(tampered),
                                   os.path.basename(tampered)))
    return tampered


def test_manifest_tampering_detected(canonical_builds):
    artifact = canonical_builds["first"].artifact
    with tempfile.TemporaryDirectory(prefix="pkg-tamper-") as dest:
        def flip(path):
            target = os.path.join(path, "backend", "app", "main.py")
            data = bytearray(open(target, "rb").read())
            data[0] = (data[0] + 1) % 256
            open(target, "wb").write(bytes(data))

        tampered = _rebuild_tampered(artifact, dest, flip)
        proc = _run([BUILD_SH, "--verify", tampered])
        assert proc.returncode != 0
        combined = (proc.stdout + proc.stderr).decode("utf-8", "replace")
        assert "mismatch" in combined or "differs from git blob" in combined


def test_release_identity_tampering_detected(canonical_builds):
    artifact = canonical_builds["first"].artifact
    with tempfile.TemporaryDirectory(prefix="pkg-identity-") as dest:
        def mutate(path):
            target = os.path.join(path, "RELEASE_IDENTITY.json")
            identity = json.load(open(target))
            identity["commit_sha"] = "0" * 40
            with open(target, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(identity, indent=2, sort_keys=True) + "\n")

        tampered = _rebuild_tampered(artifact, dest, mutate)
        proc = _run([BUILD_SH, "--verify", tampered])
        assert proc.returncode != 0
        combined = (proc.stdout + proc.stderr).decode("utf-8", "replace")
        assert "commit" in combined


def test_different_commit_produces_different_identity(canonical_builds,
                                                      older_build):
    head_artifact = canonical_builds["first"].artifact
    head_identity = _read_identity(head_artifact)
    older_identity = _read_identity(older_build.artifact)
    assert older_identity["commit_sha"] == older_build.commit
    assert older_identity["commit_sha"] != head_identity["commit_sha"]
    assert older_identity["tree_sha"] == _git(
        "rev-parse", "%s^{tree}" % older_build.commit).strip()
    assert older_identity["SOURCE_DATE_EPOCH"] == int(
        _git("show", "-s", "--format=%ct", older_build.commit).strip())
    assert older_build.sha256 != canonical_builds["first"].sha256


@pytest.mark.parametrize("commit", [
    "",
    "8c4e7d8",
    "8c4e7d83a894f9d5cc1ad14616719cd8972e4f8",
    "8c4e7d83a894f9d5cc1ad14616719cd8972e4f833",
    "g" * 40,
    "8C4E7D83A894F9D5CC1AD14616719CD8972E4F83",
    "f" * 40,
])
def test_bad_commit_inputs_rejected(tmp_path, commit):
    proc = _run([BUILD_SH, commit, str(tmp_path / "out")])
    assert proc.returncode != 0, commit
    combined = (proc.stdout + proc.stderr).decode("utf-8", "replace")
    assert "commit" in combined.lower()
    assert not os.path.exists(str(tmp_path / "out"))


def _write_tar(path, members):
    with tarfile.open(path, "w") as tf:
        for item in members:
            info = tarfile.TarInfo(item["name"])
            data = item.get("data", b"")
            if item.get("type") == tarfile.DIRTYPE:
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                tf.addfile(info)
                continue
            info.type = tarfile.REGTYPE
            info.mode = item.get("mode", 0o644)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))


def _safe_archive_members():
    return [
        {"name": "backend/app/x.py", "data": b"x = 1\n"},
        {"name": "systemd/a.service", "data": b"[Unit]\n"},
        {"name": "deploy/etc/v.py", "data": b"print(1)\n"},
    ]


def test_hostile_archives_refused(tmp_path):
    traversal = str(tmp_path / "traversal.tar")
    _write_tar(traversal, _safe_archive_members() + [
        {"name": "../../evil", "data": b"x"}])
    proc = _run([sys.executable, VALIDATE_ARCHIVE_PY, "--archive", traversal,
                 "--dest", str(tmp_path / "dest")])
    assert proc.returncode == 3, proc.stdout.decode("utf-8", "replace")

    setuid = str(tmp_path / "setuid.tar")
    _write_tar(setuid, _safe_archive_members() + [
        {"name": "backend/suid", "data": b"x", "mode": 0o4755}])
    proc = _run([sys.executable, VALIDATE_ARCHIVE_PY, "--archive", setuid,
                 "--dest", str(tmp_path / "dest")])
    assert proc.returncode == 3, proc.stdout.decode("utf-8", "replace")

    benign = str(tmp_path / "benign.tar")
    _write_tar(benign, _safe_archive_members())
    proc = _run([sys.executable, VALIDATE_ARCHIVE_PY, "--archive", benign,
                 "--dest", str(tmp_path / "dest")])
    assert proc.returncode == 0, proc.stdout.decode("utf-8", "replace")


def test_deploy_validators_pass_on_artifact(canonical_builds):
    artifact = canonical_builds["first"].artifact
    with tempfile.TemporaryDirectory(prefix="pkg-validate-") as dest:
        proc = _run([sys.executable, VALIDATE_ARCHIVE_PY, "--archive",
                     artifact, "--dest", dest])
        assert proc.returncode == 0, proc.stdout.decode("utf-8", "replace")
        assert "validate-archive: ok" in proc.stdout.decode("utf-8")
        _extract(artifact, dest)
        proc = _run([sys.executable, VALIDATE_RELEASE_PY, "--release", dest,
                     "--config", CONFIG_EXAMPLE,
                     "--only", "migrations,hashes,manifest,frontend"])
        assert proc.returncode == 0, proc.stdout.decode("utf-8", "replace")
        assert proc.stdout.decode("utf-8").strip() == "ok"

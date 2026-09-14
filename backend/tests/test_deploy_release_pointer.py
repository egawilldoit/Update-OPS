"""W5-D9 atomic release-pointer primitive tests (REAL filesystem symlinks).

The primitive (``backend/app/deploy_release.py``) is exercised through its
``python -m backend.app.deploy_release`` CLI against real temporary
directories and symlinks under ``tmp_path``. Every property asserted here
is a filesystem observation (readlink/lstat/os.replace semantics), never a
string match on the script.

Covered RED properties (W5-D9):
 1. no missing-``current`` window during repeated switches (reader loop)
 2. exact previous raw target captured before switch
 3. successful switch resolves exactly to the candidate
 5. unexpected type at ``current`` fails closed
 6. ``current`` escaping the releases root fails closed
 7. candidate missing/invalid fails before the pointer is touched
 8. candidate must be an immutable release directory
 9. post-switch verification mismatch fails the switch (and never lies)
17. repeated switch to the same target is idempotent (no churn)

No /opt, /etc, /var, systemd, or live service is touched.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

MODULE = "backend.app.deploy_release"

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_INSPECT = 3
EXIT_TARGET = 4
EXIT_SWITCH = 5
EXIT_VERIFY = 6


def _run(*args, env_extra=None):
    env = dict(os.environ)
    env["PYTHONPATH"] = _REPO_ROOT
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        [sys.executable, "-m", MODULE] + [str(a) for a in args],
        capture_output=True, text=True, timeout=60, env=env)
    payload = None
    if proc.stdout.strip():
        try:
            payload = json.loads(proc.stdout)
        except ValueError:
            payload = None
    return proc.returncode, payload, proc.stderr


class Layout:
    def __init__(self, tmp_path, names=("a" * 40, "b" * 40, "c" * 40)):
        self.root = tmp_path / "opt" / "releases"
        self.root.mkdir(parents=True, exist_ok=True)
        self.releases = {}
        for name in names:
            path = self.root / name
            path.mkdir()
            # Immutable-release contract: the primitive rejects group/other
            # writable release dirs, so fixtures model the deployed 0755.
            os.chmod(str(path), 0o755)
            self.releases[name] = path
        self.names = list(names)
        self.current = tmp_path / "opt" / "current"
        os.symlink(str(self.releases[self.names[0]]), str(self.current))


def _run_raw(*args, env_extra=None):
    env = dict(os.environ)
    env["PYTHONPATH"] = _REPO_ROOT
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        [sys.executable, "-m", MODULE] + [str(a) for a in args],
        capture_output=True, text=True, timeout=60, env=env)
    return proc.returncode, proc.stdout, proc.stderr


def _switch(layout, target, previous=None, root=None, env_extra=None):
    args = ["switch", "--current", str(layout.current),
            "--target", str(target),
            "--releases-root", str(root or layout.root)]
    if previous is not None:
        args += ["--previous", str(previous)]
    return _run(*args, env_extra=env_extra)


def _inspect(layout, field=None, root=None):
    args = ["inspect", "--current", str(layout.current),
            "--releases-root", str(root or layout.root)]
    if field is not None:
        args += ["--field", field]
    return _run(*args)


def _readlink(path):
    return os.readlink(str(path))


# -- 1. atomicity: no missing-current window -----------------------------------

def test_pointer_switch_never_exposes_missing_current(tmp_path):
    layout = Layout(tmp_path)
    expected = {os.path.realpath(str(p)) for p in layout.releases.values()}
    stop = threading.Event()
    missing = []
    unexpected = []

    def reader():
        while not stop.is_set():
            try:
                target = os.readlink(str(layout.current))
            except OSError:
                missing.append(1)
                continue
            resolved = os.path.realpath(
                os.path.join(str(layout.current.parent), target) if not
                os.path.isabs(target) else target)
            if resolved not in expected:
                unexpected.append(resolved)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    try:
        for index in range(50):
            target = layout.releases[layout.names[index % 2]]
            rc, payload, err = _switch(layout, target)
            assert rc == EXIT_OK, err
    finally:
        stop.set()
        thread.join(timeout=5)
    assert missing == [], (
        "reader observed a missing `current` during atomic switches "
        "(%d observations)" % len(missing))
    assert unexpected == [], unexpected


def test_legacy_unlink_then_create_exposes_missing_window(tmp_path):
    """Control for the reader harness above: the legacy `ln -sfn` shape
    (unlink-then-create) DOES expose a missing window, proving the
    detector would catch a non-atomic switch."""
    layout = Layout(tmp_path)
    missing = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            try:
                os.readlink(str(layout.current))
            except OSError:
                missing.append(1)

    def legacy_switch(target):
        os.unlink(str(layout.current))
        time.sleep(0.005)
        os.symlink(str(target), str(layout.current))

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    try:
        for index in range(30):
            legacy_switch(layout.releases[layout.names[index % 2]])
    finally:
        stop.set()
        thread.join(timeout=5)
    assert missing, (
        "control failed to observe the legacy unlink-then-create window")


# -- 2/3. exact previous raw target + exact resolution -------------------------

def test_inspect_captures_exact_previous_raw_target(tmp_path):
    layout = Layout(tmp_path)
    raw = os.path.join("releases", layout.names[0])
    rel_link = tmp_path / "opt" / "current-rel"
    os.symlink(raw, str(rel_link))
    proc = subprocess.run(
        [sys.executable, "-m", MODULE, "inspect", "--current", str(rel_link),
         "--releases-root", str(layout.root)],
        capture_output=True, text=True, timeout=60,
        env=dict(os.environ, PYTHONPATH=_REPO_ROOT))
    assert proc.returncode == EXIT_OK, proc.stderr
    data = json.loads(proc.stdout)
    assert data["previous_raw"] == raw, (
        "inspect must report the EXACT raw symlink target, not a "
        "readlink -f guess")
    assert data["previous_target"] == os.path.realpath(
        str(layout.releases[layout.names[0]]))
    assert data["exists"] is True
    assert data["current_type"] == "symlink"
    assert data["target_type"] == "directory"


def test_switch_resolves_current_exactly_to_candidate(tmp_path):
    layout = Layout(tmp_path)
    target = layout.releases[layout.names[1]]
    rc, payload, err = _switch(layout, target)
    assert rc == EXIT_OK, err
    assert payload["ok"] is True
    assert payload["status"] == "switched"
    assert payload["previous_target"] == os.path.realpath(
        str(layout.releases[layout.names[0]]))
    assert _readlink(layout.current) == str(target)
    assert os.path.realpath(str(layout.current)) == os.path.realpath(str(target))

    rc, payload, err = _run("verify", "--current", str(layout.current),
                            "--target", str(target))
    assert rc == EXIT_OK, err
    assert payload["status"] == "verified"


def test_switch_idempotent_when_target_already_selected(tmp_path):
    layout = Layout(tmp_path)
    target = layout.releases[layout.names[1]]
    rc, _, err = _switch(layout, target)
    assert rc == EXIT_OK, err
    inode_before = os.lstat(str(layout.current)).st_ino
    rc, payload, err = _switch(layout, target)
    assert rc == EXIT_OK, err
    assert payload["already_current"] is True
    assert payload["status"] == "already_current"
    inode_after = os.lstat(str(layout.current)).st_ino
    assert inode_before == inode_after, (
        "idempotent switch must not churn the symlink")
    assert _readlink(layout.current) == str(target)


def test_switch_creates_current_when_absent(tmp_path):
    layout = Layout(tmp_path)
    os.unlink(str(layout.current))
    target = layout.releases[layout.names[1]]
    rc, payload, err = _switch(layout, target)
    assert rc == EXIT_OK, err
    assert payload["previous_target"] is None
    assert _readlink(layout.current) == str(target)


# -- 4. failed preparation leaves the pointer untouched ------------------------

def test_switch_preparation_fault_leaves_current_and_cleans_temp(tmp_path):
    layout = Layout(tmp_path)
    before = _readlink(layout.current)
    parent = str(layout.current.parent)
    rc, payload, err = _switch(
        layout, layout.releases[layout.names[1]],
        env_extra={"EGA_DEPLOY_FAULT_POINT": "pointer-switch-prep"})
    assert rc == EXIT_SWITCH, (rc, payload, err)
    assert payload["ok"] is False
    assert "FAULT" not in json.dumps(payload) or payload["reason"]
    assert _readlink(layout.current) == before, (
        "failed switch preparation changed the current pointer")
    leftovers = [n for n in os.listdir(parent) if ".tmp." in n]
    assert leftovers == [], "temporary symlink not cleaned up: %r" % leftovers


def test_switch_fault_hook_is_inert_for_other_points(tmp_path):
    layout = Layout(tmp_path)
    target = layout.releases[layout.names[1]]
    rc, _, err = _switch(layout, target,
                         env_extra={"EGA_DEPLOY_FAULT_POINT": "unrelated"})
    assert rc == EXIT_OK, err
    assert os.path.realpath(str(layout.current)) == os.path.realpath(str(target))


# -- 5/6. current type + escape fail closed ------------------------------------

def test_inspect_rejects_unexpected_current_type(tmp_path):
    layout = Layout(tmp_path)
    os.unlink(str(layout.current))
    layout.current.write_text("not a symlink\n", encoding="utf-8")
    rc, payload, err = _inspect(layout)
    assert rc == EXIT_INSPECT, (rc, payload, err)
    assert payload["ok"] is False and payload["reason"]
    assert err.strip(), "failure must carry a stderr diagnosis"
    os.unlink(str(layout.current))
    layout.current.mkdir()
    rc, payload, err = _inspect(layout)
    assert rc == EXIT_INSPECT, (rc, payload, err)


def test_inspect_rejects_escaping_symlink_target(tmp_path):
    layout = Layout(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    os.unlink(str(layout.current))
    os.symlink(str(outside), str(layout.current))
    rc, payload, err = _inspect(layout)
    assert rc == EXIT_INSPECT, (rc, payload, err)
    assert "escape" in payload["reason"] or "root" in payload["reason"]
    # `..` traversal spelling resolves outside the allowed root too.
    os.unlink(str(layout.current))
    os.symlink(os.path.join(str(layout.root), "..", "..", "outside"),
               str(layout.current))
    rc, payload, err = _inspect(layout)
    assert rc == EXIT_INSPECT, (rc, payload, err)


def test_switch_rejects_invalid_current_before_touching_pointer(tmp_path):
    layout = Layout(tmp_path)
    os.unlink(str(layout.current))
    layout.current.write_text("manual state\n", encoding="utf-8")
    rc, payload, err = _switch(layout, layout.releases[layout.names[1]])
    assert rc == EXIT_SWITCH, (rc, payload, err)
    assert layout.current.read_text(encoding="utf-8") == "manual state\n"


def test_inspect_reports_dangling_inside_root_as_missing_target(tmp_path):
    layout = Layout(tmp_path)
    gone = layout.root / ("d" * 40)
    os.unlink(str(layout.current))
    os.symlink(str(gone), str(layout.current))
    rc, payload, err = _inspect(layout)
    assert rc == EXIT_OK, (rc, payload, err)
    assert payload["target_type"] == "missing"
    assert payload["previous_target"] == str(gone)


# -- 7/8. candidate validation -------------------------------------------------

def test_switch_rejects_missing_candidate_before_switch(tmp_path):
    layout = Layout(tmp_path)
    before = _readlink(layout.current)
    rc, payload, err = _switch(layout, layout.root / ("e" * 40))
    assert rc == EXIT_TARGET, (rc, payload, err)
    assert _readlink(layout.current) == before


def test_switch_rejects_non_directory_candidate(tmp_path):
    layout = Layout(tmp_path)
    before = _readlink(layout.current)
    plain = layout.root / "plain-file"
    plain.write_text("x\n", encoding="utf-8")
    rc, _, err = _switch(layout, plain)
    assert rc == EXIT_TARGET, err
    assert _readlink(layout.current) == before


def test_switch_rejects_symlink_candidate(tmp_path):
    layout = Layout(tmp_path)
    before = _readlink(layout.current)
    link = layout.root / "alias"
    os.symlink(str(layout.releases[layout.names[1]]), str(link))
    rc, _, err = _switch(layout, link)
    assert rc == EXIT_TARGET, err
    assert _readlink(layout.current) == before


def test_switch_rejects_candidate_outside_releases_root(tmp_path):
    layout = Layout(tmp_path)
    outside = tmp_path / "outside-release"
    outside.mkdir()
    before = _readlink(layout.current)
    rc, _, err = _switch(layout, outside)
    assert rc == EXIT_TARGET, err
    assert _readlink(layout.current) == before


def test_switch_rejects_writable_candidate(tmp_path):
    layout = Layout(tmp_path)
    before = _readlink(layout.current)
    writable = layout.releases[layout.names[1]]
    os.chmod(str(writable), 0o777)
    try:
        rc, payload, err = _switch(layout, writable)
        assert rc == EXIT_TARGET, (rc, payload, err)
        assert "writable" in payload["reason"]
        assert _readlink(layout.current) == before
    finally:
        os.chmod(str(writable), 0o755)


def test_switch_target_must_be_absolute(tmp_path):
    layout = Layout(tmp_path)
    reldir = os.path.relpath(str(layout.releases[layout.names[1]]),
                             str(layout.current.parent))
    before = _readlink(layout.current)
    rc, _, err = _switch(layout, reldir)
    assert rc == EXIT_TARGET, err
    assert _readlink(layout.current) == before


# -- CAS guard ------------------------------------------------------------------

def test_switch_previous_cas_rejects_stale_evidence(tmp_path):
    layout = Layout(tmp_path)
    target = layout.releases[layout.names[1]]
    wrong_previous = layout.releases[layout.names[2]]
    rc, payload, err = _switch(layout, target, previous=wrong_previous)
    assert rc == EXIT_SWITCH, (rc, payload, err)
    assert payload["stage"] == "cas"
    assert _readlink(layout.current) == str(layout.releases[layout.names[0]])
    rc, _, err = _switch(layout, target,
                         previous=layout.releases[layout.names[0]])
    assert rc == EXIT_OK, err


# -- 9. post-switch verification mismatch --------------------------------------

def test_switch_post_verify_mismatch_fails_closed(tmp_path, monkeypatch):
    import backend.app.deploy_release as mod

    layout = Layout(tmp_path)
    target = layout.releases[layout.names[1]]
    monkeypatch.setattr(mod, "_post_switch_ok",
                        lambda current_abs, target_real: False)
    rc = mod.main(["switch", "--current", str(layout.current),
                   "--target", str(target),
                   "--releases-root", str(layout.root)])
    assert rc == EXIT_SWITCH, "post-switch verification mismatch must fail"
    # The replacement itself is not silently reverted (the deployment must
    # fail and stay drained); the JSON must not claim success.
    assert os.path.realpath(str(layout.current)) == os.path.realpath(str(target))


def test_verify_reports_mismatch(tmp_path):
    layout = Layout(tmp_path)
    rc, payload, err = _run("verify", "--current", str(layout.current),
                            "--target", str(layout.releases[layout.names[1]]))
    assert rc == EXIT_VERIFY, (rc, payload, err)
    assert payload["ok"] is False


def test_verify_rejects_non_symlink_current(tmp_path):
    layout = Layout(tmp_path)
    os.unlink(str(layout.current))
    layout.current.write_text("x\n", encoding="utf-8")
    rc, _, _ = _run("verify", "--current", str(layout.current),
                    "--target", str(layout.releases[layout.names[0]]))
    assert rc == EXIT_VERIFY


# -- CLI contract ---------------------------------------------------------------

def test_usage_errors_exit_2(tmp_path):
    rc, _, _ = _run("switch", "--current", str(tmp_path / "x"))
    assert rc == EXIT_USAGE
    rc, _, _ = _run("bogus-command")
    assert rc == EXIT_USAGE
    rc, _, _ = _run()
    assert rc == EXIT_USAGE


def test_failure_is_machine_readable_json_with_stderr(tmp_path):
    layout = Layout(tmp_path)
    rc, payload, err = _inspect(layout)
    assert rc == EXIT_OK
    assert payload["previous_target"]
    path = layout.current
    os.unlink(str(path))
    path.write_text("x\n", encoding="utf-8")
    rc, payload, err = _inspect(layout)
    assert rc == EXIT_INSPECT
    assert payload["ok"] is False and payload["reason"]
    assert err.strip()


def test_inspect_field_mode_for_shell_consumption(tmp_path):
    layout = Layout(tmp_path)
    rc, out, err = _run_raw("inspect", "--current", str(layout.current),
                            "--releases-root", str(layout.root),
                            "--field", "previous_target")
    assert rc == EXIT_OK, err
    assert out.strip() == os.path.realpath(
        str(layout.releases[layout.names[0]]))
    rc, out, err = _run_raw("inspect", "--current", str(layout.current),
                            "--releases-root", str(layout.root),
                            "--field", "previous_raw")
    assert rc == EXIT_OK, err
    assert out.strip() == str(layout.releases[layout.names[0]])


def test_inspect_reports_code_identity_when_available(tmp_path):
    layout = Layout(tmp_path)
    release = layout.releases[layout.names[1]]
    (release / "backend" / "app").mkdir(parents=True)
    (release / "backend" / "app" / "db.py").write_text(
        "CODE_VERSION = 7\n", encoding="utf-8")
    os.unlink(str(layout.current))
    os.symlink(str(release), str(layout.current))
    rc, payload, err = _inspect(layout)
    assert rc == EXIT_OK, err
    assert payload["code_version"] == 7


def test_module_is_operator_side_only():
    """No runtime module imports the deploy-release helper (operator-only)."""
    app_root = os.path.join(_REPO_ROOT, "backend", "app")
    hits = []
    for dirpath, _dirnames, filenames in os.walk(app_root):
        if "__pycache__" in dirpath:
            continue
        for filename in filenames:
            if not filename.endswith(".py"):
                continue
            full = os.path.join(dirpath, filename)
            if full == os.path.join(app_root, "deploy_release.py"):
                continue
            with open(full, "r", encoding="utf-8", errors="replace") as fh:
                if "deploy_release" in fh.read():
                    hits.append(full)
    assert hits == [], "runtime modules must not import deploy_release: %r" % hits


def test_switch_never_unlinks_current_before_create(tmp_path):
    """The temp link is created first and committed by rename(2): at no
    point is `current` removed without a replacement ready."""
    layout = Layout(tmp_path)
    target = layout.releases[layout.names[1]]
    real_replace = os.replace
    calls = []

    def tracking_replace(src, dst):
        calls.append((str(src), str(dst)))
        assert os.path.islink(str(src)), "os.replace source must be the tmp link"
        assert os.path.islink(str(dst)), "current must still be a symlink"
        return real_replace(src, dst)

    import backend.app.deploy_release as mod
    original = mod.os.replace
    mod.os.replace = tracking_replace
    try:
        rc = mod.main(["switch", "--current", str(layout.current),
                       "--target", str(target),
                       "--releases-root", str(layout.root)])
    finally:
        mod.os.replace = original
    assert rc == EXIT_OK
    assert calls, "switch must commit via os.replace"
    assert calls[0][1] == str(layout.current)

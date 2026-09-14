"""W4-D8 deployment readiness regression tests (hermetic, behavior-first).

Pins the correction of the deployment readiness defects:

* D8-2 fake readiness: ``cli status --require-ready`` proves ALL
  mandatory stages (API service identity, API security boundary, worker
  process, probe executor, owner transient execution). A fresh dispatcher
  heartbeat is no longer sufficient, and a loopback 401 alone never makes
  the overall result ready.
* D8-3 drift: ONE canonical transient owner acceptance primitive
  (``backend.app.owner_env.transient_acceptance``) is the only owner
  round-trip definition; install.sh and upgrade.sh consume the readiness
  gate exit code through the shared shell helper.
* Probe executor readiness: the durable ``probe_worker.heartbeat`` marker
  written by the W3.1 ProbeWorker thread itself (never inferred from the
  dispatcher heartbeat).

No live service, systemd, DB, /opt, /etc, or /var access. Every layout
lives under tmp_path; subprocess boundaries are injected where a real
transient unit would run.
"""
from __future__ import annotations

import argparse
import json
import os
import pwd
import socket
import sys
import types
from datetime import datetime, timedelta, timezone

_REPO_ROOT = os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)


# -- fixtures / helpers -------------------------------------------------------

def _current_user():
    return pwd.getpwuid(os.getuid()).pw_name


def _settings(tmp_path, **over):
    base = dict(
        team_domain="team.cloudflareaccess.com",
        audience="aud-1",
        owner_emails=["owner@example.invalid"],
        public_origin="https://console.example.invalid",
        listen_host="127.0.0.1",
        listen_port=8771,
        csrf_secret="fixture-csrf-secret-value",
        csrf_secret_file="",
        state_dir=str(tmp_path / "state"),
        db_path=str(tmp_path / "state" / "state.db"),
        log_dir=str(tmp_path / "state" / "logs"),
        backup_dir=str(tmp_path / "state" / "backups"),
        secrets_file=str(tmp_path / "etc" / "secrets.env"),
        inventory_file="",
        tool_owner=_current_user(),
        shared_group="ega-update",
        api_user=_current_user(),
        service_units={"api": "ega-update-api.service",
                       "worker": "ega-update-worker.service"},
        node_path="", npm_path="", npx_path="",
    )
    base.update(over)
    return types.SimpleNamespace(**base)


def _api_layout(tmp_path, csrf_mode="file"):
    """Real on-disk layout the API identity probe inspects."""
    state = tmp_path / "state"
    logs = state / "logs"
    backups = state / "backups"
    etc = tmp_path / "etc"
    for d in (state, logs, backups, etc):
        d.mkdir(parents=True, exist_ok=True)
    os.chmod(str(state), 0o770)
    os.chmod(str(logs), 0o770)
    os.chmod(str(backups), 0o770)
    os.chmod(str(etc), 0o750)
    cfg = etc / "config.json"
    config = {
        "team_domain": "team.cloudflareaccess.com",
        "audience": "aud-1",
        "owner_emails": ["owner@example.invalid"],
        "public_origin": "https://console.example.invalid",
        "listen_host": "127.0.0.1",
        "listen_port": 8771,
        "csrf_secret": "fixture-csrf-secret-value",
        "csrf_secret_file": str(etc / "csrf.secret"),
        "state_dir": str(state),
        "db_path": str(state / "state.db"),
        "log_dir": str(logs),
        "backup_dir": str(backups),
        "secrets_file": str(etc / "secrets.env"),
        "tool_owner": _current_user(),
        "shared_group": "ega-update",
    }
    if csrf_mode == "file":
        (etc / "csrf.secret").write_text(
            "fixture-csrf-secret-value\n", encoding="utf-8")
    elif csrf_mode == "missing":
        config["csrf_secret"] = ""
        config["csrf_secret_file"] = str(etc / "no-such-csrf.secret")
    (etc / "secrets.env").write_text(
        "KNOWN_SECRET=fixture-secret-value\n", encoding="utf-8")
    with open(str(cfg), "w", encoding="utf-8") as fh:
        json.dump(config, fh)
    from backend.app import db as db_lib

    conn = db_lib.connect(str(state / "state.db"))
    assert db_lib.migrate(conn) == db_lib.CODE_VERSION
    conn.commit()
    conn.close()
    return {"config_file": str(cfg), "settings": _settings(
        tmp_path,
        state_dir=str(state), db_path=str(state / "state.db"),
        log_dir=str(logs), backup_dir=str(backups),
        secrets_file=str(etc / "secrets.env"),
        csrf_secret_file=str(etc / "csrf.secret"),
        csrf_secret="",
    )}


def _heartbeat(state_dir, name="dispatcher.heartbeat", ts=None):
    os.makedirs(state_dir, exist_ok=True)
    if ts is None:
        ts = datetime.now(timezone.utc)
    path = os.path.join(state_dir, name)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"ts": ts.isoformat(), "pid": os.getpid()}, fh)
    return path


def _closed_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _patch_units(monkeypatch):
    from backend.app import units as units_lib

    monkeypatch.setattr(
        units_lib, "query_unit",
        lambda unit, timeout_s=10: {
            "state": "confirmed_stopped", "unit": unit,
            "active_state": "inactive", "sub_state": "dead",
            "main_pid": 0, "cgroup": "", "identity_ok": True,
            "detail": "manager=inactive"})
    monkeypatch.setattr(
        units_lib, "query_unit_system",
        lambda unit, timeout_s=10: {
            "state": "confirmed_stopped", "unit": unit,
            "active_state": "inactive", "sub_state": "dead",
            "main_pid": 0, "cgroup": "", "identity_ok": True,
            "detail": "manager=inactive"})
    monkeypatch.setattr(
        units_lib, "list_job_units",
        lambda prefix="ega-update-job-", timeout_s=10: ([], ""))


# -- stage 1: API service identity -------------------------------------------

def test_api_identity_probe_fails_when_csrf_secret_missing(tmp_path):
    """Engine-visible behavior: the ACTUAL identity cannot resolve the
    CSRF secret (missing/unreadable file) -> the stage fails."""
    from backend.app import deployment_readiness as dr

    layout = _api_layout(tmp_path, csrf_mode="missing")
    probe = dr.api_identity_probe(settings=layout["settings"],
                              config_file=layout["config_file"])
    assert probe["ok"] is False
    assert any("csrf" in reason for reason in probe["reasons"]), probe
    # Evidence is booleans + paths only.
    assert "fixture-csrf-secret-value" not in json.dumps(probe)


def test_api_identity_probe_passes_with_effective_access(tmp_path):
    from backend.app import deployment_readiness as dr

    layout = _api_layout(tmp_path)
    probe = dr.api_identity_probe(settings=layout["settings"],
                              config_file=layout["config_file"])
    assert probe["ok"] is True, probe
    assert probe["checks"]["db_open"]["ok"] is True
    assert probe["checks"]["config_file_read"]["ok"] is True
    assert probe["checks"]["state_dir_write"]["ok"] is True


def test_api_identity_probe_fails_on_unreadable_db(tmp_path):
    from backend.app import deployment_readiness as dr

    layout = _api_layout(tmp_path)
    os.chmod(str(tmp_path / "state" / "state.db"), 0o000)
    # Running as the same uid can still open (owner) unless we are not the
    # owner; make the parent directory non-traversable instead, which
    # denies even the owner the path lookup.
    os.chmod(str(tmp_path / "state"), 0o000)
    try:
        probe = dr.api_identity_probe(settings=layout["settings"],
                              config_file=layout["config_file"])
        assert probe["ok"] is False
    finally:
        os.chmod(str(tmp_path / "state"), 0o770)


def test_stage_api_service_identity_round_trip(tmp_path, monkeypatch):
    """The default runner spawns the probe under the configured API
    identity (here: the current uid) and parses its JSON."""
    from backend.app import deployment_readiness as dr

    layout = _api_layout(tmp_path)
    monkeypatch.setenv("EGA_RELEASE_ROOT", _REPO_ROOT)
    monkeypatch.setenv("EGA_CONFIG_FILE", layout["config_file"])
    stage = dr.stage_api_service_identity(layout["settings"])
    assert stage["ok"] is True, stage
    assert stage["evidence"]["user"]


def test_stage_api_service_identity_unresolvable_user_fails(tmp_path):
    from backend.app import deployment_readiness as dr

    settings = _settings(tmp_path, api_user="no-such-owner-xyz")
    stage = dr.stage_api_service_identity(settings)
    assert stage["ok"] is False
    assert any("identity" in r for r in stage["reasons"])


# -- stage 2: API security boundary ------------------------------------------

def test_stage_api_security_boundary_requires_loopback_rejection(tmp_path):
    from backend.app import deployment_readiness as dr

    settings = _settings(tmp_path, listen_host="0.0.0.0")
    stage = dr.stage_api_security_boundary(settings)
    assert stage["ok"] is False
    assert any("loopback" in r for r in stage["reasons"])

    settings = _settings(tmp_path, listen_host="127.0.0.1",
                         listen_port=_closed_port())
    stage = dr.stage_api_security_boundary(settings)
    assert stage["ok"] is False


def test_stage_api_security_boundary_rejects_anonymous_200(tmp_path):
    """A 200 for an unauthenticated request is an auth-boundary failure,
    never readiness evidence."""
    from backend.app import deployment_readiness as dr

    settings = _settings(tmp_path)
    stage = dr.stage_api_security_boundary(
        settings, http_probe=lambda host, port: {
            "reachable": True, "status": 200})
    assert stage["ok"] is False
    assert any("401" in r or "403" in r for r in stage["reasons"])

    stage = dr.stage_api_security_boundary(
        settings, http_probe=lambda host, port: {
            "reachable": True, "status": 401})
    assert stage["ok"] is True


# -- stage 3: worker process --------------------------------------------------

def test_stage_worker_process_uses_heartbeat_freshness(tmp_path):
    from backend.app import deployment_readiness as dr

    settings = _settings(tmp_path)
    dead = dr.stage_worker_process(
        settings, quiescence={"worker_alive": False})
    assert dead["ok"] is False
    live = dr.stage_worker_process(
        settings, quiescence={"worker_alive": True})
    assert live["ok"] is True


# -- stage 4: probe executor (durable marker, W3.1) ---------------------------

def test_probe_worker_writes_durable_ready_marker(tmp_path, monkeypatch):
    """The ProbeWorker thread itself publishes a durable ready marker;
    the dispatcher heartbeat alone proves nothing about it."""
    from backend.app import db as db_lib
    from backend.app.worker import dispatch as dispatch_lib

    monkeypatch.setattr(dispatch_lib.settings, "state_dir",
                        str(tmp_path / "state"), raising=False)
    os.makedirs(str(tmp_path / "state"), exist_ok=True)
    db_path = str(tmp_path / "state" / "state.db")
    conn = db_lib.connect(db_path)
    assert db_lib.migrate(conn) == db_lib.CODE_VERSION
    conn.commit()
    conn.close()

    assert dispatch_lib.read_probe_worker_heartbeat(
        str(tmp_path / "state")) == {}
    worker = dispatch_lib.ProbeWorker(db_path, interval_s=0.05)
    worker.start()
    try:
        marker = {}
        for _ in range(100):
            marker = dispatch_lib.read_probe_worker_heartbeat(
                str(tmp_path / "state"))
            if marker:
                break
            import time
            time.sleep(0.02)
        assert marker, "ProbeWorker never published a durable ready marker"
        assert marker.get("ready") is True
    finally:
        worker.stop(timeout_s=2.0)
    # Graceful stop withdraws the marker: the executor is no longer ready.
    assert dispatch_lib.read_probe_worker_heartbeat(
        str(tmp_path / "state")) == {}


def test_stage_probe_executor_requires_separate_marker(tmp_path):
    """A fresh dispatcher heartbeat is NOT probe-executor evidence."""
    from backend.app import deployment_readiness as dr
    from backend.app.worker import dispatch as dispatch_lib

    settings = _settings(tmp_path)
    _heartbeat(settings.state_dir)  # dispatcher alive
    stage = dr.stage_probe_executor(settings)
    assert stage["ok"] is False
    assert any("probe" in r.lower() for r in stage["reasons"])

    fresh = dispatch_lib.utcnow_iso() if hasattr(
        dispatch_lib, "utcnow_iso") else datetime.now(
        timezone.utc).isoformat()
    marker = os.path.join(settings.state_dir,
                          dispatch_lib.PROBE_WORKER_HEARTBEAT_FILENAME)
    with open(marker, "w", encoding="utf-8") as fh:
        json.dump({"ts": fresh, "pid": os.getpid(), "ready": True}, fh)
    stage = dr.stage_probe_executor(settings)
    assert stage["ok"] is True, stage

    with open(marker, "w", encoding="utf-8") as fh:
        json.dump({"ts": (datetime.now(timezone.utc) - timedelta(
            seconds=120)).isoformat(), "pid": os.getpid(),
            "ready": True}, fh)
    assert dr.stage_probe_executor(settings)["ok"] is False


# -- stage 5: canonical owner transient acceptance ----------------------------

def _acceptance_paths(tmp_path):
    work = tmp_path / "accept"
    work.mkdir(parents=True, exist_ok=True)
    os.chmod(str(work), 0o700)
    state = tmp_path / "state"
    for d in (state, state / "logs", state / "backups"):
        d.mkdir(parents=True, exist_ok=True)
    etc = tmp_path / "etc"
    etc.mkdir(parents=True, exist_ok=True)
    cfg = etc / "config.json"
    cfg.write_text("{}", encoding="utf-8")
    secrets = etc / "secrets.env"
    secrets.write_text("", encoding="utf-8")
    return {
        "owner": _current_user(), "group": "ega-update",
        "state_dir": str(state), "log_dir": str(state / "logs"),
        "backup_dir": str(state / "backups"), "config_dir": str(etc),
        "config_file": str(cfg), "secrets_file": str(secrets),
        "inventory_file": "", "work_dir": str(work),
        "release_root": _REPO_ROOT, "venv_python": sys.executable,
    }


def _live_bus_socket(tmp_path):
    path = str(tmp_path / "bus.sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(1)
    return srv, path


def _argval(argv, flag):
    inner = argv[-1]
    import shlex
    tokens = shlex.split(inner)
    return tokens[tokens.index(flag) + 1]


class _FakeTransient(object):
    """Simulates the process boundary: launch, then termination query."""

    def __init__(self, tmp_path, *, uid=None, report_uid=None,
                 result=True, result_read=True, checks_ok=True,
                 termination="inactive", launch_code=0, report_ok=True):
        self.calls = []
        self.uid = os.getuid() if uid is None else uid
        self.report_uid = self.uid if report_uid is None else report_uid
        self.result = result
        self.result_read = result_read
        self.checks_ok = checks_ok
        self.termination = termination
        self.launch_code = launch_code
        self.report_ok = report_ok
        self.work = tmp_path / "accept"

    def __call__(self, argv, env=None, timeout_s=60.0):
        self.calls.append(list(argv))
        inner = argv[-1]
        if "systemd-run" in inner:
            return self._launch()
        return self._terminate()

    def _launch(self):
        payload_path = self.work / "payload.json"
        with open(str(payload_path), "r", encoding="utf-8") as fh:
            nonce = json.load(fh)["nonce"]
        checks = {}
        for name in ("state_dir_traverse", "state_dir_write",
                     "log_dir_write", "backup_dir_write",
                     "config_dir_traverse", "config_file_read",
                     "secrets_file_read", "payload_read",
                     "result_write", "result_read"):
            checks[name] = {"ok": bool(self.checks_ok), "path": "redacted"}
        if not self.result_read:
            checks["result_read"] = {"ok": False, "path": "redacted"}
        report = {"ok": bool(self.report_ok), "profile":
                  "owner-exec-effective-access", "uid": self.report_uid,
                  "user": _current_user(), "checks": checks,
                  "reasons": [] if self.report_ok else ["forced"]}
        with open(str(self.work / "report.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(report, fh)
        if self.result:
            with open(str(self.work / "result.json"), "w",
                      encoding="utf-8") as fh:
                json.dump({"nonce": nonce, "uid": self.uid,
                           "profile": "owner-exec-effective-access"}, fh)
        return {"returncode": self.launch_code, "stdout": "",
                "stderr": "" if self.launch_code == 0 else "unit failed"}

    def _terminate(self):
        if self.termination == "not-found":
            return {"returncode": 1, "stdout": "",
                    "stderr": "Unit ega-update-accept.service not found."}
        return {"returncode": 0,
                "stdout": "ActiveState=%s\nSubState=dead\nResult=success\n"
                          % self.termination, "stderr": ""}


def test_transient_acceptance_bus_unavailable_fails(tmp_path):
    from backend.app import owner_env

    paths = _acceptance_paths(tmp_path)
    result = owner_env.transient_acceptance(
        **paths, bus_path=str(tmp_path / "no-such-bus.sock"),
        command_runner=_FakeTransient(tmp_path))
    assert result["ok"] is False
    assert any("bus" in r.lower() for r in result["reasons"])


def test_transient_acceptance_identity_mismatch_fails(tmp_path):
    from backend.app import owner_env

    srv, bus = _live_bus_socket(tmp_path)
    try:
        paths = _acceptance_paths(tmp_path)
        fake = _FakeTransient(tmp_path, report_uid=999999)
        result = owner_env.transient_acceptance(
            **paths, bus_path=bus, command_runner=fake, keep_work=True)
        assert result["ok"] is False
        assert any("identity" in r.lower() for r in result["reasons"])
    finally:
        srv.close()


def test_transient_acceptance_unreadable_paths_fails(tmp_path):
    """Membership looks right but effective access fails inside the
    transient identity -> acceptance fails."""
    from backend.app import owner_env

    srv, bus = _live_bus_socket(tmp_path)
    try:
        paths = _acceptance_paths(tmp_path)
        fake = _FakeTransient(tmp_path, checks_ok=False)
        result = owner_env.transient_acceptance(
            **paths, bus_path=bus, command_runner=fake, keep_work=True)
        assert result["ok"] is False
        assert any("payload" in r or "config" in r or "state_dir" in r
                   for r in result["reasons"])
    finally:
        srv.close()


def test_transient_acceptance_result_unreadable_fails(tmp_path):
    from backend.app import owner_env

    srv, bus = _live_bus_socket(tmp_path)
    try:
        paths = _acceptance_paths(tmp_path)
        fake = _FakeTransient(tmp_path, result=False)
        result = owner_env.transient_acceptance(
            **paths, bus_path=bus, command_runner=fake, keep_work=True)
        assert result["ok"] is False
        assert any("result" in r.lower() for r in result["reasons"])

        fake = _FakeTransient(tmp_path, result_read=False)
        result = owner_env.transient_acceptance(
            **paths, bus_path=bus, command_runner=fake, keep_work=True)
        assert result["ok"] is False
        assert any("result" in r.lower() for r in result["reasons"])
    finally:
        srv.close()


def test_transient_acceptance_stop_not_proven_fails(tmp_path):
    from backend.app import owner_env

    srv, bus = _live_bus_socket(tmp_path)
    try:
        paths = _acceptance_paths(tmp_path)
        fake = _FakeTransient(tmp_path, termination="active")
        result = owner_env.transient_acceptance(
            **paths, bus_path=bus, command_runner=fake, keep_work=True)
        assert result["ok"] is False
        assert any("terminat" in r.lower() or "stop" in r.lower()
                   for r in result["reasons"])
    finally:
        srv.close()


def test_transient_acceptance_happy_path(tmp_path):
    from backend.app import owner_env

    srv, bus = _live_bus_socket(tmp_path)
    try:
        paths = _acceptance_paths(tmp_path)
        fake = _FakeTransient(tmp_path)
        result = owner_env.transient_acceptance(
            **paths, bus_path=bus, command_runner=fake, keep_work=True)
        assert result["ok"] is True, result
        assert result["unit_terminated"] is True
        assert result["identity_ok"] is True
        assert result["result_ok"] is True
        # The transient unit is launched with the canonical primitive and
        # the termination is positively queried afterwards.
        assert any("systemd-run" in " ".join(call) for call in fake.calls)
        assert any("systemctl" in " ".join(call) for call in fake.calls)
        dumped = json.dumps(result)
        assert "fixture-secret" not in dumped
    finally:
        srv.close()


def test_execution_credentials_probe_writes_typed_result(tmp_path):
    from backend.app import owner_env

    paths = _acceptance_paths(tmp_path)
    result_path = str(tmp_path / "accept" / "result.json")
    report = owner_env.execution_credentials_probe(
        owner=paths["owner"], group=paths["group"],
        state_dir=paths["state_dir"], log_dir=paths["log_dir"],
        backup_dir=paths["backup_dir"], config_dir=paths["config_dir"],
        config_file=paths["config_file"],
        secrets_file=paths["secrets_file"],
        inventory_file=paths["inventory_file"],
        result_path=result_path, nonce="nonce-123")
    assert report["ok"] is True, report
    assert report["checks"]["result_write"]["ok"] is True
    assert report["checks"]["result_read"]["ok"] is True
    with open(result_path, "r", encoding="utf-8") as fh:
        written = json.load(fh)
    assert written["nonce"] == "nonce-123"


def test_stage_owner_transient_execution_propagates_failure(tmp_path):
    from backend.app import deployment_readiness as dr

    settings = _settings(tmp_path)
    stage = dr.stage_owner_transient_execution(
        settings, transient_runner=lambda: {
            "ok": False, "reasons": ["user bus unavailable"]})
    assert stage["ok"] is False
    assert stage["reasons"] == ["user bus unavailable"]
    stage = dr.stage_owner_transient_execution(
        settings, transient_runner=lambda: {"ok": True, "reasons": []})
    assert stage["ok"] is True


# -- aggregate readiness ------------------------------------------------------

def _all_good_stages(tmp_path):
    settings = _settings(tmp_path)
    return settings, dict(
        quiescence={"worker_alive": True, "quiescent": True,
                    "active_job": "", "drain": True},
        identity_runner=lambda argv, env=None: {
            "returncode": 0,
            "stdout": json.dumps(
                {"ok": True, "uid": os.getuid()}) + "\n", "stderr": ""},
        http_probe=lambda host, port: {"reachable": True, "status": 401},
        probe_executor_probe=lambda: {"ready": True},
        transient_runner=lambda: {"ok": True, "reasons": []},
    )


def test_readiness_requires_all_mandatory_stages(tmp_path):
    from backend.app import deployment_readiness as dr

    settings, kwargs = _all_good_stages(tmp_path)
    result = dr.assess_deployment_readiness(settings, **kwargs)
    assert result["ready"] is True, result
    assert result["schema_version"] == dr.READINESS_SCHEMA_VERSION
    assert set(result["stages"]) == set(dr.MANDATORY_STAGES)
    assert result["failed_stages"] == []

    for stage in dr.MANDATORY_STAGES:
        broken = dict(kwargs)
        if stage == "api_service_identity":
            broken["identity_runner"] = lambda argv, env=None: {
                "returncode": 1, "stdout": "", "stderr": "x"}
        elif stage == "api_security_boundary":
            broken["http_probe"] = lambda host, port: {
                "reachable": True, "status": 200}
        elif stage == "worker_process":
            broken["quiescence"] = {"worker_alive": False}
        elif stage == "probe_executor":
            broken["probe_executor_probe"] = lambda: {}
        else:
            broken["transient_runner"] = lambda: {
                "ok": False, "reasons": ["no"]}
        result = dr.assess_deployment_readiness(settings, **broken)
        assert result["ready"] is False, stage
        assert stage in result["failed_stages"], (stage, result)


def test_http_401_alone_never_makes_readiness_ready(tmp_path):
    """The security boundary is one stage; overall readiness must still
    fail when the owner transient proof fails."""
    from backend.app import deployment_readiness as dr

    settings, kwargs = _all_good_stages(tmp_path)
    kwargs["transient_runner"] = lambda: {
        "ok": False, "reasons": ["transient unit stop not proven"]}
    result = dr.assess_deployment_readiness(settings, **kwargs)
    assert result["ready"] is False
    assert result["stages"]["api_security_boundary"]["ok"] is True
    assert "owner_transient_execution" in result["failed_stages"]


def test_worker_alive_but_probe_executor_missing_is_not_ready(
        tmp_path, monkeypatch):
    """Heartbeat current + 401 + good API identity + good transient proof
    is STILL not ready while the required probe executor marker is absent
    (the defect-2 scenario, proven end to end at the assessment level)."""
    from backend.app import deployment_readiness as dr

    settings, kwargs = _all_good_stages(tmp_path)
    monkeypatch.setenv("EGA_RELEASE_ROOT", str(tmp_path))
    # Real dispatcher heartbeat (worker process stage source) but no
    # probe_worker.heartbeat marker.
    os.makedirs(settings.state_dir, exist_ok=True)
    _heartbeat(settings.state_dir, name="dispatcher.heartbeat")
    kwargs["probe_executor_probe"] = None
    result = dr.assess_deployment_readiness(settings, **kwargs)
    assert result["ready"] is False
    assert result["failed_stages"] == ["probe_executor"], result
    assert result["stages"]["worker_process"]["ok"] is True
    assert result["stages"]["api_security_boundary"]["ok"] is True


def test_readiness_failure_artifacts_contain_no_secret(tmp_path):
    from backend.app import deployment_readiness as dr

    secret = "SUPER-SECRET-CSRF-VALUE-XYZ"
    layout = _api_layout(tmp_path)
    with open(str(tmp_path / "etc" / "csrf.secret"), "w",
              encoding="utf-8") as fh:
        fh.write(secret + "\n")
    with open(str(tmp_path / "etc" / "secrets.env"), "w",
              encoding="utf-8") as fh:
        fh.write("KNOWN_SECRET=%s\n" % secret)
    settings = layout["settings"]
    stage = dr.stage_api_service_identity(
        settings, runner=lambda argv, env=None: {
            "returncode": 1, "stdout": "", "stderr": secret})
    result = dr.assess_deployment_readiness(
        settings, quiescence={"worker_alive": True},
        identity_runner=lambda argv, env=None: {
            "returncode": 1, "stdout": "", "stderr": secret},
        http_probe=lambda host, port: {"reachable": True, "status": 401},
        probe_executor_probe=lambda: {"ready": True},
        transient_runner=lambda: {"ok": False,
                                  "reasons": ["result_path unreadable"]})
    dumped = json.dumps({"stage": stage, "result": result})
    assert secret not in dumped


# -- CLI gate -----------------------------------------------------------------

def _cli_env(tmp_path, monkeypatch, port):
    """Real migrated DB + quiescence inputs + patched unit model."""
    from backend.app import cli as cli_lib
    from backend.app import db as db_lib

    settings = _settings(
        tmp_path, listen_port=port,
        api_user="no-such-api-identity-xyz",
        tool_owner="no-such-owner-xyz")
    os.makedirs(settings.state_dir, exist_ok=True)
    conn = db_lib.connect(settings.db_path)
    assert db_lib.migrate(conn) == db_lib.CODE_VERSION
    conn.commit()
    conn.close()
    _heartbeat(settings.state_dir, name="dispatcher.heartbeat")
    open(os.path.join(settings.state_dir, "drain"), "w").close()
    _patch_units(monkeypatch)
    monkeypatch.setattr(cli_lib, "_load_settings", lambda: settings)
    return cli_lib, settings


def test_cli_require_ready_rejects_heartbeat_only(tmp_path, monkeypatch,
                                                  capsys):
    """Defect D8-2: on the base commit a fresh dispatcher heartbeat made
    `status --require-ready` exit 0. It must now fail with the failed
    stages identified in machine-readable output."""
    cli_lib, _settings_ = _cli_env(tmp_path, monkeypatch, _closed_port())
    args = argparse.Namespace(require_ready=True, require_quiescent=False)
    code = cli_lib.cmd_status(args)
    assert code == cli_lib.EXIT_BLOCKED
    raw = capsys.readouterr()
    assert "fixture-csrf-secret-value" not in raw.out
    assert "fixture-csrf-secret-value" not in raw.err
    out = json.loads(raw.out.strip().splitlines()[-1])
    detail = out["detail"]
    assert detail["ready"] is False
    assert "api_service_identity" in detail["failed_stages"]
    assert "owner_transient_execution" in detail["failed_stages"]
    assert "probe_executor" in detail["failed_stages"]
    assert "worker_process" not in detail["failed_stages"]


def test_cli_require_quiescent_unchanged(tmp_path, monkeypatch, capsys):
    cli_lib, _ = _cli_env(tmp_path, monkeypatch, _closed_port())
    args = argparse.Namespace(require_ready=False, require_quiescent=True)
    code = cli_lib.cmd_status(args)
    assert code == cli_lib.EXIT_OK
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["state"] == "quiescent"
    assert out["detail"]["quiescent"] is True


def test_cli_plain_status_backward_compatible(tmp_path, monkeypatch, capsys):
    cli_lib, settings = _cli_env(tmp_path, monkeypatch, _closed_port())
    args = argparse.Namespace(require_ready=False, require_quiescent=False)
    code = cli_lib.cmd_status(args)
    assert code == cli_lib.EXIT_OK
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    detail = out["detail"]
    for key in ("worker_alive", "active_job", "unresolved_jobs",
                "recovery_jobs", "live_units", "delegated_operations",
                "drain", "quiescent", "reasons"):
        assert key in detail, key
    assert detail["worker_alive"] is True

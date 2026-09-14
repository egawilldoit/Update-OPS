#!/usr/bin/env python3
"""Manual disposable-VM contract runner. Never treats missing evidence as GREEN."""
from __future__ import annotations

import argparse
import datetime as dt
import grp
import hashlib
import json
import os
from pathlib import Path
import platform
import pwd
import sqlite3
import signal
import stat
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
BASE = "8d243a7a8cda8a1b53ffad91a934a8b00c812c8c"
REQUIRED = (
    "users_acl_stale_manager", "api_identity", "csrf_unreadable", "worker_liveness_restart",
    "user_bus_negative", "transient_launch_failure", "unit_state_unknown", "sqlite_wal",
    "flock", "atomic_pointer", "complete_install", "canonical_readiness",
    "mandatory_stage_failures", "reboot_after_drain", "reboot_after_migration",
    "reboot_after_switch",
)
STAGES = ("api_service_identity", "api_security_boundary", "worker_process",
          "probe_executor", "owner_transient_execution")


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def command(argv, *, env=None, cwd=ROOT, timeout=120):
    # Raw output stays in memory. Reports select typed fields, never transcripts.
    return subprocess.run([str(x) for x in argv], capture_output=True, text=True,
                          env=env, cwd=cwd, timeout=timeout)


def require(value, reason):
    if not value:
        raise AssertionError(reason)


def guard(marker):
    require(os.geteuid() == 0, "root required on designated disposable VM")
    require(not Path("/etc/ega-update/PRODUCTION").exists(), "production forbidden")
    require(Path("/run/systemd/system").is_dir(), "systemd host required")
    info = marker.stat()
    require(info.st_uid == 0 and not stat.S_IMODE(info.st_mode) & 0o022,
            "designation must be root-owned and not writable by group/other")
    designation = json.loads(marker.read_text())
    require(designation.get("disposable") is True, "disposable designation missing")
    require(designation.get("machine_id") == Path("/etc/machine-id").read_text().strip(),
            "designation belongs to another machine")


def filesystem_type(path):
    proc = command(["findmnt", "-n", "-T", path, "-o", "FSTYPE"])
    value = proc.stdout.strip()
    require(proc.returncode == 0 and value and all(c.isalnum() or c in "_.-" for c in value),
            "filesystem type unavailable")
    return value


def environment():
    release = {}
    for line in Path("/etc/os-release").read_text().splitlines():
        key, _, value = line.partition("=")
        if key in ("ID", "VERSION_ID", "PRETTY_NAME"):
            release[key] = value.strip('"')
    fs = command(["findmnt", "-J", "-T", str(ROOT), "-o", "FSTYPE,TARGET,OPTIONS"])
    systemd = command(["systemctl", "--version"])
    mounts = []
    if fs.returncode == 0:
        for mount in json.loads(fs.stdout).get("filesystems", []):
            options = set(mount.get("options", "").split(","))
            mounts.append({"filesystem_type": mount.get("fstype"),
                           "mount_type": "bind" if "bind" in options else "filesystem",
                           "access": sorted(options & {"ro", "rw", "noexec", "nosuid", "nodev", "acl"})})
    return {"distro": release, "kernel": platform.release(), "architecture": platform.machine(),
            "systemd": systemd.stdout.splitlines()[0] if systemd.returncode == 0 else "unavailable",
            "python": platform.python_version(), "uid": os.getuid(), "gid": os.getgid(),
            "supplementary_groups": os.getgroups(),
            "filesystem": mounts,
            "production_equivalence": False, "real_vm_designated": False}


def report(case, started, status, evidence, failure=""):
    return {"commit_sha": BASE, "environment": environment(), "test_case": case,
            "started_at": started, "finished_at": now(), "status": status,
            "evidence": evidence, "failure_reason": failure}


def write_report(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as output:
        json.dump(value, output, indent=2, sort_keys=True)
        output.write("\n")
        staged = output.name
    os.replace(staged, path)


def unit_properties(unit, keys, user_env=None, owner=None):
    argv = ["systemctl"]
    if owner:
        argv = ["runuser", "-u", owner, "--", "systemctl", "--user"]
    proc = command(argv + ["show", unit] + ["--property=" + key for key in keys], env=user_env)
    require(proc.returncode == 0, "unit state unknown")
    values = dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)
    require(set(keys) <= values.keys(), "unit properties missing")
    return values


class Suite:
    def __init__(self, config):
        self.config = config
        self.settings = json.loads(config.read_text())
        self.release = Path("/opt/ega-update/current").resolve(strict=True)
        self.python = self.release / "venv/bin/python"
        self.env = {**os.environ, "EGA_CONFIG_FILE": str(config), "PYTHONPATH": str(self.release)}
        self.owner = self.settings.get("tool_owner", "ubuntu")
        self.state = Path(self.settings["state_dir"])

    def module(self, module, *args, user=None, env=None):
        argv = [self.python, "-m", "backend.app." + module, *args]
        if user:
            argv = ["runuser", "-u", user, "--", *argv]
        return command(argv, env=env or self.env, cwd=self.release, timeout=180)

    def canonical_readiness(self):
        proc = self.module("deployment_readiness", "assess", "--config-file", self.config)
        result = json.loads(proc.stdout)
        stages = {name: result.get("stages", {}).get(name, {}).get("ok") is True for name in STAGES}
        require(proc.returncode == 0 and result.get("ready") is True and all(stages.values()),
                "mandatory readiness failed; stop case and investigate product behavior")
        return {"stages": stages, "exit_code": proc.returncode}

    def api_identity(self):
        proc = self.module("deployment_readiness", "api-identity-probe", "--config-file", self.config, user="ega-update")
        result = json.loads(proc.stdout)
        require(proc.returncode == 0 and result.get("ok") is True, "API identity unusable")
        require(result.get("uid") == pwd.getpwnam("ega-update").pw_uid, "wrong API identity")
        checks = {k: v.get("ok") is True for k, v in result.get("checks", {}).items()}
        for protected in (self.config, self.release):
            require(command(["runuser", "-u", "ega-update", "--", "test", "-w", protected]).returncode != 0,
                    "API can write protected configuration/release")
        return {"uid": result["uid"], "checks": checks, "protected_paths_not_writable": True}

    def csrf_unreadable(self):
        self.api_identity()
        secret = Path(self.settings["csrf_secret_file"])
        require(not secret.is_symlink(), "CSRF fixture must be a regular file")
        saved = secret.stat()
        try:
            os.chown(secret, 0, 0)
            secret.chmod(0)
            result = self.module("deployment_readiness", "api-identity-probe", "--config-file", self.config, user="ega-update")
            require(result.returncode != 0, "unreadable CSRF secret accepted")
            gate = self.module("deployment_readiness", "assess", "--config-file", self.config)
            require(gate.returncode != 0, "readiness accepted unreadable CSRF secret")
        finally:
            os.chown(secret, saved.st_uid, saved.st_gid)
            secret.chmod(stat.S_IMODE(saved.st_mode))
        self.api_identity()
        return {"negative_identity_exit": result.returncode, "negative_readiness_exit": gate.returncode, "restored": True}

    def users_acl_stale_manager(self):
        account = pwd.getpwnam(self.owner)
        shared = grp.getgrnam("ega-update").gr_gid
        require(account.pw_gid != shared, "owner primary group must differ from shared group")
        # Disposable only: deliberately restart with the pre-provisioning group vector.
        if shared in os.getgrouplist(self.owner, account.pw_gid):
            require(command(["gpasswd", "-d", self.owner, "ega-update"]).returncode == 0, "remove shared membership failed")
        try:
            require(command(["loginctl", "enable-linger", self.owner]).returncode == 0, "linger failed")
            require(command(["systemctl", "restart", f"user@{account.pw_uid}.service"]).returncode == 0, "user manager failed")
            manager = unit_properties(f"user@{account.pw_uid}.service", ["MainPID"])
            pid = int(manager["MainPID"])
            def groups():
                return [int(v) for line in Path(f"/proc/{pid}/status").read_text().splitlines()
                        if line.startswith("Groups:") for v in line.split()[1:]]
            before = groups()
            require(shared not in before, "manager did not start without shared group")
        finally:
            require(command(["usermod", "-a", "-G", "ega-update", self.owner]).returncode == 0, "restore group failed")
        require(self.module("owner_env", "provision", "--owner", self.owner).returncode == 0, "named-user access provisioning failed")
        after = groups()
        require(before == after and shared in os.getgrouplist(self.owner, account.pw_gid), "stale credential sequence unproven")
        runtime = f"/run/user/{account.pw_uid}"
        user_env = {**self.env, "XDG_RUNTIME_DIR": runtime, "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime}/bus"}
        require(Path(runtime, "bus").exists(), "user bus absent")
        with tempfile.TemporaryDirectory(prefix="vm-contract-", dir=self.state) as temp:
            scratch = Path(temp)
            os.chown(scratch, 0, shared)
            scratch.chmod(0o750)
            require(command(["setfacl", "-m", f"u:{self.owner}:rwx,d:u:{self.owner}:rwx", scratch]).returncode == 0, "named ACL failed")
            (scratch / "payload.json").write_text('{"kind":"vm-contract","value":7}\n')
            (scratch / "payload.json").chmod(0o640)
            require(command(["setfacl", "-m", f"u:{self.owner}:r", scratch / "payload.json"]).returncode == 0, "payload ACL failed")
            unit = f"ega-vm-contract-{os.getpid()}"
            proc = command(["runuser", "-u", self.owner, "--", "systemd-run", "--user", "--wait", "--unit=" + unit,
                            self.python, ROOT / "deploy/tests/vm-contract-payload.py", scratch, self.config], env=user_env)
            require(proc.returncode == 0, "transient launch/round trip failed")
            stopped = command(["runuser", "-u", self.owner, "--", "systemctl", "--user", "show", unit,
                               "--property=ActiveState,SubState,LoadState"], env=user_env)
            props = dict(line.split("=", 1) for line in stopped.stdout.splitlines() if "=" in line)
            require(stopped.returncode in (0, 1) and props.get("ActiveState") == "inactive"
                    and props.get("SubState") == "dead" and props.get("LoadState") in ("loaded", "not-found"),
                    "transient not positively stopped after successful --wait")
            typed = json.loads((scratch / "result.json").read_text())
            require(typed["uid"] == account.pw_uid and shared not in typed["groups"], "transient credentials not stale owner")
            require((scratch / "stream.log").read_text() == "fixture round trip\n", "stream artifact missing")
            require(self.module("owner_env", "accept").returncode == 0, "canonical owner acceptance failed")
        return {"owner_uid": account.pw_uid, "owner_gid": account.pw_gid, "shared_gid": shared,
                "manager_pid": pid, "groups_before": before, "groups_after": after,
                "transient": typed, "stopped": props, "canonical_owner_acceptance": True}

    def user_bus_negative(self):
        uid = pwd.getpwnam(self.owner).pw_uid
        env = {**self.env, "XDG_RUNTIME_DIR": f"/run/user/{uid}", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/ega-acceptance-missing-bus"}
        codes = [command(["runuser", "-u", self.owner, "--", *argv], env=env).returncode for argv in
                 (["systemctl", "--user", "show-environment"], ["systemd-run", "--user", "--wait", "/bin/true"])]
        require(all(c != 0 for c in codes), "missing bus accepted")
        return {"systemctl_exit": codes[0], "systemd_run_exit": codes[1], "canonical_fail_closed_still_requires_stage_injection": True}

    def transient_launch_failure(self):
        proc = self.module("owner_env", "accept", "--venv-python", "/bin/false")
        result = json.loads(proc.stdout)
        require(proc.returncode != 0 and isinstance(result.get("launch_exit"), int)
                and result["launch_exit"] != 0 and result.get("ok") is False,
                "transient launch failure accepted or launch was not attempted")
        return {"launch_exit": result["launch_exit"], "accepted": False}

    def unit_state_unknown(self):
        proc = command([self.python, ROOT / "deploy/tests/vm-transient-unknown.py"], env=self.env, cwd=self.release)
        require(proc.returncode == 0, "unknown-state fixture did not finish")
        result = json.loads(proc.stdout)
        require(result.get("launch_exit") == 0 and result.get("report_ok") is True
                and result.get("identity_ok") is True and result.get("result_ok") is True,
                "unknown-state case did not first prove a successful round trip")
        require(result.get("ok") is False and result.get("unit_terminated") is False
                and result.get("unit_state") == "unproven", "unknown unit state accepted")
        return result

    def sqlite_wal(self):
        with tempfile.TemporaryDirectory(prefix="vm-sqlite-", dir=self.state) as temp:
            path = Path(temp) / "fixture.db"
            backup = Path(temp) / "backup.db"
            writer = sqlite3.connect(path)
            reader = sqlite3.connect(path)
            try:
                require(writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal", "WAL unavailable")
                writer.execute("CREATE TABLE fixture(value INTEGER)")
                writer.execute("INSERT INTO fixture VALUES (1)")
                writer.commit()
                reader.execute("BEGIN")
                require(reader.execute("SELECT count(*) FROM fixture").fetchone()[0] == 1, "reader snapshot failed")
                writer.execute("INSERT INTO fixture VALUES (2)")
                writer.commit()
                require(reader.execute("SELECT count(*) FROM fixture").fetchone()[0] == 1, "WAL isolation failed")
                service = pwd.getpwnam("ega-update")
                shared = grp.getgrnam("ega-update").gr_gid
                os.chown(temp, service.pw_uid, shared)
                os.chmod(temp, 0o2770)
                require(command(["setfacl", "-m", f"u:{self.owner}:rwx,d:u:{self.owner}:rwx", temp]).returncode == 0, "SQLite directory ACL failed")
                for file in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
                    os.chown(file, service.pw_uid, shared)
                    file.chmod(0o660)
                    require(command(["setfacl", "-m", f"u:{self.owner}:rw", file]).returncode == 0, "SQLite file ACL failed")
                identity_code = "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute('INSERT INTO fixture VALUES (3)'); c.commit(); assert c.execute('PRAGMA integrity_check').fetchone()[0]=='ok'; c.close()"
                for user in ("ega-update", self.owner):
                    require(command(["runuser", "-u", user, "--", self.python, "-c", identity_code, path]).returncode == 0,
                            "SQLite API/worker identity connection failed")
                require(command(["runuser", "-u", "nobody", "--", "test", "-r", path]).returncode != 0, "unrelated identity can read fixture DB")
                require(Path(str(path) + "-wal").stat().st_size > 0, "WAL not present at backup")
                proc = self.module("db", "backup", path, backup)
                require(proc.returncode == 0, "actual deployment DB backup failed")
            finally:
                reader.close()
                writer.close()
            with sqlite3.connect(backup) as saved, sqlite3.connect(path) as reopened:
                for db in (saved, reopened):
                    require(db.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "SQLite integrity failed")
                    require(db.execute("SELECT count(*) FROM fixture").fetchone()[0] == 4, "backup/reopen lost WAL rows")
        return {"wal_at_backup": True, "concurrent_snapshot_isolation": True, "backup_rows": 4,
                "integrity_check": "ok", "api_and_worker_identity_read_write": True, "filesystem_type": filesystem_type(self.state)}

    def flock(self):
        lock = "/opt/ega-update/deploy.lock"
        # exec eliminates a shell child retaining the lock after the holder crashes.
        holder = subprocess.Popen(["flock", "-n", "-F", lock, sys.executable, "-c",
                                   "import time; print('locked',flush=True); time.sleep(60)"], stdout=subprocess.PIPE, text=True)
        try:
            require(holder.stdout.readline().strip() == "locked", "first lock unavailable")
            require(command(["flock", "-n", lock, "/bin/true"]).returncode != 0, "second lock admitted")
            holder.kill()
            holder.wait(timeout=5)
            require(command(["flock", "-n", lock, "/bin/true"]).returncode == 0, "kernel did not release lock")
        finally:
            if holder.poll() is None:
                holder.kill()
                holder.wait(timeout=5)
        return {"separate_processes": True, "contention_refused": True, "released_after_sigkill": True}

    def worker_liveness_restart(self):
        self.canonical_readiness()
        props = unit_properties("ega-update-worker", ["MainPID", "ActiveState", "NRestarts"])
        require(props["ActiveState"] == "active" and int(props["MainPID"]) > 0, "worker process absent")
        pid = int(props["MainPID"])
        require(Path(f"/proc/{pid}").is_dir(), "dispatcher PID missing")
        markers = {}
        for name in ("dispatcher.heartbeat", "probe_worker.heartbeat"):
            marker = json.loads((self.state / name).read_text())
            require(int(marker["pid"]) == pid, "heartbeat belongs to another process")
            if name == "probe_worker.heartbeat":
                require(marker.get("ready") is True, "ProbeWorker heartbeat is not ready")
            age = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(marker["ts"])).total_seconds()
            require(0 <= age < 30, "heartbeat stale")
            markers[name] = {"pid": pid, "age_s": age}
        dropin = Path("/run/systemd/system/ega-update-worker.service.d/90-vm-contract.conf")
        require(not dropin.exists(), "worker fault override already exists")
        marker = self.state / "vm-worker-fault.json"
        require(not marker.exists(), "worker fault evidence already exists")
        dropin.parent.mkdir(parents=True, exist_ok=True)
        try:
            dropin.write_text("[Service]\nExecStart=\nExecStart=" + str(self.python) + " " + str(ROOT / "deploy/tests/vm-worker-fault.py") + "\nEnvironment=PYTHONPATH=" + str(self.release) + "\n")
            require(command(["systemctl", "daemon-reload"]).returncode == 0, "daemon reload failed")
            require(command(["systemctl", "restart", "ega-update-worker"]).returncode == 0, "fault worker startup failed")
            deadline = time.monotonic() + 60
            initial = unit_properties("ega-update-worker", ["NRestarts"])
            while time.monotonic() < deadline:
                after = unit_properties("ega-update-worker", ["NRestarts", "MainPID"])
                if int(after["NRestarts"]) > int(initial["NRestarts"]) and marker.exists():
                    break
                time.sleep(1)
            else:
                raise AssertionError("ProbeWorker failure did not cause systemd restart")
            fault = json.loads(marker.read_text())
            require(fault.get("probe_ready_before_fault") is True and fault.get("probe_stopped") is True
                    and fault.get("reconcile_boot_completed") is True and fault.get("dispatcher_failed") is True, "worker fault evidence incomplete")
        finally:
            dropin.unlink(missing_ok=True)
            command(["systemctl", "daemon-reload"])
            require(command(["systemctl", "restart", "ega-update-worker"]).returncode == 0, "worker restoration failed")
        for attempt in range(20):
            try:
                self.canonical_readiness()
                break
            except AssertionError:
                if attempt == 19:
                    raise
                time.sleep(1)
        marker.unlink()
        return {"independent_markers": markers, "fault": fault, "systemd_restart_observed": True,
                "fault_method": "actual ProbeWorker.stop after startup; actual dispatcher main and system service"}

    def mandatory_stage_failures(self):
        self.canonical_readiness()
        observed = {}
        owner_uid = pwd.getpwnam(self.owner).pw_uid
        for stage in STAGES:
            changed = None
            saved = None
            stopped = None
            frozen_pid = None
            try:
                if stage == "api_service_identity":
                    changed = Path(self.settings["csrf_secret_file"])
                    saved = changed.stat()
                    os.chown(changed, 0, 0)
                    changed.chmod(0)
                elif stage == "api_security_boundary":
                    stopped = "ega-update-api"
                    require(command(["systemctl", "stop", stopped]).returncode == 0, "API stop injection failed")
                elif stage in ("worker_process", "probe_executor"):
                    frozen_pid = int(unit_properties("ega-update-worker", ["MainPID"])["MainPID"])
                    require(frozen_pid > 0, "worker PID missing for injection")
                    os.kill(frozen_pid, signal.SIGSTOP)
                    changed = self.state / ("dispatcher.heartbeat" if stage == "worker_process" else "probe_worker.heartbeat")
                    backup = changed.with_name(changed.name + ".vm-acceptance-saved")
                    require(not backup.exists(), "saved heartbeat already exists")
                    changed.rename(backup)
                    saved = backup
                else:
                    stopped = f"user@{owner_uid}.service"
                    require(command(["systemctl", "stop", stopped]).returncode == 0, "user manager stop injection failed")
                gate = self.module("deployment_readiness", "assess", "--config-file", self.config)
                result = json.loads(gate.stdout)
                require(gate.returncode != 0 and result.get("stages", {}).get(stage, {}).get("ok") is False,
                        "injected mandatory stage did not fail readiness")
                observed[stage] = {"exit_code": gate.returncode, "stage_failed": True}
            finally:
                if changed is not None and saved is not None:
                    if isinstance(saved, Path):
                        if saved.exists():
                            saved.replace(changed)
                    else:
                        os.chown(changed, saved.st_uid, saved.st_gid)
                        changed.chmod(stat.S_IMODE(saved.st_mode))
                if frozen_pid is not None:
                    os.kill(frozen_pid, signal.SIGCONT)
                if stopped is not None:
                    require(command(["systemctl", "start", stopped]).returncode == 0, "injection restoration failed")
            for attempt in range(20):
                try:
                    self.canonical_readiness()
                    break
                except AssertionError:
                    if attempt == 19:
                        raise
                    time.sleep(1)
        return {"stages": observed, "scope": "real canonical gate only",
                "deployment_refusal_during_each_fault_requires_upgrade_replay": True}

    def snapshot(self):
        with sqlite3.connect("file:" + self.settings["db_path"] + "?mode=ro", uri=True) as conn:
            version = conn.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()
            require(version is not None, "schema version unavailable")
            schema = conn.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall()
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            require(integrity == "ok", "snapshot database integrity failed")
        services = {name: unit_properties(name, ["ActiveState", "SubState", "MainPID", "NRestarts"])
                    for name in ("ega-update-api", "ega-update-worker")}
        readiness = self.module("deployment_readiness", "assess", "--config-file", self.config)
        return {"boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                "current": self.release.name, "db_version": str(version[0]),
                "schema_digest": hashlib.sha256(json.dumps(schema).encode()).hexdigest(),
                "drain": (self.state / "drain").exists(), "services": services,
                "readiness_exit": readiness.returncode, "integrity_check": integrity,
                "manual_recovery_required": (self.state / "drain").exists() or readiness.returncode != 0}

    def atomic_pointer(self):
        env = {**self.env, "EGA_VM_ACCEPTANCE": "1", "EGA_VM_DISPOSABLE": "1", "EGA_OPERATOR_CHECKOUT": str(ROOT)}
        proc = command(["bash", ROOT / "deploy/tests/vm-acceptance-failure-matrix.sh", "pointer"], env=env)
        require(proc.returncode == 0, "pointer switch/CAS/reader/restoration failed")
        return {"exit_code": proc.returncode, "power_loss_durability_proven": False,
                "filesystem_type": filesystem_type(self.release.parent)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["blocked", "guard", "install", "run", "snapshot", "after-reboot"])
    parser.add_argument("--case", choices=REQUIRED)
    parser.add_argument("--before", type=Path)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--commit", default=BASE)
    parser.add_argument("--config", type=Path, default=Path("/etc/ega-update/config.json"))
    parser.add_argument("--designation", type=Path, default=Path("/etc/ega-update/VM_ACCEPTANCE.json"))
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    started = now()
    if args.action == "blocked":
        value = report("vm_contract_acceptance", started, "BLOCKED_REAL_VM_REQUIRED",
                       {"cases": {case: "NOT_EXECUTED" for case in REQUIRED},
                        "production_contacted": False, "production_modified": False},
                       "No explicitly designated disposable VM is available; local host is not an acceptance target")
        write_report(args.report, value)
        return 2
    try:
        guard(args.designation)
        if args.action == "guard":
            value = report("guard", started, "DISPOSABLE_GUARD_PASSED", {})
            value["environment"]["real_vm_designated"] = True
            write_report(args.report, value)
            return 0
        require(args.case is not None, "--case required")
        if args.action == "install":
            require(args.case == "complete_install" and args.archive is not None, "install requires complete_install case and archive")
            require(args.commit == BASE, "initial acceptance install must use accepted base")
            proc = command(["bash", ROOT / "deploy/scripts/install.sh", "--commit", args.commit,
                            "--release-tarball", args.archive, "--config", args.config], timeout=1200)
            require(proc.returncode == 0, "disposable install failed; stop and investigate")
            args.config = Path("/etc/ega-update/config.json")
        suite = Suite(args.config)
        method = getattr(suite, args.case, None)
        if args.action == "install":
            value = report(args.case, started, "PASS_REAL_OS_CASE", suite.canonical_readiness())
        elif args.action in ("snapshot", "after-reboot"):
            require(args.case.startswith("reboot_"), "snapshot requires a reboot case")
            evidence = suite.snapshot()
            if args.action == "after-reboot":
                require(args.before is not None, "before-reboot report required")
                before_report = json.loads(args.before.read_text())
                require(before_report["test_case"] == args.case and before_report["status"] == "BEFORE_REBOOT", "wrong checkpoint evidence")
                before = before_report["evidence"]
                require(before["boot_id"] != evidence["boot_id"], "no real reboot observed")
                for fact in ("current", "db_version", "schema_digest", "drain"):
                    require(before[fact] == evidence[fact], "reboot changed an unfinished deployment fact")
                evidence["before"] = before
            value = report(args.case, started, "BEFORE_REBOOT" if args.action == "snapshot" else "REBOOT_OBSERVED_REQUIRES_REVIEW", evidence)
        elif method is None:
            value = report(args.case, started, "BLOCKED_REAL_VM_REQUIRED", {}, "Requires operator checkpoint workflow in VM-CONTRACT-ACCEPTANCE.md")
        else:
            evidence = method()
            value = report(args.case, started, "PASS_REAL_OS_CASE", evidence)
        value["environment"]["real_vm_designated"] = True
        require(len(suite.release.name) == 40 and all(c in "0123456789abcdef" for c in suite.release.name), "installed release SHA missing")
        value["commit_sha"] = suite.release.name
        value["evidence"]["installed_release"] = suite.release.name
        value["evidence"]["harness_commit"] = command(["git", "rev-parse", "HEAD"]).stdout.strip()
    except Exception as error:
        # Do not persist exception text, command output, config values, or tokens.
        value = report(args.case or "guard", started, "STOPPED_REQUIRES_INVESTIGATION", {}, str(error) if isinstance(error, AssertionError) else type(error).__name__)
    write_report(args.report, value)
    return 0 if value["status"] == "PASS_REAL_OS_CASE" else 2


if __name__ == "__main__":
    raise SystemExit(main())

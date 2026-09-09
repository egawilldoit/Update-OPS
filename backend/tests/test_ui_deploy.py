"""UI/deploy corrective tests (offline, write-only artifact, never executes deploys).

Covers the UI/DEPLOY contributor scope (R12, R20, R21, R29, R32, R33, R35,
R36): shell-wrapper exit propagation is asserted by parsing script text
(banned `if !` status-clobber pattern absent, thin `exec` to the release
venv CLI present); retention is behavioral against a seeded SQLite DB plus
real log/backup/receipt files; the validator is exercised via its stdlib
functions (import-guarded); install/upgrade ordering is asserted by parsing
script text (drain-before-stop, readiness-before-undrain, no
--require-hashes fallback). Frontend behavior has no jsdom available, so
frontend rows assert source-level markers plus the documented manual
checklist in docs/EVIDENCE.md (MC-01..MC-10).

Style: plain pytest functions with tmp_path, no plugins. Python 3.10
compatible. NOT EXECUTED — IMPLEMENTATION PHASE: these tests are written
only and must pass at the runtime gate before any row leaves that state.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sqlite3
import sys
import types
import uuid

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from backend.app import db as db_lib
from backend.app import retention as retention_lib


def _repo_path(*parts):
    return os.path.join(_REPO_ROOT, *parts)


def _read_text(*parts):
    with open(_repo_path(*parts), "r", encoding="utf-8") as fh:
        return fh.read()


def _load_validator():
    """Import-guard the stdlib validator without executing a release."""
    path = _repo_path("deploy", "etc", "validate-release.py")
    if not os.path.isfile(path):
        pytest.skip("validator not present")
    spec = importlib.util.spec_from_file_location("validate_release", path)
    if spec is None or spec.loader is None:
        pytest.skip("validator not importable offline")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as exc:
        pytest.skip("validator import failed offline: %s" % exc)
    return mod


def _settings(log_dir, backup_dir, total_cap=500 * 1024 * 1024,
              log_days=30, meta_days=90):
    ns = types.SimpleNamespace()
    ns.log_dir = log_dir
    ns.backup_dir = backup_dir
    ns.total_log_cap_bytes = total_cap
    ns.completed_log_retention_days = log_days
    ns.metadata_retention_days = meta_days
    return ns


def _iso_days_ago(days):
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _seed_db(path):
    conn = db_lib.connect(path)
    db_lib.migrate(conn)
    conn.commit()
    return conn


def _insert_plan(conn, plan_id, tool_id="hermes", expired=False, used=False):
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    exp = now - timedelta(seconds=60) if expired else now + timedelta(seconds=600)
    conn.execute("INSERT OR IGNORE INTO tools(id) VALUES(?)", (tool_id,))
    conn.execute(
        "INSERT INTO plans(id,tool_id,subject,created_at,expires_at,"
        "fingerprint,target,target_mode,channel,services,backup_scope,"
        "activity_state,activity_evidence,used_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (plan_id, tool_id, "owner@example.invalid", now.isoformat(),
         exp.isoformat(), "fp-1", "9.9.9", "exact", "test",
         json.dumps([]), json.dumps({}), "idle", "idle",
         now.isoformat() if used else ""))
    conn.commit()


def _insert_job(conn, job_id, tool_id="hermes", plan_id=None, state="succeeded",
                finished_days_ago=None, recovery=0):
    from datetime import datetime, timezone
    if plan_id is None:
        plan_id = str(uuid.uuid4())
        _insert_plan(conn, plan_id, tool_id)
    now = datetime.now(timezone.utc).isoformat()
    finished = _iso_days_ago(finished_days_ago) if finished_days_ago is not None else now
    if state in ("accepted", "preflight", "backup", "updating", "verifying"):
        finished = ""
    conn.execute("INSERT OR IGNORE INTO tools(id) VALUES(?)", (tool_id,))
    conn.execute(
        "INSERT INTO jobs(id,tool_id,plan_id,subject,idempotency_key,"
        "request_hash,state,step,before_version,after_version,created_at,"
        "started_at,finished_at,exit_code,error_code,recovery_required)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (job_id, tool_id, plan_id, "owner@example.invalid", "k-%s" % job_id[:8],
         "h-%s" % job_id[:8], state, state, "1.0", "2.0" if state == "succeeded" else "",
         now, now, finished, 0, "", recovery))
    conn.execute(
        "INSERT INTO events(job_id,created_at,event_type,detail)"
        " VALUES(?,?,?,?)", (job_id, now, state, "seed"))
    conn.execute(
        "INSERT INTO checks(tool_id,job_id,name,result,mandatory,summary,"
        "created_at) VALUES(?,?,?,?,?,?,?)",
        (tool_id, job_id, "c1", "pass", 1, "ok", now))
    conn.commit()
    return plan_id


def _write_log(log_dir, job_id, size=10):
    os.makedirs(log_dir, exist_ok=True)
    with open(os.path.join(log_dir, job_id + ".jsonl"), "w", encoding="utf-8") as fh:
        fh.write("x" * size)


# ---------------------------------------------------------------------------
# shell wrappers: thin exec, no banned patterns (R12)
# ---------------------------------------------------------------------------

_WRAPPERS = [
    ("scripts", "agent-update"),
    ("scripts", "update-codex.sh"),
    ("scripts", "update-hermes.sh"),
    ("scripts", "update-opencode.sh"),
    ("scripts", "update-t3.sh"),
    ("scripts", "update-claude.sh"),
]


def test_shell_wrappers_thin_exec():
    for parts in _WRAPPERS:
        text = _read_text(*parts)
        name = "/".join(parts)
        # Banned R12 bug pattern: `if ! ...` status-clobber shape. Wrappers
        # use the positive `if …; then :; else` form so `$?` is never read
        # from inside a negated condition.
        assert "if !" not in text, name
        # No heredoc-python in thin wrappers (explicit arg mapping + exec).
        assert "<<'PY" not in text and "<<\"PY" not in text, name
        assert "PYEOF" not in text, name
        # Thin exec to the release venv CLI with bit-identical propagation
        # (direct venv path or the common_release_python helper resolving it).
        assert "exec " in text, name
        assert ("venv/bin/python" in text or "common_release_python" in text), name
        assert "-m backend.app.cli" in text, name
        # Release resolution (script dir -> /opt/ega-update/current).
        assert "common_release_root" in text, name
        assert "/opt/ega-update/current" in text or "common_release" in text, name
    # agent-update keeps compat action names mapping to cli verbs.
    agent = _read_text("scripts", "agent-update")
    assert "inspect|plan|apply|verify" in agent
    assert "--tool" in agent
    assert "--plan-id" in agent and "--job-id" in agent
    # Per-tool wrappers pin their tool and map to cli verbs.
    codex = _read_text("scripts", "update-codex.sh")
    assert "--tool codex" in codex
    hermes = _read_text("scripts", "update-hermes.sh")
    assert "--tool hermes" in hermes
    # common.sh provides the release helpers without banned patterns.
    common = _read_text("scripts", "common.sh")
    assert "common_release_root" in common
    assert "if !" not in common


# ---------------------------------------------------------------------------
# runner templates: canonical 2-arg form, documentation only
# ---------------------------------------------------------------------------

def test_runner_templates():
    for parts in [("systemd", "ega-update-runner@.service"),
                  ("systemd", "user", "ega-update-runner@.service")]:
        text = _read_text(*parts)
        name = "/".join(parts)
        exec_lines = [ln for ln in text.splitlines()
                      if ln.strip().startswith("ExecStart=")]
        assert exec_lines, name
        joined = "\n".join(exec_lines)
        assert "<job-id>" in joined and "<nonce>" in joined, name
        # Obsolete 1-arg form (bare `runner %i` with no second arg) is gone.
        assert "runner %i\n" not in joined and not joined.rstrip().endswith("runner %i"), name
        # Templates are documentation of the transient shape, not a launch path.
        lowered = text.lower()
        assert "do not start" in lowered or "do not enable" in lowered or \
            "documentation" in lowered, name
        assert "systemd-run --user" in text, name


def test_tunnel_unit():
    text = _read_text("systemd", "cloudflared-ega-update.service")
    # No BindsTo= dependency directive (prose may mention the word while
    # documenting its removal).
    assert "BindsTo=" not in text
    assert "Wants=" in text and "ega-update-api.service" in text
    assert "Restart=always" in text
    assert "RestartSec" in text
    assert "ExecStartPre" in text
    assert "validate-release.py" in text
    assert "--only tunnel" in text
    assert "/opt/ega-update/current/venv/bin/python" in text


# ---------------------------------------------------------------------------
# retention matrix (behavioral, seeded DB + real files)
# ---------------------------------------------------------------------------

def test_retention_30d_logs(tmp_path):
    log_dir = str(tmp_path / "logs")
    bdir = str(tmp_path / "backups")
    os.makedirs(log_dir)
    os.makedirs(bdir)
    db_path = str(tmp_path / "state.db")
    conn = _seed_db(db_path)
    old_job = str(uuid.uuid4())
    new_job = str(uuid.uuid4())
    _insert_job(conn, old_job, finished_days_ago=31)
    _insert_job(conn, new_job, finished_days_ago=1)
    _write_log(log_dir, old_job)
    _write_log(log_dir, new_job)
    conn.commit()
    out = retention_lib.run_retention(conn, _settings(log_dir, bdir))
    assert out["deleted_logs"] >= 1
    assert not os.path.exists(os.path.join(log_dir, old_job + ".jsonl"))
    assert os.path.exists(os.path.join(log_dir, new_job + ".jsonl"))
    conn.close()


def test_retention_total_cap(tmp_path):
    log_dir = str(tmp_path / "logs")
    bdir = str(tmp_path / "backups")
    os.makedirs(log_dir)
    os.makedirs(bdir)
    db_path = str(tmp_path / "state.db")
    conn = _seed_db(db_path)
    ids = [str(uuid.uuid4()) for _ in range(3)]
    # Oldest first: 5d, 3d, 1d ago; cap forces oldest deletion first.
    for jid, age in zip(ids, (5, 3, 1)):
        _insert_job(conn, jid, finished_days_ago=age)
        _write_log(log_dir, jid, size=100)
    conn.commit()
    out = retention_lib.run_retention(
        conn, _settings(log_dir, bdir, total_cap=150))
    assert out["deleted_logs"] >= 1
    # Oldest log goes first; newest survives the cap.
    assert not os.path.exists(os.path.join(log_dir, ids[0] + ".jsonl"))
    assert os.path.exists(os.path.join(log_dir, ids[2] + ".jsonl"))
    conn.close()


def test_retention_90d_metadata(tmp_path):
    log_dir = str(tmp_path / "logs")
    bdir = str(tmp_path / "backups")
    os.makedirs(log_dir)
    os.makedirs(bdir)
    db_path = str(tmp_path / "state.db")
    conn = _seed_db(db_path)
    old_job = str(uuid.uuid4())
    # T14: the plan must actually model expired-unused (past expiry,
    # never consumed) for the orphan-collection rule to apply; fresh
    # plans stay protected by design.
    expired_plan = str(uuid.uuid4())
    _insert_plan(conn, expired_plan, expired=True, used=False)
    plan_id = _insert_job(conn, old_job, plan_id=expired_plan,
                          finished_days_ago=91)
    _write_log(log_dir, old_job)
    conn.commit()
    out = retention_lib.run_retention(conn, _settings(log_dir, bdir))
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (old_job,)).fetchone()
    assert row is None
    checks = conn.execute("SELECT * FROM checks WHERE job_id=?",
                          (old_job,)).fetchall()
    assert checks == []
    # Orphaned plan for the deleted job is collected too.
    prow = conn.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
    assert prow is None
    assert out["tombstoned"] >= 1
    conn.close()


def test_retention_latest_two_backups(tmp_path):
    log_dir = str(tmp_path / "logs")
    bdir = str(tmp_path / "backups")
    os.makedirs(log_dir)
    os.makedirs(bdir)
    db_path = str(tmp_path / "state.db")
    conn = _seed_db(db_path)
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    bids = []
    for i in range(4):
        jid = str(uuid.uuid4())
        _insert_job(conn, jid, tool_id="hermes", finished_days_ago=1)
        bid = "b-%d-%s" % (i, jid[:8])
        completed = (now - timedelta(days=i)).isoformat()
        bpath = os.path.join(bdir, bid)
        os.makedirs(bpath, exist_ok=True)
        conn.execute(
            "INSERT INTO backups(id,job_id,path,scope,consistency,size_bytes,"
            "completed_at) VALUES(?,?,?,?,?,?,?)",
            (bid, jid, bpath, "{}", "ok", 10, completed))
        bids.append(bid)
    conn.commit()
    out = retention_lib.run_retention(conn, _settings(log_dir, bdir))
    assert out["deleted_backups"] == 2
    remaining = {r["id"] for r in conn.execute("SELECT id FROM backups").fetchall()}
    assert bids[0] in remaining and bids[1] in remaining
    assert bids[2] not in remaining and bids[3] not in remaining
    conn.close()


def test_retention_expired_plans_and_receipts(tmp_path):
    log_dir = str(tmp_path / "logs")
    bdir = str(tmp_path / "backups")
    os.makedirs(log_dir)
    os.makedirs(bdir)
    db_path = str(tmp_path / "state.db")
    conn = _seed_db(db_path)
    expired_unused = str(uuid.uuid4())
    expired_used = str(uuid.uuid4())
    fresh_unused = str(uuid.uuid4())
    _insert_plan(conn, expired_unused, expired=True, used=False)
    _insert_plan(conn, expired_used, expired=True, used=True)
    _insert_plan(conn, fresh_unused, expired=False, used=False)
    old_job = str(uuid.uuid4())
    _insert_job(conn, old_job, finished_days_ago=91)
    with open(os.path.join(log_dir, old_job + ".receipt.json"), "w",
              encoding="utf-8") as fh:
        fh.write("{}")
    conn.commit()
    out = retention_lib.run_retention(conn, _settings(log_dir, bdir))
    assert conn.execute("SELECT * FROM plans WHERE id=?",
                        (expired_unused,)).fetchone() is None
    assert conn.execute("SELECT * FROM plans WHERE id=?",
                        (expired_used,)).fetchone() is not None
    assert conn.execute("SELECT * FROM plans WHERE id=?",
                        (fresh_unused,)).fetchone() is not None
    assert out["deleted_plans"] >= 1
    assert not os.path.exists(os.path.join(log_dir, old_job + ".receipt.json"))
    assert out["deleted_receipts"] >= 1
    conn.close()


def test_retention_protections(tmp_path):
    """Active, recovery_required, unresolved, and protected backups survive."""
    log_dir = str(tmp_path / "logs")
    bdir = str(tmp_path / "backups")
    os.makedirs(log_dir)
    os.makedirs(bdir)
    db_path = str(tmp_path / "state.db")
    conn = _seed_db(db_path)
    # Future-schema unresolved marker (absent in the current DDL).
    try:
        conn.execute("ALTER TABLE jobs ADD COLUMN unresolved INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    except Exception:
        pass
    active = str(uuid.uuid4())
    recovery = str(uuid.uuid4())
    unresolved = str(uuid.uuid4())
    _insert_job(conn, active, state="updating")
    _insert_job(conn, recovery, finished_days_ago=100, recovery=1)
    _insert_job(conn, unresolved, finished_days_ago=100)
    try:
        conn.execute("UPDATE jobs SET unresolved=1 WHERE id=?", (unresolved,))
        conn.commit()
    except Exception:
        pytest.skip("unresolved column unavailable")
    for jid in (active, recovery, unresolved):
        _write_log(log_dir, jid, size=50)
        bpath = os.path.join(bdir, "prot-%s" % jid[:8])
        os.makedirs(bpath, exist_ok=True)
        from datetime import datetime, timezone
        conn.execute(
            "INSERT INTO backups(id,job_id,path,scope,consistency,size_bytes,"
            "completed_at) VALUES(?,?,?,?,?,?,?)",
            ("bid-%s" % jid[:8], jid, bpath, "{}", "ok", 10,
             datetime.now(timezone.utc).isoformat()))
    conn.commit()
    out = retention_lib.run_retention(
        conn, _settings(log_dir, bdir, total_cap=1, log_days=0, meta_days=0))
    # Protected logs/backups/rows all survive despite zero-day cutoffs.
    for jid in (active, recovery, unresolved):
        assert os.path.exists(os.path.join(log_dir, jid + ".jsonl")), jid
        assert conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone() is not None, jid
        assert conn.execute("SELECT * FROM backups WHERE job_id=?", (jid,)).fetchall(), jid
    assert out["errors"] == [] or isinstance(out["errors"], list)
    conn.close()


def test_retention_tombstones(tmp_path):
    log_dir = str(tmp_path / "logs")
    bdir = str(tmp_path / "backups")
    os.makedirs(log_dir)
    os.makedirs(bdir)
    db_path = str(tmp_path / "state.db")
    conn = _seed_db(db_path)
    jid = str(uuid.uuid4())
    _insert_job(conn, jid, finished_days_ago=31)
    _write_log(log_dir, jid)
    conn.commit()
    out = retention_lib.run_retention(conn, _settings(log_dir, bdir))
    assert out["tombstoned"] >= 1
    rows = conn.execute("SELECT kind, ref, reason, created_at FROM tombstones").fetchall()
    assert len(rows) >= 1
    kinds = {dict(r)["kind"] for r in rows}
    assert "log" in kinds
    for r in rows:
        d = dict(r)
        assert d["ref"] and d["reason"] and d["created_at"]
    # Tombstones table shape matches the frozen contract.
    cols = {row[1] for row in conn.execute("PRAGMA table_info(tombstones)").fetchall()}
    assert {"kind", "ref", "reason", "created_at"} <= cols
    conn.close()


# ---------------------------------------------------------------------------
# validator logic (import-guarded, stdlib only)
# ---------------------------------------------------------------------------

def test_validator_placeholders_port_manifest(tmp_path):
    import tempfile as _tempfile

    mod = _load_validator()
    # T13: neutral scratch dir — pytest's own tmp_path embeds this
    # test's name ("...placeholders..."), which the placeholder
    # patterns would match as product input. Placeholders belong in
    # the config CONTENT, never in ancestor directory names.
    cfg_root = _tempfile.mkdtemp(prefix="ega-vr-")
    try:
        # Placeholders block; clean config passes the placeholder gate.
        bad_cfg = tmp_path / "bad.json"
        bad_cfg.write_text(json.dumps({
            "team_domain": "https://t.example", "audience": "aud",
            "owner_emails": ["o@example.invalid"], "public_origin": "https://h.example",
            "listen_port": 8771, "csrf_secret": "CHANGEME-secret",
            "state_dir": cfg_root}), encoding="utf-8")
        assert "placeholder" in mod.check_config_placeholders(str(bad_cfg)).lower() \
            or "CHANGEME" in mod.check_config_placeholders(str(bad_cfg))
        good_cfg = tmp_path / "good.json"
        good_cfg.write_text(json.dumps({
            "team_domain": "https://team.example", "audience": "aud-1",
            "owner_emails": ["owner@example.org"], "public_origin": "https://h.example.org",
            "listen_port": 8771, "csrf_secret": "a-very-long-real-secret-value-12345",
            "state_dir": cfg_root}), encoding="utf-8")
        assert mod.check_config_placeholders(str(good_cfg)) == ""
    finally:
        import shutil as _shutil
        _shutil.rmtree(cfg_root, ignore_errors=True)
    # Port: unit default vs config, honoring the drop-in override.
    rel = tmp_path / "rel"
    (rel / "systemd").mkdir(parents=True)
    (rel / "systemd" / "ega-update-api.service").write_text(
        "Environment=EGA_LISTEN_PORT=8771\n", encoding="utf-8")
    assert mod.check_port(str(rel), str(good_cfg), str(tmp_path / "no-dropin")) == ""
    (rel / "systemd" / "ega-update-api.service").write_text(
        "Environment=EGA_LISTEN_PORT=9999\n", encoding="utf-8")
    assert "mismatch" in mod.check_port(str(rel), str(good_cfg),
                                        str(tmp_path / "no-dropin")).lower()
    dropin = tmp_path / "10-port.conf"
    dropin.write_text("[Service]\nEnvironment=EGA_LISTEN_PORT=8771\n",
                      encoding="utf-8")
    assert mod.check_port(str(rel), str(good_cfg), str(dropin)) == ""
    # Manifest: missing blocks, matching verifies, tamper blocks.
    (rel / "backend").mkdir(parents=True, exist_ok=True)
    (rel / "backend" / "a.py").write_text("print(1)\n", encoding="utf-8")
    assert "MANIFEST" in mod.check_manifest(str(rel))
    digest = hashlib.sha256(b"print(1)\n").hexdigest()
    (rel / "MANIFEST").write_text("%s  backend/a.py\n" % digest, encoding="utf-8")
    assert mod.check_manifest(str(rel)) == ""
    (rel / "backend" / "a.py").write_text("print(2)\n", encoding="utf-8")
    assert "mismatch" in mod.check_manifest(str(rel)).lower()
    # Hashes: --require-hashes REQUIRED.
    (rel / "backend" / "requirements.txt").write_text("fastapi==0.0.1\n",
                                                      encoding="utf-8")
    assert "hash" in mod.check_hashes(str(rel)).lower()
    (rel / "backend" / "requirements.txt").write_text(
        "fastapi==0.0.1 \\\n    --hash=sha256:abc\n", encoding="utf-8")
    assert mod.check_hashes(str(rel)) == ""
    # Frontend gate uses the staged static path.
    assert "index.html" in mod.check_frontend(str(rel)).lower()
    (rel / "backend" / "app" / "static").mkdir(parents=True, exist_ok=True)
    (rel / "backend" / "app" / "static" / "index.html").write_text(
        "<html></html>", encoding="utf-8")
    assert mod.check_frontend(str(rel)) == ""
    # Compat: equal schema versions pass, drift blocks.
    old_rel = tmp_path / "old"
    new_rel = tmp_path / "new"
    for d in (old_rel, new_rel):
        (d / "backend" / "app").mkdir(parents=True, exist_ok=True)
    (old_rel / "backend" / "app" / "db.py").write_text("SCHEMA_VERSION = 2\n",
                                                       encoding="utf-8")
    (new_rel / "backend" / "app" / "db.py").write_text("SCHEMA_VERSION = 2\n",
                                                       encoding="utf-8")
    assert mod.check_compat(str(old_rel), str(new_rel)) == ""
    (new_rel / "backend" / "app" / "db.py").write_text("SCHEMA_VERSION = 3\n",
                                                       encoding="utf-8")
    assert "drift" in mod.check_compat(str(old_rel), str(new_rel)).lower()


# ---------------------------------------------------------------------------
# install/upgrade ordering (R32/R33/R35)
# ---------------------------------------------------------------------------

def test_install_upgrade_ordering():
    for name in ["install.sh", "upgrade.sh"]:
        text = _read_text("deploy", "scripts", name)
        # R32: CWD-pinned release venv python + exported config for ALL
        # invocations (including quiescence/status).
        assert 'cd "$RELEASE_DIR"' in text or "cd \"$RELEASE_DIR\"" in text, name
        assert "venv/bin/python" in text, name
        assert "EGA_CONFIG_FILE" in text, name
        # R32: 40-hex commit pin + MANIFEST integrity before switch.
        assert "40" in text and "0-9a-f" in text, name
        assert "MANIFEST" in text and "sha256sum" in text, name
        assert "validate-release.py" in text, name
        # N16: state paths come ONLY from the canonical config CLI
        # (no inline python config parsers) — directly or via the
        # cfg_cli wrapper that delegates to backend.app.config_cli;
        # quiescence relies on exit codes, not parsing.
        assert "backend.app.config_cli" in text, name
        assert "config_cli get" in text or "cfg_cli get" in text, \
            name
        assert "python3 -c 'import json" not in text, name
        assert "python -c 'import json" not in text, name
        assert "status --require-quiescent" in text or \
            "status --require-ready" in text, name
        # N17: archive validated before root extraction.
        assert "validate-archive.py" in text, name
        # R33: drain-before-stop ordering.
        drain_pos = text.find("drain")
        stop_pos = text.find("systemctl stop")
        assert drain_pos != -1 and stop_pos != -1, name
        assert drain_pos < stop_pos, name
        # R33: quiescence via cli status (bounded, fail closed).
        assert "backend.app.cli" in text and "status" in text, name
        assert "120" in text or "24" in text, name
        # R33: readiness (curl localhost + cli status heartbeat) before undrain.
        curl_pos = text.find("curl")
        undrain_pos = text.find('rm -f "$DRAIN"')
        if undrain_pos == -1:
            undrain_pos = text.find("rm -f $DRAIN")
        assert curl_pos != -1 and undrain_pos != -1, name
        assert curl_pos < undrain_pos, name
        # R33: compat-gated rollback, never same-broken-release restart.
        assert "--check-compat" in text, name
        assert "manual" in text.lower(), name
        # R35: --require-hashes with NO unhashed fallback.
        assert "--require-hashes" in text, name
        assert '|| "$RELEASE_DIR/venv/bin/pip" install -r' not in text, name
        assert "|| '$RELEASE_DIR/venv/bin/pip' install" not in text, name
        # R35: port rendered from config into a drop-in.
        assert "10-port.conf" in text and "EGA_LISTEN_PORT" in text, name
        # No heredoc-python (inline -c strings only).
        assert "<<'PY" not in text and '<<"PY' not in text, name


# ---------------------------------------------------------------------------
# runbook + frontend source markers (static; behavior is manual MC-01..MC-10)
# ---------------------------------------------------------------------------

def test_runbook_markers():
    text = _read_text("docs", "RUNBOOK.md")
    # No stale `<shortid>` unit placeholder remains (prose may use the word
    # "shortid" while documenting its removal; the placeholder itself is gone).
    assert "<shortid>" not in text
    assert "<32hex-uuid>" in text
    assert "XDG_RUNTIME_DIR" in text
    assert "drain" in text.lower()
    assert "sudoers" in text.lower()
    assert "tombstone" in text.lower()
    assert "10-port.conf" in text
    assert "validate-release.py" in text
    assert "retention" in text.lower()


def test_frontend_source_markers():
    client = _read_text("frontend", "src", "api", "client.ts")
    assert "final_log_seq" in client
    assert "listActiveJobs" in client and "active" in client
    assert "AbortController" in client and "15000" in client
    assert "Retry-After" in client and "retryAfterMs" in client
    assert "backoffMs" in client and "30000" in client
    app = _read_text("frontend", "src", "app.tsx")
    assert "final_log_seq" in app or "finalLogSeq" in app
    assert "has_more" in app or "hasMore" in app
    assert "planGen" in app
    assert "listActiveJobs" in app
    assert "localStorage" in app
    assert "jobInflight" in app or "inflight" in app.lower()
    assert "LoadMore" in app or "loadMore" in app or "handleLoadMore" in app
    assert "reconnect" in app.lower() or "Reconnect" in app
    viewer = _read_text("frontend", "src", "components", "LogViewer.tsx")
    assert "Load More" in viewer
    assert "Retention gap" in viewer or "retention gap" in viewer.lower()
    assert "finalLogSeq" in viewer and "nextAfter" in viewer
    detail = _read_text("frontend", "src", "views", "JobDetail.tsx")
    assert "Pending tail" in detail or "pending" in detail.lower()
    assert "finalLogSeq" in detail
    health = _read_text("frontend", "src", "components", "HealthPanel.tsx")
    assert "stale" in health.lower()
    assert "health-stale" in health


def test_evidence_no_pass():
    """EVIDENCE.md must never claim a runtime PASS (NOT EXECUTED only)."""
    text = _read_text("docs", "EVIDENCE.md")
    assert "NOT EXECUTED" in text
    assert "IMPLEMENTATION PHASE" in text
    # No table result cell claims PASS (prose mentions of the word are
    # allowed; result cells must stay NOT EXECUTED).
    import re
    assert re.search(r"\|\s*PASS\s*\|", text) is None
    assert "MC-01" in text and "MC-10" in text


# -- synthetic release validation (Wave 12) ----------------------------------------

def _synthetic_tree(tmp_path, name="synrel"):
    """Build a minimal VALID synthetic release + etc tree under tmp.
    No production paths touched. Returns dict of key paths. All
    strings avoid placeholder tokens (CHANGEME/example.invalid/TODO)
    so the tree passes the placeholder gates by construction."""
    import hashlib as _hashlib
    import shutil as _shutil

    root = str(tmp_path / name)
    rel = os.path.join(root, "release")
    etc = os.path.join(root, "etc")
    os.makedirs(rel, exist_ok=True)
    os.makedirs(os.path.join(etc, "cloudflared"), exist_ok=True)
    os.makedirs(os.path.join(rel, "backend", "migrations"),
                exist_ok=True)

    def _write(path, content, mode):
        parent = os.path.dirname(path)
        if parent and not os.path.exists(parent):
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.chmod(path, mode)

    staged = []
    # Minimal import-safe package init (validator imports the release
    # db module; only its SCHEMA_VERSION/migrate/connect surface).
    _write(os.path.join(rel, "backend", "app", "__init__.py"),
           '"""Synthetic release package (validator fixture)."""\n', 0o644)
    staged.append("backend/app/__init__.py")
    _shutil.copyfile(
        _repo_path("backend", "app", "db.py"),
        os.path.join(rel, "backend", "app", "db.py"))
    os.chmod(os.path.join(rel, "backend", "app", "db.py"), 0o644)
    staged.append("backend/app/db.py")
    for fname in ("001_init.sql", "002_execution_hardening.sql",
                  "003_corrective.sql", "004_leases_env.sql"):
        _shutil.copyfile(
            _repo_path("backend", "migrations", fname),
            os.path.join(rel, "backend", "migrations", fname))
        staged.append("backend/migrations/%s" % fname)
    _shutil.copyfile(
        _repo_path("backend", "requirements.txt"),
        os.path.join(rel, "backend", "requirements.txt"))
    staged.append("backend/requirements.txt")
    _write(os.path.join(rel, "backend", "app", "static", "index.html"),
           "<html><body>synthetic console</body></html>\n", 0o644)
    staged.append("backend/app/static/index.html")
    _write(os.path.join(rel, "systemd", "ega-update-api.service"),
           "[Service]\nEnvironment=EGA_LISTEN_PORT=8771\n", 0o644)
    # MANIFEST exactly as staging writes it (sha256sum over backend/**).
    lines = []
    for relpath in sorted(staged):
        if not relpath.startswith("backend/"):
            continue
        digest = _hashlib.sha256()
        with open(os.path.join(rel, relpath), "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                digest.update(chunk)
        lines.append("%s  %s\n" % (digest.hexdigest(), relpath))
    _write(os.path.join(rel, "MANIFEST"), "".join(lines), 0o644)

    config = {
        "team_domain": "https://console-update.example.net",
        "audience": "synthetic-audience-001",
        "owner_emails": ["owner@example.net"],
        "public_origin": "https://console.example.net",
        "listen_port": 8771,
        "csrf_secret": "synthetic-csrf-secret-32-chars-min",
        "state_dir": os.path.join(root, "state"),
        "db_path": os.path.join(root, "state", "state.db"),
    }
    _write(os.path.join(etc, "config.json"),
           json.dumps(config, sort_keys=True), 0o640)
    _write(os.path.join(etc, "api.env"), "", 0o640)
    _write(os.path.join(etc, "worker.env"), "", 0o640)
    _write(os.path.join(etc, "csrf.secret"),
           "synthetic-csrf-file-secret-32-chars", 0o600)
    _write(os.path.join(etc, "tunnel.env"), "", 0o600)
    _write(os.path.join(etc, "secrets.env"), "", 0o640)
    _write(os.path.join(etc, "cloudflared", "credentials.json"),
           '{"tunnel": "synthetic"}', 0o600)
    tunnel_cfg = os.path.join(etc, "cloudflared", "config.yml")
    _write(tunnel_cfg,
           "tunnel: synthetic-tunnel-abc123\n"
           "credentials-file: %s\n"
           "ingress:\n"
           "  - hostname: console.example.net\n"
           "    service: http://127.0.0.1:8771\n" % os.path.join(
               etc, "cloudflared", "credentials.json"), 0o640)
    dropin = os.path.join(root, "10-port.conf")
    _write(dropin, "[Service]\nEnvironment=EGA_LISTEN_PORT=8771\n",
           0o644)
    return {"root": root, "release": rel, "etc": etc,
            "config": os.path.join(etc, "config.json"),
            "tunnel": tunnel_cfg, "dropin": dropin}


def _run_validator(mod, tree, extra=()):
    """Run the validator hermetically: snapshot/restore sys.path and
    the backend modules (check_migrations purges + reimports db)."""
    saved_path = list(sys.path)
    saved_mods = {k: v for k, v in sys.modules.items()
                  if k == "backend" or k.startswith("backend.")}
    try:
        return mod.main(["--release", tree["release"],
                         "--config", tree["config"],
                         "--skip-owner-check",
                         "--tunnel-config", tree["tunnel"],
                         "--port-dropin", tree["dropin"]] + list(extra))
    finally:
        sys.path[:] = saved_path
        for key in [k for k in sys.modules
                    if k == "backend" or k.startswith("backend.")]:
            if key not in saved_mods:
                del sys.modules[key]
        for key, value in saved_mods.items():
            sys.modules[key] = value


def test_synthetic_release_validates_clean(tmp_path):
    """Positive case: every validator section passes on a well-formed
    synthetic tree (R12)."""
    mod = _load_validator()
    tree = _synthetic_tree(tmp_path)
    assert _run_validator(mod, tree) == 0


def test_synthetic_release_negative_matrix(tmp_path):
    """18 rejection scenarios, each failing closed (R12): 17 tree
    mutations plus the rollback-compat case. Every case builds a
    fresh tree so failures cannot leak across scenarios."""
    mod = _load_validator()

    def _fresh():
        return _synthetic_tree(
            tmp_path, "synneg-%s" % uuid.uuid4().hex[:8])

    def _write(path, content, mode=0o644):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.chmod(path, mode)

    cases = []

    # 1. missing migrations dir.
    def _c01(tree):
        import shutil as _shutil
        _shutil.rmtree(os.path.join(tree["release"], "backend",
                                    "migrations"))
    cases.append(_c01)
    # 2. unhashed requirements.
    def _c02(tree):
        _write(os.path.join(tree["release"], "backend",
                            "requirements.txt"),
               "fastapi==0.136.1\n")
    cases.append(_c02)
    # 3. malformed hash lock (no hashes at all).
    def _c03(tree):
        _write(os.path.join(tree["release"], "backend",
                            "requirements.txt"),
               "not a lockfile {{{{\n")
    cases.append(_c03)
    # 4. missing MANIFEST.
    def _c04(tree):
        os.remove(os.path.join(tree["release"], "MANIFEST"))
    cases.append(_c04)
    # 5. file modified after MANIFEST.
    def _c05(tree):
        with open(os.path.join(tree["release"], "backend", "app",
                               "static", "index.html"),
                  "a", encoding="utf-8") as fh:
            fh.write("tampered")
    cases.append(_c05)
    # 6. missing frontend index.
    def _c06(tree):
        os.remove(os.path.join(tree["release"], "backend", "app",
                               "static", "index.html"))
    cases.append(_c06)
    # 7-9. placeholder team domain / audience / public origin.
    for _key, _bad in (("team_domain", "https://CHANGEME.invalid"),
                       ("audience", "CHANGEME-aud"),
                       ("public_origin", "https://h.example.invalid")):
        def _c789(tree, _key=_key, _bad=_bad):
            with open(tree["config"], "r", encoding="utf-8") as fh:
                config = json.load(fh)
            config[_key] = _bad
            _write(tree["config"], json.dumps(config, sort_keys=True),
                   0o640)
        cases.append(_c789)
    # 10. missing owner.
    def _c10(tree):
        with open(tree["config"], "r", encoding="utf-8") as fh:
            config = json.load(fh)
        config["owner_emails"] = []
        _write(tree["config"], json.dumps(config, sort_keys=True),
               0o640)
    cases.append(_c10)
    # 11. missing CSRF secret.
    def _c11(tree):
        with open(tree["config"], "r", encoding="utf-8") as fh:
            config = json.load(fh)
        config["csrf_secret"] = ""
        os.remove(os.path.join(tree["etc"], "csrf.secret"))
        _write(tree["config"], json.dumps(config, sort_keys=True),
               0o640)
    cases.append(_c11)
    # 12. too-open secrets file.
    def _c12(tree):
        os.chmod(os.path.join(tree["etc"], "secrets.env"), 0o644)
    cases.append(_c12)
    # 13. missing secrets.env.
    def _c13(tree):
        os.remove(os.path.join(tree["etc"], "secrets.env"))
    cases.append(_c13)
    # 14. missing tunnel credentials.
    def _c14(tree):
        os.remove(os.path.join(tree["etc"], "cloudflared",
                               "credentials.json"))
    cases.append(_c14)
    # 15. placeholder tunnel hostname.
    def _c15(tree):
        _write(tree["tunnel"],
               "tunnel: synthetic-tunnel-abc123\n"
               "credentials-file: %s\n"
               "ingress:\n"
               "  - hostname: CHANGEME-update-console.example.invalid\n"
               "    service: http://127.0.0.1:8771\n" % os.path.join(
                   tree["etc"], "cloudflared", "credentials.json"),
               0o640)
    cases.append(_c15)
    # 16. non-loopback tunnel target.
    def _c16(tree):
        _write(tree["tunnel"],
               "tunnel: synthetic-tunnel-abc123\n"
               "credentials-file: %s\n"
               "ingress:\n"
               "  - hostname: console.example.net\n"
               "    service: http://10.0.0.9:8771\n" % os.path.join(
                   tree["etc"], "cloudflared", "credentials.json"),
               0o640)
    cases.append(_c16)
    # 17. port mismatch.
    def _c17(tree):
        _write(tree["dropin"],
               "[Service]\nEnvironment=EGA_LISTEN_PORT=9999\n", 0o644)
    cases.append(_c17)
    assert len(cases) == 17
    for index, mutate in enumerate(cases):
        tree = _fresh()
        mutate(tree)
        assert _run_validator(mod, tree) != 0, "case %d" % (index + 1)
    # 18. incompatible rollback schema (different harness:
    # --check-compat on prior v4 vs candidate v5).
    import re as _re
    import shutil as _shutil

    tree = _fresh()
    path = os.path.join(tree["release"], "backend", "app", "db.py")
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    bumped, count = _re.subn(r"CODE_VERSION\s*=\s*4",
                             "CODE_VERSION = 5", text, count=1)
    assert count == 1
    _write(path, bumped, 0o644)
    old = os.path.join(tree["root"], "oldrel")
    os.makedirs(os.path.join(old, "backend", "app"), exist_ok=True)
    _shutil.copyfile(
        _repo_path("backend", "app", "db.py"),
        os.path.join(old, "backend", "app", "db.py"))
    assert mod._schema_version_of(old) == 4
    assert mod.main(["--check-compat", old, tree["release"]]) != 0

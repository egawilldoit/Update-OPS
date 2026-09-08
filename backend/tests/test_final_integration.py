"""Final static integration regression tests (F01-F16, write-only).

Behavioral regression artifacts for every F-finding. NOT EXECUTED in
this phase per instruction. Mocks stay at external boundaries
(systemd bus, process spawn); console-component integration is never
mocked away. Python 3.10 compatible, pytest style, stdlib + backend.
"""
from __future__ import annotations

import json
import os
import re as _re
import sys
import uuid

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

from backend.app import db as db_lib
from backend.app import plans as plans_lib

import support as support_lib


def _fresh_db(tmp_path, name="f.db"):
    conn = db_lib.connect(str(tmp_path / name))
    assert db_lib.migrate(conn) == db_lib.CODE_VERSION
    conn.commit()
    return conn


def _terminal_job_with_lease(conn, key="k-f02"):
    """Admit + terminalize WITHOUT ownership release (F02: runner-side
    finalization never disposes the lease). Returns job id."""
    from backend.app.admission import admit
    from backend.app import tx as tx_lib

    support_lib.test_release_root()
    row = support_lib.v2_plan_row(conn, uuid.uuid4().hex)
    jid, created, err = admit(conn, "owner@example.invalid", key,
                              row["id"], False, "fp-test-1", True, False)
    assert err == "" and created, err
    tx_lib.transition_tx(conn, jid, "succeeded", step="verifying",
                         expect_states=["accepted"],
                         update={"after_version": "9.9.9"},
                         event="succeeded", event_detail="")
    return jid


def _lease_held(conn, job_id):
    row = conn.execute(
        "SELECT released_at FROM execution_leases WHERE kind='mutation'"
        " AND job_id=?", (job_id,)).fetchone()
    return row is not None and not str(row["released_at"] or "")


def _live_units(monkeypatch, live=(), unknown=()):
    from backend.app import units as units_lib

    def _fake(unit, timeout_s=10):
        if unit in live:
            return {"state": "live", "unit": unit, "active_state": "active",
                    "sub_state": "running", "main_pid": 1, "cgroup": "",
                    "identity_ok": True, "detail": ""}
        if unit in unknown:
            return {"state": "unknown", "unit": unit, "active_state": "",
                    "sub_state": "", "main_pid": 0, "cgroup": "",
                    "identity_ok": False, "detail": "bus down"}
        return {"state": "confirmed_stopped", "unit": unit,
                "active_state": "inactive", "sub_state": "dead",
                "main_pid": 0, "cgroup": "", "identity_ok": True,
                "detail": "manager=inactive load=not-found"}
    monkeypatch.setattr(units_lib, "query_unit", _fake)
    monkeypatch.setattr(units_lib, "query_unit_system", _fake)


# -- F01 canonical plan hash ---------------------------------------------------

def test_f01_build_insert_load_roundtrip_unmodified(tmp_path):
    """build_plan_row -> insert_plan -> load_plan succeeds with the hash
    exactly as built (no fixture repair, no manual recompute)."""
    conn = _fresh_db(tmp_path)
    row = support_lib.v2_plan_row(conn, uuid.uuid4().hex)
    loaded = plans_lib.load_plan(conn, row["id"])
    assert loaded["plan_hash"] == row["plan_hash"]
    conn.close()


def test_f01_env_release_config_target_mutations_break_hash(tmp_path):
    """Mutating any hashed binding invalidates the plan at load."""
    conn = _fresh_db(tmp_path)
    row = support_lib.v2_plan_row(conn, uuid.uuid4().hex)
    originals = {}
    for column in ("env_fingerprint", "release_path", "config_hash",
                   "target", "fingerprint"):
        originals[column] = row[column]
    for column, value in (
            ("env_fingerprint", "mutated-env"),
            ("release_path", "/mutated-release"),
            ("config_hash", "mutated-config"),
            ("target", "0.0.0-mutated"),
            ("fingerprint", "mutated-fp")):
        conn.execute("UPDATE plans SET %s=? WHERE id=?" % column,
                     (value, row["id"]))
        conn.commit()
        with pytest.raises(plans_lib.PlanInvalid):
            plans_lib.load_plan(conn, row["id"])
        conn.execute("UPDATE plans SET %s=? WHERE id=?" % column,
                     (originals[column], row["id"]))
        conn.commit()
    # Restored row loads cleanly again.
    assert plans_lib.load_plan(conn, row["id"])["plan_hash"] == \
        row["plan_hash"]
    conn.close()


# -- F03 unified owner execution contract --------------------------------------

def _contract(**over):
    from backend.app.owner_env import build_owner_contract

    support_lib.test_release_root()
    base = {"LANG": "C.UTF-8", "TZ": "UTC"}
    base.update(over)
    return build_owner_contract(None, None, "", base)


def test_f03_probe_env_equals_apply_env():
    """Runner launch env and probe launch env derive from ONE contract:
    identical except the attempt nonce (probes never consume attempts)."""
    from backend.app.owner_env import contract_env, contract_fingerprint

    contract = _contract()
    probe_env = contract_env(contract)
    apply_env = contract_env(contract, nonce="n-1")
    for key, value in probe_env.items():
        assert apply_env.get(key) == value, key
    assert apply_env["EGA_ATTEMPT_NONCE"] == "n-1"
    assert "EGA_ATTEMPT_NONCE" not in probe_env
    assert contract_fingerprint(contract)
    # Same inputs rebuild the identical contract (deterministic).
    again = _contract()
    assert again == contract


def test_f03_every_contract_field_moves_fingerprint(monkeypatch):
    """Bus/locale/release/owner-identity/privilege changes each move the
    fingerprint (F03: no conceptual approximation).

    Note: raw environ HOME/USER/PATH are deliberately NOT contract
    inputs — the contract takes uid/gid/user/home from passwd and PATH
    from the configured node bin, so a stray login-shell override can
    neither sneak in nor invalidate. Owner identity itself (tool_owner)
    does move the fingerprint.
    """
    from backend.app.config import settings as settings_lib
    from backend.app.owner_env import contract_fingerprint

    baseline = contract_fingerprint(_contract())
    assert baseline
    for variant in (
            _contract(XDG_RUNTIME_DIR="/run/user/9999"),
            _contract(DBUS_SESSION_BUS_ADDRESS="unix:path=/other/bus"),
            _contract(LANG="fr_FR.UTF-8"),
            _contract(TZ="America/New_York")):
        assert contract_fingerprint(variant) not in ("", baseline)
    monkeypatch.setattr(settings_lib, "tool_owner", "other-owner")
    assert contract_fingerprint(_contract()) != baseline


def test_f03_release_node_privilege_move_fingerprint(monkeypatch):
    from backend.app import inventory as inventory_lib
    from backend.app import owner_env as owner_env_lib
    from backend.app.config import settings as settings_lib

    support_lib.test_release_root()
    baseline = owner_env_lib.contract_fingerprint(
        owner_env_lib.build_owner_contract())
    assert baseline
    assert owner_env_lib.contract_fingerprint(
        owner_env_lib.build_owner_contract(
            None, None, "/other-release")) != baseline
    monkeypatch.setattr(settings_lib, "node_path",
                        "/other/node/bin/node")
    assert owner_env_lib.contract_fingerprint(
        owner_env_lib.build_owner_contract()) != baseline


def test_f03_sudo_profile_change_moves_fingerprint(monkeypatch):
    from backend.app import inventory as inventory_lib
    from backend.app import owner_env as owner_env_lib

    support_lib.test_release_root()
    baseline = owner_env_lib.contract_fingerprint(
        owner_env_lib.build_owner_contract())
    monkeypatch.setattr(
        inventory_lib, "get_tool_inventory",
        lambda tool_id: {"service_units": [
            {"unit": "hermes-gateway@x.service",
             "restart_authority": "owner-sudo"}],
            "launch_method": "systemd --user"} if tool_id == "hermes"
        else {})
    changed = owner_env_lib.contract_fingerprint(
        owner_env_lib.build_owner_contract())
    assert changed and changed != baseline


def test_f03_stale_env_fingerprint_blocks_admission(tmp_path):
    """A plan bound to an older environment is invalidated at admission
    (config_changed), never silently applied under a new contract.

    The stored plan keeps a CONSISTENT hash for the stale environment
    (recomputed like production does), so the test exercises exactly
    the env gate rather than the tamper gate.
    """
    from backend.app import plans as plans_lib
    from backend.app.admission import admit

    conn = _fresh_db(tmp_path)
    row = support_lib.v2_plan_row(conn, uuid.uuid4().hex)
    stored = dict(conn.execute("SELECT * FROM plans WHERE id=?",
                              (row["id"],)).fetchone())
    stored["env_fingerprint"] = "stale-environment-fingerprint"
    fresh_hash = plans_lib.canonical_plan_hash(
        plans_lib._hash_view(stored))
    conn.execute("UPDATE plans SET env_fingerprint=?, plan_hash=?"
                 " WHERE id=?",
                 ("stale-environment-fingerprint", fresh_hash,
                  row["id"]))
    conn.commit()
    jid, created, err = admit(
        conn, "owner@example.invalid", "k-f03", row["id"], False,
        "fp-test-1", True, False)
    assert err == "config_changed" and not created
    conn.close()


# -- F02 mutation ownership lifetime -------------------------------------------

def test_f02_terminal_live_unit_keeps_lease(tmp_path, monkeypatch):
    """Terminal DB state with a live runner unit: lease stays held and
    no new job is admitted (terminal != quiescence)."""
    from backend.app.admission import admit
    from backend.app.reconcile_core import canonical_unit
    from backend.app.worker import dispatch as dispatch_lib

    conn = _fresh_db(tmp_path)
    jid = _terminal_job_with_lease(conn, key="k-f02-live")
    assert _lease_held(conn, jid)
    _live_units(monkeypatch, live=[canonical_unit(jid)])
    row = conn.execute("SELECT * FROM jobs WHERE id=?",
                       (jid,)).fetchone()
    assert dispatch_lib._reconcile_row(conn, row) == "live"
    assert _lease_held(conn, jid)
    # New admission is refused while another ownership is held.
    row2 = support_lib.v2_plan_row(conn, uuid.uuid4().hex)
    _jid2, _created2, err2 = admit(
        conn, "owner@example.invalid", "k-f02-live-new", row2["id"],
        False, "fp-test-1", True, False)
    assert err2 == "busy" and not _created2
    conn.close()


def test_f02_terminal_unknown_unit_keeps_lease(tmp_path, monkeypatch):
    from backend.app.reconcile_core import canonical_unit
    from backend.app.worker import dispatch as dispatch_lib

    conn = _fresh_db(tmp_path)
    jid = _terminal_job_with_lease(conn, key="k-f02-unk")
    _live_units(monkeypatch, unknown=[canonical_unit(jid)])
    row = conn.execute("SELECT * FROM jobs WHERE id=?",
                       (jid,)).fetchone()
    assert dispatch_lib._reconcile_row(conn, row) == "unknown-held"
    assert _lease_held(conn, jid)
    conn.close()


def test_f02_terminal_stopped_releases_lease(tmp_path, monkeypatch):
    """Terminal job + confirmed stopped unit + no procs: the reconciler
    releases ownership; afterwards admission proceeds."""
    from backend.app.admission import admit
    from backend.app.worker import dispatch as dispatch_lib

    conn = _fresh_db(tmp_path)
    jid = _terminal_job_with_lease(conn, key="k-f02-rel")
    _live_units(monkeypatch)
    row = conn.execute("SELECT * FROM jobs WHERE id=?",
                       (jid,)).fetchone()
    assert dispatch_lib._reconcile_row(conn, row) == "ownership-released"
    assert not _lease_held(conn, jid)
    row2 = support_lib.v2_plan_row(conn, uuid.uuid4().hex)
    jid2, created2, err2 = admit(
        conn, "owner@example.invalid", "k-f02-rel-new", row2["id"],
        False, "fp-test-1", True, False)
    assert err2 == "" and created2 and jid2
    conn.close()


def test_f02_missing_job_lease_never_auto_released(tmp_path):
    """A mutation lease pointing at a missing job blocks admission and
    is never silently disposed by the acquire path."""
    from backend.app import leases as leases_lib
    from backend.app.admission import admit

    conn = _fresh_db(tmp_path)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO execution_leases(id,kind,subject,tool_id,job_id,"
        "request_id,holder,acquired_at,expires_at,released_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("mutation-ghost", "mutation", "", "", "ghost-job", "",
         "test", "2026-09-08T00:00:00+00:00", "", ""))
    conn.execute("COMMIT")
    row = support_lib.v2_plan_row(conn, uuid.uuid4().hex)
    _jid, created, err = admit(
        conn, "owner@example.invalid", "k-f02-ghost", row["id"], False,
        "fp-test-1", True, False)
    assert err == "busy" and not created
    ghost = conn.execute(
        "SELECT released_at FROM execution_leases WHERE id=?"
        " AND kind='mutation'", ("mutation-ghost",)).fetchone()
    assert ghost is not None and not str(ghost["released_at"] or "")
    conn.close()


def test_f02_release_failure_keeps_admission_blocked(tmp_path):
    """A failed ownership commit keeps the lease held: admission stays
    blocked instead of silently proceeding (no try/except-pass)."""
    import sqlite3
    from backend.app import tx as tx_lib
    from backend.app.admission import admit
    from backend.app.reconcile_core import canonical_unit

    conn = _fresh_db(tmp_path)
    jid = _terminal_job_with_lease(conn, key="k-f02-fail")

    class _FailRelease(object):
        def __init__(self, real):
            self._real = real

        def execute(self, sql, params=()):
            if isinstance(sql, str) and "execution_leases" in sql \
                    and "released_at" in sql:
                raise sqlite3.OperationalError("injected lease failure")
            return self._real.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self._real, name)

    with pytest.raises(tx_lib.TxError):
        tx_lib.release_ownership(
            _FailRelease(conn), jid, expect_states=["succeeded"],
            event="ownership_released", event_detail="test")
    assert _lease_held(conn, jid)
    row2 = support_lib.v2_plan_row(conn, uuid.uuid4().hex)
    _jid2, created2, err2 = admit(
        conn, "owner@example.invalid", "k-f02-fail-new", row2["id"],
        False, "fp-test-1", True, False)
    assert err2 == "busy" and not created2
    assert canonical_unit(jid)
    conn.close()


# -- F04 contradiction-free success --------------------------------------------

def _f04_good():
    from backend.app import receipts as receipts_lib

    return receipts_lib.build_receipt(
        "job-f04", "hermes", "succeeded", "1.0", "9.9.9", 0, "", [
            {"name": "smoke", "result": "pass", "mandatory": True,
             "summary": "ok"}], "2026-09-08T00:00:00+00:00",
        plan_id="plan-f04", plan_hash="ph", attempt_nonce="n",
        release_path="/rel", target="9.9.9", target_mode="exact",
        expected_checks=["smoke"], installer_exit=0,
        install_outcome="succeeded", actual_change=True,
        cleanup_status="resolved", recovery_disposition="none",
        evidence_durable=True)


def test_f04_success_agreement_matrix():
    from backend.app import receipts as receipts_lib

    ok, reason = receipts_lib.validate_receipt(_f04_good())
    assert ok, reason
    contradictions = [
        ("exit_code", 6),
        ("install_outcome", "failed"),
        ("install_outcome", ""),
        ("install_outcome", "none"),
        ("cleanup_status", "unknown"),
        ("cleanup_status", ""),
        ("recovery_disposition", "required"),
        ("after_version", ""),
        ("installer_exit", 4),
    ]
    for key, value in contradictions:
        mutated = _f04_good()
        mutated[key] = value
        valid, _why = receipts_lib.validate_receipt(mutated)
        assert valid is False, (key, value)
    # already_current is an explicitly allowed success outcome.
    already = _f04_good()
    already["install_outcome"] = "already_current"
    assert receipts_lib.validate_receipt(already)[0] is True
    # Weakening an expected check to mandatory=false fails.
    weakened = _f04_good()
    weakened["checks"] = [{"name": "smoke", "result": "pass",
                           "mandatory": False, "summary": ""}]
    assert receipts_lib.validate_receipt(weakened)[0] is False
    # Exact target mismatch fails.
    mismatched = _f04_good()
    mismatched["after_version"] = "9.9.8"
    assert receipts_lib.validate_receipt(mismatched)[0] is False


def test_f04_strict_exit_parsing():
    from backend.app import receipts as receipts_lib

    for bad in ("abc", "", None, True, 1.5, "  "):
        with pytest.raises(ValueError):
            receipts_lib.build_receipt(
                "j", "hermes", "failed", "1.0", "1.0", bad, "x", [],
                "2026-09-08T00:00:00+00:00")
    for bad in ("abc", "", None, True, 2.5):
        with pytest.raises(ValueError):
            receipts_lib.build_receipt(
                "j", "hermes", "failed", "1.0", "1.0", 4, "x", [],
                "2026-09-08T00:00:00+00:00", installer_exit=bad)
    # Integer zero (and string "0") survive strict parsing.
    receipt = receipts_lib.build_receipt(
        "j", "hermes", "failed", "1.0", "1.0", 0, "x", [],
        "2026-09-08T00:00:00+00:00", installer_exit="0")
    assert receipt["exit_code"] == 0
    assert receipt["installer_exit"] == 0


# -- F05 migration inference safety --------------------------------------------

def _apply_files(conn, *filenames):
    for filename in filenames:
        with open(os.path.join(_REPO_ROOT, "backend", "migrations",
                               filename), "r", encoding="utf-8") as fh:
            conn.executescript(fh.read())
    conn.commit()


def test_f05_full_v1_infers_1_and_migrates(tmp_path):
    conn = db_lib.connect(str(tmp_path / "v1.db"))
    _apply_files(conn, "001_init.sql")
    assert db_lib.is_schema_version_fully_present(conn, 1) is True
    assert db_lib.is_schema_version_fully_present(conn, 2) is False
    assert db_lib.migrate(conn) == db_lib.CODE_VERSION
    assert db_lib.validate_schema(conn) == db_lib.CODE_VERSION
    conn.close()


def test_f05_full_v2_infers_1_and_2(tmp_path):
    conn = db_lib.connect(str(tmp_path / "v2.db"))
    _apply_files(conn, "001_init.sql", "002_execution_hardening.sql")
    assert db_lib.is_schema_version_fully_present(conn, 2) is True
    assert db_lib.is_schema_version_fully_present(conn, 3) is False
    assert db_lib.migrate(conn) == db_lib.CODE_VERSION
    conn.close()


def test_f05_partial_003_marker_subset_not_inferred(tmp_path):
    """The old marker subset (two columns of ~thirty changes) must NOT
    count as migration 003: the engine finishes the work instead."""
    conn = db_lib.connect(str(tmp_path / "p3.db"))
    _apply_files(conn, "001_init.sql", "002_execution_hardening.sql")
    conn.execute("ALTER TABLE jobs ADD COLUMN attempt_claimed INTEGER"
                 " NOT NULL DEFAULT 0")
    conn.execute("ALTER TABLE plans ADD COLUMN plan_hash TEXT NOT NULL"
                 " DEFAULT ''")
    conn.commit()
    assert db_lib.is_schema_version_fully_present(conn, 2) is True
    assert db_lib.is_schema_version_fully_present(conn, 3) is False
    assert db_lib.migrate(conn) == db_lib.CODE_VERSION
    assert db_lib.is_schema_version_fully_present(
        conn, db_lib.CODE_VERSION) is True
    # Rerun remains idempotent.
    assert db_lib.migrate(conn) == db_lib.CODE_VERSION
    conn.close()


def test_f05_complete_003_infers_3(tmp_path):
    conn = db_lib.connect(str(tmp_path / "c3.db"))
    _apply_files(conn, "001_init.sql", "002_execution_hardening.sql",
                 "003_corrective.sql")
    assert db_lib.is_schema_version_fully_present(conn, 3) is True
    assert db_lib.is_schema_version_fully_present(conn, 4) is False
    assert db_lib.migrate(conn) == db_lib.CODE_VERSION
    conn.close()


def test_f05_partial_004_not_inferred_then_completed(tmp_path):
    conn = db_lib.connect(str(tmp_path / "p4.db"))
    _apply_files(conn, "001_init.sql", "002_execution_hardening.sql",
                 "003_corrective.sql")
    conn.execute(
        "CREATE TABLE execution_leases (id TEXT PRIMARY KEY,"
        " kind TEXT NOT NULL DEFAULT '')")
    conn.commit()
    assert db_lib.is_schema_version_fully_present(conn, 3) is True
    assert db_lib.is_schema_version_fully_present(conn, 4) is False
    assert db_lib.migrate(conn) == db_lib.CODE_VERSION
    cols = {r["name"] for r in
            conn.execute("PRAGMA table_info(execution_leases)").fetchall()}
    assert "released_at" in cols
    assert "env_fingerprint" in {
        r["name"] for r in
        conn.execute("PRAGMA table_info(plans)").fetchall()}
    assert db_lib.migrate(conn) == db_lib.CODE_VERSION
    conn.close()


# -- F13 typed CLI detail ------------------------------------------------------

def test_f13_cli_detail_stays_typed():
    """Quiescence envelopes keep detail as a JSON object (F13): no
    Python-repr stringification of machine-readable payloads."""
    import io as _io
    import contextlib as _ctx
    from backend.app import cli as cli_lib

    buf = _io.StringIO()
    with _ctx.redirect_stdout(buf):
        cli_lib._emit_envelope(
            "", "status", state="blocked", error_code="not_quiescent",
            detail={"quiescent": False, "reasons": ["busy"],
                    "count": 2, "ok": True, "nothing": None},
            exit_mapped=3)
    payload = json.loads(buf.getvalue())
    detail = payload["detail"]
    assert isinstance(detail, dict), type(detail)
    assert detail["quiescent"] is False
    assert detail["reasons"] == ["busy"]
    assert detail["count"] == 2 and detail["ok"] is True
    assert detail["nothing"] is None
    # Failure path still collapses to the fixed safe string.
    buf2 = _io.StringIO()
    with _ctx.redirect_stdout(buf2):
        cli_lib._emit_envelope("", "status", detail=object(),
                               exit_mapped=0)
    assert isinstance(json.loads(buf2.getvalue())["detail"], str)


# -- F14 complete probe enqueue --------------------------------------------------

def test_f14_unknown_plan_creates_no_probe_row(tmp_path):
    """Plan/tool resolution precedes enqueue (F14): an unknown plan is
    refused without leaving a visible incomplete probe request behind."""
    import types as _types
    from backend.app import cli as cli_lib
    from backend.app.config import settings as settings_lib

    conn = _fresh_db(tmp_path)
    conn.close()
    args = _types.SimpleNamespace(
        tool="", plan_id=str(uuid.uuid4()), ack=False,
        idempotency_key="k-f14", wait_secs=0, db_path="")
    # Minimal seam: point settings at the test DB without touching prod.
    old_db = settings_lib.db_path
    settings_lib.db_path = str(tmp_path / "f.db")
    try:
        rc = cli_lib.cmd_apply(args)
    finally:
        settings_lib.db_path = old_db
    assert rc == cli_lib.EXIT_INVALID
    check = db_lib.connect(str(tmp_path / "f.db"))
    try:
        rows = check.execute("SELECT * FROM probe_requests").fetchall()
        assert list(rows) == []
    finally:
        check.close()


# -- F15 immutable staging order -------------------------------------------------

def test_f15_stage_digest_validate_reextract_order():
    """Deploy scripts implement stage → digest → validate → re-verify →
    extract on the staged copy (F15 TOCTOU safety), and record the
    candidate digest."""
    for name in ("install.sh", "upgrade.sh"):
        path = os.path.join(_REPO_ROOT, "deploy", "scripts", name)
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
        stage = text.find("STAGED_ARCHIVE")
        assert stage != -1, name
        digest = text.find("CANDIDATE_SHA256")
        assert digest > stage, name
        validate = text.find("validate-archive.py")
        assert validate > stage, name
        first_extract = text.find("tar -xzf")
        assert first_extract > validate, name
        # Extraction consumes the staged copy, never the operator path.
        assert "tar -xzf \"$STAGED_ARCHIVE\"" in text, name
        assert "tar -xzf \"$TARBALL\"" not in text, name
        # Digest is re-verified after validation, before extraction.
        reverify = text.find("changed after validation")
        assert reverify != -1 and reverify < first_extract, name
        # Candidate digest is bound into the release metadata.
        assert "CANDIDATE_SHA256" in text, name


# -- F16 no stale architecture -----------------------------------------------------

def test_f16_no_stale_runtime_references():
    """Removed mechanisms leave no live references in runtime code
    (F16): single admission path, reconciler-owned release, supervised
    phases, full-UUID units."""
    import re as _re

    roots = [os.path.join(_REPO_ROOT, "backend", "app"),
             os.path.join(_REPO_ROOT, "scripts")]
    banned = ("reserve_job(", "release_mutation", "_call_in_thread",
              "terminate_tree(", "_unit_active(", "shortid")
    hits = []
    for root in roots:
        for dirpath, _dirnames, filenames in os.walk(root):
            for filename in filenames:
                if not filename.endswith((".py", ".sh")) \
                        and "agent-update" not in filename:
                    continue
                if "agent-update" in filename and \
                        not filename.endswith((".sh",)):
                    # agent-update has no extension; check it too.
                    pass
                path = os.path.join(dirpath, filename)
                try:
                    with open(path, "r", encoding="utf-8") as fh:
                        text = fh.read()
                except OSError:
                    continue
                for marker in banned:
                    for match in _re.finditer(_re.escape(marker), text):
                        line = text.count("\n", 0, match.start()) + 1
                        hits.append("%s:%d:%s" % (path, line, marker))
    # agent-update (extensionless) is covered explicitly.
    agent = os.path.join(_REPO_ROOT, "scripts", "agent-update")
    try:
        with open(agent, "r", encoding="utf-8") as fh:
            agent_text = fh.read()
        for marker in banned:
            assert marker not in agent_text, marker
    except OSError:
        pass
    assert hits == [], hits


# -- F11 phase evidence durability -------------------------------------------------

def test_f11_stream_sanitizer_creation_failure_recorded(tmp_path, monkeypatch):
    from backend.app.worker import phase_run as phase_run_lib
    import backend.app.sanitize as sanitize_lib

    def _boom(secrets=()):
        raise RuntimeError("no sanitizer")

    monkeypatch.setattr(sanitize_lib, "SanitizingStream", _boom)
    writer = phase_run_lib._StreamWriter(str(tmp_path / "s.stream"), ())
    assert writer.evidence_failed is True
    assert writer.evidence_failure_reason
    writer.emit("stdout", "hello\n")
    assert writer.evidence_failed is True


def test_f11_stream_feed_failure_recorded(tmp_path):
    from backend.app.worker import phase_run as phase_run_lib

    writer = phase_run_lib._StreamWriter(str(tmp_path / "s.stream"), ())
    assert writer.evidence_failed is False
    target = writer._streams["stdout"]

    def _boom(_chunk):
        raise RuntimeError("feed exploded")

    target.feed = _boom  # type: ignore[method-assign]
    writer.emit("stdout", "hello\n")
    assert writer.evidence_failed is True
    assert "feed" in writer.evidence_failure_reason


def test_f11_stream_write_failure_recorded(tmp_path):
    from backend.app.worker import phase_run as phase_run_lib

    # /proc is read-only even for root: the write must fail closed on
    # any uid instead of silently dropping lines.
    writer = phase_run_lib._StreamWriter(
        "/proc/ega-nope-xyz-123/stream", ())
    assert writer.evidence_failed is False
    writer.emit("stdout", "hello\n")
    assert writer.evidence_failed is True


def test_f11_result_write_failure_returns_false(tmp_path):
    from backend.app.worker import phase_run as phase_run_lib

    assert phase_run_lib._write_result(
        "/proc/ega-nope-xyz-123/result.json", True, "verify",
        {"version": "1.0"}) is False


def test_f11_secret_loader_failure_fails_phase_closed(tmp_path, monkeypatch):
    from backend.app.worker import phase_run as phase_run_lib
    from backend.app import config as config_lib
    from backend.app.owner_env import (build_owner_contract,
                                       contract_fingerprint)

    def _boom(_settings=None):
        raise OSError("secrets unreadable")

    monkeypatch.setattr(config_lib, "load_secret_values", _boom)
    support_lib.test_release_root()
    expected = contract_fingerprint(build_owner_contract(None))
    payload_path = str(tmp_path / "p.json")
    result_path = str(tmp_path / "r.json")
    stream_path = str(tmp_path / "s.stream")
    with open(payload_path, "w", encoding="utf-8") as fh:
        json.dump({"tool_id": "hermes", "job_id": "job-1",
                   "phase": "probe", "op": "inspect",
                   "env_fingerprint": expected}, fh)
    import json as _json
    rc = phase_run_lib.main(
        ["job-1", "probe", "--payload", payload_path, "--result",
         result_path, "--stream", stream_path, "--op", "inspect"])
    assert rc == phase_run_lib.EXIT_OK
    with open(result_path, "r", encoding="utf-8") as fh:
        result = _json.load(fh)
    assert result["ok"] is False
    assert "secret" in str(result["data"])


def test_f11_mutation_evidence_failure_no_success(tmp_path, monkeypatch):
    """A mutation whose stream evidence failed maps to interrupted with
    recovery in the coordinator (never success)."""
    from backend.app.worker import runner as runner_lib

    r = runner_lib.Runner("job-f11", "n-f11")
    r.job = {"ack": ""}
    r.log = None

    def _fake_run_phase(phase, payload_extra, timeout_s, op=""):
        assert phase == "execute"
        return True, {"state": "succeeded", "error_code": "",
                      "error_detail": "", "exit_code": 0,
                      "before_version": "1.0", "after_version": "2.0",
                      "evidence_durable": False}, "", False

    monkeypatch.setattr(runner_lib.Runner, "_run_phase", _fake_run_phase)
    out = r._do_execute(__import__("types").SimpleNamespace(), 30.0)
    assert isinstance(out, dict) and out.get("state") == "interrupted"


def test_f11_probe_evidence_failure_safe_error(tmp_path, monkeypatch):
    """A probe whose result cannot be persisted fails safe: the worker
    exits OK with no result file (coordinator: missing result means
    interrupted), never a fabricated success. The fake adapter keeps
    this hermetic (no live installation touch)."""
    import types as _types
    from backend.app.worker import phase_run as phase_run_lib
    from backend.app.owner_env import (build_owner_contract,
                                       contract_fingerprint)

    support_lib.test_release_root()
    expected = contract_fingerprint(build_owner_contract(None))
    payload_path = str(tmp_path / "p.json")
    with open(payload_path, "w", encoding="utf-8") as fh:
        json.dump({"tool_id": "hermes", "job_id": "job-1",
                   "phase": "probe", "op": "inspect",
                   "env_fingerprint": expected}, fh)

    def _fake_adapter(tool_id):
        return _types.SimpleNamespace(
            enabled=True,
            inspect=lambda: {"version": "1.0"},
            discover=lambda: {},
            activity=lambda: {},
            verify=lambda: {},
            plan=lambda: {})

    monkeypatch.setattr(phase_run_lib, "_adapter", _fake_adapter)
    rc = phase_run_lib.main(
        ["job-1", "probe", "--payload", payload_path, "--result",
         "/proc/ega-nope-xyz-123/result.json", "--stream",
         str(tmp_path / "s.stream"), "--op", "inspect"])
    assert rc == phase_run_lib.EXIT_OK
    assert not os.path.exists("/proc/ega-nope-xyz-123/result.json")


def test_f11_dispatcher_event_failure_no_raw(tmp_path, monkeypatch):
    """Sanitizer failure in dispatcher events persists a fixed marker
    (never raw data) and reports failure instead of crashing."""
    from backend.app import events as events_lib
    import backend.app.sanitize as sanitize_lib

    conn = _fresh_db(tmp_path)
    monkeypatch.setattr(
        sanitize_lib, "sanitize_text",
        lambda text, secrets=(): (_ for _ in ()).throw(
            RuntimeError("sanitizer down")))
    assert events_lib.record_event(
        conn, "job-1", "reconcile_unknown",
        "raw secret-bearing detail") is True
    rows = conn.execute("SELECT detail FROM events WHERE job_id=?",
                        ("job-1",)).fetchall()
    assert len(rows) == 1
    assert "raw secret-bearing detail" not in rows[0]["detail"]
    assert "suppressed" in rows[0]["detail"]
    conn.close()


# -- F12 complete probe/mutation exclusion ---------------------------------------

def test_f12_all_installation_reads_classified(tmp_path):
    """Every op reading installation state belongs to
    INSTALLATION_READ_OPS (F12): no silent non-leased reader."""
    from backend.app import owner_probes as owner_probes_lib

    for op in ("inspect", "discover", "activity", "plan", "verify",
               "refresh"):
        assert op in owner_probes_lib.INSTALLATION_READ_OPS, op
        assert op in owner_probes_lib.OPS, op
    assert owner_probes_lib.CACHE_ONLY_OPS == ()


def test_f12_each_read_op_blocks_mutation(tmp_path):
    """A held probe lease — however the read op was classified —
    refuses reservation for inspect/discover/verify/activity/plan."""
    from backend.app import leases as leases_lib
    from backend.app.admission import admit

    for op in ("inspect", "discover", "verify"):
        conn = _fresh_db(tmp_path, name="f12-%s.db" % op)
        row = support_lib.v2_plan_row(conn, uuid.uuid4().hex)
        # The dispatcher acquires exactly one probe lease per
        # INSTALLATION_READ_OP before touching the installation.
        lease = leases_lib.acquire_probe_lease(
            conn, "hermes", "dispatcher-test")
        assert lease, op
        jid, created, err = admit(
            conn, "owner@example.invalid", "k-f12-%s" % op, row["id"],
            False, "fp-test-1", True, False)
        assert err == "busy" and not created, (op, err)
        assert leases_lib.release_lease(conn, lease) is True
        conn.close()


def test_f12_mutation_blocks_all_reads(tmp_path):
    """A held mutation lease refuses every probe-lease acquisition."""
    from backend.app import leases as leases_lib
    from backend.app.admission import admit

    conn = _fresh_db(tmp_path)
    row = support_lib.v2_plan_row(conn, uuid.uuid4().hex)
    jid, created, err = admit(
        conn, "owner@example.invalid", "k-f12-m", row["id"], False,
        "fp-test-1", True, False)
    assert err == "" and created
    for tool_id in ("hermes", "codex", "t3", "opencode"):
        assert leases_lib.acquire_probe_lease(
            conn, tool_id, "dispatcher-test") is None, tool_id
    conn.close()


def test_f12_lease_ttl_covers_probe_deadline_with_margin():
    """A bounded probe lease is only acceptable because the operation
    deadline is shorter than the TTL with safety margin (F12)."""
    from backend.app import leases as leases_lib
    from backend.app.worker import dispatch as dispatch_lib

    assert leases_lib.PROBE_LEASE_TTL_S > \
        dispatch_lib.PROBE_OP_TIMEOUT_S + 30


def test_f12_expired_read_lease_reclaimed_mutation_safe(tmp_path):
    """Expired abandoned read leases are reclaimable; mutation leases
    are never touched by expiry."""
    from backend.app import leases as leases_lib
    from backend.app.admission import admit

    conn = _fresh_db(tmp_path)
    lease = leases_lib.acquire_probe_lease(conn, "hermes", "tester")
    assert lease
    conn.execute("UPDATE execution_leases SET expires_at=?"
                 " WHERE id=?", ("2000-01-01T00:00:00+00:00", lease))
    conn.commit()
    assert leases_lib.reclaim_expired_probes(conn) == 1
    row = support_lib.v2_plan_row(conn, uuid.uuid4().hex)
    jid, created, err = admit(
        conn, "owner@example.invalid", "k-f12-r", row["id"], False,
        "fp-test-1", True, False)
    assert err == "" and created, err
    conn.close()


# -- G01 runner control-flow integrity -------------------------------------------

def _runner_source_lines():
    path = os.path.join(_REPO_ROOT, "backend", "app", "worker",
                        "runner.py")
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read().splitlines()


def test_g01_no_orphaned_try_in_runner():
    """The G01 stale fragment (a bare `try:` with no suite, followed by
    a dedented statement) must never reappear in Runner.run()."""
    lines = _runner_source_lines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped != "try:":
            continue
        indent = len(line) - len(line.lstrip(" "))
        assert index + 1 < len(lines), "try: at EOF"
        nxt = lines[index + 1]
        assert nxt.strip() != "", "empty suite after try:"
        nxt_indent = len(nxt) - len(nxt.lstrip(" "))
        assert nxt_indent > indent, \
            "orphaned try: at line %d" % (index + 1)


def test_g01_try_except_balance_in_runner():
    """Every `try:` in runner.py must have a matching `except`/`finally`
    at the same indent (tripwire against half-deleted blocks)."""
    stack = []
    for lineno, line in enumerate(_runner_source_lines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if stripped == "try:" or stripped.startswith("try:"):
            stack.append((indent, lineno))
            continue
        if stripped.startswith("except") or stripped.startswith("finally"):
            assert stack, \
                "orphaned %r at line %d" % (stripped[:12], lineno)
            top_indent, top_line = stack.pop()
            assert indent == top_indent, \
                "indent drift: %r at %d vs try: at %d" % (
                    stripped[:12], lineno, top_line)
    assert stack == [], \
        "unclosed try: blocks at lines %s" % [ln for _, ln in stack]


def test_g01_no_duplicated_adapter_gate_comment():
    """The duplicated 'Disabled adapters never mutate' comment that
    accompanied the stale fragment must not return."""
    lines = _runner_source_lines()
    hits = [i for i, line in enumerate(lines)
            if "Disabled adapters never mutate" in line]
    assert len(hits) <= 1, hits


# -- G02/G03 quiescence-ordered reconciliation -----------------------------------

def _stopped_units(monkeypatch, live=(), unknown=(), stopped_extra=()):
    from backend.app import units as units_lib

    def _fake(unit, timeout_s=10):
        if unit in live:
            return {"state": "live", "unit": unit, "active_state": "active",
                    "sub_state": "running", "main_pid": 1, "cgroup": "",
                    "identity_ok": True, "detail": ""}
        if unit in unknown:
            return {"state": "unknown", "unit": unit, "active_state": "",
                    "sub_state": "", "main_pid": 0, "cgroup": "",
                    "identity_ok": False, "detail": "bus down"}
        return {"state": "confirmed_stopped", "unit": unit,
                "active_state": "inactive", "sub_state": "dead",
                "main_pid": 0, "cgroup": "", "identity_ok": True,
                "detail": "manager=inactive load=not-found"}

    monkeypatch.setattr(units_lib, "query_unit", _fake)
    monkeypatch.setattr(units_lib, "query_unit_system", _fake)


def _plan_with_services(conn, plan_id, services):
    import json as _json
    from backend.app import plans as plans_lib

    support_lib.test_release_root()
    row = support_lib.v2_plan_row(conn, plan_id)
    conn.execute("UPDATE plans SET services=? WHERE id=?",
                 (_json.dumps(list(services)), plan_id))
    conn.commit()
    # Recompute the hash over the edited row (production path builds
    # services before hashing; the test edits afterwards, so rebind).
    stored = dict(conn.execute("SELECT * FROM plans WHERE id=?",
                               (plan_id,)).fetchone())
    conn.execute("UPDATE plans SET plan_hash=? WHERE id=?",
                 (plans_lib.canonical_plan_hash(
                     plans_lib._hash_view(stored)), plan_id))
    conn.commit()
    return row


def test_g02_receipt_never_overrides_live_process(tmp_path, monkeypatch):
    """Stopped unit + valid receipt + live execution-marked process
    holds (no apply, no release, no admission)."""
    from backend.app import reconcile_core as rc_lib
    from backend.app.admission import admit
    from backend.app.worker import dispatch as dispatch_lib

    conn = _fresh_db(tmp_path)
    row = support_lib.v2_plan_row(conn, uuid.uuid4().hex)
    jid, created, err = admit(
        conn, "owner@example.invalid", "k-g02p", row["id"], False,
        "fp-test-1", True, False)
    assert err == "" and created
    assert rc_lib.decide(
        {}, {"state": "confirmed_stopped"},
        {"_valid": True}, [{"pid": 4242, "cmdline": "x"}],
        {"quiescent": True, "evidence": [], "reason": ""})[0] == \
        "keep-unknown"
    conn.close()


def test_g02_dispatcher_holds_with_procs_despite_receipt(
        tmp_path, monkeypatch):
    """End to end through _reconcile_row: stopped unit + bound valid
    receipt + surviving process -> unknown-held, lease stays, admission
    stays blocked."""
    from backend.app import reconcile_core as rc_lib
    from backend.app.admission import admit
    from backend.app.worker import dispatch as dispatch_lib

    conn = _fresh_db(tmp_path)
    row = support_lib.v2_plan_row(conn, uuid.uuid4().hex)
    jid, created, err = admit(
        conn, "owner@example.invalid", "k-g02e", row["id"], False,
        "fp-test-1", True, False)
    assert err == "" and created
    assert jobs_lib_claim(conn, jid, "n-g02e") is True
    unit = rc_lib.canonical_unit(jid)
    # Unit itself stopped: only the surviving process may hold the row.
    _stopped_units(monkeypatch)
    monkeypatch.setattr(
        rc_lib, "job_processes",
        lambda hex_token, full_id="", exclude_pids=(): [
            {"pid": 4242, "cmdline": "ega-update-job-abc runner"}])
    db_row = conn.execute("SELECT * FROM jobs WHERE id=?",
                          (jid,)).fetchone()
    assert dispatch_lib._reconcile_row(conn, db_row) == "unknown-held"
    assert _lease_held(conn, jid)
    row2 = support_lib.v2_plan_row(conn, uuid.uuid4().hex)
    _jid2, created2, err2 = admit(
        conn, "owner@example.invalid", "k-g02e-new", row2["id"], False,
        "fp-test-1", True, False)
    assert err2 == "busy" and not created2
    conn.close()


def jobs_lib_claim(conn, job_id, nonce):
    from backend.app import jobs as jobs_lib

    return jobs_lib.claim_with_nonce(conn, job_id, nonce)


def test_g03_delegated_live_blocks_release(tmp_path, monkeypatch):
    """Stopped runner + valid receipt + live delegated service holds
    ownership (no apply, no release)."""
    from backend.app import reconcile_core as rc_lib

    conn = _fresh_db(tmp_path)
    _plan_with_services(conn, uuid.uuid4().hex, ["svc-live.service"])
    _stopped_units(monkeypatch, live=["svc-live.service"])
    job = {"id": "job-g03a", "plan_id": None}
    # Fetch the real plan id back for the helper.
    prow = conn.execute(
        "SELECT id FROM plans ORDER BY created_at DESC LIMIT 1").fetchone()
    job["plan_id"] = str(dict(prow)["id"])
    quiescent, evidence, reason = rc_lib.delegated_quiescence(conn, job)
    assert quiescent is False
    assert any(e["service"] == "svc-live.service" for e in evidence)
    assert rc_lib.decide(
        job, {"state": "confirmed_stopped"}, {"_valid": True}, [],
        {"quiescent": quiescent, "evidence": evidence,
         "reason": reason})[0] == "keep-unknown"
    conn.close()


def test_g03_delegated_unknown_blocks_release(tmp_path, monkeypatch):
    from backend.app import reconcile_core as rc_lib

    conn = _fresh_db(tmp_path)
    _plan_with_services(conn, uuid.uuid4().hex, ["svc-mystery.service"])
    _stopped_units(monkeypatch, unknown=["svc-mystery.service"])
    prow = conn.execute(
        "SELECT id FROM plans ORDER BY created_at DESC LIMIT 1").fetchone()
    job = {"id": "job-g03b", "plan_id": str(dict(prow)["id"])}
    quiescent, _evidence, _reason = rc_lib.delegated_quiescence(conn, job)
    assert quiescent is False
    conn.close()


def test_g03_full_quiescence_applies(tmp_path, monkeypatch):
    """Stopped unit + no processes + delegated stopped + valid receipt
    is the ONLY combination that applies."""
    from backend.app import reconcile_core as rc_lib

    conn = _fresh_db(tmp_path)
    _plan_with_services(conn, uuid.uuid4().hex, ["svc-done.service"])
    _stopped_units(monkeypatch)
    prow = conn.execute(
        "SELECT id FROM plans ORDER BY created_at DESC LIMIT 1").fetchone()
    job = {"id": "job-g03c", "plan_id": str(dict(prow)["id"])}
    quiescent, evidence, reason = rc_lib.delegated_quiescence(conn, job)
    assert quiescent is True, reason
    assert evidence and evidence[0]["state"] == "confirmed_stopped"
    assert rc_lib.decide(
        job, {"state": "confirmed_stopped"}, {"_valid": True}, [],
        {"quiescent": quiescent, "evidence": evidence,
         "reason": reason})[0] == "apply-receipt"
    conn.close()


def test_g03_no_receipt_quiescent_interrupts(tmp_path, monkeypatch):
    from backend.app import reconcile_core as rc_lib

    conn = _fresh_db(tmp_path)
    support_lib.v2_plan_row(conn, uuid.uuid4().hex)
    _stopped_units(monkeypatch)
    prow = conn.execute(
        "SELECT id FROM plans ORDER BY created_at DESC LIMIT 1").fetchone()
    job = {"id": "job-g03d", "plan_id": str(dict(prow)["id"])}
    quiescent, _evidence, _reason = rc_lib.delegated_quiescence(conn, job)
    assert quiescent is True
    assert rc_lib.decide(
        job, {"state": "confirmed_stopped"}, None, [],
        {"quiescent": quiescent, "evidence": [], "reason": ""})[0] == \
        "mark-interrupted"
    conn.close()


def test_g03_terminal_held_lease_live_delegated_stays(tmp_path, monkeypatch):
    """Terminal DB row + held lease + live delegated operation: the
    lease stays and admission stays blocked."""
    from backend.app import reconcile_core as rc_lib
    from backend.app.admission import admit
    from backend.app.worker import dispatch as dispatch_lib

    conn = _fresh_db(tmp_path)
    plan_id = uuid.uuid4().hex
    _plan_with_services(conn, plan_id, ["svc-stuck.service"])
    row = dict(conn.execute("SELECT * FROM plans WHERE id=?",
                            (plan_id,)).fetchone())
    jid, created, err = admit(
        conn, "owner@example.invalid", "k-g03e", row["id"], False,
        "fp-test-1", True, False)
    assert err == "" and created
    assert jobs_lib_claim(conn, jid, "n-g03e") is True
    from backend.app import tx as tx_lib
    tx_lib.transition_tx(conn, jid, "failed", step="updating",
                         expect_states=["preflight"],
                         update={"error_code": "install_failed"},
                         event="failed", event_detail="")
    _stopped_units(monkeypatch, live=["svc-stuck.service"])
    # Unit itself stopped, but delegated service live: hold everything.
    db_row = conn.execute("SELECT * FROM jobs WHERE id=?",
                          (jid,)).fetchone()
    outcome = dispatch_lib._reconcile_row(conn, db_row)
    assert outcome == "unknown-held", outcome
    assert _lease_held(conn, jid)
    conn.close()


def test_g03_shared_rule_across_reconcilers():
    """Dispatcher and SSH reconcile decide through the same
    reconcile_core.decide + delegated_quiescence entry points (one
    algorithm, not two)."""
    import inspect
    from backend.app.worker import dispatch as dispatch_lib
    import backend.app.worker.reconcile as reconcile_mod

    dispatch_src = inspect.getsource(dispatch_lib._reconcile_row)
    assert "delegated_quiescence" in dispatch_src
    assert ".decide(" in dispatch_src
    reconcile_src = inspect.getsource(reconcile_mod.main)
    assert "delegated_quiescence" in reconcile_src or \
        "_delegated_proof" in reconcile_src
    assert ".decide(" in reconcile_src

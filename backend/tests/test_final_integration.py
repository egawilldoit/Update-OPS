"""Final static integration regression tests (F01-F16, write-only).

Behavioral regression artifacts for every F-finding. NOT EXECUTED in
this phase per instruction. Mocks stay at external boundaries
(systemd bus, process spawn); console-component integration is never
mocked away. Python 3.10 compatible, pytest style, stdlib + backend.
"""
from __future__ import annotations

import os
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

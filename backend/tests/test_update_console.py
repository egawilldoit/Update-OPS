"""Offline unit tests for Update-OPS V1 console (never needs network).

Covers review findings without executing servers, builds, or probes:
- concurrent single-slot admission via admission.admit (threads + temp SQLite)
- idempotent replay vs 409 conflict (same key/same payload vs different payload)
- JWT rejection: forged / expired / wrong-audience + owner allow-list
  (import-guarded when PyJWT is missing)
- CSRF mint/check + Origin exactness
- redaction of secrets split across StreamRedactor.feed chunk boundaries
- job transition timestamps (started_at / finished_at + events)
- log cursor pagination ordering via routes._read_log_page

Style: plain pytest functions with tmp_path, no plugins. Stdlib threads,
sqlite3, and json only except for the guarded PyJWT / FastAPI imports.
Python 3.10 compatible.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import time
import uuid

import pytest

# Make `backend.app.*` importable whether pytest runs from the repo root or
# from backend/. Never touches the real state dir.
_REPO_ROOT = os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

from backend.app import db as db_lib
from backend.app import jobs as jobs_lib
from backend.app import redaction as redaction_lib
from backend.app import auth as auth_lib
from backend.app.admission import admit as admit_lib

import support as support_lib

try:
    import jwt as pyjwt  # type: ignore
    _HAS_PYJWT = True
except Exception:
    pyjwt = None  # type: ignore
    _HAS_PYJWT = False


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_db(path):
    # type: (str) -> sqlite3.Connection
    conn = db_lib.connect(path)
    db_lib.migrate(conn)
    conn.commit()
    return conn


def _insert_plan(conn, plan_id, tool_id="hermes", fingerprint="fp-test-1",
                 target="9.9.9", expires_future=True):
    # type: (...) -> None
    # v2 immutable plans via the shared helper (single admission path).
    support_lib.v2_plan_row(conn, plan_id, tool_id=tool_id,
                            fingerprint=fingerprint, target=target,
                            expires_future=expires_future)


def _reserve_in_tx(db_path, tool_id, plan_id, subject, idem_key, ack, out, idx):
    # type: (...) -> None
    """One admission attempt in its own connection (no tx held by caller;
    admission owns its transaction)."""
    support_lib.test_release_root()
    conn = db_lib.connect(db_path)
    try:
        try:
            plan = conn.execute("SELECT fingerprint FROM plans WHERE id=?",
                                (plan_id,)).fetchone()
            fp = str(dict(plan).get("fingerprint", "")) if plan else ""
        except Exception as exc:
            out[idx] = ("error", "", False, "plan_read:%s" % exc)
            return
        try:
            job_id, created, err = admit_lib(
                conn, subject, idem_key, plan_id, ack, fp, True, False)
        except Exception as exc:
            out[idx] = ("error", "", False, "raise:%s" % exc)
            return
        if err:
            out[idx] = ("err", "", False, err)
        else:
            out[idx] = ("ok", job_id, created, "")
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 1. concurrent reservation: single active slot
# ---------------------------------------------------------------------------

def test_concurrent_reservation_single_slot(tmp_path):
    db_path = str(tmp_path / "state.db")
    setup = _make_db(db_path)
    plan_ids = [str(uuid.uuid4()) for _ in range(8)]
    for pid in plan_ids:
        _insert_plan(setup, pid)
    setup.close()

    n = len(plan_ids)
    out = [None] * n  # type: ignore
    threads = []
    for i, pid in enumerate(plan_ids):
        t = threading.Thread(
            target=_reserve_in_tx,
            args=(db_path, "hermes", pid, "owner@example.invalid",
                  "key-%d" % i, False, out, i))
        threads.append(t)
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert all(o is not None for o in out)
    successes = [o for o in out if o[0] == "ok"]
    # Exactly one winner owns the single slot; the rest see busy.
    assert len(successes) == 1, out
    for o in out:
        if o[0] == "err":
            assert o[3] == "busy", o
    # Slot is held: a further reservation must also see busy.
    extra = [None]  # type: ignore
    _reserve_in_tx(db_path, "hermes", plan_ids[0],
                   "owner@example.invalid", "extra-key", False, extra, 0)
    assert extra[0][0] == "err" and extra[0][3] == "busy"


def test_concurrent_same_idempotency_key_single_winner(tmp_path):
    """Same subject+key racing: one wins, the other replays or stays busy."""
    db_path = str(tmp_path / "state.db")
    setup = _make_db(db_path)
    pid = str(uuid.uuid4())
    _insert_plan(setup, pid)
    setup.close()
    out = [None, None]  # type: ignore
    t1 = threading.Thread(
        target=_reserve_in_tx,
        args=(db_path, "hermes", pid, "owner@example.invalid",
              "shared-key", False, out, 0))
    t2 = threading.Thread(
        target=_reserve_in_tx,
        args=(db_path, "hermes", pid, "owner@example.invalid",
              "shared-key", False, out, 1))
    t1.start()
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)
    kinds = sorted(o[0] for o in out)
    assert kinds in (["ok", "ok"], ["err", "ok"]), out
    if kinds == ["ok", "ok"]:
        # Idempotent replay under race: both report the same job id.
        assert out[0][1] == out[1][1], out


# ---------------------------------------------------------------------------
# 2. idempotent replay vs 409 conflict
# ---------------------------------------------------------------------------

def test_idempotent_replay_vs_conflict(tmp_path):
    db_path = str(tmp_path / "state.db")
    conn = _make_db(db_path)
    pid_a = str(uuid.uuid4())
    pid_b = str(uuid.uuid4())
    _insert_plan(conn, pid_a, fingerprint="fp-a", target="1.0.0")
    _insert_plan(conn, pid_b, fingerprint="fp-b", target="2.0.0")
    subject = "owner@example.invalid"
    key = "idem-123"
    support_lib.test_release_root()
    job_a, created_a, err_a = admit_lib(
        conn, subject, key, pid_a, False, "fp-a", True, False)
    assert err_a == "" and created_a is True and job_a
    # Same key + same payload replays the original job.
    job_replay, created_replay, err_replay = admit_lib(
        conn, subject, key, pid_a, False, "fp-a", True, False)
    assert err_replay == "" and created_replay is False
    assert job_replay == job_a
    # Same key + different payload (different plan) is a 409 conflict.
    _job_c, _created_c, err_c = admit_lib(
        conn, subject, key, pid_b, False, "fp-b", True, False)
    assert err_c == "conflict"
    # Same key + different ack is also a conflict (ack is part of the hash).
    _job_d, _created_d, err_d = admit_lib(
        conn, subject, key, pid_a, True, "fp-a", True, False)
    assert err_d == "conflict"
    conn.close()


# ---------------------------------------------------------------------------
# 3. JWT: forged / expired / wrong-audience + owner allow-list
# ---------------------------------------------------------------------------

def _needs_pyjwt():
    return pytest.mark.skipif(not _HAS_PYJWT, reason="PyJWT not installed")


@_needs_pyjwt()
def test_jwt_malformed_and_bad_alg_rejected(monkeypatch):
    # Malformed tokens fail before any JWKS fetch.
    claims, err = auth_lib.validate_access_token(
        "not-a-jwt", "https://team.example", "aud-1",
        ["owner@example.invalid"], ttl_s=600)
    assert claims is None and err
    # HS256 is rejected (RS256 only) without trusting the token.
    tok = pyjwt.encode({"email": "owner@example.invalid",
                        "exp": int(time.time()) + 600,
                        "iss": "https://team.example",
                        "aud": "aud-1"},
                       "insecure-secret", algorithm="HS256")
    if isinstance(tok, bytes):
        tok = tok.decode("utf-8")
    claims, err = auth_lib.validate_access_token(
        tok, "https://team.example", "aud-1",
        ["owner@example.invalid"], ttl_s=600)
    assert claims is None and err in (
        "bad_algorithm", "jwks_unavailable", "invalid_token",
        "invalid_token:InvalidAlgorithmError")


@_needs_pyjwt()
def test_jwt_forged_expired_audience_and_allowlist(monkeypatch):
    try:
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives import serialization
    except Exception:
        pytest.skip("cryptography not installed for RSA JWT tests")
    import jwt as _jwt
    team = "https://team.example"
    aud = "aud-1"
    owners = ["owner@example.invalid"]
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()).decode("utf-8")
    pub = key.public_key()
    pub_pem = pub.public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo).decode("utf-8")
    # jwt.decode accepts a PEM string, so the mocked JWKS fetch returns PEM.
    monkeypatch.setattr(
        auth_lib, "fetch_jwks", lambda *a, **k: [pub_pem])

    def _mint(payload, headers=None):
        tok = _jwt.encode(payload, priv_pem, algorithm="RS256",
                          headers=headers or {"kid": "test-key"})
        return tok.decode("utf-8") if isinstance(tok, bytes) else tok

    now = int(time.time())
    base = {"email": "owner@example.invalid", "iss": team, "aud": aud}
    # Valid token passes and binds the owner identity.
    good = _mint(dict(base, exp=now + 600, iat=now))
    claims, err = auth_lib.validate_access_token(
        good, team, aud, owners, ttl_s=600)
    assert err == "" and claims is not None
    assert str(claims.get("email", "")).lower() == "owner@example.invalid"
    # Forged: signed by a different key must not validate.
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other_pem = other.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()).decode("utf-8")
    forged = _jwt.encode(dict(base, exp=now + 600, iat=now),
                         other_pem, algorithm="RS256",
                         headers={"kid": "test-key"})
    forged = forged.decode("utf-8") if isinstance(forged, bytes) else forged
    claims, err = auth_lib.validate_access_token(
        forged, team, aud, owners, ttl_s=600)
    assert claims is None and err
    # Expired token is rejected even with a valid signature.
    expired = _mint(dict(base, exp=now - 3600, iat=now - 7200))
    claims, err = auth_lib.validate_access_token(
        expired, team, aud, owners, ttl_s=600)
    assert claims is None and err == "expired"
    # Wrong audience is rejected.
    wrong_aud = _mint(dict(base, exp=now + 600, iat=now, aud="other-aud"))
    claims, err = auth_lib.validate_access_token(
        wrong_aud, team, aud, owners, ttl_s=600)
    assert claims is None and err == "wrong_audience"
    # Valid signature but non-owner identity is forbidden (allow-list).
    outsider = _mint({"email": "intruder@example.invalid",
                      "iss": team, "aud": aud, "exp": now + 600, "iat": now})
    claims, err = auth_lib.validate_access_token(
        outsider, team, aud, owners, ttl_s=600)
    assert claims is None and err == "identity_not_authorized"


# ---------------------------------------------------------------------------
# 4. CSRF mint/check + Origin exactness
# ---------------------------------------------------------------------------

def test_csrf_mint_check_roundtrip():
    secret = "test-csrf-secret-32-bytes-minimum-xyz"
    subject = "owner@example.invalid"
    tok = auth_lib.mint_csrf(subject, secret)
    assert tok and isinstance(tok, str)
    assert auth_lib.check_csrf(tok, subject, secret) is True
    # Wrong subject, wrong secret, or tampered token must fail.
    assert auth_lib.check_csrf(tok, "other@example.invalid", secret) is False
    assert auth_lib.check_csrf(tok, subject, "other-secret") is False
    assert auth_lib.check_csrf(tok + "x", subject, secret) is False
    assert auth_lib.check_csrf("", subject, secret) is False
    assert auth_lib.check_csrf(tok, "", secret) is False
    assert auth_lib.check_csrf(tok, subject, "") is False


def test_origin_exactness():
    allowed = "https://update-console.example.invalid"
    assert auth_lib.check_origin(allowed, allowed) is True
    # Scheme, host, case, trailing slash, and prefix tricks all fail.
    assert auth_lib.check_origin("http://update-console.example.invalid",
                                 allowed) is False
    assert auth_lib.check_origin(
        "https://update-console.example.invalid/", allowed) is False
    assert auth_lib.check_origin(
        "https://update-console.example.invalid.evil.invalid",
        allowed) is False
    assert auth_lib.check_origin(
        "https://UPDATE-CONSOLE.EXAMPLE.INVALID", allowed) is False
    assert auth_lib.check_origin("", allowed) is False
    assert auth_lib.check_origin(allowed, "") is False


# ---------------------------------------------------------------------------
# 5. redaction across StreamRedactor.feed chunk boundaries
# ---------------------------------------------------------------------------

def test_stream_redactor_splits_secret_across_chunks():
    secret = "supersecret-ABC-123-xyz"
    red = redaction_lib.StreamRedactor((secret,))
    # Split the secret across two feeds with no newline in between: the
    # decoder must buffer the partial line and still redact on completion.
    part1 = ("deploying with token " + secret[:8]).encode("utf-8")
    part2 = (secret[8:] + " done\n").encode("utf-8")
    out1 = red.feed(part1)
    assert out1 == []
    out2 = red.feed(part2)
    assert len(out2) == 1
    assert secret not in out2[0]
    assert redaction_lib.REPLACEMENT in out2[0]
    # Pattern redaction still applies after secret replacement.
    red2 = redaction_lib.StreamRedactor(())
    lines = red2.feed(b"Authorization: Bearer abcdefgh12345678\n")
    assert lines and "***REDACTED***" in lines[0]
    assert "abcdefgh12345678" not in lines[0]


def test_stream_redactor_flush_redacts_tail_without_newline():
    secret = "tailsecret-999"
    red = redaction_lib.StreamRedactor((secret,))
    assert red.feed(b"partial ") == []
    tail = red.flush()
    assert len(tail) == 1 and "partial" in tail[0]
    red3 = redaction_lib.StreamRedactor((secret,))
    assert red3.feed(("leak " + secret).encode("utf-8")) == []
    flushed = red3.flush()
    assert len(flushed) == 1
    assert secret not in flushed[0]
    assert redaction_lib.REPLACEMENT in flushed[0]


def test_redact_text_bare_secret_and_controls():
    secret = "bare-secret-value-42"
    line = "prefix \x1b[31m" + secret + " suffix Bearer abcdefgh12345678"
    cleaned = redaction_lib.redact_text(
        redaction_lib.strip_controls(line), (secret,))
    assert secret not in cleaned
    assert "abcdefgh12345678" not in cleaned
    assert "\x1b" not in cleaned


# ---------------------------------------------------------------------------
# 6. job transition timestamps
# ---------------------------------------------------------------------------

def test_job_transition_timestamps(tmp_path):
    db_path = str(tmp_path / "state.db")
    conn = _make_db(db_path)
    pid = str(uuid.uuid4())
    _insert_plan(conn, pid)
    support_lib.test_release_root()
    job_id, created, err = admit_lib(
        conn, "owner@example.invalid", "k-1", pid, False, "fp-test-1",
        True, False)
    assert err == "" and created
    row = conn.execute(
        "SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    assert row["created_at"]
    assert row["started_at"] == ""
    assert row["finished_at"] == ""
    jobs_lib.transition(conn, job_id, "preflight", step="preflight")
    conn.commit()
    row = conn.execute(
        "SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    assert row["state"] == "preflight" and row["started_at"]
    assert row["finished_at"] == ""
    jobs_lib.transition(conn, job_id, "succeeded", step="succeeded",
                        exit_code=0, after_version="9.9.9")
    conn.commit()
    row = conn.execute(
        "SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    assert row["state"] == "succeeded" and row["finished_at"]
    assert row["after_version"] == "9.9.9"
    events = conn.execute(
        "SELECT event_type FROM events WHERE job_id=? ORDER BY seq",
        (job_id,)).fetchall()
    kinds = [r["event_type"] for r in events]
    assert "accepted" in kinds and "preflight" in kinds \
        and "succeeded" in kinds
    conn.close()


# ---------------------------------------------------------------------------
# 7. log cursor pagination ordering
# ---------------------------------------------------------------------------

def test_log_cursor_pagination_ordering(tmp_path, monkeypatch):
    try:
        from backend.app.api import routes as routes_lib
        from backend.app.config import settings as settings_lib
    except Exception:
        pytest.skip("API routes not importable offline")
    log_dir = str(tmp_path / "logs")
    os.makedirs(log_dir, exist_ok=True)
    monkeypatch.setattr(settings_lib, "log_dir", log_dir)
    monkeypatch.setattr(settings_lib, "per_job_log_cap_bytes",
                        20 * 1024 * 1024)
    # G05: an explicit secrets file (patterns still apply to these
    # secret-free lines; a missing source would fail closed instead).
    _secrets_path = str(tmp_path / "test-secrets.env")
    with open(_secrets_path, "w", encoding="utf-8") as _sfh:
        _sfh.write("TEST_DUMMY_SECRET_KEY=dummy-secret-value-12345\n")
    monkeypatch.setattr(settings_lib, "secrets_file", _secrets_path)
    job_id = str(uuid.uuid4())
    path = os.path.join(log_dir, job_id + ".jsonl")
    with open(path, "w", encoding="utf-8") as fh:
        for seq in range(10):
            fh.write(json.dumps(
                {"seq": seq, "ts": "2026-09-08T00:00:%02dZ" % seq,
                 "stream": "stdout" if seq % 2 == 0 else "stderr",
                 "line": "line-%d" % seq}) + "\n")
    page1 = routes_lib._read_log_page(job_id, 0, 4)
    assert [r["seq"] for r in page1["records"]] == [1, 2, 3, 4]
    assert page1["next_after"] == 4
    page2 = routes_lib._read_log_page(job_id, page1["next_after"], 4)
    assert [r["seq"] for r in page2["records"]] == [5, 6, 7, 8]
    page3 = routes_lib._read_log_page(job_id, page2["next_after"], 4)
    assert [r["seq"] for r in page3["records"]] == [9]
    assert page3["next_after"] == 9
    # after beyond the end returns no records and keeps the cursor.
    page4 = routes_lib._read_log_page(job_id, 9, 4)
    assert page4["records"] == [] and page4["next_after"] == 9
    # Unknown job log file returns an empty page (never raises).
    missing = routes_lib._read_log_page(str(uuid.uuid4()), 0, 10)
    assert missing["records"] == []


def test_log_truncation_marker_only(tmp_path, monkeypatch):
    try:
        from backend.app.api import routes as routes_lib
        from backend.app.config import settings as settings_lib
    except Exception:
        pytest.skip("API routes not importable offline")
    log_dir = str(tmp_path / "logs")
    os.makedirs(log_dir, exist_ok=True)
    monkeypatch.setattr(settings_lib, "log_dir", log_dir)
    monkeypatch.setattr(settings_lib, "per_job_log_cap_bytes",
                        20 * 1024 * 1024)
    # G05: explicit secrets file (a missing source would fail closed).
    _secrets_path = str(tmp_path / "test-secrets.env")
    with open(_secrets_path, "w", encoding="utf-8") as _sfh:
        _sfh.write("TEST_DUMMY_SECRET_KEY=dummy-secret-value-12345\n")
    monkeypatch.setattr(settings_lib, "secrets_file", _secrets_path)
    job_id = str(uuid.uuid4())
    path = os.path.join(log_dir, job_id + ".jsonl")
    with open(path, "w", encoding="utf-8") as fh:
        # A user line containing "truncate" must NOT flag truncation, and a
        # per-line overlong suffix must not either; only the runner's
        # per-job cap marker does.
        fh.write(json.dumps(
            {"seq": 0, "ts": "2026-09-08T00:00:00Z", "stream": "stdout",
             "line": "please truncate this table"}) + "\n")
        fh.write(json.dumps(
            {"seq": 1, "ts": "2026-09-08T00:00:01Z", "stream": "stdout",
             "line": "x" * 9000}) + "\n")
    page = routes_lib._read_log_page(job_id, -1, 10)
    assert page["truncated"] is False
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(
            {"seq": 2, "ts": "2026-09-08T00:00:02Z", "stream": "event",
             "line": routes_lib.LOG_TRUNCATION_MARKER}) + "\n")
    page2 = routes_lib._read_log_page(job_id, -1, 10)
    assert page2["truncated"] is True

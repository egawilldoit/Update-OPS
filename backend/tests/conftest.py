"""Shared pytest fixtures for Update-OPS V1 (write-only artifacts).

The G05 strict secret contract requires every secret-loading path to
fail closed when its source is broken. To keep the suite hermetic
without editing every test, this autouse fixture provisions an
explicit disposable secrets file per test (real file, real loader —
never silent). Tests covering secret FAILURE override it with
monkeypatch afterwards; the fixture never masks those paths.
"""
from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _provision_test_secrets(tmp_path, monkeypatch):
    secrets_path = tmp_path / "test-secrets.env"
    try:
        secrets_path.write_text(
            "TEST_DUMMY_SECRET_KEY=dummy-secret-value-12345\n",
            encoding="utf-8",
        )
    except OSError:
        pass
    try:
        from backend.app.config import settings as _settings
        monkeypatch.setattr(
            _settings, "secrets_file", str(secrets_path))
    except Exception:
        pass
    yield
    try:
        os.environ.pop("EGA_RELEASE_ROOT", None)
    except Exception:
        pass

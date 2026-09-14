"""W4-D8 VM acceptance harness wrapper (MANUAL / VM ONLY).

Skips by default. Set EGA_VM_ACCEPTANCE=1 on a disposable root VM to run
``deploy/tests/vm-acceptance-readiness.sh``, which exercises real Linux
users/groups, POSIX ACLs, the already-running user manager,
``systemd --user``, a real transient unit, service identities, and the
canonical readiness assessment against the live deployment.

This file never runs automatically against production: the harness
itself requires EGA_VM_ACCEPTANCE=1, root, systemd, and a release venv,
and this wrapper skips unless the same environment variable is set.
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))
_HARNESS = os.path.join(_REPO_ROOT, "deploy", "tests",
                        "vm-acceptance-readiness.sh")


def test_vm_harness_is_marked_manual_and_guarded():
    assert os.path.isfile(_HARNESS)
    with open(_HARNESS, "r", encoding="utf-8") as fh:
        text = fh.read()
    assert "EGA_VM_ACCEPTANCE" in text
    assert "NOT RUN" in text
    assert "MANUAL" in text


@pytest.mark.skipif(
    os.environ.get("EGA_VM_ACCEPTANCE") != "1" or os.geteuid() != 0,
    reason="manual VM acceptance harness (set EGA_VM_ACCEPTANCE=1 as root "
           "on a disposable VM)")
def test_vm_acceptance_readiness_harness():
    proc = subprocess.run(
        ["/bin/bash", _HARNESS], capture_output=True, text=True,
        timeout=600)
    sys.stderr.write(proc.stderr)
    assert proc.returncode == 0, proc.stdout + proc.stderr

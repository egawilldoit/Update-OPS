"""Hermetic report/guard tests. These do not execute VM acceptance."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("vm_contract", ROOT / "deploy/tests/vm-contract-acceptance.py")
vm = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(vm)


def test_blocked_report_records_every_unexecuted_contract(tmp_path, monkeypatch):
    target = tmp_path / "report.json"
    monkeypatch.setattr(vm.sys, "argv", ["vm", "blocked", "--report", str(target)])
    assert vm.main() == 2
    result = json.loads(target.read_text())
    assert result["status"] == "BLOCKED_REAL_VM_REQUIRED"
    assert set(result) == {"commit_sha", "environment", "test_case", "started_at", "finished_at", "status", "evidence", "failure_reason"}
    assert result["evidence"]["cases"] == {case: "NOT_EXECUTED" for case in vm.REQUIRED}
    assert result["environment"]["production_equivalence"] is False


def test_refused_guard_never_constructs_suite(tmp_path, monkeypatch):
    target = tmp_path / "report.json"
    def refused(_):
        raise AssertionError("disposable designation missing")
    def forbidden(_):
        pytest.fail("suite constructed before successful guard")
    monkeypatch.setattr(vm, "guard", refused)
    monkeypatch.setattr(vm, "Suite", forbidden)
    monkeypatch.setattr(vm.sys, "argv", ["vm", "run", "--case", "flock", "--report", str(target)])
    assert vm.main() == 2
    assert json.loads(target.read_text())["status"] != "PASS_REAL_OS_CASE"


@pytest.mark.parametrize("stage", vm.STAGES)
def test_each_missing_or_failed_stage_refuses_readiness(stage):
    suite = vm.Suite.__new__(vm.Suite)
    suite.config = Path("fixture-config")
    for entry in ({"ok": False}, None):
        stages = {name: {"ok": True, "private": "do-not-persist"} for name in vm.STAGES}
        if entry is None:
            del stages[stage]
        else:
            stages[stage] = entry
        suite.module = lambda *a: SimpleNamespace(returncode=0, stdout=json.dumps({"ready": True, "stages": stages}))
        with pytest.raises(AssertionError, match="mandatory readiness failed"):
            suite.canonical_readiness()


def test_readiness_evidence_only_contains_allowlisted_booleans():
    suite = vm.Suite.__new__(vm.Suite)
    suite.config = Path("fixture-config")
    suite.module = lambda *a: SimpleNamespace(returncode=0, stdout=json.dumps({
        "ready": True, "csrf_token": "do-not-persist", "stages": {
            name: {"ok": True, "evidence": "do-not-persist"} for name in vm.STAGES}}))
    evidence = suite.canonical_readiness()
    assert evidence == {"stages": {name: True for name in vm.STAGES}, "exit_code": 0}
    assert "do-not-persist" not in json.dumps(evidence)


def test_atomic_report_replacement(tmp_path):
    path = tmp_path / "output" / "report.json"
    vm.write_report(path, {"status": "old"})
    vm.write_report(path, {"status": "BLOCKED_REAL_VM_REQUIRED"})
    assert json.loads(path.read_text())["status"] == "BLOCKED_REAL_VM_REQUIRED"
    assert list(path.parent.iterdir()) == [path]


def test_legacy_readiness_requires_disposable_acknowledgement():
    import os
    import subprocess
    result = subprocess.run(["bash", str(ROOT / "deploy/tests/vm-acceptance-readiness.sh")],
                            env={**os.environ, "EGA_VM_ACCEPTANCE": "1", "EGA_VM_DISPOSABLE": "0"},
                            capture_output=True, text=True)
    assert result.returncode == 2
    assert "disposable VM required" in result.stderr

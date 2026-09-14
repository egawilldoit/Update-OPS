#!/usr/bin/env python3
"""Acceptance-only entry point. Stop the real ProbeWorker, not its dispatcher."""
import json
import os
from pathlib import Path
import threading

from backend.app.worker import dispatch

marker = Path(dispatch.settings.state_dir) / "vm-worker-fault.json"
# Inject once. The systemd restart then uses the unmodified dispatcher main.
if not marker.exists():
    start = dispatch.ProbeWorker.start
    reconcile = dispatch.reconcile_boot
    facts = {"reconcile_boot_completed": False}

    def reconciled(conn):
        reconcile(conn)
        facts["reconcile_boot_completed"] = True

    def started(worker, timeout_s=None):
        result = start(worker, timeout_s)
        def stop_probe():
            facts["probe_ready_before_fault"] = worker.is_ready()
            worker.stop()
            facts["probe_stopped"] = not worker.is_alive() and not worker.is_ready()
            facts["dispatcher_pid"] = os.getpid()
            marker.write_text(json.dumps(facts))
        timer = threading.Timer(3, stop_probe)
        timer.daemon = True
        timer.start()
        return result

    dispatch.reconcile_boot = reconciled
    dispatch.ProbeWorker.start = started

try:
    dispatch.main()
except SystemExit as error:
    if marker.exists():
        result = json.loads(marker.read_text())
        result["dispatcher_failed"] = str(error) == "probe worker not ready; dispatcher exiting"
        marker.write_text(json.dumps(result))
    raise

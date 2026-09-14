#!/usr/bin/env python3
"""Acceptance-only: make the real stop-proof query use an unavailable user bus."""
import json

from backend.app.config import load_settings
from backend.app.owner_env import _default_command_runner, transient_acceptance


def runner(argv, env=None, timeout_s=60):
    command = list(argv)
    if "systemctl --user show" in command[-1]:
        command[-1] = "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/ega-acceptance-missing-bus " + command[-1]
    return _default_command_runner(command, env, timeout_s)


result = transient_acceptance(settings=load_settings(), command_runner=runner)
print(json.dumps({key: result[key] for key in
                  ("ok", "launch_exit", "report_ok", "identity_ok", "result_ok", "unit_terminated", "unit_state")}))

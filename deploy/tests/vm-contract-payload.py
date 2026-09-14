#!/usr/bin/env python3
"""Disposable transient fixture; output contains only identity and fixture facts."""
import json
import os
from pathlib import Path
import sys

scratch, config = map(Path, sys.argv[1:])
payload = json.loads((scratch / "payload.json").read_text())
assert payload == {"kind": "vm-contract", "value": 7}
assert isinstance(json.loads(config.read_text()), dict)
(scratch / "result.json").write_text(json.dumps({
    "kind": "vm-contract-result", "uid": os.getuid(), "gid": os.getgid(),
    "groups": os.getgroups(), "payload_read": True, "config_read": True,
}))
(scratch / "stream.log").write_text("fixture round trip\n")

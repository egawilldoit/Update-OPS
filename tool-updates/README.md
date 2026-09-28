# VM tool wrappers

`/home/ubuntu/bin/update` dispatches to the canonical scripts under
`/home/ubuntu/tool-updates/scripts/`. The scripts in this directory mirror
those deployed VM paths; the shared backup helper is deployed beside them.
The repository's existing `scripts/` directory contains the separate API
dispatcher and remains unchanged.

## T3 inactive split-state recovery

`update-t3.sh` starts an inactive service only for a validated repairable
split state: the CLI and `service-state.json` versions differ, the recorded
active runtime is a real executable inside the canonical versions directory,
systemd's exact `ExecStart` names that runtime, `MainPID` is zero, no pending,
failed, or committed native update state is recorded, and no T3 runtime
process is live. After starting the service, the wrapper checks active state,
`MainPID`, executable, `ExecStart`, conflicting runtime processes, and HTTP
200 before native update reconciliation. Failed recovery reports systemd
status, recent journal lines, and the current runtime paths. It does not edit
service state or the launcher.

The fixture regression matrix lives in `tests/test-t3-recovery.sh`; it runs
against temporary runtime directories and mocked systemd/HTTP responses.

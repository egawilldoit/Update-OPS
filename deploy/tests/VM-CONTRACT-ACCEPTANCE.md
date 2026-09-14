# Disposable VM contract acceptance

Current result: **BLOCKED_REAL_VM_REQUIRED**. No disposable VM was designated;
no install, account change, service action, deployment, or reboot was executed.
The checked-in JSON records every real case as NOT_EXECUTED. Local Ubuntu/ARM64
metadata is context only; it is not production equivalence or acceptance.

## Preparation on a disposable VM only

Use a fresh Ubuntu 22.04 snapshot, preferably ARM64, with Python 3.10, systemd,
ACL tools, SQLite, util-linux, and the dependencies in docs/DEPENDENCIES.md.
The VM must contain no production data, credentials, or managed installations.
Do not use the production OpenClaw VM, even while drained. Keep the tunnel
disabled. Provide a fixture tool owner named ubuntu and synthetic configuration.

Place this harness checkout at `/opt/ega-acceptance`, readable by the tool owner.
Provide a release archive built from accepted SHA
`8d243a7a8cda8a1b53ffad91a934a8b00c812c8c`, including built frontend assets, at
`/opt/ega-acceptance/release.tar.gz`. Put a valid fixture configuration at
`/opt/ega-acceptance/fixture-config.json`, based on deploy/etc/config.example.json;
replace placeholders with disposable values and local runtime paths. Never copy
production configuration. Archive/config provisioning is a prerequisite, not a
completed acceptance result.

After independently identifying this machine as disposable, designate it locally:

```sh
sudo python3 - <<'PY'
import json, pathlib
p = pathlib.Path('/etc/ega-update/VM_ACCEPTANCE.json')
assert not pathlib.Path('/etc/ega-update/PRODUCTION').exists()
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(json.dumps({'disposable': True, 'machine_id': pathlib.Path('/etc/machine-id').read_text().strip()}))
p.chmod(0o600)
PY
cd /opt/ega-acceptance
sudo python3 deploy/tests/vm-contract-acceptance.py guard --report /var/tmp/ega-vm/guard.json
sudo python3 deploy/tests/vm-contract-acceptance.py install \
  --case complete_install --commit 8d243a7a8cda8a1b53ffad91a934a8b00c812c8c \
  --archive /opt/ega-acceptance/release.tar.gz \
  --config /opt/ega-acceptance/fixture-config.json \
  --report /var/tmp/ega-vm/complete_install.json
```

The machine-bound, root-owned designation is required before mutations. A
production marker always refuses execution. Do not bypass either guard.
Take a VM snapshot after successful fixture installation. Run without queued
jobs or interactive tool-owner login. This is a destructive acceptance fixture.

## Sequential OS cases

```sh
cd /opt/ega-acceptance
for case_name in users_acl_stale_manager api_identity csrf_unreadable \
  worker_liveness_restart user_bus_negative transient_launch_failure \
  unit_state_unknown sqlite_wal flock atomic_pointer canonical_readiness \
  mandatory_stage_failures; do
  sudo python3 deploy/tests/vm-contract-acceptance.py run \
    --case "$case_name" --report "/var/tmp/ega-vm/$case_name.json" || break
done
```

A nonzero exit stops the sequence. Investigate before resuming; do not count a
stopped case as passed. The stale-manager case restarts the manager before
adding shared membership, records its actual group vector, then provisions
named-user ACLs and performs a real typed transient round trip and stop proof.
The worker case temporarily runs the actual dispatcher through an acceptance
entrypoint, stops its actual ProbeWorker, and observes process failure, systemd
restart, boot reconciliation, and separate readiness/heartbeat evidence. Its
runtime drop-in is removed afterward. Failed restoration requires snapshot
recovery; never repair a fixture by touching production.

SQLite checks use generated data, two live connections plus both service
identities, a live WAL, the actual deployment backup CLI, and reopened integrity
checks. Lock tests use distinct processes and SIGKILL. Pointer tests use the
actual pointer primitive, concurrent reads, CAS refusal, restoration, and temp
cleanup. They do **not** establish power-loss durability.

`mandatory_stage_failures` injects each failure into the real canonical readiness
gate. It does not yet replay the entire upgrade under each injected failure.
That deployment-refusal replay, service-startup failure during deployment, and
all real reboot observations remain required before GREEN_REAL_VM. The 94-case
hermetic matrix remains the exhaustive state-machine proof. Do not relabel
canonical-gate evidence as complete deployment evidence.

## Real reboot checkpoints

Use a separate restored snapshot for each case. Provide a distinct valid target
release archive and its actual 40-character commit SHA; reusing the installed
SHA would not prove pointer-switch recovery. Set these two shell variables to
those fixture artifacts before running:

```sh
: "${TARGET_SHA:?actual target fixture commit required}"
: "${TARGET_ARCHIVE:?absolute target archive required}"
case_name=reboot_after_drain
# Repeat on fresh snapshots with reboot_after_migration and reboot_after_switch.
sudo env EGA_VM_CHECKOUT=/opt/ega-acceptance \
  EGA_VM_CHECKPOINT="$case_name" \
  EGA_VM_BEFORE_REPORT="/var/tmp/ega-vm/$case_name-before.json" \
  BASH_ENV=/opt/ega-acceptance/deploy/tests/vm-contract-reboot-hook.sh \
  bash /opt/ega-acceptance/deploy/scripts/upgrade.sh \
  --commit "$TARGET_SHA" --release-tarball "$TARGET_ARCHIVE"
```

The hook guards the machine before upgrade starts, records the selected facts,
and SIGSTOPs the deployment while its kernel lock is still held. It never
reboots automatically. Wait for the checkpoint message. In a second console
on that same disposable machine, verify BEFORE_REBOOT in the report, then:

```sh
sudo python3 /opt/ega-acceptance/deploy/tests/vm-contract-acceptance.py guard \
  --report /var/tmp/ega-vm/reboot-guard.json && sudo systemctl reboot
```

After boot, set the same case name and record:

```sh
case_name=reboot_after_drain
sudo python3 /opt/ega-acceptance/deploy/tests/vm-contract-acceptance.py after-reboot \
  --case "$case_name" --before "/var/tmp/ega-vm/$case_name-before.json" \
  --report "/var/tmp/ega-vm/$case_name-after.json"
```

Snapshot actions deliberately exit 2: observations require review, not an
automatic acceptance label. Require a changed boot ID; compare pointer, DB
version/schema/integrity, drain, service states, automatic readiness, and
manual-recovery requirement. Drain and unfinished deployment facts must remain
consistent with docs/RUNBOOK.md. No boot behavior may assume an unknown step
completed. Record any manual recovery separately before restoring the snapshot.
The hook itself remains unexecuted on a real VM.

## Evidence and defects

Each JSON contains commit_sha, environment, test_case, started_at, finished_at,
status, evidence, and failure_reason. Reports persist selected typed values,
not raw subprocess output, configuration, tokens, or credentials. Do not tee
installer output or attach raw journals. Review any additional evidence before
persistence. A case PASS_REAL_OS_CASE is not an aggregate GREEN_REAL_VM.

For an unexpected product failure, stop that case and record severity, root
cause, exact sanitized reproduction, expected/actual behavior, and affected
files. Add a RED regression before the smallest correction on a dedicated fix
branch. Harness errors must be identified as harness errors, never used to
hide a runtime defect. No product defect has been established by this blocked
run. All real OS and reboot claims remain outstanding.

To regenerate a truthful blocked report without mutating OS state:

```sh
python3 deploy/tests/vm-contract-acceptance.py blocked \
  --report deploy/tests/evidence/w7-vm-contract.json
# Expected exit: 2; expected status: BLOCKED_REAL_VM_REQUIRED.
```

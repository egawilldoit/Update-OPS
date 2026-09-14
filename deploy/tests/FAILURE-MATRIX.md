# Deployment failure / acceptance matrix (W6, D8+D9)

Status: acceptance-engineering artifact. The executable contract is
`backend/tests/test_deploy_failure_matrix.py` (run through
`backend/tests/deploy_harness.py`: real `install.sh`/`upgrade.sh` in a
tmp sandbox with PATH shims, real symlink/SQLite-path semantics). This
document is the human-readable state table plus the manual
disposable-VM acceptance procedure. Every cell marked "tested" below was
observed as process exit code + filesystem/symlink/service-shim/drain
state, not by grep.

No host is mutated by the test suite. The deployment lock serializes
real deployments; the tests hold a real kernel flock in the harness.

## 1. Stage model (one state machine)

| Stage | Name | Mutation | Failure policy |
| --- | --- | --- | --- |
| P0 | READ-ONLY PRECHECK | none | refuse before any maintenance/pointer mutation (exit 1/2) |
| P1 | DRAIN (admission stop) | drain file | abort; nothing else mutated |
| P2 | QUIESCENCE | none (bounded 120 s) | timeout = fail closed; drain kept; services untouched |
| P3 | STOP + PROVE STOPPED | services stopped | abort; drain kept; manual recovery if stop unprovable |
| P4 | HOST/RUNTIME MUTATION | secrets source, accounts, dirs, linger, ACLs | fail closed; drain kept |
| P5 | RELEASE PREPARATION | stage/validate/extract/venv/pip/MANIFEST/release-validate | fail closed; pointer unchanged |
| P6 | BACKUP | consistent SQLite backup | abort before migration; pointer unchanged |
| P7 | MIGRATION | DB schema | `MIGRATION_STARTED` set before the attempt; failure = services stopped, no old-code boot |
| P8 | POINTER SWITCH | `current` symlink | CAS + temp same-dir symlink + `os.replace` + post-verify |
| P9 | UNITS | unit copies, drop-ins, daemon-reload | post-switch policy (restore only if compat proven) |
| P10 | SERVICE START | services started | post-switch policy |
| P11 | CANONICAL READINESS | none | post-switch policy |
| P12 | SUCCESS | drain released only if created by us | `done: <commit>` |

Canonical readiness = `api_service_identity`, `api_security_boundary`,
`worker_process`, `probe_executor`, `owner_transient_execution`. A
401/403 alone is never ready; the shell consumes only the gate exit
code.

## 2. Existing deployment (upgrade) failure matrix — A1..A30

"pointer" is `/opt/ega-update/current`; "old services" are the units
active before maintenance. "compat" = `validate-release.py
--check-compat OLD NEW`. Every row is tested unless noted.

| # | Trigger (harness knob / fault) | drain | old svc | migration/DB | pointer | services | restore | final state / exit |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A1 | lock held (`lock_held`) | not created | running | not started | old | running | n/a | refuse before mutation; rc!=0 |
| A2 | invalid pointer: file/dir/escape (`current_invalid`) | not created | running | not started | untouched | running | none | refuse at P0; rc!=0 |
| A2b | config unparsable (`config_parse_fail`) | not created | running | not started | old | running | none | refuse at P0 (W6-1 fix); rc!=0 |
| A3 | drain create fails (`drain_create_fail`) | absent | running | not started | old | running | none | "cannot create drain"; rc!=0 |
| A4 | quiescence unprovable (`quiesce_fail`) | kept | running | not started | old | running | none | timeout; no host/release mutation; rc!=0 |
| A5 | host prep fails after drain (install: `provision_fail`; upgrade: unwritable `/etc` + validator fidelity gate) | kept | stopped | not started | old | upgrade restarts; install stays stopped | none | rc!=0 |
| A6 | archive staging fails (`fault_point=stage`) | kept | stopped | not started | old | as A5 | none | rc!=0 |
| A7 | archive validation fails (`archive_validate_fail`) | kept | stopped | not started | old | as A5 | none | rc!=0 |
| A8 | venv fails (`fault_point=venv`) | kept | stopped | not started | old | as A5 | none | rc!=0 |
| A8b | pip deps fail (`pip_fail`) | kept | stopped | not started | old | as A5 | none | rc!=0 |
| A9 | built frontend missing (`missing_frontend`) | kept | stopped | not started | old | as A5 | none | rc!=0 |
| A10 | release validation fails (`fault_point=validate`) | kept | stopped | not started | old | as A5 | none | rc!=0 |
| A11 | DB backup fails (`fault_point=backup`) | kept | stopped | not started | old | as A5 | none | rc!=0 |
| A12 | migration fails (`fault_point=migrate`) | kept | stopped | `MIGRATION_STARTED`, not committed | old | **not restarted** | none | manual recovery; rc!=0 |
| A13 | post-migration pre-switch fails (`fault_point=pre-switch`) | kept | stopped | committed (MIGRATED) | old | restarted iff compat proven | none (pointer unchanged) | "POINTER UNCHANGED; DATABASE NOT ROLLED BACK"; rc!=0 |
| A14 | switch preparation fails (`fault_point=pointer-switch-prep`) | kept | stopped | committed | old; temp cleaned | restarted (compat proven) | none | rc!=0 |
| A15 | CAS race: another actor moves `current` (`cas_race`) | kept | stopped | committed | **racing actor's target** | restarted (compat proven) | none | fail closed, never overwrite; rc!=0 |
| A16 | `os.replace` fails (primitive-level monkeypatch) | n/a | n/a | n/a | old byte-identical; temp cleaned | n/a | none | `current_replaced=false`; rc=5 (primitive) |
| A17 | post-switch verification fails (`switch_post_replace_fail`) | kept | stopped | committed | restored to old when compat proven | restarted | pointer only | "POINTER RESTORE COMPLETE"; rc!=0 |
| A17b | same + compat unknown/false | kept | stopped | committed | stays candidate | stopped | forbidden | manual recovery; rc!=0 |
| A18 | service start fails after switch (`start_fail_units`) | kept | stopped | committed | restored when compat proven | restoration attempted | pointer only | rc!=0 |
| A18b | same + compat unknown/false | kept | stopped | committed | stays candidate | stopped | forbidden | manual recovery; rc!=0 |
| A19 | `api_service_identity` fails | kept | stopped | committed | restored (compat proven) | restarted | pointer only | rc!=0 |
| A20 | `api_security_boundary` fails | kept | stopped | committed | restored | restarted | pointer only | rc!=0 |
| A21 | `worker_process` fails | kept | stopped | committed | restored | restarted | pointer only | rc!=0 |
| A22 | `probe_executor` fails | kept | stopped | committed | restored | restarted | pointer only | rc!=0 |
| A23 | `owner_transient_execution` fails | kept | stopped | committed | restored | restarted | pointer only | rc!=0 |
| A24 | full readiness fails, compat PROVEN | kept | stopped | committed; **DB not rolled back** | restored to old (same atomic primitive, CAS) | restarted | pointer only | explicit "DB … is NOT restored"; rc!=0 |
| A25 | full readiness fails, compat FALSE/UNKNOWN (`compat_fail`/`compat_error`) | kept | stopped | committed | stays candidate | stopped (last op is stop) | forbidden | manual recovery; rc!=0 |
| A26 | pointer restoration fails (`fault_point=restore`) | kept | stopped | committed | stays candidate | stopped | failed loudly | "RESTORE FAILED"; manual; rc!=0 |
| A27 | service restoration fails after a successful pointer restore | kept | stopped | committed | old | one/both units not active | pointer done | no success claim; manual reconcile; rc!=0 |
| A28 | success, drain created by us | removed after readiness | restarted | committed | new | active | n/a | rc=0; `done: <commit>` |
| A29 | success with pre-existing operator drain | kept | restarted | committed | new | active | n/a | rc=0; "pre-existing drain kept" |
| A30 | repeated invocation same commit after success | **not created** (W6-2 fix) | untouched | untouched | unchanged | untouched | n/a | fail-fast at P0; rc!=0 |

Notes:

* A5: `upgrade.sh` cannot fail the `secrets.env` write explicitly; the
  release validator's required-secrets section is the backstop (modelled
  by the harness fidelity gate). No switch/migration occurs.
* A9: the frontend is built before packaging; the deploy gate is the
  presence of `backend/app/static/index.html` in the staged release.
* A16 is primitive-level because a mid-`os.replace` failure cannot be
  injected through the CLI without a hook; the script-level analogue is
  A14 (preparation) and A17 (post-replace).
* **Divergence (documented, not a safety defect):** on pre-migration
  failures (A5-A11) `upgrade.sh` reconciles previously-active services
  ("restart what was running"), while `install.sh` leaves them stopped.
  Both keep the drain and leave the pointer/DB untouched. Candidate for
  V2 alignment; no automatic restart is a safe default.
* **V1 boundary:** a failure after `mkdir "$RELEASE_DIR"` but before the
  switch leaves a partial release dir; a same-commit retry refuses at the
  P0 release-dir precheck until the operator removes it deliberately.
  This is fail-closed and now fail-fast.

## 3. Fresh install matrix (B)

| State | Drain | current | Services | Notes |
| --- | --- | --- | --- | --- |
| no previous current | none | absent → created | started | inspect never run; switch carries no `--previous` |
| no previous DB | none | created | started | no backup step; initial `db migrate` runs |
| no existing services | none | created | started | `WAS_*`=0; no restoration bookkeeping |
| user/group bootstrap | none | created | started | `useradd` only when account absent (idempotent) |
| runtime path prep + owner ACL | none | created | started | `owner_env provision` before venv |
| release validation | none | created | started | validator runs before migrate |
| initial migration | none | created | started | `MIGRATION_STARTED`/`MIGRATED` set |
| first atomic current creation | none | created by `os.replace` | started | no `--previous`; idempotent verification |
| unit installation | none | created | started | port drop-in + user-bus drop-in + 3 system units |
| service startup | none | created | active | api then worker |
| canonical readiness | none | created | active | `READINESS_OK` required for success |
| success | none created | created | active | `done: <commit>` |

Failure classes (fresh):

| Failure | current | restart/rollback | drain | Result |
| --- | --- | --- | --- | --- |
| before first pointer (stage/validate/migrate) | absent | none (no previous release — never pretend a rollback) | none (no admission existed) | rc!=0, explicit fail-closed message |
| after migration, before first pointer | absent | "compatibility NOT proven" → no service start | none | rc!=0, manual recovery |
| after first pointer, before readiness | candidate | `SWITCH_DONE` + no previous → pointer NOT restored, services stopped | none | rc!=0, manual recovery |
| readiness stage failure on first install | candidate | same as above; stage named in report/stderr | none | rc!=0 |

## 4. Migration compatibility matrix (E)

| Case | DB state | compat | Pointer restore | Service restoration | Manual recovery |
| --- | --- | --- | --- | --- | --- |
| A migration not started | unchanged | not consulted | none (pointer unchanged) | only restart of previously-active units | no (unless stop/start unprovable) |
| B migration failed | unproven | not consulted | **never** | none (old code must not boot unproven) | yes |
| C migration committed | migrated | TRUE | permitted (atomic, CAS) | yes | only if restoration fails |
| D migration committed | migrated | FALSE | forbidden | none (services stopped) | yes |
| E migration committed | migrated | unavailable/unknown (`compat_error`, missing validator) | forbidden | none | yes |
| F pointer restored | migrated | TRUE | done | fails → no success, drain kept | yes |

Message vocabulary is asserted distinct: **POINTER RESTORE** (console
symlink only), **SERVICE RESTORATION** (restart of previously-active
units), **DATABASE NOT ROLLED BACK / the DB migrated by … is NOT
restored**. Automatic recovery never claims "rollback successful".

## 5. Atomic pointer semantics

* Atomic visibility: `current` is committed by `os.replace` of a
  same-directory temporary symlink (rename(2)). A concurrent reader only
  ever observes the complete old target or the complete new target —
  never a missing `current` and never a partial target (real-FS reader
  loop, 20 bidirectional switches + 50-switch primitive test).
* CRASH DURABILITY: after the rename the primitive fsyncs the pointer's
  parent directory **best effort** (`OSError` swallowed). The V1 promise
  is atomic visibility on local filesystems. It is NOT a promise that a
  power loss immediately after `switch` cannot lose the new pointer
  entry; the durability behaviour depends on the filesystem. Test D03
  proves both the success-path fsync call and that a simulated fsync
  failure neither fails the switch nor produces a false success claim.
  Do not upgrade this promise without a durability test on the target FS.
* Rejections (all leave `current` untouched, exit 4): target outside the
  releases root, relative target, unexpected current type (file/dir),
  escaping current link, candidate symlink, candidate missing/plain
  file, group/other-writable candidate. Failed preparation cleans the
  temp link (exit 5, `current_replaced=false`). Already-current is an
  idempotent no-op (inode preserved).
* `current` is never switched with `ln -sfn` (structural test).

## 6. Unit / drop-in artifact boundary after restore (F)

Automatic recovery reverts the POINTER only. The unit files copied from
the candidate remain installed; the port drop-in is rendered from config
(not from the release). Test F1 proves the copied unit still embeds the
candidate commit while `current` resolves to the previous release, and
`docs/RUNBOOK.md §7` documents the V1 boundary. Test F2 proves every
shipped unit resolves code through `/opt/ega-update/current` and never
through a versioned `/opt/ega-update/releases/<commit>` path. Restart
after a pointer restore is therefore allowed **only** under the
documented template-compatible V1 assumption; this is never a full
deployment rollback.

## 7. Lock / race

* Real kernel flock on `/opt/ega-update/deploy.lock`, acquired before
  any maintenance mutation, fail-fast, released by the kernel on process
  exit. No PID/stale metadata: the lock file stays empty and a second
  acquisition after exit needs no cleanup (test C02, real flock).
* Pointer CAS: `inspect` captures the exact previous target; the switch
  carries it as `--previous`. If another actor moves `current` in
  between, the switch fails with `stage=cas` and never overwrites the
  racing target (test A15 end-to-end, primitive CAS test).
* Lock contention against a live harness-held flock is asserted for both
  scripts (A1): no drain, no stop, no release mutation.

## 8. Fault-hook inertness

`ega_maybe_fail` (shell) and the primitive's `EGA_DEPLOY_FAULT_POINT`
check are inert unless the variable equals the exact point. Tested:
unset, unrelated value, and exact value (behavioral, sourced shell);
end-to-end upgrade with an unrelated point succeeds; committed defaults
cannot enable a fault (config example, units, and both scripts never set
the variable). Point names used by the matrix: `stage`, `validate`,
`venv`, `pre-switch`, `pointer-switch-prep`, `post-switch`, `units`,
`api-start`, `worker-start`, `restore`, `migrate`, `backup`.

## 9. Success acceptance (H)

A success is only accepted with all of: lock held during the mutation
window (observed at drain creation), drain → quiescence → proven stop →
venv order, backup before migration before switch, exactly one switch
through the shared primitive with CAS, intended service restart, all
readiness stages green, exact deployed release resolution, no fault
injected, and the correct drain policy. "Exit 0" alone is not evidence.

## 10. Reboot-state matrix (spec; `reboot-check` phase)

At boot, systemd starts enabled units against whatever `current`
resolves to. No unit migrates, backs up, or switches anything (test G1
inspections; service startup validates the schema read-only and refuses
pending/newer schemas).

| State after reboot | pointer | DB | drain | automatic boot action allowed | manual recovery |
| --- | --- | --- | --- | --- | --- |
| after drain, before migration | old | untouched | present | start old units; API refuses admission while drain present | remove drain only after reconcile |
| during/after failed migration | old | unproven/partial | present | start old units; schema validation refuses pending/newer schema (fail closed) | yes — restore backup / re-run controlled migration |
| after migration, before pointer switch | old | migrated (compat proven) | present | start old units (compat gate already proven) | remove drain after verify |
| after pointer switch, before service start | new | migrated | present | start new units (candidate code complete) | remove drain after readiness |
| readiness failure post-switch | old if restored, else new | migrated | present | start pointer's units; readiness gate still governs jobs | reconcile per RUNBOOK |
| failed pointer restore | candidate/unknown | migrated | present | start pointer's units only; never assume the restore succeeded | yes — inspect `current` first |

`reboot-check <state>` asserts: drain present for every failure state,
and no `ExecStart`/`ExecStartPre` contains a deployment/migration
command.

## 11. Disposable Ubuntu 22.04 VM acceptance harness

`deploy/tests/vm-acceptance-failure-matrix.sh` — NEVER run
automatically, never against production.

Exact invocations:

```bash
# read-only host acceptance (identities, ACLs, SQLite modes, pointer
# verify, kernel flock contention, unit content, user-manager reach)
sudo EGA_VM_ACCEPTANCE=1 EGA_VM_DISPOSABLE=1 \
  EGA_OPERATOR_CHECKOUT=/home/ubuntu/Update-OPS \
  deploy/tests/vm-acceptance-failure-matrix.sh verify

# real atomic pointer + CAS + concurrent reader on SCRATCH releases
sudo EGA_VM_ACCEPTANCE=1 EGA_VM_DISPOSABLE=1 \
  EGA_OPERATOR_CHECKOUT=/home/ubuntu/Update-OPS \
  deploy/tests/vm-acceptance-failure-matrix.sh pointer

# service stop/prove/start/prove + readiness window (destructive)
sudo EGA_VM_ACCEPTANCE=1 EGA_VM_DISPOSABLE=1 EGA_VM_DESTRUCTIVE=1 \
  EGA_VM_SERVICE_MUTATION=1 \
  deploy/tests/vm-acceptance-failure-matrix.sh services

# after inducing a failure state + reboot (see table §10)
sudo EGA_VM_ACCEPTANCE=1 EGA_VM_DISPOSABLE=1 \
  deploy/tests/vm-acceptance-failure-matrix.sh reboot-check <state>
```

Guards (all enforced): `EGA_VM_ACCEPTANCE=1` (otherwise NOT RUN, exit
0), `EGA_VM_DISPOSABLE=1`, euid 0, systemd host, no
`/etc/ega-update/PRODUCTION` marker, installed deployment present;
destructive phases additionally require `EGA_VM_DESTRUCTIVE=1` and (for
services) `EGA_VM_SERVICE_MUTATION=1`.

To induce a failure state on the disposable VM (never production), use
the inert-by-default fault hook with a real upgrade and then reboot:

```bash
sudo EGA_DEPLOY_FAULT_POINT=migrate \
  deploy/scripts/upgrade.sh --commit <40hex> --release-tarball <tar>
sudo systemctl reboot
# after reboot:
sudo EGA_VM_ACCEPTANCE=1 EGA_VM_DISPOSABLE=1 \
  deploy/tests/vm-acceptance-failure-matrix.sh reboot-check after-failed-migration
```

Valid states: `drain-before-migration`, `after-failed-migration`,
`post-migration-pre-switch`, `post-switch-pre-start`,
`readiness-failure`, `failed-pointer-restore`.

## 12. Remaining untested real-OS boundaries

* Actual power-loss durability of the pointer entry (see §5); only
  best-effort directory fsync is implemented and tested.
* A real reboot was not performed in this worktree environment; the
  reboot matrix is spec + post-boot `reboot-check` assertions, not an
  executed reboot.
* Real `systemd --user` transient job units, linger, and the
  already-running `user@UID` supplementary-group/ACL interaction: covered
  by `deploy/tests/vm-acceptance-readiness.sh`; not re-executed here.
* Real `render-worker-bus-env.sh` UID semantics and real chown/chmod
  modes are shimmed in the hermetic matrix (real modes are asserted by
  the `verify` VM phase).
* Real SQLite backup API/WAL contents and the real archive validator are
  exercised by their own suites; the harness models them with a fidelity
  gate (secrets section).
* The CAS race is deterministically injected at inspect time, not a
  true nanosecond race window.
* Cloudflared tunnel start/stop is out of scope (tunnel phase is
  explicit opt-in).
* flock semantics were proven on a local filesystem only; network or
  overlay filesystems are not covered.

## 13. Defects found and fixed in W6

* **W6-1 (HIGH, fixed):** `install.sh` + `upgrade.sh` resolved
  `state_dir`/`db_path`/`backup_dir` inside function command
  substitutions; `cfg_value`'s fail-closed `exit 1` ended only the
  subshell, so an existing-but-unparsable config let the install
  continue with EMPTY paths and (observed) `rc=0`, while a root host
  would have created `/logs`, `/conflicting-automation`, and reported
  success. Fix: `|| exit 1` on each assignment; the no-drain message is
  now truthful when no drain exists. RED: `test_a02b...[install.sh]`
  (rc=0). GREY: the upgrade variant was latent — it aborted only later
  (root: after touching `/drain`), so its test passed on base; fixed
  identically.
* **W6-2 (MEDIUM, availability/ordering, fixed):** a repeated invocation
  after success ran the full maintenance prologue before discovering the
  release dir exists — `install.sh` stopped services and left them
  stopped; `upgrade.sh` drained, stopped, backed up, then failed and
  restarted. Fix: release-dir existence is now a read-only precheck
  before the maintenance boundary in both scripts (fail fast, no drain,
  no service stop). RED: `test_a30...` for both scripts.
* Observations (not changed): the A5-A11 restart divergence between
  install and upgrade (§2); the partial-release-dir retry boundary
  (§2); `upgrade.sh`'s unchecked `: > secrets.env` (validator backstop).

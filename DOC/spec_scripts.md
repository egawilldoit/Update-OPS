# Update scripts specification

Date: 2026-09-08. Target: EGA Update Console V1.
Status: implementation specification, not a tested replacement updater.

## 1. Purpose and scope

Implement reliable per-tool update scripts for the actual Ubuntu VM. The dashboard worker calls these scripts and owns durable jobs, authentication, and crash recovery as defined in [SPEC-UPDATE SYSTEM.md](<SPEC-UPDATE SYSTEM.md>). Scripts own installation checks, update execution, and tool verification.

The required V1 tools remain Codex, OpenCode, Hermes, and T3 nightly. Claude Code is inventoried and receives an optional adapter, disabled until its installation ownership is resolved. This document does not silently expand the four-tool PRD or authorize a Claude migration.

The supplied agent-stack-updater(1).sh is reference code. Do not expose it unchanged as a dashboard backend. Its Bash syntax passes bash -n; that does not verify execution or safety. No update was run for this review.

## 2. Evidence from the attachments

Sources reviewed: Pasted text(20260908-095544).txt, Pasted markdown (2).md, and agent-stack-updater(1).sh. Audit timestamp: 2026-09-08 09:54:41 UTC. Treat these as historical observations and recheck before mutation.

| Component | Observed installation | Observed version | Decision |
| --- | --- | --- | --- |
| VM | Ubuntu 22.04.5, aarch64, owner ubuntu UID 1001 | Node 24.18.0 | Preserve architecture and runtime |
| Codex | /home/ubuntu/.local/bin/codex links into /home/ubuntu/.codex/packages/standalone/releases/ | 0.153.4 | Native standalone updater; inspect daemon separately |
| OpenCode | /home/ubuntu/.opencode/bin/opencode | 1.18.29 | Explicit curl-method upgrade |
| Hermes | Wrapper /home/ubuntu/.local/bin/hermes; repo /home/ubuntu/.hermes/hermes-agent | 0.21.1; commit 866332bfb52c46e543143b2620a9aeee8bce9c77 | Native updater, branch main |
| T3 nightly | Not established by audit | Unknown | Inventory gate before update |
| Claude | /usr/local/bin/claude resolves into root-owned /usr/local/lib/node_modules/@anthropic-ai/claude-code/ | 2.1.167 | Read-only until ownership/method resolved |

Additional findings:

- Root filesystem had 3.4 GiB free and was 96% used. Recalculate space per job.
- Hermes home was 4.8 GiB, OpenCode data 13 GiB, and Codex home 3.7 GiB. Do not archive these wholesale by default.
- OpenCode has a second npm installation under NVM. Codex has another executable at /usr/local/bin/codex. Preserve both; configure the intended executable explicitly.
- Hermes reported an earlier incomplete gateway restart. A version check alone is insufficient.
- Hermes gateway and serve units were listed as system units. Enabled is not proof of running. Inspect User, ExecStart, active state, and restart authority.
- Codex processes were running. Their presence does not prove daemon health or whether a task is idle.
- The audit did not establish T3's service, flags, port, state path, or running version. The accompanying prose cannot substitute for that evidence.

## 3. Required corrections to the supplied script

| Finding | Required correction |
| --- | --- |
| Doctor/service errors become warnings followed by success | Mandatory checks fail the result; distinguish install_failed from health_failed |
| update_all invokes functions inside || expressions | Bash errexit can be suppressed throughout those functions. Explicitly check every mutating command; remove update-all from V1 |
| disk_guard 1024/1536 bypasses MIN_FREE_MB=3072 | Enforce max(configured floor, operation estimate) on every affected filesystem |
| Missing flock only warns | Block mutation if locking is unavailable |
| All commands acquire one lock | Read-only cached status must remain available during updates; mutation alone owns the exclusive execution lock |
| first_line hides command failures and may capture a warning | Preserve exit status and parse full bounded output into a version field |
| PATH winner used dynamically | Validate configured absolute paths and fingerprints before mutation |
| Hermes --force permits dirty checkout | Remove this bypass; dirty/unreadable Git state blocks |
| Hermes quick snapshot assumed without reading configuration | Inspect effective backup mode, scope, and receipt; do not claim full recovery |
| systemctl list-unit-files exit code treated as existence | Inspect LoadState and actual unit configuration in correct system/user scope |
| Nonempty Codex daemon output treated as presence | Parse supported structured output and distinguish absent, healthy, stale, and failed probe |
| T3 state file alone establishes a service | Correlate state, unit, executable, process, and endpoint |
| T3 unknown after-version can pass | Required version evidence missing means verification failure |
| check_t3 runs npx with a new target | This can download and execute code. Discovery uses registry metadata and existing installed files only |
| Process detection only warns | Known busy blocks; unknown requires recorded owner acknowledgment |
| Unbounded tee logs and second-based filenames | Redact before persistence, use job UUIDs, cap logs, and preserve write failures |
| Generic registry ping gates standalone tools | Probe only each adapter's actual update sources; npm outage must not block unrelated native updates |
| Legacy root-owned Claude attempted anyway | Block before mutation; no automatic sudo, uninstall, or migration |

## 4. Script layout and interface

Deliver these executable entry points, backed by one shared implementation rather than duplicated logic:

- scripts/update-codex.sh
- scripts/update-opencode.sh
- scripts/update-hermes.sh
- scripts/update-t3.sh
- scripts/update-claude.sh, optional and disabled by default
- scripts/agent-update, dispatcher

Use thin Bash wrappers around the Python adapter implementation in [SPEC-UPDATE SYSTEM.md](<SPEC-UPDATE SYSTEM.md>). Run subprocesses with fixed argument arrays and shell=False. Never use eval or bash -lc with generated command text.

Proposed interface, to be implemented:

```text
scripts/agent-update inspect codex
scripts/agent-update plan opencode
scripts/agent-update apply hermes --plan-id <id> --job-id <uuid>
scripts/agent-update verify t3
```

inspect and plan may write console observations but may not install/download executable packages, restart services, or modify tool configuration. Registry metadata retrieval is allowed. Explicit native checks that fetch Git metadata must be labeled as such.

apply requires a trusted server-generated plan from the protected job store. IDs are not paths. Reject stale plans, changed installation fingerprints, unvalidated targets, unknown flags, and concurrent jobs. No all, force, or generic command argument.

Every operation returns a JSON result with schema_version, tool, action, job_id, before_version, requested_target, target_mode, after_version, state, checks, error_code, and timestamps. Stream redacted log events separately. Exit codes: 0 verified success or already-current, 2 invalid request, 3 blocked, 4 installation failure, 5 verification failure, 6 interrupted/recovery required. already-current is not evidence of a tested upgrade.

Use the authenticated worker's existing environment with a configured PATH for child dependencies. Validate Node at /home/ubuntu/.nvm/versions/node/v24.18.0/bin/node and matching npm/npx. Do not source interactive shell profiles or repurpose HOME/CODEX_HOME. Preserve genuine tool state-home settings only when inventoried.

Lock ownership belongs to the job runner. Wrappers must not reacquire and deadlock against the same lock. Direct CLI invocation goes through that same runner. Preserve the job across browser or API restart as specified in [SPEC-UPDATE SYSTEM.md](<SPEC-UPDATE SYSTEM.md>).

## 5. Codex procedure

The audit proves standalone layout and lists update and doctor commands. Prefer the native update path over reinstalling through npm.

Inspection and capability probes:

```bash
/home/ubuntu/.local/bin/codex --version
/home/ubuntu/.local/bin/codex update --help
/home/ubuntu/.local/bin/codex app-server daemon --help
readlink -f /home/ubuntu/.local/bin/codex
```

Mutation after preflight:

```bash
/home/ubuntu/.local/bin/codex update
```

Only use supported noninteractive options confirmed by local help. This command's audited semantics are latest, not an exact target. Record target_mode=native_latest, observed candidate, and actual installed version. Do not invent a version flag or equate npm metadata with the standalone channel.

When local help confirms the daemon interface, use:

```bash
/home/ubuntu/.local/bin/codex app-server daemon version
/home/ubuntu/.local/bin/codex app-server daemon restart
```

Restart only a confirmed existing daemon, after activity checks, if required for the new CLI. Do not start a previously absent daemon, re-pair remote control, or delete auth/session data. Execute the updater outside the Codex process tree being restarted.

Verify CLI startup/version, supported diagnostics, existing daemon readiness and version agreement, and configured T3 compatibility checks. A daemon probe error is not absence. Retain previous standalone release metadata without promising database rollback.

Sources: local audit; [official daemon reference](https://github.com/openai/codex/blob/main/codex-rs/app-server-daemon/README.md).

## 6. OpenCode procedure

Use the configured standalone binary, not the secondary NVM npm package.

```bash
/home/ubuntu/.opencode/bin/opencode --version
/home/ubuntu/.opencode/bin/opencode upgrade --help
```

Resolve an official release target as metadata, validate its version and ARM64 availability, and store it in the plan. Mutation template, with TARGET supplied by validated server state:

```bash
/home/ubuntu/.opencode/bin/opencode upgrade "$TARGET" --method curl
```

The audit confirms the positional target and --method curl. Verify exact installed version and CLI startup. Record whether any configured server still needs a restart; only restart an inventoried service with preserved flags. Unknown running processes require manual handling.

Preserve configuration and session data. Never delete opencode.db, WAL/SHM, caches, plugins, or the alternate npm installation to make verification pass. Do not back up the full 13 GiB state directory merely to replace a binary. If a verification/startup path can migrate state, require a consistent backup for that affected database or block that step.

Source: [OpenCode CLI](https://opencode.ai/docs/cli/).

## 7. Hermes procedure

Verify the wrapper's actual repo/venv, configured main branch, expected remote, and clean Git state including untracked files. A Git error blocks rather than implying clean.

```bash
/home/ubuntu/.local/bin/hermes update --help
git -C /home/ubuntu/.hermes/hermes-agent status --porcelain=v1
git -C /home/ubuntu/.hermes/hermes-agent rev-parse HEAD
/home/ubuntu/.local/bin/hermes update --plan
/home/ubuntu/.local/bin/hermes update --check
```

After capability and backup checks:

```bash
/home/ubuntu/.local/bin/hermes update --yes
```

Use --yes only when supported locally. Inspect effective backup settings first. A full backup request uses the supported --backup option only when sufficient storage exists. Quick mode must be displayed as limited state protection, not full rollback. Never silently downgrade an existing full-backup policy because disk is low.

Use the native updater's restart handling and collect its receipt where available. Verify every inventoried gateway/serve process with the correct supervisor, not just two hardcoded names. On this VM, system-scoped units require confirmed restart authority. If ubuntu cannot perform a required restart, block before mutation and configure a narrowly scoped service action during deployment. Never prompt for sudo mid-job.

Record actual final commit, diagnostics, restart outcome, and running-version evidence. Native updater exit zero alone is insufficient. Do not automatically stash, reset, clean, change branch, or discard local work.

Sources: [Hermes update reference](https://hermes-agent.nousresearch.com/docs/reference/cli-commands), [backup and update behavior](https://hermes-agent.nousresearch.com/docs/getting-started/updating).

## 8. T3 nightly procedure

First identify the live installation. Inspect system and user units, executable provenance, configured state home, working directory, and startup options. Sensitive arguments are retained privately, never printed into audit logs. Candidate paths such as ~/.t3/runtime/service-state.json are hints, not proof.

For an official managed user service, corroborate the unit and state before proceeding. Inspect:

```bash
systemctl --user show t3code.service -p LoadState -p ActiveState -p SubState -p MainPID
```

Resolve nightly metadata with the configured NVM npm:

```bash
/home/ubuntu/.nvm/versions/node/v24.18.0/bin/npm view t3@nightly version --json
```

Parse exactly one valid version using a SemVer library. Reject malformed metadata, unsupported runtime/architecture, and unintended downgrade. Record the exact package version. Installing a newly resolved CLI belongs to apply, never inspect or plan.

Official service mutation template:

```bash
/home/ubuntu/.nvm/versions/node/v24.18.0/bin/npx --yes "t3@$TARGET" service update
```

Verify using supported local/installed status interfaces, the actual running version, unit readiness, and the configured endpoint. Metadata in a state file alone is insufficient. A native rollback is recorded as an unsuccessful requested update with the restored version and health; it is not success.

If T3 is a foreground/custom server, block the generic service update. Record its exact launch recipe and define a separate reviewed migration or custom-service adapter. Never kill it with pkill -f or run service install implicitly. Preserve host, port, Tailscale flags, data home, and authentication.

Sources: [official background service](https://github.com/pingdotgg/t3code/blob/main/docs/user/background-service.md), [T3 updating](https://github.com/pingdotgg/t3code/blob/main/docs/user/updating.md). Nightly is the user's requested channel; current installed channel remains unverified.

## 9. Optional Claude procedure

Current audit evidence means BLOCKED_INSTALL_OWNERSHIP. Inspect version, executable ownership, and supported diagnostics without requesting an update. Startup itself may perform background checks, so account for effective auto-update settings.

After an explicitly reviewed migration or ownership resolution, re-inventory the active executable. Its supported manual command is:

```text
<verified-absolute-claude-path> update
```

Verify resulting version and required diagnostics. Do not run sudo npm install, automatically migrate-installer, or remove the root-owned copy. Do not treat the audit's registry version as the current target. This optional adapter does not block four-tool V1 acceptance.

Source: [Claude setup and update documentation](https://code.claude.com/docs/en/setup).

## 10. Shared preflight and recovery

Required space is max(3 GiB configured default floor, estimated staging + required backup + 1 GiB reserve), measured on every affected filesystem. This stricter script default supersedes the earlier 2 GiB example in [SPEC-UPDATE SYSTEM.md](<SPEC-UPDATE SYSTEM.md>). Include package extraction, cache, DB/WAL, and native snapshots; database extensions alone do not prove backup size. Unknown estimates block until conservatively configured.

No blind full-home archives. Every adapter declares backup scope, exclusions, consistency method, required bytes, and recovery limitations. No consistent backup available for a required state migration means blocked. Tool-native rollback is allowed to complete; the console does not add automatic rollback.

Known busy work blocks. Unknown activity requires plan-specific acknowledgment. Avoid model requests and billable probes. If a mandatory health probe is unavailable, report verification incomplete instead of green.

Preserve explicit exit codes regardless of caller conditionals. Cap command duration, redact secrets before logs, and handle failed logging/storage. Timeout recovery must inspect native updater descendants and delegated services; killing only the shell is not proof mutation stopped. Leave recovery_required until reconciled. Do not retry automatically.

## 11. Implementation checklist and acceptance

- [ ] SC-01: Re-inventory all four required tools; record T3 launch method and Hermes system-unit restart authority.
- [ ] SC-02: Implement shared executor, protected plans, unique job IDs, explicit exit codes, and redacted bounded logs.
- [ ] SC-03: Fail every mocked installer and mandatory health probe; no result is incorrectly successful, including conditional shell callers.
- [ ] SC-04: Two simultaneous apply requests produce one updater; status remains readable; missing lock support blocks mutation.
- [ ] SC-05: A larger configured disk floor is honored by every adapter. Full-backup settings cannot be silently bypassed.
- [ ] SC-06: Alternate Codex/OpenCode binaries remain untouched; versions are read from configured paths under a noninteractive systemd environment.
- [ ] SC-07: Codex daemon absence, probe failure, stale version, restart failure, and active work are distinguished; pairing data is unchanged.
- [ ] SC-08: Hermes dirty checkout, unreadable Git state, failed backup, unavailable restart authority, and stale gateway each prevent success.
- [ ] SC-09: T3 inspect/plan executes no downloaded CLI. Unverified/custom service, malformed nightly version, absent final version, or native rollback cannot pass as a successful upgrade.
- [ ] SC-10: Optional root-owned Claude is blocked without elevation or migration. Shared runtimes and other tools are untouched.
- [ ] SC-11: Browser/API restart preserves the job. Runner crash/timeout leaves no duplicate update and requires reconciliation when native child state is unknown.
- [ ] SC-12: Complete one genuine supported upgrade for each required tool and record before/after version, exit code, mandatory checks, and redacted logs. Already-current runs prove idempotent behavior only.

Deliver these scripts and adapters, fixture-based tests, sanitized inventory, capability evidence, recovery instructions, and an SC-01 through SC-12 evidence table. VM updates and executable script implementation have not been performed by creating this document.

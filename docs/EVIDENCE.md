# Update-OPS V1 — UI/Deploy Evidence Matrix (Implementation Phase)

Branch: `feat/v1-implementation`. Baseline: `2e0f898` plus uncommitted
UI/DEPLOY corrective-pass changes (DO NOT commit per tasking).
Status convention: `implemented-static` means code/config written and
source-readable, with NO runtime execution. Every runtime result below is
`NOT EXECUTED — IMPLEMENTATION PHASE`. There is no `PASS` anywhere runtime
is needed; nothing here is `verified`/`accepted`/`complete`.

Environment: TBD (deploy VM identity, OS, Python/Node versions, and
`/opt/ega-update` + `/etc/ega-update` + `/var/lib/ega-update` state to be
recorded at runtime-gate time). Runtime gates are listed at the end.

Ownership: this file covers the UI/DEPLOY contributor scope only —
`frontend/**`, `scripts/*`, `systemd/**`, `deploy/**`, `docs/RUNBOOK.md`,
plus created `backend/app/retention.py`,
`deploy/etc/validate-release.py`, `backend/tests/test_ui_deploy.py`.
Backend core/API/worker/adapters rows (R01–R11, R13–R19, R22–R28, R30–R31,
R34) belong to the main/agent contributors and are referenced as
`out-of-scope (main agent)` where they gate our interfaces.

Regression test artifact (all): `backend/tests/test_ui_deploy.py`
(write-only; behavioral where possible; frontend behavior is a documented
manual checklist below — no jsdom available). Tested commit for every row:
`2e0f898` + uncommitted `feat/v1-implementation` UI/DEPLOY pass.

## AC rows (acceptance criteria, UI/DEPLOY owned)

| ID | Criterion (ledger) | Implementation status | Code paths | Regression test artifact | Tested commit | Environment | Result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| AC-01 | Shell wrappers propagate CLI exits bit-identically; no `if !` status-clobber bug (R12) | implemented-static | `scripts/agent-update`, `scripts/update-{codex,hermes,opencode,t3,claude}.sh`, `scripts/common.sh` (`common_release_root`, `common_release_python`, `exec … backend.app.cli`) | `test_ui_deploy.py::test_shell_wrappers_thin_exec` (banned `if !` + heredoc-python absent, `exec … venv/bin/python -m backend.app.cli` present, explicit `--plan-id/--job-id` mapping) | 2e0f898+new (uncommitted) | TBD | NOT EXECUTED — IMPLEMENTATION PHASE |
| AC-02 | Wrappers map to frozen CLI verbs; `apply` = reserve + observe canonical dispatch, never runs runner (R12/R13) | implemented-static | same scripts (`inspect\|plan\|apply\|verify → cli <verb> --tool …`); runner argv documented only in `systemd/*` | `test_shell_wrappers_thin_exec` (verb allow-list, `--tool` pinned per wrapper, no `runner` spawn in scripts) | 2e0f898+new | TBD | NOT EXECUTED — IMPLEMENTATION PHASE |
| AC-03 | Log drain until terminal + cursor ≥ final_log_seq + !has_more (R20) | implemented-static | `frontend/src/app.tsx` (`drained()`), `frontend/src/api/client.ts` (`final_log_seq`), `frontend/src/views/JobDetail.tsx` | manual checklist MC-01 (no jsdom); `test_frontend_source_markers` asserts drain predicate + `final_log_seq` plumbing present | 2e0f898+new | TBD | NOT EXECUTED — IMPLEMENTATION PHASE |
| AC-04 | Functional Load More; reopen resumes 0→drain; pending-tail indicator (R20) | implemented-static | `frontend/src/components/LogViewer.tsx` (Load More button, gap/pending-tail), `JobDetail.tsx` (cursor/final display), `app.tsx` (`handleLoadMore`, reopen reset) | manual checklist MC-02/MC-03; `test_frontend_source_markers` | 2e0f898+new | TBD | NOT EXECUTED — IMPLEMENTATION PHASE |
| AC-05 | 15s AbortController timeouts; 429 honors Retry-After + exp backoff max 30s + reconnect state (R21) | implemented-static | `client.ts` (`REQUEST_TIMEOUT_MS`, `parseRetryAfterMs`, `backoffMs`, `retryAfterMs` on `ApiError`), `app.tsx` (per-bucket backoff, reconnect banner) | manual checklist MC-04; `test_frontend_source_markers` (timeout/backoff/Retry-After markers) | 2e0f898+new | TBD | NOT EXECUTED — IMPLEMENTATION PHASE |
| AC-06 | Plan generation IDs per tool; stale responses ignored (R21) | implemented-static | `app.tsx` (`planGen` ref per tool) | manual checklist MC-05; `test_frontend_source_markers` | 2e0f898+new | TBD | NOT EXECUTED — IMPLEMENTATION PHASE |
| AC-07 | Overview polls `GET /jobs?active=true` globally every 5s for gating (R21) | implemented-static | `client.ts` (`listActiveJobs`), `app.tsx` (`loadActive`, 5s interval, gating from `activeJobs`) | manual checklist MC-06; `test_frontend_source_markers` | 2e0f898+new | TBD | NOT EXECUTED — IMPLEMENTATION PHASE |
| AC-08 | Health stale → stale badge styling, never green + text (R21) | implemented-static | `frontend/src/components/HealthPanel.tsx` (stale forces `stale` badge + `health-stale` class), `ToolCard.tsx` (unchanged, already stale-maps) | manual checklist MC-07; `test_frontend_source_markers` | 2e0f898+new | TBD | NOT EXECUTED — IMPLEMENTATION PHASE |
| AC-09 | Persist selected job route; restore after recovery; serialize per-job polls (R21) | implemented-static | `app.tsx` (localStorage `ega-update:selected-job`, `jobInflight` serialize guard) | manual checklist MC-08/MC-09; `test_frontend_source_markers` | 2e0f898+new | TBD | NOT EXECUTED — IMPLEMENTATION PHASE |
| AC-10 | Retention policy + tombstones + viewer gap note (R29) | implemented-static | `backend/app/retention.py` (`run_retention`), `LogViewer.tsx` (`showGap`), `docs/RUNBOOK.md` (retention ops) | `test_retention_*` (30d/cap/90d/latest-two/expired-plans/receipts/protections/tombstones); manual MC-10 for gap note | 2e0f898+new | TBD | NOT EXECUTED — IMPLEMENTATION PHASE |
| AC-11 | Deploy CWD+venv+config pinning; commit + MANIFEST gates; config-parsed paths (R32) | implemented-static | `deploy/scripts/install.sh`, `deploy/scripts/upgrade.sh` (`cd $RELEASE_DIR`, `$RELEASE_DIR/venv/bin/python`, `EGA_CONFIG_FILE` exported, 40-hex commit, `MANIFEST` stage+verify, `cfg_value` python -c JSON) | `test_install_upgrade_ordering` (cd/venv/EGA_CONFIG_FILE/commit/MANIFEST/cfg_value markers; no heredoc-python) | 2e0f898+new | TBD | NOT EXECUTED — IMPLEMENTATION PHASE |
| AC-12 | 11-step maintenance protocol + compat-gated rollback (R33) | implemented-static | `install.sh` (existing-deploy path) + `upgrade.sh` (drain→status→prove-stopped→backup→stage/validate→migrate→switch→units→start→readiness→undrain; `--check-compat` restore, manual-recovery else) | `test_install_upgrade_ordering` (drain-before-stop, readiness-before-undrain, compat-restore, no same-broken-restart) | 2e0f898+new | TBD | NOT EXECUTED — IMPLEMENTATION PHASE |
| AC-13 | Tunnel independence + pre-start validation + hashes + port drop-in (R35) | implemented-static | `systemd/cloudflared-ega-update.service` (`Wants`, no `BindsTo`, `ExecStartPre` validator `--only tunnel`), `install.sh`/`upgrade.sh` (`--require-hashes` no fallback, `10-port.conf` render), `validate-release.py` (tunnel/port/hashes sections) | `test_tunnel_unit`, `test_validator_*`, `test_install_upgrade_ordering` (no hash fallback, drop-in render) | 2e0f898+new | TBD | NOT EXECUTED — IMPLEMENTATION PHASE |
| AC-14 | RUNBOOK updated: user-manager/XDG, drain, sudoers, placeholders, lockfile, retention, full-UUID units | implemented-static | `docs/RUNBOOK.md` (§5 XDG + CLI reconcile, §10 drain/sudoers/placeholders/lockfile/retention, full-UUID renames, port drop-in, tunnel independence) | `test_runbook_markers` (full-UUID, XDG_RUNTIME_DIR, drain, sudoers, tombstones, drop-in markers; no `<shortid>`) | 2e0f898+new | TBD | NOT EXECUTED — IMPLEMENTATION PHASE |
| AC-15 | Behavioral regression tests written, none executed (R36) | implemented-static | `backend/tests/test_ui_deploy.py` (shell/retention/validator/ordering/source-marker suites) | self (this file row); suite is write-only | 2e0f898+new | TBD | NOT EXECUTED — IMPLEMENTATION PHASE |

## SC rows (safety / negative checks)

| ID | Check | Implementation status | Code paths | Regression test artifact | Tested commit | Environment | Result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| SC-01 | No heredoc-python in `scripts/*` or python sections of `deploy/scripts/*` | implemented-static | scripts/*, `install.sh`/`upgrade.sh` (`-c` strings only) | `test_shell_wrappers_thin_exec`, `test_install_upgrade_ordering` | 2e0f898+new | TBD | NOT EXECUTED — IMPLEMENTATION PHASE |
| SC-02 | No `if ! …; then status=$?` exit-clobber pattern in wrappers | implemented-static | scripts/* (positive `if …; then :; else` form, `exec` propagation) | `test_shell_wrappers_thin_exec` | 2e0f898+new | TBD | NOT EXECUTED — IMPLEMENTATION PHASE |
| SC-03 | No `pip install` hash-fallback (`--require-hashes … \|\| … install -r`) | implemented-static | `install.sh`, `upgrade.sh` (fail-closed `--require-hashes`) | `test_install_upgrade_ordering` | 2e0f898+new | TBD | NOT EXECUTED — IMPLEMENTATION PHASE |
| SC-04 | No hardcoded state paths for backup/drain (config-parsed) | implemented-static | `install.sh`/`upgrade.sh` (`cfg_value` + `EFFECTIVE_*`) | `test_install_upgrade_ordering` | 2e0f898+new | TBD | NOT EXECUTED — IMPLEMENTATION PHASE |
| SC-05 | No shortid unit references remain in RUNBOOK/systemd docs | implemented-static | `docs/RUNBOOK.md`, `systemd/*` (full `<32hex-uuid>` / `<job-id> <nonce>`) | `test_runbook_markers`, `test_runner_templates` | 2e0f898+new | TBD | NOT EXECUTED — IMPLEMENTATION PHASE |
| SC-06 | Runner templates carry 2-arg `<job-id> <nonce>` ExecStart, doc-only | implemented-static | `systemd/ega-update-runner@.service`, `systemd/user/ega-update-runner@.service` | `test_runner_templates` | 2e0f898+new | TBD | NOT EXECUTED — IMPLEMENTATION PHASE |
| SC-07 | Tunnel has no `BindsTo` API dependency; persistent restart | implemented-static | `systemd/cloudflared-ega-update.service` | `test_tunnel_unit` | 2e0f898+new | TBD | NOT EXECUTED — IMPLEMENTATION PHASE |
| SC-08 | Retention never touches protected jobs/backups | implemented-static | `backend/app/retention.py` (protected set, backup skip) | `test_retention_protections` (+ matrix) | 2e0f898+new | TBD | NOT EXECUTED — IMPLEMENTATION PHASE |
| SC-09 | Rollback never restarts the just-failed release | implemented-static | `install.sh`/`upgrade.sh` `fail()` (compat-gated prior restore, manual-recovery else) | `test_install_upgrade_ordering` | 2e0f898+new | TBD | NOT EXECUTED — IMPLEMENTATION PHASE |
| SC-10 | No secrets/placeholders shipped (validator fail-closed) | implemented-static | `deploy/etc/validate-release.py` (config/tunnel/secrets sections) | `test_validator_placeholders_port_manifest` | 2e0f898+new | TBD | NOT EXECUTED — IMPLEMENTATION PHASE |
| SC-11 | No `frontend/dist` gate; staged `backend/app/static/index.html` only | implemented-static | `install.sh`/`upgrade.sh`, `validate-release.py` (`check_frontend`) | `test_install_upgrade_ordering`, `test_validator_placeholders_port_manifest` | 2e0f898+new | TBD | NOT EXECUTED — IMPLEMENTATION PHASE |
| SC-12 | No fabricated PASS: every runtime claim is NOT EXECUTED | implemented-static | this file (no `PASS` token outside this sentence) | `test_evidence_no_pass` (fails if a runtime PASS appears) | 2e0f898+new | TBD | NOT EXECUTED — IMPLEMENTATION PHASE |

## Frontend manual checklist (no jsdom available — execute at runtime gate)

- MC-01: Open an active job; let it go terminal with multi-page logs. Confirm polling continues past terminal until cursor ≥ final seq and no more pages, then stops and refetches history.
- MC-02: With `has_more` true, press Load More; confirm the next page appends immediately without duplicates.
- MC-03: Close and reopen a finished job; confirm logs resume from 0 and drain fully; confirm the pending-tail note appears while terminal-but-behind and clears at drain.
- MC-04: Force a 429 (two tabs / rapid refresh); confirm the UI honors Retry-After, backs off exponentially (max 30s), shows the reconnect banner, and never busy-spins.
- MC-05: Start plans for two tools in quick succession; confirm a slow first response never overwrites the newer plan (generation guard).
- MC-06: With History open, start a job elsewhere; confirm Overview gating updates within ~5s without a history refresh (global active poll).
- MC-07: Age health past 5 min (or disconnect); confirm all health badges render stale styling (grey, explicit stale text), never green.
- MC-08: Open a job, reload the page after session recovery; confirm the same job view restores from localStorage.
- MC-09: Observe per-job network traffic; confirm no overlapping job/log fetches (serialized polls) and every request times out at 15s on hang.
- MC-10: For a retention-aged job, confirm the LogViewer shows the retention-gap note when the cursor sits behind final seq with no further pages.

## Runtime gates (required before any row can leave implementation phase)

1. Deploy-environment shell: `bash -n` on all touched shell files; `python3 -m py_compile` on `retention.py`, `validate-release.py`, `test_ui_deploy.py`; `tsc --noEmit` on frontend (main-agent pins).
2. `pytest backend/tests/test_ui_deploy.py` on the deploy host (seeded-DB retention matrix, wrapper/validator/ordering assertions).
3. Live deploy rehearsal (maintenance protocol, validator gates, tunnel survival across API restart, frontend MC-01..MC-10) with environment recorded above.

## Focused integration corrective addendum (N01–N18, main-agent pass)

Previous baseline `2e0f898`. This pass landed as implementation commit
`9f2d337cc0be9f227f2371090cc83b67fe0f370a` (replacing the
`PENDING:final-sha` placeholder used while the SHA did not yet exist).
Every row: `implemented-static`, result `NOT EXECUTED —
IMPLEMENTATION PHASE`. No runtime PASS claimed. New regression
artifact: `backend/tests/test_focused_corrective.py` (N01–N18 blocks)
plus conversions of `test_execution.py`, `test_api_flows.py`,
`test_update_console.py`, `test_core_corrective.py` to the shared
admission service and receipts v2 binding.

## Final static integration addendum (F01–F16, main-agent pass)

Previous baseline `679bacec77a5ba684e99cfec4160df5c5a69fb11`. This
pass landed as implementation head
`a664f1162f8ac3e2be2f6c4d6e59756761836671` (replacing the
`PENDING:final-sha` placeholder used while the SHA did not yet exist). Every row: `implemented-static`,
result `NOT EXECUTED — IMPLEMENTATION PHASE`. No runtime PASS claimed.
New regression artifacts: `backend/tests/test_final_integration.py`
(F01–F16 blocks), `backend/tests/test_deploy_bootstrap.py` (F06–F10
controller and script behavior), `backend/tests/support.py`
(`bound_receipt`, keyword-built v2 fixtures), plus conversions of
`test_execution.py`, `test_api_flows.py`, `test_update_console.py`,
and `test_core_corrective.py` to shared admission, receipts v2
binding, and contradiction-free success evidence.

## Final pre-runtime surgical addendum (G01–G08, main-agent pass)

Previous baseline `d4b1ba3281db36b626bf03ac4f7c26e7314a7a03`. This
pass landed as implementation head
`be123ddde8944aee864b59846f6c184914c28173` (replacing the
`PENDING:final-sha` placeholder used while the SHA did not yet exist). Every row: `implemented-static`,
result `NOT EXECUTED — IMPLEMENTATION PHASE`. No runtime PASS claimed.
New regression artifacts: `backend/tests/test_final_integration.py`
(G01–G08 blocks) plus `backend/tests/test_core_corrective.py`
(updated decide matrix) and `backend/tests/test_deploy_bootstrap.py`
(G07/G08 fail-closed controller behavior).

## Final pre-runtime surgical addendum, wave H (H01–H06, S01)

Starting SHA `07ba218` (H01–H03 already landed). This pass landed as
implementation head
`3414a8b42cad4fd43277bb5f228aa1f5ac8d22b0`. Every row:
`implemented-static`, result `NOT EXECUTED — IMPLEMENTATION PHASE`.
No runtime PASS claimed. New regression artifact:
`backend/tests/test_final_pre_runtime_static.py` (H01 block 5, H02
block 6, H03 block 15, H04 block 10, H05 block 8, H06 block 6, S01
block 8; write-only) plus updated G04 contract/launcher tests and the
G02 hook update in `backend/tests/test_final_integration.py`.

| Finding | Root-cause fix | Key files |
| --- | --- | --- |
| H04 | Authoritative probes run as transient probe SERVICES with the same NNP-off owner profile as the runner (`run_supervised_probe`, one shared `TRANSIENT_EXEC_PROPERTIES` tuple, one shared supervision helper); contract binds `probe_no_new_privileges=false` + `phase_privilege_source=runner` | `owner_env.py`, `phase_run.py`, `dispatch.py` |
| H05 | One canonical recovery decision: proven success is resolved (recovery 0); all reconcile sites share it; success releases ownership so a second job admits | `reconcile_core.py`, `dispatch.py`, `reconcile.py` |
| H06 | Evidence-init failure refuses before any adapter call (fixed `evidence_unavailable` literal); `_open_log` verifies `persist_failed`; execute refusal blocks without recovery; mid-operation failures stay interrupted-with-recovery | `runner.py`, `phase_run.py` |
| S01 | install/upgrade provision empty `secrets.env` (0640 `root:ega-update`); validator requires it; example + RUNBOOK agree | `install.sh`, `upgrade.sh`, `validate-release.py`, `config.example.json`, `docs/RUNBOOK.md` |

## H05 final addendum — resolved success clears historical uncertainty

Implementation head `44851b0` (starting from `cff4a4d`). Every row:
`implemented-static`, result `NOT EXECUTED — IMPLEMENTATION PHASE`.
No runtime PASS claimed.

Historical `unresolved`/`recovery_required` markers are cleared only
by a fully validated bound succeeded receipt after complete
execution-quiescence proof: the canonical helper decides proven
success before historical uncertainty, and reconciled receipt
application persists `state=succeeded`, `unresolved=0`,
`recovery_required=0` atomically before ownership release. Generic
receipt loads never clear execution uncertainty; manual
`--clear-recovery` remains for ambiguous failure cases. Regression
artifact: H05-final block (11) in
`backend/tests/test_final_pre_runtime_static.py` (write-only),
including crash-after-success reconciliation → second-job admission.

Evidence pin: implementation SHA
`6c11ece066219f2aac1b23fbded82bd1aae8f66f` (code + tests +
substantive docs). This pin commit is docs-only.

## Main-agent core corrective addendum (senior review R01–R36, Gate A–C)

Branch `feat/v1-implementation`, previous baseline `2e0f898`, this pass
uncommitted at matrix-update time. Every row: `implemented-static`,
result `NOT EXECUTED — IMPLEMENTATION PHASE`. No runtime PASS claimed.

| Finding | Root-cause fix | Key files | Regression test artifact |
| --- | --- | --- | --- |
| R01 | Typed probe queue (`probe_requests`/`probe_results`); dispatcher executes ops as ubuntu; API waits bounded off-loop, falls back cached+stale | `owner_probes.py`, `worker/dispatch.py::run_probe_queue`, `api/routes.py` (check/plans via `_owner_probe`) | `test_core_corrective.py::test_probe_queue_roundtrip`; `test_api_flows.py` probe-fake suite |
| R02 | Canonical allow-list env + per-job immutable release binding; preview/apply share it | `owner_env.py`, `plans.release_path`, `worker/runner.py::_validate_env_release` | disposable-verification gate (env equivalence), not unit-testable offline |
| R03 | Lock handle held for life; atomic nonce claim with unit+release+deadline; one-shot consume; full-UUID units; guarded transitions | `dispatch.py::main`, `jobs.py::claim_with_nonce/consume_attempt`, `tx.py`, `runner.py` | `test_claim_deadline_enforced`, `test_consume_attempt_single_use` |
| R04 | Five-state unit model from bounded show; unknown holds reservation; terminal unresolved rows reconciled | `units.py`, `reconcile_core.py::decide`, `dispatch.py::_reconcile_row` | `test_units_*`, `test_decide_matrix`, `test_reconcile_unknown_holds_reservation` |
| R05 | `tx.transition_tx`: one explicit transaction per transition incl. recovery/checks/event; unresolved marker pre-mutation; synchronous=FULL; ownership release only via `tx.release_ownership` after quiescence proof | `tx.py`, `db.py`, `runner.py::_finish`, `dispatch.py`, `reconcile.py` | `test_tx_guard_and_atomic_terminal` |
| R06 | Supervised phase-worker processes in coordinator-owned scopes; coordinator kills scope, proves empty; delegated services read from plan | `executor.py` (streaming Popen), `phase_run.py::run_supervised_phase`, `runner.py::_run_phase/_hard_timeout_recovery`, adapters via `run_stream` | `test_executor_timeout_kills_and_reports`, `test_executor_cancel_event`, adapter timeout-funnel tests |
| R07 | Shared `reconcile_core` + `units` + v2 receipts + tx in SSH CLI; hex self-exclusion; JSON-first validation, no fallback promotion; abandoned rows terminalized | `reconcile_core.py`, `worker/reconcile.py` | `test_decide_matrix`, observer-exclusion by construction (hex never in observer argv) |
| R08 | Receipts v2: full binding + strict success criteria + atomic apply + contradiction refusal | `receipts.py`, `schemas.py::ReceiptModel` | validate matrix, apply/idempotency/binding/contradiction tests |
| R09 | Streaming Popen executor: concurrent drains, incremental decode, live sink, bounded tails, true exits, scope containment | `executor.py`, `runner.py::_live_on_line`, `phase_run.py` stream tailing | `test_executor_*` (live line <5s gate at runtime) |
| R10 | `SanitizingStream`: withheld blocks, tick-safe, oversized suppression, fail-closed, secrets-before-truncation | `sanitize.py`, `runner.py::JobLog` | `test_sanitizer_*` |
| R11 | One sanitizer for DB/API/receipt/CLI surfaces; mutation gated on log init; mid-op failure stops to recovery | `sanitize.py`, `runner.py::_record_checks_dict`, `receipts.py`, `events.py::record_event` | seeded-secret scan gate at runtime |
| R13 | Packaged `cli.py` (shared admission + observe, never direct runner); thin `exec` scripts | `app/cli.py`, `scripts/*` | `test_shell_wrappers_thin_exec`, exit-mapping review |
| R14 | Shared admission service, replay-first; subject/expiry/one-use; worker-readiness 503 | `admission.py::admit`, `api/routes.py::post_job`, `app/cli.py::cmd_apply` | `test_find_replay_precedes_admission`, replay-matrix tests |
| R15 | Immutable v2 plans (hash-bound, config+release bound); authoritative revalidation; invalidation never silent alteration | `plans.py`, `api/routes.py`, `runner.py::_build_plan/_validate_env_release` | `test_plan_hash_tamper_rejected`, fingerprint/config tests |
| R16 | Probes off-loop (queue + worker-thread wait); streaming size guard; small plan schema | `owner_probes.py`, `api/routes.py`, `api/deps.py::check_body_limit` | slow-probe responsiveness gate at runtime |
| R17 | Heartbeat admission gate 503; atomic claim deadline; safe sweep (accepted+empty-nonce only) | `api/routes.py`, `jobs.py::claim_with_nonce/expire_stale_accepted` | `test_claim_deadline_enforced`, worker-down 503 tests |
| R18 | installer_exit/install_outcome/actual_change/final_log_seq; observation after every verification; already-current distinct | `runner.py::_finish/_record_observation`, `schemas.py`, `api/routes.py::_job_view` | observation/health-failed replaces-green tests |
| R19 | Success vs attempt timestamps separated; force bypasses cache; unknown preserves last-known | `api/routes.py` check handler, `frontend/src/app.tsx::handleCheck(force=true)` | check-again/failed-check tests |
| R31 | Ordered ledger migrations + validation-only startup + rollback compat | `db.py`, `migrations/001..004` | `test_migrate_fresh_rerun_and_newer_rejected`, `test_migrate_001_to_current_upgrade` |
| R34 | Placeholder-fails-readiness; per-service secret files; one env-style parser | `readiness.py`, `sanitize.py::parse_secrets_file`, `config.py` | `test_parse_secrets_file_formats`, readability matrix gate at runtime |

## Pre-real-update closure addendum (non-destructive runtime)

Starting baseline `114b327`. Every number below comes from executed
disposable runs (worktree + venv + tmp SQLite); nothing here ran
against managed tools, host systemd mutation, production state, or
any deployment target. Historical static-phase rows above remain
`NOT EXECUTED — IMPLEMENTATION PHASE` as recorded at the time.

Backend suite: 342 collected, 0 collection errors; full suite
green 3 consecutive times (342 passed each, 0 warnings), run from a
detached acceptance worktree in a hash-locked venv
(`pip install --require-hashes`). Concurrency contender deterministic
via barrier, proven 50/50. H-wave module 69/69 green; H05 critical
subset 12/12 green, including crash-after-success reconciliation
to succeeded/0/0 with lease release and second-job admission.

Migrations: fresh v0→v4 green, second migrate idempotent green,
schema validation green (both passes), SQLite integrity ok, ledger
rows 1–4 present, backup API green with 4 migration rows. Partial
resume, pre-ledger inference, and newer-schema refusal covered by
regression tests, all green.

Frontend: Node v24.18.0 / npm 11.19.1; deterministic lock committed;
`npm ci`, typecheck, and build green; `backend/app/static/index.html`
emitted.

Release simulation: synthetic positive tree validates clean across
all eight validator sections; the 18-scenario negative matrix fails
closed in every case (missing/unhashed/malformed lock material,
MANIFEST absence/tamper, missing frontend, placeholder identity
fields, missing/short secrets, open/missing secret files, missing
or placeholder tunnel material, non-loopback target, port
mismatch, incompatible rollback schema).

Shell scripts pass `bash -n`; install/upgrade never executed
against the host in this phase.

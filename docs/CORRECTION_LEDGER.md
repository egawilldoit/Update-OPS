# Update-OPS V1 — Senior-Review Correction Ledger (R01–R36, N01–N18, F01–F16, G01–G08, H01–H06, S01)

Branch: `feat/v1-implementation`. Baseline: `2e0f898`.
Statuses: `open` → `implementing` → `implemented-static` → `runtime-pending`.
Nothing is `verified`/`accepted`/`complete` without execution.
All runtime results: `NOT EXECUTED — IMPLEMENTATION PHASE`.

Seven invariants: (1) owner execution boundary, (2) one process supervisor,
(3) transactional state machine, (4) evidence/redaction pipeline,
(5) config/inventory contract, (6) streaming executor, (7) typed plan/outcome.

| ID | Sev | Root cause | Invariant | Files (owner) | Status | Regression test |
| --- | --- | --- | --- | --- | --- | --- |
| R01 | P1 | API (ega-update+ProtectHome) runs ubuntu probes directly | 1 | owner_probes.py, dispatch.py, routes.py (main) | implemented-static | boundary blocks direct probe; typed queue works |
| R02 | P1 | No canonical owner env; release/env not bound per job | 1,5 | owner_env.py, jobs/plans cols, dispatch, runner (main) | implemented-static | env equivalence preview vs apply; boot w/o login |
| R03 | P1 | Lock handle dropped; nonce read-only; 8-char units | 2,3 | dispatch.py, jobs.py, tx.py, runner.py (main) | implemented-static | same-nonce race, replay, 2nd dispatcher |
| R04 | P1 | active/not-active binary; unknown→terminal | 2,3 | units.py, dispatch.py, reconcile_core.py (main) | implemented-static | activating/deactivating/failed-query matrix |
| R05 | P1 | Autocommit splits terminal state vs recovery flag | 3 | tx.py, runner.py, dispatch.py, reconcile, receipts (main) | implemented-static | fault between every terminal write |
| R06 | P1 | Thread timeouts don't stop work; self-unit kill | 2,6 | executor.py, runner.py, adapters (main+adapters) | implemented-static | hanging phase, reparent, delegated op |
| R07 | P1 | Reconcile detects itself; bad receipt fallback | 2,3 | reconcile_core.py, reconcile.py, cli.py (main) | implemented-static | self-exclusion, malformed receipt |
| R08 | P1 | Receipts unbound, weak success criteria | 3,4 | receipts.py v2, schemas.py (main) | implemented-static | swapped/stale/contradictory receipts |
| R09 | P1 | subprocess.run PIPE buffers; slice-after-capture | 6,4 | executor.py (main), adapters/* (adapters) | implemented-static | live line <5s, huge output bounded |
| R10 | P1 | Raw fallback on empty; tick finalizes PEM; pre-truncation secrets | 4 | sanitize.py, redaction.py, runner JobLog (main) | implemented-static | JobLog-path PEM/tick/UTF-8 cases |
| R11 | P1 | DB/API surfaces bypass sanitizer; evidence failure non-blocking | 4 | sanitize.py + all writers (main), adapters summaries (adapters) | implemented-static | seeded-secret DB/JSONL/API scan |
| R12 | P1 | `if !` clobbers `$?` → shell false-success | 7 | cli.py (main), scripts/* (ui/deploy) | implemented-static | exit 0/2/3/4/5/6 via shell |
| R13 | P1 | Direct apply runs runner w/o claim/systemd | 2 | cli.py (main), scripts/*, systemd/* (ui/deploy) | implemented-static | apply==canonical path only |
| R14 | P2 | Idempotency checked after invalidating conditions | 3,7 | routes.py post_job (main) | implemented-static | replay after expiry/drain/recovery |
| R15 | P1 | Stale cache fingerprint; plans under-bound | 5,7 | plans.py, owner_probes, routes, runner (main) | implemented-static | SSH-change invalidates; config drift blocks |
| R16 | P1 | Sync probes in async routes; gate race | 1 | routes.py + owner_probes (main) | implemented-static | slow probe keeps API responsive |
| R17 | P1 | Admission ignores worker readiness; claim has no deadline | 3 | routes.py, jobs.py claim (main) | implemented-static | worker-down 503; deadline enforced |
| R18 | P1 | Health/version/installer-exit conflated; already-current indistinct | 7,4 | runner.py, routes.py, schemas.py (main) | implemented-static | fail-after-install, green replacement |
| R19 | P2 | Cache served for manual refresh; error refreshes timestamp | 4 | routes.py check (main) | implemented-static | force refresh; failed-check timestamps |
| R20 | P2 | Poll stops at terminal with has_more pending | 4 | frontend (ui/deploy) + final_log_seq (main) | implemented-static | reopen drains all pages |
| R21 | P2 | Polling exceeds own rate limit; stale global state | 4 | deps.py + frontend (main+ui/deploy) | implemented-static | two-tab, 429 backoff, plan race |
| R22 | P1 | Hermes identity/activity incomplete | 7 | hermes.py (adapters) | implemented-static | wrong remote, dirty, unknown activity |
| R23 | P1 | Backup proof = help text + output keyword | 7 | hermes.py (adapters) | implemented-static | keyword w/o artifact fails |
| R24 | P1 | show≠restart proof; CLI version≠running version | 7 | hermes.py (adapters) | implemented-static | old-commit unit fails |
| R25 | P1 | Daemon absence = N/A; broad-string absence; generic T3 HTTP | 7 | codex.py (adapters) | implemented-static | expected-daemon-missing fails |
| R26 | P1 | T3 pillars uncorrelated; state version; mutable ExecStart | 7 | t3.py (adapters) | implemented-static | stale state + stranger process fails |
| R27 | P1 | Running-service backup impossible/contradictory | 7 | t3.py, opencode.py (adapters) | implemented-static | quiesce path; basename collisions |
| R28 | P1 | Fixed invented backup sizes; floor not re-held | 7 | adapters + runner (adapters+main) | implemented-static | unknown blocks; post-backup recheck |
| R29 | P2 | No retention implementation | 4 | retention.py (ui/deploy), dispatch hook (main) | implemented-static | policy + tombstones + protection |
| R30 | P1 | Lexical prerelease order; no runtime/arch proof | 7 | semver.py + opencode/t3 (adapters) | implemented-static | beta.10>beta.2; engines mismatch blocks |
| R31 | P1 | migrate() ignores numbered files; version hardcoded | 3,5 | db.py, migrations/ (main) | implemented-static | 001→003, partial, rerun, newer-reject |
| R32 | P1 | Deploy python CWD-dependent; config not pinned | 5 | deploy/scripts (ui/deploy) | implemented-static | unrelated-CWD deploy |
| R33 | P1 | No real quiescence; failure path not restorative | 2,3 | deploy/scripts (ui/deploy) | implemented-static | drain race, stop failure, readiness |
| R34 | P1 | Secret precedence/format/ownership disagree | 5 | config.py, readiness.py (main); installer (ui/deploy) | implemented-static | readability matrix |
| R35 | P2 | BindsTo stops tunnel; no pre-start validation; hash fallback | 5 | systemd/*, deploy/* (ui/deploy) | implemented-static | API-restart tunnel survival |
| R36 | P2 | Tests mock the integration; no evidence matrix | — | all tests + docs/EVIDENCE.md (all) | implemented-static | behavioral cases per row above |

Ownership: main agent owns shared schema/migrations/contracts/DB/worker/
runner/supervisor/config/receipts/retention-hook/API-core; adapter contributor
owns adapters/* + semver.py; UI/deploy contributor owns frontend/*,
scripts/*, systemd/*, deploy/*, retention.py, docs/RUNBOOK.md, docs/EVIDENCE.md.

# Focused integration corrections (N01–N18, this pass)

Statuses: implemented-static only. No verified/accepted/passed.

| ID | Root-cause fix | Files | Regression tests |
| --- | --- | --- | --- |
| N01 | Canonical quiescence schema (`quiescence.assess_quiescence`) + `status --require-quiescent/--require-ready` exit codes; scripts consume exits only | `quiescence.py`, `units.py` (list/enumerate), `cli.py`, `install.sh`, `upgrade.sh` | `test_focused_corrective.py` N01 block (6) |
| N02 | `_strict_int` (zero valid; missing/null/malformed raise); success fail-closed | `receipts.py` | N02 block (3) |
| N03 | Binding extended to plan row (hash/release/target/mode/manifest), mandatory-non-weakening, cleanup/evidence/version/exit/outcome checks; contradictions refuse | `receipts.py`, `dispatch.py`, `reconcile.py` | N03 block |
| N04 | `migrate()` only via `db` module CLI (deploy) + tests; services validate only; allowed callers documented | `db.py`, `runner.py`, `dispatch.py`, `main.py`, scripts | N04 source test |
| N05 | Column-aware applier, one explicit tx per migration, ledger last; rerun/resume safe | `db.py` | N05 block (3) |
| N06 | `validate_schema` zero DDL/DML; absent ledger = pending | `db.py` | N06 block |
| N07 | LoadState-gated stopped proof; not-found removal proof; MainPID-0/cgroup-unreadable never decide; duplicate `_prove_launch` removed | `units.py`, `dispatch.py` | N07 block (3) |
| N08 | `execution_leases` (migration 004): atomic probe leases, non-expiring mutation leases, reclaim-only-probes; dispatcher + admission wired | `leases.py`, `004_leases_env.sql`, `dispatch.py`, `admission.py` | N08 block (2) |
| N09 | Canonical env fingerprint bound into plans; dispatcher bus env at startup; NoNewPrivileges parity already present | `owner_env.py`, `plans.py`, `routes.py`, `runner.py`, `dispatch.py` | N09 block (2) |
| N10 | `phase_run.py`: worker process per phase in owned scope; coordinator kills scope, proves empty; no thread supervision; dispatcher probes supervised too | `phase_run.py`, `runner.py`, `dispatch.py`, `base.py` (extra=allow) | N10 block (2, bus-gated) |
| N11 | `run_fixed` fails closed (127, no fallback); subprocess inventory documented | `registry.py`, ledger | N11 block (2) |
| N12 | Suppression markers / hard failure instead of raw: registry strict helper, receipts raise, dispatch error result, CLI fixed message | `registry.py`, `receipts.py`, `dispatch.py`, `cli.py` | N12 block (3) |
| N13 | `tx.transition_tx` boundary sanitization (event/checks/evidence cols) → TxError fail-closed; API exc details sanitized | `tx.py`, `routes.py` | N13 block |
| N14 | `admission.admit` shared by routes+CLI; replay-first incl. drain/recovery; `reserve_job` deleted | `admission.py`, `routes.py`, `cli.py`, `jobs.py`, tests | N14 block (4) |
| N15 | Job INSERT + plan used_at + mutation lease + event in one tx; injected-failure rollback proven | `admission.py`, `tx.py` (lease release) | N15 block |
| N16 | `config_cli get/json` single parser; scripts converted; typo keys fail | `config_cli.py`, `install.sh`, `upgrade.sh` | N16 block (2) |
| N17 | `validate-archive.py` member inspection; pre-extraction gates in both scripts; checkout validator (never tarball code) | `validate-archive.py`, `install.sh`, `upgrade.sh` | N17 block (5) |
| N18 | This section + EVIDENCE addendum with PENDING:final-sha marker | ledger, `docs/EVIDENCE.md` | N18 block |

Cleanup disposition:
- duplicate `_prove_launch`: removed (single implementation kept).
- transient unit/documentation parity: `owner_env.build_transient_cmd` (+ scope names, env fingerprint) is the single source; both templates match (KillMode/Restart/env/wd/interpreter/job+nonce); dispatch delegates; RUNBOOK already full-UUID.
- atomic claim deadline: single-statement predicate (`claim_deadline>now`, empty refuses); Python pre-read removed.

# Final static integration corrections (F01–F16, this pass)

Statuses: implemented-static only. No verified/accepted/passed.

| ID | Root-cause fix | Files | Regression tests |
| --- | --- | --- | --- |
| F01 | One canonical hash view (`_hash_view` at build and load); fixture repair removed | `plans.py`, `tests/support.py` | `test_final_integration.py` F01 block (2) |
| F02 | Terminal state decoupled from ownership; `tx.release_ownership` after quiescence proof; no opportunistic release; sweep covers terminal-held-lease rows | `tx.py`, `leases.py`, `runner.py`, `receipts.py`, `dispatch.py`, `reconcile.py`, `quiescence.py` | F02 block (5) |
| F03 | `OwnerExecutionContract` (uid/gid/user/home/path/config/release/venv/node/bus/locale/nnp/scope/authority); contract env for probes+phases; worker parity proof; templates parity | `owner_env.py`, `phase_run.py`, `runner.py`, `dispatch.py`, templates | F03 block (4) |
| F04 | Strict exits, explicit outcome vocabulary, cleanup/recovery/checks agreement, no weakening, build-time strictness | `receipts.py` | F04 block (2) |
| F05 | Full cumulative structural contracts; proof-gated inference; complete verification | `db.py` | F05 block (5) |
| F06 | Checkout-based bootstrap (`REPO_ROOT`); no candidate dependency | `install.sh`, `upgrade.sh` | deploy bootstrap tests |
| F07 | `deploy/etc/quiescence-check.py` version-independent controller; scripts consume exits | `quiescence-check.py`, scripts | deploy bootstrap tests |
| F08 | Detection after trusted config parse; unparsable config blocks | `install.sh` | deploy bootstrap tests |
| F09 | Controller-only quiescence; direct-stop fallback removed | `install.sh`, `upgrade.sh` | deploy bootstrap tests |
| F10 | Coherent interpreter+CWD+source tuples; no ambient fallback | `upgrade.sh` | deploy bootstrap tests |
| F11 | Worker evidence flags + durability result; secret gate; coordinator mapping; central events | `phase_run.py`, `runner.py`, `events.py`, `dispatch.py`, `reconcile.py` | F11 block (7) |
| F12 | Operation classification; leases for all installation reads; explicit TTL/deadline margin | `owner_probes.py`, `dispatch.py` | F12 block (4) |
| F13 | Typed CLI detail preserved (no Python-repr stringification) | `cli.py` | F13 test |
| F14 | Plan/tool resolved before probe enqueue (no repair UPDATE) | `cli.py` | F14 test |
| F15 | Immutable staging + digest + validate + re-verify + extract staged + digest record | `install.sh`, `upgrade.sh` | F15 test |
| F16 | Stale architecture sweep (docstring, dead flag, source-scan test) | `runner.py`, tests | F16 test |

# Final pre-runtime surgical corrections (G01–G08, this pass)

Statuses: implemented-static only. No verified/accepted/passed.

| ID | Root-cause fix | Files | Regression tests |
| --- | --- | --- | --- |
| G01 | Orphaned `try:` + duplicated gate comment removed from `Runner.run()`; try/except balance + fragment tripwires | `runner.py` | `test_final_integration.py` G01 block (3) |
| G02 | Receipt never overrides surviving processes: `decide()` orders unit → processes → delegated → receipt; `procs=None` means unprovable and holds | `reconcile_core.py`, `dispatch.py`, `reconcile.py` | G02 block (2) + updated decide matrix |
| G03 | Shared `delegated_quiescence()` (plan services, user-then-system bus, per-service proof); dispatcher + SSH use the same entry point; retention protects held-lease jobs/plans | `reconcile_core.py`, `dispatch.py`, `reconcile.py`, `retention.py` | G03 block (6) |
| G04 | Privilege truth: job execution is NNP-off (Hermes sudo path requires elevation); contract binds `runner/scope_no_new_privileges=false` + `privilege_profile`; launch argv states it; templates match reality | `owner_env.py`, templates | G04 block (4) |
| G05 | `SecretSourceError` distinguishes broken source from by-design empty; all durable paths fail closed; JobLog init gates mutation | `config.py`, `sanitize.py`, `receipts.py`, `runner.py`, `routes.py` | G05 block (4) |
| G06 | `RECOVERY_RESOLVED_VALUES=("none",)`: success requires exactly `none` in validate + binding | `receipts.py` | G06 allowlist test |
| G07 | Deploy quiescence is proof-structured: unreadable jobs/leases/plans block; pre-lease schemas explicit; missing tables block | `quiescence-check.py` | G07 block (2) |
| G08 | Held leases independently fatal; terminal-owned jobs in delegated/unit/process proof | `quiescence-check.py` | G08 block (6) |

# Pre-runtime surgical corrections, wave H (H01–H03 implemented-static; H04–H06/S01 pending finding definitions)

Statuses: implemented-static only. No verified/accepted/passed.
All runtime results: `NOT EXECUTED — IMPLEMENTATION PHASE`.
Commits (branch `feat/v1-implementation`): H01 `66b2fb1`, H02 `a9bab77`, H03 `60f1c4b`.

| ID | Root-cause fix | Files | Regression tests |
| --- | --- | --- | --- |
| H01 | Mutation lease bound to the REAL job UUID before acquisition (`mutation-<job-uuid>`, `job_id` set in the same reservation transaction); no `mutation-__pending__` placeholder insert/update/repair; held older leases still block; rollback leaves neither job nor lease | `admission.py` | `test_final_pre_runtime_static.py` H01 block (5) |
| H02 | Scope-directed delegated quiescence: `parse_service_ref()` (`scope:unit`, bare unit = user scope, malformed = UNKNOWN and blocks); `delegated_quiescence()` queries ONLY the declared manager (user-then-system fallback removed); deploy `quiescence-check.py` mirrors the parser scope-directly; codex drops non-unit `codex-daemon`, opencode emits `user:<unit>`, hermes/t3 already `scope:unit` | `reconcile_core.py`, `dispatch.py` (unchanged call sites), `adapters/codex.py`, `adapters/opencode.py`, `deploy/etc/quiescence-check.py` | H02 block (6) |
| H03 | Structured process proof `prove_processes()` (`ok`/`processes`/`reason`; enumeration failure, PermissionError/other non-exit read failures, undecodable cmdlines are UNPROVABLE and hold; exited PIDs skip); all reconcile paths + `_prove_launch` + runner sentinel (`pid -1`) fail closed; `expected_phase_scopes()` + `phase_scopes_quiescence()` (all four job scopes confirmed stopped, pinned to `phase_run.PHASES` minus probe) gate both reconcilers before receipt/ownership; `run_supervised_phase()` proves scope exit on normal completion (reap-then-deliver; unreapable = timeout, keep recovery) | `reconcile_core.py`, `dispatch.py`, `reconcile.py`, `runner.py`, `phase_run.py` | H03 block (15) + G02 hook update in `test_final_integration.py` |

# Pre-runtime surgical corrections, wave H continued (H04–H06, S01 — implemented-static)

Statuses: implemented-static only. No verified/accepted/passed.
All runtime results: `NOT EXECUTED — IMPLEMENTATION PHASE`.
Commits (branch `feat/v1-implementation`): H04 `6ebe8a2`, H05 `b4c825d`, H06 `c81fe47`, S01 `47a969b`.

| ID | Root-cause fix | Files | Regression tests |
| --- | --- | --- | --- |
| H04 | Probe/apply privilege parity: authoritative probes launch as transient SERVICES (`transient_probe_name()` + `build_probe_cmd()` on the ONE shared `TRANSIENT_EXEC_PROPERTIES` tuple: KillMode=control-group, Restart=no, NoNewPrivileges=no) via new `run_supervised_probe()`; shared `_supervise_launched()` so phase scopes and probe services share one supervision algorithm incl. the H03 exit proof. Contract tells the truth (`probe_no_new_privileges=false`, `phase_privilege_source=runner`; unenforced `scope_no_new_privileges` removed, fingerprint-bound so stale plans fail closed). Dispatcher probes, deadlines, leases, exclusion, release binding, redaction, fail-closed launch unchanged; adapter mutations stay direct spawns inside the NNP-off worker (`plan.scope_unit` cleared by runner) | `owner_env.py`, `phase_run.py`, `dispatch.py`, `docs/CONTRACTS.md` | H04 block (10) + updated G04 contract/launcher tests in `test_final_integration.py` |
| H05 | One canonical recovery decision `recovery_required_for_outcome()`: proven success (valid bound succeeded receipt incl. already_current, disposition none, execution quiescent) is resolved (recovery 0) — mutation evidence alone never implies recovery; disposition required / non-success mutation evidence / mutation-window anchors / unknown-contradictory require; positively pre-mutation anchors without evidence resolve. All 4 reconcile sites (dispatcher + SSH, apply + interrupt) share it (source-pinned; legacy `recovery_for()` is a thin wrapper). Runner success finalization already recovers 0; all reconcile flows through the migrated paths (continuous + boot + SSH) | `reconcile_core.py`, `dispatch.py`, `reconcile.py` | H05 block (8) incl. success-release-second-admission integration |
| H06 | Evidence-init blocks mutation: `_open_log()` raises on `persist_failed` (same pre-mutation blocked path, no recovery); worker refuses with fixed literal `evidence_unavailable` immediately after `_StreamWriter` construction, before backup/execute/verify/probe adapter calls; runner preserves the literal through `_do_execute` to blocked-without-recovery (mutation positively never began); missing results and mid-operation failures keep interrupted-with-recovery | `runner.py`, `phase_run.py` | H06 block (6) |
| S01 | Fresh installs satisfy the secret-source contract: install/upgrade provision EMPTY `secrets.env` when absent (never overwrite/values/contents); exactly 0640 `root:ega-update` (both service users read via group; 0600 root-owned would fail readiness); validator requires it present at 0640; example + RUNBOOK agree | `install.sh`, `upgrade.sh`, `validate-release.py`, `config.example.json`, `docs/RUNBOOK.md` | S01 block (8) |

# H05 final addendum — resolved success clears historical uncertainty

Commit `44851b0` (branch `feat/v1-implementation`). Status:
implemented-static. Result: `NOT EXECUTED — IMPLEMENTATION PHASE`.

Defect: the canonical recovery helper preserved `unresolved=1` before
considering new authoritative evidence, and receipt application
preserved stale `recovery_required`/`unresolved` flags — a proven
success could never unblock admission.

Fix (two linked parts, one interpretation):
- `recovery_required_for_outcome()` decides fully proven success
  FIRST: a valid bound succeeded receipt (incl. already_current)
  with disposition none, applied state succeeded, and independently
  proven execution quiescence returns False even with
  `unresolved=True`. Historical uncertainty blocks only UNTIL
  authoritative resolution exists. All non-success/ambiguous paths
  keep fail-closed recovery.
- `apply_receipt(..., execution_quiescent=True)` (resolved-success
  mode, only from reconciliation after full H02/H03 proof) clears
  `unresolved=0` + `recovery_required=0` atomically in the SAME
  transaction as the terminal evidence, before ownership release —
  including a same-state terminal rewrite when stale flags survive.
  Generic loads (flag False) preserve flags; required dispositions
  still set recovery. Dispatcher + SSH reconcilers pass the flag;
  manual `--clear-recovery` stays for ambiguous failure cases.

Regression: H05-final block (11) in
`backend/tests/test_final_pre_runtime_static.py` — unresolved+success
clears, unresolved without proof remains, unproven execution remains,
apply clears both flags (crash + stale-terminal variants), legacy
apply preserves, failed keeps recovery, tampered refused with
flags/lease intact, live scope holds everything, live delegated
service holds, crash-after-success E2E → second-job admission.
Write-only.

# Pre-real-update closure (implementation + non-destructive runtime)

Baseline `114b327`. All fixes below are committed on
`feat/v1-implementation`; runtime evidence comes only from disposable
worktrees/venvs/DBs (no managed-tool, systemd, production, or
deployment mutation).

## Failure ledger closure (29 verified suite failures, all dispositioned)

| Failure(s) | Classification | Fix |
| --- | --- | --- |
| h01 rollback / n15 predicate never fires | TEST (upper-vs-lowercase needle) | normalize both sides |
| n09 / h04 `stale_plan` vs `config_changed` | TEST (row tampering simulates TRACE 8, not drift) | true env-drift simulation via patched contract |
| opencode/t3/adapter-timeout fakes | TEST (fake drift: missing scope_unit/env) | fakes match current contracts |
| hermes-sudo / g04-hermes / lifespan / n16 / install-ordering / f15 | TEST (whole-file/prose substring brittleness) | behavioral + directive/command-order assertions |
| canonical_argv `cmd[-4:]` vs 5 items | TEST (impossible slice) | `cmd[-5:]` |
| f11 mutation fake | TEST (unbound method) | bound `self` signature |
| f11 event fake job | TEST (nonexistent job vs intentional FK=ON) | real admitted job fixture |
| claim/api busy (3) | TEST (raw transition never releases leases per F02) | reconcile+release pattern |
| drain unknown-plan status | TEST (over-strict 503 pin; refusal preserved as 4xx + no side effects) | 4xx + no-job/no-lease assertions |
| sanitizer markers (2) | TEST (stale `***REDACTED***` literal) | current contract markers |
| f05 raw-ledger fixtures (2) | TEST (raw SQL bypasses Python-managed ledger v3 requires) | engine-built states via migrate(target=) |
| f03 bus variants | TEST (live bus shadows dict overrides) | neutralize bus source in test |
| validator tmpdir token | TEST (dirname self-collision) | neutral mkdtemp |
| retention orphan plan | TEST expectation correct; PRODUCT falsy-zero (`or 1`) bug | `or 0` + regression via existing test |
| t3 staging gate | TEST (registry probe unfaked) | fake `_staging_estimate` |
| g05 empty-string semantic | PRODUCT vs documented contract | empty/unset/None → `()`; configured paths strict |
| SSH stale verdict (P2) | PRODUCT (pre-read snapshot) | verdict from persisted re-read |
| validator SCHEMA_VERSION alias | PRODUCT (validator demanded a name the engine never defined) | CODE_VERSION fallback mirroring compat path |
| f14 /var/lib writes | ENVIRONMENT coupling in test | hermetic loader patch |
| concurrent reservation flake | TEST timing (plus stale_plan-vs-busy on reused plan) | Barrier determinism + fresh plan probe; 50/50 green |
| JWT short key warning | TEST hygiene | 32B synthetic key |

## Product corrections in closure

- G05 final: missing attribute / None / `""` → `()`; non-string →
  SecretSourceError; configured path read-or-raise. Deployed
  Settings always carry the V1 default path, so worker/API paths
  stay fail-closed. Nine behavior tests.
- SSH reconcile: final verdict re-reads persisted
  state/unresolved/recovery_required; stale pre-read can no longer
  print a false UNRESOLVED. Crash-success → exit 0 resolved;
  ambiguous → nonzero; `--clear-recovery` preserved. Three tests.
- Retention: orphan-plan cascade `or 1` → `or 0` (falsy-zero guard).
- Validator: `CODE_VERSION` accepted where `SCHEMA_VERSION` demanded
  (mirrors the compat path); synthetic 18-scenario negative matrix
  plus positive case committed as regression.
- Deps: `frontend/package-lock.json` committed (deterministic across
  generations); `backend/requirements.in` + hashed
  `backend/requirements.txt` via pip-tools (no version changes);
  placeholder `requirements.pinned.txt` removed.

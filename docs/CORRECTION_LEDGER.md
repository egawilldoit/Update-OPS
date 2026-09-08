# Update-OPS V1 — Senior-Review Correction Ledger (R01–R36)

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

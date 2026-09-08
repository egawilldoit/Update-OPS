# Update-OPS V1 — Implementation Contracts (main-agent owned)

Status: frozen for `feat/v1-implementation` unless main agent amends.
Sources: `DOC/PRD-UPDATE SYSTEM.md`, `DOC/SPEC-UPDATE SYSTEM.md`, `DOC/spec_scripts.md`.

## 1. File ownership (no overlapping edits)

| Area | Owner | Files |
| --- | --- | --- |
| Shared contracts, backend core, DB, auth, jobs, manifests | main agent | `docs/CONTRACTS.md`, `backend/app/__init__.py`, `backend/app/config.py`, `backend/app/db.py`, `backend/app/models.py`, `backend/app/schemas.py`, `backend/app/auth.py`, `backend/app/redaction.py`, `backend/app/jobs.py`, `backend/app/main.py` (skeleton only), `backend/app/adapters/base.py`, `backend/requirements.txt`, `backend/requirements.pinned.txt`, `frontend/package.json`, `README.md`, `docs/DEPENDENCIES.md` |
| Frontend + API integration | subagent A | `frontend/**` (except `frontend/package.json` which is frozen), `backend/app/api/**`, `backend/app/main.py` (route wiring only, no schema/contract changes) |
| Tool adapters + script wrappers | subagent B | `backend/app/adapters/{codex,opencode,hermes,t3,claude}.py`, `backend/app/worker/runner.py` (adapter invocation only), `scripts/*` |
| systemd / deployment / recovery docs | subagent C | `systemd/*`, `deploy/**`, `docs/RUNBOOK.md`, `docs/INVENTORY.md`, `deploy/etc/inventory.schema.json`, `backend/app/worker/reconcile.py`, `backend/migrations/*` (numbered SQL only) |
| Worker core (not delegated) | main agent | `backend/app/worker/dispatch.py`, `backend/app/worker/__init__.py` |

Rules: subagents import from `backend/app/schemas.py`, `backend/app/adapters/base.py`,
`backend/app/config.py` — they do not edit them. Schema/dependency changes are
serialized through the main agent.

## 2. Tool identities

`TOOL_IDS = ("hermes", "opencode", "codex", "t3")`. `claude` exists only as a
disabled optional adapter (`enabled=false`, `BLOCKED_INSTALL_OWNERSHIP`).
Canonical display order: Hermes, OpenCode, Codex, T3.

## 3. Job states, steps, error codes

States (SPEC §8): `accepted → preflight → backup → updating → verifying → succeeded`.
Terminal alternatives: `blocked`, `failed`, `health_failed`, `interrupted`.
`NONTERMINAL = {accepted, preflight, backup, updating, verifying}`.
Single active slot enforced by constant-expression partial unique index
(`ON jobs((1)) WHERE state IN NONTERMINAL`).
`recovery_required` flag blocks new jobs independently of terminal state.
One admission path: `admission.admit()` (replay-first; gates; atomic
job INSERT + plan used_at + mutation lease + event). One-shot execution:
`jobs.dispatch_nonce` claimed atomically with unit+release+deadline
(single-statement predicate); runner consumes `attempt_claimed` before
opening logs. `execution_leases` arbitrate probes vs mutation
(`leases.py`); mutation leases never time-expire, probe leases are
bounded and reclaimable. Terminal DB state never releases mutation
ownership: only `tx.release_ownership()`, called by a reconciler after
proving unit confirmed-stopped + no execution-marked processes + no
unresolved delegated mutation, disposes the lease (F02/G02/G03). A
valid receipt proves outcome, never quiescence. Runner argv is
`runner <job-id> <nonce>` and refuses
mismatches with exit 6 before any mutation.

Steps mirror states plus `not_applicable` only when the adapter declares it in `plan()`
before execution. Step timeouts come from adapter `plan().timeouts`.

Error codes (API `code` field + adapter `error_code`):
`worker_unavailable, busy, stale_plan, fingerprint_changed, config_changed,
activity_blocked,
ack_required, disk_blocked, git_dirty, install_method_unsupported, backup_unsupported,
backup_failed, install_failed, health_failed, timeout, log_limit_truncated,
storage_failure, recovery_required, invalid_request, unauthorized, forbidden,
conflict, not_found, unavailable, maintenance, rate_limited`.

Script exit codes (spec_scripts.md §4): `0` verified success or already-current,
`2` invalid request, `3` blocked, `4` installation failure, `5` verification
failure, `6` interrupted/recovery required.

## 4. API contract (all under `/api/v1`, `Cache-Control: no-store`)

| Method + route | Notes |
| --- | --- |
| `GET /session` | owner identity + CSRF token |
| `GET /tools` | four cards, independent observations + freshness |
| `POST /tools/{id}/check` | bounded read-only refresh; cached `stale`/`updating` during mutation |
| `POST /tools/{id}/plans` | v2 immutable preview (plan_version 2, hash-bound, config+release bound, single-use); `unknown` activity allowed without ack (ack enforced at job creation); owner probes via typed queue (bounded wait, event loop free); 15-min discovery coalesce (`?force=1` bypass, explicit Check again always forces); 409 without probing when a job is active or recovery is required; 503 when drained |
| `POST /jobs` | idempotency lookup FIRST (replay returns recorded job incl. during drain/recovery; changed payload → 409); then drain 503, worker-readiness 503, plan subject/expiry/one-use, authoritative owner fingerprint + config/release revalidation (409 fingerprint_changed/config_changed), activity ack; `202` new job, `200` idempotent replay with `replayed:true`; fingerprint compared against `tools.fingerprint` (adapter value — `install_identity` is display-only) |
| `GET /jobs?cursor=&limit=` | paginated history, default 25, max 100; `?active=true` returns nonterminal only (global gating poll) |
| `GET /jobs/{id}` | state, step, timings, checks, backup summary, errors |
| `GET /jobs/{id}/logs?after=&limit=` | ordered redacted records, next cursor; `has_more` = page continuation, `truncated` strictly = storage-cap loss; log seq starts at 1 |
| `GET /health` | authenticated API/DB/worker readiness; worker liveness from `<state_dir>/dispatcher.heartbeat` file (fresh ≤20s), never inferred from job absence |

Errors: `{code, message, details, request_id}`. `401` auth, `403` identity/CSRF,
`409` busy/stale/conflict, `422` invalid input, `503` worker/storage,
`429` per-identity rate limit (`code: rate_limited`).
No secrets in `details`. Mutations require JSON, exact allowed Origin,
`X-CSRF-Token` bound to subject. CORS disabled. Never mutate via GET.

## 5. Adapter interface (`backend/app/adapters/base.py`)

Each adapter implements `inspect()`, `discover()`, `activity()`, `plan()`,
`backup()`, `execute()`, `verify()` per SPEC §5. Browser input never supplies
paths, env, service names, URLs, or argv. All subprocesses use fixed argument
arrays with `shell=False`. Pydantic models for every result are defined in
`base.py`; concrete adapters return those models only.

Target semantics: exact-version adapters install the planned target;
`native_latest` adapters (Codex) record observed candidate + actual result and
the UI discloses main/latest-tracking behavior.
Central runner rule: for `target_mode=exact`, `after_version != plan.target`
is mandatory verification failure (`health_failed/exact_target_mismatch`).
Adapter mutation subprocess timeout = step timeout +
`registry.MUTATION_TIMEOUT_MARGIN_S` (120s); any adapter timeout funnels into
the runner hard-timeout path (systemd unit termination, survivor verify,
`recovery_required` when unproved). `ExecuteResult.timed_out` marks it.

## 6. Disk / backup floors

Default floor is `max(3 GiB, estimated staging + required backup + 1 GiB reserve)`
on every affected filesystem (spec_scripts.md §10 supersedes the SPEC §9 2 GiB
example). Unknown estimates block. No wholesale archiving of the observed
4.8 GiB Hermes home / 13 GiB OpenCode data / 3.7 GiB Codex home by default.

## 7. Paths (deferred to deploy config; defaults)

- Release: `/opt/ega-update/releases/<commit>/`, pointer `/opt/ega-update/current`
- Config: `/etc/ega-update/` (secrets outside shared storage; `config.json`
  owner-managed, `api.env`/`worker.env` per-service overrides, `csrf.secret`
  consumed when `csrf_secret` unset, `secrets.env` for known-secret values)
- State: `/var/lib/ega-update/state.db` (SQLite WAL, outside releases)
- Logs: `/var/lib/ega-update/logs/<job-id>.jsonl`
- Backups: `/var/lib/ega-update/backups/`
- Dispatcher heartbeat: `/var/lib/ega-update/dispatcher.heartbeat` (`{ts, pid}`)
- Drain flag: `/var/lib/ega-update/drain` (presence refuses new plans/jobs: 503)
- Log caps: 20 MiB/job, 500 MiB total, 30 days completed logs, 90 days metadata.

## 9. Corrective architecture (senior review R01–R36; precedence: canonical specs > findings > this file)

- Owner boundary: API (ega-update) never probes `/home/ubuntu`. Typed probe
  queue tables `probe_requests`/`probe_results`; dispatcher (ubuntu) executes
  ops `inspect|discover|activity|plan|verify`; API waits bounded (threadpool),
  else cached+stale. Probe/mutation exclusion owned by dispatcher.
- Canonical env (`owner_env.py::build_owner_contract`): ONE typed
  OwnerExecutionContract (uid/gid/user/home/path/config/release/venv/
  node/npm/npx/xdg/dbus/locale/nnp-policy/manager-scope/config-identity/
  sudo-profile); runner launch args, probe launch args, and environment
  fingerprint all derive from it — never reconstructed per module.
  Privilege truth (G04/H04): owner execution is NNP-off
  (`runner/probe_no_new_privileges=false`,
  `phase_privilege_source=runner`,
  `privilege_profile=owner-exec-nnp-off`) so the inventoried Hermes
  sudo path can elevate; the fingerprint binds it. Authoritative probes
  run as transient probe SERVICES with the same NNP-off properties as
  the runner service (never inherited scopes from the NNP-on
  dispatcher); phase scopes inherit the runner context. Release pointer
  resolved once per job into `jobs.release_path`;
  preview and apply share it; the phase worker recomputes and refuses
  on mismatch.
- Attempts: `jobs.attempt_nonce` (claim token) + `attempt_claimed` consumed
  atomically with expected state+unit+release; units named
  `ega-update-job-<32hex-uuid>.service`. Dispatcher lock handle held for life.
- Unit model (`units.py`): live|starting|stopping|confirmed_stopped|unknown
  from `systemctl --user show`; unknown never releases reservation.
- State machine (`tx.py`): single explicit transaction per transition incl.
  recovery flag/checks/event/reservation; guarded by prior state+attempt.
  `jobs.unresolved` marker persisted before mutation.
- Supervisor: phases run via `executor.py` streaming Popen in per-phase
  `--scope` units; runner (coordinator) never inside the killed scope;
  delegated service ops tracked explicitly; uncertainty keeps recovery.
- Receipts v2 (`receipts.py`): binds job/tool/plan/plan-hash/attempt/release/
  target/checks-manifest/installer-exit/versions/evidence/backup/cleanup;
  success requires exit 0 + exact-target + all mandatory pass + durable
  evidence; binding checked against DB row + filename; atomic apply.
- Evidence (`sanitize.py`): one sanitizer for every persisted/returned string;
  streaming `SanitizingStream` (incremental UTF-8, withheld sensitive blocks,
  fail-closed, no raw fallback); mutation gated on log init; mid-op evidence
  failure stops toward recovery.
- Plans v2 (`plans.py`): immutable versioned contracts (identity, fingerprint,
  config hash, services/launch/homes, backup policy, probes manifest, budgets,
  deadlines, release); one-use (`used_at`); admission revalidates against
  fresh owner inspection + worker readiness + claim deadline; idempotent
  replay precedes all admission conditions.
- Config/secrets (`readiness.py`): placeholders fail readiness; per-service
  secret files with validated readability; one env-style parser
  (`sanitize.parse_secrets_content`); `api.env`/`worker.env` non-secret only.
  A configured-but-broken secret source raises `SecretSourceError` and
  fails durable evidence closed (G05); only an unconfigured source
  validly yields an empty set.
- Migrations: `backend/migrations/NNN_*.sql` single ordered source +
  `schema_migrations` ledger; code version `CODE_VERSION`; startup validates,
  deploy migrates; rollback compat from ledger.
- CLI (`backend/app/cli.py`, `python -m backend.app.cli`): JSON envelope,
  exits 0/2/3/4/5/6 preserved via `exec` wrappers; `apply` = reserve +
  observe canonical dispatch, never direct runner.
- Retention (`retention.py`): 500MiB/30d/90d/latest-two-backups + tombstones;
  protects active/recovery/unresolved; dispatcher invokes daily.
- Logs: `final_log_seq` published after durable flush; UI drains to terminal
  cursor; read/mutation rate buckets separate with Retry-After.

## 8. Canonical job launch (exactly one mechanism; units use full UUID hex)

Dispatcher (system service, `User=ubuntu`, lingering enabled) launches each job
via ubuntu's user manager only:
`systemd-run --user --collect --unit=ega-update-job-<32hex>.service
--working-directory=<resolved-release> --setenv=EGA_CONFIG_FILE=...
--setenv=EGA_ATTEMPT_NONCE=... --setenv=<allow-listed owner env>
--property=KillMode=control-group
--property=Restart=no <release-venv-python> -m backend.app.worker.runner <job-id> <nonce>`.
Launch is proved (exit status + `systemctl --user is-active`); otherwise the
job is `blocked/worker_unavailable`. No system-manager fallback, no sudo.
Continuous per-loop reconcile applies validated completion receipts
(`backend/app/receipts.py`, schema v2) or marks `interrupted`; never reruns.

## 10. Dependency pins (verified 2026-09-08, see `docs/DEPENDENCIES.md`)

Frontend: `react==19.2.8`, `react-dom==19.2.8`, `@types/react==19.2.18`,
`@types/react-dom==19.2.7`, `vite==8.1.5`, `@vitejs/plugin-react==6.1.1`,
`typescript==7.0.2`. Backend: `fastapi==0.136.1`, `pydantic==2.13.5`,
`pydantic-settings==2.15.0`, `uvicorn==0.52.4`, `PyJWT==2.13.0` (+`cryptography`
via `PyJWT[crypto]`), `requests`+`cryptography` resolved at deploy (no invented lockfile).
Python `>=3.10,<3.14`; Node `24.18.0`; npm `11.19.1` (shared runtimes untouched).
No lockfile contents invented; `pip freeze`/`npm` lock generation is deferred to
the deploy environment and documented.

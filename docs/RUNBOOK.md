# EGA Update Console — Operator Runbook (V1)

Scope: SPEC §12 deployment and recovery. All procedures below work over **SSH
or the provider console**, independent of the managed tools and of the web
path. If the dashboard is unreachable, start here — never through a managed
tool's own CLI wrappers.

> Status: implementation artifact. Not E2E-verified, not production-ready.
> No fabricated passes: untested steps are marked as such.

## Service names and paths (§7)

| Item | Value |
| --- | --- |
| API unit | `ega-update-api.service` (user `ega-update`, loopback `127.0.0.1:8771`) |
| Dispatcher worker unit | `ega-update-worker.service` (user `ubuntu`, singleton via `worker.lock`) |
| Per-job runner | transient `ega-update-job-<32hex-uuid>.service` via `systemd-run --user --collect` as `ubuntu` (user manager; full job-UUID hex, never shortid; runner templates document the transient shape only and are never started directly) — inspect with `systemctl --user` as `ubuntu` (see §5 for XDG_RUNTIME_DIR context) |
| User-manager linger | `loginctl enable-linger ubuntu` REQUIRED (idempotent; see install.sh §4b) or `--user` job units + T3 user services die with the last SSH session |
| Drain file | `<state_dir>/drain` (`/var/lib/ega-update/drain`): API refuses new plans/jobs while present; absent by default |
| Tunnel unit | `cloudflared-ega-update.service` (dedicated hostname → `127.0.0.1:8771`; placeholder config fail-closed, see §1) |
| Release / pointer | `/opt/ega-update/releases/<commit>/` · `/opt/ega-update/current` |
| Config / secrets | `/etc/ega-update/` (`config.json` 0640; `*.secret`/`tunnel.env` 0600) |
| Env files | `/etc/ega-update/api.env` · `/etc/ega-update/worker.env` |
| State DB | `/var/lib/ega-update/state.db` (SQLite WAL, outside releases) |
| Worker lock | `/var/lib/ega-update/worker.lock` |
| Job logs | `/var/lib/ega-update/logs/<job-id>.jsonl` (+ `<job-id>.receipt.json` when proven) |
| Backups | `/var/lib/ega-update/backups/` |
| Conflicting-automation records | `/var/lib/ega-update/conflicting-automation/` |

Global rules (SPEC §8–§9):

- **No auto-rerun.** A failed/interrupted/timeout job is never retried
  automatically. The operator reconciles, then creates a fresh plan + job.
- **No false success.** Missing required verification cannot produce
  `succeeded`. Installer nonzero exit is `failed` even if the old version
  still runs; zero exit + mandatory check failure is `health_failed`.
- Terminal job history is never rewritten by refreshes; health observations
  and job outcomes stay independent.

## 0. Triage order

1. Can you SSH in? If not, use the provider console (all steps below work there).
2. Drain present? `ls -l /var/lib/ega-update/drain` — when present the API
   refuses new plans/jobs (admission stopped for upgrade/maintenance, see
   §10). Do NOT remove it until quiescence + healthy start are proven.
3. `loginctl show-user ubuntu -p Linger` must be `yes`; if not, `--user` job
   units cannot survive logout — re-run `sudo loginctl enable-linger ubuntu`.
2. `systemctl is-active ega-update-api ega-update-worker cloudflared-ega-update`
3. `journalctl -u ega-update-api -n 50 --no-pager`; same for `-worker`, `cloudflared-ega-update`.
4. Any nonterminal job? Check SQLite (read-only):
   `sqlite3 /var/lib/ega-update/state.db "SELECT id,tool_id,state,runner_unit,recovery_required FROM jobs WHERE state IN ('accepted','preflight','backup','updating','verifying');"`
5. Any `recovery_required=1`? Then §6 before any new job.

## 1. Access / tunnel failure (web path down, SSH works)

Symptoms: browser shows Access error / tunnel error / timeout; SSH fine.

1. Tunnel unit: `systemctl status cloudflared-ega-update --no-pager`; `journalctl -u cloudflared-ega-update -n 100 --no-pager`.
2. Config: `/etc/ega-update/cloudflared/config.yml` must route **only** the
   dedicated hostname to `http://127.0.0.1:8771`. No other ingress.
   Install.sh generates this file with an explicit placeholder hostname
   (`CHANGEME-*`, fail-closed): the service refuses to route while any
   placeholder remains — replace hostname + tunnel ID + credentials file
   before enabling. Credentials live in
   `/etc/ega-update/cloudflared/credentials.json` (0600) with optional token
   overrides in `/etc/ega-update/tunnel.env` (0600, via `EnvironmentFile`
   in the unit; never logged).
3. API loopback: `ss -ltnp | grep 127.0.0.1:8771` (or configured
   `EGA_LISTEN_PORT` from config `listen_port`, rendered at install into
   `/etc/systemd/system/ega-update-api.service.d/10-port.conf` as
   `Environment=EGA_LISTEN_PORT=<from config>` — the validator enforces
   config vs effective-port consistency). If the API listens on anything
   else, stop it and fix `listen_host` to `127.0.0.1` — never publish the
   API directly.
4. Access: confirm the Cloudflare Access policy still gates the hostname
   (owner identities only) and `team_domain`/`audience` in
   `/etc/ega-update/config.json` match the policy. The API validates
   `Cf-Access-Jwt-Assertion` itself (RS256, fixed audience, expiry,
   fail closed) — an Access misconfiguration surfaces as 401/403 in the API
   log, not as a bypass.
5. Cache: API responses use `Cache-Control: no-store`; the zone must bypass
   cache for `/api/*`. A stale dashboard after a job change is a cache-rule
   bug, not ground truth — verify via SSH/SQLite.
6. Restart order: `systemctl restart ega-update-api`, then
   `systemctl restart cloudflared-ega-update`. Preserve SSH throughout.

## 2. Stopped API (`ega-update-api.service` inactive)

1. `systemctl status ega-update-api --no-pager`; `journalctl -u ega-update-api -n 100 --no-pager`.
2. Common causes: bad `/etc/ega-update/api.env` or `config.json`
   (permissions: `config.json` 0640 `root:ega-update`, secrets 0600),
   port already taken, broken `current` symlink, venv missing.
3. Fix config/symlink, then `systemctl start ega-update-api`.
4. Verify: `ss -ltnp | grep 127.0.0.1:8771` and an authenticated
   `GET /api/v1/health` (unauthenticated reads must still 401/403).
5. Note: an API restart never disturbs a running job — the runner is an
   independent transient unit and the dispatcher resumes observation.

## 3. Stopped worker (`ega-update-worker.service` inactive)

1. `systemctl status ega-update-worker --no-pager`; logs as above.
2. If it exits with "another dispatcher holds the worker lock", a second
   dispatcher is running — do NOT kill the lock file; find the duplicate
   (`systemctl status`, `ps`) and stop it.
3. `systemctl start ega-update-worker`. On start it runs `reconcile_boot()`:
   nonterminal jobs with no live runner unit and no completion receipt become
   `interrupted` + `recovery_required=1`. This is expected after a crash or
   VM reboot — continue to §6. Never delete the lock to "fix" this.

## 4. Disk exhaustion (3 GiB floor semantics)

Pre-mutation gate: `max(3 GiB configured floor, estimated staging +
required backup + 1 GiB reserve)` free on **every** affected filesystem.
Unknown estimates block. Disk pressure blocks updates; it never silently
deletes recovery data.

Diagnose: `df -h / /var/lib/ega-update /opt/ega-update /home/ubuntu`;
`du -sh /var/lib/ega-update/logs /var/lib/ega-update/backups`.

What you MAY delete:

- Truncated/excess log output beyond caps is already stopped by the runner;
  completed-job logs older than 30 days (retention: 30 days completed logs,
  90 days metadata, 500 MiB total, 20 MiB/job).
- Older completed backups beyond the latest two per tool — **except** below.

What you must NEVER delete:

- Active-job data (logs/receipt of any nonterminal job).
- The current job's backup.
- Unresolved-failure backups (any job with `recovery_required=1` or an
  unreconciled `interrupted`/`failed`/`health_failed` under investigation).
- Never `rm -rf` tool home dirs (4.8 GiB Hermes home / 13 GiB OpenCode data
  / 3.7 GiB Codex home are NOT archived wholesale and must not be "cleaned"
  to make space).

After freeing space, re-run the failed step via a fresh plan — never by
hand-editing job state.

## 5. Interrupted jobs + partial tool updates

Context: runner crash, VM reboot, hard timeout, or dispatcher restart marks
the job `interrupted` (+ `recovery_required=1`) unless a persisted
completion receipt proves the outcome. A hard timeout kills the job cgroup
(SIGTERM, then SIGKILL after the grace period) and records *possible partial
installation* — assume the tool may be half-updated.

Procedure (SSH only — run as root or with sudo; user-manager commands run as `ubuntu`):

1. Inspect the recorded unit and processes (read-only). The recorded
   `jobs.runner_unit` is the full-UUID transient name
   `ega-update-job-<32hex-uuid>.service` (never shortid). User-manager
   commands MUST run as `ubuntu` with its runtime dir — either login shell
   form or explicit XDG_RUNTIME_DIR:
   `sudo -u ubuntu -i systemctl --user status ega-update-job-<32hex-uuid>.service --no-pager`;
   `sudo -u ubuntu -i systemctl --user show ega-update-job-<32hex-uuid>.service -p ActiveState,SubState,MainPID,Result`;
   or equivalently
   `sudo -u ubuntu XDG_RUNTIME_DIR=/run/user/$(id -u ubuntu) systemctl --user is-active ega-update-job-<32hex-uuid>.service`;
   `ps -ef | grep -i <tool>` — distinguish *absent / healthy / stale /
   failed probe*; running processes do not prove health. Never use the
   system manager (`systemctl status` without `--user`) for job units.
2. Run the SSH-only reconcile command (from `/opt/ega-update/current`,
   EGA_CONFIG_FILE exported):
   `EGA_CONFIG_FILE=/etc/ega-update/config.json venv/bin/python -m backend.app.cli reconcile --job-id <uuid>`
   (equivalent worker path: `venv/bin/python -m backend.app.worker.reconcile --job-id <uuid>`).
   It prints a JSON envelope plus a verdict and records a `reconcile`
   event. With no updater remaining it tells you to re-run with
   `--clear-recovery`.
3. Only after proving no updater remains (unit inactive, MainPID dead, no
   job processes):
   `EGA_CONFIG_FILE=/etc/ega-update/config.json venv/bin/python -m backend.app.cli reconcile --job-id <uuid> --clear-recovery`
   Heartbeat age alone NEVER justifies clearing.
4. Assess partial installation with the tool's read-only probes (`--version`,
   diagnostics, unit states per `docs/INVENTORY.md`). If partially updated,
   plan the next update from the *observed* state with a fresh plan.
5. Manual health refresh (preserves terminal history):
   `POST /api/v1/tools/{id}/check` from the dashboard, or the adapter
   `verify` path — this writes a new observation, never rewrites the
   terminal job.

## 6. Recovery block (`recovery_required=1` — new jobs get 409)

New jobs are blocked until the SSH reconcile above clears the flag.
The dashboard shows which job holds the block; terminal state alone does
not clear it. Do not hand-edit `recovery_required` in SQLite — use the
reconcile command so the `recovered` event is recorded.

## 7. Console code rollback (schema-compatibility rule)

- Compatible (new release's `schema_meta.version` == current): re-point and restart:
  `sudo ln -sfn /opt/ega-update/releases/<prev-commit> /opt/ega-update/current &&
   sudo systemctl restart ega-update-api ega-update-worker`
- Incompatible / migrate failed: follow the **migration-recovery procedure**:
  1. Stop console services. 2. Restore the pre-upgrade backup
     (`/var/lib/ega-update/backups/state-preupgrade-<ts>.db`) to
     `/var/lib/ega-update/state.db` via `sqlite3` (never bare `cp` over a
     live DB — stop writers first). 3. Re-point `current` at the previous
     release. 4. Restart and verify `/api/v1/health`.
- Console rollback NEVER restores tool data. Tool backups in
  `/var/lib/ega-update/backups/<job-id>/` are only used via an explicit
  manual procedure, per-job, after reconcile.

## 8. DB / log recovery

- DB: SQLite WAL on a local filesystem. On `database is locked`/corruption:
  stop API+worker, take a file-level copy aside, then
  `sqlite3 state.db "PRAGMA integrity_check;"`. Restore from the newest
  `backups/state-pre*.db` only via the sqlite3 backup/recovery steps above.
  During execution the runner preserves its redacted receipt on disk where
  possible — a missing receipt after a crash means the outcome is unproved.
- Logs: JSONL per job, flushed ≥1/s with sequence numbers and an explicit
  gap/truncation marker. A partial final record after a crash is tolerated;
  `GET /api/v1/jobs/{id}/logs?after=&limit=` replays from retained history
  (poll every 2 s while active).

## 9. Manual `Check again` after SSH repair

After any SSH-side fix (restarted gateway, repaired unit, freed disk):
use the dashboard `Check again` (`POST /api/v1/tools/{id}/check`) for a
bounded read-only refresh. During an active mutation, checks return cached
observations labeled `stale`/`updating` and must not launch contending
probes. Discovery refresh is cached 15 min. A manual check never rewrites
terminal job history and never proves model inference — only the named
health checks listed on the tool card.

## Appendix: quick command reference

```bash
systemctl is-active ega-update-api ega-update-worker cloudflared-ega-update
sudo -u ubuntu -i systemctl --user is-active ega-update-job-<32hex-uuid>.service
# equivalent without a login shell (XDG_RUNTIME_DIR context required for --user):
sudo -u ubuntu XDG_RUNTIME_DIR=/run/user/$(id -u ubuntu) systemctl --user show ega-update-job-<32hex-uuid>.service -p ActiveState,SubState,MainPID,Result
loginctl show-user ubuntu -p Linger
journalctl -u ega-update-api -u ega-update-worker -u cloudflared-ega-update -n 100 --no-pager
ss -ltnp | grep 127.0.0.1:8771
sqlite3 /var/lib/ega-update/state.db "SELECT id,tool_id,state,recovery_required,runner_unit FROM jobs ORDER BY created_at DESC LIMIT 10;"
ls -l /var/lib/ega-update/drain
cd /opt/ega-update/current && EGA_CONFIG_FILE=/etc/ega-update/config.json venv/bin/python -m backend.app.cli reconcile --job-id <uuid>
cd /opt/ega-update/current && EGA_CONFIG_FILE=/etc/ega-update/config.json venv/bin/python -m backend.app.cli status
```

## 10. Admission drain, Hermes sudoers, lockfiles (release gates)

- **--user launch model + linger.** Job runners launch via the single
  canonical dispatch `systemd-run --user ... runner <job-id> <nonce>` as
  `ubuntu` into the ubuntu user manager (full-UUID unit
  `ega-update-job-<32hex-uuid>.service`; runner templates document this
  transient shape only and are never started directly). Inspect job units
  with `systemctl --user` as `ubuntu` — always with the user runtime
  context: `sudo -u ubuntu -i systemctl --user ...` or
  `sudo -u ubuntu XDG_RUNTIME_DIR=/run/user/$(id -u ubuntu) systemctl --user ...`
  (reconcile.py and `cli status`/`reconcile` do this internally).
  `loginctl enable-linger ubuntu` is required (idempotent check in
  install.sh §4b) so the manager exists at boot for `--user` job units +
  T3 user services. Without linger the manager dies with the last SSH
  session.
- **Drain procedure (maintenance protocol, 11 steps).** `<state_dir>/drain`
  (`/var/lib/ega-update/drain`, resolved from config `state_dir` — never
  hardcoded in deploy scripts) blocks new plans/jobs (API refuses while
  present with 503). Absent by default. Admission stop:
  `sudo touch /var/lib/ega-update/drain`. Re-admit ONLY after quiescence
  (`python3 deploy/etc/quiescence-check.py --config
  /etc/ega-update/config.json` proves worker alive, no
  active/unresolved/recovery work, no live/unknown runner units, no
  active delegated operations, drain present — bounded 120s, fail
  closed; version-independent, needs no installed release) + proven stop +
  consistent backup + stage + validator + migrate + atomic switch + units
  (+ port drop-in) + start + bounded readiness (curl localhost health +
  `cli status --require-ready` worker alive):
  `sudo rm -f /var/lib/ega-update/drain`. install.sh (existing-deploy
  path) and upgrade.sh create the drain FIRST, prove quiescence via the
  deploy controller, prove services stopped (fail closed, abort, keep
  drain), and remove the drain only on the success path. Failures keep
  the drain; the PRIOR release symlink is restored ONLY when
  `validate-release.py --check-compat OLD NEW` passes, else the host stays
  in manual-recovery state (never "start same broken release" as
  rollback).
- **Hermes sudoers snippet.** The Hermes adapter never uses a generic
  `sudo -n true` proof. Install the narrowly-scoped allow-list:
  `sudo cp deploy/etc/sudoers.d/ega-update-hermes.example
  /etc/sudoers.d/ega-update-hermes` (replace every `CHANGEME-*` unit with
  the inventoried Hermes units from `settings.service_units` /
  inventory.json), then `sudo chown root:root` + `chmod 0440` +
  `sudo visudo -c`. The adapter probes ONLY
  `sudo -n systemctl --no-pager show <unit>` in that exact shape, else
  `BLOCKED_RESTART_AUTHORITY` before mutation.
- **Cloudflared placeholder config.** `install.sh` writes
  `/etc/ega-update/cloudflared/config.yml` with an explicit placeholder
  hostname only; routing stays disabled until the owner replaces
  hostname/tunnel/credentials (service validates non-placeholder before
  routing). Token overrides live in `/etc/ega-update/tunnel.env` via
  `EnvironmentFile` (never logged).
- **Lockfiles remain a release gate (not fabricated).** The singleton
  `worker.lock` and per-job `execution.lock` + cgroup containment are
  release gates: a second dispatcher/runner exits instead of duplicating
  work. Never delete a lock to "fix" a stuck job — reconcile the recorded
  `--user` unit + receipt first (§5–§6).
- **Placeholder validation (fail closed).** `deploy/etc/validate-release.py
  --release DIR --config FILE` (exit 0 ok / 3 blocked+reason) gates every
  stage/switch: it imports `backend.app.db` from the release root on
  sys.path for the migration ledger, rejects any CHANGEME/placeholder
  secret in the effective config (inline `csrf_secret` or its
  `csrf_secret_file` fallback), enforces `listen_port` consistency
  (config vs api-unit default, honoring the install-rendered drop-in
  `10-port.conf`), requires a non-placeholder tunnel hostname plus a
  credentials file at mode ≤0600, checks the secrets owner:group/mode
  table, requires staged `backend/app/static/index.html`, verifies the
  stage-time `MANIFEST` (sha256 of `backend/**` written via sha256sum —
  the validator only verifies, never writes), and requires hashed
  requirements (`pip --require-hashes` REQUIRED; a hashless file blocks
  with a message — hashes are generated during authorized release prep,
  never fabricated). Tunnel pre-start (`ExecStartPre`) runs the same
  validator with `--only tunnel` under the release venv python (missing
  venv refuses start — no fallback). Rollback compat:
  `validate-release.py --check-compat OLD NEW` must pass before the prior
  symlink is restored.
- **Retention / tombstone ops.** Dispatcher calls
  `backend/app/retention.py:run_retention(conn, settings)` daily (main
  agent wires the hook); operators can also run
  `EGA_CONFIG_FILE=/etc/ega-update/config.json venv/bin/python -m
  backend.app.cli retention` (JSON envelope). Policy: 500 MiB total
  completed-log cap (oldest first), 30d completed logs, 90d metadata
  (jobs/checks/events/plans rows for terminal jobs), latest-two completed
  backups per tool, expired unused plans, old receipts
  (`<job-id>.receipt.json`). NEVER touched: nonterminal jobs,
  `recovery_required` jobs, `unresolved=1` jobs, backups referenced by
  protected jobs. Every removal writes a `tombstones` row
  (`kind, ref, reason, created_at`):
  `sqlite3 /var/lib/ega-update/state.db "SELECT kind,ref,reason,created_at
  FROM tombstones ORDER BY created_at DESC LIMIT 20;"`.
  UI behavior (API out of scope): the LogViewer shows a "retention gap"
  note when `has_more` is false but `next_after < final_log_seq` — records
  in between were removed by retention; shown records are ordered but not
  complete across the gap. See frontend `LogViewer.tsx` and `retention.py`.

## Notes (clarifications only — procedures above are unchanged)

- Runner unit naming: the dispatcher (as `ubuntu`) records the transient
  `ega-update-job-<32hex-uuid>.service` user unit name in
  `jobs.runner_unit` (full job-UUID hex without dashes, spawned via the
  canonical `systemd-run --user --collect ... runner <job-id> <nonce>`
  dispatch; the runner refuses nonce mismatches with exit 6). Both runner
  templates (`systemd/ega-update-runner@.service` and
  `systemd/user/ega-update-runner@.service`, the latter installed to
  `ubuntu`'s `~/.config/systemd/user`) document this transient shape ONLY
  — their `ExecStart` carries the two-arg `<job-id> <nonce>` form and they
  are never started directly (the obsolete 1-arg `%i`-only form is gone).
  Reconcile resolves the recorded `runner_unit` always via
  `systemctl --user` as `ubuntu` with XDG_RUNTIME_DIR context.
- Tunnel independence: `cloudflared-ega-update.service` uses `Wants`
  (never `BindsTo`) + `Restart=always` + `RestartSec`, so API
  restarts/reinstalls never permanently stop the tunnel — it reconnects
  once the API is healthy. `ExecStartPre` runs the validator tunnel checks
  under the release venv python (missing venv refuses start).
- Frontend build output: the release gate checks
  `backend/app/static/index.html` — `frontend/vite.config.ts` sets
  `outDir` to `../backend/app/static` and `backend/app/main.py` serves
  that directory (not `frontend/dist/`).
- Config ownership: `/etc/ega-update/config.json` is the owner-managed
  shared file; `/etc/ega-update/api.env` and `worker.env` carry
  per-service overrides + secrets only (units set `EGA_CONFIG_FILE`
  before `EnvironmentFile` so env-file keys still win).

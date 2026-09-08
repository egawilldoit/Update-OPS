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
| Per-job runner | transient `ega-update-job-<shortid>.service` via `systemd-run --collect` (template: `ega-update-runner@.service`) |
| Tunnel unit | `cloudflared-ega-update.service` (dedicated hostname → `127.0.0.1:8771`) |
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
3. API loopback: `ss -ltnp | grep 127.0.0.1:8771` (or configured
   `EGA_LISTEN_PORT`). If the API listens on anything else, stop it and fix
   `listen_host` to `127.0.0.1` — never publish the API directly.
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

Procedure (SSH only):

1. Inspect the recorded unit and processes (read-only):
   `systemctl status ega-update-job-<shortid> --no-pager`;
   `systemctl show ega-update-job-<shortid> -p ActiveState,SubState,MainPID,Result`;
   `ps -ef | grep -i <tool>` — distinguish *absent / healthy / stale /
   failed probe*; running processes do not prove health.
2. Run the SSH-only reconcile command (from `/opt/ega-update/current`):
   `venv/bin/python -m backend.app.worker.reconcile --job-id <uuid>`
   It prints a verdict and records a `reconcile` event. With no updater
   remaining it tells you to re-run with `--clear-recovery`.
3. Only after proving no updater remains (unit inactive, MainPID dead, no
   job processes):
   `venv/bin/python -m backend.app.worker.reconcile --job-id <uuid> --clear-recovery`
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
journalctl -u ega-update-api -u ega-update-worker -u cloudflared-ega-update -n 100 --no-pager
ss -ltnp | grep 127.0.0.1:8771
sqlite3 /var/lib/ega-update/state.db "SELECT id,tool_id,state,recovery_required,runner_unit FROM jobs ORDER BY created_at DESC LIMIT 10;"
cd /opt/ega-update/current && venv/bin/python -m backend.app.worker.reconcile --job-id <uuid>
```

## Notes (clarifications only — procedures above are unchanged)

- Runner unit naming: the dispatcher records a transient
  `ega-update-job-<shortid>` unit name in `jobs.runner_unit`
  (first 8 chars of the job UUID, spawned via `systemd-run --collect`).
  Sites that prefer template instances may use
  `ega-update-runner@<jobid>.service` instead (see
  `systemd/ega-update-runner@.service`). Reconcile handles both naming
  schemes when resolving the recorded `runner_unit`.
- Frontend build output: the release gate checks
  `backend/app/static/index.html` — `frontend/vite.config.ts` sets
  `outDir` to `../backend/app/static` and `backend/app/main.py` serves
  that directory (not `frontend/dist/`).
- Config ownership: `/etc/ega-update/config.json` is the owner-managed
  shared file; `/etc/ega-update/api.env` and `worker.env` carry
  per-service overrides + secrets only (units set `EGA_CONFIG_FILE`
  before `EnvironmentFile` so env-file keys still win).

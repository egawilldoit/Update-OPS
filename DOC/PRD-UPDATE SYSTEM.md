# EGA Update Console: V1 PRD

Status: ready for implementation, subject to VM inventory.
Date: 2026-09-08.
Companion: [Technical specification](<SPEC-UPDATE SYSTEM.md>).

## Goal

Give one owner a private web page to manually update Hermes, OpenCode, Codex, and T3 on one Ubuntu VM, follow logs, and see the verified result. The owner repairs failures through SSH and refreshes the dashboard afterward.

Success means all four real installations can complete this flow. A mock dashboard or one working adapter is not a complete V1.

## Deployment decision

Host the frontend, API, worker, database, and logs on the existing VM. Cloudflare Access provides login. Cloudflare Tunnel connects the private local application to a dedicated hostname. Cloudflare does not execute updates.

The dashboard runs independently of the managed tools. If the VM is unavailable, the dashboard is unavailable. SSH or the provider console remains the recovery path.

## Scope

Include:

- One authorized owner and one VM.
- Four tool cards with installed version or commit, available target, update channel, last successful update, last attempt, current health, and check timestamp.
- Check updates, update one tool, inspect job logs, view history, and check health again.
- Persistent jobs that survive browser closure and API restart.
- One update at a time, with duplicate-request protection.
- Preflight checks, supported backups, bounded logs, and tool-specific verification.
- Responsive layout for desktop and phone, accessible controls, text labels alongside status colors.
- Deployment scripts, systemd units, configuration example, and SSH recovery runbook.

Exclude:

- Scheduled installation, automatic retries, automatic rollback, update-all, and deferred job queues.
- Browser terminal, arbitrary commands, AI repair, model inference tests, and paid API calls.
- Multi-user roles, multiple VMs, Windows, notifications, and plugin updates.
- Updating this console, cloudflared, operating system packages, or shared Node/Python runtimes through the dashboard.
- Moving tool data or replacing the existing installation method without a separate migration decision.

## User flow

1. Open the dedicated hostname and sign in through Cloudflare Access.
2. Review the four cards and their freshness timestamps.
3. Open Update for a tool. See target, channel, restart impact, backup scope, and activity result.
4. Start the update. If activity is unknown, explicitly acknowledge possible interruption. Known active work blocks the action.
5. Follow actual step names, elapsed time, and logs. Do not display invented percentages.
6. See installation result and health results separately.
7. If needed, repair over SSH and select Check again. Preserve the failed job in history.

## Product rules

- Never report healthy based only on an updater exit code.
- Never call a CLI unhealthy merely because no persistent process exists.
- Show unknown or stale when evidence is missing. Never infer idle from a running service alone.
- Last successful update changes only after installation and all mandatory checks pass.
- An update can fail while the tool remains healthy. Display both facts.
- Updates require a current server-generated plan. The browser cannot supply command text.
- No delayed surprise updates. Busy requests fail immediately.
- Closing the page does not cancel an accepted update.
- Automatic read-only discovery is allowed. Automatic installation is not.
- A supported backup is not a promise of rollback. Display its scope and location only to the owner.

## Acceptance criteria

| ID | Acceptance test |
| --- | --- |
| AC-01 | Authorized identity can access the application. Missing, expired, forged, wrong-audience, and unauthorized-identity tokens cannot read logs or start jobs. The VM web port is not publicly reachable. |
| AC-02 | All four cards match executable paths and versions independently checked on the VM. Discovery failure displays unknown and its timestamp without erasing the last observation. |
| AC-03 | Update preview states target/channel, affected services, backup scope, and activity result. Known busy work blocks execution; unknown activity requires a recorded acknowledgment. |
| AC-04 | A real update for each tool records before/after versions, steps, exit result, and mandatory health checks. An already-current installation alone does not prove update acceptance. |
| AC-05 | With two tabs submitting updates concurrently, only one job starts. Repeating the same request returns its existing job. A competing request never waits to run later. |
| AC-06 | Newly captured logs appear within 5 seconds under normal connectivity. Reopening a job restores ordered logs without duplicates. Logs show explicit truncation if retention limits apply. |
| AC-07 | Closing the browser, expiring the login, and restarting the API do not terminate an update. Login recovery returns access to the same job. |
| AC-08 | Worker crash or VM reboot cannot cause automatic rerun or false success. Recovery checks process state and leaves an interrupted job when completion cannot be proved. |
| AC-09 | A simulated installer failure and a separate post-install health failure produce different, accurate results with useful error details. |
| AC-10 | Low disk space, dirty source checkout, unsupported install method, and missing required backup capability block mutation with a reason. Existing files and Git changes remain intact. |
| AC-11 | After manual SSH repair, Check again updates current version and health while preserving the original failed attempt. |
| AC-12 | Seeded credentials, authorization headers, and terminal control sequences do not leak into saved logs, API responses, or rendered HTML. |
| AC-13 | Timeout, log limits, and database write failure do not produce success or an uncontrolled retry. No concurrent update starts while process state is unresolved. |
| AC-14 | Restarting the VM brings the console and tunnel back. A console code deployment preserves jobs and logs. Recovery instructions work through SSH independently of the managed tools. |
| AC-15 | Desktop and 390px-wide mobile flows support login, update preview, logs, and history without losing controls. Controls are keyboard accessible. |

## Delivery order

1. Record VM inventory and resolve the installation method for every tool.
2. Deliver authentication, read-only cards, and deployed access through Cloudflare.
3. Deliver durable job execution and a complete Hermes flow.
4. Complete OpenCode, Codex, and T3 adapters.
5. Exercise acceptance tests, complete recovery documentation, and deploy the verified release.

## Release evidence

Provide an acceptance table with ID, result, evidence path, tested commit, and environment. Include redacted logs for each real tool update, failure tests, security tests, browser checks, service configuration, and the live hostname.

Unit tests may use fake installers. Real VM acceptance must use actual configured installations. If a safe real update is unavailable, mark AC-04 pending rather than claiming completion.

## Inputs needed during setup

Resolve through inspection where possible: VM access, tool paths, owner account, service names, installation methods, existing automatic updaters, writable storage, and backup capabilities. Supply hostname, Cloudflare zone access, Access identity, and recovery access. Never fabricate these values.

Existing SSH stays available. Disable only identified conflicting automatic updates as part of the reviewed installation configuration.

## References

Verified 2026-09-08. Requirements above are design decisions, not claims that these products supply the console.

- [Cloudflare Tunnel](https://developers.cloudflare.com/tunnel/)
- [Cloudflare Access JWT validation](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/validating-json/)
- [Hermes updating](https://hermes-agent.nousresearch.com/docs/getting-started/updating)
- [OpenCode CLI](https://opencode.ai/docs/cli/)

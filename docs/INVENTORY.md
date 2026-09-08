# EGA Update Console — Tool Inventory Guide (V1)

Produces `inventory.json` per `deploy/etc/inventory.schema.json` (SPEC §3,
spec_scripts §11 SC-01). Inventory is **read-only evidence**: absolute
paths, versions, units, and probes. It never contains credentials, tokens,
or auth material, and it never mutates tools.

> Status: implementation artifact. Not E2E-verified, not production-ready.

## How to produce inventory.json without credentials

Run over SSH (or the provider console) as the tool-owner account (`ubuntu`).
Every command below is read-only. Redact nothing by hand — these commands
do not print secrets — but never append env dumps, unit files containing
tokens, or state files with auth material.

```bash
# 0. VM baseline
uname -a; lsb_release -a
/home/ubuntu/.nvm/versions/node/v24.18.0/bin/node --version
df -h / /home/ubuntu
id ubuntu

# 1. Per tool: executable, resolved target, owner, version
for t in codex opencode hermes t3 claude; do
  echo "== $t =="
  command -v $t || echo "(not on PATH)"
done
readlink -f /home/ubuntu/.local/bin/codex /home/ubuntu/.opencode/bin/opencode \
  /home/ubuntu/.local/bin/hermes /usr/local/bin/codex /usr/local/bin/claude
ls -l /home/ubuntu/.local/bin/codex /home/ubuntu/.opencode/bin/opencode \
  /home/ubuntu/.local/bin/hermes /usr/local/bin/codex /usr/local/bin/claude
/home/ubuntu/.local/bin/codex --version
/home/ubuntu/.opencode/bin/opencode --version
/home/ubuntu/.local/bin/hermes --version 2>/dev/null || true
git -C /home/ubuntu/.hermes/hermes-agent rev-parse HEAD 2>/dev/null || echo "(no hermes repo)"
git -C /home/ubuntu/.hermes/hermes-agent status --porcelain=v1 2>/dev/null || echo "(git state unreadable)"

# 2. Service units (system AND user scope; enabled != running)
/usr/bin/systemctl list-units --all --no-pager | grep -iE 'hermes|t3|codex|opencode' || true
systemctl --user list-units --all --no-pager 2>/dev/null | grep -iE 'hermes|t3|codex|opencode' || true
systemctl show <unit> -p LoadState,ActiveState,SubState,ExecStart,User 2>/dev/null
systemctl --user show t3code.service -p LoadState,ActiveState,SubState,MainPID 2>/dev/null || echo "(no t3 user unit)"

# 3. Runtime paths and state dirs (sizes inform backup scope, not wholesale archives)
ls -ld ~/.codex ~/.opencode ~/.hermes ~/.t3 2>/dev/null || true
du -sh ~/.hermes ~/.opencode ~/.codex 2>/dev/null || true

# 4. Overlapping automation (record-only at this stage)
crontab -l 2>/dev/null || echo "(no root crontab)"
crontab -u ubuntu -l 2>/dev/null || echo "(no ubuntu crontab)"
systemctl list-timers --all --no-pager
# tool self-update flags: record effective settings only (no changes)
```

Assemble the outputs into `inventory.json` following
`deploy/etc/inventory.schema.json` (one entry per tool with `executable`,
`resolved_target`, `install_kind`, `owner`, `version`/`commit`,
`source_status`, `runtime_paths`, `service_units`, `state_dirs`, `channel`,
`activity_probe`, `verification_probes`, `backup_procedure`, plus
`launch_method` for T3). Record cron/timers/self-update overlap under
`overlapping_automation` and the console-lock limitation under
`limitations.note`.

Validate (structural check only, no mutation):

```bash
python3 -c "import json; json.load(open('inventory.json')); print('valid JSON')"
# full schema validation against deploy/etc/inventory.schema.json with any
# Draft-07 validator before submitting as SC-01 evidence.
```

## SC-01 checklist (spec_scripts §11)

- [ ] All four required tools re-inventoried (hermes, opencode, codex, t3);
      claude recorded as optional/disabled (`BLOCKED_INSTALL_OWNERSHIP`).
- [ ] **T3 launch method**: exact recipe recorded — unit (system/user) or
      foreground/custom server, executable provenance, working directory,
      flags, port, state path, running version. A state file alone is not
      proof; state + unit + executable + process + endpoint must correlate.
      A foreground/custom server blocks the generic service update and needs
      a separate reviewed custom-service adapter.
- [ ] **Hermes system-unit restart authority**: for each inventoried
      gateway/serve unit, record scope, `User=`, `ExecStart`, active state,
      and whether `ubuntu` can restart it. If a required restart needs
      authority ubuntu lacks, the adapter blocks before mutation and a
      narrowly scoped service action is configured at deploy time — never
      `sudo` mid-job.
- [ ] One actual installation method chosen and implemented per tool;
      unsupported/ambiguous installs stay read-only with a reason.
- [ ] Existing cron, systemd timers, and tool self-update settings recorded;
      only confirmed-conflicting automation disabled, with prior config saved
      under `/var/lib/ega-update/conflicting-automation/` for restoration.
- [ ] Limitation documented: the console lock cannot protect against
      unrelated SSH commands or external updaters.

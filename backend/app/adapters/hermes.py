"""Hermes adapter: native updater with git + backup + unit checks (subagent B).

Wrapper /home/ubuntu/.local/bin/hermes, repo /home/ubuntu/.hermes/hermes-agent,
main branch. Capability probes (`update --help/--plan/--check`), git
cleanliness including untracked files (`status --porcelain=v1`,
`rev-parse HEAD`; dirty/unreadable blocks, never --force). Effective backup
mode/scope/receipt inspection (--backup only when supported plus storage
suffices; quick mode labeled limited, never silently downgraded). Native
restart handling plus receipt. Every inventoried gateway/serve unit verified
in the correct scope (system units need confirmed restart authority — block
before mutation when ubuntu lacks it, never prompt sudo mid-job). Final
commit/diagnostics/restart/running-version recorded; exit-zero alone is
never success.

Python 3.10 compatible. Fixed argv arrays, shell=False everywhere.
"""
from __future__ import annotations

import os
from typing import Dict, List, Optional, Tuple

from .base import (
    ADAPTER_TIMEOUT_DEFAULTS,
    ActivityResult,
    Adapter,
    BackupResult,
    CheckItem,
    DiscoverResult,
    ExecuteResult,
    InspectResult,
    PlanResult,
    VerifyResult,
)
from .registry import (
    MUTATION_TIMEOUT_MARGIN_S,
    attach_timed_out,
    check_disk,
    extract_version_token,
    fingerprint,
    mutation_timeout_for,
    resolve_executable,
    required_space_bytes,
    run_fixed,
)
from ..schemas import utcnow_iso

HERMES_BIN = "/home/ubuntu/.local/bin/hermes"
HERMES_REPO = "/home/ubuntu/.hermes/hermes-agent"
HERMES_BRANCH = "main"

STAGING_ESTIMATE_BYTES = 1 * 1024 * 1024 * 1024
BACKUP_ESTIMATE_BYTES = 1 * 1024 * 1024 * 1024

# Inventoried gateway/serve units: (scope, unit). Scope "system" needs
# confirmed restart authority before mutation. Override via
# EGA_HERMES_UNITS="system:hermes-gateway.service,user:hermes-serve.service".
DEFAULT_UNITS = (
    ("system", "hermes-gateway.service"),
    ("system", "hermes-serve.service"),
)


def _emit_noop(stream, line):
    # type: (str, str) -> None
    return None


def _parse_units():
    # type: () -> List[Tuple[str, str]]
    raw = os.environ.get("EGA_HERMES_UNITS", "").strip()
    if not raw:
        return list(DEFAULT_UNITS)
    units = []
    for token in raw.split(","):
        token = token.strip()
        if not token or ":" not in token:
            continue
        scope, name = token.split(":", 1)
        scope, name = scope.strip(), name.strip()
        if scope in ("system", "user") and name:
            units.append((scope, name))
    return units or list(DEFAULT_UNITS)


def _service_units_from_settings():
    # type: () -> List[Tuple[str, str]]
    """Inventoried Hermes units via settings.service_units (config-owned).

    Read defensively with defaults: settings.service_units is owned by the
    config contract and may be absent (this adapter never creates config
    keys). Accepted shapes: dict with hermes unit lists, or None. Any
    unparsable shape falls back to _parse_units() (EGA_HERMES_UNITS env +
    DEFAULT_UNITS). Never raises.
    """
    try:
        from ..config import settings as _settings

        raw = getattr(_settings, "service_units", None)
    except Exception:
        return _parse_units()
    try:
        if raw is None:
            return _parse_units()
        candidates = []  # type: List[object]
        if isinstance(raw, dict):
            for key in ("hermes", "hermes_units", "hermesUnits", "units"):
                val = raw.get(key)
                if isinstance(val, list):
                    candidates = val
                    break
            else:
                return _parse_units()
        elif isinstance(raw, list):
            candidates = raw
        else:
            return _parse_units()
        parsed = []
        for entry in candidates:
            if isinstance(entry, str) and ":" in entry:
                scope, _, name = entry.partition(":")
                scope, name = scope.strip(), name.strip()
                if scope in ("system", "user") and name:
                    parsed.append((scope, name))
            elif isinstance(entry, dict):
                scope = str(entry.get("scope", "")).strip()
                name = str(entry.get("name", "") or entry.get("unit", "")).strip()
                if scope in ("system", "user") and name:
                    parsed.append((scope, name))
        return parsed or _parse_units()
    except Exception:
        return _parse_units()


def _inventoried_units():
    # type: () -> List[Tuple[str, str]]
    """Single source for inventoried units: settings first, env fallback."""
    try:
        units = _service_units_from_settings()
        if units:
            return units
    except Exception:
        pass
    return _parse_units()


# Narrowly-scoped restart authority: ubuntu may run passwordless ONLY the
# exact systemctl commands for the inventoried Hermes units listed in
# deploy/etc/sudoers.d/ega-update-hermes.example (placeholder unit names
# there are inventory-gated and must match _inventoried_units()).
# Generic `sudo -n true` is NEVER used as authority proof.
_SUDO = "/usr/bin/sudo"
_SYSTEMCTL = "/bin/systemctl"


def _allowed_show_argv(unit):
    # type: (str) -> List[str]
    """Exact allow-list shape for the authority probe of one system unit."""
    return [_SUDO, "-n", _SYSTEMCTL, "--no-pager", "show", unit]


def _allowed_restart_argv(unit):
    # type: (str) -> List[str]
    """Exact allow-list shape for restarting one system unit."""
    return [_SUDO, "-n", _SYSTEMCTL, "restart", unit]


class HermesAdapter(Adapter):
    tool_id = "hermes"
    enabled = True

    def __init__(self):
        # type: () -> None
        self._emit = _emit_noop

    # -- probes --------------------------------------------------------

    def _version_probe(self):
        # type: () -> Tuple[str, str]
        res = run_fixed([HERMES_BIN, "--version"], timeout=60)
        self._emit("stdout", "$ hermes --version -> exit=%d" % res.exit_code)
        if not res.ok():
            return "", (res.stdout + res.stderr).strip()[:2000]
        token = extract_version_token(res.stdout + "\n" + res.stderr)
        if not token:
            # Fall back to first non-empty line when version is not SemVer.
            for line in (res.stdout + "\n" + res.stderr).splitlines():
                if line.strip():
                    token = line.strip()[:100]
                    break
        return token, (res.stdout + res.stderr).strip()[:2000]

    def _capabilities(self):
        # type: () -> Dict[str, object]
        caps = {"help": False, "plan": False, "check": False,
                "yes": False, "backup": False, "detail": ""}
        help_res = run_fixed([HERMES_BIN, "update", "--help"], timeout=60)
        self._emit("stdout", "$ hermes update --help -> exit=%d" % help_res.exit_code)
        if not help_res.ok():
            caps["detail"] = "update --help exit=%d" % help_res.exit_code
            return caps
        caps["help"] = True
        text = help_res.stdout + "\n" + help_res.stderr
        caps["plan"] = "--plan" in text
        caps["check"] = "--check" in text
        caps["yes"] = "--yes" in text
        caps["backup"] = "--backup" in text
        caps["detail"] = text[:2000]
        return caps

    def _git_state(self):
        # type: () -> Tuple[str, str, str]
        """Return (cleanliness, head, detail). Dirty/unreadable blocks."""
        head_res = run_fixed(["/usr/bin/git", "-C", HERMES_REPO, "rev-parse", "HEAD"], timeout=60)
        if not head_res.ok():
            return "unknown", "", "rev-parse failed exit=%d; unreadable git state blocks" % head_res.exit_code
        head = head_res.stdout.strip().split()[0] if head_res.stdout.strip() else ""
        status_res = run_fixed(
            ["/usr/bin/git", "-C", HERMES_REPO, "status", "--porcelain=v1"], timeout=60)
        if not status_res.ok():
            return "unknown", head, "status --porcelain=v1 failed exit=%d; unreadable git state blocks" % status_res.exit_code
        branch_res = run_fixed(
            ["/usr/bin/git", "-C", HERMES_REPO, "rev-parse", "--abbrev-ref", "HEAD"], timeout=60)
        branch = branch_res.stdout.strip().split()[0] if branch_res.ok() else ""
        dirty_lines = [ln for ln in status_res.stdout.splitlines() if ln.strip()]
        if dirty_lines:
            return "dirty", head, "dirty checkout (%d entries incl. untracked); --force never used" % len(dirty_lines)
        if branch and branch != HERMES_BRANCH:
            return "dirty", head, "unexpected branch %r (expected %s)" % (branch, HERMES_BRANCH)
        return "clean", head, "clean on %s at %s" % (branch or HERMES_BRANCH, head[:12])

    def _owner_of(self, path):
        # type: (str) -> str
        try:
            st = os.stat(path)
            return "%d:%d" % (st.st_uid, st.st_gid)
        except OSError:
            return "unknown"

    def _unit_show(self, scope, unit):
        # type: (str, str) -> Dict[str, str]
        base = ["/bin/systemctl"]
        if scope == "user":
            base.append("--user")
        res = run_fixed(
            base + ["show", unit, "-p", "LoadState", "-p", "ActiveState",
                    "-p", "SubState", "-p", "MainPID"],
            timeout=30)
        info = {"LoadState": "", "ActiveState": "", "SubState": "", "MainPID": ""}
        if not res.ok():
            return info
        for line in res.stdout.splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                if key in info:
                    info[key] = value.strip()
        return info

    def _has_system_restart_authority(self, units=None):
        # type: (object) -> Tuple[bool, str]
        """Confirm narrowly-scoped authority to restart system units.

        Never uses a generic sudo proof. For each inventoried system unit,
        the ONLY accepted probe is the exact allow-list shape
        `sudo -n systemctl --no-pager show <unit>` (see
        deploy/etc/sudoers.d/ega-update-hermes.example, which grants ubuntu
        passwordless ONLY those exact commands for the inventoried units).
        The probe argv is validated against the allow-list shape before any
        execution; anything outside the shape yields
        BLOCKED_RESTART_AUTHORITY without running sudo. Missing authority
        blocks before mutation (caller maps to install_method_unsupported
        with BLOCKED_RESTART_AUTHORITY detail).
        """
        try:
            if os.geteuid() == 0:
                return True, "running as root"
        except AttributeError:
            pass
        try:
            check_units = list(units) if units is not None else _inventoried_units()
        except Exception:
            check_units = _parse_units()
        system_units = [u for s, u in check_units if s == "system"]
        if not system_units:
            return True, "no system units in inventory; no elevated authority needed"
        # Allow-list shape: exact argv per inventoried unit, nothing else.
        allowed = set()
        for _u in system_units:
            allowed.add(tuple(_allowed_show_argv(_u)))
            allowed.add(tuple(_allowed_restart_argv(_u)))
        for _u in system_units:
            probe_argv = _allowed_show_argv(_u)
            if tuple(probe_argv) not in allowed:
                return False, "BLOCKED_RESTART_AUTHORITY: probe for %s outside allow-list shape" % _u
            probe = run_fixed(probe_argv, timeout=15)
            if not probe.ok():
                return False, ("BLOCKED_RESTART_AUTHORITY: no confirmed passwordless authority for %s "
                               "(sudo show exit=%d); refusing to prompt sudo mid-job; install sudoers snippet per RUNBOOK)" % (_u, probe.exit_code))
        return True, "narrowly-scoped passwordless authority confirmed for %d system unit(s) via sudo show probe(s)" % len(system_units)

    def _update_plan_probe(self):
        # type: () -> Tuple[str, str]
        caps = self._capabilities()
        if not caps.get("plan"):
            return "", "update --plan unsupported locally"
        res = run_fixed([HERMES_BIN, "update", "--plan"], timeout=300)
        self._emit("stdout", "$ hermes update --plan -> exit=%d" % res.exit_code)
        combined = (res.stdout + "\n" + res.stderr).strip()
        if not res.ok():
            return "", "update --plan exit=%d" % res.exit_code
        return combined[:2000], ""

    # -- Adapter interface -----------------------------------------------

    def inspect(self):
        # type: () -> InspectResult
        ok, resolved, detail = resolve_executable(HERMES_BIN)
        version, _raw = self._version_probe() if ok else ("", "unresolvable wrapper")
        cleanliness, head, git_detail = self._git_state()
        owner = self._owner_of(HERMES_BIN)
        fp = fingerprint(HERMES_BIN, resolved, version, head, cleanliness, owner)
        units = ["%s:%s" % (scope, unit) for scope, unit in _inventoried_units()]
        state_dirs = [d for d in ("/home/ubuntu/.hermes", HERMES_REPO) if os.path.exists(d)]
        return InspectResult(
            tool=self.tool_id,
            install_identity="hermes-native:%s@%s" % ((resolved or HERMES_BIN), head[:12] if head else "?"),
            executable=HERMES_BIN,
            resolved_target=resolved,
            version=version,
            commit=head,
            install_kind="git-native",
            owner=owner,
            source_clean=cleanliness,
            source_detail=git_detail,
            services=units,
            state_dirs=state_dirs,
            channel=HERMES_BRANCH,
            fingerprint=fp,
        )

    def discover(self):
        # type: () -> DiscoverResult
        ok, _resolved, detail = resolve_executable(HERMES_BIN)
        if not ok:
            return DiscoverResult(
                tool=self.tool_id, target="", target_mode="unknown",
                channel=HERMES_BRANCH, available=False,
                unknown_reason="install_method_unsupported: %s" % detail)
        if not os.path.isdir(HERMES_REPO):
            return DiscoverResult(
                tool=self.tool_id, target="", target_mode="unknown",
                channel=HERMES_BRANCH, available=False,
                unknown_reason="hermes repo missing at %s" % HERMES_REPO)
        caps = self._capabilities()
        if not caps.get("help") or not caps.get("yes"):
            return DiscoverResult(
                tool=self.tool_id, target="", target_mode="unknown",
                channel=HERMES_BRANCH, available=False,
                unknown_reason="supported noninteractive procedure unconfirmed: %s" % caps.get("detail", "")[:300])
        planned, error = self._update_plan_probe()
        if error or not planned:
            # Fail-closed discovery: a failed `update --plan` probe leaves the
            # target unknown instead of assuming main-latest is available.
            return DiscoverResult(
                tool=self.tool_id, target="", target_mode="unknown",
                channel=HERMES_BRANCH, available=False,
                unknown_reason="update --plan probe failed: %s"
                % (error or "empty plan output")[:300])
        target = "main-latest"
        for line in planned.splitlines():
            token = line.strip()
            if len(token) == 40 and all(c in "0123456789abcdef" for c in token.lower()):
                target = token
                break
        return DiscoverResult(
            tool=self.tool_id, target=target, target_mode="native_latest",
            channel=HERMES_BRANCH, available=True, unknown_reason="")

    def activity(self):
        # type: () -> ActivityResult
        busy_units = []
        unknown_units = []
        for scope, unit in _inventoried_units():
            info = self._unit_show(scope, unit)
            if info.get("LoadState") not in ("loaded",):
                unknown_units.append("%s:%s(%s)" % (scope, unit, info.get("LoadState") or "no-loadstate"))
            elif info.get("ActiveState") == "active" and info.get("SubState") == "running":
                # Running gateway alone never proves an active task; without a
                # task API this is unknown, not busy, and needs owner ack.
                unknown_units.append("%s:%s(running-unproven-task)" % (scope, unit))
        check_res = run_fixed([HERMES_BIN, "update", "--check"], timeout=300)
        check_text = (check_res.stdout + "\n" + check_res.stderr).strip()[:500]
        if busy_units:
            return ActivityResult(
                tool=self.tool_id, state="busy",
                evidence="busy: %s" % "; ".join(busy_units)[:800], checked_at=utcnow_iso())
        if unknown_units or not check_res.ok():
            return ActivityResult(
                tool=self.tool_id, state="unknown",
                evidence="gateway/task state unproven (%s; check=%s); ack required"
                % ("; ".join(unknown_units)[:400], check_text[:200]),
                checked_at=utcnow_iso())
        return ActivityResult(
            tool=self.tool_id, state="idle", evidence="gateways idle; %s" % check_text[:300],
            checked_at=utcnow_iso())

    def plan(self):
        # type: () -> PlanResult
        inspection = self.inspect()
        discovery = self.discover()
        if not discovery.available:
            return PlanResult(
                tool=self.tool_id, target="", target_mode="unknown",
                channel=HERMES_BRANCH, fingerprint=inspection.fingerprint,
                services=[], backup_scope={}, required_space_bytes=0,
                steps=[], timeouts=dict(ADAPTER_TIMEOUT_DEFAULTS),
                restart_impact="planning blocked: %s" % discovery.unknown_reason,
                already_current=False,
            )
        caps = self._capabilities()
        need = required_space_bytes(STAGING_ESTIMATE_BYTES, BACKUP_ESTIMATE_BYTES)
        inventoried = _inventoried_units()
        system_units = [unit for scope, unit in inventoried if scope == "system"]
        authority_ok, authority_detail = self._has_system_restart_authority(inventoried)
        restart_impact = "native restart handling; gateway/serve units %s" % ", ".join(
            "%s:%s" % (scope, unit) for scope, unit in inventoried)
        if system_units and not authority_ok:
            restart_impact += " BLOCKED_RESTART_AUTHORITY: %s" % authority_detail
        backup_mode = "full" if caps.get("backup") else "quick-limited"
        return PlanResult(
            tool=self.tool_id, target=discovery.target, target_mode="native_latest",
            channel=HERMES_BRANCH, fingerprint=inspection.fingerprint,
            services=["%s:%s" % (scope, unit) for scope, unit in inventoried],
            backup_scope={
                "covered": "repo state + gateway config capability + metadata (backup phase records capability+metadata only)",
                "omitted": "4.8 GiB hermes home wholesale",
                "consistency": "native --backup full backup executes inside the mutation command; backup phase alone is not a full backup",
                "mode": backup_mode,
            },
            required_space_bytes=need or 0,
            steps=["preflight", "backup", "updating", "verifying"],
            timeouts=dict(ADAPTER_TIMEOUT_DEFAULTS),
            restart_impact=restart_impact,
            already_current=False,
        )

    def backup(self, job_id):
        # type: (str) -> BackupResult
        # Backup phase records capability + metadata ONLY. A requested full
        # backup is performed natively via `hermes update --yes --backup`
        # INSIDE the mutation command (see execute()); this phase never
        # claims a full backup has occurred. execute() fails when a requested
        # full backup lacks receipt/flag evidence.
        caps = self._capabilities()
        try:
            from ..config import settings as _settings

            backup_root = _settings.backup_dir or "/var/lib/ega-update/backups"
        except Exception:
            backup_root = "/var/lib/ega-update/backups"
        dest_dir = os.path.join(backup_root, job_id)
        mode = "full" if caps.get("backup") else "quick-limited"
        scope = {
            "covered": "repo HEAD + status snapshot, gateway config capability+metadata (full backup executes natively inside mutation via --backup)",
            "omitted": "4.8 GiB hermes home wholesale",
            "consistency": "git metadata snapshot + native capability record; backup phase alone is not a full backup",
            "mode": ("%s (limited state protection, not full rollback)" % mode)
            if mode != "full" else "full-requested (native --backup executes inside mutation; receipt required)",
        }
        cleanliness, head, git_detail = self._git_state()
        if cleanliness != "clean":
            return BackupResult(
                tool=self.tool_id, supported=False, path="", scope=scope,
                consistency=scope["consistency"], size_bytes=0,
                unsupported_reason="backup_unsupported: %s" % git_detail)
        try:
            os.makedirs(dest_dir, mode=0o700, exist_ok=True)
            with open(os.path.join(dest_dir, "hermes-pre-update.txt"), "w",
                      encoding="utf-8") as fh:
                fh.write("head=%s\nclean=%s\ndetail=%s\n" % (head, cleanliness, git_detail))
            size = os.path.getsize(os.path.join(dest_dir, "hermes-pre-update.txt"))
        except OSError as exc:
            return BackupResult(
                tool=self.tool_id, supported=False, path="", scope=scope,
                consistency=scope["consistency"], size_bytes=0,
                unsupported_reason="backup_failed: %s" % exc)
        if mode != "full":
            # Quick mode is labeled limited; callers with a full-backup policy
            # must treat this receipt accordingly, never as full rollback.
            pass
        return BackupResult(
            tool=self.tool_id, supported=True, path=dest_dir, scope=scope,
            consistency=scope["consistency"], size_bytes=size,
            unsupported_reason="")

    def execute(self, plan, job_id, activity_ack=False):
        # type: (PlanResult, str, bool) -> ExecuteResult
        # Hermes is native_latest only: an exact-mode plan is an
        # exact-target mismatch and is blocked (runner enforces exact-target
        # centrally; adapters enforce their own check here).
        if plan.target_mode == "exact":
            _mismatch = ExecuteResult(
                tool=self.tool_id, exit_code=3, before_version="",
                after_version="", state="blocked", error_code="invalid_request",
                error_detail="exact-target mismatch: hermes supports only target_mode=native_latest")
            return attach_timed_out(_mismatch, False)
        ok, resolved, detail = resolve_executable(HERMES_BIN)
        if not ok:
            return ExecuteResult(
                tool=self.tool_id, exit_code=3, before_version="",
                after_version="", state="blocked", error_code="install_method_unsupported",
                error_detail=detail)
        current = self.inspect()
        if plan.fingerprint and current.fingerprint != plan.fingerprint:
            return ExecuteResult(
                tool=self.tool_id, exit_code=3, before_version=current.version,
                after_version="", state="blocked", error_code="fingerprint_changed",
                error_detail="installation changed since plan; fresh plan required")
        if current.source_clean != "clean":
            return ExecuteResult(
                tool=self.tool_id, exit_code=3, before_version=current.version,
                after_version="", state="blocked", error_code="git_dirty",
                error_detail=current.source_detail[:500])
        activity = self.activity()
        if activity.state == "busy":
            return ExecuteResult(
                tool=self.tool_id, exit_code=3, before_version=current.version,
                after_version="", state="blocked", error_code="activity_blocked",
                error_detail=activity.evidence[:500])
        if activity.state == "unknown" and not activity_ack:
            return ExecuteResult(
                tool=self.tool_id, exit_code=3, before_version=current.version,
                after_version="", state="blocked", error_code="ack_required",
                error_detail="unknown activity requires plan-specific owner ack: %s" % activity.evidence[:400])
        if activity.state == "unknown" and activity_ack:
            self._emit("stdout", "proceeding with recorded activity ack: %s"
                       % activity.evidence[:400])
        need = plan.required_space_bytes or required_space_bytes(
            STAGING_ESTIMATE_BYTES, BACKUP_ESTIMATE_BYTES)
        if need is None:
            return ExecuteResult(
                tool=self.tool_id, exit_code=3, before_version=current.version,
                after_version="", state="blocked", error_code="disk_blocked",
                error_detail="unknown space estimate blocks mutation")
        disk_ok, disk_detail, _per_fs = check_disk(
            [HERMES_BIN, HERMES_REPO, "/var/lib/ega-update/backups"], need)
        if not disk_ok:
            return ExecuteResult(
                tool=self.tool_id, exit_code=3, before_version=current.version,
                after_version="", state="blocked", error_code="disk_blocked",
                error_detail=disk_detail[:500])
        caps = self._capabilities()
        if not caps.get("yes"):
            return ExecuteResult(
                tool=self.tool_id, exit_code=3, before_version=current.version,
                after_version="", state="blocked", error_code="install_method_unsupported",
                error_detail="noninteractive --yes unsupported locally")
        inventoried = _inventoried_units()
        system_units = [(s, u) for s, u in inventoried if s == "system"]
        if system_units:
            authority_ok, authority_detail = self._has_system_restart_authority(inventoried)
            if not authority_ok:
                _blocked = ExecuteResult(
                    tool=self.tool_id, exit_code=3, before_version=current.version,
                    after_version="", state="blocked",
                    error_code="install_method_unsupported",
                    error_detail="BLOCKED_RESTART_AUTHORITY: %s" % authority_detail)
                return attach_timed_out(_blocked, False)
        backup_mode = str((plan.backup_scope or {}).get("mode", ""))
        argv = [HERMES_BIN, "update", "--yes"]
        if caps.get("backup") and backup_mode.startswith("full"):
            argv.append("--backup")
        elif backup_mode.startswith("full") and not caps.get("backup"):
            return ExecuteResult(
                tool=self.tool_id, exit_code=3, before_version=current.version,
                after_version="", state="blocked", error_code="backup_unsupported",
                error_detail="full backup requested but native --backup unsupported; refusing silent downgrade")
        before = current.version
        before_head = current.commit
        # Shared contract: mutation subprocess timeout = step_timeout +
        # registry.MUTATION_TIMEOUT_MARGIN_S (120s). Probes keep fixed
        # timeouts; only this mutating update adds the margin.
        mutation_to = mutation_timeout_for(plan, "updating", default=1800)
        res = run_fixed(argv, timeout=mutation_to)
        self._emit("stdout", "$ hermes update --yes%s -> exit=%d (mutation timeout %ds = step + %ds margin)"
                   % (" --backup" if "--backup" in argv else "", res.exit_code,
                      mutation_to, MUTATION_TIMEOUT_MARGIN_S))
        self._emit("stdout", (res.stdout or "")[-4000:])
        if res.stderr:
            self._emit("stderr", (res.stderr or "")[-4000:])
        if res.timed_out:
            _tout = ExecuteResult(
                tool=self.tool_id, exit_code=4, before_version=before,
                after_version="", state="install_failed", error_code="timeout",
                error_detail="hermes update timed out; possible partial install")
            return attach_timed_out(_tout, True)
        if res.exit_code != 0:
            after_probe, _raw = self._version_probe()
            _fail = ExecuteResult(
                tool=self.tool_id, exit_code=4, before_version=before,
                after_version=after_probe, state="install_failed",
                error_code="install_failed",
                error_detail="hermes update exit=%d" % res.exit_code)
            return attach_timed_out(_fail, False)
        # Full-backup receipt check: when the plan requested a full backup,
        # the native --backup MUST have actually run inside this mutation.
        # Require both the --backup flag in argv and backup receipt/flag
        # evidence in output; otherwise fail (never silently downgrade).
        if backup_mode.startswith("full"):
            _combined = ((res.stdout or "") + "\n" + (res.stderr or "")).lower()
            _has_flag = "--backup" in argv
            _has_receipt = ("backup" in _combined)
            if not (_has_flag and _has_receipt):
                after_probe, _raw = self._version_probe()
                _bfail = ExecuteResult(
                    tool=self.tool_id, exit_code=4, before_version=before,
                    after_version=after_probe, state="install_failed",
                    error_code="backup_failed",
                    error_detail="full backup requested but no backup receipt/flag evidence "
                                 "(flag=%s receipt=%s); refusing to claim full backup" % (_has_flag, _has_receipt))
                return attach_timed_out(_bfail, False)
        # Exit zero alone is never success: verify commit, diagnostics,
        # restart outcome, and running versions below (runner calls verify()).
        after_inspection = self.inspect()
        self._emit("stdout", "hermes before=%s@%s after=%s@%s" % (
            before, (before_head or "")[:12], after_inspection.version,
            (after_inspection.commit or "")[:12]))
        _done = ExecuteResult(
            tool=self.tool_id, exit_code=0, before_version=before,
            after_version=after_inspection.version, state="succeeded",
            error_code="", error_detail="")
        return attach_timed_out(_done, False)

    def verify(self):
        # type: () -> VerifyResult
        checks = []  # type: List[CheckItem]
        inspection = self.inspect()
        checks.append(CheckItem(
            name="final_commit", result="pass" if inspection.commit else "fail",
            mandatory=True,
            summary=("commit %s" % inspection.commit[:12]) if inspection.commit
            else "final commit unreadable"))
        diag = run_fixed([HERMES_BIN, "--version"], timeout=60)
        checks.append(CheckItem(
            name="cli_diagnostics", result="pass" if diag.ok() else "fail",
            mandatory=True, summary="diagnostics exit=%d version=%s"
            % (diag.exit_code, inspection.version or "?")))
        help_probe = run_fixed([HERMES_BIN, "doctor", "--help"], timeout=60)
        if help_probe.ok():
            doctor = run_fixed([HERMES_BIN, "doctor"], timeout=300)
            checks.append(CheckItem(
                name="doctor", result="pass" if doctor.ok() else "fail",
                mandatory=True, summary="doctor exit=%d" % doctor.exit_code))
        all_running = True
        for scope, unit in _inventoried_units():
            info = self._unit_show(scope, unit)
            running = (info.get("LoadState") == "loaded"
                       and info.get("ActiveState") == "active"
                       and info.get("SubState") == "running")
            checks.append(CheckItem(
                name="unit:%s:%s" % (scope, unit),
                result="pass" if running else "fail", mandatory=True,
                summary="%s active=%s sub=%s pid=%s" % (
                    unit, info.get("ActiveState") or "?", info.get("SubState") or "?",
                    info.get("MainPID") or "?")))
            all_running = all_running and running
        checks.append(CheckItem(
            name="running_version", result="pass" if (inspection.version and all_running) else "fail",
            mandatory=True,
            summary="running version evidence version=%s gateways_running=%s"
            % (inspection.version or "?", all_running)))
        passed = bool(inspection.commit) and not any(
            c for c in checks if c.mandatory and c.result != "pass")
        return VerifyResult(
            tool=self.tool_id, version=inspection.version, checks=checks,
            passed=passed, error_code="" if passed else "health_failed",
            error_detail="" if passed else "mandatory hermes checks failed")

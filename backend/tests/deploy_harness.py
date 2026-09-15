"""Executable install.sh/upgrade.sh sandbox harness (hermetic; PATH shims).

Runs the REAL deploy scripts against a rewritten path layout
(/etc/ega-update, /opt/ega-update, /var/lib/ega-update, /etc/systemd and
/tmp targets are redirected into a tmp_path sandbox) with every
mutating/privileged command shimmed through a PATH shim directory. Each
shim records an event into an events log, so tests can assert the
OBSERVED order of host/runtime mutations relative to the drain +
quiescence maintenance boundary.

Deterministic W6 acceptance knobs (all default off / production
behavior, so the existing W4/W5 suites are unaffected):
  quiesce_fail           quiescence-check.py exits 3 forever
  archive_validate_fail  validate-archive.py blocks the staged tarball
  pip_fail               release venv pip install fails
  missing_frontend       extracted tarball lacks backend/app/static/index.html
  provision_fail         owner_env provision (ACL bootstrap) fails
  drain_create_fail      touch <state>/drain fails
  config_parse_fail      config_cli get --require fails (broken config)
  current_invalid        current is a regular file / dir / escaping symlink
  cas_race               another actor moves `current` after inspect
  start_fail_units       systemctl start/restart fails for named units
  compat_error           --check-compat exits with an unexpected error (unknown)
  secrets_env_present    omit the pre-provisioned secrets.env
  etc_readonly           /etc/ega-update is not writable (host-prep failure)
  unit_content_from_commit  copied units embed the release commit (F boundary)

Safety: no real account/group/systemd/ACL/filesystem mutation is
possible — the rewritten constants point at the sandbox and every
external command that could touch the host is shimmed. Only the deploy
scripts under test are executed, never any service.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
import textwrap


def _write(path, content, mode=0o755):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    os.chmod(path, mode)


def _shim(bin_dir, name, body):
    _write(os.path.join(bin_dir, name), textwrap.dedent("""\
        #!/usr/bin/env bash
        record() { printf '%%s\\n' "$*" >> "$EGA_HARNESS_EVENTS"; }
        %s
        """) % textwrap.dedent(body))


_FAKE_VENV_PY = """\
#!/usr/bin/env bash
record() { printf '%s\\n' "$*" >> "$EGA_HARNESS_EVENTS"; }
record "RELEASE_PY $*"
if [ "${1:-}" = "-m" ] && [ "${2:-}" = "backend.app.config_cli" ]; then
  shift 2
  cmd="${1:-}"; shift || true
  case "$cmd" in
    get)
      key=""
      while [ $# -gt 0 ]; do
        case "$1" in
          --require) key="${2:-}"; shift 2 ;;
          *) key="$1"; shift ;;
        esac
      done
      case "$key" in
        db_path) printf '%s\\n' "$EGA_HARNESS_DB" ;;
        listen_port) printf '8771\\n' ;;
        listen_host) printf '127.0.0.1\\n' ;;
        owner_emails) printf 'owner@example.invalid\\n' ;;
        csrf_secret) printf 'fixture-csrf-secret-value\\n' ;;
        *) printf '\\n' ;;
      esac
      exit 0 ;;
    json)
      printf '{"team_domain": "team.cloudflareaccess.com", "audience": "aud-1", "public_origin": "https://console.example.invalid"}\\n'
      exit 0 ;;
  esac
  exit 0
fi
if [ "${1:-}" = "-m" ] && [ "${2:-}" = "backend.app.cli" ]; then
  if [ "${3:-}" = "status" ]; then
    if [ "${EGA_HARNESS_READY_FAIL:-0}" = "1" ]; then
      record "READINESS_FAIL"
      printf 'not ready: failed stages: owner_transient_execution\\n' >&2
      printf '{"state":"blocked"}\\n'
      exit 3
    fi
    if [ -n "${EGA_HARNESS_READY_FAIL_STAGES:-}" ]; then
      record "READINESS_FAIL_STAGES ${EGA_HARNESS_READY_FAIL_STAGES}"
      printf 'not ready: failed stages: %s\\n' \
        "${EGA_HARNESS_READY_FAIL_STAGES}" >&2
      printf '{"state":"blocked"}\\n'
      exit 3
    fi
    record "READINESS_OK"
    printf '{"state":"ready"}\\n'
    exit 0
  fi
  exit 0
fi
case "${1:-}" in
  *validate-release.py*)
    _check_compat=0
    _validate=0
    for _a in "$@"; do
      [ "$_a" = "--check-compat" ] && _check_compat=1
      [ "$_a" = "--release" ] && _validate=1
    done
    if [ "$_check_compat" = "1" ]; then
      case "${EGA_HARNESS_COMPAT_FAIL:-0}" in
        error)
          record "VALIDATE_COMPAT_ERROR"
          printf 'error: compatibility check unavailable (harness)\\n'
          exit 7 ;;
        fail|1)
          record "VALIDATE_COMPAT_FAIL"
          printf 'blocked: schema drift 3->4 (harness compat failure)\\n'
          exit 3 ;;
        *) exit 0 ;;
      esac
    fi
    if [ "$_validate" = "1" ] \
       && [ "${EGA_HARNESS_VALIDATOR_REQUIRE_SECRETS:-1}" = "1" ] \
       && [ ! -e "${EGA_HARNESS_ETC:-/nonexistent}/secrets.env" ]; then
      record "VALIDATE_BLOCKED_SECRETS"
      printf 'blocked: secrets.env missing (harness fidelity gate)\\n'
      exit 3
    fi
    record "VALIDATE_RELEASE"; exit 0 ;;
  *owner_env*) record "RELEASE_PY_OWNER_ENV"; exit 0 ;;
esac
exit 0
"""

_FAKE_VENV_PIP = """\
#!/usr/bin/env bash
record() { printf '%s\\n' "$*" >> "$EGA_HARNESS_EVENTS"; }
record "PIP $*"
if [ "${EGA_HARNESS_PIP_FAIL:-0}" = "1" ]; then
  record "PIP_FAIL"
  exit 1
fi
exit 0
"""


def _install_shims(bin_dir, sandbox):
    _shim(bin_dir, "id", """\
        if [ "${1:-}" = "-u" ]; then
          if [ "${2:-}" = "ega-update" ]; then exit 1; fi
          if [ $# -ge 2 ]; then echo 1000; else echo 0; fi
          exit 0
        fi
        case "${1:-}" in
          ega-update)
            [ "${EGA_HARNESS_USER_EXISTS:-0}" = "1" ] && exit 0 || exit 1 ;;
          *) exit 0 ;;
        esac
        """)
    _shim(bin_dir, "getent", """
        record "GETENT $*"
        exit 0
        """)
    _shim(bin_dir, "useradd", """
        record "USERADD $*"
        exit 0
        """)
    _shim(bin_dir, "usermod", """
        record "USERMOD $*"
        exit 0
        """)
    _shim(bin_dir, "groupadd", """
        record "GROUPADD $*"
        exit 0
        """)
    _shim(bin_dir, "chown", """
        record "CHOWN $*"
        exit 0
        """)
    _shim(bin_dir, "chmod", """
        record "CHMOD $*"
        exit 0
        """)
    _shim(bin_dir, "touch", """
        record "TOUCH $*"
        if [ "${EGA_HARNESS_DRAIN_CREATE_FAIL:-0}" = "1" ]; then
          for _a in "$@"; do
            case "$_a" in
              */drain) record "TOUCH_FAIL $_a"; exit 1 ;;
            esac
          done
        fi
        for _a in "$@"; do
          case "$_a" in
            */drain)
              if [ -n "${EGA_HARNESS_LOCK:-}" ] \\
                 && [ -e "${EGA_HARNESS_LOCK}" ]; then
                exec 8>"${EGA_HARNESS_LOCK}"
                if flock -n 8 2>/dev/null; then
                  record "LOCK_FREE_DURING_MUTATION"
                  flock -u 8 2>/dev/null || true
                else
                  record "LOCK_HELD_DURING_MUTATION"
                fi
                exec 8>&-
              fi ;;
          esac
        done
        command /usr/bin/touch "$@"
        """)
    _shim(bin_dir, "mkdir", """
        record "MKDIR $*"
        command /usr/bin/mkdir "$@"
        rc=$?
        for d in "$@"; do
          case "$d" in
            -*) ;;
            *) [ -d "$d" ] && command /usr/bin/chmod 0755 "$d" 2>/dev/null ;;
          esac
        done
        exit $rc
        """)
    _shim(bin_dir, "cp", """
        record "CP $*"
        command /usr/bin/cp "$@"
        """)
    _shim(bin_dir, "rm", """
        record "RM $*"
        command /usr/bin/rm "$@"
        """)
    _shim(bin_dir, "mktemp", """
        command /usr/bin/mktemp "$@"
        """)
    _shim(bin_dir, "sha256sum", """\
        record "SHA256"
        for f in "$@"; do
          printf 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa  %s\\n' "$f"
        done
        exit 0
        """)
    _shim(bin_dir, "readlink", """
        command /usr/bin/readlink "$@"
        """)
    _shim(bin_dir, "loginctl", """\
        record "LOGINCTL $*"
        case "$1" in
          show-user) echo no ;;
        esac
        exit 0
        """)
    _shim(bin_dir, "systemctl", """\
        state_file="$EGA_HARNESS_SANDBOX/.systemctl-state"
        statedir="$EGA_HARNESS_SANDBOX/.systemctl-state.d"
        unit_state() {
          if [ -f "$statedir/$1" ]; then cat "$statedir/$1"
          elif [ -f "$state_file" ]; then cat "$state_file"
          else echo inactive; fi
        }
        set_state() {
          st="$1"; shift
          mkdir -p "$statedir"
          for u in "$@"; do
            case "$u" in -*) continue ;; esac
            printf '%s\\n' "$st" > "$statedir/$u" 2>/dev/null || true
          done
          printf '%s\\n' "$st" > "$state_file"
        }
        case "${1:-}" in
          is-active)
            unit=""
            for a in "$@"; do
              case "$a" in --quiet|-q|--no-pager) ;; *) unit="$a" ;; esac
            done
            if [ -n "$unit" ]; then
              [ "$(unit_state "$unit")" = "active" ] && exit 0 || exit 3
            fi
            [ "$(unit_state '')" = "active" ] && exit 0 || exit 3 ;;
          stop)
            record "SYSTEMCTL $*"
            shift
            set_state stopped "$@"
            exit 0 ;;
          start|restart)
            record "SYSTEMCTL $*"
            for resolved_u in "$@"; do
              case "$resolved_u" in
                ega-update-api|ega-update-worker)
                  resolved_target=""
                  if [ -n "${EGA_HARNESS_CURRENT:-}" ] \
                     && [ -e "${EGA_HARNESS_CURRENT}" ]; then
                    resolved_target="$(command /usr/bin/readlink -f \
                      "${EGA_HARNESS_CURRENT}" 2>/dev/null || true)"
                  fi
                  record "SYSTEMCTL_RESOLVED $resolved_u $resolved_target" ;;
              esac
            done
            for u in "${@:2}"; do
              case ",${EGA_HARNESS_START_FAIL_UNITS:-}," in
                *",$u,"*)
                  printf 'stopped\\n' > "$statedir/$u" 2>/dev/null || true
                  record "SYSTEMCTL_FAIL $*"
                  exit 1 ;;
              esac
            done
            shift
            set_state active "$@"
            exit 0 ;;
          list-timers)
            echo "(none)"
            exit 0 ;;
          *)
            record "SYSTEMCTL $*"
            exit 0 ;;
        esac
        """)
    _shim(bin_dir, "su", """
        record "SU $*"
        exit 0
        """)
    _shim(bin_dir, "sleep", """\
        record "SLEEP $*"
        exit 0
        """)
    _shim(bin_dir, "curl", """\
        record "CURL $*"
        printf '401'
        exit 0
        """)
    _shim(bin_dir, "ss", """\
        echo "LISTEN 0 4096 127.0.0.1:8771 0.0.0.0:*"
        exit 0
        """)
    _shim(bin_dir, "crontab", """
        exit 1
        """)
    _shim(bin_dir, "tar", """\
        record "TAR $*"
        dest=""
        prev=""
        for arg in "$@"; do
          if [ "$prev" = "-C" ]; then dest="$arg"; fi
          prev="$arg"
        done
        [ -n "$dest" ] || exit 1
        _commit="$(basename "$dest")"
        mkdir -p "$dest/backend/app/static" "$dest/backend/migrations" \\
                 "$dest/systemd/user" "$dest/deploy/etc"
        if [ "${EGA_HARNESS_MISSING_FRONTEND:-0}" = "1" ]; then
          record "TAR_MISSING_FRONTEND"
        else
          : > "$dest/backend/app/static/index.html"
        fi
        : > "$dest/backend/requirements.txt"
        : > "$dest/backend/app/__init__.py"
        : > "$dest/deploy/etc/validate-release.py"
        if [ "${EGA_HARNESS_UNIT_CONTENT_FROM_COMMIT:-0}" = "1" ]; then
          printf '# unit from release %s\\n' "$_commit" \\
            > "$dest/systemd/ega-update-api.service"
          printf '# unit from release %s\\n' "$_commit" \\
            > "$dest/systemd/ega-update-worker.service"
          printf '# unit from release %s\\n' "$_commit" \\
            > "$dest/systemd/ega-update-runner@.service"
          printf '# unit from release %s\\n' "$_commit" \\
            > "$dest/systemd/user/ega-update-runner@.service"
        else
          : > "$dest/systemd/ega-update-api.service"
          : > "$dest/systemd/ega-update-worker.service"
          : > "$dest/systemd/ega-update-runner@.service"
          : > "$dest/systemd/user/ega-update-runner@.service"
        fi
        exit 0
        """)
    _shim(bin_dir, "python3", """\
        record "PYTHON3 $*"
        if [ "${1:-}" = "-m" ]; then
          case "${2:-}" in
            backend.app.config_cli)
              if [ "${EGA_HARNESS_CONFIG_PARSE_FAIL:-0}" = "1" ]; then
                record "CONFIG_PARSE_FAIL $*"
                exit 1
              fi
              shift 2
              cmd="${1:-}"; shift || true
              case "$cmd" in
                get)
                  key=""
                  while [ $# -gt 0 ]; do
                    case "$1" in
                      --require) key="${2:-}"; shift 2 ;;
                      *) key="$1"; shift ;;
                    esac
                  done
                  case "$key" in
                    state_dir) printf '%s\\n' "$EGA_HARNESS_STATE" ;;
                    db_path) printf '%s\\n' "$EGA_HARNESS_DB" ;;
                    backup_dir) printf '%s\\n' "$EGA_HARNESS_BACKUPS" ;;
                    shared_group) printf 'ega-update\\n' ;;
                    listen_port) printf '8771\\n' ;;
                    listen_host) printf '127.0.0.1\\n' ;;
                    owner_emails) printf 'owner@example.invalid\\n' ;;
                    csrf_secret) printf 'fixture-csrf-secret-value\\n' ;;
                    *) printf '\\n' ;;
                  esac
                  exit 0 ;;
                json)
                  printf '{"team_domain": "team.cloudflareaccess.com", "audience": "aud-1", "public_origin": "https://console.example.invalid"}\\n'
                  exit 0 ;;
              esac
              exit 0 ;;
            backend.app.owner_env)
              case "${3:-}" in
                provision)
                  record "PROVISION $*"
                  if [ "${EGA_HARNESS_PROVISION_FAIL:-0}" = "1" ]; then
                    record "PROVISION_FAIL"
                    exit 1
                  fi ;;
                probe) record "TRANSIENT_PROBE" ;;
                verify) record "TRANSIENT_VERIFY" ;;
                bus-env)
                  record "BUS_ENV $*"
                  printf 'XDG_RUNTIME_DIR=/run/user/1001 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1001/bus\n'
                  exit 0 ;;
              esac
              exit 0 ;;
            venv)
              dest="${3:-}"
              mkdir -p "$dest/bin"
              cp "$EGA_HARNESS_FAKE_PY" "$dest/bin/python"
              cp "$EGA_HARNESS_FAKE_PIP" "$dest/bin/pip"
              chmod 0755 "$dest/bin/python" "$dest/bin/pip"
              record "VENV $dest"
              exit 0 ;;
            backend.app.deploy_release)
              shift 2
              record "DEPLOY_RELEASE $*"
              if [ "${EGA_HARNESS_SWITCH_POST_REPLACE_FAIL:-0}" = "1" ] \
                 && [ "${1:-}" = "switch" ] \
                 && [ ! -f "$EGA_HARNESS_SANDBOX/.switch-post-replace-failed" ]; then
                "$EGA_HARNESS_REAL_PY" -m backend.app.deploy_release "$@"
                _rc=$?
                if [ "$_rc" = "0" ]; then
                  : > "$EGA_HARNESS_SANDBOX/.switch-post-replace-failed"
                  record "DEPLOY_RELEASE_SWITCH_POST_REPLACE_FAIL"
                  exit 5
                fi
                exit "$_rc"
              fi
              "$EGA_HARNESS_REAL_PY" -m backend.app.deploy_release "$@"
              _rc=$?
              if [ "${EGA_HARNESS_CAS_RACE:-0}" = "1" ] \
                 && [ "${1:-}" = "inspect" ] \
                 && [ ! -f "$EGA_HARNESS_SANDBOX/.cas-raced" ]; then
                mkdir -p "$EGA_HARNESS_RACE_TARGET/venv/bin" \
                         "$EGA_HARNESS_RACE_TARGET/deploy/etc"
                cp "$EGA_HARNESS_FAKE_PY" \
                   "$EGA_HARNESS_RACE_TARGET/venv/bin/python"
                cp "$EGA_HARNESS_FAKE_PIP" \
                   "$EGA_HARNESS_RACE_TARGET/venv/bin/pip"
                chmod 0755 "$EGA_HARNESS_RACE_TARGET/venv/bin/python" \
                           "$EGA_HARNESS_RACE_TARGET/venv/bin/pip"
                : > "$EGA_HARNESS_RACE_TARGET/deploy/etc/validate-release.py"
                chmod 0755 "$EGA_HARNESS_RACE_TARGET"
                ln -sfn "$EGA_HARNESS_RACE_TARGET" "$EGA_HARNESS_CURRENT"
                : > "$EGA_HARNESS_SANDBOX/.cas-raced"
                record "CAS_RACE_SWITCH $EGA_HARNESS_CURRENT -> $EGA_HARNESS_RACE_TARGET"
              fi
              exit "$_rc" ;;
            *) exit 0 ;;
          esac
        fi
        if [ "${1:-}" = "-c" ]; then exit 0; fi
        case "${1:-}" in
          *quiescence-check.py*)
            if [ "${EGA_HARNESS_QUIESCE_FAIL:-0}" = "1" ]; then
              record "QUIESCE_FAIL"
              printf '{"quiescent": false, "reasons": ["harness"]}\\n'
              exit 3
            fi
            record "QUIESCE"; exit 0 ;;
          *validate-archive.py*)
            if [ "${EGA_HARNESS_ARCHIVE_VALIDATE_FAIL:-0}" = "1" ]; then
              record "ARCHIVE_VALIDATE_FAIL"
              printf 'blocked: archive member rejected (harness)\\n'
              exit 3
            fi
            record "ARCHIVE_VALIDATE"; exit 0 ;;
          *validate-release.py*) record "VALIDATE_RELEASE"; exit 0 ;;
        esac
        exit 0
        """)


def _rewrite_script(text, repo_root, sandbox, script_name):
    etc = os.path.join(sandbox, "etc", "ega-update")
    opt = os.path.join(sandbox, "opt", "ega-update")
    var = os.path.join(sandbox, "var", "lib", "ega-update")
    systemd = os.path.join(sandbox, "etc", "systemd")
    tmp = os.path.join(sandbox, "tmp")
    home = os.path.join(sandbox, "home", "ubuntu")
    text = text.replace(
        'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"',
        'SCRIPT_DIR="%s/deploy/scripts"' % repo_root)
    text = text.replace(
        'REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"',
        'REPO_ROOT="%s"' % repo_root)
    # /var/tmp and /tmp straddle each other as substrings; rewrite the
    # exact script templates in ONE left-to-right pass.
    text = re.sub(r"/var/tmp/ega-|/tmp/ega-",
                  lambda _m: os.path.join(tmp, "ega-"), text)
    for real, fake in (
            ("/var/tmp", tmp),
            ("/etc/ega-update", etc),
            ("/opt/ega-update", opt),
            ("/var/lib/ega-update", var),
            ("/etc/systemd", systemd),
            ("/home/$TOOL_OWNER", home),
            ("/home/ubuntu", home)):
        text = text.replace(real, fake)
    return text


def run_deploy_script(tmp_path, repo_root, script_name, *, existing_deploy,
                      db_exists=True, preexisting_drain=False,
                      ready_fail=False, ready_fail_stages=None,
                      services_active=False, fault_point=None,
                      compat_fail=False, compat_error=False,
                      switch_post_replace_fail=False, lock_held=False,
                      quiesce_fail=False, archive_validate_fail=False,
                      pip_fail=False, missing_frontend=False,
                      provision_fail=False, drain_create_fail=False,
                      config_parse_fail=False, current_invalid=None,
                      cas_race=False, start_fail_units=(),
                      secrets_env_present=True, etc_readonly=False,
                      unit_content_from_commit=False, user_exists=False,
                      preserve_pointer=False, env_extra=None):
    """Run install.sh/upgrade.sh in a sandbox. Returns (proc, sandbox,
    events, env, paths).

    Deterministic W5-D9 fault injection:
      * fault_point        -> EGA_DEPLOY_FAULT_POINT for the shared
                              deploy_common.sh hook / primitive hook
      * compat_fail        -> fake release venv says --check-compat fails
      * compat_error       -> fake --check-compat exits with an unexpected
                              error (compatibility unknown, not proven false)
      * switch_post_replace_fail -> harness corrupts the switch AFTER the
                              real atomic replace (post-switch recovery)
      * lock_held          -> the harness holds the kernel flock on the
                              sandbox deployment lock during the run

    Deterministic W6 failure-matrix knobs (see module docstring).
    """
    sandbox = str(tmp_path / "sandbox")
    for rel in ("etc/ega-update", "etc/systemd/system",
                "opt/ega-update/releases", "var/lib/ega-update/logs",
                "var/lib/ega-update/backups", "tmp", "bin",
                "home/ubuntu/.config"):
        os.makedirs(os.path.join(sandbox, rel), exist_ok=True)
    bin_dir = os.path.join(sandbox, "bin")
    events = os.path.join(sandbox, "events.log")
    _write(events, "")
    state = os.path.join(sandbox, "var", "lib", "ega-update")
    db_path = os.path.join(state, "state.db")
    backups = os.path.join(state, "backups")
    etc = os.path.join(sandbox, "etc", "ega-update")
    prefix = os.path.join(sandbox, "opt", "ega-update")
    current = os.path.join(prefix, "current")
    releases = os.path.join(prefix, "releases")
    systemd_dir = os.path.join(sandbox, "etc", "systemd")

    fake_py = os.path.join(sandbox, "fake-venv-python")
    fake_pip = os.path.join(sandbox, "fake-venv-pip")
    _write(fake_py, _FAKE_VENV_PY)
    _write(fake_pip, _FAKE_VENV_PIP)

    config = {
        "state_dir": state, "db_path": db_path, "backup_dir": backups,
        "log_dir": os.path.join(state, "logs"),
        "listen_host": "127.0.0.1", "listen_port": 8771,
        "team_domain": "team.cloudflareaccess.com", "audience": "aud-1",
        "owner_emails": ["owner@example.invalid"],
        "public_origin": "https://console.example.invalid",
        "csrf_secret": "fixture-csrf-secret-value",
        "tool_owner": "ubuntu", "shared_group": "ega-update",
    }
    with open(os.path.join(etc, "config.json"), "w",
              encoding="utf-8") as fh:
        json.dump(config, fh)
    _write(os.path.join(etc, "csrf.secret"), "fixture-csrf-secret-value\n",
           0o600)
    if secrets_env_present:
        _write(os.path.join(etc, "secrets.env"), "", 0o640)
    _write(os.path.join(etc, "inventory.json"), '{"tools": {}}', 0o640)

    if db_exists:
        _write(db_path, "sqlite-placeholder\n", 0o660)
    if preexisting_drain:
        _write(os.path.join(state, "drain"), "")

    prev_release = os.path.join(releases, "0" * 40)
    if existing_deploy:
        os.makedirs(os.path.join(prev_release, "venv", "bin"),
                    exist_ok=True)
        _write(os.path.join(prev_release, "venv", "bin", "python"),
               _FAKE_VENV_PY)
        _write(os.path.join(prev_release, "venv", "bin", "pip"),
               _FAKE_VENV_PIP)
        # Immutable-release contract for the restore target (0755).
        os.chmod(prev_release, 0o755)
    if current_invalid == "file":
        _write(current, "manual state\n")
    elif current_invalid == "dir":
        os.makedirs(current, exist_ok=True)
    elif current_invalid == "escape":
        outside = os.path.join(sandbox, "outside-current")
        os.makedirs(outside, exist_ok=True)
        os.symlink(outside, current)
    elif existing_deploy:
        if preserve_pointer and os.path.lexists(current):
            pass
        else:
            if os.path.islink(current) or os.path.exists(current):
                os.unlink(current)
            os.symlink(prev_release, current)

    if etc_readonly:
        os.chmod(etc, 0o555)

    tarball = os.path.join(sandbox, "tmp", "release.tar.gz")
    _write(tarball, "not-a-real-tarball\n")

    _install_shims(bin_dir, sandbox)

    with open(os.path.join(repo_root, "deploy", "scripts",
                           script_name), "r", encoding="utf-8") as fh:
        script_text = fh.read()
    if script_name == "install.sh":
        args = ["--commit", "1" * 40, "--release-tarball", tarball]
    else:
        args = ["--commit", "2" * 40, "--release-tarball", tarball]
    rewritten = _rewrite_script(script_text, repo_root, sandbox, script_name)
    script_path = os.path.join(sandbox, "%s.under-test.sh" % script_name)
    _write(script_path, rewritten)

    env = dict(os.environ)
    env.update({
        "PATH": bin_dir + ":" + env.get("PATH", "/usr/bin:/bin"),
        "EGA_HARNESS_EVENTS": events,
        "EGA_HARNESS_SANDBOX": sandbox,
        "EGA_HARNESS_STATE": state,
        "EGA_HARNESS_DB": db_path,
        "EGA_HARNESS_BACKUPS": backups,
        "EGA_HARNESS_ETC": etc,
        "EGA_HARNESS_CURRENT": current,
        "EGA_HARNESS_LOCK": os.path.join(prefix, "deploy.lock"),
        "EGA_HARNESS_RACE_TARGET": os.path.join(releases, "3" * 40),
        "EGA_HARNESS_FAKE_PY": fake_py,
        "EGA_HARNESS_FAKE_PIP": fake_pip,
        "EGA_HARNESS_REAL_PY": sys.executable,
    })
    if ready_fail:
        env["EGA_HARNESS_READY_FAIL"] = "1"
    if ready_fail_stages:
        env["EGA_HARNESS_READY_FAIL_STAGES"] = ",".join(ready_fail_stages)
    if fault_point:
        env["EGA_DEPLOY_FAULT_POINT"] = fault_point
    if compat_fail:
        env["EGA_HARNESS_COMPAT_FAIL"] = "fail"
    if compat_error:
        env["EGA_HARNESS_COMPAT_FAIL"] = "error"
    if switch_post_replace_fail:
        env["EGA_HARNESS_SWITCH_POST_REPLACE_FAIL"] = "1"
    if quiesce_fail:
        env["EGA_HARNESS_QUIESCE_FAIL"] = "1"
    if archive_validate_fail:
        env["EGA_HARNESS_ARCHIVE_VALIDATE_FAIL"] = "1"
    if pip_fail:
        env["EGA_HARNESS_PIP_FAIL"] = "1"
    if missing_frontend:
        env["EGA_HARNESS_MISSING_FRONTEND"] = "1"
    if provision_fail:
        env["EGA_HARNESS_PROVISION_FAIL"] = "1"
    if drain_create_fail:
        env["EGA_HARNESS_DRAIN_CREATE_FAIL"] = "1"
    if config_parse_fail:
        env["EGA_HARNESS_CONFIG_PARSE_FAIL"] = "1"
    if cas_race:
        env["EGA_HARNESS_CAS_RACE"] = "1"
    if start_fail_units:
        env["EGA_HARNESS_START_FAIL_UNITS"] = ",".join(start_fail_units)
    if unit_content_from_commit:
        env["EGA_HARNESS_UNIT_CONTENT_FROM_COMMIT"] = "1"
    if user_exists:
        env["EGA_HARNESS_USER_EXISTS"] = "1"
    if env_extra:
        env.update(env_extra)
    if services_active:
        statedir = os.path.join(sandbox, ".systemctl-state.d")
        os.makedirs(statedir, exist_ok=True)
        for unit in ("ega-update-api", "ega-update-worker",
                     "cloudflared-ega-update"):
            _write(os.path.join(statedir, unit), "active\n")
        _write(os.path.join(sandbox, ".systemctl-state"), "active\n")
    lock_handle = None
    if lock_held:
        lock_path = os.path.join(sandbox, "opt", "ega-update", "deploy.lock")
        lock_handle = open(lock_path, "a+", encoding="utf-8")
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        proc = subprocess.run(
            ["/bin/bash", script_path] + args, env=env,
            capture_output=True, text=True, timeout=180)
    finally:
        if lock_handle is not None:
            fcntl.flock(lock_handle, fcntl.LOCK_UN)
            lock_handle.close()
    with open(events, "r", encoding="utf-8") as fh:
        event_lines = [ln.strip() for ln in fh.read().splitlines() if ln]
    return proc, sandbox, event_lines, env, {
        "state": state, "db": db_path, "backups": backups, "etc": etc,
        "prefix": prefix, "current": current, "releases": releases,
        "systemd": systemd_dir}

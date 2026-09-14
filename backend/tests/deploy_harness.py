"""Executable install.sh/upgrade.sh sandbox harness (hermetic; PATH shims).

Runs the REAL deploy scripts against a rewritten path layout
(/etc/ega-update, /opt/ega-update, /var/lib/ega-update, /etc/systemd and
/tmp targets are redirected into a tmp_path sandbox) with every
mutating/privileged command shimmed through a PATH shim directory. Each
shim records an event into an events log, so tests can assert the
OBSERVED order of host/runtime mutations relative to the drain +
quiescence maintenance boundary.

Safety: no real account/group/systemd/ACL/filesystem mutation is
possible — the rewritten constants point at the sandbox and every
external command that could touch the host is shimmed. Only the deploy
scripts under test are executed, never any service.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
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
    record "READINESS_OK"
    printf '{"state":"ready"}\\n'
    exit 0
  fi
  exit 0
fi
case "${1:-}" in
  *validate-release.py*) record "VALIDATE_RELEASE"; exit 0 ;;
  *owner_env*) record "RELEASE_PY_OWNER_ENV"; exit 0 ;;
esac
exit 0
"""

_FAKE_VENV_PIP = """\
#!/usr/bin/env bash
record() { printf '%s\\n' "$*" >> "$EGA_HARNESS_EVENTS"; }
record "PIP $*"
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
          ega-update) exit 1 ;;
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
        command /usr/bin/touch "$@"
        """)
    _shim(bin_dir, "mkdir", """
        record "MKDIR $*"
        command /usr/bin/mkdir "$@"
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
        case "${1:-}" in
          is-active)
            [ "$(cat "$state_file" 2>/dev/null)" = "active" ] && exit 0 || exit 3 ;;
          stop)
            printf 'stopped\\n' > "$state_file"
            record "SYSTEMCTL $*"
            exit 0 ;;
          start|restart)
            printf 'active\\n' > "$state_file"
            record "SYSTEMCTL $*"
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
        mkdir -p "$dest/backend/app/static" "$dest/backend/migrations" \\
                 "$dest/systemd/user" "$dest/deploy/etc"
        : > "$dest/backend/app/static/index.html"
        : > "$dest/backend/requirements.txt"
        : > "$dest/backend/app/__init__.py"
        : > "$dest/systemd/ega-update-api.service"
        : > "$dest/systemd/ega-update-worker.service"
        : > "$dest/systemd/ega-update-runner@.service"
        : > "$dest/systemd/user/ega-update-runner@.service"
        exit 0
        """)
    _shim(bin_dir, "python3", """\
        record "PYTHON3 $*"
        if [ "${1:-}" = "-m" ]; then
          case "${2:-}" in
            backend.app.config_cli)
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
                provision) record "PROVISION" ;;
                probe) record "TRANSIENT_PROBE" ;;
                verify) record "TRANSIENT_VERIFY" ;;
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
            *) exit 0 ;;
          esac
        fi
        if [ "${1:-}" = "-c" ]; then exit 0; fi
        case "${1:-}" in
          *quiescence-check.py*) record "QUIESCE"; exit 0 ;;
          *validate-archive.py*) record "ARCHIVE_VALIDATE"; exit 0 ;;
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
                      ready_fail=False, services_active=False):
    """Run install.sh/upgrade.sh in a sandbox. Returns (proc, sandbox,
    events, env)."""
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
    _write(os.path.join(etc, "secrets.env"), "", 0o640)
    _write(os.path.join(etc, "inventory.json"), '{"tools": {}}', 0o640)

    if db_exists:
        _write(db_path, "sqlite-placeholder\n", 0o660)
    if preexisting_drain:
        _write(os.path.join(state, "drain"), "")

    prev_release = os.path.join(sandbox, "opt", "ega-update", "releases",
                                "0" * 40)
    if existing_deploy:
        os.makedirs(os.path.join(prev_release, "venv", "bin"),
                    exist_ok=True)
        _write(os.path.join(prev_release, "venv", "bin", "python"),
               _FAKE_VENV_PY)
        _write(os.path.join(prev_release, "venv", "bin", "pip"),
               _FAKE_VENV_PIP)
        link = os.path.join(sandbox, "opt", "ega-update", "current")
        if os.path.islink(link) or os.path.exists(link):
            os.unlink(link)
        os.symlink(prev_release, link)

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
        "EGA_HARNESS_FAKE_PY": fake_py,
        "EGA_HARNESS_FAKE_PIP": fake_pip,
    })
    if ready_fail:
        env["EGA_HARNESS_READY_FAIL"] = "1"
    if services_active:
        _write(os.path.join(sandbox, ".systemctl-state"), "active\n")
    proc = subprocess.run(
        ["/bin/bash", script_path] + args, env=env,
        capture_output=True, text=True, timeout=180)
    with open(events, "r", encoding="utf-8") as fh:
        event_lines = [ln.strip() for ln in fh.read().splitlines() if ln]
    return proc, sandbox, event_lines, env, {"state": state, "db": db_path,
                                             "backups": backups, "etc": etc}

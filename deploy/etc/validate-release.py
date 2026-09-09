#!/usr/bin/env python3
"""Release validator (UI/DEPLOY owned). Python 3.10 compatible, stdlib only.

Usage:
  validate-release.py --release DIR --config FILE [--only SECTION] [--skip-owner-check]
  validate-release.py --check-compat OLD_RELEASE NEW_RELEASE

Exit 0 ok / 3 blocked+reason (2 = invalid invocation). Prints "ok" or
"blocked: <reason>" on stdout (reason is safe identifiers only, never
secret values).

Sections (--only, repeatable or comma-separated; default all):
  migrations, config, port, tunnel, secrets, frontend, manifest, hashes

Checks (frozen interface section 5):
- migration ledger validates (imports backend.app.db from the release root
  on sys.path — sys.path is set to DIR explicitly here).
- no CHANGEME/placeholder secrets in the effective config.
- listen_port consistency (config vs systemd api unit Environment default,
  honoring the install-rendered drop-in
  /etc/systemd/system/ega-update-api.service.d/10-port.conf when present).
- tunnel hostname non-placeholder + credential file exists with mode<=0600.
- secrets files exist with the expected owner:group/mode table.
- staged frontend index.html present.
- release MANIFEST (sha256 of backend/**) verifies against files staged at
  stage time (validator only verifies a MANIFEST written at stage time via
  sha256sum; it never writes one).
- requirements carry hashes (--require-hashes REQUIRED; missing hashes file
  content is blocked with a message — generate during authorized release
  prep, never fabricated here).

Rollback compat (--check-compat OLD NEW): exits 0 when the prior release
OLD can run against a DB migrated by NEW (SCHEMA_VERSION equality gate);
else 3 blocked with a manual-recovery message. Never inspects live DB state.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys

EXIT_OK = 0
EXIT_BLOCKED = 3
EXIT_USAGE = 2

PLACEHOLDER_PATTERNS = ("CHANGEME", "changeme", "EXAMPLE", "example.invalid",
                        ".invalid", "placeholder", "PLACEHOLDER", "TODO",
                        "CHANGEME-")

API_UNIT_REL = os.path.join("systemd", "ega-update-api.service")
FRONTEND_INDEX_REL = os.path.join("backend", "app", "static", "index.html")
REQUIREMENTS_REL = os.path.join("backend", "requirements.txt")
MANIFEST_NAME = "MANIFEST"
TUNNEL_CONFIG_DEFAULT = "/etc/ega-update/cloudflared/config.yml"
PORT_DROPIN_DEFAULT = ("/etc/systemd/system/ega-update-api.service.d/"
                       "10-port.conf")

# Secrets table: path -> (required, mode, owner, group). Owner/group are
# checked when available; --skip-owner-check limits the gate to modes for
# portable test environments. S01: secrets.env is REQUIRED (fresh
# installs provision it; readiness independently requires a readable
# secret source) and EXACTLY 0640 root:ega-update — group-readable by
# design, because the ubuntu worker and the ega-update API both read it
# via group membership (0600 root-owned would fail readiness for both
# service users, who are not root). Never world-readable, never
# owner-only. csrf.secret stays 0600 (API-only inline fallback path).
SECRETS_TABLE = {
    "config.json": (True, 0o640, "root", "ega-update"),
    "api.env": (True, 0o640, "root", "ega-update"),
    "worker.env": (True, 0o640, "root", "ega-update"),
    "csrf.secret": (True, 0o600, "root", "ega-update"),
    "tunnel.env": (False, 0o600, "root", "ega-update"),
    "secrets.env": (True, 0o640, "root", "ega-update"),
    "cloudflared/credentials.json": (True, 0o600, "root", "ega-update"),
    "cloudflared/config.yml": (True, 0o640, "root", "ega-update"),
}


def _fail(reason):
    # type: (str) -> int
    sys.stdout.write("blocked: %s\n" % reason[:1000])
    sys.stdout.flush()
    return EXIT_BLOCKED


def _ok():
    # type: () -> int
    sys.stdout.write("ok\n")
    sys.stdout.flush()
    return EXIT_OK


def _contains_placeholder(value):
    # type: (object) -> bool
    if not isinstance(value, str) or not value:
        return False
    for pat in PLACEHOLDER_PATTERNS:
        if pat in value:
            return True
    return False


def _walk_strings(obj):
    # type: (object) -> Any
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            for s in _walk_strings(v):
                yield s
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            for s in _walk_strings(v):
                yield s


def check_migrations(release):
    # type: (str) -> str
    """Import backend.app.db from the release root; return '' ok or reason."""
    if not os.path.isdir(release):
        return "release dir missing: %s" % release
    # Frozen: set sys.path to DIR explicitly before importing release code.
    if release not in sys.path:
        sys.path.insert(0, release)
    # Force re-import from this release root when the validator is reused.
    for mod in [m for m in list(sys.modules) if m == "backend.app.db"
                or m.startswith("backend.app.db.")]:
        try:
            del sys.modules[mod]
        except Exception:
            pass
    try:
        from backend.app import db as db_mod  # type: ignore
    except Exception as exc:
        return "migration ledger import failed: %s" % str(exc)[:300]
    try:
        ver = getattr(db_mod, "SCHEMA_VERSION", None)
        if ver is None:
            # Engine names the constant CODE_VERSION (same contract as
            # _schema_version_of below); accept either, never block a
            # real release on the alias.
            ver = getattr(db_mod, "CODE_VERSION", None)
        if not isinstance(ver, int) or ver < 1:
            return "db SCHEMA_VERSION invalid: %r" % (ver,)
    except Exception as exc:
        return "db version unreadable: %s" % str(exc)[:200]
    try:
        has_migrate = callable(getattr(db_mod, "migrate", None))
        has_connect = callable(getattr(db_mod, "connect", None))
        if not has_migrate or not has_connect:
            return "db missing migrate/connect"
    except Exception as exc:
        return "db api unreadable: %s" % str(exc)[:200]
    mig_dir = os.path.join(release, "backend", "migrations")
    if not os.path.isdir(mig_dir):
        return "migrations dir missing: %s" % mig_dir
    try:
        files = sorted(f for f in os.listdir(mig_dir) if f.endswith(".sql"))
    except Exception as exc:
        return "migrations unreadable: %s" % str(exc)[:200]
    if not files:
        return "no migration files in %s" % mig_dir
    seen = set()
    for name in files:
        m = re.match(r"^(\d{3})_.+\.sql$", name)
        if not m:
            return "migration not numbered NNN_*.sql: %s" % name
        num = int(m.group(1))
        if num in seen:
            return "duplicate migration number: %s" % name
        seen.add(num)
    return ""


def _load_config_file(path):
    # type: (str) -> Any
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("config must be a JSON object")
    return data


def check_config_placeholders(config_path):
    # type: (str) -> str
    try:
        data = _load_config_file(config_path)
    except Exception as exc:
        return "config unreadable: %s" % str(exc)[:300]
    # Effective csrf_secret: inline value OR csrf_secret_file content.
    csrf_inline = str(data.get("csrf_secret", "") or "")
    csrf_file = str(data.get("csrf_secret_file", "") or
                    "/etc/ega-update/csrf.secret")
    csrf_effective = csrf_inline
    if _contains_placeholder(csrf_inline) or not csrf_inline:
        try:
            if csrf_file and os.path.isfile(csrf_file):
                with open(csrf_file, "r", encoding="utf-8",
                          errors="replace") as fh:
                    for line in fh.read().splitlines():
                        s = line.strip()
                        if s:
                            csrf_effective = s
                            break
        except Exception:
            pass
    # Scan every config string for placeholders, exempting the inline
    # csrf_secret key itself when the file fallback provides the effective
    # value (the inline placeholder is then documentation, not the secret).
    for key, val in data.items():
        if key == "csrf_secret" and not _contains_placeholder(csrf_effective) \
                and csrf_effective and len(csrf_effective) >= 16:
            continue
        if isinstance(val, str) and _contains_placeholder(val):
            return "config key %s still holds a placeholder value" % key
        if isinstance(val, (dict, list)):
            for s in _walk_strings(val):
                if _contains_placeholder(s):
                    return ("config key %s still holds a placeholder value"
                            % key)
    # Critical keys must be present and non-placeholder in effect.
    for key in ("team_domain", "audience", "public_origin"):
        val = str(data.get(key, "") or "")
        if not val or _contains_placeholder(val):
            return "config key %s missing or placeholder" % key
    if not data.get("owner_emails"):
        return "config owner_emails missing"
    if not csrf_effective or _contains_placeholder(csrf_effective):
        return "csrf secret missing or placeholder (csrf_secret/csrf.secret)"
    if len(csrf_effective) < 16:
        return "csrf secret too short"
    return ""


def _parse_env_port_file(path):
    # type: (str) -> Any
    """Return EGA_LISTEN_PORT from a systemd Environment/EnvironmentFile line."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return None
    m = re.search(r"EGA_LISTEN_PORT\s*=\s*(\d+)", text)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def check_port(release, config_path, dropin_path=PORT_DROPIN_DEFAULT):
    # type: (str, str, str) -> str
    try:
        data = _load_config_file(config_path)
    except Exception as exc:
        return "config unreadable: %s" % str(exc)[:200]
    try:
        cfg_port = int(data.get("listen_port", 8771))
    except Exception:
        return "config listen_port invalid"
    unit_path = os.path.join(release, API_UNIT_REL)
    unit_port = _parse_env_port_file(unit_path)
    if unit_port is None:
        return "api unit missing EGA_LISTEN_PORT default: %s" % unit_path
    effective = unit_port
    try:
        if dropin_path and os.path.isfile(dropin_path):
            drop_port = _parse_env_port_file(dropin_path)
            if drop_port is not None:
                effective = drop_port
    except Exception:
        pass
    if effective != cfg_port:
        return ("listen_port mismatch: config=%d effective=%d "
                "(unit default=%d; render the install drop-in 10-port.conf "
                "from config)") % (cfg_port, effective, unit_port)
    return ""


def check_tunnel(config_path, tunnel_config=TUNNEL_CONFIG_DEFAULT):
    # type: (str, str) -> str
    del config_path  # tunnel path is fixed; config only gates placeholders.
    if not os.path.isfile(tunnel_config):
        return "tunnel config missing: %s" % tunnel_config
    try:
        with open(tunnel_config, "r", encoding="utf-8",
                  errors="replace") as fh:
            text = fh.read()
    except Exception as exc:
        return "tunnel config unreadable: %s" % str(exc)[:200]
    if _contains_placeholder(text):
        return ("tunnel config still holds placeholder "
                "(hostname/tunnel-id must be replaced)")
    # Hostname present and non-placeholder.
    hostnames = re.findall(r"hostname\s*:\s*(\S+)", text)
    if not hostnames:
        return "tunnel config has no hostname ingress"
    for h in hostnames:
        if _contains_placeholder(h) or h.endswith(".invalid"):
            return "tunnel hostname is placeholder: %s" % h[:100]
        if "." not in h:
            return "tunnel hostname invalid: %s" % h[:100]
    # Service must target loopback.
    if "127.0.0.1" not in text:
        return "tunnel ingress must target http://127.0.0.1:<port>"
    # Credentials file exists with mode<=0600.
    m = re.search(r"credentials-file\s*:\s*(\S+)", text)
    cred = m.group(1) if m else "/etc/ega-update/cloudflared/credentials.json"
    if not os.path.isfile(cred):
        return "tunnel credentials file missing: %s" % cred
    try:
        mode = stat.S_IMODE(os.stat(cred).st_mode)
    except Exception as exc:
        return "tunnel credentials unreadable: %s" % str(exc)[:200]
    if mode & 0o077:
        return ("tunnel credentials file mode %04o too open "
                "(require <=0600): %s" % (mode, cred))
    return ""


def _etc_root_for(config_path):
    # type: (str) -> str
    d = os.path.dirname(os.path.abspath(config_path))
    # /etc/ega-update/config.json -> /etc/ega-update
    return d


def check_secrets(config_path, skip_owner_check=False):
    # type: (str, bool) -> str
    etc_root = _etc_root_for(config_path)
    try:
        import pwd as _pwd  # type: ignore
        import grp as _grp  # type: ignore
    except Exception:
        _pwd = None  # type: ignore
        _grp = None  # type: ignore
    for rel, (required, mode, owner, group) in SECRETS_TABLE.items():
        if rel == "config.json":
            path = config_path
        else:
            path = os.path.join(etc_root, rel)
        if not os.path.exists(path):
            if required:
                return "secrets file missing: %s" % path
            continue
        try:
            st = os.stat(path)
        except Exception as exc:
            return "secrets file unreadable %s: %s" % (path, str(exc)[:150])
        actual_mode = stat.S_IMODE(st.st_mode)
        if actual_mode != mode:
            # Credentials/tokens require <=0600 semantics; shared files
            # require exactly 0640. Report the expected table value.
            if mode == 0o600 and (actual_mode & 0o077):
                return ("secrets file %s mode %04o too open (require 0600)"
                        % (path, actual_mode))
            if actual_mode != mode:
                return ("secrets file %s mode %04o (require %04o)"
                        % (path, actual_mode, mode))
        if skip_owner_check:
            continue
        if _pwd is not None:
            try:
                actual_owner = _pwd.getpwuid(st.st_uid).pw_name
            except Exception:
                actual_owner = str(st.st_uid)
            if actual_owner != owner:
                return ("secrets file %s owner %s (require %s:%s)"
                        % (path, actual_owner, owner, group))
        if _grp is not None:
            try:
                actual_group = _grp.getgrgid(st.st_gid).gr_name
            except Exception:
                actual_group = str(st.st_gid)
            if actual_group != group:
                return ("secrets file %s group %s (require %s:%s)"
                        % (path, actual_group, owner, group))
    return ""


def check_frontend(release):
    # type: (str) -> str
    path = os.path.join(release, FRONTEND_INDEX_REL)
    if not os.path.isfile(path):
        return ("staged frontend missing: %s "
                "(release must ship backend/app/static/index.html)") % path
    return ""


def check_hashes(release):
    # type: (str) -> str
    path = os.path.join(release, REQUIREMENTS_REL)
    if not os.path.isfile(path):
        return "requirements missing: %s" % path
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except Exception as exc:
        return "requirements unreadable: %s" % str(exc)[:200]
    if "--hash=" not in text and "--hash " not in text:
        return ("requirements lack hashes (pip --require-hashes REQUIRED; "
                "generate the hashed requirements during authorized release "
                "prep — never fabricate hashes here)")
    return ""


def check_manifest(release):
    # type: (str) -> str
    manifest = os.path.join(release, MANIFEST_NAME)
    if not os.path.isfile(manifest):
        return ("release MANIFEST missing: %s (stage step must write it via "
                "sha256sum over backend/** before validation)") % manifest
    try:
        with open(manifest, "r", encoding="utf-8", errors="replace") as fh:
            lines = [ln.strip() for ln in fh if ln.strip()]
    except Exception as exc:
        return "MANIFEST unreadable: %s" % str(exc)[:200]
    if not lines:
        return "MANIFEST empty"
    for ln in lines:
        parts = ln.split()
        if len(parts) < 2:
            return "MANIFEST malformed line: %s" % ln[:120]
        digest, rel = parts[0], parts[-1]
        if len(digest) != 64 or not re.fullmatch(r"[0-9a-f]{64}", digest):
            return "MANIFEST bad digest for %s" % rel[:150]
        target = os.path.join(release, rel)
        if not os.path.isfile(target):
            return "MANIFEST references missing file: %s" % rel[:150]
        h = hashlib.sha256()
        try:
            with open(target, "rb") as fh:
                for chunk in iter(lambda: fh.read(65536), b""):
                    h.update(chunk)
        except Exception as exc:
            return "MANIFEST unreadable file %s: %s" % (
                rel[:150], str(exc)[:150])
        if h.hexdigest() != digest.lower():
            return "MANIFEST integrity mismatch: %s" % rel[:150]
    return ""


def _schema_version_of(release):
    # type: (str) -> Any
    path = os.path.join(release, "backend", "app", "db.py")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except Exception:
        return None
    m = re.search(r"SCHEMA_VERSION\s*=\s*(\d+)", text)
    if not m:
        # CODE_VERSION fallback (future schema constant name).
        m = re.search(r"CODE_VERSION\s*=\s*(\d+)", text)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def check_compat(old_release, new_release):
    # type: (str, str) -> str
    old_v = _schema_version_of(old_release)
    new_v = _schema_version_of(new_release)
    if old_v is None:
        return "prior release schema unreadable: %s" % old_release
    if new_v is None:
        return "new release schema unreadable: %s" % new_release
    if old_v != new_v:
        return ("schema drift %s->%s: prior release cannot run the migrated "
                "DB — leave manual-recovery state, do not re-point current "
                "(see RUNBOOK migration-recovery)" % (old_v, new_v))
    return ""


def main(argv=None):
    # type: (Any) -> int
    ap = argparse.ArgumentParser(description="EGA release validator")
    ap.add_argument("--release", default="")
    ap.add_argument("--config", default="")
    ap.add_argument("--only", default="",
                    help="comma-separated subset: migrations,config,port,"
                    "tunnel,secrets,frontend,manifest,hashes")
    ap.add_argument("--check-compat", nargs=2, metavar=("OLD", "NEW"),
                    default=None)
    ap.add_argument("--skip-owner-check", action="store_true")
    ap.add_argument("--tunnel-config", default=TUNNEL_CONFIG_DEFAULT)
    ap.add_argument("--port-dropin", default=PORT_DROPIN_DEFAULT)
    args = ap.parse_args(argv)

    if args.check_compat is not None:
        old_rel, new_rel = args.check_compat
        reason = check_compat(old_rel, new_rel)
        if reason:
            return _fail(reason)
        return _ok()

    release = args.release
    config = args.config
    if not release or not config:
        sys.stderr.write("usage: validate-release.py --release DIR "
                         "--config FILE [--only SECTION]\n")
        return EXIT_USAGE
    only_raw = str(args.only or "").strip()
    if only_raw:
        wanted = set(s.strip() for s in only_raw.split(",") if s.strip())
    else:
        wanted = set(["migrations", "config", "port", "tunnel", "secrets",
                      "frontend", "manifest", "hashes"])

    order = ["migrations", "hashes", "manifest", "frontend", "config",
             "secrets", "port", "tunnel"]
    for section in order:
        if section not in wanted:
            continue
        if section == "migrations":
            reason = check_migrations(release)
        elif section == "hashes":
            reason = check_hashes(release)
        elif section == "manifest":
            reason = check_manifest(release)
        elif section == "frontend":
            reason = check_frontend(release)
        elif section == "config":
            reason = check_config_placeholders(config)
        elif section == "secrets":
            reason = check_secrets(config, args.skip_owner_check)
        elif section == "port":
            reason = check_port(release, config, args.port_dropin)
        elif section == "tunnel":
            reason = check_tunnel(config, args.tunnel_config)
        else:
            reason = "unknown section: %s" % section
        if reason:
            return _fail("%s: %s" % (section, reason))
    return _ok()


if __name__ == "__main__":
    raise SystemExit(main())

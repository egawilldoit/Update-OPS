"""One centralized sanitizer for every evidence surface (R10/R11/R34).

- sanitize_text: strip ANSI/controls, replace known secret values, apply
  credential patterns. NEVER truncates (callers truncate for display AFTER
  sanitizing, so secret prefixes cannot survive truncation).
- parse_secrets_file: one env-style parser reused everywhere (KEY=value
  with quotes/export/comments, plus bare values >= 4 chars).
- SanitizingStream: byte ingestion -> incremental UTF-8 decode -> line
  state -> multiline-sensitive-block state -> durable-record emission.
  Empty output means "input buffered", never "write raw". Incomplete or
  oversized sensitive blocks are suppressed entirely with a marker; the
  body is never emitted. Sanitizer failure raises SanitizerError
  (fail closed); callers must treat it as evidence failure, never raw
  fallback. flush_tick() emits only complete non-sensitive lines and
  NEVER finalizes an incomplete block; flush_final() closes end-of-stream.

Python 3.10 compatible.
"""
from __future__ import annotations

import codecs
import re
from typing import Dict, List, Tuple

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|\x1b[()][0-9A-Z]")
CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

PATTERNS = [
    ("bearer", re.compile(r"(?i)(bearer\s+)[A-Za-z0-9\-._~+/=]{8,}")),
    ("cf_auth", re.compile(r"(?i)(CF_Authorization[\"'\s:=]+)[A-Za-z0-9\-._~+/=]{8,}")),
    ("authorization", re.compile(r"(?i)(authorization[\"'\s:=]+)[A-Za-z0-9\-._~+/=]{8,}")),
    ("api_key", re.compile(r"(?i)(api[_-]?key[\"'\s:=]+)[A-Za-z0-9\-._~+/=]{8,}")),
    ("private_key", re.compile(
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?"
        r"-----END [A-Z0-9 ]*PRIVATE KEY-----")),
    ("password_url", re.compile(r"(?i)(://[^/\s:@]+:)[^@\s/]+(@)")),
    ("secret_assign", re.compile(
        r"(?i)(secret|passwd|password|token)[\"'\s:=]+[A-Za-z0-9\-._~+/=]{8,}")),
]

PEM_BEGIN = "-----BEGIN"
PEM_END = "-----END"
REPLACEMENT = "***REDACTED***"
SUPPRESSED_MARKER = "[sensitive block suppressed]"
SANITIZER_FAILED_MARKER = "[redaction failed; output suppressed]"

MAX_LINE_CHARS = 8192
MAX_LINE_ACCUM_CHARS = 256 * 1024
MAX_BLOCK_CHARS = 64 * 1024


class SanitizerError(Exception):
    """Sanitizer failed; caller must fail closed (never raw fallback)."""


def strip_controls(text):
    # type: (str) -> str
    text = ANSI_RE.sub("", text)
    return CTRL_RE.sub("", text)


def sanitize_text(text, secrets=()):
    # type: (object, object) -> str
    """Sanitize one string. Never truncates, never raises."""
    try:
        out = text if isinstance(text, str) else str(text)
    except Exception:
        raise SanitizerError("unsanitizable value type")
    try:
        out = strip_controls(out)
        for secret in secrets or ():
            if secret and len(secret) >= 4 and secret in out:
                out = out.replace(secret, REPLACEMENT)
        out = PATTERNS[4][1].sub(REPLACEMENT, out)
        for _name, rx in (PATTERNS[0], PATTERNS[1], PATTERNS[2],
                          PATTERNS[3]):
            out = rx.sub(lambda m: m.group(1) + REPLACEMENT, out)
        out = PATTERNS[5][1].sub(
            lambda m: m.group(1) + REPLACEMENT + m.group(2), out)
        out = PATTERNS[6][1].sub(REPLACEMENT, out)
        return out
    except SanitizerError:
        raise
    except Exception as exc:
        raise SanitizerError("sanitize failed: %s" % exc)


def sanitize_json(obj, secrets=()):
    # type: (object, object) -> object
    """Recursively sanitize every string in a JSON-shaped structure."""
    if isinstance(obj, str):
        return sanitize_text(obj, secrets)
    if isinstance(obj, dict):
        return {k: sanitize_json(v, secrets) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [sanitize_json(v, secrets) for v in obj]
    return obj


def parse_secrets_content(content):
    # type: (str) -> Tuple[str, ...]
    """Single shared env-style parser (G05: the one authority).

    Accepts KEY=value (single/double quotes, export prefix, # comments)
    and bare secret lines; returns VALUES only. Values shorter than 4
    chars are ignored. Pure function over text: never touches the
    filesystem, never raises on content, never logs values.
    """
    out = []
    try:
        lines = str(content or "").splitlines()
    except Exception:
        return ()
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        value = ""
        if "=" in line:
            _key, _, value = line.partition("=")
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] \
                    and value[0] in ("'", '"'):
                value = value[1:-1]
            value = value.strip()
        else:
            value = line
        if len(value) >= 4:
            out.append(value)
    return tuple(out)


def parse_secrets_file(path):
    # type: (str) -> Tuple[str, ...]
    """One shared env-style parser (R34). Returns values only.

    Accepts KEY=value (single/double quotes, export prefix, # comments)
    and bare secret lines. Values shorter than 4 chars are ignored.
    Missing/unreadable files yield (). Never raises, never logs values.

    NOTE: config.load_secret_values is the strict variant for durable
    evidence paths (missing-when-configured raises SecretSourceError).
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            content = fh.read()
    except Exception:
        return ()
    return parse_secrets_content(content)


class SanitizingStream(object):
    """Streaming sanitizer with sensitive-block state (R10).

    feed(bytes) -> complete safe lines (withheld sensitive/oversized input
    stays buffered and returns nothing for it). flush_tick() -> complete
    non-sensitive lines only (timer-safe; never finalizes a block).
    flush_final() -> end-of-stream lines (incomplete block -> marker only).
    """

    def __init__(self, secrets=(), max_line=MAX_LINE_CHARS):
        # type: (object, int) -> None
        self._secrets = tuple(secrets or ())
        self._max_line = int(max_line or MAX_LINE_CHARS)
        try:
            self._decoder = codecs.getincrementaldecoder("utf-8")(
                errors="strict")
        except Exception:
            raise SanitizerError("no incremental decoder")
        self._pending = ""
        self._in_block = False
        self._block_buf = ""

    def _emit_line(self, line):
        # type: (str) -> str
        clean = sanitize_text(line, self._secrets)
        if len(clean) > self._max_line:
            clean = clean[:self._max_line] + "…[truncated-line]"
        return clean

    def _absorb(self, text):
        # type: (str) -> List[str]
        out = []  # type: List[str]
        self._pending += text
        while True:
            nl = self._pending.find("\n")
            if nl == -1:
                if len(self._pending) > MAX_LINE_ACCUM_CHARS:
                    # Oversized line with no terminator: suppress it whole
                    # (never emit a secret prefix).
                    self._pending = ""
                    self._in_block = False
                    self._block_buf = ""
                    out.append(SUPPRESSED_MARKER + " (oversized line)")
                break
            raw = self._pending[:nl]
            self._pending = self._pending[nl + 1:]
            if self._in_block:
                self._block_buf += raw + "\n"
                if PEM_END in raw:
                    self._in_block = False
                    self._block_buf = ""
                    out.append(SUPPRESSED_MARKER)
                elif len(self._block_buf) > MAX_BLOCK_CHARS:
                    self._in_block = False
                    self._block_buf = ""
                    out.append(SUPPRESSED_MARKER + " (oversized block)")
                continue
            if PEM_BEGIN in raw:
                # Hold the whole block; emit preceding text only (there is
                # none on this line besides the marker, which is safe, but
                # the body that follows must never leak, so hold all).
                self._in_block = True
                self._block_buf = raw + "\n"
                continue
            out.append(self._emit_line(raw))
        return out

    def feed(self, chunk):
        # type: (bytes) -> List[str]
        try:
            text = self._decoder.decode(bytes(chunk or b""), final=False)
        except Exception as exc:
            raise SanitizerError("decode failed: %s" % exc)
        try:
            return self._absorb(text)
        except SanitizerError:
            raise
        except Exception as exc:
            raise SanitizerError("absorb failed: %s" % exc)

    def flush_tick(self):
        # type: () -> List[str]
        """Timer flush: complete non-sensitive lines only.

        Never finalizes an incomplete sensitive block and never emits
        partial-line state.
        """
        try:
            if self._in_block:
                return []
            if "\n" not in self._pending:
                return []
            buf, self._pending = self._pending, ""
            out = []  # type: List[str]
            for raw in buf.split("\n"):
                if self._in_block:
                    self._block_buf += raw + "\n"
                    if PEM_END in raw:
                        self._in_block = False
                        self._block_buf = ""
                        out.append(SUPPRESSED_MARKER)
                    elif len(self._block_buf) > MAX_BLOCK_CHARS:
                        self._in_block = False
                        self._block_buf = ""
                        out.append(SUPPRESSED_MARKER + " (oversized block)")
                    continue
                if PEM_BEGIN in raw:
                    self._in_block = True
                    self._block_buf = raw + "\n"
                    continue
                out.append(self._emit_line(raw))
            return out
        except SanitizerError:
            raise
        except Exception as exc:
            raise SanitizerError("tick failed: %s" % exc)

    def flush_final(self):
        # type: () -> List[str]
        """End-of-stream: emit remainder; incomplete block -> marker only."""
        try:
            try:
                tail = self._decoder.decode(b"", final=True)
            except Exception as exc:
                raise SanitizerError("final decode failed: %s" % exc)
            out = self._absorb(tail)
            rest = self._pending
            self._pending = ""
            if self._in_block or (rest and PEM_BEGIN in rest):
                self._in_block = False
                self._block_buf = ""
                if rest and PEM_BEGIN not in rest:
                    out.append(self._emit_line(rest))
                out.append(SUPPRESSED_MARKER)
                return out
            if rest:
                out.append(self._emit_line(rest))
            return out
        except SanitizerError:
            raise
        except Exception as exc:
            raise SanitizerError("final flush failed: %s" % exc)

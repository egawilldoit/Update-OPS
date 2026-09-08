"""Log redaction + streaming decode. Best-effort; logs remain private.

- Streaming decoder buffers incomplete lines across chunk boundaries so a
  secret split across reads is still redacted.
- Strips ANSI/terminal control sequences, caps line length.
- Replaces configured secret values plus credential/header patterns before
  writing anywhere. Raw output is never persisted.
Python 3.10 compatible.
"""
from __future__ import annotations

import re
from typing import List, Tuple

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|\x1b[()][0-9A-Z]")
CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

PATTERNS = [
    # (name, regex)
    ("bearer", re.compile(r"(?i)(bearer\s+)[A-Za-z0-9\-._~+/=]{8,}")),
    ("cf_auth", re.compile(r"(?i)(CF_Authorization[\"'\s:=]+)[A-Za-z0-9\-._~+/=]{8,}")),
    ("authorization", re.compile(r"(?i)(authorization[\"'\s:=]+)[A-Za-z0-9\-._~+/=]{8,}")),
    ("api_key", re.compile(r"(?i)(api[_-]?key[\"'\s:=]+)[A-Za-z0-9\-._~+/=]{8,}")),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----")),
    ("password_url", re.compile(r"(?i)(://[^/\s:@]+:)[^@\s/]+(@)")),
    ("secret_assign", re.compile(r"(?i)(secret|passwd|password|token)[\"'\s:=]+[A-Za-z0-9\-._~+/=]{8,}")),
]

MAX_LINE = 8192
REPLACEMENT = "***REDACTED***"


def strip_controls(text):
    # type: (str) -> str
    text = ANSI_RE.sub("", text)
    return CTRL_RE.sub("", text)


def redact_text(text, secrets=()):
    # type: (str, tuple) -> str
    for secret in secrets or ():
        if secret and len(secret) >= 4 and secret in text:
            text = text.replace(secret, REPLACEMENT)
    text = PATTERNS[4][1].sub(REPLACEMENT, text)
    text = PATTERNS[0][1].sub(lambda m: m.group(1) + REPLACEMENT, text)
    text = PATTERNS[1][1].sub(lambda m: m.group(1) + REPLACEMENT, text)
    text = PATTERNS[2][1].sub(lambda m: m.group(1) + REPLACEMENT, text)
    text = PATTERNS[3][1].sub(lambda m: m.group(1) + REPLACEMENT, text)
    text = PATTERNS[5][1].sub(lambda m: m.group(1) + REPLACEMENT + m.group(2), text)
    text = PATTERNS[6][1].sub(REPLACEMENT, text)
    return text


class StreamRedactor:
    """Incremental UTF-8 decoder + line buffer + redaction."""

    def __init__(self, secrets=()):
        # type: (tuple) -> None
        self._buf = bytearray()
        self._secrets = tuple(secrets or ())

    def feed(self, chunk):
        # type: (bytes) -> List[str]
        self._buf.extend(chunk)
        text = self._buf.decode("utf-8", errors="replace")
        parts = text.split("\n")
        complete, remainder = parts[:-1], parts[-1]
        self._buf = bytearray(remainder.encode("utf-8", errors="replace"))
        out = []
        for line in complete:
            line = strip_controls(line)
            if len(line) > MAX_LINE:
                line = line[:MAX_LINE] + "…[truncated-line]"
            out.append(redact_text(line, self._secrets))
        return out

    def flush(self):
        # type: () -> List[str]
        if not self._buf:
            return []
        text = strip_controls(self._buf.decode("utf-8", errors="replace"))
        self._buf = bytearray()
        if len(text) > MAX_LINE:
            text = text[:MAX_LINE] + "…[truncated-line]"
        return [redact_text(text, self._secrets)]

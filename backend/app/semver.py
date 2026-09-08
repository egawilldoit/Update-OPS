"""Vendored strict SemVer 2.0 implementation (ADAPTER contributor owned).

Pending maintained-lib lock: this file is a minimal vendored implementation
used by opencode + t3 target resolution until a maintained SemVer library is
pinned in backend/requirements.pinned.txt (main agent owns pins). Do not
extend with ranges/constraints; only exact precedence + upgrade checks.

Strict SemVer 2.0 rules enforced:
- MAJOR.MINOR.PATCH numeric, no leading zeroes (0 allowed, 01 rejected).
- Prerelease: dot-separated identifiers [0-9A-Za-z-], non-empty, no empty
  segments, numeric identifiers MUST NOT include leading zeroes.
- Build metadata ignored for precedence but must be syntactically valid.
- Leading/trailing whitespace stripped; optional leading 'v'/'V' stripped
  for operator/npm convenience (documented leniency, not part of spec).
  Anything else malformed (empty string, missing parts, empty prerelease
  ids, numeric-empty ids, leading-zero numerics) raises/returns None.

Precedence (spec section 11):
- Compare major, minor, patch numerically.
- Release (no prerelease) outranks any prerelease with same core.
- Prerelease identifiers compared left to right:
  numeric identifiers compared NUMERICALLY (beta.10 > beta.2),
  alphanumeric compared lexically (ASCII),
  numeric always lower than alphanumeric,
  larger set wins when all preceding equal.

Python 3.10 compatible.
"""
from __future__ import annotations

import re
from functools import total_ordering

_SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|[0-9]*[a-zA-Z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9]\d*|[0-9]*[a-zA-Z-][0-9A-Za-z-]*))*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


def _strip_prefix(text):
    # type: (str) -> str
    cleaned = text.strip()
    if len(cleaned) >= 2 and cleaned[0] in ("v", "V") and cleaned[1].isdigit():
        cleaned = cleaned[1:]
    return cleaned


def _is_numeric_id(token):
    # type: (str) -> bool
    return bool(token) and all(c.isdigit() for c in token)


@total_ordering
class SemVer(object):
    """Parsed SemVer with precedence ordering (build ignored)."""

    def __init__(self, major, minor, patch, prerelease=(), build=""):
        # type: (int, int, int, tuple, str) -> None
        self.major = int(major)
        self.minor = int(minor)
        self.patch = int(patch)
        self.prerelease = tuple(prerelease)
        self.build = str(build or "")

    def core(self):
        # type: () -> tuple
        return (self.major, self.minor, self.patch)

    def _cmp_key(self):
        # type: () -> tuple
        # Release outranks prerelease: key 1 for release, 0 for prerelease.
        return (self.core(), 1 if not self.prerelease else 0)

    def __eq__(self, other):
        # type: (object) -> bool
        if not isinstance(other, SemVer):
            return NotImplemented
        if self.core() != other.core():
            return False
        return list(self.prerelease) == list(other.prerelease)

    def __lt__(self, other):
        # type: (object) -> bool
        if not isinstance(other, SemVer):
            return NotImplemented
        if self.core() != other.core():
            return self.core() < other.core()
        # Same core: release > prerelease.
        if not self.prerelease and not other.prerelease:
            return False
        if not self.prerelease:
            return False
        if not other.prerelease:
            return True
        # Both prerelease: identifier-wise comparison.
        for left, right in zip(self.prerelease, other.prerelease):
            if left == right:
                continue
            left_num = _is_numeric_id(left)
            right_num = _is_numeric_id(right)
            if left_num and right_num:
                return int(left) < int(right)
            if left_num and not right_num:
                return True
            if not left_num and right_num:
                return False
            return left < right
        return len(self.prerelease) < len(other.prerelease)

    def __hash__(self):
        # type: () -> int
        return hash((self.major, self.minor, self.patch, self.prerelease))

    def __str__(self):
        # type: () -> str
        base = "%d.%d.%d" % (self.major, self.minor, self.patch)
        if self.prerelease:
            base += "-" + ".".join(self.prerelease)
        if self.build:
            base += "+" + self.build
        return base

    def __repr__(self):
        # type: () -> str
        return "SemVer(%r)" % (str(self),)


def parse(text):
    # type: (str) -> SemVer
    """Strict parse; raises ValueError on malformed input."""
    if not isinstance(text, str):
        raise ValueError("not a string: %r" % (text,))
    cleaned = _strip_prefix(text)
    if not cleaned:
        raise ValueError("empty version")
    match = _SEMVER_RE.match(cleaned)
    if not match:
        raise ValueError("malformed SemVer: %r" % (text,))
    major, minor, patch = int(match.group(1)), int(match.group(2)), int(match.group(3))
    pre_raw = match.group(4) or ""
    build = match.group(5) or ""
    prerelease = tuple(pre_raw.split(".")) if pre_raw else ()
    # Defensive: regex already rejects empty ids and leading-zero numerics,
    # but re-verify for clarity (fail-closed).
    for ident in prerelease:
        if not ident:
            raise ValueError("empty prerelease identifier in %r" % (text,))
        if _is_numeric_id(ident) and len(ident) > 1 and ident[0] == "0":
            raise ValueError("leading-zero numeric prerelease id in %r" % (text,))
    return SemVer(major, minor, patch, prerelease, build)


def try_parse(text):
    # type: (object) -> object
    """Parse or None on malformed (fail-closed helper for adapters)."""
    try:
        if not isinstance(text, str):
            return None
        return parse(text)
    except (ValueError, TypeError, AttributeError):
        return None


def compare(left, right):
    # type: (str, str) -> object
    """-1/0/1 on valid pairs; None when either side malformed."""
    parsed_l = try_parse(left) if isinstance(left, str) else None
    parsed_r = try_parse(right) if isinstance(right, str) else None
    if parsed_l is None or parsed_r is None:
        return None
    if parsed_l == parsed_r:
        return 0
    return -1 if parsed_l < parsed_r else 1


def is_upgrade(before, target):
    # type: (str, str) -> bool
    """True iff both valid and target strictly greater than before.

    Equal, downgrade, and malformed (either side) yield False so callers
    block downgrades/malformed targets. Empty before (unknown current)
    with a valid target yields True (fresh install/unknown baseline is
    an upgrade candidate, never a downgrade).
    """
    if not isinstance(target, str) or not target.strip():
        return False
    parsed_t = try_parse(target)
    if parsed_t is None:
        return False
    if before is None or (isinstance(before, str) and not before.strip()):
        return True
    parsed_b = try_parse(before) if isinstance(before, str) else None
    if parsed_b is None:
        return False
    return parsed_t > parsed_b

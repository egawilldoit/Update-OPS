"""API dependencies: auth, CSRF/Origin/JSON guards, rate limit, pagination.

Route handlers in routes.py use these helpers. No subprocess use here.
Python 3.10 compatible.
"""
from __future__ import annotations

import time
import uuid
from typing import Dict, List, Optional, Tuple

from fastapi import Request
from fastapi.responses import JSONResponse

from .. import auth as auth_lib
from ..config import settings

TOOL_IDS = ("hermes", "opencode", "codex", "t3")

NO_STORE = {"Cache-Control": "no-store"}

# Rate limits (R21): separate read-polling budget from mutation abuse
# protection so normal UI polling can never consume its own admission
# limit. Buckets are per-identity sliding 60s windows. 429 carries
# Retry-After (seconds until the oldest bucket entry ages out).
READ_LIMIT_PER_MIN = 600
WRITE_LIMIT_PER_MIN = 60
_rate_buckets = {}  # type: Dict[str, List[float]]


def _bucket_limit(kind):
    # type: (str) -> int
    if kind == "write":
        try:
            return int(getattr(settings, "rate_limit_per_min", 0)
                       or WRITE_LIMIT_PER_MIN)
        except Exception:
            return WRITE_LIMIT_PER_MIN
    return READ_LIMIT_PER_MIN


def check_rate_limit(identity, kind="read"):
    # type: (str, str) -> bool
    """Sliding 60s window per identity per bucket kind ("read"|"write").

    Legacy single-argument callers land in the read bucket.
    """
    limit = _bucket_limit(kind if kind in ("read", "write") else "read")
    now = time.time()
    key = "%s\x00%s" % (kind, identity)
    bucket = _rate_buckets.get(key, [])
    bucket = [t for t in bucket if now - t < 60.0]
    if len(bucket) >= limit:
        _rate_buckets[key] = bucket
        return False
    bucket.append(now)
    _rate_buckets[key] = bucket
    return True


def retry_after_s(identity, kind="read"):
    # type: (str, str) -> int
    """Seconds until the oldest bucket entry ages out (for Retry-After)."""
    now = time.time()
    key = "%s\x00%s" % (kind, identity)
    bucket = [t for t in _rate_buckets.get(key, []) if now - t < 60.0]
    if not bucket:
        return 0
    return max(0, int(60.0 - (now - min(bucket))) + 1)


def new_request_id():
    # type: () -> str
    return uuid.uuid4().hex[:12]


def error_envelope(status, code, message, details="", request_id=""):
    # type: (int, str, str, str, str) -> JSONResponse
    rid = request_id or new_request_id()
    # Never include secrets: callers must pass only safe detail strings.
    return JSONResponse(
        status_code=status,
        content={"code": code, "message": message,
                 "details": details or "", "request_id": rid},
        headers=dict(NO_STORE),
    )


def extract_token(request):
    # type: (Request) -> str
    # Primary: Cf-Access-Jwt-Assertion header; fallback: CF_Authorization cookie.
    header_token = request.headers.get("cf-access-jwt-assertion", "")
    if header_token:
        return header_token.strip()
    # Starlette parses cookies case-sensitively; try both key spellings.
    cookie_token = request.cookies.get("CF_Authorization", "")
    if cookie_token:
        return cookie_token.strip()
    # Manual Cookie header fallback (defensive; same value, no logging).
    raw_cookie = request.headers.get("cookie", "")
    if raw_cookie and "CF_Authorization" in raw_cookie:
        for part in raw_cookie.split(";"):
            name, _, value = part.partition("=")
            if name.strip() == "CF_Authorization" and value.strip():
                return value.strip()
    return ""


def authenticate(request):
    # type: (Request) -> Tuple[Optional[Dict], Optional[JSONResponse], str]
    """Validate the Access JWT. Returns (claims, error_response, request_id).

    Fail closed: any validation failure yields an error response.
    401 for missing/invalid (incl. expired/forged/wrong-audience);
    403 only for a validly-signed token whose identity is not the owner.
    """
    rid = new_request_id()
    token = extract_token(request)
    if not token:
        return None, error_envelope(
            401, "unauthorized", "missing credentials", "", rid), rid
    claims, err = auth_lib.validate_access_token(
        token,
        settings.team_domain,
        settings.audience,
        list(settings.owner_emails),
        ttl_s=settings.jwks_cache_ttl_s,
    )
    if claims is None:
        if err == "identity_not_authorized":
            return None, error_envelope(
                403, "forbidden", "identity not authorized", "", rid), rid
        # Map every other failure (expired, wrong audience/issuer,
        # malformed, bad algorithm, JWKS unavailable) to 401 without
        # leaking token internals into details.
        return None, error_envelope(
            401, "unauthorized", "invalid credentials", "", rid), rid
    return claims, None, rid


def subject_of(claims):
    # type: (Dict) -> str
    email = str(claims.get("email", "") or "").strip().lower()
    if email:
        return email
    return str(claims.get("sub", "") or "")


def rate_limited_response(request_id="", retry_after=60):
    # type: (str, int) -> JSONResponse
    try:
        retry_s = max(0, int(retry_after))
    except (TypeError, ValueError):
        retry_s = 60
    resp = error_envelope(
        429, "rate_limited",
        "rate limit exceeded; retry after %ds" % retry_s, "",
        request_id or new_request_id())
    resp.headers["Retry-After"] = str(retry_s)
    return resp


def check_body_limit(request):
    # type: (Request) -> bool
    """True if the request size looks acceptable.

    First line of defence is the Content-Length header vs
    config.body_limit_bytes. Starlette/FastAPI reads the full body for JSON
    models afterwards; oversized chunked bodies without a length header are
    still bounded by the reverse-proxy/client timeouts in deploy config.
    Oversize maps to 422 invalid_request to stay inside the contract's
    401/403/409/422/503 envelope mapping.
    """
    try:
        limit = int(getattr(settings, "body_limit_bytes", 256 * 1024))
    except Exception:
        limit = 256 * 1024
    raw = request.headers.get("content-length", "")
    if raw:
        try:
            if int(raw) > limit:
                return False
        except ValueError:
            return False
    return True


async def read_bounded_body(request, limit=None):
    # type: (Request, object) -> bytes
    """Read the body enforcing the byte limit WHILE streaming (R16).

    Raises ValueError on overflow instead of buffering unlimited input
    before enforcement. Returns b"" for empty bodies.
    """
    if limit is None:
        try:
            limit = int(getattr(settings, "body_limit_bytes",
                                256 * 1024) or 256 * 1024)
        except Exception:
            limit = 256 * 1024
    chunks = []
    total = 0
    try:
        stream = request.stream()
    except Exception:
        # No streaming interface (e.g. test doubles): bounded single read.
        try:
            body = await request.body()
        except Exception:
            return b""
        try:
            raw = bytes(body or b"")
        except Exception:
            return b""
        if len(raw) > int(limit):
            raise ValueError("request body too large")
        return raw
    async for chunk in stream:
        if not chunk:
            continue
        total += len(chunk)
        if total > int(limit):
            raise ValueError("request body too large")
        chunks.append(chunk)
    return b"".join(chunks)


def require_mutation_guards(request, claims):
    # type: (Request, Dict) -> Optional[JSONResponse]
    """Enforce JSON content-type + exact Origin + CSRF binding for mutations.

    Returns an error response on failure, else None. 403 on every failure
    (denied identity/CSRF per contract section 4).
    """
    rid = new_request_id()
    ctype = request.headers.get("content-type", "")
    # Mutations require JSON. Parse the media type before ';' parameters
    # and compare exact `application/json` (case-insensitive) so values
    # like `application/json-malicious` do not pass a substring check.
    media_type = (ctype or "").split(";")[0].strip().lower()
    if media_type != "application/json":
        return error_envelope(
            403, "forbidden",
            "mutations require Content-Type: application/json", "", rid)
    origin = request.headers.get("origin", "")
    if not auth_lib.check_origin(origin, settings.public_origin):
        return error_envelope(
            403, "forbidden", "origin not allowed", "", rid)
    provided = request.headers.get("x-csrf-token", "")
    subject = subject_of(claims)
    if not auth_lib.check_csrf(provided, subject, settings.csrf_secret):
        return error_envelope(
            403, "forbidden", "invalid CSRF token", "", rid)
    if not check_body_limit(request):
        return error_envelope(
            422, "invalid_request", "request body too large", "", rid)
    return None


def classify_tool_id(tool_id):
    # type: (str) -> str
    """Return 'ok', 'claude', or 'unknown'."""
    if tool_id in TOOL_IDS:
        return "ok"
    if tool_id == "claude":
        return "claude"
    return "unknown"


def parse_limit(raw, default=25, max_n=100):
    # type: (Optional[str], int, int) -> int
    """Parse a limit query param. Raises ValueError on invalid input."""
    if raw is None or raw == "":
        return default
    value = int(str(raw).strip())
    if value <= 0:
        raise ValueError("limit must be positive")
    if value > max_n:
        return max_n
    return value


def parse_after(raw, default=0):
    # type: (Optional[str], int) -> int
    if raw is None or raw == "":
        return default
    value = int(str(raw).strip())
    if value < 0:
        raise ValueError("after must be >= 0")
    return value


def is_valid_uuid(value):
    # type: (str) -> bool
    try:
        uuid.UUID(str(value))
        return True
    except Exception:
        return False

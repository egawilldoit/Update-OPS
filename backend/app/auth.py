"""Cloudflare Access JWT validation + CSRF/Origin gates. Fail closed.

Validates ``Cf-Access-Jwt-Assertion`` (header, primary) with fallback to the
``CF_Authorization`` cookie. RS256 only, fixed audience, configured issuer,
expiry enforced, owner identity allow-listed. JWKS fetched only from the
configured ``{team_domain}/cdn-cgi/access/certs`` issuer; cached with TTL and
refreshed on key-id miss (rotation). Never trusts an email header alone.

CSRF tokens are HMAC(secret, subject) — stateless, survive API restart, bound
to the authenticated subject. Mutations additionally require an exact allowed
Origin and JSON content type. Python 3.10 compatible.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.request
from typing import Dict, List, Optional, Tuple

import jwt
from jwt.algorithms import RSAAlgorithm

REQUIRED_ALG = "RS256"
CLOCK_SKEW_S = 60

_jwks_cache = {"fetched_at": 0.0, "keys": []}  # type: dict


def certs_url(team_domain):
    # type: (str) -> str
    return team_domain.rstrip("/") + "/cdn-cgi/access/certs"


def fetch_jwks(team_domain, ttl_s=600, force=False):
    # type: (str, int, bool) -> list
    now = time.time()
    if not force and _jwks_cache["keys"] and (now - _jwks_cache["fetched_at"]) < ttl_s:
        return _jwks_cache["keys"]
    url = certs_url(team_domain)
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    keys = []
    for key_dict in payload.get("keys", []):
        try:
            keys.append(RSAAlgorithm.from_jwk(json.dumps(key_dict)))
        except Exception:
            continue
    if not keys:
        raise ValueError("no usable JWKS keys")
    _jwks_cache["fetched_at"] = now
    _jwks_cache["keys"] = keys
    return keys


def _fail(reason):
    # type: (str) -> Tuple[None, str]
    return None, reason


def validate_access_token(token, team_domain, audience, owner_emails, ttl_s=600):
    # type: (str, str, str, List[str], int) -> Tuple[Optional[Dict], str]
    """Return (claims, error). claims is None on any failure (fail closed)."""
    if not token or not team_domain or not audience or not owner_emails:
        return _fail("not_configured_or_missing_token")
    try:
        header = jwt.get_unverified_header(token)
    except Exception:
        return _fail("malformed_token")
    if header.get("alg") != REQUIRED_ALG:
        return _fail("bad_algorithm")
    keys = []
    try:
        keys = fetch_jwks(team_domain, ttl_s=ttl_s)
    except Exception:
        return _fail("jwks_unavailable")
    last_err = "invalid_token"
    for key in keys:
        try:
            claims = jwt.decode(
                token,
                key=key,
                algorithms=[REQUIRED_ALG],
                audience=audience,
                issuer=team_domain,
                leeway=CLOCK_SKEW_S,
                options={"require": ["exp", "iss", "aud"]},
            )
            email = str(claims.get("email", "")).lower()
            allowed = [e.lower() for e in owner_emails]
            if email not in allowed:
                return _fail("identity_not_authorized")
            return claims, ""
        except jwt.ExpiredSignatureError:
            return _fail("expired")
        except jwt.InvalidAudienceError:
            return _fail("wrong_audience")
        except jwt.InvalidIssuerError:
            return _fail("wrong_issuer")
        except Exception as exc:
            last_err = "invalid_token:%s" % type(exc).__name__
            continue
    # Refresh once for rotation, then fail closed.
    try:
        keys = fetch_jwks(team_domain, ttl_s=ttl_s, force=True)
    except Exception:
        return None, last_err
    for key in keys:
        try:
            claims = jwt.decode(
                token, key=key, algorithms=[REQUIRED_ALG],
                audience=audience, issuer=team_domain,
                leeway=CLOCK_SKEW_S,
                options={"require": ["exp", "iss", "aud"]},
            )
            email = str(claims.get("email", "")).lower()
            if email not in [e.lower() for e in owner_emails]:
                return _fail("identity_not_authorized")
            return claims, ""
        except Exception:
            continue
    return None, last_err


def mint_csrf(subject, secret):
    # type: (str, str) -> str
    mac = hmac.new(secret.encode("utf-8"), subject.encode("utf-8"),
                   hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode("ascii").rstrip("=")


def check_csrf(provided, subject, secret):
    # type: (str, str, str) -> bool
    if not provided or not subject or not secret:
        return False
    expected = mint_csrf(subject, secret)
    return hmac.compare_digest(provided, expected)


def check_origin(origin, public_origin):
    # type: (str, str) -> bool
    if not public_origin:
        return False
    return origin == public_origin

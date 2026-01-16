import base64
import hashlib
import hmac
import json
import time
import urllib.parse
import urllib.request
from typing import Optional


# =========================
# Cognito configuration
# (values injected at deploy time by CDK)
# =========================
COGNITO_DOMAIN = "__COGNITO_DOMAIN__"
COGNITO_CLIENT_ID = "__COGNITO_CLIENT_ID__"
COGNITO_CLIENT_SECRET = "__COGNITO_CLIENT_SECRET__"
REDIRECT_URI = "__REDIRECT_URI__"
EXPECTED_ISSUER = "__COGNITO_ISSUER__"

# =========================
# Static configuration
# (must be constants for Lambda@Edge)
# =========================
COOKIE_NAME = "hp_id_token"
COOKIE_MAX_AGE = 3600        # seconds
COOKIE_PATH = "/"
SCOPES = "openid email"

# Cache JWKS in the execution environment (best-effort)
_JWKS_CACHE = {"fetched_at": 0, "jwks": None}
_JWKS_TTL_SECONDS = 3600


def handler(event, context):
    request = event["Records"][0]["cf"]["request"]
    uri = request.get("uri", "/")
    qs = request.get("querystring", "")
    headers = request.get("headers", {})

    # OAuth callback handler
    if uri == "/_auth/callback":
        return _handle_callback(qs)

    # Require auth for everything else
    id_token = _get_cookie(headers, COOKIE_NAME)
    if not id_token:
        return _redirect_to_login(uri, qs)

    try:
        claims = _verify_jwt_rs256(id_token)
    except Exception:
        return _redirect_to_login(uri, qs)

    # Optional: enforce issuer + audience already done in _verify_jwt_rs256
    # If you later want group checks for UI, you'd read claims.get("cognito:groups")

    return request


# -------------------------
# OAuth: callback + cookies
# -------------------------

def _handle_callback(querystring: str):
    params = urllib.parse.parse_qs(querystring or "", keep_blank_values=True)
    code = (params.get("code") or [None])[0]
    state = (params.get("state") or [None])[0]

    if not code:
        return _resp(400, "Missing code")

    target = "/"
    if state:
        try:
            target = _b64url_decode_str(state)
            if not target.startswith("/"):
                target = "/"
        except Exception:
            target = "/"

    token_resp = _exchange_code_for_tokens(code)
    id_token = token_resp.get("id_token")
    if not id_token:
        return _resp(500, "Token exchange failed")

    # Validate token immediately before setting it
    _verify_jwt_rs256(id_token)

    return {
        "status": "302",
        "statusDescription": "Authenticated",
        "headers": {
            "location": [{"key": "Location", "value": target}],
            "set-cookie": [
                {
                    "key": "Set-Cookie",
                    "value": _make_cookie(COOKIE_NAME, id_token),
                }
            ],
            "cache-control": [{"key": "Cache-Control", "value": "no-store"}],
        },
    }


def _exchange_code_for_tokens(code: str) -> dict:
    url = f"https://{COGNITO_DOMAIN}/oauth2/token"
    body = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "client_id": COGNITO_CLIENT_ID,
            "code": code,
            "redirect_uri": REDIRECT_URI,
        }
    ).encode("utf-8")

    basic = base64.b64encode(
        f"{COGNITO_CLIENT_ID}:{COGNITO_CLIENT_SECRET}".encode("utf-8")
    ).decode("utf-8")

    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("Authorization", f"Basic {basic}")

    with urllib.request.urlopen(req, timeout=5) as resp:
        payload = resp.read().decode("utf-8")
        return json.loads(payload)


def _redirect_to_login(uri: str, querystring: str):
    original = uri
    if querystring:
        original = f"{uri}?{querystring}"

    state = _b64url_encode_str(original)

    # Cognito Hosted UI uses space-separated scopes in query
    scope_qs = urllib.parse.quote(SCOPES)

    login_url = (
        f"https://{COGNITO_DOMAIN}/login"
        f"?client_id={urllib.parse.quote(COGNITO_CLIENT_ID)}"
        f"&response_type=code"
        f"&scope={scope_qs}"
        f"&redirect_uri={urllib.parse.quote(REDIRECT_URI)}"
        f"&state={urllib.parse.quote(state)}"
    )

    return {
        "status": "302",
        "statusDescription": "Redirect to login",
        "headers": {
            "location": [{"key": "Location", "value": login_url}],
            "cache-control": [{"key": "Cache-Control", "value": "no-store"}],
        },
    }


def _make_cookie(name: str, value: str) -> str:
    # Secure cookie for app domain
    # SameSite=Lax supports typical Cognito redirects
    return (
        f"{name}={value}; "
        f"Max-Age={COOKIE_MAX_AGE}; "
        f"Path={COOKIE_PATH}; "
        f"HttpOnly; Secure; SameSite=Lax"
    )


def _get_cookie(headers: dict, cookie_name: str) -> Optional[str]:
    cookie_headers = headers.get("cookie")
    if not cookie_headers:
        return None

    # CloudFront provides cookies as a list of {key,value} dicts
    cookie_str = "; ".join([h.get("value", "") for h in cookie_headers if h.get("value")])
    for part in cookie_str.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        if k.strip() == cookie_name:
            return v.strip()
    return None


def _resp(status_code: int, body: str):
    return {
        "status": str(status_code),
        "statusDescription": "Error",
        "headers": {"content-type": [{"key": "Content-Type", "value": "text/plain"}]},
        "body": body,
    }


# -------------------------
# JWT verification (RS256)
# -------------------------

def _verify_jwt_rs256(jwt: str) -> dict:
    header_b64, payload_b64, sig_b64 = jwt.split(".")
    header = json.loads(_b64url_decode_bytes(header_b64).decode("utf-8"))
    claims = json.loads(_b64url_decode_bytes(payload_b64).decode("utf-8"))

    if header.get("alg") != "RS256":
        raise ValueError("Unsupported alg")

    kid = header.get("kid")
    if not kid:
        raise ValueError("Missing kid")

    # Basic claim checks
    now = int(time.time())
    exp = int(claims.get("exp", 0))
    if exp and now >= exp:
        raise ValueError("Token expired")

    iss = claims.get("iss")
    if not iss or iss != EXPECTED_ISSUER:
        raise ValueError("Bad issuer")

    aud = claims.get("aud")
    if aud != COGNITO_CLIENT_ID:
        raise ValueError("Bad audience")

    jwks = _get_jwks(iss)
    jwk = _find_jwk(jwks, kid)

    signing_input = f"{header_b64}.{payload_b64}".encode("utf-8")
    signature = _b64url_decode_bytes(sig_b64)

    _verify_rs256_signature(signing_input, signature, jwk)
    return claims


def _get_jwks(iss: str) -> dict:
    now = int(time.time())
    if _JWKS_CACHE["jwks"] and (now - _JWKS_CACHE["fetched_at"] < _JWKS_TTL_SECONDS):
        return _JWKS_CACHE["jwks"]

    jwks_url = f"{iss}/.well-known/jwks.json"
    req = urllib.request.Request(jwks_url, method="GET")
    with urllib.request.urlopen(req, timeout=5) as resp:
        payload = resp.read().decode("utf-8")
        jwks = json.loads(payload)

    _JWKS_CACHE["jwks"] = jwks
    _JWKS_CACHE["fetched_at"] = now
    return jwks


def _find_jwk(jwks: dict, kid: str) -> dict:
    keys = jwks.get("keys", [])
    for k in keys:
        if k.get("kid") == kid:
            return k
    raise ValueError("JWK not found")


def _verify_rs256_signature(signing_input: bytes, signature: bytes, jwk: dict):
    # Minimal RSA PKCS#1 v1.5 verify using stdlib only.
    # JWK contains base64url n (modulus) and e (exponent).
    n = int.from_bytes(_b64url_decode_bytes(jwk["n"]), "big")
    e = int.from_bytes(_b64url_decode_bytes(jwk["e"]), "big")

    # RSA verify: m = s^e mod n
    s = int.from_bytes(signature, "big")
    m = pow(s, e, n)
    em = m.to_bytes((n.bit_length() + 7) // 8, "big")

    # Expected encoding: 0x00 0x01 PS 0x00 DER(SHA256) HASH
    # DER prefix for SHA256 DigestInfo:
    der_prefix = bytes.fromhex(
        "3031300d060960864801650304020105000420"
    )
    hashed = hashlib.sha256(signing_input).digest()
    t = der_prefix + hashed

    if len(em) < 11 or em[0] != 0x00 or em[1] != 0x01:
        raise ValueError("Bad signature padding")

    # Find 0x00 separator
    try:
        sep_idx = em.index(b"\x00", 2)
    except ValueError:
        raise ValueError("Bad signature format")

    # All bytes between must be 0xFF
    ps = em[2:sep_idx]
    if any(b != 0xFF for b in ps) or len(ps) < 8:
        raise ValueError("Bad signature padding")

    recovered = em[sep_idx + 1 :]
    if not hmac.compare_digest(recovered, t):
        raise ValueError("Signature mismatch")


def _b64url_decode_bytes(s: str) -> bytes:
    s += "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s.encode("utf-8"))


def _b64url_encode_str(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode("utf-8")).decode("utf-8").rstrip("=")


def _b64url_decode_str(s: str) -> str:
    return _b64url_decode_bytes(s).decode("utf-8")
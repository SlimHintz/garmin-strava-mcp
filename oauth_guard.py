"""Auth for the running-data MCP endpoint (/tools).

- /tools requires `Authorization: Bearer <API_KEY>` or an OAuth access token issued below.
- Minimal OAuth 2.1 + PKCE so remote MCP clients' custom connectors can authorize: /oauth/authorize asks for the API key once, then issues a 30-day token.
- Failed authorize attempts are rate limited per IP (brute-force guard) and logged.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import sqlite3
import time
from datetime import timedelta
from pathlib import Path

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

PUBLIC_URL = os.environ.get("PUBLIC_URL", "http://localhost:8787").rstrip("/")
DB = Path(os.environ.get("OAUTH_DB_PATH", Path(__file__).parent / "oauth.db"))
TOKEN_TTL = timedelta(days=30)
CODE_TTL = timedelta(minutes=10)
MAX_FAILS, FAIL_WINDOW = 5, 900  # 5 wrong keys per 15 minutes per IP

log = logging.getLogger("running.auth")
_fails: dict[str, list[float]] = {}


def api_key() -> str:
    k = os.environ.get("API_KEY", "")
    if len(k) < 20:
        raise RuntimeError("API_KEY env var missing or too short")
    return k


def _db() -> sqlite3.Connection:
    c = sqlite3.connect(DB, timeout=10)
    c.row_factory = sqlite3.Row
    c.executescript("""
      CREATE TABLE IF NOT EXISTS clients(id TEXT PRIMARY KEY, name TEXT, redirect_uris TEXT);
      CREATE TABLE IF NOT EXISTS codes(code TEXT PRIMARY KEY, client_id TEXT, redirect_uri TEXT,
        challenge TEXT, method TEXT, expires REAL, used INTEGER DEFAULT 0);
      CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY, client_id TEXT, expires REAL);""")
    return c


def key_ok(provided: str) -> bool:
    return bool(provided) and hmac.compare_digest(provided, api_key())


def token_ok(provided: str) -> bool:
    if key_ok(provided):
        return True
    if not provided:
        return False
    with _db() as c:
        r = c.execute("SELECT expires FROM tokens WHERE token=?", (provided,)).fetchone()
    return bool(r and r["expires"] > time.time())


class GuardTools:
    """ASGI middleware: everything under /tools needs a valid bearer token."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith("/tools"):
            auth = dict(scope.get("headers") or []).get(b"authorization", b"").decode("latin-1")
            provided = auth[7:] if auth.startswith("Bearer ") else ""
            if not token_ok(provided):
                ip = (scope.get("client") or ["?"])[0]
                log.warning("unauthorized /tools request from %s", ip)
                resp = JSONResponse({"error": "unauthorized"}, status_code=401, headers={
                    "WWW-Authenticate": f'Bearer resource_metadata="{PUBLIC_URL}'
                                        '/.well-known/oauth-protected-resource"'})
                await resp(scope, receive, send)
                return
        await self.app(scope, receive, send)


# ---- OAuth endpoints ------------------------------------------------------------


async def as_metadata(request: Request) -> Response:
    return JSONResponse({
        "issuer": PUBLIC_URL,
        "authorization_endpoint": f"{PUBLIC_URL}/oauth/authorize",
        "token_endpoint": f"{PUBLIC_URL}/oauth/token",
        "registration_endpoint": f"{PUBLIC_URL}/oauth/register",
        "response_types_supported": ["code"], "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"]})


async def pr_metadata(request: Request) -> Response:
    return JSONResponse({"resource": f"{PUBLIC_URL}/tools", "authorization_servers": [PUBLIC_URL]})


async def register(request: Request) -> Response:
    body = await request.json()
    uris = body.get("redirect_uris") or []
    if not uris or not all(isinstance(u, str) and u.startswith("https://") for u in uris):
        return JSONResponse({"error": "invalid_client_metadata"}, status_code=400)
    cid = secrets.token_urlsafe(24)
    with _db() as c:
        c.execute("INSERT INTO clients VALUES(?,?,?)", (cid, str(body.get("client_name", ""))[:100],
                                                       json.dumps(uris)))
    return JSONResponse({"client_id": cid, "client_name": body.get("client_name"),
                         "redirect_uris": uris, "token_endpoint_auth_method": "none",
                         "grant_types": ["authorization_code"], "response_types": ["code"]},
                        status_code=201)


def _form(client_name, redirect_uri, hidden, error=None):
    e = html.escape
    fields = "".join(f'<input type="hidden" name="{e(k)}" value="{e(v)}">' for k, v in hidden.items())
    err = f'<p style="color:#ff7a66">{e(error)}</p>' if error else ""
    return HTMLResponse(f"""<!doctype html><html><head><meta name="viewport"
content="width=device-width,initial-scale=1"><title>Running Data - Authorize</title><style>
body{{font-family:-apple-system,system-ui,sans-serif;background:#0b1020;color:#eef1fb;display:flex;
align-items:center;justify-content:center;min-height:100vh;margin:0}}.c{{background:#141b33;
padding:2rem;border-radius:14px;max-width:360px;width:90%}}input[type=password]{{width:100%;
padding:.6rem;border-radius:8px;border:1px solid #344070;background:#0b1020;color:#eef1fb;
box-sizing:border-box}}button{{width:100%;padding:.7rem;margin-top:1rem;border-radius:8px;
border:0;background:#4cd38a;font-weight:600}}p.s{{color:#9aa6c8;font-size:.85rem;
word-break:break-all}}</style></head><body><div class="c"><h3>🏃 {e(client_name or "An app")}
wants your running data</h3><p class="s">Redirecting to: {e(redirect_uri)}</p>{err}
<form method="post">{fields}<label>Running API key</label><input type="password" name="key"
autofocus required><button>Authorize</button></form></div></body></html>""",
                        status_code=401 if error else 200)


async def authorize(request: Request) -> Response:
    params = request.query_params if request.method == "GET" else await request.form()
    cid, ruri = params.get("client_id", ""), params.get("redirect_uri", "")
    hidden = {"client_id": cid, "redirect_uri": ruri, "state": params.get("state", ""),
              "code_challenge": params.get("code_challenge", ""),
              "code_challenge_method": params.get("code_challenge_method", "S256")}
    with _db() as c:
        cl = c.execute("SELECT * FROM clients WHERE id=?", (cid,)).fetchone()
    if not cl or ruri not in json.loads(cl["redirect_uris"]):
        return HTMLResponse("Unknown client or redirect_uri.", status_code=400)
    if request.method == "GET":
        return _form(cl["name"], ruri, hidden)
    ip = request.client.host if request.client else "?"
    now = time.time()
    _fails[ip] = [t for t in _fails.get(ip, []) if now - t < FAIL_WINDOW]
    if len(_fails[ip]) >= MAX_FAILS:
        log.warning("authorize locked out for %s", ip)
        return HTMLResponse("Too many attempts. Try again in 15 minutes.", status_code=429)
    if not key_ok(params.get("key", "")):
        _fails[ip].append(now)
        log.warning("authorize failure from %s", ip)
        return _form(cl["name"], ruri, hidden, "Incorrect key.")
    if hidden["code_challenge_method"] != "S256" or not hidden["code_challenge"]:
        return HTMLResponse("PKCE S256 required.", status_code=400)
    code = secrets.token_urlsafe(32)
    with _db() as c:
        c.execute("INSERT INTO codes(code, client_id, redirect_uri, challenge, method, expires) "
                  "VALUES(?,?,?,?,?,?)", (code, cid, ruri, hidden["code_challenge"], "S256",
                                          now + CODE_TTL.total_seconds()))
    sep = "&" if "?" in ruri else "?"
    return RedirectResponse(f"{ruri}{sep}code={code}&state={hidden['state']}", status_code=302)


async def token(request: Request) -> Response:
    f = await request.form()
    if f.get("grant_type") != "authorization_code":
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)
    with _db() as c:
        r = c.execute("SELECT * FROM codes WHERE code=?", (f.get("code", ""),)).fetchone()
        verifier = f.get("code_verifier", "")
        digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
        if (not r or r["used"] or r["expires"] < time.time()
                or r["client_id"] != f.get("client_id") or r["redirect_uri"] != f.get("redirect_uri")
                or not verifier or not hmac.compare_digest(digest.decode(), r["challenge"])):
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        c.execute("UPDATE codes SET used=1 WHERE code=?", (r["code"],))
        tok = secrets.token_urlsafe(32)
        c.execute("INSERT INTO tokens VALUES(?,?,?)", (tok, r["client_id"],
                                                     time.time() + TOKEN_TTL.total_seconds()))
    return JSONResponse({"access_token": tok, "token_type": "Bearer",
                         "expires_in": int(TOKEN_TTL.total_seconds())})


ROUTES = [
    Route("/.well-known/oauth-authorization-server", as_metadata),
    Route("/.well-known/oauth-protected-resource", pr_metadata),
    Route("/.well-known/oauth-protected-resource/tools", pr_metadata),
    Route("/oauth/register", register, methods=["POST"]),
    Route("/oauth/authorize", authorize, methods=["GET", "POST"]),
    Route("/oauth/token", token, methods=["POST"]),
]


def install(app) -> None:
    """Wire into the FastAPI app: OAuth routes + the /tools guard (outermost middleware)."""
    api_key()  # fail fast if the key isn't configured
    for r in ROUTES:
        app.router.routes.insert(0, r)
    app.add_middleware(GuardTools)

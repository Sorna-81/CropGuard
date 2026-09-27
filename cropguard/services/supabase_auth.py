"""Server-side Supabase Auth adapter. Privileged keys never leave this module."""
import logging
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from fastapi import HTTPException
from .. import config

log = logging.getLogger("cropguard.supabase.auth")
_http = requests.Session()
# A stale/global HTTPS_PROXY was breaking every Auth verification call. Use a
# proxy only when explicitly configured for Supabase; certificate checks stay on.
_http.trust_env = False
if config.SUPABASE_PROXY:
    _http.proxies.update({"http": config.SUPABASE_PROXY, "https": config.SUPABASE_PROXY})
_retry = Retry(total=2, connect=2, read=0, status=0, backoff_factor=0.25, allowed_methods=frozenset({"GET"}))
_http.mount("https://", HTTPAdapter(max_retries=_retry))

def _verify_setting():
    # requests verifies certificates by default. An optional CA bundle supports
    # managed/corporate TLS roots without weakening certificate validation.
    return config.SUPABASE_CA_BUNDLE or True

def configuration_error():
    if not config.SUPABASE_DATABASE_ENABLED:
        return None
    missing = [name for name, value in (("SUPABASE_URL", config.SUPABASE_URL),
        ("SUPABASE_PUBLISHABLE_KEY", config.SUPABASE_PUBLISHABLE_KEY),
        ("SUPABASE_SECRET_KEY", config.SUPABASE_SECRET_KEY)) if not value]
    return "Supabase authentication is misconfigured. Set " + ", ".join(missing) + "." if missing else None

def require_configuration():
    message = configuration_error()
    if message:
        raise HTTPException(503, message)

def enabled():
    # PostgreSQL mode must never silently fall through to local JWT/password auth.
    return config.SUPABASE_DATABASE_ENABLED

def _headers(access_token=None):
    headers = {"apikey": config.SUPABASE_PUBLISHABLE_KEY, "Content-Type": "application/json"}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    return headers

def _request(method, path, *, payload=None, access_token=None):
    require_configuration()
    if not enabled():
        raise HTTPException(503, "Supabase Auth is not configured for the PostgreSQL database")
    try:
        response = _http.request(method, f"{config.SUPABASE_URL}/auth/v1/{path}", headers=_headers(access_token), json=payload, timeout=(5, 12), verify=_verify_setting())
    except requests.RequestException as exc:
        log.warning("Supabase Auth request unavailable (%s)", type(exc).__name__)
        raise HTTPException(503, "Supabase Auth is unreachable. Check backend network access and the configured TLS certificate bundle.")
    if response.status_code >= 400:
        if response.status_code in (400, 401, 422):
            raise HTTPException(response.status_code, "Email or password is invalid, or email confirmation is required")
        log.error("Supabase Auth returned HTTP %s", response.status_code)
        raise HTTPException(503, "Authentication service could not complete the request")
    return response.json() if response.content else {}

def signup(email, password, full_name):
    return _request("POST", "signup", payload={"email": email, "password": password, "data": {"full_name": full_name}})

def login(email, password):
    return _request("POST", "token?grant_type=password", payload={"email": email, "password": password})

def refresh(refresh_token):
    return _request("POST", "token?grant_type=refresh_token", payload={"refresh_token": refresh_token})

def logout(access_token):
    return _request("POST", "logout", access_token=access_token)

def recover(email):
    return _request("POST", "recover", payload={"email": email})

def update_password(access_token, password):
    return _request("PUT", "user", payload={"password": password}, access_token=access_token)

def verify(access_token):
    require_configuration()
    try:
        response = _http.get(f"{config.SUPABASE_URL}/auth/v1/user", headers=_headers(access_token), timeout=(5, 12), verify=_verify_setting())
    except requests.RequestException as exc:
        log.warning("Supabase session verification unavailable (%s)", type(exc).__name__)
        raise HTTPException(503, "Supabase Auth is unreachable. Check backend network access and the configured TLS certificate bundle.")
    if response.status_code != 200:
        return None
    try:
        data = response.json()
    except ValueError:
        return None
    if not data.get("id") or not data.get("email"):
        return None
    return data
